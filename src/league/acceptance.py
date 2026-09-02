"""Explicit-root isolated acceptance and reversible cutover foundation.

This module operates only beneath caller-supplied temporary roots.  Its
adapters are deliberately synthetic and its receipts never claim real harness
or terminal-backend support.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import MAX_ACCEPTANCE_SENTINEL_PATHS, __version__
from .guidance import (
    LEAGUE_TARGET,
    rollback_guidance,
    stage_guidance,
    validate_guidance_manifest,
)
from .importer import build_import_plan
from .sqlite_store import CURRENT_SCHEMA_VERSION, SQLiteStorage
from .storage import StorageRefusal
from .storage_types import FaultInjector


RECEIPT_SCHEMA = "league.acceptance-receipt.v1"
HOOK_FIXTURE_SCHEMA = "league.synthetic-hook.v1"
PROCESS_SENTINEL_SCHEMA = "league.synthetic-process-sentinel.v1"
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
POINTER_STAGES = (
    "lock_acquired",
    "backup_recorded",
    "old_writer_quiesced",
    "pointer_prepared",
    "pointer_switched",
    "new_writer_activated",
    "generation_verified",
)
PENDING_SLICES = (
    ("request", 17),
    ("assignment", 4),
    ("watcher", 3),
    ("stop", 5),
    ("teardown", 11),
)
UNVERIFIED_RUNTIMES = ("codex", "cursor", "pi", "herdr", "tmux")
FIXTURE_RUNTIME_ROOT = Path("/synthetic/league-acceptance-runtime")
MAX_RELEASE_FILE_BYTES = 4 * 1024 * 1024
RELEASE_READ_CHUNK_BYTES = 64 * 1024
STAGING_RESERVATION_FILENAME = ".league-staging-reservation.json"
STAGING_RESERVATION_SCHEMA = "league.staging-reservation.v1"


def _stable_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    _atomic_write(path, _stable_bytes(value), mode=mode)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _read_object(path: Path, *, schema: Optional[str] = None) -> dict[str, Any]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise StorageRefusal("invalid_sentinel", "sentinel must be an explicit regular file")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise StorageRefusal("invalid_sentinel", "sentinel JSON is malformed") from exc
    if not isinstance(value, dict) or (schema is not None and value.get("schema") != schema):
        raise StorageRefusal("invalid_sentinel", "sentinel JSON schema is unsupported")
    return value


def _tree_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.exists():
        raise StorageRefusal(
            "invalid_sentinel", "sentinel paths must be explicit existing non-symlinks"
        )
    records: list[dict[str, Any]] = []
    candidates = [path] if path.is_file() else [path, *sorted(path.rglob("*"))]
    for candidate in candidates:
        if candidate.is_symlink():
            raise StorageRefusal("invalid_sentinel", "sentinel trees cannot contain symlinks")
        relative = "." if candidate == path else candidate.relative_to(path).as_posix()
        stat = candidate.stat()
        if candidate.is_dir():
            records.append({"path": relative, "kind": "directory", "mode": stat.st_mode & 0o777})
        elif candidate.is_file():
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": stat.st_mode & 0o777,
                    "bytes": stat.st_size,
                    "sha256": _sha256(candidate.read_bytes()),
                }
            )
        else:
            raise StorageRefusal("invalid_sentinel", "sentinel trees require regular files")
    return {"path": str(path), "digest": _sha256(_stable_bytes(records)), "records": records}


@dataclass
class SentinelSet:
    byte_paths: tuple[Path, ...]
    config_path: Path
    process_path: Path
    before: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        config = _read_object(self.config_path)
        processes = _read_object(self.process_path, schema=PROCESS_SENTINEL_SCHEMA)
        if not isinstance(processes.get("processes"), list):
            raise StorageRefusal("invalid_sentinel", "process sentinel list is required")
        self.before = {
            "bytes": [_tree_snapshot(path) for path in self.byte_paths],
            "config": {
                "snapshot": _tree_snapshot(self.config_path),
                "canonical_sha256": _sha256(_stable_bytes(config)),
            },
            "process": {
                "snapshot": _tree_snapshot(self.process_path),
                "canonical_sha256": _sha256(_stable_bytes(processes)),
                "count": len(processes["processes"]),
            },
        }

    def verify(self) -> dict[str, Any]:
        after = SentinelSet(self.byte_paths, self.config_path, self.process_path).before
        if after != self.before:
            raise StorageRefusal("sentinel_changed", "a caller-specified live sentinel changed")
        return {
            "unchanged": True,
            "byte_paths": len(self.before["bytes"]),
            "config_sha256": self.before["config"]["canonical_sha256"],
            "process_sha256": self.before["process"]["canonical_sha256"],
            "process_count": self.before["process"]["count"],
        }


@dataclass
class DeterministicContext:
    at: str = "2026-01-01T00:00:00Z"
    sequence: int = 0

    def identifier(self, kind: str) -> str:
        self.sequence += 1
        return f"synthetic-{kind}-{self.sequence:04d}"


class FakeAdapter:
    def __init__(self, name: str, context: DeterministicContext) -> None:
        self.name = name
        self.context = context
        self.calls: list[dict[str, Any]] = []

    def call(self, operation: str, **fields: Any) -> dict[str, Any]:
        receipt = {
            "adapter": f"fake-{self.name}",
            "operation": operation,
            "receipt_id": self.context.identifier(f"{self.name}-receipt"),
            "at": self.context.at,
            "fields": fields,
        }
        self.calls.append(receipt)
        return receipt


class FakeProcessResourceAdapter(FakeAdapter):
    def __init__(self, context: DeterministicContext) -> None:
        super().__init__("process-resource", context)
        self.processes: dict[int, dict[str, Any]] = {}

    def start(
        self, *, pid: int, process_start: str, endpoint: str, generation: str
    ) -> dict[str, Any]:
        if pid in self.processes:
            raise StorageRefusal("resource_conflict", "synthetic process identity collided")
        self.processes[pid] = {
            "pid": pid,
            "process_start": process_start,
            "endpoint": endpoint,
            "generation": generation,
            "running": True,
        }
        return self.call("start", **self.processes[pid])

    def terminate(self, identity: dict[str, Any]) -> dict[str, Any]:
        observed = self.processes.get(int(identity["pid"]))
        if observed is None or any(
            observed.get(key) != identity.get(key)
            for key in ("pid", "process_start", "endpoint", "generation")
        ):
            raise StorageRefusal("resource_identity_mismatch", "exact canary resource changed")
        observed["running"] = False
        return self.call("terminate", **observed)


def _fake_adapters(context: DeterministicContext) -> dict[str, FakeAdapter]:
    return {
        "harness": FakeAdapter("harness", context),
        "terminal_backend": FakeAdapter("terminal-backend", context),
        "git": FakeAdapter("git", context),
        "github": FakeAdapter("github", context),
        "process_resource": FakeProcessResourceAdapter(context),
        "notification": FakeAdapter("notification", context),
        "deployment": FakeAdapter("deployment", context),
        "hook": FakeAdapter("hook", context),
    }


def validate_hook_fixture(harness: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate only the synthetic hook envelope, never provider capability."""
    if harness not in {"codex", "cursor", "pi"}:
        raise StorageRefusal("invalid_hook_fixture", "synthetic harness fixture is unsupported")
    if payload != {
        "schema": HOOK_FIXTURE_SCHEMA,
        "harness": harness,
        "event": "stop",
        "session_ref": f"synthetic-{harness}-session",
    }:
        raise StorageRefusal("invalid_hook_fixture", "synthetic hook fixture is malformed")
    return {
        "harness": harness,
        "fixture_consumed": True,
        "configuration_mutated": False,
        "runtime_support_proven": False,
    }


def _load_fixture_module(source_root: Path) -> Any:
    fixture_path = source_root / "tests/storage_fixture.py"
    if not fixture_path.is_file():
        raise StorageRefusal("fixture_missing", "source-managed legacy fixture is missing")
    spec = importlib.util.spec_from_file_location("league_acceptance_fixture", fixture_path)
    if spec is None or spec.loader is None:
        raise StorageRefusal("fixture_missing", "source-managed legacy fixture cannot load")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_shadow(home: Path, source_root: Path) -> dict[str, Any]:
    fixture_module = _load_fixture_module(source_root)
    legacy_root = home / "legacy-fixture"
    state_root = home / "state"
    legacy_root.mkdir(mode=0o700)
    state_root.mkdir(mode=0o700)
    fixture = fixture_module.write_complete_fixture(
        legacy_root, runtime_root=FIXTURE_RUNTIME_ROOT
    )
    legacy_before = _tree_snapshot(legacy_root)["digest"]
    with SQLiteStorage.for_migration(state_root, request_wal=False) as store:
        migration = store.migrate()
    with SQLiteStorage(state_root, request_wal=False) as store:
        plan = build_import_plan(
            legacy_root, fixture["manifest"], target_counts=store.import_target_counts()
        )
        dry_run = plan["report"]
        if not dry_run["dry_run"] or not dry_run["eligible"]:
            raise StorageRefusal("shadow_parity_failed", "fixture dry-run was not eligible")
        applied = store.apply_import(plan, plan["report_digest"])
        exported = json.loads(
            store.export_bytes(format_name="json", purpose="rollback", max_records=10_000)
        )
        expected_rows = plan["rows"]
        for table, rows in expected_rows.items():
            expected = sorted(rows, key=lambda row: _stable_bytes(row))
            expected_columns = {column for row in rows for column in row}
            observed = sorted(
                (
                    {column: row[column] for column in expected_columns}
                    for row in exported["tables"][table]
                ),
                key=lambda row: _stable_bytes(row),
            )
            if expected != observed:
                raise StorageRefusal("shadow_parity_failed", f"fixture parity failed for {table}")
        parity_payload = {
            table: sorted(rows, key=lambda row: _stable_bytes(row))
            for table, rows in expected_rows.items()
        }
        parity_digest = _sha256(_stable_bytes(parity_payload))
        if not store.integrity()["ok"]:
            raise StorageRefusal("shadow_parity_failed", "fixture target integrity failed")
    return {
        "migration": migration,
        "dry_run": {
            "eligible": True,
            "report_digest": dry_run["report_digest"],
            "source_digest": dry_run["source_digest"],
            "artifact_counts": dry_run["artifact_counts"],
        },
        "apply": {"applied": applied["applied"], "row_counts": dry_run["row_counts"]},
        "exact_parity": True,
        "parity_sha256": parity_digest,
        "legacy_unchanged": legacy_before == _tree_snapshot(legacy_root)["digest"],
    }


def _open_release_descriptor(
    root: Path,
    relative: Path,
    *,
    directory: bool,
    refusal_code: str,
) -> int:
    if (
        not root.is_absolute()
        or relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise StorageRefusal(
            refusal_code, "release paths must stay beneath one explicit root"
        )
    opened_directories: list[int] = []
    try:
        root_status = root.lstat()
        if not stat.S_ISDIR(root_status.st_mode):
            raise StorageRefusal(
                refusal_code, "release root must be an explicit directory"
            )
        canonical_root = root.resolve(strict=True)
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        current = os.open(canonical_root.anchor, flags)
        opened_directories.append(current)
        for component in canonical_root.parts[1:]:
            current = os.open(component, flags, dir_fd=current)
            opened_directories.append(current)
        for component in relative.parts[:-1]:
            current = os.open(component, flags, dir_fd=current)
            opened_directories.append(current)
        final_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        if directory:
            final_flags |= os.O_DIRECTORY
        return os.open(relative.parts[-1], final_flags, dir_fd=current)
    except StorageRefusal:
        raise
    except (OSError, RuntimeError) as exc:
        raise StorageRefusal(
            refusal_code, "release path traversal requires non-symlink components"
        ) from exc
    finally:
        for descriptor in reversed(opened_directories):
            os.close(descriptor)


def _release_directory_files(
    source_root: Path, relative: Path, suffix: str
) -> list[Path]:
    descriptor = _open_release_descriptor(
        source_root,
        relative,
        directory=True,
        refusal_code="release_incomplete",
    )
    try:
        names = sorted(
            name
            for name in os.listdir(descriptor)
            if not name.startswith(".") and name.endswith(suffix)
        )
    except OSError as exc:
        raise StorageRefusal(
            "release_incomplete", "release manifest directory is unreadable"
        ) from exc
    finally:
        os.close(descriptor)
    return [source_root / relative / name for name in names]


def _release_files(source_root: Path) -> list[Path]:
    files = [
        source_root / "VERSION",
        source_root / "bin/agent-watcher",
        source_root / "bin/league",
        source_root / "src/agent_watcher.py",
        source_root / "tests/storage_fixture.py",
        source_root / "src/league/report_template.html",
        source_root / "skills/league-report/SKILL.md",
        source_root / "global-agent-instructions/league/AGENTS.md",
        source_root / "integrations/pi/league-runtime.ts",
        source_root / "integrations/pi/league-bash.sb",
        source_root / "integrations/herdr/league-restore/herdr-plugin.toml",
        source_root / "integrations/herdr/league-restore/restore.sh",
        source_root / "integrations/herdr/league-restore/README.md",
        source_root / "config/league-model-routing.example.json",
    ]
    files.extend(_release_directory_files(source_root, Path("src/league"), ".py"))
    for package in (
        Path("src/league/agent_adapters"),
        Path("src/league/agent_adapters/codex"),
        Path("src/league/agent_adapters/pi"),
        Path("src/league/agent_adapters/cursor_cli"),
        Path("src/league/multiplexer_adapters"),
        Path("src/league/multiplexer_adapters/herdr"),
        Path("src/league/multiplexer_adapters/tmux"),
    ):
        files.extend(_release_directory_files(source_root, package, ".py"))
    files.extend(_release_directory_files(source_root, Path("schema"), ".json"))
    for path in files:
        _validate_regular_file(
            path, root=source_root, refusal_code="release_incomplete"
        )
    return files


def _validate_regular_file(path: Path, *, root: Path, refusal_code: str) -> None:
    descriptor: Optional[int] = None
    verification_descriptor: Optional[int] = None
    try:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise StorageRefusal(
                refusal_code, "release path escapes its explicit root"
            ) from exc
        descriptor = _open_release_descriptor(
            root,
            relative,
            directory=False,
            refusal_code=refusal_code,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > MAX_RELEASE_FILE_BYTES
        ):
            raise StorageRefusal(
                refusal_code, "release source must be one bounded regular file"
            )
        verification_descriptor = _open_release_descriptor(
            root,
            relative,
            directory=False,
            refusal_code=refusal_code,
        )
        current = os.fstat(verification_descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or current.st_size != opened.st_size
            or current.st_mtime_ns != opened.st_mtime_ns
        ):
            raise StorageRefusal(
                refusal_code, "release file identity changed during preflight"
            )
    except StorageRefusal:
        raise
    except OSError as exc:
        raise StorageRefusal(
            refusal_code, "release source requires a stable regular file"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if verification_descriptor is not None:
            os.close(verification_descriptor)


def _read_regular_file(
    path: Path,
    *,
    root: Path,
    refusal_code: str,
    capture: bool,
) -> tuple[Optional[bytes], str]:
    descriptor: Optional[int] = None
    verification_descriptor: Optional[int] = None
    try:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise StorageRefusal(
                refusal_code, "release path escapes its explicit root"
            ) from exc
        descriptor = _open_release_descriptor(
            root,
            relative,
            directory=False,
            refusal_code=refusal_code,
        )
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if not stat.S_ISREG(opened.st_mode):
            raise StorageRefusal(
                refusal_code, "release bytes must come from a regular file"
            )
        if opened.st_size > MAX_RELEASE_FILE_BYTES:
            raise StorageRefusal(
                refusal_code, "release file exceeds the bounded staging size"
            )
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        byte_count = 0
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            while chunk := handle.read(RELEASE_READ_CHUNK_BYTES):
                byte_count += len(chunk)
                if byte_count > MAX_RELEASE_FILE_BYTES:
                    raise StorageRefusal(
                        refusal_code, "release file exceeds the bounded staging size"
                    )
                digest.update(chunk)
                if capture:
                    chunks.append(chunk)
            after_descriptor = os.fstat(handle.fileno())
        verification_descriptor = _open_release_descriptor(
            root,
            relative,
            directory=False,
            refusal_code=refusal_code,
        )
        current = os.fstat(verification_descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or (after_descriptor.st_dev, after_descriptor.st_ino) != identity
            or (current.st_dev, current.st_ino) != identity
            or current.st_size != opened.st_size
            or current.st_mtime_ns != opened.st_mtime_ns
            or after_descriptor.st_size != opened.st_size
            or after_descriptor.st_mtime_ns != opened.st_mtime_ns
            or byte_count != opened.st_size
        ):
            raise StorageRefusal(
                refusal_code, "release file identity changed while it was read"
            )
        return (b"".join(chunks) if capture else None), digest.hexdigest()
    except StorageRefusal:
        raise
    except OSError as exc:
        raise StorageRefusal(
            refusal_code, "release bytes require a stable regular file"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if verification_descriptor is not None:
            os.close(verification_descriptor)


def _read_regular_bytes(path: Path, *, root: Path, refusal_code: str) -> bytes:
    payload, _ = _read_regular_file(
        path, root=root, refusal_code=refusal_code, capture=True
    )
    if payload is None:
        raise AssertionError("captured release bytes are required")
    return payload


def _regular_file_digest(path: Path, *, root: Path, refusal_code: str) -> str:
    _, digest = _read_regular_file(
        path, root=root, refusal_code=refusal_code, capture=False
    )
    return digest


def _directory_identity(path: Path) -> tuple[int, int]:
    status = path.lstat()
    if not stat.S_ISDIR(status.st_mode):
        raise OSError("reserved release identity is not a directory")
    return status.st_dev, status.st_ino


def _remove_reserved_directory(
    path: Path,
    identity: tuple[int, int],
    *,
    recursive: bool,
    fault: Optional[FaultInjector] = None,
) -> None:
    if fault is not None:
        fault(f"before_reserved_cleanup:{path.name}")
    quarantine = path.with_name(
        f".{path.name}.cleanup-{secrets.token_hex(16)}"
    )
    moved = False
    try:
        os.rename(path, quarantine)
        moved = True
        try:
            matches = _directory_identity(quarantine) == identity
        except OSError:
            matches = False
        if not matches:
            if not os.path.lexists(path):
                os.rename(quarantine, path)
            return
        if recursive:
            shutil.rmtree(quarantine)
        else:
            quarantine.rmdir()
    except OSError:
        if moved and os.path.lexists(quarantine) and not os.path.lexists(path):
            try:
                os.rename(quarantine, path)
            except OSError:
                pass


def _cleanup_reserved_directories(
    reserved: list[tuple[Path, tuple[int, int]]],
    *,
    recursive: bool,
    fault: Optional[FaultInjector] = None,
) -> None:
    for directory, identity in reversed(reserved):
        try:
            _remove_reserved_directory(
                directory,
                identity,
                recursive=recursive,
                fault=fault,
            )
        except BaseException:
            pass


def _atomic_write_release(
    root: Path,
    relative: Path,
    payload: bytes,
    *,
    mode: int,
) -> None:
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise StorageRefusal(
            "staged_parity_failed", "staged release path escapes its root"
        )
    directories: list[int] = []
    temporary: Optional[str] = None
    try:
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        current = os.open(root, flags)
        directories.append(current)
        for component in relative.parts[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            current = os.open(component, flags, dir_fd=current)
            directories.append(current)
        temporary = f".{relative.name}.tmp-{secrets.token_hex(16)}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=current,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), mode)
            os.link(
                temporary,
                relative.name,
                src_dir_fd=current,
                dst_dir_fd=current,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=current)
            temporary = None
            os.fsync(current)
        except BaseException:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=current)
                except OSError:
                    pass
            raise
    except StorageRefusal:
        raise
    except OSError as exc:
        raise StorageRefusal(
            "staged_parity_failed",
            "staged release write requires non-symlink directories",
        ) from exc
    finally:
        for directory in reversed(directories):
            os.close(directory)


def _write_staging_reservation(
    reserved: list[tuple[Path, tuple[int, int]]],
    source_version_sha256: str,
) -> dict[str, Any]:
    if len(reserved) != 2:
        raise AssertionError("bundle and release reservations are required")
    marker = {
        "schema": STAGING_RESERVATION_SCHEMA,
        "version": __version__,
        "token": secrets.token_hex(32),
        "source_version_sha256": source_version_sha256,
        "bundle_identity": list(reserved[0][1]),
        "release_identity": list(reserved[1][1]),
    }
    for directory, _ in reserved:
        _write_json(directory / STAGING_RESERVATION_FILENAME, marker)
    return marker


def _read_staging_reservation(directory: Path) -> dict[str, Any]:
    payload = _read_regular_bytes(
        directory / STAGING_RESERVATION_FILENAME,
        root=directory,
        refusal_code="staged_release_identity_exists",
    )
    try:
        marker = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs
        )
    except (UnicodeError, ValueError) as exc:
        raise StorageRefusal(
            "staged_release_identity_exists",
            "partial staging reservation is malformed",
        ) from exc
    if (
        not isinstance(marker, dict)
        or set(marker)
        != {
            "schema",
            "version",
            "token",
            "source_version_sha256",
            "bundle_identity",
            "release_identity",
        }
        or marker["schema"] != STAGING_RESERVATION_SCHEMA
        or marker["version"] != __version__
        or not isinstance(marker["token"], str)
        or re.fullmatch(r"[0-9a-f]{64}", marker["token"]) is None
        or not isinstance(marker["source_version_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", marker["source_version_sha256"]) is None
        or any(
            not isinstance(value, list)
            or len(value) != 2
            or any(not isinstance(item, int) for item in value)
            for value in (
                marker["bundle_identity"],
                marker["release_identity"],
            )
        )
    ):
        raise StorageRefusal(
            "staged_release_identity_exists",
            "partial staging reservation is unsupported",
        )
    return marker


def _recover_version_only_staging(
    release_bundle: Path,
    release: Path,
    source_root: Path,
) -> None:
    candidates = (release_bundle, release)
    if not any(os.path.lexists(candidate) for candidate in candidates):
        return
    if not all(os.path.lexists(candidate) for candidate in candidates):
        raise StorageRefusal(
            "staged_release_identity_exists",
            "the candidate release identity is only partially allocated",
        )
    bundle_marker = _read_staging_reservation(release_bundle)
    release_marker = _read_staging_reservation(release)
    if bundle_marker != release_marker:
        raise StorageRefusal(
            "staged_release_identity_exists",
            "partial staging reservations disagree",
        )
    identities = [
        tuple(bundle_marker["bundle_identity"]),
        tuple(bundle_marker["release_identity"]),
    ]
    source_version_sha256 = _regular_file_digest(
        source_root / "VERSION",
        root=source_root,
        refusal_code="release_incomplete",
    )
    if bundle_marker["source_version_sha256"] != source_version_sha256:
        raise StorageRefusal(
            "staged_release_identity_exists",
            "partial staging source identity changed",
        )
    for directory, identity in zip(candidates, identities):
        if _directory_identity(directory) != identity:
            raise StorageRefusal(
                "staged_release_identity_exists",
                "partial staging directory identity changed",
            )
        entries = sorted(
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
        )
        if entries != [STAGING_RESERVATION_FILENAME, "VERSION"]:
            raise StorageRefusal(
                "staged_release_identity_exists",
                "partial staging contents are not safely recoverable",
            )
        if _regular_file_digest(
            directory / "VERSION",
            root=directory,
            refusal_code="staged_release_identity_exists",
        ) != source_version_sha256:
            raise StorageRefusal(
                "staged_release_identity_exists",
                "partial staged VERSION differs from source",
            )
    _cleanup_reserved_directories(
        list(zip(candidates, identities)), recursive=True
    )
    if any(os.path.lexists(candidate) for candidate in candidates):
        raise StorageRefusal(
            "staged_release_identity_exists",
            "partial staging recovery did not complete",
        )


def _remove_staging_reservation(
    reserved: list[tuple[Path, tuple[int, int]]],
    marker: dict[str, Any],
) -> None:
    for directory, identity in reserved:
        if _directory_identity(directory) != identity:
            raise StorageRefusal(
                "staged_parity_failed", "staging reservation identity changed"
            )
        if _read_staging_reservation(directory) != marker:
            raise StorageRefusal(
                "staged_parity_failed", "staging reservation marker changed"
            )
        (directory / STAGING_RESERVATION_FILENAME).unlink()


def _switch_symlink(link: Path, target: str) -> None:
    temporary = link.with_name(f".{link.name}.next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def _run_checked(arguments: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise StorageRefusal("staged_check_failed", "a staged executable check failed")
    return result.stdout


def _stage_release_bytes(
    source_root: Path,
    release_bundle: Path,
    release: Path,
    forbidden_paths: tuple[bytes, ...],
    *,
    fault: Optional[FaultInjector] = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    source_hashes: dict[str, str] = {}
    release_hashes: dict[str, str] = {}
    staged_hashes: dict[str, str] = {}
    for source in _release_files(source_root):
        relative = source.relative_to(source_root)
        name = relative.as_posix()
        mode = 0o755 if relative.parent == Path("bin") else 0o644
        payload = _read_regular_bytes(
            source, root=source_root, refusal_code="release_incomplete"
        )
        if any(value in payload for value in forbidden_paths):
            raise StorageRefusal(
                "staged_path_leak", "release source bytes contain a local path leak"
            )
        digest = _sha256(payload)
        source_hashes[name] = digest
        bundle_file = release_bundle / relative
        destination = release / relative
        _atomic_write_release(release_bundle, relative, payload, mode=mode)
        _atomic_write_release(release, relative, payload, mode=mode)
        release_hashes[name] = _regular_file_digest(
            bundle_file,
            root=release_bundle,
            refusal_code="staged_parity_failed",
        )
        staged_hashes[name] = _regular_file_digest(
            destination,
            root=release,
            refusal_code="staged_parity_failed",
        )
        if fault is not None:
            fault(f"after_release_file:{name}")
    if not source_hashes == release_hashes == staged_hashes:
        raise StorageRefusal(
            "staged_parity_failed", "source, release bundle, and staged bytes differ"
        )
    return source_hashes, release_hashes, staged_hashes


def _staged_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
    }


def _check_staged_launcher(
    stable: Path, home: Path, environment: dict[str, str]
) -> dict[str, Any]:
    version_output = _run_checked(
        [str(stable), "--version"], cwd=home, env=environment
    ).strip()
    help_output = _run_checked([str(stable), "--help"], cwd=home, env=environment)
    if version_output != f"league {__version__}" or "acceptance" not in help_output:
        raise StorageRefusal("staged_version_failed", "staged launcher version/help mismatch")
    staged_state = home / "staged-schema-state"
    staged_state.mkdir(mode=0o700)
    staged_migration = json.loads(
        _run_checked(
            [
                str(stable),
                "--state-root",
                str(staged_state),
                "--no-wal",
                "storage",
                "migrate",
            ],
            cwd=home,
            env=environment,
        )
    )
    staged_integrity = json.loads(
        _run_checked(
            [
                str(stable),
                "--state-root",
                str(staged_state),
                "--no-wal",
                "storage",
                "integrity",
            ],
            cwd=home,
            env=environment,
        )
    )
    migration_result = staged_migration.get("result", {})
    integrity_result = staged_integrity.get("result", {})
    if (
        not staged_migration.get("ok")
        or migration_result.get("to_version") != CURRENT_SCHEMA_VERSION
        or not staged_integrity.get("ok")
        or not integrity_result.get("ok")
    ):
        raise StorageRefusal(
            "staged_schema_failed", "staged schema migration or integrity check failed"
        )
    return {
        "to_version": migration_result["to_version"],
        "journal_mode": migration_result["policy"]["journal_mode"],
        "integrity": integrity_result["ok"],
    }


def _check_staged_schemas_and_hooks(
    release: Path, home: Path, environment: dict[str, str]
) -> tuple[int, list[dict[str, Any]]]:
    schema_files = sorted((release / "schema").glob("*.json"))
    for schema_file in schema_files:
        value = json.loads(schema_file.read_text(encoding="utf-8"))
        if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise StorageRefusal("staged_schema_failed", "staged schema is malformed")
    routing_script = (
        "import json,sys;"
        "sys.path.insert(0,sys.argv[1]);"
        "from league.routing import load_routing_config;"
        "print(json.dumps(load_routing_config(__import__('pathlib').Path(sys.argv[2])),"
        "sort_keys=True,separators=(',',':')))"
    )
    routing = json.loads(
        _run_checked(
            [
                sys.executable,
                "-c",
                routing_script,
                str(release / "src"),
                str(release / "config/league-model-routing.example.json"),
            ],
            cwd=home,
            env={**environment, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    )
    if routing.get("schema") != 3:
        raise StorageRefusal(
            "staged_schema_failed", "installed model routing policy is not schema 3"
        )
    hook_script = (
        "import json,sys;"
        "sys.path.insert(0,sys.argv[1]);"
        "from league.acceptance import validate_hook_fixture;"
        "h=sys.argv[2];"
        "p={'schema':'league.synthetic-hook.v1','harness':h,'event':'stop',"
        "'session_ref':'synthetic-'+h+'-session'};"
        "print(json.dumps(validate_hook_fixture(h,p),sort_keys=True,separators=(',',':')))"
    )
    hook_environment = {**environment, "PYTHONDONTWRITEBYTECODE": "1"}
    hook_checks = [
        json.loads(
            _run_checked(
                [sys.executable, "-c", hook_script, str(release / "src"), harness],
                cwd=home,
                env=hook_environment,
            )
        )
        for harness in ("codex", "cursor", "pi")
    ]
    watcher_help = _run_checked(
        [str(release / "bin/agent-watcher"), "--help"],
        cwd=home,
        env=environment,
    )
    if "usage:" not in watcher_help:
        raise StorageRefusal("staged_check_failed", "staged watcher help check failed")
    return len(schema_files), hook_checks


def _check_staged_manifest_unchanged(
    release: Path, manifest: Mapping[str, str]
) -> None:
    actual: dict[str, str] = {}
    for candidate in sorted(release.rglob("*")):
        relative = candidate.relative_to(release).as_posix()
        status = candidate.lstat()
        if stat.S_ISDIR(status.st_mode):
            continue
        if not stat.S_ISREG(status.st_mode) or candidate.is_symlink():
            raise StorageRefusal(
                "staged_parity_failed", "staged runtime created a non-regular release node"
            )
        actual[relative] = _regular_file_digest(
            candidate,
            root=release,
            refusal_code="staged_parity_failed",
        )
    if actual != dict(manifest):
        raise StorageRefusal(
            "staged_parity_failed", "staged runtime changed the immutable release manifest"
        )


def _check_staged_permissions(
    release_bundle: Path,
    prefix: Path,
    releases: Path,
    release: Path,
    legacy: Path,
    manifest: dict[str, str],
) -> None:
    expected_modes = {
        relative: (0o755 if Path(relative).parent == Path("bin") else 0o644)
        for relative in manifest
    }
    staged_modes = {
        relative: (release / relative).stat().st_mode & 0o777 for relative in manifest
    }
    release_modes = {
        relative: (release_bundle / relative).stat().st_mode & 0o777
        for relative in manifest
    }
    if staged_modes != expected_modes or release_modes != expected_modes or any(
        path.stat().st_mode & 0o022
        for path in (release_bundle, prefix, releases, release, legacy)
    ):
        raise StorageRefusal(
            "staged_permissions_failed", "staged release permissions are not owner-controlled"
        )


def _rollback_staged_pointer(
    current: Path,
    stable: Path,
    previous_target: str,
    home: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    _switch_symlink(current, previous_target)
    rollback_version = _run_checked(
        [str(stable), "--version"], cwd=home, env=environment
    ).strip()
    if rollback_version != "league 0.0.0-legacy":
        raise StorageRefusal(
            "staged_rollback_failed", "staged stable pointer did not roll back"
        )
    return {
        "completed": True,
        "restored_target": previous_target,
        "observed_version": rollback_version.removeprefix("league "),
    }


def _stage_guidance_rehearsal(
    home: Path,
    release: Path,
    guidance_target: str,
    staged_hashes: dict[str, str],
) -> dict[str, Any]:
    agents_root = home / "synthetic-agents"
    agents_root.mkdir(mode=0o700)
    universal = b"synthetic toolkit-owned universal guide\n"
    prior_league = b"synthetic prior League supplement\n"
    _atomic_write(agents_root / "AGENTS.md", universal)
    _atomic_write(agents_root / guidance_target, prior_league)
    universal_before = _sha256((agents_root / "AGENTS.md").read_bytes())
    guidance_stage = stage_guidance(
        (release / "global-agent-instructions/league/AGENTS.md").resolve(),
        "codex",
        agents_root.resolve(),
        target=guidance_target,
    )
    if guidance_stage["installed_sha256"] != staged_hashes[
        "global-agent-instructions/league/AGENTS.md"
    ]:
        raise StorageRefusal(
            "staged_parity_failed",
            "packaged and installed League guidance bytes differ",
        )
    guidance_rollback = rollback_guidance(
        agents_root.resolve(),
        "codex",
        guidance_stage,
        target=guidance_target,
    )
    universal_after_rollback = _sha256((agents_root / "AGENTS.md").read_bytes())
    restored_league = _sha256((agents_root / guidance_target).read_bytes())
    if (
        universal_before != guidance_stage["universal_after_sha256"]
        or universal_before != universal_after_rollback
        or restored_league != _sha256(prior_league)
    ):
        raise StorageRefusal(
            "staged_rollback_failed",
            "guide ownership or League supplement rollback did not verify",
        )
    return {
        "source": "global-agent-instructions/league/AGENTS.md",
        "target": guidance_target,
        "source_sha256": guidance_stage["source_sha256"],
        "installed_sha256": guidance_stage["installed_sha256"],
        "prior_sha256": guidance_stage["prior_sha256"],
        "restored_sha256": guidance_rollback["restored_sha256"],
        "universal_before_sha256": universal_before,
        "universal_after_install_sha256": guidance_stage["universal_after_sha256"],
        "universal_after_rollback_sha256": universal_after_rollback,
        "universal_unchanged": True,
        "rollback_completed": guidance_rollback["completed"],
    }


def _staged_install(
    home: Path,
    source_root: Path,
    *,
    guidance_targets: tuple[str, ...] = (LEAGUE_TARGET,),
    fault: Optional[FaultInjector] = None,
) -> dict[str, Any]:
    guidance_target = validate_guidance_manifest(guidance_targets)[0]
    _release_files(source_root)
    version_bytes = _read_regular_bytes(
        source_root / "VERSION",
        root=source_root,
        refusal_code="release_incomplete",
    )
    try:
        source_version = version_bytes.decode("utf-8").strip()
    except UnicodeError as exc:
        raise StorageRefusal(
            "staged_version_failed", "source version declaration is malformed"
        ) from exc
    if source_version != __version__:
        raise StorageRefusal("staged_version_failed", "source version declarations disagree")
    prefix = home / "stage-prefix"
    release_bundle = home / "release-bundle" / __version__
    releases = prefix / "releases"
    release = releases / __version__
    legacy = releases / "0.0.0-legacy"
    _recover_version_only_staging(release_bundle, release, source_root)
    if any(
        candidate.exists() or candidate.is_symlink()
        for candidate in (release_bundle, release)
    ):
        raise StorageRefusal(
            "staged_release_identity_exists",
            "the candidate release identity is already allocated",
        )
    for directory in (
        release_bundle.parent,
        prefix,
        releases,
        prefix / "bin",
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    reserved: list[tuple[Path, tuple[int, int]]] = []
    try:
        release_bundle.mkdir(mode=0o700)
        reserved.append((release_bundle, _directory_identity(release_bundle)))
        release.mkdir(mode=0o700)
        reserved.append((release, _directory_identity(release)))
    except FileExistsError as exc:
        _cleanup_reserved_directories(reserved, recursive=False)
        raise StorageRefusal(
            "staged_release_identity_exists",
            "the candidate release identity was allocated concurrently",
        ) from exc
    marker = _write_staging_reservation(reserved, _sha256(version_bytes))
    forbidden = (str(source_root).encode(), str(prefix).encode())
    try:
        source_hashes, release_hashes, staged_hashes = _stage_release_bytes(
            source_root, release_bundle, release, forbidden, fault=fault
        )
        if source_hashes["VERSION"] != _sha256(version_bytes):
            raise StorageRefusal(
                "staged_parity_failed", "source version changed during release staging"
            )
        _remove_staging_reservation(reserved, marker)
    except BaseException:
        _cleanup_reserved_directories(reserved, recursive=True, fault=fault)
        raise
    legacy.mkdir(mode=0o700)
    (legacy / "bin").mkdir(mode=0o700)
    legacy_launcher = legacy / "bin/league"
    _atomic_write(
        legacy_launcher,
        b"#!/usr/bin/env python3\nprint('league 0.0.0-legacy')\n",
        mode=0o755,
    )
    guidance = _stage_guidance_rehearsal(
        home, release, guidance_target, staged_hashes
    )
    current = prefix / "current"
    current.symlink_to("releases/0.0.0-legacy")
    stable = prefix / "bin/league"
    stable.symlink_to("../current/bin/league")
    previous_target = os.readlink(current)
    environment = _staged_environment()
    switched = False
    try:
        _switch_symlink(current, f"releases/{__version__}")
        switched = True
        if fault is not None:
            fault("before_staged_launcher_validation")
        schema_migration = _check_staged_launcher(stable, home, environment)
        schema_count, hook_checks = _check_staged_schemas_and_hooks(
            release, home, environment
        )
        _check_staged_manifest_unchanged(release, staged_hashes)
        _check_staged_permissions(
            release_bundle, prefix, releases, release, legacy, staged_hashes
        )
        rollback = _rollback_staged_pointer(
            current, stable, previous_target, home, environment
        )
        switched = False
    except BaseException:
        if switched:
            try:
                _rollback_staged_pointer(
                    current, stable, previous_target, home, environment
                )
            except BaseException:
                pass
        raise
    source_manifest_digest = _sha256(_stable_bytes(source_hashes))
    release_manifest_digest = _sha256(_stable_bytes(release_hashes))
    staged_manifest_digest = _sha256(_stable_bytes(staged_hashes))
    return {
        "prefix": str(prefix),
        "version": __version__,
        "source_release_staged_parity": True,
        "source_manifest_sha256": source_manifest_digest,
        "release_manifest_sha256": release_manifest_digest,
        "staged_manifest_sha256": staged_manifest_digest,
        "file_count": len(source_hashes),
        "launcher_resolution": True,
        "help_checked": True,
        "schemas_checked": schema_count,
        "schema_migration": schema_migration,
        "hook_fixtures": hook_checks,
        "permissions_checked": True,
        "path_leaks": False,
        "guidance": guidance,
        "rollback": rollback,
    }


def _operation_write(
    path: Path,
    receipt: dict[str, Any],
    state: str,
    context: DeterministicContext,
    **extra: Any,
) -> None:
    receipt["state"] = state
    receipt["history"].append({"state": state, "at": context.at, **extra})
    _write_json(path, receipt)


CUTOVER_CHILD_ERROR_EXIT = 87
CUTOVER_RECOVERY_ERROR_EXIT = 88


def _set_sandbox_writers(
    path: Path,
    values: list[dict[str, str]],
    history_path: Path,
) -> None:
    if len(values) > 1:
        raise StorageRefusal("dual_writer", "two canonical writers are forbidden")
    _write_json(path, {"active": values})
    history = (
        json.loads(history_path.read_text(encoding="utf-8"))["snapshots"]
        if history_path.exists()
        else []
    )
    history.append([item["generation"] for item in values])
    _write_json(history_path, {"snapshots": history})


def _coherent_sandbox_writer(pointer_path: Path, writers_path: Path) -> dict[str, Any]:
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    writers = json.loads(writers_path.read_text(encoding="utf-8"))["active"]
    if len(writers) != 1 or writers[0]["generation"] != pointer["generation"]:
        raise StorageRefusal("generation_mismatch", "writer and pointer generations disagree")
    return pointer


def _write_cutover_journal(
    path: Path,
    old: dict[str, str],
    new: dict[str, str],
    stage: str,
    state: str,
    *,
    selected_generation: Optional[str] = None,
) -> None:
    value: dict[str, Any] = {
        "schema": "league.cutover-journal.v1",
        "state": state,
        "stage": stage,
        "old": old,
        "new": new,
    }
    if selected_generation is not None:
        value["selected_generation"] = selected_generation
    _write_json(path, value)


def _hard_crash_cutover_process(fault_stage: Optional[str], stage: str) -> None:
    if fault_stage == stage:
        os.kill(os.getpid(), signal.SIGKILL)
        os._exit(CUTOVER_CHILD_ERROR_EXIT)


def _execute_cutover_switch(
    root: Path,
    context: DeterministicContext,
    old: dict[str, str],
    new: dict[str, str],
    fault_stage: Optional[str],
) -> None:
    lock_path = root / "cutover.lock"
    pointer_path = root / "writer-pointer.json"
    writers_path = root / "writers.json"
    writer_history_path = root / "writer-history.json"
    journal_path = root / "cutover-journal.json"
    operation_path = root / "operation.json"
    receipt = json.loads(operation_path.read_text(encoding="utf-8"))
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        second = lock_path.open("a+b")
        try:
            try:
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_exclusive = True
            else:
                lock_exclusive = False
                fcntl.flock(second, fcntl.LOCK_UN)
        finally:
            second.close()
        if not lock_exclusive:
            raise StorageRefusal(
                "cutover_lock_failed", "exclusive cutover lock was not exclusive"
            )
        _operation_write(
            operation_path, receipt, "executing", context, stage="lock_acquired"
        )
        _write_cutover_journal(
            journal_path, old, new, "lock_acquired", "executing"
        )
        _hard_crash_cutover_process(fault_stage, "lock_acquired")
        _write_json(root / "pointer-backup.json", old)
        _write_cutover_journal(
            journal_path, old, new, "backup_recorded", "executing"
        )
        _hard_crash_cutover_process(fault_stage, "backup_recorded")
        _set_sandbox_writers(writers_path, [], writer_history_path)
        _write_cutover_journal(
            journal_path, old, new, "old_writer_quiesced", "executing"
        )
        _hard_crash_cutover_process(fault_stage, "old_writer_quiesced")
        _write_json(root / "pointer.next.json", new)
        _write_cutover_journal(
            journal_path, old, new, "pointer_prepared", "executing"
        )
        _hard_crash_cutover_process(fault_stage, "pointer_prepared")
        os.replace(root / "pointer.next.json", pointer_path)
        _write_cutover_journal(
            journal_path, old, new, "pointer_switched", "executing"
        )
        _hard_crash_cutover_process(fault_stage, "pointer_switched")
        _set_sandbox_writers(writers_path, [new], writer_history_path)
        _write_cutover_journal(
            journal_path, old, new, "new_writer_activated", "executing"
        )
        _hard_crash_cutover_process(fault_stage, "new_writer_activated")
        _coherent_sandbox_writer(pointer_path, writers_path)
        _write_cutover_journal(
            journal_path, old, new, "generation_verified", "executing"
        )
        _hard_crash_cutover_process(fault_stage, "generation_verified")
        _operation_write(operation_path, receipt, "completed", context, outcome="new")
        _write_cutover_journal(
            journal_path,
            old,
            new,
            "generation_verified",
            "completed",
            selected_generation=new["generation"],
        )


def _reconcile_cutover_startup(
    root: Path,
    *,
    resume: bool,
) -> dict[str, Any]:
    lock_path = root / "cutover.lock"
    pointer_path = root / "writer-pointer.json"
    writers_path = root / "writers.json"
    writer_history_path = root / "writer-history.json"
    journal_path = root / "cutover-journal.json"
    operation_path = root / "operation.json"
    operation = json.loads(operation_path.read_text(encoding="utf-8"))
    context = DeterministicContext()
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        old = journal.get("old")
        new = journal.get("new")
        if (
            journal.get("schema") != "league.cutover-journal.v1"
            or journal.get("state") != "executing"
            or not isinstance(old, dict)
            or not isinstance(new, dict)
            or set(old) != {"schema", "generation", "writer", "version"}
            or set(new) != {"schema", "generation", "writer", "version"}
            or old.get("schema") != "league.writer-pointer.v1"
            or new.get("schema") != "league.writer-pointer.v1"
            or old.get("generation") == new.get("generation")
            or journal.get("stage") != operation["fault_stage"]
        ):
            raise StorageRefusal(
                "cutover_recovery_failed", "cutover recovery journal is inconsistent"
            )
        selected = json.loads(pointer_path.read_text(encoding="utf-8"))
        if selected not in (old, new):
            raise StorageRefusal(
                "cutover_recovery_failed", "cutover pointer is not a journal generation"
            )
        _set_sandbox_writers(writers_path, [selected], writer_history_path)
        _coherent_sandbox_writer(pointer_path, writers_path)
        _write_cutover_journal(
            journal_path,
            old,
            new,
            journal["stage"],
            "reconciled",
            selected_generation=selected["generation"],
        )
        _operation_write(
            operation_path,
            operation,
            "blocked",
            context,
            stage=operation["fault_stage"],
            resumable=True,
        )
        if resume:
            _operation_write(
                operation_path, operation, "executing", context, stage="resume"
            )
            outcome = "new" if selected["generation"] == new["generation"] else "old"
            _operation_write(
                operation_path, operation, "completed", context, outcome=outcome
            )
        return selected


def _cutover_case(
    root: Path,
    context: DeterministicContext,
    fault_stage: Optional[str],
    *,
    resume: bool,
) -> dict[str, Any]:
    root.mkdir(parents=True, mode=0o700)
    pointer_path = root / "writer-pointer.json"
    writers_path = root / "writers.json"
    writer_history_path = root / "writer-history.json"
    operation_path = root / "operation.json"
    journal_path = root / "cutover-journal.json"
    old = {
        "schema": "league.writer-pointer.v1",
        "generation": "generation-old",
        "writer": "legacy",
        "version": "0.0.0-legacy",
    }
    new = {
        "schema": "league.writer-pointer.v1",
        "generation": "generation-new",
        "writer": "sqlite",
        "version": __version__,
    }
    _write_json(pointer_path, old)
    _set_sandbox_writers(writers_path, [old], writer_history_path)
    receipt = {
        "schema": "league.cutover-operation.v1",
        "operation_id": context.identifier("cutover-operation"),
        "fault_stage": fault_stage,
        "history": [],
    }
    _operation_write(operation_path, receipt, "planned", context)
    if fault_stage is None:
        _execute_cutover_switch(root, context, old, new, None)
        crash_signal = None
        recovery_exit = None
        startup_reconciled = False
        process_restart_simulated = False
    else:
        crash_pid = os.fork()
        if crash_pid == 0:
            try:
                _execute_cutover_switch(root, context, old, new, fault_stage)
            except BaseException:
                os._exit(CUTOVER_CHILD_ERROR_EXIT)
            os._exit(CUTOVER_CHILD_ERROR_EXIT)
        _, crash_status = os.waitpid(crash_pid, 0)
        crash_exit = os.waitstatus_to_exitcode(crash_status)
        if crash_exit != -signal.SIGKILL:
            raise StorageRefusal(
                "cutover_crash_failed", "cutover child did not stop at the fault stage"
            )
        recovery_pid = os.fork()
        if recovery_pid == 0:
            try:
                _reconcile_cutover_startup(root, resume=resume)
            except BaseException:
                os._exit(CUTOVER_RECOVERY_ERROR_EXIT)
            os._exit(0)
        _, recovery_status = os.waitpid(recovery_pid, 0)
        recovery_exit = os.waitstatus_to_exitcode(recovery_status)
        if recovery_exit != 0:
            raise StorageRefusal(
                "cutover_recovery_failed", "restart reconciliation process failed"
            )
        crash_signal = "SIGKILL"
        startup_reconciled = True
        process_restart_simulated = True
    final = _coherent_sandbox_writer(pointer_path, writers_path)
    receipt = json.loads(operation_path.read_text(encoding="utf-8"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    writer_history = json.loads(writer_history_path.read_text(encoding="utf-8"))[
        "snapshots"
    ]
    return {
        "fault_stage": fault_stage,
        "terminal_state": receipt["state"],
        "final_generation": final["generation"],
        "history": receipt["history"],
        "max_active_writers": max(len(item) for item in writer_history),
        "lock_exclusive": True,
        "coherent": True,
        "journal_state": journal["state"],
        "startup_reconciled": startup_reconciled,
        "process_restart_simulated": process_restart_simulated,
        "crash_signal": crash_signal,
        "recovery_exit": recovery_exit,
    }


def _cutover_matrix(home: Path, context: DeterministicContext) -> dict[str, Any]:
    cases = [_cutover_case(home / "normal", context, None, resume=True)]
    for index, stage in enumerate(POINTER_STAGES):
        cases.append(
            _cutover_case(
                home / f"fault-{index:02d}-{stage}",
                context,
                stage,
                resume=index != len(POINTER_STAGES) - 1,
            )
        )
    if any(not case["coherent"] or case["max_active_writers"] > 1 for case in cases):
        raise StorageRefusal("cutover_matrix_failed", "cutover matrix left an incoherent writer")
    return {
        "pointer_schema": "league.writer-pointer.v1",
        "generation_bound": True,
        "exclusive_lock": True,
        "crash_recovery_journal": True,
        "fault_stages": list(POINTER_STAGES),
        "cases": cases,
        "never_two_writers": True,
    }


def _canary(
    home: Path,
    context: DeterministicContext,
    adapters: dict[str, FakeAdapter],
) -> dict[str, Any]:
    process = adapters["process_resource"]
    if not isinstance(process, FakeProcessResourceAdapter):
        raise StorageRefusal("adapter_mismatch", "synthetic process adapter is unavailable")
    generation = "synthetic-canary-generation-0001"
    identity = {
        "pid": 4242,
        "process_start": "synthetic-start-0001",
        "endpoint": "synthetic://canary/endpoint-0001",
        "generation": generation,
    }
    process.start(**identity)
    resource = {
        "resource_id": context.identifier("canary-resource"),
        "task_id": "synthetic-acceptance-task",
        "owner": "SyntheticChampion",
        "generation": generation,
        **identity,
    }
    registry = home / "canary-resources.json"
    _write_json(registry, {"schema": "league.canary-resources.v1", "resources": [resource]})
    observed = json.loads(registry.read_text(encoding="utf-8"))["resources"]
    if observed != [resource]:
        raise StorageRefusal("resource_identity_mismatch", "canary registration changed")
    wrong_identity = dict(identity, generation="synthetic-wrong-generation")
    try:
        process.terminate(wrong_identity)
    except StorageRefusal as exc:
        if exc.code != "resource_identity_mismatch":
            raise
        mismatch_refused = True
    else:
        raise StorageRefusal("resource_cleanup_failed", "wrong-generation cleanup was accepted")
    cleanup = process.terminate(identity)
    _write_json(registry, {"schema": "league.canary-resources.v1", "resources": []})
    if json.loads(registry.read_text(encoding="utf-8"))["resources"]:
        raise StorageRefusal("resource_cleanup_failed", "canary registry was not released")
    return {
        "registered_exactly": True,
        "resource_id": resource["resource_id"],
        "generation": resource["generation"],
        "cleanup_exact": True,
        "wrong_generation_refused": mismatch_refused,
        "adapter_receipt": cleanup["receipt_id"],
        "real_runtime_proven": False,
    }


def _acceptance_operation_write(
    path: Path,
    operation: dict[str, Any],
    state: str,
    context: DeterministicContext,
    attempt: int,
    **extra: Any,
) -> None:
    operation["state"] = state
    operation["attempt"] = attempt
    operation["history"].append(
        {"state": state, "at": context.at, "attempt": attempt, **extra}
    )
    _write_json(path, operation)


def _load_blocked_acceptance_operation(
    path: Path, namespace: str, sentinel_sha256: str
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise StorageRefusal(
            "namespace_collision", "acceptance namespace already exists"
        )
    try:
        operation = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise StorageRefusal(
            "resume_refused", "acceptance operation receipt is malformed"
        ) from exc
    if (
        isinstance(operation, dict)
        and operation.get("schema") == "league.acceptance-operation.v1"
        and operation.get("namespace") == namespace
        and operation.get("state") != "blocked"
    ):
        raise StorageRefusal(
            "namespace_collision", "acceptance namespace already exists"
        )
    history = operation.get("history") if isinstance(operation, dict) else None
    last = history[-1] if isinstance(history, list) and history else None
    if (
        not isinstance(operation, dict)
        or set(operation)
        != {"schema", "namespace", "state", "attempt", "sentinel_sha256", "history"}
        or operation.get("schema") != "league.acceptance-operation.v1"
        or operation.get("namespace") != namespace
        or operation.get("state") != "blocked"
        or operation.get("sentinel_sha256") != sentinel_sha256
        or not isinstance(operation.get("attempt"), int)
        or isinstance(operation.get("attempt"), bool)
        or operation["attempt"] < 1
        or not isinstance(last, dict)
        or last.get("state") != "blocked"
        or last.get("resumable") is not True
    ):
        raise StorageRefusal(
            "resume_refused", "acceptance operation cannot resume safely"
        )
    return operation


def run_acceptance(
    temporary_root: Path,
    namespace: str,
    *,
    sentinel_paths: tuple[Path, ...],
    config_sentinel: Path,
    process_sentinel: Path,
    source_root: Optional[Path] = None,
) -> dict[str, Any]:
    root = Path(temporary_root)
    if (
        not root.is_absolute()
        or not root.is_dir()
        or root.is_symlink()
        or root.resolve() == Path("/")
    ):
        raise StorageRefusal(
            "invalid_temporary_root", "temporary root must be an explicit directory"
        )
    if not NAMESPACE_PATTERN.fullmatch(namespace):
        raise StorageRefusal("invalid_namespace", "acceptance namespace is invalid")
    if not sentinel_paths:
        raise StorageRefusal("sentinel_required", "at least one byte sentinel is required")
    if len(sentinel_paths) > MAX_ACCEPTANCE_SENTINEL_PATHS:
        raise StorageRefusal(
            "too_many_sentinels",
            f"at most {MAX_ACCEPTANCE_SENTINEL_PATHS} byte sentinels are allowed",
        )
    source = (source_root or Path(__file__).resolve().parents[2]).resolve()
    sentinels = SentinelSet(
        tuple(Path(path) for path in sentinel_paths),
        Path(config_sentinel),
        Path(process_sentinel),
    )
    sentinel_sha256 = _sha256(_stable_bytes(sentinels.before))
    home = root / f"league-{namespace}"
    try:
        home.mkdir(mode=0o700)
    except FileExistsError as exc:
        if not home.is_dir() or home.is_symlink():
            raise StorageRefusal(
                "namespace_collision", "acceptance namespace already exists"
            ) from exc
        operation = _load_blocked_acceptance_operation(
            home / "acceptance-operation.json", namespace, sentinel_sha256
        )
        attempt = operation["attempt"] + 1
    else:
        attempt = 1
        operation = {
            "schema": "league.acceptance-operation.v1",
            "namespace": namespace,
            "state": "planned",
            "attempt": attempt,
            "sentinel_sha256": sentinel_sha256,
            "history": [],
        }
    context = DeterministicContext()
    operation_path = home / "acceptance-operation.json"
    if not operation["history"]:
        _acceptance_operation_write(
            operation_path, operation, "planned", context, attempt
        )
    _acceptance_operation_write(
        operation_path, operation, "executing", context, attempt
    )
    work = home / "attempts" / f"attempt-{attempt:04d}"
    try:
        try:
            work.mkdir(parents=True, mode=0o700)
        except FileExistsError as exc:
            raise StorageRefusal(
                "resume_refused", "acceptance attempt directory already exists"
            ) from exc
        adapters = _fake_adapters(context)
        adapters["harness"].call("create", namespace=namespace)
        adapters["terminal_backend"].call("create-namespace", namespace=namespace)
        adapters["git"].call("inspect-fixture", repository="synthetic://repository")
        adapters["github"].call("inspect-fixture", repository="synthetic://repository")
        adapters["notification"].call("record-only", delivery="disabled")
        adapters["deployment"].call("record-only", deployment="disabled")
        for harness in ("codex", "cursor", "pi"):
            adapters["hook"].call(
                "consume-fixture",
                **validate_hook_fixture(
                    harness,
                    {
                        "schema": HOOK_FIXTURE_SCHEMA,
                        "harness": harness,
                        "event": "stop",
                        "session_ref": f"synthetic-{harness}-session",
                    },
                ),
            )
        migration = _migration_shadow(work, source)
        staged = _staged_install(work, source)
        cutover = _cutover_matrix(work / "cutover", context)
        canary = _canary(work, context, adapters)
        sentinel_receipt = sentinels.verify()
        adapter_receipt = {
            name: {
                "kind": f"fake-{adapter.name}",
                "calls": len(adapter.calls),
                "real": False,
            }
            for name, adapter in adapters.items()
        }
        _acceptance_operation_write(
            operation_path, operation, "completed", context, attempt
        )
        result = {
            "schema": RECEIPT_SCHEMA,
            "version": __version__,
            "namespace": namespace,
            "home": str(home),
            "operation": operation,
            "determinism": {"clock": context.at, "ids_allocated": context.sequence},
            "sentinels": sentinel_receipt,
            "migration_shadow": migration,
            "staged_install": staged,
            "cutover": cutover,
            "canary": canary,
            "adapters": adapter_receipt,
            "pending_assertions": [
                {
                    "slice": name,
                    "issue": issue,
                    "status": "pending",
                    "passed": False,
                    "reason": "owning lifecycle slice is not merged",
                }
                for name, issue in PENDING_SLICES
            ],
            "runtime_claims": [
                {"runtime": name, "status": "unverified", "mock_proof": False}
                for name in UNVERIFIED_RUNTIMES
            ],
        }
        _write_json(home / "acceptance-receipt.json", result)
        return result
    except BaseException as exc:
        error_code = exc.code if isinstance(exc, StorageRefusal) else "acceptance_failed"
        _acceptance_operation_write(
            operation_path,
            operation,
            "blocked",
            context,
            attempt,
            error_code=error_code,
            resumable=True,
        )
        raise
