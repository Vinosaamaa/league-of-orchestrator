#!/usr/bin/env python3
"""Focused Pi runtime/provider launch, lineage, metadata, and restart tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.pi_launch import (  # noqa: E402
    HerdrPiLaunchAdapter,
    deterministic_pi_session_id,
    pi_start_arguments,
    resume_pi_after_restart,
)
from league.pi_session_migration import (  # noqa: E402
    _inventory_identity,
    migrate_pi_session,
)
from league.real_cleanup import HerdrHarnessAdapter  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from league.request_services import AssignmentSpec  # noqa: E402
from request_lifecycle_fixture import LUX_ID, create_context  # noqa: E402
from storage_fixture import REPOSITORY, SHOTCALLER_ID  # noqa: E402


PARENT_ID = "11111111-1111-4111-8111-111111111111"
CHILD_ID = "22222222-2222-4222-8222-222222222222"


class FakePiHerdr:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.running = False
        self.endpoint = {
            "workspace_id": "w1",
            "tab_id": "w1:t84",
            "pane_id": "w1:p84",
            "terminal_id": "term_pi_84",
        }
        self.env: dict[str, str] = {}
        self.pi_arguments: list[str] = []
        self.session_id = ""
        self.session_path = ""
        self.parent_path: str | None = None
        self.start_count = 0
        self.calls: list[tuple[str, ...]] = []
        self.native_title_reads_remaining = 0
        self.agent_get_count = 0
        self.report_process_argv = True
        self.launch_metadata_available = True
        self.native_session_available = True

    @staticmethod
    def _completed(arguments, result, returncode=0):
        payload = {"result": result} if returncode == 0 else {"error": {"code": "not_found"}}
        return subprocess.CompletedProcess(arguments, returncode, json.dumps(payload) + "\n", "")

    @staticmethod
    def _pairs(arguments: list[str], flag: str) -> list[str]:
        return [arguments[index + 1] for index, item in enumerate(arguments[:-1]) if item == flag]

    def stop_for_restart(self) -> None:
        self.running = False
        self.pi_arguments = []
        self.launch_metadata_available = False
        self.native_session_available = False

    def _agent(self) -> dict:
        callsign = self.env["LEAGUE_CALLSIGN"]
        provider = self.env["LEAGUE_PROVIDER_KIND"]
        role = self.env["LEAGUE_LAUNCH_ROLE"]
        title = (
            callsign
            if role == "shotcaller"
            else f"{callsign} · {self.env['LEAGUE_PROJECT_CODE']}|{self.env['LEAGUE_TASK_LABEL']}"
        )
        tokens = {
            "runtime_kind": "pi",
            "provider_kind": provider,
            "role": role,
            "placement": self.env["LEAGUE_LAUNCH_PLACEMENT"],
            "sidebar_name": callsign,
            "project_code": self.env["LEAGUE_PROJECT_CODE"],
            "task_label": self.env["LEAGUE_TASK_LABEL"],
            "routing_alias": self.env["LEAGUE_ROUTING_ALIAS"],
            "session_id": self.session_id,
            "session_path": self.session_path,
            "thread_title": title,
            "activation_phase": "session_started",
        }
        if self.launch_metadata_available:
            tokens.update({
                "launch_runtime_kind": "pi",
                "launch_provider_kind": provider,
                "launch_role": role,
                "launch_placement": self.env["LEAGUE_LAUNCH_PLACEMENT"],
                "launch_callsign": callsign,
                "launch_project_code": self.env["LEAGUE_PROJECT_CODE"],
                "launch_task_label": self.env["LEAGUE_TASK_LABEL"],
                "launch_routing_alias": self.env["LEAGUE_ROUTING_ALIAS"],
                "launch_session_id": self.session_id,
                "launch_session_path_digest": __import__("hashlib").sha256(self.session_path.encode()).hexdigest(),
                "launch_descriptor_sha256": self.env["LEAGUE_LAUNCH_DESCRIPTOR_DIGEST"],
                "launch_activation_phase": "session_started",
            })
        if self.parent_path:
            tokens["parent_session_path"] = self.parent_path
            if self.launch_metadata_available:
                tokens["launch_parent_digest"] = __import__("hashlib").sha256(self.parent_path.encode()).hexdigest()
        value = {
            "agent": "pi",
            "agent_status": "idle",
            "interactive_ready": True,
            "name": self.env["LEAGUE_ROUTING_ALIAS"],
            "display_agent": provider,
            "metadata_source": "league:pi-launch:" + self.env["LEAGUE_LAUNCH_DESCRIPTOR_DIGEST"][:16],
            "cwd": self.env["LEAGUE_WORKTREE"],
            "foreground_cwd": self.env["LEAGUE_WORKTREE"],
            "terminal_title": title,
            "title": f"◇ Cursor | {title}",
            "tokens": tokens,
            "state_change_seq": 84,
            **self.endpoint,
        }
        if self.native_session_available:
            value["agent_session"] = {
                "agent": "pi", "kind": "path", "source": "herdr:pi", "value": self.session_path,
            }
        if self.native_title_reads_remaining:
            self.native_title_reads_remaining -= 1
            value["terminal_title"] = f"π - {self.session_id} - worktree"
        return value

    def run(self, arguments, *, timeout_seconds=30):
        arguments = list(arguments)
        self.calls.append(tuple(arguments))
        if arguments[1:3] == ["tab", "create"]:
            for pair in self._pairs(arguments, "--env"):
                key, value = pair.split("=", 1)
                self.env[key] = value
            return self._completed(
                arguments,
                {
                    "tab": {"tab_id": self.endpoint["tab_id"]},
                    "root_pane": {
                        "pane_id": self.endpoint["pane_id"],
                        "terminal_id": self.endpoint["terminal_id"],
                    },
                },
            )
        if arguments[1:3] == ["pane", "split"]:
            for pair in self._pairs(arguments, "--env"):
                key, value = pair.split("=", 1)
                self.env[key] = value
            return self._completed(
                arguments,
                {
                    "tab_id": self.endpoint["tab_id"],
                    "pane": {
                        "pane_id": self.endpoint["pane_id"],
                        "terminal_id": self.endpoint["terminal_id"],
                    },
                },
            )
        if arguments[1:3] == ["agent", "start"]:
            self.start_count += 1
            self.pi_arguments = arguments[arguments.index("--") + 1 :]
            explicit = {
                "league-state-root": "LEAGUE_STATE_ROOT",
                "league-pane-id": "HERDR_PANE_ID",
                "league-watcher-command": "LEAGUE_WATCHER_COMMAND",
                "league-worktree": "LEAGUE_WORKTREE",
                "league-sandbox-profile": "LEAGUE_PI_SANDBOX_PROFILE",
                "league-runtime-kind": "LEAGUE_RUNTIME_KIND",
                "league-provider-kind": "LEAGUE_PROVIDER_KIND",
                "league-role": "LEAGUE_LAUNCH_ROLE",
                "league-placement": "LEAGUE_LAUNCH_PLACEMENT",
                "league-callsign": "LEAGUE_CALLSIGN",
                "league-project-code": "LEAGUE_PROJECT_CODE",
                "league-task-label": "LEAGUE_TASK_LABEL",
                "league-routing-alias": "LEAGUE_ROUTING_ALIAS",
                "league-descriptor-digest": "LEAGUE_LAUNCH_DESCRIPTOR_DIGEST",
            }
            for flag, key in explicit.items():
                option = f"--{flag}"
                if option in self.pi_arguments:
                    self.env[key] = self.pi_arguments[self.pi_arguments.index(option) + 1]
            if "--fork" in self.pi_arguments:
                self.session_id = CHILD_ID
                self.session_path = str((self.root / "child-session.jsonl").resolve())
                self.parent_path = self.pi_arguments[self.pi_arguments.index("--fork") + 1]
            elif "--session-id" in self.pi_arguments:
                self.session_id = self.pi_arguments[self.pi_arguments.index("--session-id") + 1]
                self.session_path = str((self.root / f"{self.session_id}.jsonl").resolve())
                self.parent_path = None
            else:
                self.session_path = self.pi_arguments[self.pi_arguments.index("--session") + 1]
            self.running = True
            return self._completed(arguments, {"accepted": True})
        if arguments[1:3] == ["agent", "get"]:
            self.agent_get_count += 1
            return self._completed(
                arguments,
                {"agent": self._agent()} if self.running else {},
                0 if self.running else 1,
            )
        if arguments[1:3] == ["pane", "process-info"]:
            process = {
                "name": "node", "argv0": "pi", "pid": 8401,
                "cwd": self.env["LEAGUE_WORKTREE"],
            }
            if self.report_process_argv:
                process["argv"] = ["pi", *self.pi_arguments]
            processes = (
                [process]
                if self.running
                else [{
                    "name": "zsh", "argv0": "zsh", "argv": ["-zsh"],
                    "pid": 8399, "cwd": self.env["LEAGUE_WORKTREE"],
                }]
            )
            return self._completed(
                arguments,
                {
                    "process_info": {
                        "pane_id": self.endpoint["pane_id"],
                        "shell_pid": 8399,
                        "foreground_process_group_id": 8401 if self.running else 8399,
                        "foreground_processes": processes,
                    }
                },
            )
        if arguments[1:3] == ["pane", "get"]:
            role = self.env["LEAGUE_LAUNCH_ROLE"]
            label = (
                self.env["LEAGUE_CALLSIGN"]
                if role == "shotcaller"
                else f"{self.env['LEAGUE_CALLSIGN']} · {self.env['LEAGUE_PROJECT_CODE']}|{self.env['LEAGUE_TASK_LABEL']}"
            )
            return self._completed(arguments, {"pane": {**self._agent(), "label": label}})
        if arguments[1:3] == ["pane", "rename"]:
            return self._completed(arguments, {"renamed": True})
        if arguments[1:3] in (["pane", "report-agent-session"], ["pane", "report-metadata"]):
            if arguments[1:3] == ["pane", "report-metadata"]:
                self.launch_metadata_available = True
            return self._completed(arguments, {"accepted": True})
        if arguments[1:3] == ["agent", "prompt"]:
            return self._completed(arguments, {"accepted": True})
        if arguments[1:3] in (["tab", "close"], ["pane", "close"]):
            self.running = False
            return self._completed(arguments, {"closed": True})
        raise AssertionError(f"unexpected Herdr command: {arguments}")


class PiCleanupInspectionRunner:
    def __init__(self, pi: FakePiHerdr) -> None:
        self.pi = pi

    def run(self, arguments, *, allow_failure=False):
        exact = tuple(arguments)
        if exact[1:3] != ("agent", "list"):
            raise AssertionError(exact)
        return subprocess.CompletedProcess(
            list(exact),
            0,
            json.dumps({"result": {"agents": [self.pi._agent()]}}),
            "",
        )


def _spec(worktree: Path) -> AssignmentSpec:
    return AssignmentSpec(
        assignment_id="assignment:pi-provider",
        request_id="request:pi-provider",
        claim_token="claim:pi-provider",
        task_id="task:pi-provider",
        task_summary="Preserve Pi session lifecycle",
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID,
        repository=REPOSITORY,
        issue=84,
        branch="agent/synthetic/pi-provider",
        worktree=str(worktree),
        issue_receipt=None,
        callsign="Lux",
    )


def _descriptor(root: Path, worktree: Path, provider: str, mode: str) -> dict:
    descriptor_id = f"pi-launch:assignment:{provider}:{mode}"
    parent_path = str((root / "parent-session.jsonl").resolve())
    return {
        "schema": "league.pi-launch-descriptor.v1",
        "descriptor_id": descriptor_id,
        "assignment_id": "assignment:pi-provider",
        "runtime_kind": "pi",
        "provider_kind": provider,
        "model": "grok-4.6" if provider == "cursor" else "gpt-5.6-codex",
        "effort": "xhigh",
        "cwd": str(worktree.resolve()),
        "role": "champion",
        "placement": "new_tab",
        "callsign": "pending",
        "project_code": "LEAGUE",
        "task_label": "Session Restore",
        "routing_name": "pending",
        "workspace_id": "w1",
        "creator_pane_id": None,
        "state_root": str((root / "state").resolve()),
        "release_root": str(ROOT),
        "launch_mode": mode,
        "requested_session_id": (
            deterministic_pi_session_id(descriptor_id) if mode == "create" else None
        ),
        "requested_session_path": None,
        "parent_session_id": PARENT_ID if mode == "fork" else None,
        "parent_session_path": parent_path if mode == "fork" else None,
    }


def test_fork_metadata_restart_and_duplicate_suppression(root: Path) -> None:
    state, store, _clock = create_context(root, "pi-fork")
    worktree = root / "pi-fork" / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    descriptor = _descriptor(root / "pi-fork", worktree, "cursor", "fork")
    fake = FakePiHerdr(root / "pi-fork")
    fake.native_title_reads_remaining = 1
    fake.report_process_argv = False
    adapter = HerdrPiLaunchAdapter(
        store,
        descriptor,
        at="2026-01-01T00:00:00Z",
        runner=fake,
        environment={"HERDR_ENV": "1"},
    )
    receipt = adapter.launch(_spec(worktree))
    assert receipt["harness_kind"] == "pi-thread"
    assert receipt["display_agent"] == "cursor"
    assert receipt["thread_id"] == fake.session_path
    assert receipt["session_id"] == CHILD_ID
    assert receipt["session_path"] == fake.session_path
    cleanup_action = {
        "expected_identity": {
            "agent_name": receipt["routing_name"],
            "pane_id": receipt["endpoint"],
            "session_id": receipt["thread_id"],
        },
        "intended_state": {"completed": True, "action": "session_exit"},
    }
    assert HerdrHarnessAdapter(
        {
            "agent_name": receipt["routing_name"],
            "provider_kind": "pi",
            "exit_prompt": "/quit",
        },
        PiCleanupInspectionRunner(fake),
    ).inspect(cleanup_action) == cleanup_action["expected_identity"]
    assert fake.agent_get_count >= 3
    assert any(call[1:3] == ("tab", "create") for call in fake.calls)
    start = next(call for call in fake.calls if call[1:3] == ("agent", "start"))
    assert "pi-cursor" not in start
    assert "--approve" in start
    assert ("--provider", "cursor") == tuple(
        start[start.index("--provider") : start.index("--provider") + 2]
    )
    assert "--fork" in start and descriptor["parent_session_path"] in start
    stored = store.provider_launch_descriptor(descriptor["descriptor_id"])
    assert stored["session_id"] == CHILD_ID
    assert stored["launch_receipt"]["parent_session_path"] == descriptor["parent_session_path"]
    assert stored["launch_receipt"]["cwd"] == str(worktree.resolve())
    assert stored["launch_receipt"]["task_label"] == "Session Restore"

    retried_adapter = HerdrPiLaunchAdapter(
        store,
        descriptor,
        at="2026-01-01T00:00:30Z",
        runner=fake,
        environment={"HERDR_ENV": "1"},
    )
    retried = retried_adapter.launch(_spec(worktree))
    assert retried["thread_id"] == fake.session_path and fake.start_count == 1

    fake.stop_for_restart()
    resumed = resume_pi_after_restart(
        store,
        descriptor_id=descriptor["descriptor_id"],
        restart_id="restart:one",
        pane_id=fake.endpoint["pane_id"],
        at="2026-01-01T00:01:00Z",
        runner=fake,
        environment={"HERDR_ENV": "1"},
    )
    assert resumed["state"] == "effect_applied"
    assert fake.start_count == 2
    restart_start = [call for call in fake.calls if call[1:3] == ("agent", "start")][-1]
    assert "--session" in restart_start and stored["session_path"] in restart_start
    assert "--fork" not in restart_start
    duplicate = resume_pi_after_restart(
        store,
        descriptor_id=descriptor["descriptor_id"],
        restart_id="restart:one",
        pane_id=fake.endpoint["pane_id"],
        at="2026-01-01T00:01:01Z",
        runner=fake,
        environment={"HERDR_ENV": "1"},
    )
    assert duplicate["idempotent"] is True and fake.start_count == 2

    fake.stop_for_restart()
    (worktree / ".git").rename(worktree / ".git.owner")
    (worktree / ".git").mkdir()
    try:
        resume_pi_after_restart(
            store,
            descriptor_id=descriptor["descriptor_id"],
            restart_id="restart:worktree-replaced",
            pane_id=fake.endpoint["pane_id"],
            at="2026-01-01T00:01:02Z",
            runner=fake,
            environment={"HERDR_ENV": "1"},
        )
    except StorageRefusal as exc:
        assert exc.code == "provider_restart_worktree_mismatch"
    else:
        raise AssertionError("restart trusted a cwd that was no longer the bound worktree")
    assert fake.start_count == 2
    store.close()


def test_unified_inventory_migration_preserves_bytes_and_lineage(root: Path) -> None:
    _state, store, _clock = create_context(root, "pi-migrate")
    base = root / "pi-migrate"
    worktree = base / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    legacy = base / "legacy-sessions"
    unified = base / "unified-sessions"
    unified.mkdir()
    relative = Path("--synthetic-project--") / "2026-01-01_child.jsonl"
    parent = legacy / "--parent-project--" / "2026-01-01_parent.jsonl"
    parent.parent.mkdir(parents=True)
    parent.write_text(
        json.dumps({"type": "session", "version": 3, "id": PARENT_ID, "cwd": str(base)}) + "\n",
        encoding="utf-8",
    )
    source = legacy / relative
    source.parent.mkdir(parents=True)
    payload = (
        json.dumps(
            {
                "type": "session", "version": 3, "id": CHILD_ID,
                "cwd": str(worktree.resolve()), "parentSession": str(parent.resolve()),
            },
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps({"type": "message", "id": "opaque-history", "text": "preserved"})
        + "\n"
    ).encode()
    source.write_bytes(payload)
    destination = unified / relative
    descriptor = _descriptor(base, worktree, "cursor", "fork")
    descriptor.update(
        {
            "descriptor_id": "pi-launch:migrated:cursor",
            "launch_mode": "resume",
            "requested_session_id": CHILD_ID,
            "requested_session_path": str(destination.resolve()),
            "parent_session_id": PARENT_ID,
            "parent_session_path": str(parent.resolve()),
            "callsign": "Lux",
            "routing_name": "lux",
        }
    )
    fake = FakePiHerdr(base)
    fake.env["LEAGUE_WORKTREE"] = str(worktree.resolve())
    manifest = {
        "schema": "league.pi-session-migration.v1",
        "migration_id": "pi-migration:cursor-child",
        "source_inventory_root": str(legacy.resolve()),
        "unified_inventory_root": str(unified.resolve()),
        "relative_session_path": str(relative),
        "expected_sha256": __import__("hashlib").sha256(payload).hexdigest(),
        "descriptor": descriptor,
        "endpoint": fake.endpoint,
    }
    migrated = migrate_pi_session(
        store, manifest, at="2026-01-01T00:03:00Z", runner=fake
    )
    assert migrated["state"] == "bound"
    assert destination.read_bytes() == payload
    assert store.provider_launch_descriptor(descriptor["descriptor_id"])["session_path"] == str(destination.resolve())
    retried = migrate_pi_session(
        store, manifest, at="2026-01-01T00:03:01Z", runner=fake
    )
    assert retried["state"] == "bound" and retried["idempotent"] is True
    assert len(list(unified.rglob("*.jsonl"))) == 1

    fake.running = True
    refused_manifest = dict(manifest)
    refused_manifest["migration_id"] = "pi-migration:active-refusal"
    refused_descriptor = dict(descriptor)
    refused_descriptor["descriptor_id"] = "pi-launch:active-refusal"
    refused_manifest["descriptor"] = refused_descriptor
    try:
        migrate_pi_session(store, refused_manifest, at="2026-01-01T00:03:02Z", runner=fake)
    except StorageRefusal as exc:
        assert exc.code == "pi_session_migration_runtime_active"
    else:
        raise AssertionError("active Pi process migration was not refused")
    store.close()


def test_unified_inventory_duplicate_scan_refuses(root: Path) -> None:
    inventory = root / "pi-duplicate-inventory"
    for name in ("expected", "unexpected"):
        path = inventory / name / "same-session.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "type": "session",
                    "version": 3,
                    "id": CHILD_ID,
                    "cwd": str(root.resolve()),
                }
            )
            + "\n",
            encoding="utf-8",
        )
    try:
        _inventory_identity(inventory, CHILD_ID)
    except StorageRefusal as exc:
        assert exc.code == "pi_session_identity_duplicate"
    else:
        raise AssertionError("bounded inventory scan stopped before finding a duplicate")


def test_provider_mapping_and_role_placement(root: Path) -> None:
    _state, store, _clock = create_context(root, "pi-placement")
    worktree = root / "pi-placement" / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    for provider, cli_provider in (("cursor", "cursor"), ("codex", "openai-codex")):
        descriptor = _descriptor(root / "pi-placement", worktree, provider, "create")
        descriptor["descriptor_digest"] = "a" * 64
        descriptor["pane_id"] = "w1:p84"
        arguments = pi_start_arguments(descriptor)
        assert arguments[arguments.index("--provider") + 1] == cli_provider

    descriptor = _descriptor(root / "pi-placement", worktree, "codex", "create")
    descriptor.update(
        {
            "descriptor_id": "pi-launch:shotcaller:placement",
            "role": "shotcaller",
            "placement": "sibling_pane",
            "creator_pane_id": "w1:p1",
            "requested_session_id": deterministic_pi_session_id(
                "pi-launch:shotcaller:placement"
            ),
        }
    )
    fake = FakePiHerdr(root / "pi-placement")
    adapter = HerdrPiLaunchAdapter(
        store,
        descriptor,
        at="2026-01-01T00:02:00Z",
        runner=fake,
        environment={"HERDR_ENV": "1"},
    )
    receipt = adapter.launch(_spec(worktree))
    assert receipt["display_agent"] == "codex"
    assert any(call[1:3] == ("pane", "split") for call in fake.calls)
    assert not any(call[1:3] == ("tab", "create") for call in fake.calls)
    agent = fake._agent()
    assert agent["terminal_title"] == "Lux"
    assert agent["tokens"]["task_label"] == "Session Restore"
    assert agent["tokens"]["sidebar_name"] == "Lux"
    store.close()


def test_cli_exposes_explicit_pi_inputs() -> None:
    completed = subprocess.run(
        (str(ROOT / "bin" / "league"), "assign", "run", "--help"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    for option in (
        "--runtime-kind",
        "--provider-kind",
        "--project-code",
        "--session-mode",
        "--session-id",
        "--session-path",
        "--parent-session-id",
        "--parent-session-path",
    ):
        assert option in completed.stdout
    restart = subprocess.run(
        (str(ROOT / "bin" / "league"), "runtime", "resume-launch", "--help"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert restart.returncode == 0 and "--restart-id" in restart.stdout


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-pi-provider-") as directory:
        root = Path(directory)
        test_fork_metadata_restart_and_duplicate_suppression(root)
        test_provider_mapping_and_role_placement(root)
        test_unified_inventory_migration_preserves_bytes_and_lineage(root)
        test_unified_inventory_duplicate_scan_refuses(root)
    test_cli_exposes_explicit_pi_inputs()
    print("PASS: Pi provider launch, one-time fork lineage, metadata, placement, and exact restart resume")


if __name__ == "__main__":
    main()
