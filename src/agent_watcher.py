#!/usr/bin/env python3
"""Small, agent- and multiplexer-neutral Champion lifecycle router.

Watching is read-only. Explicit lifecycle commands mutate only caller-selected
control files or exact resources that passed their fail-closed proof gates.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


ACTIVE_STATUSES = {
    "active",
    "working",
    "blocked",
    "progress",
    "started",
    "running",
    "completed",
    "complete",
    "cancelled",
    "canceled",
    "failed",
    "ready_to_land",
}
SUPPORTED_LIFECYCLE_STATUSES = {
    "active",
    "started",
    "working",
    "progress",
    "blocked",
    "completed",
    "complete",
    "cancelled",
    "canceled",
    "failed",
    "ready_to_land",
}
DELIVERY_STATUSES = {
    "blocked",
    "completed",
    "complete",
    "cancelled",
    "canceled",
    "failed",
    "ready_to_land",
}
RECONCILE_ACTIVE_STATUSES = {"active", "started", "working", "progress"}
HERDR_SETTLED_STATUSES = {"done", "completed", "complete", "closed", "exited", "settled"}
HERDR_RUNNING_STATUSES = {
    "active",
    "blocked",
    "idle",
    "needs-input",
    "running",
    "working",
}
LEGACY_SILENT_STATUSES = {"heartbeat", "liveness", "health"}
READY_TO_LAND_STATUS = "ready_to_land"
CURRENT_RECORD_FORMAT = "current"
EXACT_THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
HERDR_ADDRESS_PATTERN = re.compile(r"^w[0-9A-Za-z]+:p[0-9A-Za-z]+$")
TMUX_ADDRESS_PATTERN = re.compile(r"^%[0-9]+$")
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PLACEHOLDER_IDENTITY_VALUES = {
    "current-codex-thread",
    "current-session",
    "current-thread",
    "n/a",
    "none",
    "null",
    "placeholder",
    "tbd",
    "unavailable",
    "unknown",
}
WATCHER_STATE_SCHEMA = 2
DEFAULT_STATE = {
    "schema": WATCHER_STATE_SCHEMA,
    "enabled": True,
    "allow_stop_once": False,
    "stop_blocked": False,
    "generation": 0,
    "initialized": False,
    "last_active": [],
    "offsets": {},
    "seen": [],
    "user_message_generation": 0,
    "wait_active": False,
    "wait_generation": 0,
    "wait_pid": None,
    "wait_process_start": None,
    "pending_events": {},
    "delivered_events": {},
    "last_event_id": None,
    "reconciliation": {},
}


class WatcherError(RuntimeError):
    pass


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise WatcherError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WatcherError(f"JSON record is not an object: {path}")
    return value


def _object_without_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = nested
    return value


def _decode_strict_object(text: str, source: str) -> Dict[str, Any]:
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise WatcherError(f"record contract violation in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise WatcherError(f"record contract violation in {source}: JSON value must be an object")
    return value


def _require_record_text(value: Dict[str, Any], key: str, source: str) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not field.strip():
        raise WatcherError(
            f"record contract violation in {source}: {key} must be a non-empty string"
        )
    return field


def _require_exact_record_text(value: Dict[str, Any], key: str, source: str) -> str:
    field = _require_record_text(value, key, source).strip()
    if field.lower() in PLACEHOLDER_IDENTITY_VALUES:
        raise WatcherError(
            f"record contract violation in {source}: {key} must be exact, not a placeholder"
        )
    return field


def _champion_identity(value: Dict[str, Any], source: str) -> Dict[str, Any]:
    backend = _require_exact_record_text(value, "backend", source).lower()
    if backend not in {"herdr", "tmux"}:
        raise WatcherError(
            f"record contract violation in {source}: backend must be herdr or tmux"
        )
    thread_id = _require_exact_record_text(value, "thread_id", source)
    if not EXACT_THREAD_ID_PATTERN.fullmatch(thread_id):
        raise WatcherError(
            f"record contract violation in {source}: thread_id must be an exact Codex UUID"
        )
    address = _require_exact_record_text(value, "address", source)
    address_pattern = HERDR_ADDRESS_PATTERN if backend == "herdr" else TMUX_ADDRESS_PATTERN
    if not address_pattern.fullmatch(address):
        raise WatcherError(
            f"record contract violation in {source}: address does not match backend {backend}"
        )
    task_id = _require_exact_record_text(value, "task_id", source)
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise WatcherError(
            f"record contract violation in {source}: task_id is not an immutable safe identifier"
        )

    repository_fields = ("repository", "issue", "branch", "worktree")
    missing = [key for key in repository_fields if key not in value]
    if missing:
        raise WatcherError(
            f"record contract violation in {source}: required identity field {missing[0]} is missing"
        )
    repository_values = [value.get(key) for key in repository_fields]
    if any(item is None for item in repository_values):
        if not all(item is None for item in repository_values):
            raise WatcherError(
                f"record contract violation in {source}: repository, issue, branch, and worktree must all be exact or all null"
            )
        repository: Optional[str] = None
        issue: Optional[int] = None
        branch: Optional[str] = None
        worktree: Optional[str] = None
    else:
        repository = _require_exact_record_text(value, "repository", source)
        issue_value = value.get("issue")
        if isinstance(issue_value, bool) or not isinstance(issue_value, int) or issue_value <= 0:
            raise WatcherError(
                f"record contract violation in {source}: issue must be a positive integer or null"
            )
        issue = issue_value
        branch = _require_exact_record_text(value, "branch", source)
        worktree = _require_exact_record_text(value, "worktree", source)
        if not Path(worktree).is_absolute():
            raise WatcherError(
                f"record contract violation in {source}: worktree must be an exact absolute path"
            )

    return {
        "callsign": value.get("callsign"),
        "role": value.get("role"),
        "shotcaller": value.get("shotcaller"),
        "thread_id": thread_id,
        "address": address,
        "backend": backend,
        "task_id": task_id,
        "repository": repository,
        "issue": issue,
        "branch": branch,
        "worktree": worktree,
    }


def _validate_launch_names(value: Dict[str, Any], source: str) -> Tuple[str, str]:
    callsign = _require_exact_record_text(value, "callsign", source)
    routing_name = _require_exact_record_text(value, "routing_name", source)
    display_agent = _require_exact_record_text(value, "display_agent", source)
    if routing_name != callsign.lower():
        raise WatcherError(
            f"record contract violation in {source}: routing_name must be the lowercase callsign"
        )
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", routing_name):
        raise WatcherError(
            f"record contract violation in {source}: routing_name is not a valid Herdr name"
        )
    return routing_name, display_agent


def _require_record_timestamp(value: Any, field: str, source: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WatcherError(
            f"record contract violation in {source}: {field} must be an RFC3339 string"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WatcherError(
            f"record contract violation in {source}: {field} must be RFC3339"
        ) from exc
    if parsed.tzinfo is None:
        raise WatcherError(
            f"record contract violation in {source}: {field} must include a UTC offset"
        )
    return value


def _validate_status_snapshot(value: Dict[str, Any], path: Path) -> Dict[str, Any]:
    source = str(path)
    callsign = _require_record_text(value, "callsign", source)
    role = _require_record_text(value, "role", source).lower()
    if role not in {"shotcaller", "champion", "worker"}:
        raise WatcherError(
            f"record contract violation in {source}: unsupported role {value['role']!r}"
        )
    if "shotcaller" not in value:
        raise WatcherError(
            f"record contract violation in {source}: shotcaller field is required"
        )
    owner = value.get("shotcaller")
    if role == "shotcaller":
        if owner is not None:
            raise WatcherError(
                f"record contract violation in {source}: shotcaller must be null for a Shotcaller"
            )
    elif not isinstance(owner, str) or not owner.strip():
        raise WatcherError(
            f"record contract violation in {source}: shotcaller must be a non-empty string"
        )
    for key in ("kind", "address", "thread_id", "task"):
        _require_record_text(value, key, source)
    status = _require_record_text(value, "status", source).lower()
    if status not in SUPPORTED_LIFECYCLE_STATUSES:
        raise WatcherError(
            f"record contract violation in {source}: unsupported lifecycle status {value['status']!r}"
        )
    _require_record_timestamp(value.get("updated_at"), "updated_at", source)
    _require_record_text(value, "update", source)
    if "blocker" not in value:
        raise WatcherError(
            f"record contract violation in {source}: blocker field is required"
        )
    blocker = value.get("blocker")
    if blocker is not None and (not isinstance(blocker, str) or not blocker.strip()):
        raise WatcherError(
            f"record contract violation in {source}: blocker must be null or a non-empty string"
        )
    _require_record_text(value, "next", source)
    if "routing_name" in value or "display_agent" in value:
        if "routing_name" not in value or "display_agent" not in value:
            raise WatcherError(
                f"record contract violation in {source}: routing_name and display_agent must be recorded together"
            )
        _validate_launch_names(value, source)
    if callsign != path.parent.name:
        raise WatcherError(
            f"record contract violation in {source}: callsign does not match the record directory"
        )
    if role in {"champion", "worker"}:
        expected_owner = path.parent.parent.parent.name
        if owner != expected_owner:
            raise WatcherError(
                f"record contract violation in {source}: shotcaller does not match the Roster path"
            )
    if role == "champion":
        _champion_identity(value, source)
    return value


def _load_status(path: Path) -> Dict[str, Any]:
    if CURRENT_RECORD_FORMAT == "legacy":
        return _load_json(path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise WatcherError(f"record contract violation in {path}: invalid UTF-8") from exc
    except OSError as exc:
        raise WatcherError(f"cannot read status record {path}: {exc}") from exc
    return _validate_status_snapshot(_decode_strict_object(text, str(path)), path)


def _validate_transition(value: Dict[str, Any], source: str) -> Dict[str, Any]:
    _require_record_timestamp(value.get("at"), "at", source)
    status = _require_record_text(value, "status", source).lower()
    if status not in SUPPORTED_LIFECYCLE_STATUSES:
        raise WatcherError(
            f"record contract violation in {source}: unsupported lifecycle status {value['status']!r}"
        )
    _require_record_text(value, "update", source)
    return value


def _load_transitions(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise WatcherError(f"record contract violation in {path}: cannot read transition log: {exc}") from exc
    transitions: List[Dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        source = f"{path}:{line_number}"
        if CURRENT_RECORD_FORMAT == "legacy":
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WatcherError(f"invalid update JSON {source}: {exc}") from exc
            if not isinstance(value, dict):
                raise WatcherError(f"update is not an object: {source}")
        else:
            value = _validate_transition(_decode_strict_object(line, source), source)
        transitions.append(value)
    if CURRENT_RECORD_FORMAT != "legacy" and not transitions:
        raise WatcherError(f"record contract violation in {path}: transition log is empty")
    return transitions


@contextmanager
def _record_lock(record_dir: Path):
    lock_path = record_dir / "updates.jsonl"
    try:
        handle = lock_path.open("r", encoding="utf-8")
    except OSError as exc:
        raise WatcherError(
            f"record contract violation in {record_dir}: transition log is missing or unreadable"
        ) from exc
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_record_pair_unlocked(status_path: Path) -> Dict[str, Any]:
    snapshot = _load_status(status_path)
    if CURRENT_RECORD_FORMAT == "legacy" or str(snapshot.get("role", "")).lower() == "shotcaller":
        return snapshot
    updates_path = status_path.parent / "updates.jsonl"
    transitions = _load_transitions(updates_path)
    latest = transitions[-1]
    expected = {
        "status": snapshot.get("status"),
        "at": snapshot.get("updated_at"),
        "update": snapshot.get("update"),
    }
    observed = {
        "status": latest.get("status"),
        "at": latest.get("at"),
        "update": latest.get("update"),
    }
    if observed != expected:
        raise WatcherError(
            f"record contract violation in {status_path.parent}: latest transition does not match status snapshot"
        )
    return snapshot


def _validate_record_pair(status_path: Path) -> Dict[str, Any]:
    with _record_lock(status_path.parent):
        return _validate_record_pair_unlocked(status_path)


def _resolve_transition_target(records_root: Path, record_dir: Path) -> Path:
    try:
        resolved_root = records_root.resolve(strict=True)
        resolved_record = record_dir.expanduser().resolve(strict=True)
        resolved_record.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise WatcherError("transition record must be an existing directory under records-root") from exc
    return resolved_record


def transition_record(
    records_root: Path,
    record_dir: Path,
    status: str,
    update: str,
    next_action: str,
    blocker: Optional[str],
    at: Optional[str],
) -> Dict[str, Any]:
    if CURRENT_RECORD_FORMAT != "current":
        raise WatcherError("atomic transition requires the current record format")
    resolved_record = _resolve_transition_target(records_root, record_dir)
    status = status.lower()
    if status not in DELIVERY_STATUSES:
        raise WatcherError("atomic transition status must be material")
    if not update.strip() or not next_action.strip():
        raise WatcherError("atomic transition update and next must be non-empty")
    if blocker is not None and not blocker.strip():
        raise WatcherError("atomic transition blocker must be non-empty when supplied")
    transition_at = at or datetime.now().astimezone().isoformat(timespec="seconds")
    _require_record_timestamp(transition_at, "at", "transition command")
    status_path = resolved_record / "status.json"
    updates_path = resolved_record / "updates.jsonl"
    with _record_lock(resolved_record):
        snapshot = _validate_record_pair_unlocked(status_path)
        if str(snapshot.get("role", "")).lower() not in {"champion", "worker"}:
            raise WatcherError("atomic transition target must be a Champion or worker record")
        next_snapshot = dict(snapshot)
        next_snapshot.update(
            {
                "status": status,
                "updated_at": transition_at,
                "update": update,
                "blocker": blocker,
                "next": next_action,
            }
        )
        _validate_status_snapshot(next_snapshot, status_path)
        transition = _validate_transition(
            {"at": transition_at, "status": status, "update": update},
            "transition command",
        )
        try:
            original_size = updates_path.stat().st_size
        except OSError as exc:
            raise WatcherError(f"cannot inspect transition log {updates_path}: {exc}") from exc
        try:
            with updates_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(transition, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            _write_json_atomic(status_path, next_snapshot)
        except (OSError, WatcherError) as exc:
            try:
                with updates_path.open("r+b") as handle:
                    handle.truncate(original_size)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as rollback_error:
                raise WatcherError(
                    f"atomic transition failed and log rollback failed: {rollback_error}"
                ) from exc
            raise WatcherError(f"atomic transition failed without a partial record: {exc}") from exc
        _validate_record_pair_unlocked(status_path)
    return {
        "event": "record-transition",
        "record": str(resolved_record),
        "status": status,
        "at": transition_at,
        "update": update,
    }


class Store:
    """Atomic control/cursor state with a process lock for mutations."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_path = state_dir / "state.json"
        self.lock_path = state_dir / ".state.lock"
        self.wait_lock_path = state_dir / ".wait.lock"

    def read(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return dict(DEFAULT_STATE)
        value = _load_json(self.state_path)
        state = dict(DEFAULT_STATE)
        state.update(value)
        return state

    def mutate(self, function: Callable[[Dict[str, Any]], Any]) -> Any:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            state = self.read()
            result = function(state)
            self._write(state)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            return result

    def _write(self, state: Dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.state_dir, prefix=".state.", delete=False
        ) as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, self.state_path)

    def acquire_wait_lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = self.wait_lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise WatcherError("another watcher wait is already active") from exc
        return handle


def _write_json_atomic(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _mutate_json(path: Path, function: Callable[[Dict[str, Any]], Any]) -> Any:
    lock_path = path.with_name(f".{path.name}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        value = _load_json(path)
        result = function(value)
        _write_json_atomic(path, value)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return result


def _resolve_shotcaller(
    records_root: Path, shotcaller: Optional[str], session_id: Optional[str]
) -> Optional[str]:
    if shotcaller:
        status_path = records_root / shotcaller / "status.json"
        record = _load_status(status_path)
        if str(record.get("role", "")).lower() != "shotcaller":
            raise WatcherError(f"Shotcaller record has the wrong role: {status_path}")
        return shotcaller
    if not session_id:
        return None
    matches: List[str] = []
    for status_path in sorted(records_root.glob("*/status.json")):
        record = _load_status(status_path)
        if str(record.get("role", "")).lower() != "shotcaller":
            continue
        identities = {str(record.get("thread_id", "")), str(record.get("address", ""))}
        if session_id in identities:
            matches.append(str(record.get("callsign") or status_path.parent.name))
    if len(matches) != 1:
        detail = "not found" if not matches else "ambiguous"
        raise WatcherError(f"Shotcaller for session {session_id} is {detail}")
    return matches[0]


def _record_paths(records_root: Path, shotcaller: Optional[str] = None) -> Iterable[Path]:
    if not records_root.is_dir():
        raise WatcherError(f"records root is unavailable: {records_root}")
    if shotcaller:
        champions_root = records_root / shotcaller / "champions"
        if not champions_root.exists():
            return []
        return sorted(champions_root.glob("*/status.json"))
    return sorted(records_root.glob("*/champions/*/status.json"))


def _active_records(
    records_root: Path, shotcaller: Optional[str] = None
) -> List[Tuple[Path, Dict[str, Any]]]:
    active: List[Tuple[Path, Dict[str, Any]]] = []
    for status_path in _record_paths(records_root, shotcaller):
        record = _validate_record_pair(status_path)
        if str(record.get("role", "")).lower() not in {"champion", "worker"}:
            continue
        if str(record.get("status", "")).lower() in ACTIVE_STATUSES:
            active.append((status_path, record))
    return active


def _event_digest(path: Path, offset: int, line: str) -> str:
    payload = f"{path}\0{offset}\0{line}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_updates(
    updates_path: Path, offset: int, seen: set[str], record: Dict[str, Any]
) -> Tuple[int, List[Dict[str, Any]], set[str], bool]:
    if not updates_path.exists():
        if CURRENT_RECORD_FORMAT == "legacy":
            return 0, [], seen, False
        raise WatcherError(f"record contract violation in {updates_path}: transition log is missing")
    try:
        size = updates_path.stat().st_size
        if size < offset:
            offset = 0
        with updates_path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError as exc:
        raise WatcherError(f"cannot read updates {updates_path}: {exc}") from exc

    consumed = 0
    events: List[Dict[str, Any]] = []
    saw_transition = False
    line_number = 0
    for raw_line in data.splitlines(keepends=True):
        if not raw_line.endswith(b"\n"):
            break
        line_number += 1
        line_size = len(raw_line)
        try:
            line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
        except UnicodeError as exc:
            raise WatcherError(
                f"record contract violation in {updates_path}@byte-{offset + consumed}: invalid UTF-8"
            ) from exc
        digest = _event_digest(updates_path, offset + consumed, line)
        consumed += line_size
        if digest in seen:
            continue
        seen.add(digest)
        source = f"{updates_path}@byte-{offset + consumed - line_size}"
        if CURRENT_RECORD_FORMAT == "legacy":
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WatcherError(f"invalid update JSON {source}: {exc}") from exc
            if not isinstance(value, dict):
                raise WatcherError(f"update is not an object: {source}")
        else:
            value = _validate_transition(_decode_strict_object(line, source), source)
        saw_transition = True
        status = str(value.get("status", "")).lower()
        if CURRENT_RECORD_FORMAT == "legacy" and status in LEGACY_SILENT_STATUSES:
            continue
        if status not in DELIVERY_STATUSES:
            continue
        role = str(record.get("role", "champion")).lower()
        events.append(
            {
                "event": "worker-update" if role == "worker" else "champion-update",
                "event_id": digest,
                "record": str(updates_path.parent),
                "source_path": str(updates_path),
                "source_offset": offset + consumed - line_size,
                "callsign": record.get("callsign"),
                "shotcaller": record.get("shotcaller"),
                "status": value.get("status"),
                "at": value.get("at"),
                "update": value.get("update"),
            }
        )
    return offset + consumed, events, seen, saw_transition


def _pending_candidate(event_id: Any, value: Any) -> Dict[str, Any]:
    if not isinstance(event_id, str) or not event_id:
        raise WatcherError("watcher event candidate has an invalid event id")
    if not isinstance(value, dict) or value.get("event_id") != event_id:
        raise WatcherError("watcher event candidate id and payload are not atomic")
    candidate = dict(value)
    for field in ("event", "record", "source_path", "callsign", "shotcaller", "status", "at", "update"):
        if not isinstance(candidate.get(field), str) or not str(candidate[field]).strip():
            raise WatcherError(f"watcher event candidate has an invalid {field}")
    source_offset = candidate.get("source_offset")
    if isinstance(source_offset, bool) or not isinstance(source_offset, int) or source_offset < 0:
        raise WatcherError("watcher event candidate has an invalid source offset")
    if candidate["event"] in {"champion-update", "worker-update"}:
        if candidate["status"] not in DELIVERY_STATUSES:
            raise WatcherError("watcher event candidate has an invalid lifecycle status")
    elif candidate["event"] != "champion_stalled" or candidate["status"] != "champion_stalled":
        raise WatcherError("watcher event candidate has an unsupported event type")
    return candidate


def _next_pending_event(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pending = state.get("pending_events", {})
    delivered = state.get("delivered_events", {})
    if not isinstance(pending, dict) or not isinstance(delivered, dict):
        raise WatcherError("watcher delivery state is invalid")
    for event_id in list(pending):
        if event_id in delivered:
            del pending[event_id]
    if not pending:
        return None
    candidates = [_pending_candidate(event_id, event) for event_id, event in pending.items()]
    return min(
        candidates,
        key=lambda event: (str(event.get("source_path", "")), int(event.get("source_offset", 0))),
    )


def _direct_event_record_dir(
    event: Dict[str, Any], records_root: Path, shotcaller: str
) -> Path:
    record_dir = Path(str(event.get("record", "")))
    try:
        resolved_record = record_dir.resolve(strict=True)
        resolved_record.relative_to((records_root / shotcaller / "champions").resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise WatcherError("direct delivery candidate has an invalid Roster identity") from exc
    return resolved_record


def _event_matches_latest_unlocked(
    event: Dict[str, Any], resolved_record: Path
) -> bool:
    snapshot = _validate_record_pair_unlocked(resolved_record / "status.json")
    updates_path = resolved_record / "updates.jsonl"
    try:
        source_path = Path(event["source_path"])
        resolved_source = source_path.resolve(strict=True)
        source_offset = event["source_offset"]
        with updates_path.open("rb") as handle:
            handle.seek(source_offset)
            raw_line = handle.readline()
        raw_lines = updates_path.read_bytes().splitlines(keepends=True)
    except OSError as exc:
        raise WatcherError(f"cannot read updates {updates_path}: {exc}") from exc
    try:
        resolved_event_record = Path(event["record"]).resolve(strict=True)
    except OSError as exc:
        raise WatcherError("event candidate conflicts with its durable source") from exc
    if resolved_source != updates_path or resolved_event_record != resolved_record:
        raise WatcherError("event candidate conflicts with its durable source")
    if not raw_line.endswith(b"\n"):
        raise WatcherError("event candidate conflicts with its durable source")
    try:
        line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
    except UnicodeError as exc:
        raise WatcherError("event candidate conflicts with its durable source") from exc
    transition = _validate_transition(
        _decode_strict_object(line, f"{updates_path}@byte-{source_offset}"),
        f"{updates_path}@byte-{source_offset}",
    )
    if _event_digest(source_path, source_offset, line) != event["event_id"]:
        raise WatcherError("event candidate conflicts with its durable source")
    expected_role_event = (
        "worker-update" if str(snapshot.get("role", "")).lower() == "worker" else "champion-update"
    )
    candidate_identity = {
        "event": event.get("event"),
        "callsign": event.get("callsign"),
        "shotcaller": event.get("shotcaller"),
        "status": event.get("status"),
        "at": event.get("at"),
        "update": event.get("update"),
    }
    durable_identity = {
        "event": expected_role_event,
        "callsign": snapshot.get("callsign"),
        "shotcaller": snapshot.get("shotcaller"),
        "status": transition.get("status"),
        "at": transition.get("at"),
        "update": transition.get("update"),
    }
    if candidate_identity != durable_identity:
        raise WatcherError("event candidate conflicts with its durable source")
    latest_offset = sum(len(item) for item in raw_lines[:-1])
    if source_offset != latest_offset:
        return False
    return {
        "status": event.get("status"),
        "at": event.get("at"),
        "update": event.get("update"),
    } == {
        "status": snapshot.get("status"),
        "at": snapshot.get("updated_at"),
        "update": snapshot.get("update"),
    }


def _baseline_state(
    state: Dict[str, Any], active: List[Tuple[Path, Dict[str, Any]]]
) -> Tuple[List[Path], Optional[Dict[str, Any]], bool]:
    offsets: Dict[str, int] = {}
    seen: set[str] = set()
    active_paths = [path for path, _ in active]
    for status_path, record in active:
        updates_path = status_path.parent / "updates.jsonl"
        next_offset, _, seen, _ = _read_updates(updates_path, 0, seen, record)
        offsets[str(updates_path)] = next_offset
    state.update(
        {
            "schema": WATCHER_STATE_SCHEMA,
            "initialized": True,
            "last_active": [str(path) for path in active_paths],
            "offsets": offsets,
            "seen": list(seen)[-2048:],
            "pending_events": {},
            "last_event_id": None,
        }
    )
    return active_paths, None, False


def _scan_state(
    state: Dict[str, Any], records_root: Path, shotcaller: Optional[str]
) -> Tuple[List[Path], Optional[Dict[str, Any]], bool]:
    active = _active_records(records_root, shotcaller)
    if state.get("schema") != WATCHER_STATE_SCHEMA or state.get("initialized") is not True:
        return _baseline_state(state, active)
    active_paths = [path for path, _ in active]
    active_keys = [str(path) for path in active_paths]
    offsets = dict(state.get("offsets", {}))
    seen = set(state.get("seen", []))
    pending = state.get("pending_events", {})
    delivered = state.get("delivered_events", {})
    if not isinstance(pending, dict) or not isinstance(delivered, dict):
        raise WatcherError("watcher delivery state is invalid")
    saw_transition = False

    for status_path, record in active:
        updates_path = status_path.parent / "updates.jsonl"
        key = str(updates_path)
        next_offset, update_events, seen, saw_updates = _read_updates(
            updates_path, int(offsets.get(key, 0)), seen, record
        )
        saw_transition = saw_transition or saw_updates
        offsets[key] = next_offset
        for update_event in update_events:
            event_id = str(update_event["event_id"])
            state["last_event_id"] = event_id
            if event_id not in delivered:
                pending[event_id] = update_event

    state["initialized"] = True
    state["last_active"] = active_keys
    state["offsets"] = offsets
    state["seen"] = list(seen)[-2048:]
    state["pending_events"] = pending
    return active_paths, _next_pending_event(state), saw_transition


def _scan_and_record(
    store: Store, records_root: Path, shotcaller: Optional[str] = None
) -> Tuple[List[Path], Optional[Dict[str, Any]], bool]:
    return store.mutate(lambda state: _scan_state(state, records_root, shotcaller))


def _ensure_state_baseline(store: Store, records_root: Path, shotcaller: str) -> None:
    def ensure(state: Dict[str, Any]) -> None:
        if state.get("schema") != WATCHER_STATE_SCHEMA or state.get("initialized") is not True:
            _baseline_state(state, _active_records(records_root, shotcaller))

    store.mutate(ensure)


def _claim_watcher_event(
    store: Store,
    event: Dict[str, Any],
    records_root: Path,
    shotcaller: Optional[str],
) -> bool:
    if event.get("event") in {"champion-update", "worker-update"}:
        event_shotcaller = shotcaller or str(event.get("shotcaller", ""))
        if not event_shotcaller:
            raise WatcherError("material event delivery requires an exact Shotcaller scope")
        record_dir = _direct_event_record_dir(event, records_root, event_shotcaller)
        with _record_lock(record_dir):
            if not _event_matches_latest_unlocked(event, record_dir):
                _mark_delivered(store, str(event["event_id"]), "superseded")
                return False
    return _mark_delivered(store, str(event["event_id"]), "watcher")


def _mark_state_delivered(state: Dict[str, Any], event_id: str, channel: str) -> bool:
    delivered = state.get("delivered_events", {})
    pending = state.get("pending_events", {})
    if not isinstance(delivered, dict) or not isinstance(pending, dict):
        raise WatcherError("watcher delivery state is invalid")
    if event_id in delivered:
        return False
    delivered[event_id] = {"channel": channel}
    while len(delivered) > 2048:
        del delivered[next(iter(delivered))]
    pending.pop(event_id, None)
    state["delivered_events"] = delivered
    state["pending_events"] = pending
    return True


def _mark_delivered(store: Store, event_id: str, channel: str) -> bool:
    def mark(state: Dict[str, Any]) -> bool:
        return _mark_state_delivered(state, event_id, channel)

    return bool(store.mutate(mark))


def _emit(value: Dict[str, Any]) -> None:
    print(_json_dump(value), flush=True)


def _repair_once(command: Optional[str], delay: float) -> None:
    if command:
        try:
            subprocess.run(
                shlex.split(command),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        time.sleep(min(max(delay, 0.05), 0.25))


def _is_record_contract_error(exc: WatcherError) -> bool:
    return "record contract violation" in str(exc)


def _runtime_route(
    adapter: Optional[str], herdr_session: Optional[str], tmux_socket: Optional[str]
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    resolved = adapter
    if resolved is None and os.environ.get("HERDR_ENV") == "1":
        resolved = "herdr"
    if resolved is None and os.environ.get("TMUX"):
        resolved = "tmux"
    if resolved == "herdr":
        return resolved, herdr_session or os.environ.get("HERDR_SESSION"), None
    if resolved == "tmux":
        socket = tmux_socket
        if socket is None and os.environ.get("TMUX"):
            socket = os.environ["TMUX"].split(",", 1)[0]
        if not socket:
            raise WatcherError("automatic tmux reconciliation requires --tmux-socket or TMUX")
        return resolved, None, socket
    return None, None, None


def wait_for_event(
    records_root: Path,
    store: Store,
    poll_seconds: float,
    liveness_seconds: float,
    repair_command: Optional[str],
    shotcaller: Optional[str] = None,
    event_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    runtime_adapter: Optional[str] = None,
    herdr_session: Optional[str] = None,
    tmux_socket: Optional[str] = None,
    reconcile_seconds: float = 30.0,
    reconcile_consecutive: int = 2,
) -> int:
    try:
        wait_handle = store.acquire_wait_lock()
    except WatcherError as exc:
        print(f"ERROR: {exc}; fail-open", file=sys.stderr)
        _emit({"event": "watcher-unavailable", "fail_open": True, "reason": str(exc)})
        return 0

    try:
        def mark_wait_active(state: Dict[str, Any]) -> None:
            process = inspect_process(os.getpid())
            state.update(
                {
                    "stop_blocked": False,
                    "wait_active": True,
                    "wait_pid": os.getpid(),
                    "wait_process_start": process["process_start"],
                }
            )
            state["wait_generation"] = int(state.get("wait_generation", 0)) + 1

        store.mutate(mark_wait_active)
        try:
            active, initial_event, _ = _scan_and_record(store, records_root, shotcaller)
        except WatcherError as first_error:
            if _is_record_contract_error(first_error):
                print(f"ERROR: {first_error}", file=sys.stderr)
                return 1
            print(
                f"WARN: watcher read failed; one bounded repair/restart attempt: {first_error}",
                file=sys.stderr,
            )
            _repair_once(repair_command, poll_seconds)
            try:
                active, initial_event, _ = _scan_and_record(store, records_root, shotcaller)
            except WatcherError as second_error:
                if _is_record_contract_error(second_error):
                    print(f"ERROR: {second_error}", file=sys.stderr)
                    return 1
                print(f"ERROR: watcher unavailable after one retry; fail-open: {second_error}", file=sys.stderr)
                _emit({"event": "watcher-unavailable", "fail_open": True, "reason": str(second_error)})
                return 0

        state = store.read()
        if not state.get("enabled", True):
            _emit({"event": "disabled"})
            return 0
        if state.get("allow_stop_once"):
            _emit({"event": "allow-stop"})
            return 0
        if initial_event is not None:
            if _claim_watcher_event(store, initial_event, records_root, shotcaller):
                if event_handler is not None:
                    initial_event = event_handler(initial_event)
                _emit(initial_event)
                return 0
        if not active:
            _emit({"event": "idle", "active": 0})
            return 0

        runtime_adapter, herdr_session, tmux_socket = _runtime_route(
            runtime_adapter, herdr_session, tmux_socket
        )
        reconciliation_enabled = (
            runtime_adapter is not None and shotcaller is not None and reconcile_seconds > 0
        )
        reconciliation_deadline = (
            time.monotonic() + max(reconcile_seconds, 0.05)
            if reconciliation_enabled
            else float("inf")
        )
        user_generation = int(state.get("user_message_generation", 0))
        liveness_deadline = time.monotonic() + max(liveness_seconds, 0.01)
        while True:
            state = store.read()
            if not state.get("enabled", True):
                _emit({"event": "disabled"})
                return 0
            if state.get("allow_stop_once"):
                _emit({"event": "allow-stop"})
                return 0
            if int(state.get("user_message_generation", 0)) != user_generation:
                _emit({"event": "user-message", "priority": "user", "shotcaller": shotcaller})
                return 0
            now = time.monotonic()
            if now >= reconciliation_deadline:
                try:
                    reconcile_runtime(
                        records_root,
                        store,
                        str(shotcaller),
                        str(runtime_adapter),
                        herdr_session,
                        tmux_socket,
                        reconcile_consecutive,
                        deliver_event=False,
                    )
                except WatcherError as exc:
                    if not _is_record_contract_error(exc):
                        store.mutate(lambda value: value.update({"reconciliation": {}}))
                    print(f"WARN: runtime reconciliation preserved records: {exc}", file=sys.stderr)
                reconciliation_deadline = now + max(reconcile_seconds, 0.05)
            try:
                active, event, _ = _scan_and_record(store, records_root, shotcaller)
            except WatcherError as first_error:
                if _is_record_contract_error(first_error):
                    print(f"ERROR: {first_error}", file=sys.stderr)
                    return 1
                print(
                    f"WARN: watcher read failed; one bounded repair/restart attempt: {first_error}",
                    file=sys.stderr,
                )
                _repair_once(repair_command, poll_seconds)
                try:
                    active, event, _ = _scan_and_record(store, records_root, shotcaller)
                except WatcherError as second_error:
                    if _is_record_contract_error(second_error):
                        print(f"ERROR: {second_error}", file=sys.stderr)
                        return 1
                    print(f"ERROR: watcher unavailable after one retry; fail-open: {second_error}", file=sys.stderr)
                    _emit({"event": "watcher-unavailable", "fail_open": True, "reason": str(second_error)})
                    return 0
            if event is not None:
                if _claim_watcher_event(store, event, records_root, shotcaller):
                    if event_handler is not None:
                        event = event_handler(event)
                    _emit(event)
                    return 0
            if not active:
                _emit({"event": "champions-idle", "active": 0})
                return 0

            now = time.monotonic()
            if now >= liveness_deadline:
                # Liveness is deliberately silent. Resetting the deadline is
                # the only observable effect, so this never wakes the model.
                liveness_deadline = now + max(liveness_seconds, 0.01)
            next_deadline = min(liveness_deadline, reconciliation_deadline)
            time.sleep(min(max(poll_seconds, 0.01), max(next_deadline - now, 0.01)))
    finally:
        try:
            def clear_wait(state: Dict[str, Any]) -> None:
                if state.get("wait_pid") == os.getpid():
                    state.update(
                        {"wait_active": False, "wait_pid": None, "wait_process_start": None}
                    )

            store.mutate(clear_wait)
        except WatcherError:
            pass
        fcntl.flock(wait_handle.fileno(), fcntl.LOCK_UN)
        wait_handle.close()


def _control(store: Store, action: str) -> Dict[str, Any]:
    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        state["generation"] = int(state.get("generation", 0)) + 1
        if action == "enable":
            state["enabled"] = True
            state["stop_blocked"] = False
        elif action == "disable":
            state["enabled"] = False
            state["allow_stop_once"] = False
            state["stop_blocked"] = False
        elif action == "allow-stop":
            state["allow_stop_once"] = True
            state["stop_blocked"] = False
        elif action == "user-message":
            state["user_message_generation"] = int(state.get("user_message_generation", 0)) + 1
            state["stop_blocked"] = False
        else:
            raise WatcherError(f"unknown control action: {action}")
        return {"event": action, "enabled": bool(state["enabled"]), "generation": state["generation"]}

    return store.mutate(mutate)


def codex_stop_hook(records_root: Path, store: Store, shotcaller: Optional[str]) -> int:
    """Emit Codex Stop-hook JSON without waiting or invoking model work."""
    try:
        active = _active_records(records_root, shotcaller)
        state = store.read()
    except WatcherError as exc:
        print(f"ERROR: Stop-hook watcher unavailable; fail-open: {exc}", file=sys.stderr)
        _emit({})
        return 0

    if not state.get("enabled", True) or not active:
        _emit({})
        return 0
    if state.get("allow_stop_once"):
        def consume(value: Dict[str, Any]) -> None:
            value["allow_stop_once"] = False
            value["stop_blocked"] = False

        store.mutate(consume)
        _emit({})
        return 0
    def block(value: Dict[str, Any]) -> None:
        value["stop_blocked"] = True

    store.mutate(block)
    _emit(
        {
            "decision": "block",
            "reason": f"Delegates for attached Shotcaller {shotcaller or 'current'} remain active. Every unchanged Stop remains blocked until obligations settle or the explicit allow-stop --once override is used. A ready_to_land Champion remains intact until the Shotcaller supplies exact landing/release proof to teardown.",
        }
    )
    return 0


def codex_user_prompt_hook(store: Store) -> int:
    _control(store, "user-message")
    _emit({})
    return 0


def _direct_prompt_text(event: Dict[str, Any]) -> str:
    update = " ".join(str(event.get("update", "")).split())
    return (
        f"CHAMPION TRANSITION [{event['event_id']}] "
        f"{event.get('callsign')} {event.get('status')}: {update}"
    )


def _live_wait(state: Dict[str, Any]) -> bool:
    if state.get("wait_active") is not True:
        return False
    pid = state.get("wait_pid")
    expected_start = state.get("wait_process_start")
    if not isinstance(pid, int) or not isinstance(expected_start, str):
        state.update({"wait_active": False, "wait_pid": None, "wait_process_start": None})
        return False
    try:
        process = inspect_process(pid)
    except WatcherError:
        state.update({"wait_active": False, "wait_pid": None, "wait_process_start": None})
        return False
    if process["process_start"] != expected_start:
        state.update({"wait_active": False, "wait_pid": None, "wait_process_start": None})
        return False
    return True


def _herdr_direct_prompt(
    shotcaller_record: Dict[str, Any], session: Optional[str], prompt: str
) -> Optional[Dict[str, Any]]:
    if session:
        base = ["herdr", "--session", session]
    elif os.environ.get("HERDR_ENV") == "1":
        base = ["herdr"]
    else:
        raise WatcherError("Herdr direct delivery requires the current Herdr session or --herdr-session")
    try:
        listed = subprocess.run(
            base + ["agent", "list"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        payload = json.loads(listed.stdout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise WatcherError(f"Herdr delivery adapter unavailable: {exc}") from exc
    agents = payload.get("result", {}).get("agents", []) if isinstance(payload, dict) else []
    thread_id = str(shotcaller_record["thread_id"])
    address = str(shotcaller_record["address"])
    pane_address = address if ":p" in address else None
    matches = []
    for agent in agents if isinstance(agents, list) else []:
        if not isinstance(agent, dict):
            continue
        agent_session = agent.get("agent_session")
        session_value = agent_session.get("value") if isinstance(agent_session, dict) else None
        identities = {str(value) for value in (session_value, agent.get("agent"), agent.get("name")) if value}
        if thread_id not in identities:
            continue
        if pane_address is not None and agent.get("pane_id") != pane_address:
            continue
        matches.append(agent)
    if not matches:
        return None
    if len(matches) != 1:
        raise WatcherError("Herdr direct delivery identity is ambiguous")
    pane_id = str(matches[0].get("pane_id", ""))
    if not pane_id:
        raise WatcherError("Herdr direct delivery pane identity is missing")
    try:
        subprocess.run(
            base + ["agent", "prompt", pane_id, prompt],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise WatcherError(f"Herdr direct prompt failed; durable event preserved: {exc}") from exc
    return {"pane_id": pane_id, "thread_id": thread_id}


def _tmux_direct_prompt(
    shotcaller_record: Dict[str, Any], socket: Optional[str], prompt: str
) -> Optional[Dict[str, Any]]:
    if not socket:
        raise WatcherError("tmux direct delivery requires --tmux-socket")
    pane = str(shotcaller_record["address"])
    try:
        inspected = subprocess.run(
            [
                "tmux",
                "-S",
                socket,
                "display-message",
                "-p",
                "-t",
                pane,
                "#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_command}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.CalledProcessError:
        return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WatcherError(f"tmux delivery adapter unavailable: {exc}") from exc
    fields = inspected.stdout.rstrip("\n").split("\t")
    if len(fields) != 4 or fields[0] != pane or not fields[1].isdigit() or fields[2] != "0":
        raise WatcherError("tmux direct delivery identity conflicts with the Shotcaller record")
    if fields[3] in {"sh", "bash", "zsh", "fish"}:
        raise WatcherError("tmux direct delivery refused: exact pane is an ordinary shell, not an agent")
    try:
        subprocess.run(
            ["tmux", "-S", socket, "send-keys", "-t", pane, "-l", prompt],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        subprocess.run(
            ["tmux", "-S", socket, "send-keys", "-t", pane, "Enter"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise WatcherError(f"tmux direct prompt failed; durable event preserved: {exc}") from exc
    return {"pane_id": pane, "pane_pid": int(fields[1]), "command": fields[3]}


def _herdr_runtime_snapshot(session: Optional[str]) -> List[Dict[str, Any]]:
    if session:
        base = ["herdr", "--session", session]
    elif os.environ.get("HERDR_ENV") == "1":
        base = ["herdr"]
    else:
        raise WatcherError("Herdr reconciliation requires the current Herdr session or --herdr-session")
    try:
        result = subprocess.run(
            base + ["agent", "list"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise WatcherError(f"Herdr reconciliation adapter unavailable: {exc}") from exc
    agents = payload.get("result", {}).get("agents", []) if isinstance(payload, dict) else []
    if not isinstance(agents, list):
        raise WatcherError("Herdr reconciliation response has an invalid agent list")
    return [agent for agent in agents if isinstance(agent, dict)]


def _herdr_runtime_state(
    record: Dict[str, Any], agents: List[Dict[str, Any]]
) -> Tuple[str, Dict[str, Any]]:
    thread_id = str(record["thread_id"])
    address = str(record["address"])
    matches: List[Dict[str, Any]] = []
    for agent in agents:
        agent_session = agent.get("agent_session")
        session_value = agent_session.get("value") if isinstance(agent_session, dict) else None
        identities = {str(value) for value in (session_value, agent.get("agent"), agent.get("name")) if value}
        if thread_id not in identities:
            continue
        if ":p" in address and agent.get("pane_id") != address:
            continue
        matches.append(agent)
    if not matches:
        return "closed", {"thread_id": thread_id, "address": address}
    if len(matches) != 1:
        raise WatcherError("Herdr reconciliation identity is ambiguous")
    runtime_status = str(matches[0].get("agent_status", "")).lower()
    evidence = {
        "thread_id": thread_id,
        "pane_id": matches[0].get("pane_id"),
        "runtime_status": runtime_status,
    }
    if runtime_status in HERDR_SETTLED_STATUSES:
        return "settled", evidence
    if runtime_status in HERDR_RUNNING_STATUSES:
        return "running", evidence
    raise WatcherError(f"Herdr reconciliation refused unknown runtime status: {runtime_status!r}")


def _tmux_runtime_snapshot(socket: Optional[str]) -> Dict[str, Dict[str, Any]]:
    if not socket:
        raise WatcherError("tmux reconciliation requires --tmux-socket")
    try:
        result = subprocess.run(
            [
                "tmux",
                "-S",
                socket,
                "list-panes",
                "-a",
                "-F",
                "#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_command}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise WatcherError(f"tmux reconciliation adapter unavailable: {exc}") from exc
    panes: Dict[str, Dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or not fields[0] or not fields[1].isdigit() or fields[2] not in {"0", "1"}:
            raise WatcherError("tmux reconciliation snapshot is invalid")
        panes[fields[0]] = {
            "pane_id": fields[0],
            "pane_pid": int(fields[1]),
            "pane_dead": fields[2] == "1",
            "command": fields[3],
        }
    return panes


def _tmux_runtime_state(
    record: Dict[str, Any], panes: Dict[str, Dict[str, Any]]
) -> Tuple[str, Dict[str, Any]]:
    pane = str(record["address"])
    evidence = panes.get(pane)
    if evidence is None or evidence.get("pane_dead") is True:
        return "closed", {"pane_id": pane}
    if evidence["command"] in {"sh", "bash", "zsh", "fish"}:
        return "settled", evidence
    return "running", evidence


def _reconciliation_event_id(status_path: Path, record: Dict[str, Any], condition: str) -> str:
    payload = "\0".join(
        (
            "champion_stalled",
            str(status_path.parent),
            str(record.get("updated_at")),
            str(record.get("status")),
            condition,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reconcile_runtime(
    records_root: Path,
    store: Store,
    shotcaller: str,
    adapter: str,
    herdr_session: Optional[str],
    tmux_socket: Optional[str],
    consecutive: int,
    deliver_event: bool = True,
) -> Dict[str, Any]:
    if consecutive < 2 or consecutive > 10:
        raise WatcherError("reconciliation consecutive observations must be between 2 and 10")
    _ensure_state_baseline(store, records_root, shotcaller)
    active_records = _active_records(records_root, shotcaller)
    runtime_records = [
        item
        for item in active_records
        if str(item[1].get("status", "")).lower() in RECONCILE_ACTIVE_STATUSES
    ]
    runtime_snapshot: Any = None
    if runtime_records:
        runtime_snapshot = (
            _herdr_runtime_snapshot(herdr_session)
            if adapter == "herdr"
            else _tmux_runtime_snapshot(tmux_socket)
        )
    observations: List[Tuple[Path, Dict[str, Any], str, Dict[str, Any]]] = []
    for status_path, record in active_records:
        durable_status = str(record.get("status", "")).lower()
        if durable_status not in RECONCILE_ACTIVE_STATUSES:
            observations.append((status_path, record, "terminal", {}))
            continue
        condition, evidence = (
            _herdr_runtime_state(record, runtime_snapshot)
            if adapter == "herdr"
            else _tmux_runtime_state(record, runtime_snapshot)
        )
        observations.append((status_path, record, condition, evidence))

    def reconcile(state: Dict[str, Any]) -> Dict[str, Any]:
        reconciliation = state.get("reconciliation", {})
        pending = state.get("pending_events", {})
        delivered = state.get("delivered_events", {})
        if not isinstance(reconciliation, dict) or not isinstance(pending, dict) or not isinstance(delivered, dict):
            raise WatcherError("watcher reconciliation state is invalid")
        queued: List[str] = []
        results: List[Dict[str, Any]] = []
        observed_keys = set()
        for status_path, record, condition, evidence in observations:
            key = str(status_path.parent)
            observed_keys.add(key)
            if condition in {"running", "terminal"}:
                reconciliation.pop(key, None)
                results.append({"record": key, "runtime": condition, "event": None})
                continue
            previous = reconciliation.get(key, {})
            count = int(previous.get("count", 0)) + 1 if previous.get("condition") == condition else 1
            observation = {
                "condition": condition,
                "count": count,
                "record_updated_at": record.get("updated_at"),
                "evidence": evidence,
            }
            reconciliation[key] = observation
            event_id = _reconciliation_event_id(status_path, record, condition)
            event_queued = False
            if count >= consecutive and event_id not in delivered:
                event = {
                    "event": "champion_stalled",
                    "event_id": event_id,
                    "record": key,
                    "source_path": str(status_path),
                    "source_offset": 0,
                    "callsign": record.get("callsign"),
                    "shotcaller": record.get("shotcaller"),
                    "status": "champion_stalled",
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "update": (
                        f"Runtime is stably {condition} while durable status remains "
                        f"{record.get('status')}; inspect the exact Champion endpoint and record."
                    ),
                    "runtime_evidence": evidence,
                }
                pending[event_id] = event
                state["last_event_id"] = event_id
                queued.append(event_id)
                event_queued = True
            results.append(
                {
                    "record": key,
                    "runtime": condition,
                    "observation": count,
                    "event": event_id if event_queued else None,
                }
            )
        for key in list(reconciliation):
            if key not in observed_keys:
                del reconciliation[key]
        state["reconciliation"] = reconciliation
        state["pending_events"] = pending
        return {"observations": results, "queued": queued}

    result = store.mutate(reconcile)
    if result["queued"] and deliver_event:
        result["delivery"] = deliver_transition(
            records_root, store, shotcaller, adapter, herdr_session, tmux_socket
        )
    else:
        result["delivery"] = None
    return result


def deliver_transition(
    records_root: Path,
    store: Store,
    shotcaller: str,
    adapter: str,
    herdr_session: Optional[str],
    tmux_socket: Optional[str],
) -> Dict[str, Any]:
    shotcaller_record = _load_status(records_root / shotcaller / "status.json")
    if str(shotcaller_record.get("role", "")).lower() != "shotcaller":
        raise WatcherError("direct delivery target is not a Shotcaller")

    def deliver(state: Dict[str, Any]) -> Dict[str, Any]:
        _, event, saw_transition = _scan_state(state, records_root, shotcaller)
        delivered = state.get("delivered_events", {})
        if not isinstance(delivered, dict):
            raise WatcherError("watcher delivery state is invalid")
        superseded: List[str] = []
        while event is not None and event.get("event") in {"champion-update", "worker-update"}:
            record_dir = _direct_event_record_dir(event, records_root, shotcaller)
            with _record_lock(record_dir):
                if _event_matches_latest_unlocked(event, record_dir):
                    break
            event_id = str(event["event_id"])
            _mark_state_delivered(state, event_id, "superseded")
            superseded.append(event_id)
            event = _next_pending_event(state)
        if event is None:
            if superseded:
                return {
                    "delivered": False,
                    "reason": "superseded",
                    "event_ids": superseded,
                }
            last_event_id = state.get("last_event_id")
            if isinstance(last_event_id, str) and last_event_id in delivered:
                return {
                    "delivered": False,
                    "reason": "duplicate",
                    "event_id": last_event_id,
                    "channel": delivered[last_event_id].get("channel"),
                }
            return {
                "delivered": False,
                "reason": "non-material" if saw_transition else "no-pending-transition",
            }
        def prompt_candidate() -> Dict[str, Any]:
            event_id = str(event["event_id"])
            if not state.get("enabled", True):
                return {
                    "delivered": False,
                    "reason": "disabled",
                    "event_id": event_id,
                    "preserved": True,
                }
            if _live_wait(state):
                return {
                    "delivered": False,
                    "reason": "watcher-active",
                    "event_id": event_id,
                    "preserved": True,
                }
            prompt = _direct_prompt_text(event)
            endpoint = (
                _herdr_direct_prompt(shotcaller_record, herdr_session, prompt)
                if adapter == "herdr"
                else _tmux_direct_prompt(shotcaller_record, tmux_socket, prompt)
            )
            if endpoint is None:
                return {
                    "delivered": False,
                    "reason": "shotcaller-closed",
                    "event_id": event_id,
                    "preserved": True,
                }
            if not _mark_state_delivered(state, event_id, f"direct-{adapter}"):
                return {"delivered": False, "reason": "duplicate", "event_id": event_id}
            return {
                "delivered": True,
                "reason": "direct-prompt",
                "channel": adapter,
                "event_id": event_id,
                "endpoint": endpoint,
            }

        if event.get("event") in {"champion-update", "worker-update"}:
            record_dir = _direct_event_record_dir(event, records_root, shotcaller)
            with _record_lock(record_dir):
                if not _event_matches_latest_unlocked(event, record_dir):
                    event_id = str(event["event_id"])
                    _mark_state_delivered(state, event_id, "superseded")
                    return {"delivered": False, "reason": "superseded", "event_ids": [event_id]}
                return prompt_candidate()
        return prompt_candidate()

    return store.mutate(deliver)


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SENSITIVE_KEY_PATTERN = re.compile(
    r"(^|_)(secret|cookie|credential|password|token|api_key|access_key|private_key)($|_)",
    re.IGNORECASE,
)
SENSITIVE_CONTENT_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"(?i)(api[_-]?key|access[_-]?token|password|cookie)\s*[:=]"),
    re.compile(rb"(?i)\b(bearer\s+[A-Za-z0-9._~-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{8,})"),
)
MAX_ARCHIVE_EVIDENCE_BYTES = 256 * 1024


def _require_object(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise WatcherError(f"teardown refused: {name} object missing")
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WatcherError(f"teardown refused: {name} missing")
    return value


def _require_https_url(value: Any, name: str) -> str:
    text = _require_text(value, name)
    if not text.startswith("https://"):
        raise WatcherError(f"teardown refused: {name} must be an HTTPS owner-source URL")
    return text


def _sha256_file(path: Path, name: str) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise WatcherError(f"teardown refused: {name} must be an absolute regular file")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise WatcherError(f"teardown refused: cannot read {name}: {exc}") from exc
    return digest.hexdigest()


def _verify_local_release(
    release: Dict[str, Any], merge_commit: str
) -> Dict[str, Any]:
    if release.get("type") != "local_install":
        raise WatcherError("teardown refused: unsupported release type")
    revision = _require_text(release.get("revision"), "release.revision")
    required_revision = _require_text(
        release.get("required_revision"), "release.required_revision"
    )
    if revision != required_revision or not SHA_PATTERN.fullmatch(revision):
        raise WatcherError("teardown refused: local release revision conflicts")
    if revision != merge_commit:
        raise WatcherError("teardown refused: local release revision is not the exact merge")
    source = _require_object(release.get("source"), "release.source")
    installed = _require_object(release.get("installed"), "release.installed")
    source_path = Path(_require_text(source.get("path"), "release.source.path")).expanduser()
    installed_path = Path(_require_text(installed.get("path"), "release.installed.path")).expanduser()
    source_sha = _require_text(source.get("sha256"), "release.source.sha256").lower()
    installed_sha = _require_text(installed.get("sha256"), "release.installed.sha256").lower()
    if not SHA256_PATTERN.fullmatch(source_sha) or not SHA256_PATTERN.fullmatch(installed_sha):
        raise WatcherError("teardown refused: local release hashes are invalid")
    if release.get("parity") is not True:
        raise WatcherError("teardown refused: local install parity proof missing")
    if _sha256_file(source_path, "release.source.path") != source_sha:
        raise WatcherError("teardown refused: source install hash changed")
    if _sha256_file(installed_path, "release.installed.path") != installed_sha:
        raise WatcherError("teardown refused: installed watcher hash changed")
    if source_sha != installed_sha:
        raise WatcherError("teardown refused: source and installed watcher bytes differ")
    _require_text(release.get("receipt"), "release.receipt")
    smoke = release.get("smoke")
    if smoke is None:
        smoke = release.get("post_install_smoke")
    smoke = _require_object(smoke, "release.smoke")
    if smoke.get("passed") is not True:
        raise WatcherError("teardown refused: local install smoke is not green")
    _require_text(smoke.get("receipt"), "release.smoke.receipt")
    return {
        "type": "local_install",
        "revision": revision,
        "source": str(source_path),
        "installed": str(installed_path),
        "sha256": source_sha,
        "smoke_receipt": smoke["receipt"],
    }


def _safe_changed_files(value: Any, name: str) -> List[str]:
    if not isinstance(value, list) or not value:
        raise WatcherError(f"teardown refused: {name} must be a non-empty path list")
    paths: List[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("path")
        if not isinstance(item, str) or not item or item.startswith("/"):
            raise WatcherError(f"teardown refused: {name} contains an invalid path")
        path = Path(item)
        if path.is_absolute() or ".." in path.parts or "\0" in item:
            raise WatcherError(f"teardown refused: {name} contains an unsafe path")
        if item not in paths:
            paths.append(item)
    return paths


def _verify_merge_integration(
    repository: Path,
    tested_head: str,
    merge_commit: str,
    merge: Dict[str, Any],
    pr: Dict[str, Any],
) -> None:
    try:
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "merge-base",
                "--is-ancestor",
                tested_head,
                merge_commit,
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WatcherError("teardown refused: tested head integration proof failed") from exc
    if ancestry.returncode == 0:
        return
    changed = merge.get("changed_files")
    if changed is None:
        changed = _git(repository, "diff", "--name-only", f"{merge_commit}^", merge_commit).splitlines()
        if not changed:
            raise WatcherError("teardown refused: exact squash merge changed-file proof is empty")
    paths = _safe_changed_files(changed, "merge.changed_files")
    pr_changed = pr.get("changed_files")
    if pr_changed is not None and _safe_changed_files(pr_changed, "pr.changed_files") != paths:
        raise WatcherError("teardown refused: PR and merge changed-file proofs conflict")
    for path in paths:
        result = subprocess.run(
            ["git", "-C", str(repository), "diff", "--quiet", tested_head, merge_commit, "--", path],
            check=False,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise WatcherError(
                f"teardown refused: changed-file integration proof failed for {path}"
            )


def _reject_sensitive_keys(value: Any, path: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if SENSITIVE_KEY_PATTERN.search(str(key)):
                raise WatcherError(f"teardown refused: sensitive manifest key {path}.{key}")
            _reject_sensitive_keys(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive_keys(nested, f"{path}[{index}]")


def _git(repository: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise WatcherError(f"teardown refused: Git proof failed for {' '.join(args)}: {exc}") from exc
    return result.stdout.strip()


def inspect_process(pid: int) -> Dict[str, Any]:
    if pid <= 1:
        raise WatcherError("task resource PID is unsafe")
    try:
        start = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        state = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise WatcherError(f"task resource process is stale or unavailable: {pid}") from exc
    if not start or not state or state.startswith("Z"):
        raise WatcherError(f"task resource process is stale or exited: {pid}")
    return {"pid": pid, "process_start": start, "state": state}


def _verify_task_resources(manifest: Dict[str, Any]) -> Dict[str, Any]:
    resources = manifest.get("task_resources", [])
    if not isinstance(resources, list):
        raise WatcherError("teardown refused: task_resources must be a list")
    if not resources:
        return {"registry": None, "resources": []}
    registry_path = Path(_require_text(manifest.get("resource_registry"), "resource_registry"))
    if not registry_path.is_absolute():
        raise WatcherError("teardown refused: resource registry path must be absolute")
    registry = _load_json(registry_path)
    registered = _require_object(registry.get("resources"), "resource registry resources")
    target = _require_object(manifest.get("target"), "target")
    task_id = str(manifest["task_id"])
    owner = str(target["callsign"])
    validated: List[Dict[str, Any]] = []
    for resource in resources:
        resource = _require_object(resource, "task resource")
        resource_id = _require_text(resource.get("resource_id"), "task resource.resource_id")
        if resource.get("task_id") != task_id or resource.get("owner") != owner:
            raise WatcherError("teardown refused: task resource ownership conflicts")
        _require_text(resource.get("endpoint"), "task resource.endpoint")
        _require_text(resource.get("generation"), "task resource.generation")
        kind = resource.get("kind")
        if kind == "process":
            assignment = registered.get(resource_id)
            if not isinstance(assignment, dict):
                raise WatcherError("teardown refused: task resource registry record is stale")
            keys = ("pid", "task_id", "owner", "endpoint", "generation", "process_start")
            if any(assignment.get(key) != resource.get(key) for key in keys):
                raise WatcherError("teardown refused: task resource registry ownership conflicts")
            pid = resource.get("pid")
            if not isinstance(pid, int):
                raise WatcherError("teardown refused: task resource PID missing")
            process = inspect_process(pid)
            if process["process_start"] != resource.get("process_start"):
                raise WatcherError("teardown refused: task resource PID generation conflicts")
            validated.append(dict(resource))
        elif kind == "shared-agent-chrome":
            chrome = _require_object(registry.get("shared_agent_chrome"), "shared Agent Chrome lease")
            owners = chrome.get("owners")
            if not isinstance(owners, list):
                raise WatcherError("teardown refused: shared Agent Chrome owner registry is invalid")
            action = resource.get("action")
            if action == "restart":
                if owners:
                    raise WatcherError("teardown refused: shared Agent Chrome restart has active lease owners")
                raise WatcherError("teardown refused: shared Agent Chrome restart requires separate explicit authority")
            if action != "release-lease":
                raise WatcherError("teardown refused: shared Agent Chrome action is unsupported")
            expected = {
                "task_id": task_id,
                "owner": owner,
                "generation": resource["generation"],
            }
            if expected not in owners:
                raise WatcherError("teardown refused: shared Agent Chrome lease ownership conflicts")
            validated.append(dict(resource))
        else:
            raise WatcherError("teardown refused: task resource kind is unsupported")
    return {"registry": registry_path, "resources": validated}


def _process_has_exited(pid: int) -> bool:
    try:
        state = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return not state or state.startswith("Z")


def cleanup_task_resources(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    registry_path = context.get("registry")
    resources = context.get("resources", [])
    if registry_path is None:
        return []
    cleaned: List[Dict[str, Any]] = []
    for resource in resources:
        if resource["kind"] != "process":
            continue
        pid = int(resource["pid"])
        current = inspect_process(pid)
        if current["process_start"] != resource["process_start"]:
            raise WatcherError("teardown refused: task resource PID was reused before cleanup")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            raise WatcherError(f"task resource graceful stop failed: {pid}: {exc}") from exc
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _process_has_exited(pid):
            time.sleep(0.05)
        if not _process_has_exited(pid):
            raise WatcherError(f"task resource did not exit after SIGTERM: {pid}")
        cleaned.append({"resource_id": resource["resource_id"], "pid": pid, "exit_verified": True})

    def release(value: Dict[str, Any]) -> None:
        registered = _require_object(value.get("resources"), "resource registry resources")
        chrome = value.get("shared_agent_chrome")
        for resource in resources:
            if resource["kind"] == "process":
                assignment = registered.get(resource["resource_id"])
                keys = ("pid", "task_id", "owner", "endpoint", "generation", "process_start")
                if not isinstance(assignment, dict) or any(
                    assignment.get(key) != resource.get(key) for key in keys
                ):
                    raise WatcherError("task resource registry changed before release")
                del registered[resource["resource_id"]]
            else:
                chrome_record = _require_object(chrome, "shared Agent Chrome lease")
                owners = chrome_record.get("owners")
                expected = {
                    "task_id": resource["task_id"],
                    "owner": resource["owner"],
                    "generation": resource["generation"],
                }
                if not isinstance(owners, list) or expected not in owners:
                    raise WatcherError("shared Agent Chrome lease changed before release")
                owners.remove(expected)
                cleaned.append({"resource_id": resource["resource_id"], "lease_released": True})

    _mutate_json(Path(registry_path), release)
    return cleaned


def _verify_registered_worktree(
    repository: Path, worktree: Path, branch: str, tested_head: str
) -> None:
    registered = False
    for block in _git(repository, "worktree", "list", "--porcelain").split("\n\n"):
        fields: Dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        registered_path = fields.get("worktree")
        if not registered_path or Path(registered_path).resolve() != worktree.resolve():
            continue
        registered = True
        if fields.get("HEAD") != tested_head or fields.get("branch") != f"refs/heads/{branch}":
            raise WatcherError("teardown refused: registered worktree head or branch conflicts")
    if not registered:
        raise WatcherError("teardown refused: exact worktree is not registered")


def _verify_git_proof(manifest: Dict[str, Any]) -> None:
    repository = Path(_require_text(manifest.get("repository_path"), "repository_path"))
    worktree = Path(_require_text(manifest.get("worktree"), "worktree"))
    branch = _require_text(manifest.get("branch"), "branch")
    tested_head = _require_text(manifest.get("tested_head"), "tested_head")
    published_ref = _require_text(manifest.get("published_ref"), "published_ref")
    if not repository.is_absolute() or not worktree.is_absolute():
        raise WatcherError("teardown refused: repository and worktree paths must be absolute")
    if repository.resolve() == worktree.resolve() or branch in {"main", "master"}:
        raise WatcherError("teardown refused: cleanup target is unsafe")
    if not SHA_PATTERN.fullmatch(tested_head):
        raise WatcherError("teardown refused: tested_head is not an exact commit SHA")
    if manifest.get("clean_state") is not True or manifest.get("no_unpublished_commits") is not True:
        raise WatcherError("teardown refused: clean/no-unpublished proof missing")
    if Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree.resolve():
        raise WatcherError("teardown refused: worktree path conflicts with Git")
    if _git(worktree, "status", "--porcelain"):
        raise WatcherError("teardown refused: worktree is dirty")
    if _git(worktree, "rev-parse", "HEAD") != tested_head:
        raise WatcherError("teardown refused: tested head conflicts with worktree HEAD")
    if _git(repository, "rev-parse", f"refs/heads/{branch}") != tested_head:
        raise WatcherError("teardown refused: local branch conflicts with tested head")
    if _git(repository, "rev-parse", published_ref) != tested_head:
        raise WatcherError("teardown refused: published ref conflicts with tested head")
    _verify_registered_worktree(repository, worktree, branch, tested_head)


def verify_teardown_manifest(
    manifest: Dict[str, Any], records_root: Path
) -> Dict[str, Any]:
    _reject_sensitive_keys(manifest)
    if any(pattern.search(_json_dump(manifest).encode("utf-8")) for pattern in SENSITIVE_CONTENT_PATTERNS):
        raise WatcherError("teardown refused: manifest may contain secret-like content")
    if manifest.get("schema") != 2:
        raise WatcherError("teardown refused: manifest schema must be 2")
    task_id = _require_text(manifest.get("task_id"), "task_id")
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise WatcherError("teardown refused: task_id is not an immutable safe identifier")
    generated_by = _require_object(manifest.get("generated_by"), "generated_by")
    target = _require_object(manifest.get("target"), "target")
    if generated_by.get("role") != "shotcaller":
        raise WatcherError("teardown refused: manifest was not generated by a Shotcaller")
    if target.get("role") != "champion" or target.get("persistent") is True:
        raise WatcherError("teardown refused: Shotcallers and persistent supervisors are ineligible")
    shotcaller = _require_text(target.get("shotcaller"), "target.shotcaller")
    callsign = _require_text(target.get("callsign"), "target.callsign")
    if generated_by.get("callsign") != shotcaller:
        raise WatcherError("teardown refused: generating Shotcaller conflicts with target owner")
    if not SAFE_COMPONENT_PATTERN.fullmatch(shotcaller) or not SAFE_COMPONENT_PATTERN.fullmatch(callsign):
        raise WatcherError("teardown refused: archive identity is not path-safe")
    _require_text(generated_by.get("thread_id"), "generated_by.thread_id")
    target_thread = _require_text(target.get("thread_id"), "target.thread_id")
    target_address = _require_text(target.get("address"), "target.address")
    record_dir = Path(_require_text(target.get("record_dir"), "target.record_dir"))
    expected_record_dir = records_root / shotcaller / "champions" / callsign
    if record_dir != expected_record_dir:
        raise WatcherError("teardown refused: target record path conflicts with Roster identity")
    status_path = record_dir / "status.json"
    updates_path = record_dir / "updates.jsonl"
    status = _validate_record_pair(status_path)
    durable_identity = _champion_identity(status, str(status_path))
    manifest_identity = _require_object(manifest.get("durable_identity"), "durable_identity")
    if manifest_identity != durable_identity:
        raise WatcherError("teardown refused: durable Champion identity conflicts with status")
    disposition = manifest.get("disposition")
    status_value = str(status.get("status", "")).lower()
    if disposition == "landed" and status_value != READY_TO_LAND_STATUS:
        raise WatcherError("teardown refused: landed Champion is not ready_to_land")
    if disposition == "rejected" and status_value not in {
        READY_TO_LAND_STATUS,
        "blocked",
        "cancelled",
        "canceled",
        "failed",
    }:
        raise WatcherError("teardown refused: rejected Champion status is not terminal")
    for key, expected in (
        ("callsign", callsign),
        ("shotcaller", shotcaller),
        ("thread_id", target_thread),
        ("address", target_address),
        ("task_id", task_id),
    ):
        if status.get(key) != expected:
            raise WatcherError(f"teardown refused: status.{key} conflicts with manifest")
    if not updates_path.is_file():
        raise WatcherError("teardown refused: updates.jsonl missing")

    identity = _require_object(manifest.get("identity"), "identity")
    expected_identity = _require_object(manifest.get("expected_identity"), "expected_identity")
    if identity != expected_identity or identity.get("pane_id") != target_address:
        raise WatcherError("teardown refused: exact endpoint identity conflicts")
    adapter = _require_text(manifest.get("adapter"), "adapter")
    if adapter not in {"herdr", "tmux"}:
        raise WatcherError("teardown refused: unsupported endpoint adapter")
    if adapter != durable_identity["backend"]:
        raise WatcherError("teardown refused: endpoint adapter conflicts with durable Champion identity")
    _require_text(identity.get("pane_id"), "identity.pane_id")
    if identity.get("thread_id") != durable_identity["thread_id"]:
        raise WatcherError("teardown refused: endpoint thread conflicts with durable Champion identity")
    if adapter == "tmux":
        _require_text(identity.get("socket"), "identity.socket")
    else:
        _require_text(identity.get("session"), "identity.session")
        if bool(identity.get("source")) != bool(identity.get("agent")):
            raise WatcherError("teardown refused: Herdr source/agent identity is incomplete")
    if manifest.get("grace_elapsed") is not True or manifest.get("terminal_or_idle") is not True:
        raise WatcherError("teardown refused: endpoint grace or terminal proof missing")
    if manifest.get("pending_decision_clear") is not True:
        raise WatcherError("teardown refused: pending decision proof missing")

    issue = _require_object(manifest.get("issue"), "issue")
    if not isinstance(issue.get("number"), int) or issue["number"] <= 0:
        raise WatcherError("teardown refused: issue number missing")
    issue_url = _require_https_url(issue.get("url"), "issue.url")
    repository_url = _require_https_url(manifest.get("repository_url"), "repository_url").rstrip("/")
    if issue_url != f"{repository_url}/issues/{issue['number']}":
        raise WatcherError("teardown refused: issue owner-source URL conflicts")
    if (
        durable_identity["repository"] != repository_url
        or durable_identity["issue"] != issue["number"]
        or durable_identity["branch"] != manifest.get("branch")
        or durable_identity["worktree"] != manifest.get("worktree")
    ):
        raise WatcherError("teardown refused: Git task proof conflicts with durable Champion identity")
    _verify_git_proof(manifest)
    if disposition == "landed":
        pr = _require_object(manifest.get("pr"), "pr")
        merge = _require_object(manifest.get("merge"), "merge")
        tested_head = str(manifest["tested_head"])
        if not isinstance(pr.get("number"), int) or pr["number"] <= 0:
            raise WatcherError("teardown refused: PR number missing")
        if pr.get("head") != tested_head or pr.get("green") is not True:
            raise WatcherError("teardown refused: red or conflicting PR proof")
        pr_url = _require_https_url(pr.get("url"), "pr.url")
        if pr_url != f"{repository_url}/pull/{pr['number']}":
            raise WatcherError("teardown refused: PR owner-source URL conflicts")
        _require_https_url(pr.get("ci_url"), "pr.ci_url")
        _require_text(pr.get("ci_receipt"), "pr.ci_receipt")
        merge_commit = _require_text(merge.get("commit"), "merge.commit")
        if merge.get("head") != tested_head or not SHA_PATTERN.fullmatch(merge_commit):
            raise WatcherError("teardown refused: merge proof conflicts with tested head")
        merge_url = _require_https_url(merge.get("url"), "merge.url")
        if merge_url != f"{repository_url}/commit/{merge_commit}":
            raise WatcherError("teardown refused: merge owner-source URL conflicts")
        repository = Path(str(manifest["repository_path"]))
        if _git(repository, "rev-parse", merge_commit) != merge_commit:
            raise WatcherError("teardown refused: exact merge commit is unavailable")
        _verify_merge_integration(repository, tested_head, merge_commit, merge, pr)
        release = manifest.get("release")
        if isinstance(release, dict) and release.get("type") == "local_install":
            release_context = _verify_local_release(release, merge_commit)
        else:
            deployment = _require_object(manifest.get("deployment"), "deployment")
            smoke = _require_object(manifest.get("post_deploy_smoke"), "post_deploy_smoke")
            deployed_revision = _require_text(deployment.get("revision"), "deployment.revision")
            required_revision = _require_text(
                deployment.get("required_revision"), "deployment.required_revision"
            )
            if deployed_revision != required_revision:
                raise WatcherError("teardown refused: deployed revision conflicts with required revision")
            _require_https_url(deployment.get("url"), "deployment.url")
            _require_text(deployment.get("receipt"), "deployment.receipt")
            if smoke.get("passed") is not True:
                raise WatcherError("teardown refused: post-deploy smoke is not green")
            _require_https_url(smoke.get("url"), "post_deploy_smoke.url")
            _require_text(smoke.get("receipt"), "post_deploy_smoke.receipt")
            release_context = {
                "type": "web_deployment",
                "revision": required_revision,
                "smoke_receipt": smoke["receipt"],
            }
    elif disposition == "rejected":
        rejection = _require_object(manifest.get("rejection"), "rejection")
        if rejection.get("explicit") is not True or rejection.get("authorized_by") != "user":
            raise WatcherError("teardown refused: rejected work lacks exact user authority")
        rejection_url = _require_https_url(rejection.get("url"), "rejection.url")
        if not rejection_url.startswith(issue_url + "#"):
            raise WatcherError("teardown refused: rejection owner-source URL conflicts")
        _require_text(rejection.get("receipt"), "rejection.receipt")
    else:
        raise WatcherError("teardown refused: disposition must be landed or rejected")

    landed_at = _require_text(manifest.get("landed_at"), "landed_at")
    try:
        archive_date = datetime.fromisoformat(landed_at.replace("Z", "+00:00")).date().isoformat()
    except ValueError as exc:
        raise WatcherError("teardown refused: landed_at is not RFC3339") from exc
    evidence_files = manifest.get("small_evidence_files", [])
    if not isinstance(evidence_files, list) or not all(isinstance(path, str) for path in evidence_files):
        raise WatcherError("teardown refused: small_evidence_files must be a path list")
    resource_context = _verify_task_resources(manifest)
    _validate_visible_callsign(manifest.get("callsign_release"), target)
    return {
        "adapter": adapter,
        "archive_date": archive_date,
        "branch": str(manifest["branch"]),
        "callsign": callsign,
        "durable_identity": durable_identity,
        "evidence_files": [Path(path) for path in evidence_files],
        "record_dir": record_dir,
        "resource_context": resource_context,
        "repository": Path(str(manifest["repository_path"])),
        "shotcaller": shotcaller,
        "status_path": status_path,
        "task_id": task_id,
        "updates_path": updates_path,
        "worktree": Path(str(manifest["worktree"])),
    }


def _validate_small_evidence(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise WatcherError(f"teardown refused: archive evidence is not a regular file: {path}")
    size = path.stat().st_size
    if size > MAX_ARCHIVE_EVIDENCE_BYTES:
        raise WatcherError(f"teardown refused: archive evidence exceeds 256 KiB: {path}")
    if path.suffix.lower() not in {".json", ".jsonl", ".txt", ".log", ".md"}:
        raise WatcherError(f"teardown refused: archive evidence type is not allowed: {path}")
    data = path.read_bytes()
    if b"\0" in data or any(pattern.search(data) for pattern in SENSITIVE_CONTENT_PATTERNS):
        raise WatcherError(f"teardown refused: archive evidence may contain secrets: {path}")
    return data


def _prepare_archive(
    manifest: Dict[str, Any],
    manifest_path: Path,
    context: Dict[str, Any],
    archive_root: Path,
) -> Tuple[Path, Path]:
    destination = (
        archive_root
        / context["shotcaller"]
        / "archive"
        / context["archive_date"]
        / context["callsign"]
        / context["task_id"]
    )
    if destination.exists():
        raise WatcherError(f"teardown refused: archive collision for immutable task-id: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{context['task_id']}.", dir=destination.parent))
    try:
        shutil.copy2(context["status_path"], stage / "status.json")
        shutil.copy2(context["updates_path"], stage / "updates.jsonl")
        task = {
            "task_id": context["task_id"],
            "task": _load_status(context["status_path"]).get("task"),
            "issue": manifest["issue"],
            "callsign": context["callsign"],
            "shotcaller": context["shotcaller"],
            "branch": context["branch"],
            "tested_head": manifest["tested_head"],
            "disposition": manifest["disposition"],
            "durable_identity": context["durable_identity"],
        }
        _write_json_atomic(stage / "task.json", task)
        _write_json_atomic(stage / "teardown-manifest.json", manifest)
        evidence_dir = stage / "evidence"
        names: set[str] = set()
        for source in context["evidence_files"]:
            data = _validate_small_evidence(source)
            if source.name in names:
                raise WatcherError(f"teardown refused: archive evidence name collision: {source.name}")
            names.add(source.name)
            evidence_dir.mkdir(exist_ok=True)
            (evidence_dir / source.name).write_bytes(data)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage, destination


def _cleanup_plan(manifest: Dict[str, Any]) -> List[List[str]]:
    repository = str(manifest["repository_path"])
    worktree = str(manifest["worktree"])
    branch = str(manifest["branch"])
    return [
        ["git", "-C", repository, "worktree", "remove", worktree],
        ["git", "-C", repository, "branch", "-d", branch],
    ]


def _release_visible_callsign(specification: Any) -> Optional[str]:
    if specification is None:
        return None
    if not isinstance(specification, dict):
        raise WatcherError("callsign release is not an object")
    path = Path(str(specification.get("pool", ""))).expanduser()
    callsign = str(specification.get("callsign", ""))
    role = str(specification.get("role", ""))
    if not path.is_absolute() or role not in {"champion", "shotcaller"} or not callsign:
        raise WatcherError("callsign release is incomplete")

    def release(value: Dict[str, Any]) -> str:
        available = value.get("available")
        in_use = value.get("in_use")
        if not isinstance(available, dict) or not isinstance(in_use, dict):
            raise WatcherError("callsign pool schema is invalid")
        assignment = in_use.get(callsign)
        if not isinstance(assignment, dict) or assignment.get("role") != role:
            raise WatcherError("callsign release identity does not match")
        role_pool = available.get(role)
        if not isinstance(role_pool, list):
            raise WatcherError("callsign available pool is invalid")
        del in_use[callsign]
        if callsign not in role_pool:
            role_pool.append(callsign)
            role_pool.sort()
        return callsign

    return _mutate_json(path, release)


def _validate_visible_callsign(specification: Any, target: Dict[str, Any]) -> None:
    specification = _require_object(specification, "callsign_release")
    path = Path(_require_text(specification.get("pool"), "callsign_release.pool")).expanduser()
    callsign = _require_text(specification.get("callsign"), "callsign_release.callsign")
    if not path.is_absolute() or callsign != target.get("callsign") or specification.get("role") != "champion":
        raise WatcherError("teardown refused: callsign release conflicts with Champion identity")
    pool = _load_json(path)
    in_use = _require_object(pool.get("in_use"), "callsign pool in_use")
    assignment = in_use.get(callsign)
    if not isinstance(assignment, dict) or assignment.get("role") != "champion":
        raise WatcherError("teardown refused: callsign is not assigned as a Champion")
    if assignment.get("record") != target.get("record_dir"):
        raise WatcherError("teardown refused: callsign record conflicts with target")


def teardown_plan(adapter: str, manifest: Dict[str, Any]) -> List[List[str]]:
    identity = manifest["identity"]
    if adapter == "herdr":
        session = identity["session"]
        pane = identity["pane_id"]
        commands: List[List[str]] = []
        source = identity.get("source")
        agent = identity.get("agent")
        if source and agent:
            commands.append(
                ["herdr", "--session", session, "pane", "release-agent", pane, "--source", source, "--agent", agent]
            )
        commands.append(["herdr", "--session", session, "pane", "close", pane])
        return commands + _cleanup_plan(manifest)
    if adapter == "tmux":
        socket = identity["socket"]
        pane = identity["pane_id"]
        return [
            ["tmux", "-S", socket, "send-keys", "-t", pane, "C-c"],
            ["tmux", "-S", socket, "kill-pane", "-t", pane],
        ] + _cleanup_plan(manifest)
    raise WatcherError(f"unknown teardown adapter: {adapter}")


def _verify_live_endpoint(adapter: str, identity: Dict[str, Any]) -> None:
    if adapter == "tmux":
        command = [
            "tmux",
            "-S",
            str(identity["socket"]),
            "display-message",
            "-p",
            "-t",
            str(identity["pane_id"]),
            "#{pane_id}",
        ]
        expected = str(identity["pane_id"])
    else:
        command = ["herdr", "--session", str(identity["session"]), "agent", "list"]
        expected = None
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise WatcherError("teardown refused: exact endpoint is unavailable") from exc
    if expected is not None and result.stdout.strip() != expected:
        raise WatcherError("teardown refused: live endpoint identity conflicts")
    if adapter == "herdr":
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise WatcherError("teardown refused: live Herdr identity is unreadable") from exc
        agents = payload.get("result", {}).get("agents", []) if isinstance(payload, dict) else []
        matches = []
        for agent in agents if isinstance(agents, list) else []:
            if not isinstance(agent, dict) or agent.get("pane_id") != identity["pane_id"]:
                continue
            session = agent.get("agent_session")
            session_id = session.get("value") if isinstance(session, dict) else None
            if session_id == identity["thread_id"]:
                matches.append(agent)
        if len(matches) != 1:
            raise WatcherError("teardown refused: live Herdr endpoint identity conflicts")


def execute_teardown(
    adapter: str,
    manifest: Dict[str, Any],
    manifest_path: Path,
    records_root: Path,
    archive_root: Path,
    execute: bool,
) -> Dict[str, Any]:
    context = verify_teardown_manifest(manifest, records_root)
    if adapter != context["adapter"]:
        raise WatcherError("teardown refused: CLI adapter conflicts with manifest")
    commands = teardown_plan(adapter, manifest)
    destination = (
        archive_root
        / context["shotcaller"]
        / "archive"
        / context["archive_date"]
        / context["callsign"]
        / context["task_id"]
    )
    if not execute:
        if destination.exists():
            raise WatcherError(f"teardown refused: archive collision for immutable task-id: {destination}")
        return {"verified": True, "dry_run": True, "commands": commands, "archive": str(destination)}
    _verify_live_endpoint(adapter, manifest["identity"])
    transition_record(
        records_root,
        context["record_dir"],
        "completed",
        "Teardown proof verified; archiving the completed task record.",
        "Archive completed record and release the verified callsign.",
        None,
        datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    stage, destination = _prepare_archive(manifest, manifest_path, context, archive_root)
    try:
        cleaned_resources = cleanup_task_resources(context["resource_context"])
    except WatcherError:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    for command in commands:
        try:
            subprocess.run(command, check=True, timeout=15, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            shutil.rmtree(stage, ignore_errors=True)
            raise WatcherError(f"teardown action failed after verification: {exc}") from exc
    os.replace(stage, destination)
    context["status_path"].unlink()
    context["updates_path"].unlink()
    try:
        context["record_dir"].rmdir()
    except OSError:
        pass
    released_callsign = _release_visible_callsign(manifest.get("callsign_release"))
    archived_manifest = _load_json(destination / "teardown-manifest.json")
    archived_manifest["teardown_result"] = {
        "commands": commands,
        "released_callsign": released_callsign,
        "remote_branch_deleted": False,
        "task_resources": cleaned_resources,
    }
    _write_json_atomic(destination / "teardown-manifest.json", archived_manifest)
    return {
        "verified": True,
        "executed": True,
        "archive": str(destination),
        "commands": commands,
        "released_callsign": released_callsign,
        "task_resources": cleaned_resources,
    }


def supervise_event(
    event: Dict[str, Any]
) -> Dict[str, Any]:
    if str(event.get("status", "")).lower() != READY_TO_LAND_STATUS:
        return event
    ready = dict(event)
    ready["event"] = "champion-ready-to-land"
    ready["teardown_eligible"] = False
    ready["next"] = "Shotcaller verifies exact head/authority, lands, deploys, records smoke, then supplies the teardown manifest."
    return ready


HIDDEN_WORKER_RELEASE_GATES = (
    "terminal_or_idle",
    "result_delivered",
    "unpublished_work_reconciled",
)


def allocate_hidden_worker(
    pool_path: Path,
    owner: str,
    worker_id: str,
    model: str,
    effort: str,
    reason: str,
) -> Dict[str, Any]:
    if not all((owner, worker_id, model, effort, reason)):
        raise WatcherError("hidden-worker assignment fields cannot be empty")

    def allocate(value: Dict[str, Any]) -> Dict[str, Any]:
        available = value.get("available")
        active = value.get("active")
        if not isinstance(available, list) or not isinstance(active, dict):
            raise WatcherError("hidden-worker pool schema is invalid")
        if any(entry.get("worker_id") == worker_id for entry in active.values() if isinstance(entry, dict)):
            raise WatcherError(f"hidden worker is already active: {worker_id}")
        if not available:
            raise WatcherError("hidden-worker scientist pool is exhausted")
        callsign = str(available.pop(0))
        assignment = {
            "callsign": callsign,
            "role": "hidden-worker",
            "owner": owner,
            "worker_id": worker_id,
            "model": model,
            "effort": effort,
            "routing_reason": reason,
            "status": "working",
        }
        active[callsign] = assignment
        return assignment

    return _mutate_json(pool_path, allocate)


def release_hidden_worker(pool_path: Path, evidence: Dict[str, Any]) -> Dict[str, Any]:
    failures = [gate for gate in HIDDEN_WORKER_RELEASE_GATES if evidence.get(gate) is not True]
    identity = evidence.get("identity")
    if not isinstance(identity, dict):
        failures.append("identity object missing")
        identity = {}
    callsign = str(identity.get("callsign", ""))
    worker_id = str(identity.get("worker_id", ""))
    owner = str(identity.get("owner", ""))
    if not all((callsign, worker_id, owner)):
        failures.append("hidden-worker identity incomplete")
    if failures:
        raise WatcherError("hidden-worker release refused: " + ", ".join(failures))

    def release(value: Dict[str, Any]) -> Dict[str, Any]:
        available = value.get("available")
        active = value.get("active")
        if not isinstance(available, list) or not isinstance(active, dict):
            raise WatcherError("hidden-worker pool schema is invalid")
        assignment = active.get(callsign)
        if not isinstance(assignment, dict):
            raise WatcherError("hidden-worker callsign is not active")
        if assignment.get("worker_id") != worker_id or assignment.get("owner") != owner:
            raise WatcherError("hidden-worker release identity does not match")
        del active[callsign]
        if callsign not in available:
            available.append(callsign)
            available.sort()
        return {"released": True, "callsign": callsign, "worker_id": worker_id, "owner": owner}

    return _mutate_json(pool_path, release)


LEAD_MATERIAL_STATUSES = {
    "blocked",
    "completed",
    "complete",
    "cancelled",
    "canceled",
    "failed",
    "ready_to_land",
}


def relay_to_lead(
    config_path: Path,
    event_path: Path,
    relay_state_path: Path,
    delivery_command: Optional[str],
) -> Dict[str, Any]:
    config = _load_json(config_path)
    event = _load_json(event_path)
    status = str(event.get("status", "")).lower()
    if status in LEGACY_SILENT_STATUSES or status not in LEAD_MATERIAL_STATUSES:
        return {"relayed": False, "reason": "non-material"}
    lead = config.get("lead")
    if lead is None:
        return {"relayed": False, "reason": "lead-unassigned", "durable": True}
    if not isinstance(lead, dict) or not lead.get("callsign"):
        raise WatcherError("Lead configuration is invalid")
    if not delivery_command:
        raise WatcherError("Lead delivery command is required when a Lead is assigned")
    digest = hashlib.sha256(
        (_json_dump(event) + "\0" + str(lead["callsign"])).encode("utf-8")
    ).hexdigest()
    relay_state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = relay_state_path.with_name(f".{relay_state_path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = _load_json(relay_state_path) if relay_state_path.exists() else {"delivered": []}
        delivered = state.get("delivered")
        if not isinstance(delivered, list):
            raise WatcherError("Lead relay state is invalid")
        if digest in delivered:
            return {"relayed": False, "reason": "duplicate", "lead": lead["callsign"]}
        payload = {"lead": lead, "event": event}
        try:
            subprocess.run(
                shlex.split(delivery_command),
                input=_json_dump(payload) + "\n",
                check=True,
                text=True,
                timeout=10,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise WatcherError(f"Lead delivery failed; event remains durable: {exc}") from exc
        delivered.append(digest)
        state["delivered"] = delivered[-2048:]
        _write_json_atomic(relay_state_path, state)
        return {"relayed": True, "lead": lead["callsign"], "digest": digest}


def route_model(
    config_path: Path,
    task_profile: str,
    explicit_model: Optional[str],
    explicit_effort: Optional[str],
) -> Dict[str, Any]:
    config = _load_json(config_path)
    tiers = config.get("tiers")
    if not isinstance(tiers, dict):
        raise WatcherError("model-routing tiers are missing")
    if task_profile == "coordination":
        tier = "COORDINATOR"
        reason = "Shotcaller coordination uses the configured coordinator tier."
    elif task_profile == "bounded":
        tier = "WORKER_FAST"
        reason = "Bounded, checkable work uses the lowest sufficient worker tier."
    elif task_profile in {"ambiguous", "high-impact", "weak-verification"}:
        tier = "WORKER_STRONG"
        reason = f"{task_profile} work requires the strong worker tier."
    else:
        raise WatcherError(f"unknown task profile: {task_profile}")
    selected = tiers.get(tier)
    if not isinstance(selected, dict) or not selected.get("model") or not selected.get("effort"):
        raise WatcherError(f"model-routing tier is incomplete: {tier}")
    model = explicit_model if explicit_model is not None else str(selected["model"])
    effort = explicit_effort if explicit_effort is not None else str(selected["effort"])
    return {
        "tier": tier,
        "model": model,
        "effort": effort,
        "reason": "Explicit user choice preserved exactly; unspecified fields use the selected tier."
        if explicit_model is not None or explicit_effort is not None
        else reason,
        "explicit": {"model": explicit_model is not None, "effort": explicit_effort is not None},
    }


def install_codex_hooks(hooks_path: Path, stable_command: str) -> Dict[str, Any]:
    hooks_document = _load_json(hooks_path) if hooks_path.exists() else {"hooks": {}}
    hooks = hooks_document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise WatcherError("Codex hooks object is invalid")
    wanted = {
        "UserPromptSubmit": f"{stable_command} codex-user-prompt-hook",
        "Stop": f"{stable_command} codex-stop-hook",
    }
    added: List[str] = []
    for event_name, command in wanted.items():
        groups = hooks.setdefault(event_name, [])
        if not isinstance(groups, list):
            raise WatcherError(f"Codex hook event is invalid: {event_name}")
        existing = any(
            isinstance(group, dict)
            and any(
                isinstance(handler, dict) and handler.get("command") == command
                for handler in group.get("hooks", [])
            )
            for group in groups
        )
        if existing:
            continue
        groups.append(
            {
                "hooks": [
                    {"type": "command", "command": command, "timeout": 5}
                ]
            }
        )
        added.append(event_name)
    _write_json_atomic(hooks_path, hooks_document)
    return {"installed": True, "added": added, "hooks": wanted}


def _default_records_root() -> Path:
    return Path(os.environ.get("AGENT_WATCHER_RECORDS_ROOT", "~/.agents/shotcallers")).expanduser()


def _default_state_dir() -> Path:
    return Path(os.environ.get("AGENT_WATCHER_STATE_DIR", "~/.local/state/agent-watcher")).expanduser()


def _default_archive_dir() -> Path:
    return Path(os.environ.get("AGENT_WATCHER_ARCHIVE_DIR", "~/.agents/shotcallers")).expanduser()


def _read_hook_payload() -> Dict[str, Any]:
    data = sys.stdin.buffer.read(1_048_577)
    if len(data) > 1_048_576:
        raise WatcherError("Codex hook input exceeds 1 MiB")
    if not data.strip():
        return {}
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise WatcherError(f"Codex hook input is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WatcherError("Codex hook input is not an object")
    return value


def _base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Block the current turn until a material Champion event.")
    parser.add_argument("--records-root", type=Path, default=_default_records_root(), help="Champion record tree containing status.json and updates.jsonl.")
    parser.add_argument("--state-dir", type=Path, default=_default_state_dir(), help="Durable watcher control/cursor directory.")
    parser.add_argument("--shotcaller", help="Scope records and durable state to one exact Shotcaller callsign.")
    parser.add_argument("--session-id", help="Resolve one Shotcaller by exact Codex session/thread id.")
    parser.add_argument(
        "--record-format",
        choices=("current", "legacy"),
        default="current",
        help="Validate the current durable record contract; legacy input requires explicit classification.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("enable", help="Persistently enable watching.")
    subparsers.add_parser("disable", help="Persistently disable watching and cancel waits.")
    allow_stop = subparsers.add_parser("allow-stop", help="Atomically allow the next Stop and cancel the current wait.")
    allow_stop.add_argument("--once", action="store_true", required=True, help="Require the explicit one-shot permission form.")
    subparsers.add_parser("status", help="Read watcher state without changing it.")
    wait = subparsers.add_parser("wait", help="Block silently until a material event or control action.")
    wait.add_argument("--poll-seconds", type=float, default=1.0, help="Internal poll interval; no output is produced by polling.")
    wait.add_argument("--liveness-seconds", type=float, default=300.0, help="Silent health-check interval.")
    wait.add_argument("--repair-command", help="One bounded command used for a single watcher repair/restart attempt.")
    supervise = subparsers.add_parser("supervise", help="Wait once for a scoped material event; ready_to_land remains intact.")
    supervise.add_argument("--poll-seconds", type=float, default=1.0, help="Internal poll interval; no output is produced by polling.")
    supervise.add_argument("--liveness-seconds", type=float, default=300.0, help="Silent health-check interval.")
    supervise.add_argument("--repair-command", help="One bounded command used for a single watcher repair/restart attempt.")
    for runtime_wait in (wait, supervise):
        runtime_wait.add_argument("--adapter", choices=("herdr", "tmux"), help="Explicit runtime adapter; current Herdr/tmux environment is otherwise detected.")
        runtime_wait.add_argument("--herdr-session", help="Exact Herdr session for batched runtime snapshots.")
        runtime_wait.add_argument("--tmux-socket", help="Exact tmux socket for batched runtime snapshots.")
        runtime_wait.add_argument("--reconcile-seconds", type=float, default=30.0, help="Runtime snapshot interval; zero disables reconciliation.")
        runtime_wait.add_argument("--reconcile-consecutive", type=int, default=2, help="Identical mismatch observations required before champion_stalled.")
    subparsers.add_parser("codex-stop-hook", help="Emit one Codex Stop-hook decision without waiting.")
    subparsers.add_parser("codex-user-prompt-hook", help="Give an ordinary Codex user prompt priority over a pending watcher wait.")
    deliver = subparsers.add_parser(
        "deliver", help="Deliver one durable material transition through a watcher or verified direct prompt."
    )
    deliver.add_argument("--adapter", choices=("herdr", "tmux"), required=True)
    deliver.add_argument("--herdr-session", help="Exact running Herdr session when not using current-session routing.")
    deliver.add_argument("--tmux-socket", help="Exact tmux socket for the recorded Shotcaller pane.")
    transition = subparsers.add_parser(
        "transition", help="Atomically append one material transition and replace its matching snapshot."
    )
    transition.add_argument("--record", type=Path, required=True, help="Exact owner-controlled Champion or worker record directory.")
    transition.add_argument("--status", choices=tuple(sorted(DELIVERY_STATUSES)), required=True)
    transition.add_argument("--update", required=True)
    transition.add_argument("--next", dest="next_action", required=True)
    transition.add_argument("--blocker")
    transition.add_argument("--at", help="Explicit RFC3339 transition time; defaults to the current local time.")
    transition.add_argument("--adapter", choices=("herdr", "tmux"), help="Explicit delivery adapter; current Herdr/tmux environment is otherwise detected.")
    transition.add_argument("--herdr-session", help="Exact Herdr session for immediate watcher/direct delivery.")
    transition.add_argument("--tmux-socket", help="Exact tmux socket for immediate watcher/direct delivery.")
    transition.add_argument("--no-deliver", action="store_true", help="Write only; intended for isolated fixtures and diagnostics.")
    reconcile = subparsers.add_parser(
        "reconcile", help="Observe active Champion endpoints and route one stable runtime/status mismatch."
    )
    reconcile.add_argument("--adapter", choices=("herdr", "tmux"), required=True)
    reconcile.add_argument("--herdr-session", help="Exact running Herdr session when not using current-session routing.")
    reconcile.add_argument("--tmux-socket", help="Exact tmux socket containing recorded Champion and Shotcaller panes.")
    reconcile.add_argument("--consecutive", type=int, default=2, help="Required identical mismatch observations; bounded to 2-10.")
    preflight = subparsers.add_parser(
        "preflight", help="Reconcile the Roster, callsign pool, and live Herdr identity before launch."
    )
    preflight.add_argument("--pool", type=Path, required=True, help="League callsign pool JSON.")
    preflight.add_argument("--callsign", required=True, help="Exact visible Champion callsign.")
    preflight.add_argument("--name", dest="routing_name", required=True, help="Exact Herdr routing name.")
    preflight.add_argument("--display", dest="display_agent", required=True, help="Backend kind shown in the sidebar.")
    preflight.add_argument("--thread-id", required=True, help="Exact Codex thread UUID.")
    preflight.add_argument("--address", required=True, help="Exact Herdr pane identity.")
    preflight.add_argument("--herdr-session", required=True, help="Exact existing Herdr session.")
    launch = subparsers.add_parser(
        "launch", help="Atomically preflight and launch one visible Champion with separate routing/display names."
    )
    launch.add_argument("--pool", type=Path, required=True, help="League callsign pool JSON.")
    launch.add_argument("--callsign", required=True, help="Exact visible Champion callsign.")
    launch.add_argument("--name", dest="routing_name", required=True, help="Exact Herdr routing name.")
    launch.add_argument("--display", dest="display_agent", required=True, help="Backend kind shown in the sidebar.")
    launch.add_argument("--task-id", required=True)
    launch.add_argument("--task", required=True)
    launch.add_argument("--thread-id", required=True, help="Exact Codex thread UUID.")
    launch.add_argument("--address", required=True, help="Exact Herdr pane identity.")
    launch.add_argument("--herdr-session", required=True, help="Exact existing Herdr session.")
    launch.add_argument("--repository")
    launch.add_argument("--issue", type=int)
    launch.add_argument("--branch")
    launch.add_argument("--worktree")
    teardown = subparsers.add_parser("teardown", help="Verify gates, then optionally archive and close one exact agent.")
    teardown.add_argument("--adapter", choices=("herdr", "tmux"), required=True)
    teardown.add_argument("--manifest", "--evidence", dest="evidence", type=Path, required=True, help="Shotcaller schema-2 landing/release teardown manifest.")
    teardown.add_argument("--archive-dir", type=Path, default=_default_archive_dir(), help="Shotcaller archive root; final path is derived from date, callsign, and immutable task-id.")
    teardown.add_argument("--execute", action="store_true", help="Perform the verified archive and exact adapter commands.")
    adapter = subparsers.add_parser("adapter", help="Read exact Herdr or tmux identity through a thin adapter.")
    adapter_sub = adapter.add_subparsers(dest="adapter_command", required=True)
    herdr = adapter_sub.add_parser("herdr-inspect")
    herdr.add_argument("--session", required=True)
    herdr.add_argument("--agent", required=True)
    tmux = adapter_sub.add_parser("tmux-inspect")
    tmux.add_argument("--socket", required=True)
    tmux.add_argument("--pane", required=True)
    hidden_worker = subparsers.add_parser("hidden-worker", help="Allocate or safely release one agent-neutral hidden worker.")
    hidden_sub = hidden_worker.add_subparsers(dest="hidden_command", required=True)
    hidden_allocate = hidden_sub.add_parser("allocate")
    hidden_allocate.add_argument("--pool", type=Path, required=True)
    hidden_allocate.add_argument("--owner", required=True)
    hidden_allocate.add_argument("--worker-id", required=True)
    hidden_allocate.add_argument("--model", required=True)
    hidden_allocate.add_argument("--effort", required=True)
    hidden_allocate.add_argument("--reason", required=True)
    hidden_release = hidden_sub.add_parser("release")
    hidden_release.add_argument("--pool", type=Path, required=True)
    hidden_release.add_argument("--evidence", type=Path, required=True)
    relay = subparsers.add_parser("lead-relay", help="Relay one material event to an optional configured Lead.")
    relay.add_argument("--config", type=Path, required=True)
    relay.add_argument("--event", type=Path, required=True)
    relay.add_argument("--relay-state", type=Path, required=True)
    relay.add_argument("--delivery-command")
    model_route = subparsers.add_parser("route-model", help="Resolve one semantic model/effort routing decision.")
    model_route.add_argument("--config", type=Path, required=True)
    model_route.add_argument("--task-profile", choices=("coordination", "bounded", "ambiguous", "high-impact", "weak-verification"), required=True)
    model_route.add_argument("--model")
    model_route.add_argument("--effort")
    hooks = subparsers.add_parser("install-codex-hooks", help="Idempotently add watcher hooks to one hooks.json file.")
    hooks.add_argument("--hooks", type=Path, required=True)
    hooks.add_argument("--command", dest="stable_command", required=True)
    resource = subparsers.add_parser("resource-inspect", help="Read one exact child-process PID generation for a task manifest.")
    resource.add_argument("--pid", type=int, required=True)
    return parser


def _herdr_inspect(session: str, agent: str) -> Dict[str, Any]:
    command = ["herdr", "--session", session, "agent", "get", agent]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise WatcherError(f"Herdr identity unavailable: {exc}") from exc
    if not isinstance(value, dict):
        raise WatcherError("Herdr identity response is not an object")
    return value


def _herdr_agents(session: str) -> List[Dict[str, Any]]:
    command = ["herdr", "--session", session, "agent", "list"]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise WatcherError(f"Herdr identity unavailable: {exc}") from exc
    root = value.get("result", value) if isinstance(value, dict) else None
    agents = root.get("agents") if isinstance(root, dict) else None
    if not isinstance(agents, list) or not all(isinstance(agent, dict) for agent in agents):
        raise WatcherError("Herdr identity response has no valid agent list")
    return agents


def _herdr_agent_fields(value: Dict[str, Any]) -> Dict[str, Optional[str]]:
    root: Any = value.get("result", value)
    if isinstance(root, dict) and isinstance(root.get("agent"), dict):
        root = root["agent"]
    if not isinstance(root, dict):
        raise WatcherError("Herdr identity response has no agent object")
    session = root.get("agent_session")
    thread_id = (
        session.get("value")
        if isinstance(session, dict)
        else root.get("thread_id") or root.get("session_id")
    )
    return {
        "name": root.get("name") or root.get("agent_name") or root.get("routing_name"),
        "display": root.get("display_agent") or root.get("kind") or root.get("agent_kind"),
        "pane": root.get("pane_id") or root.get("address"),
        "thread_id": thread_id,
    }


def _identity_summary(path: Path, record: Dict[str, Any]) -> str:
    return (
        f"{path}: callsign={record.get('callsign')!r}, "
        f"thread_id={record.get('thread_id')!r}, address={record.get('address')!r}, "
        f"backend={record.get('backend')!r}"
    )


def _preflight_launch(
    records_root: Path,
    pool_path: Path,
    callsign: str,
    routing_name: str,
    display_agent: str,
    thread_id: str,
    address: str,
    herdr_session: str,
) -> Dict[str, Any]:
    candidate = {
        "callsign": callsign,
        "routing_name": routing_name,
        "display_agent": display_agent,
    }
    _validate_launch_names(candidate, "launch preflight")
    if not SAFE_COMPONENT_PATTERN.fullmatch(callsign):
        raise WatcherError("launch preflight refused: callsign is not path-safe")
    if not EXACT_THREAD_ID_PATTERN.fullmatch(thread_id):
        raise WatcherError("launch preflight refused: thread_id must be an exact Codex UUID")
    if not HERDR_ADDRESS_PATTERN.fullmatch(address):
        raise WatcherError("launch preflight refused: address must be an exact Herdr pane")
    records: List[Tuple[Path, Dict[str, Any]]] = []
    for status_path in _record_paths(records_root):
        record = _validate_record_pair(status_path)
        records.append((status_path, record))
    conflicts: List[str] = []
    duplicate_groups: Dict[Tuple[str, str], List[Tuple[Path, Dict[str, Any]]]] = {}
    for status_path, record in records:
        if (
            record.get("callsign") == callsign
            or record.get("thread_id") == thread_id
            or (record.get("backend") == "herdr" and record.get("address") == address)
        ):
            conflicts.append(_identity_summary(status_path, record))
        duplicate_groups.setdefault(("callsign", str(record.get("callsign"))), []).append(
            (status_path, record)
        )
        duplicate_groups.setdefault(
            ("herdr-endpoint", f"{record.get('backend')}:{record.get('address')}"), []
        ).append((status_path, record))
        duplicate_groups.setdefault(("thread", str(record.get("thread_id"))), []).append(
            (status_path, record)
        )
    duplicate_details: List[str] = []
    for (kind, key), members in duplicate_groups.items():
        if len(members) > 1:
            duplicate_details.append(
                f"duplicate {kind} {key!r}: "
                + " | ".join(_identity_summary(path, record) for path, record in members)
            )
    try:
        pool = _load_json(pool_path)
        available = _require_object(pool.get("available"), "callsign pool available")
        champion_pool = available.get("champion")
        in_use = _require_object(pool.get("in_use"), "callsign pool in_use")
    except WatcherError as exc:
        raise WatcherError(f"launch preflight refused: {exc}") from exc
    if callsign in in_use:
        conflicts.append(f"callsign pool already assigns {callsign!r}: {in_use[callsign]!r}")
    if not isinstance(champion_pool, list) or callsign not in champion_pool:
        conflicts.append(f"callsign {callsign!r} is not in available.champion; release proof is missing")
    live_conflicts: List[str] = []
    live_groups: Dict[Tuple[str, str], List[Dict[str, Optional[str]]]] = {}
    for agent in _herdr_agents(herdr_session):
        fields = _herdr_agent_fields(agent)
        if fields.get("name"):
            live_groups.setdefault(("name", str(fields["name"])), []).append(fields)
        if fields.get("thread_id"):
            live_groups.setdefault(("thread", str(fields["thread_id"])), []).append(fields)
        if fields.get("pane"):
            live_groups.setdefault(("pane", str(fields["pane"])), []).append(fields)
        if (
            fields.get("name") == routing_name
            or fields.get("thread_id") == thread_id
            or fields.get("pane") == address
        ):
            live_conflicts.append(f"live Herdr identity: {fields!r}")
    for (kind, key), members in live_groups.items():
        if len(members) > 1:
            live_conflicts.append(f"duplicate live Herdr {kind} {key!r}: {members!r}")
    conflicts.extend(duplicate_details)
    conflicts.extend(live_conflicts)
    if conflicts:
        raise WatcherError("launch preflight refused: " + "; ".join(conflicts))
    return {
        "callsign": callsign,
        "name": routing_name,
        "display": display_agent,
        "records_checked": len(records),
        "pool": str(pool_path),
        "herdr_session": herdr_session,
    }


def _reserve_callsign(pool_path: Path, callsign: str, record: str) -> None:
    def reserve(value: Dict[str, Any]) -> None:
        available = _require_object(value.get("available"), "callsign pool available")
        champion_pool = available.get("champion")
        in_use = _require_object(value.get("in_use"), "callsign pool in_use")
        if not isinstance(champion_pool, list):
            raise WatcherError("launch refused: callsign pool available.champion is invalid")
        if callsign in in_use or callsign not in champion_pool:
            raise WatcherError("launch refused: callsign is not proven released")
        champion_pool.remove(callsign)
        in_use[callsign] = {"role": "champion", "record": record}

    _mutate_json(pool_path, reserve)


def _atomic_create_champion_record(
    record_dir: Path, snapshot: Dict[str, Any], transition: Dict[str, Any]
) -> None:
    if record_dir.exists():
        raise WatcherError("launch refused: Champion record already exists")
    record_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{record_dir.name}.", dir=record_dir.parent))
    try:
        (stage / "status.json").write_text(
            json.dumps(snapshot, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        (stage / "updates.jsonl").write_text(
            json.dumps(transition, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        with (stage / "status.json").open("rb") as handle:
            os.fsync(handle.fileno())
        with (stage / "updates.jsonl").open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(stage, record_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def launch_champion(
    records_root: Path,
    shotcaller: str,
    pool_path: Path,
    callsign: str,
    routing_name: str,
    display_agent: str,
    task_id: str,
    task: str,
    thread_id: str,
    address: str,
    repository: Optional[str],
    issue: Optional[int],
    branch: Optional[str],
    worktree: Optional[str],
    herdr_session: str,
) -> Dict[str, Any]:
    record_dir = records_root / shotcaller / "champions" / callsign
    if any(item is None for item in (repository, issue, branch, worktree)) and not all(
        item is None for item in (repository, issue, branch, worktree)
    ):
        raise WatcherError("launch refused: repository, issue, branch, and worktree must be all set or all null")
    preflight = _preflight_launch(
        records_root,
        pool_path,
        callsign,
        routing_name,
        display_agent,
        thread_id,
        address,
        herdr_session,
    )
    reserved = False
    started = False
    try:
        _reserve_callsign(pool_path, callsign, str(record_dir))
        reserved = True
        command = [
            "herdr",
            "--session",
            herdr_session,
            "agent",
            "start",
            routing_name,
            "--kind",
            display_agent,
            "--pane",
            address,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise WatcherError(f"launch refused: Herdr start failed: {exc}") from exc
        started = True
        fields = _herdr_agent_fields(_herdr_inspect(herdr_session, routing_name))
        if fields != {
            "name": routing_name,
            "display": display_agent,
            "pane": address,
            "thread_id": thread_id,
        }:
            raise WatcherError(
                f"launch refused: verified Herdr identity does not match name/display/pane/thread: {fields!r}"
            )
        at = datetime.now().astimezone().isoformat(timespec="seconds")
        update = f"Launched {display_agent} agent as {routing_name} after Roster, pool, and Herdr preflight."
        snapshot: Dict[str, Any] = {
            "callsign": callsign,
            "routing_name": routing_name,
            "display_agent": display_agent,
            "role": "champion",
            "shotcaller": shotcaller,
            "kind": "codex-thread",
            "address": address,
            "thread_id": thread_id,
            "backend": "herdr",
            "task_id": task_id,
            "task": task,
            "status": "working",
            "updated_at": at,
            "update": update,
            "blocker": None,
            "next": "Implement the assigned issue in the isolated worktree.",
            "repository": repository,
            "issue": issue,
            "branch": branch,
            "worktree": worktree,
        }
        _validate_status_snapshot(snapshot, record_dir / "status.json")
        _atomic_create_champion_record(
            record_dir,
            snapshot,
            {"at": at, "status": "working", "update": update},
        )
        return {
            "event": "champion-launched",
            "callsign": callsign,
            "name": routing_name,
            "display": display_agent,
            "record": str(record_dir),
            "identity_verified": True,
            "preflight": preflight,
        }
    except Exception as exc:
        if started:
            subprocess.run(
                ["herdr", "--session", herdr_session, "agent", "release", routing_name],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        if reserved:
            try:
                _release_visible_callsign(
                    {"pool": str(pool_path), "callsign": callsign, "role": "champion"}
                )
            except WatcherError:
                pass
        if isinstance(exc, WatcherError):
            raise
        raise WatcherError(f"launch refused: {exc}") from exc


def _tmux_inspect(socket: str, pane: str) -> Dict[str, Any]:
    format_string = "pane_id=#{pane_id};window_id=#{window_id};command=#{pane_current_command};pid=#{pane_pid}"
    command = ["tmux", "-S", socket, "display-message", "-p", "-t", pane, format_string]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WatcherError(f"tmux identity unavailable: {exc}") from exc
    fields = dict(item.split("=", 1) for item in result.stdout.strip().split(";") if "=" in item)
    fields.update({"socket": socket, "pane_id": pane})
    return fields


def main(argv: Optional[Sequence[str]] = None) -> int:
    global CURRENT_RECORD_FORMAT
    parser = _base_parser()
    args = parser.parse_args(argv)
    CURRENT_RECORD_FORMAT = args.record_format
    records_root = args.records_root.expanduser()
    try:
        hook_payload: Dict[str, Any] = {}
        if args.command in {"codex-stop-hook", "codex-user-prompt-hook"}:
            hook_payload = _read_hook_payload()
        session_id = args.session_id or hook_payload.get("session_id")
        try:
            shotcaller = _resolve_shotcaller(records_root, args.shotcaller, session_id)
        except WatcherError:
            if args.command in {"codex-stop-hook", "codex-user-prompt-hook"} and not args.shotcaller:
                _emit({})
                return 0
            raise
        state_dir = args.state_dir.expanduser()
        if shotcaller:
            state_dir = state_dir / "shotcallers" / shotcaller
        store = Store(state_dir)
        if args.command in {"enable", "disable", "allow-stop"}:
            _emit(_control(store, args.command))
            return 0
        if args.command == "preflight":
            if not shotcaller:
                raise WatcherError("launch preflight requires --shotcaller")
            _emit(
                _preflight_launch(
                    records_root,
                    args.pool.expanduser(),
                    args.callsign,
                    args.routing_name,
                    args.display_agent,
                    args.thread_id,
                    args.address,
                    args.herdr_session,
                )
            )
            return 0
        if args.command == "launch":
            if not shotcaller:
                raise WatcherError("launch requires --shotcaller")
            _emit(
                launch_champion(
                    records_root,
                    shotcaller,
                    args.pool.expanduser(),
                    args.callsign,
                    args.routing_name,
                    args.display_agent,
                    args.task_id,
                    args.task,
                    args.thread_id,
                    args.address,
                    args.repository,
                    args.issue,
                    args.branch,
                    args.worktree,
                    args.herdr_session,
                )
            )
            return 0
        if args.command == "status":
            state = store.read()
            active = _active_records(records_root, shotcaller)
            _emit(
                {
                    "enabled": bool(state.get("enabled", True)),
                    "allow_stop_once": bool(state.get("allow_stop_once", False)),
                    "stop_blocked": bool(state.get("stop_blocked", False)),
                    "active_delegates": len(active),
                    "active_champions": len(active),
                    "pending_events": len(state.get("pending_events", {})),
                    "shotcaller": shotcaller,
                    "state_path": str(store.state_path),
                }
            )
            return 0
        if args.command == "wait":
            return wait_for_event(
                records_root,
                store,
                args.poll_seconds,
                args.liveness_seconds,
                args.repair_command,
                shotcaller,
                runtime_adapter=args.adapter,
                herdr_session=args.herdr_session,
                tmux_socket=args.tmux_socket,
                reconcile_seconds=args.reconcile_seconds,
                reconcile_consecutive=args.reconcile_consecutive,
            )
        if args.command == "supervise":
            return wait_for_event(
                records_root,
                store,
                args.poll_seconds,
                args.liveness_seconds,
                args.repair_command,
                shotcaller,
                supervise_event,
                args.adapter,
                args.herdr_session,
                args.tmux_socket,
                args.reconcile_seconds,
                args.reconcile_consecutive,
            )
        if args.command == "codex-stop-hook":
            return codex_stop_hook(records_root, store, shotcaller)
        if args.command == "codex-user-prompt-hook":
            return codex_user_prompt_hook(store)
        if args.command == "deliver":
            if not shotcaller:
                raise WatcherError("direct delivery requires --shotcaller or an exact session id")
            _emit(
                deliver_transition(
                    records_root,
                    store,
                    shotcaller,
                    args.adapter,
                    args.herdr_session,
                    args.tmux_socket,
                )
            )
            return 0
        if args.command == "transition":
            resolved_record = _resolve_transition_target(records_root, args.record)
            target_snapshot = _validate_record_pair(resolved_record / "status.json")
            owner = str(target_snapshot.get("shotcaller", ""))
            if not owner:
                raise WatcherError("atomic transition target has no exact Shotcaller owner")
            scoped_store = Store(args.state_dir.expanduser() / "shotcallers" / owner)
            _ensure_state_baseline(scoped_store, records_root, owner)
            result = transition_record(
                records_root,
                resolved_record,
                args.status,
                args.update,
                args.next_action,
                args.blocker,
                args.at,
            )
            if args.no_deliver:
                result["delivery"] = {"delivered": False, "preserved": True, "reason": "disabled"}
            else:
                snapshot = _validate_record_pair(Path(result["record"]) / "status.json")
                if str(snapshot["shotcaller"]) != owner:
                    raise WatcherError("atomic transition Shotcaller owner changed during routing")
                adapter, herdr_session, tmux_socket = _runtime_route(
                    args.adapter, args.herdr_session, args.tmux_socket
                )
                if adapter is None:
                    result["delivery"] = {
                        "delivered": False,
                        "preserved": True,
                        "reason": "no-runtime-adapter",
                    }
                else:
                    result["delivery"] = deliver_transition(
                        records_root,
                        scoped_store,
                        owner,
                        adapter,
                        herdr_session,
                        tmux_socket,
                    )
            _emit(result)
            return 0
        if args.command == "reconcile":
            if not shotcaller:
                raise WatcherError("runtime reconciliation requires --shotcaller or an exact session id")
            _emit(
                reconcile_runtime(
                    records_root,
                    store,
                    shotcaller,
                    args.adapter,
                    args.herdr_session,
                    args.tmux_socket,
                    args.consecutive,
                )
            )
            return 0
        if args.command == "teardown":
            manifest_path = args.evidence.expanduser()
            manifest = _load_json(manifest_path)
            _emit(
                execute_teardown(
                    args.adapter,
                    manifest,
                    manifest_path,
                    records_root,
                    args.archive_dir.expanduser(),
                    args.execute,
                )
            )
            return 0
        if args.command == "adapter":
            if args.adapter_command == "herdr-inspect":
                _emit(_herdr_inspect(args.session, args.agent))
            else:
                _emit(_tmux_inspect(args.socket, args.pane))
            return 0
        if args.command == "hidden-worker":
            if args.hidden_command == "allocate":
                _emit(
                    allocate_hidden_worker(
                        args.pool.expanduser(),
                        args.owner,
                        args.worker_id,
                        args.model,
                        args.effort,
                        args.reason,
                    )
                )
            else:
                _emit(
                    release_hidden_worker(
                        args.pool.expanduser(), _load_json(args.evidence.expanduser())
                    )
                )
            return 0
        if args.command == "lead-relay":
            _emit(
                relay_to_lead(
                    args.config.expanduser(),
                    args.event.expanduser(),
                    args.relay_state.expanduser(),
                    args.delivery_command,
                )
            )
            return 0
        if args.command == "route-model":
            _emit(
                route_model(
                    args.config.expanduser(), args.task_profile, args.model, args.effort
                )
            )
            return 0
        if args.command == "install-codex-hooks":
            _emit(install_codex_hooks(args.hooks.expanduser(), args.stable_command))
            return 0
        if args.command == "resource-inspect":
            _emit(inspect_process(args.pid))
            return 0
        parser.error(f"unknown command: {args.command}")
    except WatcherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
