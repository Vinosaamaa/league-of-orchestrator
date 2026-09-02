#!/usr/bin/env python3
"""OS-managed multi-Squad watcher install, restart, and rollback acceptance."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import time
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.persistent_supervisor import (  # noqa: E402
    PersistentSupervisor,
    attach_shotcaller,
    send_supervisor_message,
    supervisor_status,
)
from league.supervisor_service import (  # noqa: E402
    MANIFEST_SCHEMA,
    SERVICE_LABEL,
    SupervisorServiceInstaller,
    render_launchd_plist,
)
from league.storage import StorageRefusal  # noqa: E402
from lifecycle_fakes import FakeDeliveryAdapter  # noqa: E402
from test_multisquad_supervisor import (  # noqa: E402
    CountingRuntimeObserver,
    FakeWakeAdapter,
    _multisquad_state,
)


class SyntheticLaunchd:
    """Own real supervisor threads without touching the owner machine."""

    def __init__(self) -> None:
        self.loaded = False
        self.runtime: PersistentSupervisor | None = None
        self.thread: threading.Thread | None = None
        self.errors: list[BaseException] = []
        self.starts = 0
        self.state_root: Path | None = None
        self.fail_bootstraps = 0

    def is_loaded(self, label: str) -> bool:
        assert label == SERVICE_LABEL
        return self.loaded

    def _start(self, plist_path: Path) -> None:
        value = plistlib.loads(plist_path.read_bytes())
        assert value["Label"] == SERVICE_LABEL
        assert value["ProgramArguments"][1:] == ["service-run"]
        assert "--shotcaller" not in value["ProgramArguments"]
        self.state_root = Path(value["EnvironmentVariables"]["LEAGUE_STATE_ROOT"])
        runtime = PersistentSupervisor(
            self.state_root,
            lease_seconds=0.8,
            renew_seconds=0.2,
            recovery_seconds=30,
            wake_adapter=FakeWakeAdapter(),
            delivery_adapter=FakeDeliveryAdapter(),
            runtime_observer=CountingRuntimeObserver(),
        )

        def run() -> None:
            try:
                runtime.run(emit_ready=False)
            except BaseException as exc:  # pragma: no cover - surfaced by the test
                self.errors.append(exc)

        thread = threading.Thread(target=run, name="synthetic-launchd-supervisor")
        thread.start()
        assert runtime.ready.wait(timeout=5), self.errors
        self.runtime = runtime
        self.thread = thread
        self.loaded = True
        self.starts += 1

    def _stop(self) -> None:
        runtime = self.runtime
        thread = self.thread
        if runtime is not None and thread is not None and thread.is_alive():
            send_supervisor_message(f"unix:{runtime.socket_path}", {"kind": "stop"})
            thread.join(timeout=5)
            assert not thread.is_alive(), self.errors
        self.runtime = None
        self.thread = None

    def bootstrap(self, label: str, plist_path: Path) -> None:
        assert label == SERVICE_LABEL and not self.loaded
        if self.fail_bootstraps:
            self.fail_bootstraps -= 1
            raise StorageRefusal(
                "synthetic_service_start_failed", "synthetic launchd start failed"
            )
        self._start(plist_path)

    def kickstart(self, label: str) -> None:
        assert label == SERVICE_LABEL and self.loaded and self.state_root is not None
        # launchctl -k stops the current process and starts the same installed job.
        plist_path = self.plist_path
        self._stop()
        self._start(plist_path)

    def bootout(self, label: str) -> None:
        assert label == SERVICE_LABEL and self.loaded
        self._stop()
        self.loaded = False

    @property
    def plist_path(self) -> Path:
        assert self._plist_path is not None
        return self._plist_path

    @plist_path.setter
    def plist_path(self, value: Path) -> None:
        self._plist_path = value

    _plist_path: Path | None = None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_launchd_environment_starts_the_canonical_watcher(root: Path) -> None:
    state, store = _multisquad_state(root, "launchd-environment-state")
    store.close()
    (state.parent / "league-writer-pointer.json").write_text(
        '{"writer":"sqlite"}\n', encoding="utf-8"
    )
    agent_watcher = (ROOT / "bin/agent-watcher").resolve()
    template = (ROOT / "config/league-supervisor.launchd.plist.in").resolve()

    rendered, _ = render_launchd_plist(template, agent_watcher, state.resolve())
    value = plistlib.loads(rendered)
    environment = value["EnvironmentVariables"]
    assert environment["LEAGUE_WRITER_POINTER"] == os.fspath(
        state.resolve().parent / "league-writer-pointer.json"
    )
    assert Path(environment["PATH"].split(os.pathsep)[0]).resolve() == Path(
        sys.executable
    ).resolve().parent

    completed = subprocess.run(
        [str(agent_watcher), "service-status"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    status = json.loads(completed.stdout)
    assert status["schema"] == "league.supervisor-service-status.v1"
    assert status["binding_count"] == 3


def test_install_restart_and_exact_rollback(root: Path) -> None:
    state, store = _multisquad_state(root, "state")
    store.close()
    agent_watcher = (ROOT / "bin/agent-watcher").resolve()
    template = (ROOT / "config/league-supervisor.launchd.plist.in").resolve()
    launch_agents = root / "Library" / "LaunchAgents"
    plist = (launch_agents / "io.league-of-orchestrator.supervisor.plist").resolve()
    backup = Path(f"{plist}.league-backup")
    manifest = Path(f"{plist}.league-install.json")
    prior = b"synthetic prior user plist\n"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(prior)

    launchd = SyntheticLaunchd()
    launchd.plist_path = plist
    installer = SupervisorServiceInstaller(
        state_root=state.resolve(),
        agent_watcher=agent_watcher,
        template_path=template,
        plist_path=plist,
        backup_path=backup,
        manifest_path=manifest,
        service_manager=launchd,
    )
    installed = installer.install(
        expected_agent_watcher_sha256=sha256(agent_watcher),
        expected_template_sha256=sha256(template),
    )
    assert installed["schema"] == MANIFEST_SCHEMA
    assert installed["live"] and installed["binding_count"] == 3
    assert installed["rollback_ready"] and not installed["idempotent"]
    assert launchd.loaded and launchd.starts == 1
    first = supervisor_status(state)
    assert first["live"] and first["binding_count"] == 3
    first_fences = {
        item["actor_agent_id"]: item["fence"] for item in first["bindings"]
    }

    backup.unlink()
    try:
        installer.install(
            expected_agent_watcher_sha256=sha256(agent_watcher),
            expected_template_sha256=sha256(template),
        )
    except StorageRefusal as exc:
        assert exc.code == "supervisor_service_backup_mismatch"
    else:
        raise AssertionError("idempotent install claimed a missing rollback backup")
    backup.write_bytes(b"changed synthetic rollback bytes\n")
    try:
        installer.install(
            expected_agent_watcher_sha256=sha256(agent_watcher),
            expected_template_sha256=sha256(template),
        )
    except StorageRefusal as exc:
        assert exc.code == "supervisor_service_backup_mismatch"
    else:
        raise AssertionError("idempotent install claimed a changed rollback backup")
    backup.write_bytes(prior)
    exact_retry = installer.install(
        expected_agent_watcher_sha256=sha256(agent_watcher),
        expected_template_sha256=sha256(template),
    )
    assert exact_retry["idempotent"] and launchd.starts == 1

    restarted = installer.start()
    assert restarted["live"] and restarted["restarted"]
    assert launchd.starts == 2
    second = supervisor_status(state)
    second_fences = {
        item["actor_agent_id"]: item["fence"] for item in second["bindings"]
    }
    assert all(
        second_fences[actor] > first_fences[actor] for actor in first_fences
    )

    rolled_back = installer.rollback(
        expected_installed_plist_sha256=installed["installed_plist_sha256"],
        expected_backup_sha256=hashlib.sha256(prior).hexdigest(),
    )
    assert rolled_back["rolled_back"] and not rolled_back["service_loaded"]
    assert rolled_back["restored_plist_sha256"] == hashlib.sha256(prior).hexdigest()
    assert plist.read_bytes() == prior and not launchd.loaded
    assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == "rolled_back"
    assert not launchd.errors
    after = supervisor_status(state)
    assert not after["live"]
    assert {item["reason"] for item in after["bindings"]} == {
        "registration_missing"
    }
    try:
        attach_shotcaller(state, "Garen")
    except StorageRefusal as exc:
        assert exc.code == "supervisor_not_live"
        assert "service-start" in str(exc)
    else:
        raise AssertionError("attachment changed without the OS-managed service")


def test_install_refuses_unmanaged_live_process(root: Path) -> None:
    state, store = _multisquad_state(root, "unmanaged-state")
    store.close()
    runtime = PersistentSupervisor(
        state,
        lease_seconds=0.8,
        renew_seconds=0.2,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=FakeDeliveryAdapter(),
        runtime_observer=CountingRuntimeObserver(),
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            runtime.run(emit_ready=False)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=run, name="synthetic-unmanaged-supervisor")
    thread.start()
    assert runtime.ready.wait(timeout=5), errors
    agent_watcher = (ROOT / "bin/agent-watcher").resolve()
    template = (ROOT / "config/league-supervisor.launchd.plist.in").resolve()
    plist = (root / "unmanaged" / "service.plist").resolve()
    launchd = SyntheticLaunchd()
    launchd.plist_path = plist
    installer = SupervisorServiceInstaller(
        state_root=state.resolve(),
        agent_watcher=agent_watcher,
        template_path=template,
        plist_path=plist,
        backup_path=Path(f"{plist}.backup"),
        manifest_path=Path(f"{plist}.manifest"),
        service_manager=launchd,
    )
    try:
        try:
            installer.install(
                expected_agent_watcher_sha256=sha256(agent_watcher),
                expected_template_sha256=sha256(template),
            )
        except StorageRefusal as exc:
            assert exc.code == "supervisor_service_conflict"
        else:
            raise AssertionError("installer adopted a non-launchd service process")
        assert not plist.exists() and not launchd.loaded
    finally:
        send_supervisor_message(f"unix:{runtime.socket_path}", {"kind": "stop"})
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def test_failed_install_retries_only_after_exact_rollback(root: Path) -> None:
    for name, prior in (("absent", None), ("existing", b"prior plist\n")):
        case = root / name
        state, store = _multisquad_state(case, "state")
        store.close()
        agent_watcher = (ROOT / "bin/agent-watcher").resolve()
        template = (ROOT / "config/league-supervisor.launchd.plist.in").resolve()
        plist = (case / "service.plist").resolve()
        backup = Path(f"{plist}.backup")
        manifest = Path(f"{plist}.manifest")
        if prior is not None:
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_bytes(prior)
        launchd = SyntheticLaunchd()
        launchd.plist_path = plist
        launchd.fail_bootstraps = 1
        installer = SupervisorServiceInstaller(
            state_root=state.resolve(),
            agent_watcher=agent_watcher,
            template_path=template,
            plist_path=plist,
            backup_path=backup,
            manifest_path=manifest,
            service_manager=launchd,
        )

        try:
            installer.install(
                expected_agent_watcher_sha256=sha256(agent_watcher),
                expected_template_sha256=sha256(template),
            )
        except StorageRefusal as exc:
            assert exc.code == "synthetic_service_start_failed"
        else:
            raise AssertionError("synthetic launchd failure unexpectedly installed")
        assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == "rolled_back"
        assert (plist.read_bytes() if plist.exists() else None) == prior
        assert (backup.read_bytes() if backup.exists() else None) == prior

        retried = installer.install(
            expected_agent_watcher_sha256=sha256(agent_watcher),
            expected_template_sha256=sha256(template),
        )
        assert retried["live"] and not retried["idempotent"]
        rolled_back = installer.rollback(
            expected_installed_plist_sha256=retried["installed_plist_sha256"],
            expected_backup_sha256=(
                None if prior is None else hashlib.sha256(prior).hexdigest()
            ),
        )
        assert rolled_back["rolled_back"]
        assert (plist.read_bytes() if plist.exists() else None) == prior


def test_install_refuses_unapproved_source_without_side_effects(root: Path) -> None:
    state, store = _multisquad_state(root, "refusal-state")
    store.close()
    agent_watcher = (ROOT / "bin/agent-watcher").resolve()
    template = (ROOT / "config/league-supervisor.launchd.plist.in").resolve()
    plist = (root / "refusal" / "service.plist").resolve()
    launchd = SyntheticLaunchd()
    launchd.plist_path = plist
    installer = SupervisorServiceInstaller(
        state_root=state.resolve(),
        agent_watcher=agent_watcher,
        template_path=template,
        plist_path=plist,
        backup_path=Path(f"{plist}.backup"),
        manifest_path=Path(f"{plist}.manifest"),
        service_manager=launchd,
    )
    try:
        installer.install(
            expected_agent_watcher_sha256="0" * 64,
            expected_template_sha256=sha256(template),
        )
    except StorageRefusal as exc:
        assert exc.code == "supervisor_service_source_mismatch"
    else:
        raise AssertionError("unapproved service source was installed")
    assert not plist.exists() and not launchd.loaded and launchd.starts == 0


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-supervisor-service-") as temporary:
        root = Path(temporary)
        test_launchd_environment_starts_the_canonical_watcher(root / "environment")
        test_install_restart_and_exact_rollback(root / "lifecycle")
        test_install_refuses_unmanaged_live_process(root / "unmanaged")
        test_failed_install_retries_only_after_exact_rollback(root / "retry")
        test_install_refuses_unapproved_source_without_side_effects(root / "refusal")
    print(
        "PASS: launchd-owned multi-Squad service installs, starts, restarts, "
        "refuses unmanaged ownership, and rolls back exactly"
    )


if __name__ == "__main__":
    main()
