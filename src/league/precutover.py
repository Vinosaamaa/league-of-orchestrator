"""Read-only live shadow and no-apply pre-cutover acceptance gate.

Every write performed here is beneath the caller-supplied temporary root.  The
plan names live inputs and proposed destinations explicitly; live inputs are
read and snapshotted, while proposed destinations are emitted only as an
authority-gated mutation manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import resource
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from . import MAX_ACCEPTANCE_SENTINEL_PATHS, __version__
from .acceptance import (
    NAMESPACE_PATTERN,
    PROCESS_SENTINEL_SCHEMA,
    DeterministicContext,
    SentinelSet,
    _canary,
    _cutover_matrix,
    _fake_adapters,
    _load_fixture_module,
    _migration_shadow,
    _sha256,
    _stable_bytes,
    _staged_install,
    _staged_environment,
    _run_checked,
    _write_json,
)
from .adapter_types import (
    BACKEND_CAPABILITIES,
    AdapterContract,
    AdapterInstruction,
    AdapterReceipt,
    OpaqueIdentity,
    RuntimeObservation,
)
from .adapters import AdapterRegistry, builtin_harness_contracts, builtin_registry
from .cleanup import (
    CLEANUP_ADAPTER_KINDS,
    CleanupAdapterRegistry,
    CleanupExecutor,
    CleanupPlanner,
)
from .importer import build_import_plan
from .orchestration import OrchestrationSignals
from .request_services import (
    AssignmentService,
    AssignmentSpec,
    DeliveryReceipt,
    DeliveryService,
)
from .runtime import RuntimeCreateSpec, RuntimeLifecycle
from .sqlite_store import CURRENT_SCHEMA_VERSION, SQLiteStorage
from .storage import (
    AnswerRequestCommand,
    DispatchRequestCommand,
    RequestResultCommand,
    RuntimeRegistrationCommand,
    StorageRefusal,
)


PLAN_SCHEMA = "league.pre-cutover-plan.v1"
RECEIPT_SCHEMA = "league.pre-cutover-receipt.v1"
MUTATION_SCHEMA = "league.cutover-mutation-manifest.v1"
OPERATION_SCHEMA = "league.pre-cutover-operation.v1"
TARGET_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_PLAN_BYTES = 256 * 1024
MAX_LIVE_TARGETS = 64
MAX_LEGACY_BINDINGS = 512
MAX_SNAPSHOT_ENTRIES = 20_000
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
TARGET_KINDS = frozenset(
    {
        "legacy_state",
        "installed_bundle",
        "stable_launcher",
        "hook_config",
        "watcher_state",
        "watcher_launcher",
        "writer_pointer",
        "configuration",
        "archive_metadata",
        "backup_root",
        "release_prefix",
        "sqlite_state",
        "archive_root",
    }
)
HARNESS_KINDS = frozenset({"codex", "cursor", "pi"})
SHOTCALLER_ID = "11111111-1111-4111-8111-111111111111"
CHAMPION_ID = "55555555-5555-4555-8555-555555555555"
BASE_TASK_ID = "synthetic-task-19"
LIFECYCLE_TASK_ID = "synthetic-precutover-task"
SYNTHETIC_REPOSITORY = "https://example.invalid/league.git"


def _decode_json_object(path: Path, *, label: str) -> dict[str, Any]:
    value_path = Path(path)
    if not value_path.is_absolute() or not value_path.is_file() or value_path.is_symlink():
        raise StorageRefusal("input_invalid", f"{label} must be an explicit regular file")
    if value_path.stat().st_size > MAX_PLAN_BYTES:
        raise StorageRefusal("input_too_large", f"{label} exceeds the size bound")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise StorageRefusal("duplicate_key", f"{label} contains a duplicate key")
            value[key] = item
        return value

    try:
        value = json.loads(
            value_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except StorageRefusal:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("input_invalid", f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise StorageRefusal("input_invalid", f"{label} must be an object")
    return value


def _absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise StorageRefusal("plan_invalid", f"{label} must be an exact absolute path")
    path = Path(value)
    if not path.is_absolute() or path == Path("/"):
        raise StorageRefusal("plan_invalid", f"{label} must be an exact absolute path")
    normalized = Path(os.path.abspath(path))
    if normalized != path:
        raise StorageRefusal(
            "plan_invalid", f"{label} must not contain dot path components"
        )
    return path


def _relative_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise StorageRefusal("plan_invalid", f"{label} must be a safe relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StorageRefusal("plan_invalid", f"{label} must be a safe relative path")
    return path


def _validate_plan(path: Path) -> dict[str, Any]:
    plan = _decode_json_object(path, label="pre-cutover plan")
    if set(plan) != {"schema", "legacy", "current_targets", "proposed"}:
        raise StorageRefusal("plan_invalid", "pre-cutover plan fields are unsupported")
    if plan.get("schema") != PLAN_SCHEMA:
        raise StorageRefusal("plan_invalid", "pre-cutover plan schema is unsupported")
    legacy = plan.get("legacy")
    if not isinstance(legacy, dict) or set(legacy) != {"manifest", "bindings"}:
        raise StorageRefusal("plan_invalid", "legacy shadow plan is incomplete")
    manifest = _absolute_path(legacy["manifest"], "legacy.manifest")
    if not manifest.is_file() or manifest.is_symlink():
        raise StorageRefusal("plan_invalid", "legacy manifest must be a regular file")
    bindings = legacy.get("bindings")
    if (
        not isinstance(bindings, list)
        or not bindings
        or len(bindings) > MAX_LEGACY_BINDINGS
    ):
        raise StorageRefusal("plan_invalid", "legacy bindings are missing or exceed the bound")
    normalized_bindings: list[dict[str, Any]] = []
    relative_paths: set[str] = set()
    source_paths: set[Path] = set()
    for item in bindings:
        if not isinstance(item, dict) or set(item) != {"relative_path", "source"}:
            raise StorageRefusal("plan_invalid", "legacy binding fields are unsupported")
        relative = _relative_path(item["relative_path"], "legacy binding path")
        source = _absolute_path(item["source"], "legacy binding source")
        if not source.is_file() or source.is_symlink():
            raise StorageRefusal("plan_invalid", "legacy binding source must be a regular file")
        relative_name = relative.as_posix()
        if relative_name in relative_paths or source in source_paths:
            raise StorageRefusal("plan_invalid", "legacy binding identity is duplicated")
        relative_paths.add(relative_name)
        source_paths.add(source)
        normalized_bindings.append(
            {"relative_path": relative_name, "source": str(source)}
        )

    targets = plan.get("current_targets")
    if not isinstance(targets, list) or not targets or len(targets) > MAX_LIVE_TARGETS:
        raise StorageRefusal("plan_invalid", "current targets are missing or exceed the bound")
    normalized_targets: list[dict[str, Any]] = []
    target_ids: set[str] = set()
    target_paths: set[Path] = set()
    target_by_path: dict[Path, dict[str, Any]] = {}
    for item in targets:
        if not isinstance(item, dict) or set(item) != {
            "target_id",
            "kind",
            "path",
            "required",
        }:
            raise StorageRefusal("plan_invalid", "current target fields are unsupported")
        target_id = item.get("target_id")
        kind = item.get("kind")
        required = item.get("required")
        if not isinstance(target_id, str) or not TARGET_ID.fullmatch(target_id):
            raise StorageRefusal("plan_invalid", "current target id is invalid")
        if kind not in TARGET_KINDS or not isinstance(required, bool):
            raise StorageRefusal("plan_invalid", "current target kind or requirement is invalid")
        target_path = _absolute_path(item["path"], "current target path")
        if target_id in target_ids or target_path in target_paths:
            raise StorageRefusal("plan_invalid", "current target identity is duplicated")
        if required and not os.path.lexists(target_path):
            raise StorageRefusal("target_missing", "a required current target is missing")
        normalized = {
            "target_id": target_id,
            "kind": kind,
            "path": str(target_path),
            "required": required,
        }
        target_ids.add(target_id)
        target_paths.add(target_path)
        target_by_path[target_path] = normalized
        normalized_targets.append(normalized)

    proposed = plan.get("proposed")
    if not isinstance(proposed, dict) or set(proposed) != {
        "backup_root",
        "release_prefix",
        "stable_launcher",
        "watcher_launcher",
        "state_root",
        "writer_pointer",
        "archive_root",
        "hooks",
    }:
        raise StorageRefusal("plan_invalid", "proposed cutover destinations are incomplete")
    normalized_proposed = {
        key: str(_absolute_path(proposed[key], f"proposed.{key}"))
        for key in (
            "backup_root",
            "release_prefix",
            "stable_launcher",
            "watcher_launcher",
            "state_root",
            "writer_pointer",
            "archive_root",
        )
    }
    hooks = proposed.get("hooks")
    if not isinstance(hooks, list) or not hooks or len(hooks) > len(HARNESS_KINDS):
        raise StorageRefusal("plan_invalid", "proposed hook targets are missing or invalid")
    normalized_hooks: list[dict[str, str]] = []
    hook_harnesses: set[str] = set()
    hook_targets: set[Path] = set()
    for item in hooks:
        if not isinstance(item, dict) or set(item) != {"harness", "target"}:
            raise StorageRefusal("plan_invalid", "proposed hook fields are unsupported")
        harness = item.get("harness")
        target = _absolute_path(item.get("target"), "proposed hook target")
        if (
            harness not in HARNESS_KINDS
            or harness in hook_harnesses
            or target in hook_targets
        ):
            raise StorageRefusal("plan_invalid", "proposed hook harness is invalid or duplicated")
        hook_harnesses.add(str(harness))
        hook_targets.add(target)
        normalized_hooks.append({"harness": str(harness), "target": str(target)})
    normalized_proposed["hooks"] = normalized_hooks

    required_target_kinds = {
        Path(normalized_proposed["backup_root"]): "backup_root",
        Path(normalized_proposed["release_prefix"]): "release_prefix",
        Path(normalized_proposed["stable_launcher"]): "stable_launcher",
        Path(normalized_proposed["watcher_launcher"]): "watcher_launcher",
        Path(normalized_proposed["state_root"]): "sqlite_state",
        Path(normalized_proposed["writer_pointer"]): "writer_pointer",
        Path(normalized_proposed["archive_root"]): "archive_root",
    }
    required_target_kinds.update(
        {Path(item["target"]): "hook_config" for item in normalized_hooks}
    )
    for target_path, expected_kind in required_target_kinds.items():
        target = target_by_path.get(target_path)
        if target is None or target["kind"] != expected_kind:
            raise StorageRefusal(
                "plan_invalid",
                "every proposed destination must have one exact current-target precondition",
            )
    if not any(item["kind"] == "legacy_state" for item in normalized_targets):
        raise StorageRefusal("plan_invalid", "one legacy-state target is required")
    if not any(item["kind"] == "installed_bundle" for item in normalized_targets):
        raise StorageRefusal("plan_invalid", "one installed-bundle target is required")
    return {
        "schema": PLAN_SCHEMA,
        "legacy": {
            "manifest": str(manifest),
            "bindings": sorted(normalized_bindings, key=lambda item: item["relative_path"]),
        },
        "current_targets": sorted(normalized_targets, key=lambda item: item["target_id"]),
        "proposed": normalized_proposed,
    }


def _node_records(path: Path) -> tuple[str, list[dict[str, Any]], int]:
    if not os.path.lexists(path):
        return "absent", [{"path": ".", "kind": "absent"}], 0
    records: list[dict[str, Any]] = []
    byte_count = 0

    def visit(candidate: Path, relative: str) -> None:
        nonlocal byte_count
        if len(records) >= MAX_SNAPSHOT_ENTRIES:
            raise StorageRefusal("snapshot_too_large", "current target exceeds the entry bound")
        try:
            details = candidate.lstat()
        except OSError as exc:
            raise StorageRefusal("target_unreadable", "current target could not be inspected") from exc
        mode = stat.S_IMODE(details.st_mode)
        if stat.S_ISLNK(details.st_mode):
            target = os.readlink(candidate)
            records.append(
                {"path": relative, "kind": "symlink", "mode": mode, "target": target}
            )
            return
        if stat.S_ISREG(details.st_mode):
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(candidate, os.O_RDONLY | no_follow)
            try:
                before = os.fstat(descriptor)
                if byte_count + before.st_size > MAX_SNAPSHOT_BYTES:
                    raise StorageRefusal(
                        "snapshot_too_large", "current target exceeds the byte bound"
                    )
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            payload = b"".join(chunks)
            if (
                (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or len(payload) != after.st_size
            ):
                raise StorageRefusal("source_changed", "current target changed while read")
            byte_count += len(payload)
            if byte_count > MAX_SNAPSHOT_BYTES:
                raise StorageRefusal("snapshot_too_large", "current target exceeds the byte bound")
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": mode,
                    "bytes": len(payload),
                    "sha256": _sha256(payload),
                }
            )
            return
        if stat.S_ISDIR(details.st_mode):
            records.append({"path": relative, "kind": "directory", "mode": mode})
            for child in sorted(candidate.iterdir(), key=lambda item: item.name):
                child_relative = child.name if relative == "." else f"{relative}/{child.name}"
                visit(child, child_relative)
            return
        raise StorageRefusal("target_unsupported", "current targets require files, directories, or symlinks")

    visit(path, ".")
    root_kind = records[0]["kind"]
    return root_kind, records, byte_count


def _snapshot(path: Path) -> dict[str, Any]:
    kind, records, byte_count = _node_records(path)
    return {
        "exists": kind != "absent",
        "node_kind": kind,
        "entries": len(records),
        "bytes": byte_count,
        "sha256": _sha256(_stable_bytes(records)),
    }


def _regular_content_sha256(path: Path) -> str:
    kind, records, _ = _node_records(path)
    if kind != "file" or len(records) != 1:
        raise StorageRefusal("target_unsupported", "legacy binding must be a regular file")
    return str(records[0]["sha256"])


def _copy_regular_stable(source: Path, destination: Path, *, mode: Optional[int] = None) -> None:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, os.O_RDONLY | no_follow)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SNAPSHOT_BYTES:
            raise StorageRefusal("target_unsupported", "backup source must be a bounded regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if (
        (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or len(payload) != after.st_size
    ):
        raise StorageRefusal("source_changed", "backup source changed while read")
    from .acceptance import _atomic_write

    _atomic_write(destination, payload, mode=mode if mode is not None else stat.S_IMODE(after.st_mode))


def _copy_node(source: Path, destination: Path) -> None:
    details = source.lstat()
    mode = stat.S_IMODE(details.st_mode)
    if stat.S_ISLNK(details.st_mode):
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.symlink_to(os.readlink(source))
    elif stat.S_ISREG(details.st_mode):
        _copy_regular_stable(source, destination, mode=mode)
    elif stat.S_ISDIR(details.st_mode):
        destination.mkdir(parents=True, exist_ok=True, mode=mode)
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            _copy_node(child, destination / child.name)
        os.chmod(destination, mode)
    else:
        raise StorageRefusal("target_unsupported", "backup source type is unsupported")


def _backup_rehearsal(
    home: Path,
    targets: Sequence[dict[str, Any]],
    *,
    fault: Optional[Any] = None,
) -> dict[str, Any]:
    backup_root = home / "backup-rehearsal" / "backup"
    restore_root = home / "backup-rehearsal" / "restore"
    backup_root.mkdir(parents=True, mode=0o700)
    restore_root.mkdir(parents=True, mode=0o700)
    receipts: list[dict[str, Any]] = []
    for target in targets:
        path = Path(target["path"])
        before = _snapshot(path)
        backup = backup_root / target["target_id"]
        restored = restore_root / target["target_id"]
        if before["exists"]:
            _copy_node(path, backup)
            if fault is not None:
                fault(f"after_backup:{target['target_id']}")
            if _snapshot(path) != before:
                raise StorageRefusal("source_changed", "current target changed during backup rehearsal")
            _copy_node(backup, restored)
            if _snapshot(restored)["sha256"] != before["sha256"]:
                raise StorageRefusal("rollback_parity_failed", "restored rehearsal target differs")
            rollback = "restore_exact_backup"
        else:
            rollback = "remove_created_target"
        receipts.append(
            {
                "target_id": target["target_id"],
                "kind": target["kind"],
                "exists": before["exists"],
                "node_kind": before["node_kind"],
                "bytes": before["bytes"],
                "sha256": before["sha256"],
                "rollback": rollback,
            }
        )
    return {
        "targets": receipts,
        "target_count": len(receipts),
        "restore_parity": True,
        "sandbox_only": True,
        "aggregate_sha256": _sha256(_stable_bytes(receipts)),
    }


def _copy_legacy_snapshot(plan: Mapping[str, Any], destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, mode=0o700)
    manifest_source = Path(plan["legacy"]["manifest"])
    manifest_target = destination / "import-manifest.json"
    _copy_regular_stable(manifest_source, manifest_target, mode=0o600)
    bindings: list[dict[str, Any]] = []
    for item in plan["legacy"]["bindings"]:
        source = Path(item["source"])
        target = destination / item["relative_path"]
        before = _snapshot(source)
        content_sha256 = _regular_content_sha256(source)
        _copy_regular_stable(source, target, mode=0o600)
        after = _snapshot(source)
        if before != after or _regular_content_sha256(target) != content_sha256:
            raise StorageRefusal("source_changed", "legacy source changed during read-only snapshot")
        bindings.append(
            {
                "relative_path": item["relative_path"],
                "bytes": before["bytes"],
                "sha256": content_sha256,
            }
        )
    return {
        "root": destination,
        "manifest": manifest_target,
        "manifest_sha256": _regular_content_sha256(manifest_source),
        "bindings": bindings,
    }


def _exact_rows(plan: Mapping[str, Any], exported: Mapping[str, Any]) -> str:
    parity: dict[str, list[dict[str, Any]]] = {}
    for table, rows in plan["rows"].items():
        expected = sorted(rows, key=_stable_bytes)
        expected_columns = {column for row in rows for column in row}
        exported_rows = exported["tables"][table]
        if len(exported_rows) != len(rows):
            raise StorageRefusal(
                "shadow_parity_failed", f"read-only shadow row count failed for {table}"
            )
        observed = sorted(
            (
                {column: row[column] for column in expected_columns}
                for row in exported_rows
            ),
            key=_stable_bytes,
        )
        if expected != observed:
            raise StorageRefusal("shadow_parity_failed", f"read-only shadow parity failed for {table}")
        parity[table] = expected
    return _sha256(_stable_bytes(parity))


def _read_only_shadow(home: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _copy_legacy_snapshot(plan, home / "live-shadow" / "source")
    state = home / "live-shadow" / "state"
    state.mkdir(mode=0o700)
    with SQLiteStorage.for_migration(state, request_wal=False) as store:
        migration = store.migrate()
    with SQLiteStorage(state, request_wal=False) as store:
        import_plan = build_import_plan(
            snapshot["root"],
            snapshot["manifest"],
            target_counts=store.import_target_counts(),
        )
        report = import_plan["report"]
        if not report["dry_run"] or not report["eligible"]:
            raise StorageRefusal("shadow_parity_failed", "read-only live shadow is not eligible")
        applied = store.apply_import(import_plan, report["report_digest"])
        exported = json.loads(
            store.export_bytes(format_name="json", purpose="rollback", max_records=10_000)
        )
        parity_sha256 = _exact_rows(import_plan, exported)
        integrity = store.integrity()
        if not integrity["ok"]:
            raise StorageRefusal("shadow_parity_failed", "read-only live shadow integrity failed")
        backup = store.backup("precutover-shadow.sqlite3")
        rollback_bytes = store.export_bytes(
            format_name="json", purpose="rollback", max_records=10_000
        )
        store.write_restricted("precutover-shadow-rollback.json", rollback_bytes)
    for item in plan["legacy"]["bindings"]:
        expected = next(
            value for value in snapshot["bindings"] if value["relative_path"] == item["relative_path"]
        )
        if _regular_content_sha256(Path(item["source"])) != expected["sha256"]:
            raise StorageRefusal("source_changed", "legacy source changed after shadow import")
    return {
        "migration": migration,
        "dry_run": {
            "eligible": True,
            "report_digest": report["report_digest"],
            "source_digest": report["source_digest"],
            "artifact_counts": report["artifact_counts"],
            "row_counts": report["row_counts"],
        },
        "apply": {"applied": applied["applied"]},
        "exact_parity": True,
        "parity_sha256": parity_sha256,
        "source_unchanged": True,
        "binding_count": len(snapshot["bindings"]),
        "manifest_sha256": snapshot["manifest_sha256"],
        "database_backup": backup,
        "rollback_export": {
            "sha256": _sha256(rollback_bytes),
            "bytes": len(rollback_bytes),
            "restricted": True,
        },
    }


@dataclass
class _Clock:
    value: str = "2026-01-01T01:00:00Z"

    def now(self) -> str:
        return self.value

    def after(self, seconds: int) -> str:
        parsed = datetime.fromisoformat(self.value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
        return (parsed + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )


class _Ids:
    def __init__(self) -> None:
        self.sequence = 0

    def new(self, kind: str) -> str:
        self.sequence += 1
        return f"synthetic-{kind}-{self.sequence:04d}"


class _LaunchDouble:
    def launch(self, specification: AssignmentSpec) -> dict[str, Any]:
        return {
            "verified": True,
            "assignment_id": specification.assignment_id,
            "task_id": specification.task_id,
            "champion_agent_id": specification.champion_agent_id,
            "callsign": specification.callsign,
            "runtime_instance_id": "runtime:synthetic-champion",
            "thread_id": "thread:synthetic-champion",
            "endpoint": "synthetic:champion-endpoint",
            "runtime_generation": "generation:synthetic-champion",
            "harness_kind": "codex-thread",
            "backend_kind": "herdr",
            "routing_name": str(specification.callsign).lower(),
            "display_agent": "codex",
            "repository": specification.repository,
            "issue": specification.issue,
            "branch": specification.branch,
            "worktree": specification.worktree,
            "capabilities": list(specification.required_capabilities),
        }


class _DeliveryDouble:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(
        self,
        channel: str,
        target: dict[str, Any],
        envelope: dict[str, Any],
    ) -> DeliveryReceipt:
        self.sent.append({"channel": channel, "target": dict(target), "event": envelope["event_id"]})
        return DeliveryReceipt(
            outbox_id=envelope["outbox_id"],
            event_id=envelope["event_id"],
            recipient_agent_id=envelope["recipient_agent_id"],
            effect_kind="inbox_event",
            effect_id=f"effect:{envelope['event_id']}",
        )


class _CleanupDouble:
    def __init__(self, kind: str, states: dict[str, dict[str, Any]], effects: list[str]) -> None:
        self.kind = kind
        self.states = states
        self.effects = effects

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(self.states[str(action["action_id"])])

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        action_id = str(action["action_id"])
        self.effects.append(action_id)
        self.states[action_id] = dict(action["intended_state"])
        return {"isolated_double": True, "action_id": action_id}

    def intended(self, action: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
        return dict(observation) == dict(action["intended_state"])


def _seed_synthetic_store(root: Path, source_root: Path) -> SQLiteStorage:
    fixture_module = _load_fixture_module(source_root)
    legacy = root / "legacy"
    state = root / "state"
    legacy.mkdir(parents=True, mode=0o700)
    state.mkdir(mode=0o700)
    fixture = fixture_module.write_complete_fixture(
        legacy, runtime_root=Path("/synthetic/precutover-runtime")
    )
    with SQLiteStorage.for_migration(state, request_wal=False) as store:
        store.migrate()
    store = SQLiteStorage(state, request_wal=False)
    plan = build_import_plan(
        legacy, fixture["manifest"], target_counts=store.import_target_counts()
    )
    store.apply_import(plan, plan["report_digest"])
    return store


def _cleanup_manifest() -> dict[str, Any]:
    identity = {
        "task_id": LIFECYCLE_TASK_ID,
        "owner_id": CHAMPION_ID,
        "generation": "generation:synthetic-champion",
    }
    resource = {
        "resource_id": "synthetic-precutover-process",
        "task_id": LIFECYCLE_TASK_ID,
        "owner_id": CHAMPION_ID,
        "owner_role": "champion",
        "resource_type": "synthetic-process",
        "lifetime": "task_owned",
        "expected_identity": {"resource_id": "synthetic-precutover-process", "generation": "exact"},
        "cleanup_action": "terminate",
        "adapter_kind": "process",
        "applicable": True,
        "applicability_reason": "Exact disposable acceptance resource.",
    }
    proof = {
        "identity": {"exact": True},
        "endpoint": {"terminal_or_idle": True},
        "git": {"exact_registration": True, "clean": True, "no_unpublished": True},
        "publication": {"exact_head": True, "ci_green": True, "integrated": True},
        "deployment": {"exact_revision": True, "smoke_passed": True},
        "decision": {"explicit": True},
        "failure": {"preserved": True},
    }
    return {
        "task_id": LIFECYCLE_TASK_ID,
        "owner": {"id": CHAMPION_ID, "role": "champion", "persistent": False},
        "task_class": "local_git",
        "disposition": "completed",
        "pending_decisions_clear": True,
        "expected_cleanup_version": 1,
        "identity": identity,
        "legacy_identity": dict(identity),
        "proof": proof,
        "resources": [resource],
        "final_actions": [
            {
                "action_kind": action,
                "adapter_kind": {
                    "session_exit": "harness",
                    "endpoint_close": "backend",
                    "worktree_remove": "git",
                    "branch_delete": "git",
                    "callsign_release": "callsign",
                }[action],
                "expected_identity": {"action": action, "generation": "exact"},
                "intended_state": {"completed": True, "action": action},
            }
            for action in (
                "session_exit",
                "endpoint_close",
                "worktree_remove",
                "branch_delete",
                "callsign_release",
            )
        ],
    }


def _integrated_lifecycle(home: Path, source_root: Path) -> dict[str, Any]:
    clock = _Clock()
    ids = _Ids()
    delivery = _DeliveryDouble()
    with _seed_synthetic_store(home / "lifecycle", source_root) as store:
        store.register_runtime(
            RuntimeRegistrationCommand(
                runtime_instance_id="runtime:precutover-shotcaller",
                actor_agent_id=SHOTCALLER_ID,
                harness_kind="codex-thread",
                backend_kind="herdr",
                session_ref="session:precutover-shotcaller",
                endpoint="synthetic:precutover-shotcaller",
                runtime_generation="generation:precutover-shotcaller",
                status="active",
                verified=True,
                at=clock.now(),
            )
        )
        store.register_watcher(
            "synthetic-precutover-scope",
            "watcher:synthetic-precutover",
            SHOTCALLER_ID,
            "runtime:precutover-shotcaller",
            "wake:synthetic-precutover",
            clock.after(600),
            1,
            clock.now(),
        )
        store.intake_prompt(
            "synthetic-precutover-prompt",
            SHOTCALLER_ID,
            "runtime:precutover-shotcaller",
            "codex",
            "session:precutover-shotcaller",
            "source:synthetic-precutover",
            "Run the isolated pre-cutover lifecycle.",
            clock.now(),
        )
        store.triage_prompt(
            "synthetic-precutover-prompt",
            [
                {
                    "prompt_item_id": "synthetic-precutover-item",
                    "ordinal": 1,
                    "summary": "Run the isolated pre-cutover lifecycle",
                    "disposition": "new_request",
                    "request_id": "synthetic-precutover-request",
                }
            ],
            clock.now(),
        )
        store.claim_request(
            "synthetic-precutover-request",
            "runtime:precutover-shotcaller",
            "claim:synthetic-precutover",
            clock.after(600),
            clock.now(),
        )
        dispatch = store.dispatch_request(
            DispatchRequestCommand(
                request_id="synthetic-precutover-request",
                claim_token="claim:synthetic-precutover",
                dispatch_id="dispatch:synthetic-precutover",
                work_kind="repository-write",
                requested_mode="champion",
                hidden_supported=False,
                requested_model="synthetic-model",
                requested_effort="high",
                explicit_route="SyntheticChampion",
                at=clock.now(),
                orchestration=OrchestrationSignals(False, False, False, 0, 0),
            )
        )
        assignment = AssignmentService(store, _LaunchDouble(), clock, ids).assign(
            AssignmentSpec(
                assignment_id="assignment:synthetic-precutover",
                request_id="synthetic-precutover-request",
                claim_token="claim:synthetic-precutover",
                task_id=LIFECYCLE_TASK_ID,
                task_summary="Synthetic pre-cutover task",
                coordinator_agent_id=SHOTCALLER_ID,
                champion_agent_id=CHAMPION_ID,
                callsign="Lux",
                repository=SYNTHETIC_REPOSITORY,
                issue=23,
                branch="agent/synthetic/23-precutover",
                worktree="/synthetic/worktrees/23-precutover",
            )
        )
        DeliveryService(
            store,
            delivery,
            clock,
            ids,
            dispatcher_id="dispatcher:synthetic-precutover",
        ).dispatch_source(assignment["outbox_id"], assignment["event_id"], CHAMPION_ID)
        stop_before = store.stop_decision(
            "synthetic-precutover-scope",
            SHOTCALLER_ID,
            "terminal:synthetic-precutover:before",
            clock.now(),
        )
        if stop_before["decision"] != "block":
            raise StorageRefusal("lifecycle_acceptance_failed", "Stop did not preserve active work")
        transition = store.transition_task(
            LIFECYCLE_TASK_ID,
            assignment["runtime_instance_id"],
            3,
            "completed",
            "Synthetic pre-cutover task completed",
            "Run exact fake cleanup",
            None,
            "transition:synthetic-precutover",
            "transition-key:synthetic-precutover",
            "event:synthetic-precutover-completed",
            "outbox:synthetic-precutover-completed",
            SHOTCALLER_ID,
            clock.now(),
        )
        delivery_result = DeliveryService(
            store,
            delivery,
            clock,
            ids,
            dispatcher_id="dispatcher:synthetic-precutover",
        ).dispatch_source(transition["outbox_id"], transition["event_id"], SHOTCALLER_ID)
        cleanup_plan = CleanupPlanner(store).plan(
            _cleanup_manifest(),
            operation_id="cleanup:synthetic-precutover",
            at=clock.now(),
        )
        operation = store.cleanup_operation(cleanup_plan["operation_id"])
        if operation is None:
            raise StorageRefusal("lifecycle_acceptance_failed", "cleanup plan disappeared")
        states = {
            action["action_id"]: dict(action["expected_identity"])
            for action in operation["actions"]
        }
        effects: list[str] = []
        registry = CleanupAdapterRegistry()
        for kind in CLEANUP_ADAPTER_KINDS:
            registry.register(_CleanupDouble(kind, states, effects))
        cleanup = CleanupExecutor(store, registry).execute(
            cleanup_plan["operation_id"],
            expected_fence=0,
            executor_id="executor:synthetic-precutover",
            leased_until=clock.after(600),
            at=clock.now(),
        )
        request_result = store.record_request_result(
            RequestResultCommand(
                request_id="synthetic-precutover-request",
                claim_token="claim:synthetic-precutover",
                expected_version=dispatch["request_version"],
                result_id="result:synthetic-precutover",
                idempotency_key="result-key:synthetic-precutover",
                outcome="success",
                summary="Synthetic lifecycle result synthesized",
                task_ids=(LIFECYCLE_TASK_ID,),
                at=clock.now(),
                return_to_requester=False,
                event_id=None,
                outbox_id=None,
            )
        )
        answer = store.answer_request(
            AnswerRequestCommand(
                request_id="synthetic-precutover-request",
                claim_token="claim:synthetic-precutover",
                expected_version=request_result["version"],
                response_ref_id="response:synthetic-precutover",
                adapter_kind="codex",
                session_locator="session:synthetic-precutover",
                response_locator="response:synthetic-precutover",
                durability="durable",
                content_hash=_sha256(b"synthetic-precutover-response"),
                resolution_summary="Synthetic lifecycle response delivered",
                event_id="event:synthetic-precutover-answered",
                at=clock.now(),
            )
        )
        stop_after = store.stop_decision(
            "synthetic-precutover-scope",
            SHOTCALLER_ID,
            "terminal:synthetic-precutover:after",
            clock.now(),
        )
        unresolved = store.unresolved_requests(SHOTCALLER_ID, before_action="end")
        if (
            assignment["state"] != "active"
            or delivery_result["state"] != "delivered"
            or cleanup["state"] != "cleanup_completed"
            or answer["state"] != "answered"
            or stop_after["decision"] != "allow"
            or not unresolved["safe_to_finish"]
        ):
            raise StorageRefusal("lifecycle_acceptance_failed", "integrated lifecycle did not settle")
        receipt_count = store.connection.execute(
            "SELECT COUNT(*) FROM recipient_receipts WHERE event_id=?",
            (transition["event_id"],),
        ).fetchone()[0]
        return {
            "request": {"status": "passed", "state": answer["state"], "safe_to_finish": True},
            "assignment": {
                "status": "passed",
                "state": assignment["state"],
                "exact_receipt": True,
            },
            "watcher": {
                "status": "passed",
                "delivery_state": delivery_result["state"],
                "recipient_effect_count": receipt_count,
            },
            "stop": {
                "status": "passed",
                "before": stop_before["decision"],
                "after": stop_after["decision"],
            },
            "teardown": {
                "status": "passed",
                "state": cleanup["state"],
                "action_count": len(effects),
                "fake_effects_only": True,
            },
            "schema_version": CURRENT_SCHEMA_VERSION,
            "delivery_adapter_calls": len(delivery.sent),
        }


class _BackendDouble:
    def __init__(self, kind: str, capabilities: frozenset[str] = BACKEND_CAPABILITIES) -> None:
        self.contract = AdapterContract(
            kind,
            "backend",
            frozenset(capabilities),
            "isolated-double",
            "available",
            "Deterministic pre-cutover backend double.",
        )
        self.endpoints: dict[str, dict[str, Any]] = {}
        self.operations: list[str] = []
        self.sequence = 0

    def allocate(self, specification: Mapping[str, Any]) -> AdapterReceipt:
        self.contract.require("allocate")
        self.sequence += 1
        identity = OpaqueIdentity(self.contract.kind, f"endpoint-{self.sequence}")
        self.endpoints[identity.encoded] = {
            "state": "idle",
            "generation": f"generation-{self.sequence}",
            "session_identity": None,
        }
        self.operations.append("allocate")
        return AdapterReceipt("allocate", identity, "idle", {"isolated_double": True})

    def input(self, endpoint: OpaqueIdentity, instruction: AdapterInstruction) -> AdapterReceipt:
        self.contract.require("input")
        state = self.endpoints[endpoint.encoded]
        operation = instruction.operation
        self.operations.append(operation)
        if operation == "create":
            harness = str(instruction.payload["harness"])
            state["session_identity"] = f"{harness}:session-{self.sequence}"
            state["state"] = "active"
        elif operation in {"prompt", "hook", "title"}:
            pass
        elif operation == "interrupt":
            state["state"] = "idle"
        elif operation == "resume":
            state["state"] = "active"
        elif operation == "exit":
            state["state"] = "closed"
        return AdapterReceipt(operation, endpoint, state["state"], {"isolated_double": True})

    def inspect(self, endpoint: OpaqueIdentity) -> RuntimeObservation:
        self.contract.require("inspect")
        self.operations.append("inspect")
        state = self.endpoints[endpoint.encoded]
        details = {}
        if state["session_identity"] is not None:
            details["session_identity"] = state["session_identity"]
        return RuntimeObservation(endpoint, state["state"], state["generation"], details)

    def close(self, endpoint: OpaqueIdentity) -> AdapterReceipt:
        self.contract.require("close")
        self.operations.append("close")
        state = self.endpoints[endpoint.encoded]
        state["state"] = "missing"
        state["session_identity"] = None
        return AdapterReceipt("close", endpoint, "missing", {"isolated_double": True})


def _registry_for(harness_kind: str, backend: _BackendDouble) -> AdapterRegistry:
    registry = AdapterRegistry()
    harness = next(
        item for item in builtin_harness_contracts() if item.contract.kind == harness_kind
    )
    registry.register_harness(harness)
    registry.register_backend(backend)
    return registry


def _runtime_canaries(home: Path, source_root: Path) -> dict[str, Any]:
    with _seed_synthetic_store(home / "runtime-canaries", source_root) as store:
        receipts: list[dict[str, Any]] = []
        for index, (harness_kind, backend_kind) in enumerate(
            (("codex", "herdr"), ("pi", "herdr"))
        ):
            backend = _BackendDouble(backend_kind)
            backend.sequence = index * 100
            lifecycle = RuntimeLifecycle(store, _registry_for(harness_kind, backend))
            binding_id = f"binding:{harness_kind}:{backend_kind}:{index}"
            created = lifecycle.create(
                RuntimeCreateSpec(
                    binding_id=binding_id,
                    task_id=BASE_TASK_ID,
                    harness_kind=harness_kind,
                    backend_kind=backend_kind,
                    title=f"Synthetic {harness_kind} {backend_kind}",
                    at="2026-01-01T02:00:00Z",
                    harness={},
                    backend={},
                )
            )
            lifecycle.prompt(binding_id, "Synthetic pre-cutover prompt")
            lifecycle.wake(binding_id, "synthetic-precutover-event")
            lifecycle.interrupt(binding_id)
            if harness_kind == "pi":
                lifecycle.resume(binding_id)
            lifecycle.guarded_exit(
                binding_id,
                expected_version=1,
                expected_fence=0,
                executor_id=f"executor:{binding_id}",
                leased_until="2026-01-01T02:10:00Z",
                at="2026-01-01T02:01:00Z",
            )
            receipts.append(
                {
                    "harness": harness_kind,
                    "backend": backend_kind,
                    "mode": "created",
                    "status": "passed",
                    "evidence": "isolated-double",
                    "operations": sorted(set(backend.operations)),
                    "real_runtime_proven": False,
                    "binding_id": created["binding_id"],
                }
            )

        tmux = _BackendDouble("tmux", BACKEND_CAPABILITIES - {"allocate"})
        endpoint = OpaqueIdentity("tmux", "attached-precutover")
        tmux.endpoints[endpoint.encoded] = {
            "state": "active",
            "generation": "attached-precutover-generation",
            "session_identity": "codex:attached-precutover-session",
        }
        store.register_runtime_binding(
            "binding:codex:tmux:attached",
            BASE_TASK_ID,
            "codex",
            "tmux",
            "codex:attached-precutover-session",
            endpoint.encoded,
            "attached-precutover-generation",
            {
                "harness": ["create", "exit", "hook", "identify", "interrupt", "prompt", "status", "title"],
                "backend": ["close", "input", "inspect"],
                "evidence": {"harness": "inherited-contract", "backend": "isolated-double"},
            },
            "2026-01-01T02:00:00Z",
        )
        tmux_lifecycle = RuntimeLifecycle(store, builtin_registry((tmux,)))
        tmux_lifecycle.prompt("binding:codex:tmux:attached", "Synthetic attached prompt")
        tmux_lifecycle.wake("binding:codex:tmux:attached", "synthetic-attached-event")
        tmux_lifecycle.interrupt("binding:codex:tmux:attached")
        tmux_lifecycle.guarded_exit(
            "binding:codex:tmux:attached",
            expected_version=1,
            expected_fence=0,
            executor_id="executor:codex:tmux",
            leased_until="2026-01-01T02:10:00Z",
            at="2026-01-01T02:01:00Z",
        )
        receipts.append(
            {
                "harness": "codex",
                "backend": "tmux",
                "mode": "attached",
                "status": "passed",
                "evidence": "isolated-double",
                "operations": sorted(set(tmux.operations)),
                "real_runtime_proven": False,
                "binding_id": "binding:codex:tmux:attached",
            }
        )
    return {
        "canaries": receipts,
        "all_supported_contract_canaries_passed": True,
        "real_runtime_proven": False,
        "unverified": [
            {
                "runtime": "cursor",
                "reason": "no built-in Cursor runtime adapter is registered",
            },
            {
                "runtime": "real-herdr-tmux",
                "reason": "test doubles are not real multiplexer proof",
            },
        ],
    }


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return round(ordered[index], 3)


def _supervision_benchmark(home: Path) -> dict[str, Any]:
    """Measure the isolated event path without a daemon or transcript polling."""

    messages: queue.Queue[Optional[tuple[int, str]]] = queue.Queue()
    acknowledged = threading.Event()
    event_latencies_ms: list[float] = []
    visible_output = ["supervision_started"]

    def listen() -> None:
        while True:
            message = messages.get()
            if message is None:
                return
            started_ns, event_kind = message
            event_latencies_ms.append((time.perf_counter_ns() - started_ns) / 1_000_000)
            visible_output.append(event_kind)
            acknowledged.set()

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    cpu_before = time.process_time_ns()
    listener = threading.Thread(target=listen, name="league-isolated-event-listener")
    listener.start()
    for index in range(32):
        acknowledged.clear()
        messages.put((time.perf_counter_ns(), f"material_event_{index + 1}"))
        if not acknowledged.wait(timeout=1.0):
            raise StorageRefusal("supervision_benchmark_failed", "event wake did not arrive")

    idle_cpu_before = time.process_time_ns()
    time.sleep(0.025)
    idle_cpu_ms = (time.process_time_ns() - idle_cpu_before) / 1_000_000
    messages.put(None)
    listener.join(timeout=1.0)
    if listener.is_alive():
        raise StorageRefusal("supervision_benchmark_failed", "event listener did not terminate")

    snapshot_results: list[dict[str, Any]] = []
    for champion_count in (1, 8, 32):
        backend = _BackendDouble("herdr")
        for index in range(champion_count):
            identity = OpaqueIdentity("herdr", f"benchmark-{index + 1}")
            backend.endpoints[identity.encoded] = {
                "state": "idle",
                "generation": f"benchmark-generation-{index + 1}",
                "session_identity": f"codex:benchmark-{index + 1}",
            }
        started = time.perf_counter_ns()
        for _ in range(100):
            for endpoint in tuple(backend.endpoints):
                backend.inspect(OpaqueIdentity.decode(endpoint))
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        snapshot_results.append(
            {
                "champions": champion_count,
                "iterations": 100,
                "total_ms": round(elapsed_ms, 3),
                "per_snapshot_ms": round(elapsed_ms / 100, 3),
            }
        )

    cpu_ms = (time.process_time_ns() - cpu_before) / 1_000_000
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {
        "status": "passed",
        "environment": "isolated-python-test-double",
        "event_wake": {
            "samples": len(event_latencies_ms),
            "p50_ms": _percentile(event_latencies_ms, 0.50),
            "p95_ms": _percentile(event_latencies_ms, 0.95),
            "max_ms": round(max(event_latencies_ms), 3),
            "prompt_and_transition_bypass_reconciliation": True,
        },
        "missed_wake_reconciliation": {
            "simulation": True,
            "interval_seconds": 30,
            "consecutive_observations": 2,
            "earliest_fallback_seconds": 60,
            "separate_15_second_policy": False,
        },
        "resources": {
            "process_cpu_ms": round(cpu_ms, 3),
            "idle_observation_ms": 25,
            "idle_cpu_ms": round(idle_cpu_ms, 3),
            "peak_rss_before": rss_before,
            "peak_rss_after": rss_after,
            "snapshot_scaling": snapshot_results,
        },
        "presentation": {
            "initial_messages": 1,
            "material_messages": len(visible_output) - 1,
            "unchanged_messages": 0,
            "silent_lease_maintenance": True,
        },
        "permanent_daemon_created": False,
        "transcript_polling_used": False,
        "listener_terminated": True,
        "proposal": {
            "decision": "retain_compatibility_default",
            "reconciliation_interval_seconds": 30,
            "consecutive_observations": 2,
            "requires_cutover_authority": True,
        },
    }
    _write_json(home / "supervision-benchmark.json", result)
    return result


def _staged_supervision_check(staged: Mapping[str, Any], home: Path) -> dict[str, Any]:
    release = Path(staged["prefix"]) / "releases" / __version__
    launcher = release / "bin/agent-watcher"
    source = release / "src/agent_watcher.py"
    help_output = _run_checked(
        [str(launcher), "supervise", "--help"],
        cwd=home,
        env=_staged_environment(),
    )
    source_text = source.read_text(encoding="utf-8")
    required_source = (
        'default=30.0, help="Runtime snapshot interval; zero disables reconciliation."',
        'default=2, help="Identical mismatch observations required before champion_stalled."',
        "Liveness is deliberately silent.",
    )
    if (
        "--reconcile-seconds" not in help_output
        or "--reconcile-consecutive" not in help_output
        or any(fragment not in source_text for fragment in required_source)
    ):
        raise StorageRefusal(
            "staged_supervision_failed",
            "staged supervision defaults or silent-output contract changed",
        )
    return {
        "launcher_help_checked": True,
        "source_contract_checked": True,
        "reconciliation_interval_seconds": 30,
        "consecutive_observations": 2,
        "unchanged_output": "silent",
        "separate_15_second_policy": False,
    }


def _manifest_checks(
    plan: Mapping[str, Any],
    backups: Mapping[str, Any],
    staged: Mapping[str, Any],
) -> dict[str, Any]:
    installed_targets = sorted(
        (
            item
            for item in backups["targets"]
            if item["kind"] in {"installed_bundle", "stable_launcher", "watcher_launcher"}
        ),
        key=lambda item: item["target_id"],
    )
    if not installed_targets:
        raise StorageRefusal("manifest_check_failed", "installed targets are missing")
    return {
        "source_version": __version__,
        "staged_installed_version": staged["version"],
        "version_parity": staged["version"] == __version__,
        "source_manifest_sha256": staged["source_manifest_sha256"],
        "release_manifest_sha256": staged["release_manifest_sha256"],
        "staged_installed_manifest_sha256": staged["staged_manifest_sha256"],
        "source_release_staged_parity": staged["source_release_staged_parity"],
        "current_installed_target_count": len(installed_targets),
        "current_installed_aggregate_sha256": _sha256(_stable_bytes(installed_targets)),
        "current_installed_role": "legacy_rollback_source",
        "current_installed_unchanged": True,
    }


def _target_for_path(plan: Mapping[str, Any], path: str) -> dict[str, Any]:
    return next(item for item in plan["current_targets"] if item["path"] == path)


def _mutation_manifest(
    plan: Mapping[str, Any],
    backups: Mapping[str, Any],
    shadow: Mapping[str, Any],
    staged: Mapping[str, Any],
    supervision_benchmark: Mapping[str, Any],
) -> dict[str, Any]:
    proposed = plan["proposed"]
    backup_by_id = {item["target_id"]: item for item in backups["targets"]}
    backup_root = Path(proposed["backup_root"])
    generation_input = {
        "version": __version__,
        "source_manifest": staged["source_manifest_sha256"],
        "shadow_report": shadow["dry_run"]["report_digest"],
        "shadow_parity": shadow["parity_sha256"],
    }
    generation = f"sqlite-{_sha256(_stable_bytes(generation_input))[:20]}"
    pointer = {
        "schema": "league.writer-pointer.v1",
        "generation": generation,
        "writer": "sqlite",
        "version": __version__,
    }
    operations: list[dict[str, Any]] = []
    sequence = 0

    def add(kind: str, target: str, *, precondition: Any, after: Any, rollback: Any) -> None:
        nonlocal sequence
        sequence += 1
        operations.append(
            {
                "ordinal": sequence,
                "operation": kind,
                "target": target,
                "precondition": precondition,
                "after": after,
                "rollback": rollback,
                "applied": False,
            }
        )

    guarded = [
        item["target_id"]
        for item in plan["current_targets"]
        if item["kind"] in {"legacy_state", "watcher_state"}
    ]
    add(
        "enter_maintenance_and_quiesce",
        ",".join(sorted(guarded)),
        precondition={"exact_targets": sorted(guarded), "active_writer_count": 1},
        after={"intake": "blocked", "active_writer_count": 0},
        rollback={"intake": "blocked_until_old_writer_verified"},
    )
    for target in plan["current_targets"]:
        observed = backup_by_id[target["target_id"]]
        rollback_target = str(backup_root / "targets" / target["target_id"])
        add(
            "backup_current_target",
            target["path"],
            precondition={"sha256": observed["sha256"], "exists": observed["exists"]},
            after={"backup_target": rollback_target, "verified": True},
            rollback={"action": observed["rollback"], "source": rollback_target},
        )
    release_target = str(Path(proposed["release_prefix"]) / "releases" / __version__)
    add(
        "install_release_inactive",
        release_target,
        precondition={"target_absent_or_manifest_match": True},
        after={
            "source_manifest_sha256": staged["source_manifest_sha256"],
            "version": __version__,
            "active": False,
        },
        rollback={"action": "remove_inactive_release_if_exact_manifest"},
    )
    database_target = str(Path(proposed["state_root"]) / "league.sqlite3")
    add(
        "import_final_snapshot",
        database_target,
        precondition={
            "source_unchanged": True,
            "report_digest": shadow["dry_run"]["report_digest"],
            "parity_sha256": shadow["parity_sha256"],
        },
        after={"schema_version": CURRENT_SCHEMA_VERSION, "writer_active": False},
        rollback={"action": "remove_inactive_database_if_never_canonical"},
    )
    launcher_target = _target_for_path(plan, proposed["stable_launcher"])
    add(
        "switch_stable_launcher",
        proposed["stable_launcher"],
        precondition={"sha256": backup_by_id[launcher_target["target_id"]]["sha256"]},
        after={"target": f"{release_target}/bin/league", "version": __version__},
        rollback={
            "action": backup_by_id[launcher_target["target_id"]]["rollback"],
            "source": str(backup_root / "targets" / launcher_target["target_id"]),
        },
    )
    watcher_launcher_target = _target_for_path(plan, proposed["watcher_launcher"])
    add(
        "switch_watcher_launcher",
        proposed["watcher_launcher"],
        precondition={
            "sha256": backup_by_id[watcher_launcher_target["target_id"]]["sha256"]
        },
        after={"target": f"{release_target}/bin/agent-watcher", "version": __version__},
        rollback={
            "action": backup_by_id[watcher_launcher_target["target_id"]]["rollback"],
            "source": str(
                backup_root / "targets" / watcher_launcher_target["target_id"]
            ),
        },
    )
    for hook in sorted(proposed["hooks"], key=lambda item: item["harness"]):
        target = _target_for_path(plan, hook["target"])
        hook_plan = {
            "schema": "league.hook-mutation-plan.v1",
            "harness": hook["harness"],
            "stable_launcher": proposed["stable_launcher"],
            "preserve_unrelated": True,
            "writer_generation": generation,
        }
        add(
            "replace_league_hook_adapter",
            hook["target"],
            precondition={"sha256": backup_by_id[target["target_id"]]["sha256"]},
            after={"plan_sha256": _sha256(_stable_bytes(hook_plan)), **hook_plan},
            rollback={
                "action": backup_by_id[target["target_id"]]["rollback"],
                "source": str(backup_root / "targets" / target["target_id"]),
            },
        )
    pointer_target = _target_for_path(plan, proposed["writer_pointer"])
    add(
        "switch_canonical_writer_pointer",
        proposed["writer_pointer"],
        precondition={
            "sha256": backup_by_id[pointer_target["target_id"]]["sha256"],
            "active_writer_count": 0,
        },
        after={"pointer": pointer, "sha256": _sha256(_stable_bytes(pointer))},
        rollback={
            "action": backup_by_id[pointer_target["target_id"]]["rollback"],
            "source": str(backup_root / "targets" / pointer_target["target_id"]),
        },
    )
    add(
        "activate_sqlite_writer",
        database_target,
        precondition={"pointer_generation": generation, "active_writer_count": 0},
        after={"active_writer_count": 1, "generation": generation},
        rollback={"action": "deactivate_sqlite_before_restoring_old_pointer"},
    )
    add(
        "run_live_synthetic_smoke",
        generation,
        precondition={"separate_authority": True, "intake": "blocked"},
        after={"transition_delivery_stop_teardown": "must_pass"},
        rollback={"action": "keep_intake_blocked_and_run_full_rollback"},
    )
    add(
        "reopen_intake",
        generation,
        precondition={"live_smoke": "passed", "one_writer": True},
        after={"intake": "open"},
        rollback={"action": "not_applicable_after_success_receipt"},
    )
    rollback_sequence = [
        "keep_intake_blocked",
        "deactivate_sqlite_writer",
        "restore_writer_pointer",
        "restore_hook_adapters",
        "restore_watcher_launcher",
        "restore_stable_launcher",
        "verify_legacy_state_hashes",
        "verify_old_watcher",
        "record_rollback_receipt",
    ]
    manifest = {
        "schema": MUTATION_SCHEMA,
        "applied": False,
        "authority_required": True,
        "writer_generation": generation,
        "source_version": __version__,
        "operations": operations,
        "rollback_sequence": rollback_sequence,
        "backup_root": proposed["backup_root"],
        "archive_root": proposed["archive_root"],
        "supervision": {
            "normal_wake": "event_driven_registered_listener",
            "user_prompt_wake": "event_driven_registered_listener",
            "unchanged_output": "silent",
            "reconciliation": {
                "purpose": "missed_wake_and_lease_recovery_only",
                "interval_seconds": 30,
                "consecutive_observations": 2,
                "earliest_fallback_seconds": 60,
                "separate_15_second_policy": False,
            },
            "installed_default_command": [
                proposed["watcher_launcher"],
                "supervise",
                "--reconcile-seconds",
                "30",
                "--reconcile-consecutive",
                "2",
            ],
            "override_arguments": [
                "--reconcile-seconds",
                "<accepted-seconds>",
                "--reconcile-consecutive",
                "<accepted-observations>",
            ],
            "benchmark_decision": supervision_benchmark["proposal"]["decision"],
            "requires_cutover_authority": True,
        },
    }
    manifest["manifest_sha256"] = _sha256(_stable_bytes(manifest))
    return manifest


def _write_operation(
    path: Path,
    operation: dict[str, Any],
    state: str,
    context: DeterministicContext,
    **extra: Any,
) -> None:
    operation["state"] = state
    operation["history"].append({"state": state, "at": context.at, **extra})
    if state == "blocked":
        operation["error_code"] = extra["error_code"]
        operation["resumable"] = True
    elif state == "awaiting_authority":
        operation["mutation_manifest_sha256"] = extra["mutation_manifest_sha256"]
        operation["authority"] = "separate_explicit_live_cutover"
    _write_json(path, operation)


def run_pre_cutover(
    temporary_root: Path,
    namespace: str,
    *,
    plan_path: Path,
    sentinel_paths: tuple[Path, ...],
    config_sentinel: Path,
    process_sentinel: Path,
    source_root: Optional[Path] = None,
    fault: Optional[Any] = None,
) -> dict[str, Any]:
    supplied_root = Path(temporary_root)
    if (
        not supplied_root.is_absolute()
        or not supplied_root.is_dir()
        or supplied_root.is_symlink()
    ):
        raise StorageRefusal("invalid_temporary_root", "temporary root must be an explicit directory")
    root = supplied_root.resolve(strict=True)
    if root == Path("/"):
        raise StorageRefusal("invalid_temporary_root", "temporary root must be an explicit directory")
    if not NAMESPACE_PATTERN.fullmatch(namespace):
        raise StorageRefusal("invalid_namespace", "pre-cutover namespace is invalid")
    if not sentinel_paths:
        raise StorageRefusal("sentinel_required", "at least one byte sentinel is required")
    if len(sentinel_paths) > MAX_ACCEPTANCE_SENTINEL_PATHS:
        raise StorageRefusal("too_many_sentinels", "pre-cutover sentinel count exceeds the bound")
    plan = _validate_plan(Path(plan_path))
    for target in plan["current_targets"]:
        target_path = Path(target["path"])
        resolved_target = target_path.resolve(strict=False)
        if (
            root == target_path
            or root in target_path.parents
            or target_path in root.parents
            or root == resolved_target
            or root in resolved_target.parents
            or resolved_target in root.parents
        ):
            raise StorageRefusal(
                "unsafe_root_overlap",
                "temporary root and a planned live target must not overlap",
            )
    source = (source_root or Path(__file__).resolve().parents[2]).resolve()
    sentinels = SentinelSet(
        tuple(Path(path) for path in sentinel_paths),
        Path(config_sentinel),
        Path(process_sentinel),
    )
    home = root / f"league-{namespace}-precutover"
    try:
        home.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise StorageRefusal("namespace_collision", "pre-cutover namespace already exists") from exc
    context = DeterministicContext()
    operation = {
        "schema": OPERATION_SCHEMA,
        "namespace": namespace,
        "state": "planned",
        "history": [],
    }
    operation_path = home / "precutover-operation.json"
    _write_operation(operation_path, operation, "planned", context)
    _write_operation(operation_path, operation, "executing", context)
    try:
        before_targets = {
            item["target_id"]: _snapshot(Path(item["path"]))
            for item in plan["current_targets"]
        }
        backups = _backup_rehearsal(home, plan["current_targets"], fault=fault)
        fixture_home = home / "fixture-shadow"
        fixture_home.mkdir(mode=0o700)
        fixture_shadow = _migration_shadow(fixture_home, source)
        live_shadow = _read_only_shadow(home, plan)
        staged = _staged_install(home / "staged", source)
        staged["inactive_after_checks"] = staged["rollback"]["completed"]
        staged["global_install_performed"] = False
        staged["supervision"] = _staged_supervision_check(staged, home)
        manifest_checks = _manifest_checks(plan, backups, staged)
        cutover = _cutover_matrix(home / "cutover-faults", context)
        adapters = _fake_adapters(context)
        canary = _canary(home / "fake-canary", context, adapters)
        lifecycle = _integrated_lifecycle(home, source)
        runtime_canaries = _runtime_canaries(home, source)
        supervision_benchmark = _supervision_benchmark(home)
        sentinels_receipt = sentinels.verify()
        after_targets = {
            item["target_id"]: _snapshot(Path(item["path"]))
            for item in plan["current_targets"]
        }
        if before_targets != after_targets:
            raise StorageRefusal("sentinel_changed", "a planned live target changed during preflight")
        mutation_manifest = _mutation_manifest(
            plan,
            backups,
            live_shadow,
            staged,
            supervision_benchmark,
        )
        _write_json(home / "cutover-mutation-manifest.json", mutation_manifest)
        _write_operation(
            operation_path,
            operation,
            "awaiting_authority",
            context,
            mutation_manifest_sha256=mutation_manifest["manifest_sha256"],
        )
        result = {
            "schema": RECEIPT_SCHEMA,
            "version": __version__,
            "namespace": namespace,
            "home": str(home),
            "operation": operation,
            "determinism": {"clock": context.at, "ids_allocated": context.sequence},
            "sentinels": sentinels_receipt,
            "live_targets": {
                "unchanged": True,
                "target_count": len(before_targets),
                "aggregate_sha256": _sha256(_stable_bytes(before_targets)),
            },
            "fixture_migration_shadow": fixture_shadow,
            "live_migration_shadow": live_shadow,
            "backup_rollback_rehearsal": backups,
            "staged_inactive_install": staged,
            "manifest_checks": manifest_checks,
            "integrated_lifecycle": lifecycle,
            "runtime_contract_canaries": runtime_canaries,
            "supervision_benchmark": supervision_benchmark,
            "cutover_fault_matrix": cutover,
            "fake_resource_canary": canary,
            "mutation_manifest": mutation_manifest,
            "public_claims": {
                "global_install": False,
                "live_import": False,
                "hook_or_watcher_mutation": False,
                "canonical_cutover": False,
                "live_delivery": False,
                "real_runtime_support": False,
            },
        }
        _write_json(home / "precutover-receipt.json", result)
        return result
    except BaseException as exc:
        code = exc.code if isinstance(exc, StorageRefusal) else "precutover_failed"
        _write_operation(operation_path, operation, "blocked", context, error_code=code)
        raise
