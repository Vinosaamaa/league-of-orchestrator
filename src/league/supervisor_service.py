"""Supported launchd ownership for the persistent League watcher service.

The service manager, not a model turn or Herdr plugin, owns process startup and
restart.  Installation is hash-bound, preserves one exact prior plist, and
leaves a local manifest that makes rollback deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol

from .persistent_supervisor import supervisor_status
from .sqlite_store import SQLiteStorage
from .storage import StorageRefusal


SERVICE_LABEL = "io.league-of-orchestrator.supervisor"
MANIFEST_SCHEMA = "league.supervisor-service-install.v1"
MAX_SERVICE_FILE_BYTES = 1_000_000


class ServiceManager(Protocol):
    def is_loaded(self, label: str) -> bool: ...

    def bootstrap(self, label: str, plist_path: Path) -> None: ...

    def kickstart(self, label: str) -> None: ...

    def bootout(self, label: str) -> None: ...


class LaunchctlServiceManager:
    """Bounded user-domain launchctl adapter."""

    def __init__(self, executable: Path = Path("/bin/launchctl")) -> None:
        self.executable = executable
        self.domain = f"gui/{os.getuid()}"

    def _run(self, arguments: list[str], *, allow_failure: bool = False) -> int:
        try:
            completed = subprocess.run(
                [os.fspath(self.executable), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StorageRefusal(
                "supervisor_service_manager_unavailable",
                "launchd did not complete the bounded service operation",
                retryable=True,
            ) from exc
        if completed.returncode != 0 and not allow_failure:
            raise StorageRefusal(
                "supervisor_service_manager_refused",
                "launchd refused the persistent watcher service operation",
                retryable=True,
            )
        return completed.returncode

    def is_loaded(self, label: str) -> bool:
        return self._run(["print", f"{self.domain}/{label}"], allow_failure=True) == 0

    def bootstrap(self, label: str, plist_path: Path) -> None:
        del label
        self._run(["bootstrap", self.domain, os.fspath(plist_path)])

    def kickstart(self, label: str) -> None:
        self._run(["kickstart", "-k", f"{self.domain}/{label}"])

    def bootout(self, label: str) -> None:
        self._run(["bootout", f"{self.domain}/{label}"])


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_owned_regular(path: Path, purpose: str) -> bytes:
    if not path.is_absolute():
        raise StorageRefusal(
            "supervisor_service_path_invalid", f"{purpose} path must be absolute"
        )
    try:
        observed = path.lstat()
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or observed.st_size > MAX_SERVICE_FILE_BYTES
        ):
            raise OSError
        return path.read_bytes()
    except OSError as exc:
        raise StorageRefusal(
            "supervisor_service_path_invalid",
            f"{purpose} must be one bounded user-owned regular file",
        ) from exc


def _read_optional_owned_regular(path: Path, purpose: str) -> bytes | None:
    if not path.exists():
        return None
    return _read_owned_regular(path, purpose)


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    if not path.is_absolute():
        raise StorageRefusal(
            "supervisor_service_path_invalid", "service destination must be absolute"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise StorageRefusal(
            "supervisor_service_install_failed",
            "persistent watcher service files could not be committed atomically",
        ) from exc


def _manifest_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _load_manifest(path: Path) -> dict[str, Any]:
    raw = _read_owned_regular(path, "service install manifest")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageRefusal(
            "supervisor_service_manifest_invalid",
            "service install manifest is malformed",
        ) from exc
    required = {
        "schema",
        "state",
        "label",
        "plist_path",
        "backup_path",
        "state_root",
        "agent_watcher_sha256",
        "template_sha256",
        "installed_plist_sha256",
        "previous_plist_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema") != MANIFEST_SCHEMA
        or value.get("state") not in {"prepared", "active", "rolled_back"}
        or value.get("label") != SERVICE_LABEL
        or not all(
            isinstance(value.get(key), str) and value[key]
            for key in (
                "plist_path",
                "backup_path",
                "state_root",
                "agent_watcher_sha256",
                "template_sha256",
                "installed_plist_sha256",
            )
        )
        or (
            value.get("previous_plist_sha256") is not None
            and not isinstance(value.get("previous_plist_sha256"), str)
        )
    ):
        raise StorageRefusal(
            "supervisor_service_manifest_invalid",
            "service install manifest is outside the supported contract",
        )
    return value


def render_launchd_plist(
    template_path: Path, agent_watcher: Path, state_root: Path
) -> tuple[bytes, str]:
    template = _read_owned_regular(template_path, "launchd template")
    try:
        value = plistlib.loads(template)
    except Exception as exc:
        raise StorageRefusal(
            "supervisor_service_template_invalid", "launchd template is malformed"
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("Label") != SERVICE_LABEL
        or value.get("ProgramArguments") != ["@@AGENT_WATCHER@@", "service-run"]
        or value.get("EnvironmentVariables")
        != {
            "LEAGUE_STATE_ROOT": "@@STATE_ROOT@@",
            "LEAGUE_WRITER_POINTER": "@@WRITER_POINTER@@",
            "PATH": "@@PYTHON_PATH@@",
        }
        or value.get("RunAtLoad") is not True
        or value.get("KeepAlive") != {"SuccessfulExit": False}
        or value.get("ProcessType") != "Background"
        or value.get("ThrottleInterval") != 5
    ):
        raise StorageRefusal(
            "supervisor_service_template_invalid",
            "launchd template does not express the supported persistent service",
        )
    value["ProgramArguments"] = [os.fspath(agent_watcher), "service-run"]
    python_directory = os.fspath(Path(sys.executable).resolve().parent)
    service_path = os.pathsep.join(
        dict.fromkeys(
            (
                python_directory,
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            )
        )
    )
    value["EnvironmentVariables"] = {
        "LEAGUE_STATE_ROOT": os.fspath(state_root),
        "LEAGUE_WRITER_POINTER": os.fspath(
            state_root.parent / "league-writer-pointer.json"
        ),
        "PATH": service_path,
    }
    rendered = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False)
    return rendered, _sha256(template)


class SupervisorServiceInstaller:
    """Hash-bound install/start/restart/rollback controller for one LaunchAgent."""

    def __init__(
        self,
        *,
        state_root: Path,
        agent_watcher: Path,
        template_path: Path,
        plist_path: Path,
        backup_path: Path,
        manifest_path: Path,
        service_manager: ServiceManager | None = None,
        status_reader: Callable[[Path, str | None], dict[str, Any]] = supervisor_status,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        paths = (
            state_root,
            agent_watcher,
            template_path,
            plist_path,
            backup_path,
            manifest_path,
        )
        if any(not path.is_absolute() for path in paths) or len(set(paths)) != len(paths):
            raise StorageRefusal(
                "supervisor_service_path_invalid",
                "service install paths must be distinct and absolute",
            )
        self.state_root = state_root
        self.agent_watcher = agent_watcher
        self.template_path = template_path
        self.plist_path = plist_path
        self.backup_path = backup_path
        self.manifest_path = manifest_path
        self.service_manager = service_manager or LaunchctlServiceManager()
        self.status_reader = status_reader
        self.sleeper = sleeper

    def _source(self) -> tuple[bytes, str, str]:
        watcher = _read_owned_regular(self.agent_watcher, "agent-watcher executable")
        try:
            executable = self.agent_watcher.stat().st_mode & 0o111
        except OSError as exc:
            raise StorageRefusal(
                "supervisor_service_path_invalid",
                "agent-watcher executable could not be inspected",
            ) from exc
        if not executable:
            raise StorageRefusal(
                "supervisor_service_path_invalid",
                "agent-watcher source is not executable",
            )
        rendered, template_digest = render_launchd_plist(
            self.template_path, self.agent_watcher, self.state_root
        )
        return rendered, _sha256(watcher), template_digest

    def _preflight_bindings(self) -> int:
        if not self.state_root.is_dir():
            raise StorageRefusal(
                "supervisor_service_state_unavailable",
                "canonical state root must exist before service installation",
            )
        with SQLiteStorage(self.state_root) as store:
            bindings = store.supervisor_bindings()
        if not bindings:
            raise StorageRefusal(
                "supervisor_service_binding_missing",
                "service installation requires at least one active Squad Shotcaller",
            )
        return len(bindings)

    @staticmethod
    def _fences(status: Mapping[str, Any] | None) -> dict[str, int]:
        if not isinstance(status, Mapping) or status.get("live") is not True:
            return {}
        bindings = status.get("bindings")
        if not isinstance(bindings, list):
            return {}
        result: dict[str, int] = {}
        for binding in bindings:
            if (
                not isinstance(binding, Mapping)
                or not isinstance(binding.get("actor_agent_id"), str)
                or type(binding.get("fence")) is not int
            ):
                return {}
            result[str(binding["actor_agent_id"])] = int(binding["fence"])
        return result

    def _wait_live(
        self,
        timeout_seconds: float = 10.0,
        *,
        previous_fences: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        delay = 0.02
        last: dict[str, Any] | None = None
        while True:
            try:
                last = self.status_reader(self.state_root, None)
            except (StorageRefusal, OSError):
                last = None
            current_fences = self._fences(last)
            restarted = not previous_fences or (
                set(current_fences) == set(previous_fences)
                and all(
                    current_fences[actor] > fence
                    for actor, fence in previous_fences.items()
                )
            )
            if (
                isinstance(last, dict)
                and last.get("live") is True
                and last.get("monitor_live") is True
                and restarted
            ):
                return last
            if time.monotonic() >= deadline:
                raise StorageRefusal(
                    "supervisor_service_start_timeout",
                    "OS-managed watcher did not become live before the startup bound",
                    retryable=True,
                )
            self.sleeper(delay)
            delay = min(delay * 2, 0.25)

    def _live_status(self) -> dict[str, Any] | None:
        try:
            status = self.status_reader(self.state_root, None)
        except (StorageRefusal, OSError):
            return None
        return status if status.get("live") is True else None

    def _assert_no_unmanaged_process(self) -> None:
        if self._live_status() is not None:
            raise StorageRefusal(
                "supervisor_service_conflict",
                "a live watcher process exists outside the owned launchd job",
            )

    def _assert_manifest_paths(self, manifest: Mapping[str, Any]) -> None:
        expected = {
            "plist_path": os.fspath(self.plist_path),
            "backup_path": os.fspath(self.backup_path),
            "state_root": os.fspath(self.state_root),
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise StorageRefusal(
                "supervisor_service_manifest_invalid",
                "service manifest does not own these exact local paths",
            )

    def _assert_rollback_backup(self, manifest: Mapping[str, Any]) -> None:
        expected_digest = manifest.get("previous_plist_sha256")
        backup = _read_optional_owned_regular(
            self.backup_path, "service rollback backup"
        )
        if (
            expected_digest is None
            and backup is not None
            or isinstance(expected_digest, str)
            and (backup is None or _sha256(backup) != expected_digest)
        ):
            raise StorageRefusal(
                "supervisor_service_backup_mismatch",
                "service rollback backup does not match the active manifest",
            )

    def _assert_source_manifest(self, manifest: Mapping[str, Any]) -> None:
        rendered, watcher_digest, template_digest = self._source()
        if (
            watcher_digest != manifest["agent_watcher_sha256"]
            or template_digest != manifest["template_sha256"]
            or _sha256(rendered) != manifest["installed_plist_sha256"]
        ):
            raise StorageRefusal(
                "supervisor_service_source_mismatch",
                "service source bytes or executable path no longer match the authorized manifest",
            )

    def _assert_live_source_manifest(self, manifest: Mapping[str, Any]) -> None:
        try:
            self._assert_source_manifest(manifest)
        except StorageRefusal:
            try:
                if self.service_manager.is_loaded(SERVICE_LABEL):
                    self.service_manager.bootout(SERVICE_LABEL)
            except (OSError, StorageRefusal) as rollback_exc:
                raise StorageRefusal(
                    "supervisor_service_rollback_failed",
                    "source drift after service start could not be stopped safely",
                ) from rollback_exc
            raise

    def _assert_exact_rolled_back_state(self, manifest: Mapping[str, Any]) -> None:
        if self.service_manager.is_loaded(SERVICE_LABEL):
            raise StorageRefusal(
                "supervisor_service_recovery_required",
                "rolled-back service state still has a loaded launchd job",
            )
        self._assert_no_unmanaged_process()
        current = _read_optional_owned_regular(self.plist_path, "restored plist")
        backup = _read_optional_owned_regular(
            self.backup_path, "service rollback backup"
        )
        previous_digest = manifest.get("previous_plist_sha256")
        exact = (
            (
                previous_digest is None
                and current is None
                and backup is None
            )
            or (
                isinstance(previous_digest, str)
                and current is not None
                and backup is not None
                and _sha256(current) == previous_digest
                and _sha256(backup) == previous_digest
            )
        )
        if not exact:
            raise StorageRefusal(
                "supervisor_service_recovery_required",
                "rolled-back service bytes do not match their exact prior state",
            )

    def install(
        self,
        *,
        expected_agent_watcher_sha256: str,
        expected_template_sha256: str,
    ) -> dict[str, Any]:
        binding_count = self._preflight_bindings()
        rendered, watcher_digest, template_digest = self._source()
        if (
            watcher_digest != expected_agent_watcher_sha256
            or template_digest != expected_template_sha256
        ):
            raise StorageRefusal(
                "supervisor_service_source_mismatch",
                "service source bytes do not match the authorized digests",
            )
        installed_digest = _sha256(rendered)
        existing_manifest = (
            _load_manifest(self.manifest_path)
            if self.manifest_path.exists()
            else None
        )
        if existing_manifest is not None:
            self._assert_manifest_paths(existing_manifest)
            current = _read_optional_owned_regular(self.plist_path, "installed plist")
            if (
                existing_manifest["state"] == "active"
                and existing_manifest["installed_plist_sha256"] == installed_digest
                and existing_manifest["agent_watcher_sha256"] == watcher_digest
                and existing_manifest["template_sha256"] == template_digest
                and current is not None
                and _sha256(current) == installed_digest
            ):
                self._assert_rollback_backup(existing_manifest)
                if not self.service_manager.is_loaded(SERVICE_LABEL):
                    self._assert_no_unmanaged_process()
                    self.service_manager.bootstrap(SERVICE_LABEL, self.plist_path)
                status = self._wait_live()
                self._assert_live_source_manifest(existing_manifest)
                return {
                    "schema": MANIFEST_SCHEMA,
                    "installed": True,
                    "started": True,
                    "live": True,
                    "binding_count": binding_count,
                    "installed_plist_sha256": installed_digest,
                    "rollback_ready": True,
                    "idempotent": True,
                    "service_status": status,
                }
            if existing_manifest["state"] == "rolled_back":
                self._assert_exact_rolled_back_state(existing_manifest)
            else:
                raise StorageRefusal(
                    "supervisor_service_recovery_required",
                    "an earlier service installation must be rolled back exactly first",
                )
        if self.service_manager.is_loaded(SERVICE_LABEL):
            raise StorageRefusal(
                "supervisor_service_conflict",
                "an unmanaged service already owns the launchd label",
            )
        self._assert_no_unmanaged_process()
        previous = _read_optional_owned_regular(self.plist_path, "existing plist")
        if previous is not None:
            existing_backup = _read_optional_owned_regular(
                self.backup_path, "service rollback backup"
            )
            if existing_backup is not None and existing_backup != previous:
                raise StorageRefusal(
                    "supervisor_service_backup_conflict",
                    "service rollback backup already exists",
                )
            if existing_backup is None:
                _atomic_write(self.backup_path, previous)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "state": "prepared",
            "label": SERVICE_LABEL,
            "plist_path": os.fspath(self.plist_path),
            "backup_path": os.fspath(self.backup_path),
            "state_root": os.fspath(self.state_root),
            "agent_watcher_sha256": watcher_digest,
            "template_sha256": template_digest,
            "installed_plist_sha256": installed_digest,
            "previous_plist_sha256": None if previous is None else _sha256(previous),
        }
        _atomic_write(self.manifest_path, _manifest_bytes(manifest))
        loaded = False
        try:
            _atomic_write(self.plist_path, rendered)
            if _sha256(_read_owned_regular(self.plist_path, "installed plist")) != installed_digest:
                raise StorageRefusal(
                    "supervisor_service_install_failed",
                    "installed service plist failed exact-byte verification",
                )
            self.service_manager.bootstrap(SERVICE_LABEL, self.plist_path)
            loaded = True
            status = self._wait_live()
            self._assert_source_manifest(manifest)
        except BaseException:
            try:
                if loaded and self.service_manager.is_loaded(SERVICE_LABEL):
                    self.service_manager.bootout(SERVICE_LABEL)
                if previous is None:
                    self.plist_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.plist_path, previous)
                manifest["state"] = "rolled_back"
                _atomic_write(self.manifest_path, _manifest_bytes(manifest))
            except (OSError, StorageRefusal) as rollback_exc:
                raise StorageRefusal(
                    "supervisor_service_rollback_failed",
                    "failed service installation could not restore its exact prior state",
                ) from rollback_exc
            raise
        manifest["state"] = "active"
        _atomic_write(self.manifest_path, _manifest_bytes(manifest))
        return {
            "schema": MANIFEST_SCHEMA,
            "installed": True,
            "started": True,
            "live": True,
            "binding_count": binding_count,
            "installed_plist_sha256": installed_digest,
            "rollback_ready": True,
            "idempotent": False,
            "service_status": status,
        }

    def start(self) -> dict[str, Any]:
        manifest = _load_manifest(self.manifest_path)
        self._assert_manifest_paths(manifest)
        if manifest["state"] != "active":
            raise StorageRefusal(
                "supervisor_service_recovery_required",
                "service start requires one exact active install manifest",
            )
        self._assert_source_manifest(manifest)
        installed = _read_owned_regular(self.plist_path, "installed plist")
        if _sha256(installed) != manifest["installed_plist_sha256"]:
            raise StorageRefusal(
                "supervisor_service_installed_mismatch",
                "installed service plist differs from its exact manifest",
            )
        restarted = self.service_manager.is_loaded(SERVICE_LABEL)
        previous_fences: dict[str, int] = {}
        if restarted:
            try:
                previous_fences = self._fences(
                    self.status_reader(self.state_root, None)
                )
            except (StorageRefusal, OSError):
                previous_fences = {}
            self.service_manager.kickstart(SERVICE_LABEL)
        else:
            self._assert_no_unmanaged_process()
            self.service_manager.bootstrap(SERVICE_LABEL, self.plist_path)
        status = self._wait_live(previous_fences=previous_fences)
        self._assert_live_source_manifest(manifest)
        return {
            "schema": MANIFEST_SCHEMA,
            "started": True,
            "restarted": restarted,
            "live": True,
            "service_status": status,
        }

    def rollback(
        self,
        *,
        expected_installed_plist_sha256: str,
        expected_backup_sha256: str | None = None,
    ) -> dict[str, Any]:
        manifest = _load_manifest(self.manifest_path)
        self._assert_manifest_paths(manifest)
        if manifest["state"] not in {"active", "prepared"}:
            raise StorageRefusal(
                "supervisor_service_rollback_complete",
                "service installation is already rolled back",
            )
        current = _read_owned_regular(self.plist_path, "installed plist")
        if (
            manifest["installed_plist_sha256"] != expected_installed_plist_sha256
            or _sha256(current) != expected_installed_plist_sha256
        ):
            raise StorageRefusal(
                "supervisor_service_installed_mismatch",
                "rollback refused changed installed service bytes",
            )
        previous_digest = manifest["previous_plist_sha256"]
        previous: bytes | None = None
        if previous_digest is not None:
            previous = _read_owned_regular(self.backup_path, "service rollback backup")
            if (
                expected_backup_sha256 != previous_digest
                or _sha256(previous) != previous_digest
            ):
                raise StorageRefusal(
                    "supervisor_service_backup_mismatch",
                    "rollback backup does not match the authorized prior bytes",
                )
        elif expected_backup_sha256 is not None:
            raise StorageRefusal(
                "supervisor_service_backup_mismatch",
                "rollback declared a backup where no prior plist existed",
            )
        if self.service_manager.is_loaded(SERVICE_LABEL):
            self.service_manager.bootout(SERVICE_LABEL)
        if previous is None:
            try:
                self.plist_path.unlink()
            except OSError as exc:
                raise StorageRefusal(
                    "supervisor_service_rollback_failed",
                    "installed service plist could not be removed",
                ) from exc
            restored_digest = None
        else:
            _atomic_write(self.plist_path, previous)
            restored_digest = _sha256(
                _read_owned_regular(self.plist_path, "restored service plist")
            )
        manifest["state"] = "rolled_back"
        _atomic_write(self.manifest_path, _manifest_bytes(manifest))
        return {
            "schema": MANIFEST_SCHEMA,
            "rolled_back": True,
            "service_loaded": False,
            "restored_plist_sha256": restored_digest,
            "idempotent": False,
        }


__all__ = [
    "LaunchctlServiceManager",
    "MANIFEST_SCHEMA",
    "SERVICE_LABEL",
    "ServiceManager",
    "SupervisorServiceInstaller",
    "render_launchd_plist",
]
