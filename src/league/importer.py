"""Strict, bounded, dry-run-first importer for the issue-#18 artifact inventory."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .sqlite_store import CURRENT_SCHEMA_VERSION, _IMPORT_COLUMNS
from .sqlite_project_ops import canonical_repository
from .storage import ImportPlan, StorageRefusal
from .storage_types import LIFECYCLE_STATES


MANIFEST_SCHEMA = 1
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 10_000
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
THREAD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HERDR_ADDRESS = re.compile(r"^w[0-9A-Za-z]+:p[0-9A-Za-z]+$")
TMUX_ADDRESS = re.compile(r"^%[0-9]+$")

SOURCE_KINDS = (
    "rosters",
    "pending_launches",
    "watcher_states",
    "visible_callsign_pools",
    "hidden_worker_pools",
    "lead_relay_states",
    "resource_registries",
)

# Every issue-#18 matrix entry has one explicit database/cutover disposition.
# The import report emits this map so a missing or newly discovered consumer is
# visible and blocks before apply rather than becoming an implicit scan rule.
AUDIT_COVERAGE = {
    "R1": "migrate",
    "R2": "migrate",
    "R3": "migrate",
    "R4": "retain-or-absent",
    "L1": "migrate",
    "L2": "retain",
    "L3": "transient",
    "W1": "migrate",
    "W2": "migrate",
    "W3": "transient",
    "C1": "migrate",
    "C2": "migrate",
    "D1": "migrate",
    "D2": "migrate",
    "D3": "transient",
    "D4": "migrate",
    "D5": "transient",
    "P1": "retain",
    "P2": "retain",
    "P3": "absent-planned",
    "P4": "absent-planned",
    "P5": "migrate",
    "P6": "retain",
    "T1": "retain",
    "T2": "retain",
    "T3": "retain",
    "T4": "retain",
    "T5": "retain",
    "T6": "retain",
    "H1": "retain",
    "H2": "transient",
    "H3": "transient",
    "A1": "transient",
    "A2": "transient",
    "I1": "retain",
    "I2": "retain",
    "I3": "retain",
    "S1": "retain",
    "S2": "retain",
    "S3": "out-of-scope",
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise StorageRefusal("duplicate_key", "JSON input contains a duplicate key")
        value[key] = item
    return value


def _decode_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise StorageRefusal("malformed_input", f"{label} is not valid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except json.JSONDecodeError as exc:
        raise StorageRefusal("malformed_input", f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise StorageRefusal("malformed_input", f"{label} must be a JSON object")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorageRefusal("malformed_input", f"{label} must be a non-empty RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StorageRefusal("malformed_input", f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("malformed_input", f"{label} must include a UTC offset")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorageRefusal("malformed_input", f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StorageRefusal("malformed_input", f"{label} must be an integer at least {minimum}")
    return value


def _boolean(value: Any, label: str) -> int:
    if not isinstance(value, bool):
        raise StorageRefusal("malformed_input", f"{label} must be a boolean")
    return int(value)


def _artifact_id(value: Any) -> str:
    item = _text(value, "artifact_id")
    if not SAFE_ID.fullmatch(item):
        raise StorageRefusal("malformed_input", "artifact_id is not a bounded stable identifier")
    return item


def _legacy_visible_record(value: Any) -> dict[str, str]:
    record = _text(value, "visible assignment record")
    try:
        path = Path(record)
    except (OSError, ValueError) as exc:
        raise StorageRefusal(
            "malformed_input", "visible assignment record must be an absolute canonical path"
        ) from exc
    if (
        not path.is_absolute()
        or str(path) != record
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise StorageRefusal(
            "malformed_input", "visible assignment record must be an absolute canonical path"
        )
    locator_kind = "status_snapshot" if path.name == "status.json" else "record_directory"
    record_directory = path.parent if locator_kind == "status_snapshot" else path
    if record_directory == Path("/") or not record_directory.name:
        raise StorageRefusal(
            "malformed_input", "visible assignment record has no record directory"
        )
    return {
        "record": record,
        "record_directory": str(record_directory),
        "locator_kind": locator_kind,
    }


def _legacy_digest(source: str, offset: int, line: str) -> str:
    return hashlib.sha256(f"{source}\0{offset}\0{line}".encode("utf-8")).hexdigest()


class ImportPlanner:
    def __init__(
        self,
        source_root: Path,
        manifest_path: Path,
        *,
        target_counts: Optional[dict[str, int]] = None,
    ) -> None:
        root = Path(source_root)
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise StorageRefusal("invalid_root", "source root must be an explicit non-symlink directory")
        if root.resolve() == Path("/"):
            raise StorageRefusal("invalid_root", "filesystem root cannot be an import source root")
        self.input_root = root
        self.root = root.resolve()
        self.total_bytes = 0
        self.paths: set[Path] = set()
        self.file_ids: set[tuple[int, int]] = set()
        self.artifact_ids: set[str] = set()
        self.artifacts: list[dict[str, Any]] = []
        self.retained: list[dict[str, Any]] = []
        self.retained_sources: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, list[dict[str, Any]]] = {table: [] for table in _IMPORT_COLUMNS}
        self.target_counts = target_counts or {table: 0 for table in _IMPORT_COLUMNS}
        self.callsigns: dict[str, dict[str, Any]] = {}
        self.agents: dict[str, dict[str, Any]] = {}
        self.agent_by_callsign: dict[str, str] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        self.project_by_repository: dict[str, str] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.squads: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.event_by_identity: dict[tuple[str, str, str, str], str] = {}
        self.event_aliases: dict[str, str] = {}
        self.roster_lines: dict[str, dict[int, dict[str, Any]]] = {}
        self.leases: dict[str, dict[str, Any]] = {}
        self.launch_attempts: dict[str, dict[str, Any]] = {}
        self.deliveries: dict[tuple[str, str], dict[str, Any]] = {}
        self.retired_cursor_bindings: dict[tuple[str, int], tuple[Any, ...]] = {}
        self.retired_artifact_sources: dict[str, str] = {}
        self.retired_event_sources: dict[str, str] = {}
        self._source_order: dict[str, int] = {}
        self.manifest = self._load_manifest(manifest_path)

    def _relative_path(self, relative_value: Any) -> Path:
        relative = Path(_text(relative_value, "artifact path"))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise StorageRefusal("malformed_input", "artifact path must be safely relative to source root")
        return relative

    def _read_relative(self, relative: Path, *, track: bool) -> tuple[Path, bytes]:
        """Validate and read one stable descriptor beneath the explicit root."""
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | no_follow
        directory_fd: Optional[int] = None
        file_fd: Optional[int] = None
        try:
            directory_fd = os.open(self.root, directory_flags)
            for part in relative.parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(relative.name, os.O_RDONLY | no_follow, dir_fd=directory_fd)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise StorageRefusal(
                    "malformed_input", "artifact path must identify a regular non-symlink file"
                )
            identity = (before.st_dev, before.st_ino)
            resolved = self.root.joinpath(relative)
            if track and (resolved in self.paths or identity in self.file_ids):
                raise StorageRefusal("duplicate_artifact", "one source file is listed more than once")
            if before.st_size > MAX_ARTIFACT_BYTES:
                raise StorageRefusal("input_too_large", "one import artifact exceeds the size bound")
            chunks: list[bytes] = []
            remaining = MAX_ARTIFACT_BYTES + 1
            while remaining:
                chunk = os.read(file_fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(file_fd)
            if (
                identity != (after.st_dev, after.st_ino)
                or before.st_size != after.st_size
                or len(data) != after.st_size
            ):
                raise StorageRefusal("source_changed", "import artifact changed while it was read")
            if len(data) > MAX_ARTIFACT_BYTES:
                raise StorageRefusal("input_too_large", "one import artifact exceeds the size bound")
            if track:
                self.paths.add(resolved)
                self.file_ids.add(identity)
                self.total_bytes += len(data)
                if self.total_bytes > MAX_TOTAL_BYTES:
                    raise StorageRefusal("input_too_large", "import artifacts exceed the total size bound")
            return resolved, data
        except StorageRefusal:
            raise
        except OSError as exc:
            raise StorageRefusal("malformed_input", "artifact could not be safely opened") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    def _read(self, relative_value: Any) -> tuple[Path, bytes]:
        return self._read_relative(self._relative_path(relative_value), track=True)

    def _load_manifest(self, manifest_path: Path) -> dict[str, Any]:
        manifest = Path(manifest_path)
        if not manifest.is_absolute():
            manifest = self.input_root / manifest
        relative = None
        for base in (self.input_root, self.root):
            try:
                relative = manifest.relative_to(base)
                break
            except ValueError:
                continue
        if relative is None:
            raise StorageRefusal("malformed_input", "manifest must be inside source root")
        _, data = self._read_relative(self._relative_path(str(relative)), track=False)
        value = _decode_object(data, "manifest")
        expected_keys = {"schema", "captured_at", "canonical_sources", "retained_files", "unknown_consumers"}
        if set(value) != expected_keys or value.get("schema") != MANIFEST_SCHEMA:
            raise StorageRefusal("malformed_input", "manifest schema or fields are unsupported")
        _timestamp(value["captured_at"], "captured_at")
        sources = value["canonical_sources"]
        if not isinstance(sources, dict) or set(sources) != set(SOURCE_KINDS):
            raise StorageRefusal("unknown_consumer", "manifest does not explicitly cover every canonical source family")
        if any(not isinstance(sources[kind], list) for kind in SOURCE_KINDS):
            raise StorageRefusal("malformed_input", "canonical source families must be lists")
        if not isinstance(value["retained_files"], list) or not isinstance(value["unknown_consumers"], list):
            raise StorageRefusal("malformed_input", "retained_files and unknown_consumers must be lists")
        if value["unknown_consumers"]:
            raise StorageRefusal("unknown_consumer", "unknown producer or consumer blocks import")
        return value

    def _register_artifact(
        self, artifact_id: str, kind: str, data: bytes, record_count: int
    ) -> None:
        if artifact_id in self.artifact_ids:
            raise StorageRefusal("duplicate_artifact", "artifact_id is duplicated")
        self.artifact_ids.add(artifact_id)
        self.artifacts.append(
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "digest": hashlib.sha256(data).hexdigest(),
                "record_count": record_count,
                "source_order": self._source_order[artifact_id],
            }
        )

    def _ordered_entries(self) -> list[tuple[str, dict[str, Any]]]:
        entries: list[tuple[str, dict[str, Any]]] = []
        for kind in SOURCE_KINDS:
            for raw in self.manifest["canonical_sources"][kind]:
                if not isinstance(raw, dict):
                    raise StorageRefusal("malformed_input", f"{kind} entry must be an object")
                entry = dict(raw)
                identifier = _artifact_id(entry.get("artifact_id"))
                entry["artifact_id"] = identifier
                path_key = "status" if kind == "rosters" else "path"
                sort_path = _text(entry.get(path_key), f"{kind}.{path_key}")
                entries.append((kind, entry | {"_sort_path": sort_path}))
        entries.sort(key=lambda item: (item[1]["_sort_path"], item[0], item[1]["artifact_id"]))
        for order, (_, entry) in enumerate(entries):
            self._source_order[entry["artifact_id"]] = order
        return entries

    def _ensure_project(self, repository: str, at: str) -> str:
        repository_value, repository_key = canonical_repository(repository)
        known = self.project_by_repository.get(repository_key)
        if known:
            return known
        project_id = f"project:{hashlib.sha256(repository_key.encode('utf-8')).hexdigest()[:24]}"
        row = {
            "project_id": project_id,
            "repository": repository_value,
            "state": "active",
            "version": 1,
            "updated_at": at,
            "summary": "Imported project",
            "root_path": None,
            "repository_key": repository_key,
            "root_key": None,
            "code": None,
            "code_key": None,
            "repository_visibility": "unknown",
            "export_policy": "deny",
            "root_classification": "local_only",
            "repository_classification": "local_only",
        }
        self.projects[project_id] = row
        self.project_by_repository[repository_key] = project_id
        return project_id

    def _ensure_task(
        self, task_id: str, at: str, *, project_id: Optional[str], summary: str
    ) -> dict[str, Any]:
        known = self.tasks.get(task_id)
        if known:
            if known["project_id"] != project_id and project_id is not None:
                raise StorageRefusal("identity_collision", "task project identity collides")
            return known
        row = {
            "task_id": task_id,
            "project_id": project_id,
            "summary": summary,
            "state": "active",
            "version": 1,
            "current_owner_agent_id": None,
            "current_owner_squad_id": None,
            "updated_at": at,
        }
        self.tasks[task_id] = row
        return row

    def _ensure_callsign(
        self, callsign: str, role: str, *, position: Optional[int] = None
    ) -> dict[str, Any]:
        if role == "worker":
            role = "hidden-worker"
        if role not in {"shotcaller", "champion", "hidden-worker"}:
            raise StorageRefusal("malformed_input", "callsign role is unsupported")
        known = self.callsigns.get(callsign)
        if known:
            if known["pool_role"] != role:
                raise StorageRefusal("identity_collision", "callsign role collides")
            if position is not None:
                if known["pool_position"] is not None and known["pool_position"] != position:
                    raise StorageRefusal("identity_collision", "callsign order collides")
                known["pool_position"] = position
            return known
        row = {
            "callsign": callsign,
            "pool_role": role,
            "enabled": 1,
            "pool_position": position,
            "last_released_at": None,
        }
        self.callsigns[callsign] = row
        return row

    def _parse_roster(self, entry: dict[str, Any]) -> None:
        if set(entry) - {"artifact_id", "status", "updates", "_sort_path"}:
            raise StorageRefusal("malformed_input", "roster manifest entry has unsupported fields")
        artifact_id = entry["artifact_id"]
        status_path, status_data = self._read(entry["status"])
        status = _decode_object(status_data, f"roster {artifact_id} status")
        callsign = _text(status.get("callsign"), "status.callsign")
        if status_path.parent.name != callsign:
            raise StorageRefusal("identity_collision", "roster callsign does not match its source directory")
        source_role = _text(status.get("role"), "status.role").lower()
        role = "hidden-worker" if source_role == "worker" else source_role
        self._ensure_callsign(callsign, role)
        agent_id = _text(status.get("thread_id"), "status.thread_id")
        if agent_id in self.agents or callsign in self.agent_by_callsign:
            raise StorageRefusal("identity_collision", "agent or active callsign identity is duplicated")
        status_value = _text(status.get("status"), "status.status").lower()
        if status_value not in LIFECYCLE_STATES:
            raise StorageRefusal("malformed_input", "status lifecycle state is unsupported")
        updated_at = _timestamp(status.get("updated_at"), "status.updated_at")
        update_text = _text(status.get("update"), "status.update")
        next_action = _text(status.get("next"), "status.next")
        blocker = status.get("blocker")
        if blocker is not None:
            blocker = _text(blocker, "status.blocker")
        owner_callsign = status.get("shotcaller")
        if role == "shotcaller":
            if owner_callsign is not None:
                raise StorageRefusal("identity_collision", "Shotcaller status cannot have a Shotcaller owner")
        else:
            owner_callsign = _text(owner_callsign, "status.shotcaller")
            if status_path.parent.parent.name != "champions" or status_path.parent.parent.parent.name != owner_callsign:
                raise StorageRefusal("identity_collision", "agent owner does not match its Roster path")
        task_id = status.get("task_id")
        repository = status.get("repository")
        issue = status.get("issue")
        branch = status.get("branch")
        worktree = status.get("worktree")
        project_id = None
        if role == "champion":
            if not THREAD_ID.fullmatch(agent_id):
                raise StorageRefusal("identity_collision", "Champion thread_id is not an exact UUID")
            backend = status.get("backend")
            if backend not in {"herdr", "tmux"}:
                raise StorageRefusal("identity_collision", "Champion backend is unsupported")
            address = _text(status.get("address"), "status.address")
            pattern = HERDR_ADDRESS if backend == "herdr" else TMUX_ADDRESS
            if not pattern.fullmatch(address):
                raise StorageRefusal("identity_collision", "Champion address does not match its backend")
            routing_name = status.get("routing_name")
            display_agent = status.get("display_agent")
            if (routing_name is None) != (display_agent is None):
                raise StorageRefusal("identity_collision", "routing_name and display_agent must be paired")
            if routing_name is not None and routing_name != callsign.lower():
                raise StorageRefusal("identity_collision", "routing_name must equal the lowercase callsign")
            task_id = _text(task_id, "status.task_id")
            quartet = (repository, issue, branch, worktree)
            if any(item is None for item in quartet):
                if not all(item is None for item in quartet):
                    raise StorageRefusal("identity_collision", "repository identity must be all exact or all null")
            else:
                repository = _text(repository, "status.repository")
                _integer(issue, "status.issue", minimum=1)
                _text(branch, "status.branch")
                worktree = _text(worktree, "status.worktree")
                if not Path(worktree).is_absolute():
                    raise StorageRefusal("identity_collision", "status.worktree must be absolute")
                project_id = self._ensure_project(repository, updated_at)
            self._ensure_task(
                task_id,
                updated_at,
                project_id=project_id,
                summary=_text(status.get("task"), "status.task"),
            )
        elif task_id is not None:
            task_id = _text(task_id, "status.task_id")
            self._ensure_task(task_id, updated_at, project_id=None, summary=_text(status.get("task"), "status.task"))
        updates_value = entry.get("updates")
        update_rows: list[dict[str, Any]] = []
        combined = bytearray(status_data)
        if updates_value is not None:
            _, updates_data = self._read(updates_value)
            combined.extend(b"\0")
            combined.extend(updates_data)
            if updates_data and not updates_data.endswith(b"\n"):
                raise StorageRefusal("malformed_input", "JSONL transition log must end with a newline")
            offset = 0
            for line_number, raw in enumerate(updates_data.splitlines(keepends=True), 1):
                if not raw.endswith(b"\n"):
                    raise StorageRefusal("malformed_input", "JSONL transition log is truncated")
                line_bytes = raw.rstrip(b"\r\n")
                transition = _decode_object(line_bytes, f"roster {artifact_id} update {line_number}")
                at = _timestamp(transition.get("at"), "transition.at")
                event_status = _text(transition.get("status"), "transition.status").lower()
                if event_status not in LIFECYCLE_STATES:
                    raise StorageRefusal("malformed_input", "transition status is unsupported")
                event_update = _text(transition.get("update"), "transition.update")
                version = line_number
                event_id = f"agent:{agent_id}:{version}"
                if event_id in self.events:
                    raise StorageRefusal("identity_collision", "event identity is duplicated")
                extra = {key: value for key, value in transition.items() if key not in {"at", "status", "update"}}
                event = {
                    "event_id": event_id,
                    "agent_id": agent_id,
                    "task_id": None,
                    "entity_version": version,
                    "event_type": "legacy_transition",
                    "status": event_status,
                    "update_text": event_update,
                    "occurred_at": at,
                    "detail_json": _stable_json(extra),
                }
                self.events[event_id] = event
                identity = (callsign, event_status, at, event_update)
                if identity in self.event_by_identity:
                    raise StorageRefusal("identity_collision", "transition identity is ambiguous")
                self.event_by_identity[identity] = event_id
                synthetic_alias = _legacy_digest(artifact_id, offset, line_bytes.decode("utf-8"))
                self.event_aliases[synthetic_alias] = event_id
                update_rows.append(
                    {
                        "offset": offset,
                        "line": line_bytes.decode("utf-8"),
                        "event_id": event_id,
                        "identity": identity,
                    }
                )
                offset += len(raw)
        if role != "shotcaller" and not update_rows:
            raise StorageRefusal("malformed_input", "Champion or worker transition history is required")
        if update_rows:
            latest = self.events[update_rows[-1]["event_id"]]
            if (latest["status"], latest["occurred_at"], latest["update_text"]) != (
                status_value,
                updated_at,
                update_text,
            ):
                raise StorageRefusal("snapshot_event_mismatch", "status and latest transition do not match")
        version = len(update_rows) if update_rows else 1
        known_status_keys = {
            "callsign", "role", "shotcaller", "kind", "address", "thread_id", "backend", "task_id",
            "repository", "issue", "branch", "worktree", "task", "status", "updated_at", "update",
            "blocker", "next", "routing_name", "display_agent",
        }
        row = {
            "agent_id": agent_id,
            "callsign": callsign,
            "role": role,
            "shotcaller_agent_id": owner_callsign,
            "task_id": task_id,
            "kind": _text(status.get("kind"), "status.kind"),
            "address": _text(status.get("address"), "status.address"),
            "thread_id": agent_id,
            "backend": status.get("backend"),
            "routing_name": status.get("routing_name"),
            "display_agent": status.get("display_agent"),
            "repository": repository,
            "issue": issue,
            "branch": branch,
            "worktree": worktree,
            "status": status_value,
            "version": version,
            "updated_at": updated_at,
            "update_text": update_text,
            "blocker": blocker,
            "next_action": next_action,
            "metadata_json": _stable_json({key: value for key, value in status.items() if key not in known_status_keys}),
            "retired_at": None,
        }
        self.agents[agent_id] = row
        self.agent_by_callsign[callsign] = agent_id
        self.roster_lines[callsign] = {line["offset"]: line for line in update_rows}
        if task_id is not None:
            task = self.tasks[task_id]
            if task["current_owner_agent_id"] not in {None, agent_id}:
                raise StorageRefusal("identity_collision", "task has multiple current agent owners")
            task["current_owner_agent_id"] = agent_id
        self._register_artifact(artifact_id, "roster", bytes(combined), 1 + len(update_rows))

    def _parse_visible_pool(self, entry: dict[str, Any]) -> None:
        if set(entry) != {"artifact_id", "path", "_sort_path"}:
            raise StorageRefusal("malformed_input", "visible pool manifest entry has unsupported fields")
        artifact_id = entry["artifact_id"]
        _, data = self._read(entry["path"])
        value = _decode_object(data, f"visible callsign pool {artifact_id}")
        if set(value) != {"available", "in_use"}:
            raise StorageRefusal("malformed_input", "visible callsign pool fields are unsupported")
        available = value.get("available")
        in_use = value.get("in_use")
        if not isinstance(available, dict) or not isinstance(in_use, dict):
            raise StorageRefusal("malformed_input", "visible callsign pool schema is invalid")
        if set(available) != {"shotcaller", "champion"}:
            raise StorageRefusal("malformed_input", "visible callsign available roles are unsupported")
        seen: set[str] = set()
        count = 0
        for role in ("shotcaller", "champion"):
            items = available.get(role)
            if not isinstance(items, list):
                raise StorageRefusal("malformed_input", "visible callsign available roles must be lists")
            for position, raw_callsign in enumerate(items):
                callsign = _text(raw_callsign, "available callsign")
                if callsign in seen or callsign in in_use:
                    raise StorageRefusal("identity_collision", "visible callsign is duplicated or both available and in use")
                seen.add(callsign)
                self._ensure_callsign(callsign, role, position=position)
                count += 1
        for callsign, assignment in in_use.items():
            callsign = _text(callsign, "in-use callsign")
            if callsign in seen or not isinstance(assignment, dict):
                raise StorageRefusal("identity_collision", "visible in-use callsign is duplicated or malformed")
            if set(assignment) - {"role", "task_id", "pending", "record"}:
                raise StorageRefusal("malformed_input", "visible in-use assignment fields are unsupported")
            if "record" in assignment:
                _legacy_visible_record(assignment["record"])
            seen.add(callsign)
            agent_id = self.agent_by_callsign.get(callsign)
            inferred_role = self.agents[agent_id]["role"] if agent_id else assignment.get("role", "champion")
            self._ensure_callsign(callsign, inferred_role)
            count += 1
        self.visible_in_use = dict(in_use)
        self._register_artifact(artifact_id, "visible_callsign_pool", data, count)

    def _parse_hidden_pool(self, entry: dict[str, Any]) -> None:
        if set(entry) != {"artifact_id", "path", "_sort_path"}:
            raise StorageRefusal("malformed_input", "hidden pool manifest entry has unsupported fields")
        artifact_id = entry["artifact_id"]
        _, data = self._read(entry["path"])
        value = _decode_object(data, f"hidden worker pool {artifact_id}")
        if (
            set(value) != {"schema", "available", "active"}
            or value.get("schema") != 1
            or not isinstance(value.get("available"), list)
            or not isinstance(value.get("active"), dict)
        ):
            raise StorageRefusal("malformed_input", "hidden worker pool schema is invalid")
        seen: set[str] = set()
        for position, raw_callsign in enumerate(value["available"]):
            callsign = _text(raw_callsign, "hidden available callsign")
            if callsign in seen or callsign in value["active"]:
                raise StorageRefusal("identity_collision", "hidden callsign is duplicated or both available and active")
            seen.add(callsign)
            self._ensure_callsign(callsign, "hidden-worker", position=position)
        for callsign, assignment in value["active"].items():
            callsign = _text(callsign, "hidden active callsign")
            if callsign in seen or not isinstance(assignment, dict):
                raise StorageRefusal("identity_collision", "hidden worker assignment is duplicated or malformed")
            if set(assignment) != {
                "callsign", "role", "owner", "worker_id", "model", "effort", "routing_reason", "status"
            }:
                raise StorageRefusal("malformed_input", "hidden worker assignment fields are unsupported")
            if assignment["callsign"] != callsign or assignment["role"] != "hidden-worker":
                raise StorageRefusal("identity_collision", "hidden worker assignment identity conflicts")
            seen.add(callsign)
            self._ensure_callsign(callsign, "hidden-worker")
            agent_id = _text(assignment.get("worker_id"), "hidden worker_id")
            if agent_id in self.agents or callsign in self.agent_by_callsign:
                raise StorageRefusal("identity_collision", "hidden worker identity collides")
            owner_callsign = _text(assignment.get("owner"), "hidden owner")
            status = _text(assignment.get("status"), "hidden status").lower()
            if status not in LIFECYCLE_STATES:
                raise StorageRefusal("malformed_input", "hidden worker status is unsupported")
            at = self.manifest["captured_at"]
            row = {
                "agent_id": agent_id,
                "callsign": callsign,
                "role": "hidden-worker",
                "shotcaller_agent_id": owner_callsign,
                "task_id": None,
                "kind": "hidden-worker",
                "address": None,
                "thread_id": None,
                "backend": None,
                "routing_name": None,
                "display_agent": None,
                "repository": None,
                "issue": None,
                "branch": None,
                "worktree": None,
                "status": status,
                "version": 1,
                "updated_at": at,
                "update_text": "imported hidden-worker assignment",
                "blocker": None,
                "next_action": "Reconcile imported hidden-worker assignment",
                "metadata_json": _stable_json(assignment),
                "retired_at": None,
            }
            self.agents[agent_id] = row
            self.agent_by_callsign[callsign] = agent_id
            self.leases[callsign] = {
                "callsign": callsign,
                "agent_id": agent_id,
                "launch_attempt_id": None,
                "reserved_at": at,
            }
        self._register_artifact(artifact_id, "hidden_worker_pool", data, len(seen))

    def _parse_pending_launch(self, entry: dict[str, Any]) -> None:
        if set(entry) != {"artifact_id", "path", "_sort_path"}:
            raise StorageRefusal("malformed_input", "pending launch manifest entry has unsupported fields")
        artifact_id = entry["artifact_id"]
        _, data = self._read(entry["path"])
        value = _decode_object(data, f"pending launch {artifact_id}")
        allowed = {
            "schema", "task_id", "callsign", "routing_name", "display_agent", "address", "pool", "record",
            "herdr_session", "attempt_id", "phase", "repository", "issue", "branch", "worktree",
            "resume_thread", "started_at", "runtime_generation", "observed_runtime_generation",
        }
        if set(value) - allowed or value.get("schema") != 1:
            raise StorageRefusal("malformed_input", "pending launch schema or fields are unsupported")
        task_id = _text(value.get("task_id"), "pending task_id")
        callsign = _text(value.get("callsign"), "pending callsign")
        attempt_id = _text(value.get("attempt_id"), "pending attempt_id")
        if attempt_id in self.launch_attempts:
            raise StorageRefusal("identity_collision", "launch attempt is duplicated")
        phase = _text(value.get("phase"), "pending phase")
        if phase not in {"reserved", "started"}:
            raise StorageRefusal("malformed_input", "pending launch phase is unsupported")
        repository = value.get("repository")
        project_id = None
        if repository is not None:
            repository = _text(repository, "pending repository")
            project_id = self._ensure_project(repository, self.manifest["captured_at"])
        self._ensure_task(task_id, self.manifest["captured_at"], project_id=project_id, summary=task_id)
        callsign_row = self.callsigns.get(callsign)
        if callsign_row is None:
            raise StorageRefusal("identity_collision", "pending launch callsign is not in the visible pool")
        row = {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "callsign": callsign,
            "phase": phase,
            "routing_name": _text(value.get("routing_name"), "pending routing_name"),
            "display_agent": _text(value.get("display_agent"), "pending display_agent"),
            "address": _text(value.get("address"), "pending address"),
            "pool": _text(value.get("pool"), "pending pool"),
            "record_locator": _text(value.get("record"), "pending record"),
            "runtime_generation": value.get("runtime_generation") or value.get("observed_runtime_generation"),
            "started_at": value.get("started_at"),
            "metadata_json": _stable_json({key: item for key, item in value.items() if key not in allowed - {"herdr_session", "resume_thread"}}),
        }
        if row["routing_name"] != callsign.lower():
            raise StorageRefusal("identity_collision", "pending routing_name must equal the lowercase callsign")
        self.launch_attempts[attempt_id] = row
        if callsign in self.leases:
            raise StorageRefusal("identity_collision", "callsign has multiple live leases")
        self.leases[callsign] = {
            "callsign": callsign,
            "agent_id": None,
            "launch_attempt_id": attempt_id,
            "reserved_at": value.get("started_at") or self.manifest["captured_at"],
        }
        self._register_artifact(artifact_id, "pending_launch", data, 1)

    def _event_for_candidate(self, scope_id: str, candidate: dict[str, Any]) -> str:
        event_id = _text(candidate.get("event_id"), "watcher candidate event_id")
        event_kind = _text(candidate.get("event"), "watcher candidate event")
        callsign = _text(candidate.get("callsign"), "watcher candidate callsign")
        shotcaller = _text(candidate.get("shotcaller"), "watcher candidate shotcaller")
        status = _text(candidate.get("status"), "watcher candidate status")
        at = _timestamp(candidate.get("at"), "watcher candidate at")
        update = _text(candidate.get("update"), "watcher candidate update")
        if event_kind in {"champion-update", "worker-update"}:
            canonical = self.event_by_identity.get((callsign, status, at, update))
            if canonical is None:
                raise StorageRefusal("unknown_consumer", "watcher event does not match imported durable history")
            source_path = _text(candidate.get("source_path"), "watcher candidate source_path")
            offset = _integer(candidate.get("source_offset"), "watcher candidate source_offset")
            match = self.roster_lines.get(callsign, {}).get(offset)
            if match is None or match["event_id"] != canonical:
                raise StorageRefusal("ordering_mismatch", "watcher source offset does not match imported event order")
            if _legacy_digest(source_path, offset, match["line"]) != event_id:
                raise StorageRefusal("identity_collision", "watcher legacy event digest does not match its source")
        elif event_kind == "champion_stalled" and status == "champion_stalled":
            agent_id = self.agent_by_callsign.get(callsign)
            if agent_id is None:
                raise StorageRefusal("unknown_consumer", "watcher reconciliation event references an unknown agent")
            canonical = f"watcher:{scope_id}:{event_id}"
            if canonical not in self.events:
                self.events[canonical] = {
                    "event_id": canonical,
                    "agent_id": agent_id,
                    "task_id": None,
                    "entity_version": self.agents[agent_id]["version"],
                    "event_type": "watcher_reconciliation",
                    "status": status,
                    "update_text": update,
                    "occurred_at": at,
                    "detail_json": _stable_json({"scope_id": scope_id}),
                }
        else:
            raise StorageRefusal("malformed_input", "watcher event type is unsupported")
        prior = self.event_aliases.get(event_id)
        if prior not in {None, canonical}:
            raise StorageRefusal("identity_collision", "legacy event alias is ambiguous")
        self.event_aliases[event_id] = canonical
        recipient = self.agent_by_callsign.get(shotcaller)
        if recipient is None:
            raise StorageRefusal("unknown_consumer", "watcher delivery recipient is unknown")
        return canonical

    def _parse_watcher(self, entry: dict[str, Any]) -> None:
        base_fields = {"artifact_id", "path", "_sort_path"}
        allowed_fields = base_fields | {
            "retired_cursors",
            "retired_unbound_receipts",
        }
        if not base_fields <= set(entry) or set(entry) - allowed_fields:
            raise StorageRefusal("malformed_input", "watcher manifest entry has unsupported fields")
        artifact_id = entry["artifact_id"]
        _, data = self._read(entry["path"])
        value = _decode_object(data, f"watcher state {artifact_id}")
        expected_fields = {
            "schema", "enabled", "allow_stop_once", "stop_blocked", "generation", "initialized",
            "last_active", "offsets", "seen", "user_message_generation", "wait_active", "wait_generation",
            "wait_pid", "wait_process_start", "pending_events", "delivered_events", "last_event_id",
            "reconciliation",
        }
        if set(value) != expected_fields or value.get("schema") != 2:
            raise StorageRefusal("malformed_input", "watcher state schema or fields are unsupported")
        scope_id = artifact_id
        offsets = value["offsets"]
        seen = value["seen"]
        pending = value["pending_events"]
        delivered = value["delivered_events"]
        reconciliation = value["reconciliation"]
        if not all(isinstance(item, expected) for item, expected in (
            (offsets, dict), (seen, list), (pending, dict), (delivered, dict), (reconciliation, dict)
        )):
            raise StorageRefusal("malformed_input", "watcher coordination collections are malformed")
        if set(pending) & set(delivered):
            raise StorageRefusal("identity_collision", "watcher event is both pending and delivered")
        if not isinstance(value["last_active"], list):
            raise StorageRefusal("malformed_input", "watcher last_active must be a list")
        last_active_paths = [
            _text(item, "watcher last_active path") for item in value["last_active"]
        ]
        if len(last_active_paths) != len(set(last_active_paths)):
            raise StorageRefusal(
                "identity_collision", "watcher last_active contains a duplicate Roster"
            )
        seen_events = [_text(item, "watcher seen event") for item in seen]
        if len(seen_events) != len(set(seen_events)):
            raise StorageRefusal("identity_collision", "watcher seen contains a duplicate event")
        value = dict(value)
        value["last_active"] = last_active_paths
        value["seen"] = seen_events
        retired = self._retired_watcher_cursors(entry, value)
        retired_sources = {item["source_path"] for item in retired}
        retired_status_paths = {item["status_path"] for item in retired}
        retired_event_ids = {
            event_id for item in retired for event_id in item["legacy_event_ids"]
        }
        active_agent_ids: list[str] = []
        for active_path in last_active_paths:
            if active_path in retired_status_paths:
                continue
            path = Path(active_path)
            callsign = path.parent.name if path.name == "status.json" else path.name
            agent_id = self.agent_by_callsign.get(callsign)
            if agent_id is None:
                raise StorageRefusal("unknown_consumer", "watcher last_active references an unknown Roster")
            active_agent_ids.append(agent_id)
        seen = seen_events
        wait_pid = value["wait_pid"]
        wait_process_start = value["wait_process_start"]
        if wait_pid is not None:
            _integer(wait_pid, "watcher wait_pid", minimum=2)
            _text(wait_process_start, "watcher wait_process_start")
        elif wait_process_start is not None:
            raise StorageRefusal("malformed_input", "watcher wait process identity is incomplete")
        if value["wait_active"] and wait_pid is None:
            raise StorageRefusal("malformed_input", "active watcher wait requires exact process identity")
        metadata: dict[str, Any] = {"last_active_agent_ids": active_agent_ids}
        if retired:
            metadata["retired_watcher_cursors"] = [
                {
                    key: item[key]
                    for key in (
                        "source_path",
                        "expected_next_offset",
                        "retained_status_artifact_id",
                        "retained_updates_artifact_id",
                        "expected_status_sha256",
                        "expected_updates_sha256",
                        "cursor_event_count",
                        "archived_event_count",
                        "seen_count",
                        "delivered_count",
                        "was_last_active",
                        "was_last_event",
                        "disposition",
                    )
                }
                for item in retired
            ]
        last_event_id = value["last_event_id"]
        active_last_event_id = None if last_event_id in retired_event_ids else last_event_id
        scope_row = {
            "scope_id": scope_id,
            "schema_version": 2,
            "enabled": _boolean(value["enabled"], "watcher.enabled"),
            "allow_stop_once": _boolean(value["allow_stop_once"], "watcher.allow_stop_once"),
            "stop_blocked": _boolean(value["stop_blocked"], "watcher.stop_blocked"),
            "generation": _integer(value["generation"], "watcher.generation"),
            "initialized": _boolean(value["initialized"], "watcher.initialized"),
            "user_message_generation": _integer(
                value["user_message_generation"], "watcher.user_message_generation"
            ),
            "wait_active": _boolean(value["wait_active"], "watcher.wait_active"),
            "wait_generation": _integer(value["wait_generation"], "watcher.wait_generation"),
            "wait_pid": value["wait_pid"],
            "wait_process_start": value["wait_process_start"],
            "last_event_id": active_last_event_id,
            "metadata_json": _stable_json(metadata),
        }
        self.rows["watcher_scopes"].append(scope_row)
        for original_source, next_offset in sorted(offsets.items()):
            if original_source in retired_sources:
                continue
            callsign = Path(original_source).parent.name
            agent_id = self.agent_by_callsign.get(callsign)
            if agent_id is None:
                raise StorageRefusal("unknown_consumer", "watcher cursor references an unknown Roster")
            self.rows["watcher_cursors"].append(
                {
                    "scope_id": scope_id,
                    "source_id": f"agent:{agent_id}:events",
                    "next_offset": _integer(next_offset, "watcher cursor offset"),
                }
            )
            for line in self.roster_lines.get(callsign, {}).values():
                alias = _legacy_digest(original_source, line["offset"], line["line"])
                prior = self.event_aliases.get(alias)
                if prior not in {None, line["event_id"]}:
                    raise StorageRefusal("identity_collision", "watcher cursor legacy digest is ambiguous")
                self.event_aliases[alias] = line["event_id"]
        retired_receipt_ids, retired_receipts = self._retired_watcher_receipts(
            entry, value, data, retired_event_ids
        )
        if retired_receipts is not None:
            metadata["retired_unbound_receipts"] = retired_receipts
            scope_row["metadata_json"] = _stable_json(metadata)
        pending_candidates: dict[str, dict[str, Any]] = {}
        for legacy_id, raw_candidate in pending.items():
            if not isinstance(raw_candidate, dict) or raw_candidate.get("event_id") != legacy_id:
                raise StorageRefusal("malformed_input", "pending watcher event is malformed")
            candidate = dict(raw_candidate)
            canonical = self._event_for_candidate(scope_id, candidate)
            pending_candidates[legacy_id] = candidate
            recipient = self.agent_by_callsign[candidate["shotcaller"]]
            key = (canonical, recipient)
            if key in self.deliveries:
                raise StorageRefusal("identity_collision", "delivery identity is duplicated")
            self.deliveries[key] = {
                "event_id": canonical,
                "recipient_agent_id": recipient,
                "state": "pending",
                "attempt_count": 0,
                "claim_token": None,
                "claim_expires_at": None,
                "accepted_at": None,
                "acknowledged_at": None,
                "failed_at": None,
                "last_error": None,
            }
        for legacy_id, receipt in delivered.items():
            if legacy_id in retired_event_ids or legacy_id in retired_receipt_ids:
                continue
            if not isinstance(receipt, dict) or not isinstance(receipt.get("channel"), str):
                raise StorageRefusal("malformed_input", "delivered watcher receipt is malformed")
            canonical = self.event_aliases.get(legacy_id)
            if canonical is None:
                raise StorageRefusal("unknown_consumer", "delivered watcher receipt has no imported durable event")
            event = self.events[canonical]
            agent = self.agents[event["agent_id"]]
            owner_callsign = agent["shotcaller_agent_id"]
            recipient = self.agent_by_callsign.get(owner_callsign) if owner_callsign else None
            if recipient is None:
                raise StorageRefusal("unknown_consumer", "delivered watcher receipt has no exact recipient")
            key = (canonical, recipient)
            if key in self.deliveries:
                raise StorageRefusal("identity_collision", "delivery identity is duplicated")
            self.deliveries[key] = {
                "event_id": canonical,
                "recipient_agent_id": recipient,
                "state": "superseded" if receipt["channel"] == "superseded" else "accepted",
                "attempt_count": 1,
                "claim_token": None,
                "claim_expires_at": None,
                "accepted_at": self.manifest["captured_at"],
                "acknowledged_at": None,
                "failed_at": None,
                "last_error": None,
            }
        for legacy_id in seen:
            if legacy_id in retired_event_ids or legacy_id in retired_receipt_ids:
                continue
            if legacy_id not in self.event_aliases:
                raise StorageRefusal("unknown_consumer", "watcher seen event has no imported durable event")
            self.rows["watcher_seen"].append({"scope_id": scope_id, "legacy_event_id": legacy_id})
        if active_last_event_id is not None and active_last_event_id not in self.event_aliases:
            raise StorageRefusal("unknown_consumer", "watcher last_event_id has no imported durable event")
        for record, observation in sorted(reconciliation.items()):
            if not isinstance(observation, dict):
                raise StorageRefusal("malformed_input", "watcher reconciliation observation is malformed")
            callsign = Path(record).name
            agent_id = self.agent_by_callsign.get(callsign)
            if agent_id is None:
                raise StorageRefusal("unknown_consumer", "watcher reconciliation references an unknown agent")
            self.rows["runtime_reconciliation"].append(
                {
                    "scope_id": scope_id,
                    "agent_id": agent_id,
                    "condition": _text(observation.get("condition"), "reconciliation.condition"),
                    "consecutive_count": _integer(observation.get("count"), "reconciliation.count", minimum=1),
                    "record_updated_at": observation.get("record_updated_at"),
                    "evidence_json": _stable_json(observation.get("evidence", {})),
                }
            )
        self._register_artifact(
            artifact_id,
            "watcher_state",
            data,
            1 + len(offsets) + len(seen) + len(pending) + len(delivered) + len(reconciliation),
        )

    def _parse_relay(self, entry: dict[str, Any]) -> None:
        if set(entry) != {"artifact_id", "path", "_sort_path"}:
            raise StorageRefusal("malformed_input", "relay manifest entry has unsupported fields")
        artifact_id = entry["artifact_id"]
        _, data = self._read(entry["path"])
        value = _decode_object(data, f"Lead relay state {artifact_id}")
        if set(value) != {"delivered"} or not isinstance(value["delivered"], list):
            raise StorageRefusal("malformed_input", "Lead relay state is malformed")
        seen: set[str] = set()
        for order, raw_digest in enumerate(value["delivered"]):
            digest = _text(raw_digest, "Lead relay digest")
            if digest in seen:
                raise StorageRefusal("identity_collision", "Lead relay digest is duplicated")
            seen.add(digest)
            self.rows["relay_receipts"].append(
                {"scope_id": artifact_id, "digest": digest, "source_order": order}
            )
        self._register_artifact(artifact_id, "lead_relay_state", data, len(seen))

    def _parse_resources(self, entry: dict[str, Any]) -> None:
        if set(entry) != {"artifact_id", "path", "_sort_path"}:
            raise StorageRefusal("malformed_input", "resource manifest entry has unsupported fields")
        artifact_id = entry["artifact_id"]
        _, data = self._read(entry["path"])
        value = _decode_object(data, f"resource registry {artifact_id}")
        if (
            set(value) - {"schema", "resources", "shared_agent_chrome"}
            or value.get("schema") != 1
            or not isinstance(value.get("resources"), dict)
        ):
            raise StorageRefusal("malformed_input", "resource registry schema is invalid")
        count = 0
        resource_ids: set[str] = set()
        for resource_id, assignment in sorted(value["resources"].items()):
            resource_id = _text(resource_id, "resource_id")
            if resource_id in resource_ids or not isinstance(assignment, dict):
                raise StorageRefusal("identity_collision", "resource identity is duplicated or malformed")
            if set(assignment) - {
                "kind", "task_id", "owner", "endpoint", "generation", "pid", "process_start"
            }:
                raise StorageRefusal("malformed_input", "resource assignment fields are unsupported")
            resource_ids.add(resource_id)
            task_id = _text(assignment.get("task_id"), "resource task_id")
            self._ensure_task(task_id, self.manifest["captured_at"], project_id=None, summary=task_id)
            owner_callsign = _text(assignment.get("owner"), "resource owner")
            owner_agent = self.agent_by_callsign.get(owner_callsign)
            if owner_agent is None:
                raise StorageRefusal("unknown_consumer", "resource owner is not an imported agent")
            kind = assignment.get("kind", "process")
            if kind == "process":
                _integer(assignment.get("pid"), "resource pid", minimum=2)
                _text(assignment.get("process_start"), "resource process_start")
            row = {
                "resource_id": resource_id,
                "task_id": task_id,
                "owner_agent_id": owner_agent,
                "kind": _text(kind, "resource kind"),
                "endpoint": _text(assignment.get("endpoint"), "resource endpoint"),
                "generation": _text(assignment.get("generation"), "resource generation"),
                "process_pid": assignment.get("pid"),
                "process_start": assignment.get("process_start"),
                "state": "active",
                "metadata_json": _stable_json(assignment),
            }
            self.rows["resource_leases"].append(row)
            count += 1
        chrome = value.get("shared_agent_chrome")
        if chrome is not None:
            if not isinstance(chrome, dict) or not isinstance(chrome.get("owners"), list):
                raise StorageRefusal("malformed_input", "shared resource registry is malformed")
            for owner in chrome["owners"]:
                if not isinstance(owner, dict):
                    raise StorageRefusal("malformed_input", "shared resource owner is malformed")
                if set(owner) != {"task_id", "owner", "generation"}:
                    raise StorageRefusal("malformed_input", "shared resource owner fields are unsupported")
                task_id = _text(owner.get("task_id"), "shared resource task_id")
                self._ensure_task(task_id, self.manifest["captured_at"], project_id=None, summary=task_id)
                owner_callsign = _text(owner.get("owner"), "shared resource owner")
                owner_agent = self.agent_by_callsign.get(owner_callsign)
                if owner_agent is None:
                    raise StorageRefusal("unknown_consumer", "shared resource owner is not an imported agent")
                generation = _text(owner.get("generation"), "shared resource generation")
                resource_id = f"shared-agent-chrome:{task_id}:{generation}"
                if resource_id in resource_ids:
                    raise StorageRefusal("identity_collision", "shared resource identity is duplicated")
                resource_ids.add(resource_id)
                self.rows["resource_leases"].append(
                    {
                        "resource_id": resource_id,
                        "task_id": task_id,
                        "owner_agent_id": owner_agent,
                        "kind": "shared-agent-chrome",
                        "endpoint": "shared-agent-chrome",
                        "generation": generation,
                        "process_pid": None,
                        "process_start": None,
                        "state": "active",
                        "metadata_json": _stable_json(owner),
                    }
                )
                count += 1
        self._register_artifact(artifact_id, "resource_registry", data, count)

    def _parse_retained(self) -> None:
        for raw in self.manifest["retained_files"]:
            if not isinstance(raw, dict) or set(raw) != {"artifact_id", "class", "path"}:
                raise StorageRefusal("malformed_input", "retained file entry is malformed")
            artifact_id = _artifact_id(raw["artifact_id"])
            if artifact_id in self.artifact_ids:
                raise StorageRefusal("duplicate_artifact", "retained artifact_id collides with canonical input")
            self.artifact_ids.add(artifact_id)
            source_path, data = self._read(raw["path"])
            digest = hashlib.sha256(data).hexdigest()
            retained_class = _text(raw["class"], "retained class")
            self.retained_sources[artifact_id] = {
                "artifact_id": artifact_id,
                "class": retained_class,
                "path": source_path,
                "data": data,
                "digest": digest,
            }
            self.retained.append(
                {
                    "artifact_id": artifact_id,
                    "class": retained_class,
                    "digest": digest,
                    "bytes": len(data),
                }
            )

    def _retired_watcher_cursors(
        self, entry: dict[str, Any], value: dict[str, Any]
    ) -> list[dict[str, Any]]:
        declarations = entry.get("retired_cursors")
        if declarations is None:
            return []
        if not isinstance(declarations, list) or not declarations or len(declarations) > MAX_RECORDS:
            raise StorageRefusal(
                "malformed_input", "retired watcher cursor declarations are missing or exceed the bound"
            )
        offsets = value["offsets"]
        seen = set(value["seen"])
        pending = set(value["pending_events"])
        delivered = set(value["delivered_events"])
        last_active = set(value["last_active"])
        last_event_id = value["last_event_id"]
        normalized: list[dict[str, Any]] = []
        declared_sources: set[str] = set()
        declared_artifacts: set[str] = set()
        declared_event_ids: set[str] = set()
        for declaration in declarations:
            bound = self._bind_retired_watcher_cursor(
                declaration, offsets, declared_sources, declared_artifacts
            )
            archive = self._parse_retired_watcher_archive(bound)
            classified = self._classify_retired_watcher_cursor(
                bound, archive, seen, pending, delivered, last_active, last_event_id,
                declared_event_ids,
            )
            self._register_retired_watcher_binding(bound, classified["archive_event_ids"])
            declared_sources.add(bound["source_path"])
            declared_artifacts.update(bound["artifact_ids"])
            declared_event_ids.update(classified["archive_event_ids"])
            normalized.append(classified)
        return normalized

    def _bind_retired_watcher_cursor(
        self,
        declaration: Any,
        offsets: dict[str, Any],
        declared_sources: set[str],
        declared_artifacts: set[str],
    ) -> dict[str, Any]:
        expected_fields = {
            "source_path",
            "expected_next_offset",
            "retained_status_artifact_id",
            "retained_updates_artifact_id",
            "expected_status_sha256",
            "expected_updates_sha256",
            "disposition",
        }
        if not isinstance(declaration, dict) or set(declaration) != expected_fields:
            raise StorageRefusal(
                "malformed_input", "retired watcher cursor declaration fields are unsupported"
            )
        source_path = _text(declaration["source_path"], "retired cursor source_path")
        source = Path(source_path)
        if (
            not source.is_absolute()
            or str(source) != source_path
            or source.name != "updates.jsonl"
            or any(part in {".", ".."} for part in source.parts)
        ):
            raise StorageRefusal(
                "malformed_input", "retired watcher cursor source path is not canonical"
            )
        if source_path in declared_sources or source_path not in offsets:
            raise StorageRefusal(
                "identity_collision", "retired watcher cursor is duplicated or stale"
            )
        next_offset = _integer(
            declaration["expected_next_offset"], "retired cursor expected_next_offset"
        )
        if offsets[source_path] != next_offset:
            raise StorageRefusal(
                "identity_collision", "retired watcher cursor offset does not match"
            )
        if declaration["disposition"] != "retained_non_active":
            raise StorageRefusal(
                "malformed_input", "retired watcher cursor disposition is unsupported"
            )
        status_artifact_id = _artifact_id(declaration["retained_status_artifact_id"])
        updates_artifact_id = _artifact_id(declaration["retained_updates_artifact_id"])
        artifact_ids = {status_artifact_id, updates_artifact_id}
        if len(artifact_ids) != 2 or artifact_ids & declared_artifacts:
            raise StorageRefusal("identity_collision", "retired watcher cursor artifacts overlap")
        status_source = self.retained_sources.get(status_artifact_id)
        updates_source = self.retained_sources.get(updates_artifact_id)
        if status_source is None or updates_source is None:
            raise StorageRefusal(
                "unknown_consumer", "retired watcher cursor archive pair is not retained"
            )
        retained_status_path = Path(status_source["path"])
        retained_updates_path = Path(updates_source["path"])
        if (
            retained_status_path.name != "status.json"
            or retained_updates_path.name != "updates.jsonl"
        ):
            raise StorageRefusal(
                "identity_collision", "retired watcher cursor archive pair does not match"
            )
        if status_source["class"] not in {"legacy-archive", "legacy-history"} or updates_source[
            "class"
        ] != status_source["class"]:
            raise StorageRefusal(
                "identity_collision", "retired watcher cursor archive classes do not match"
            )
        status_hash = _text(declaration["expected_status_sha256"], "retired cursor status hash")
        updates_hash = _text(
            declaration["expected_updates_sha256"], "retired cursor updates hash"
        )
        if (
            not re.fullmatch(r"[0-9a-f]{64}", status_hash)
            or not re.fullmatch(r"[0-9a-f]{64}", updates_hash)
            or status_hash != status_source["digest"]
            or updates_hash != updates_source["digest"]
        ):
            raise StorageRefusal(
                "identity_collision", "retired watcher cursor archive hash does not match"
            )
        return {
            "source_path": source_path,
            "source": source,
            "next_offset": next_offset,
            "status_artifact_id": status_artifact_id,
            "updates_artifact_id": updates_artifact_id,
            "artifact_ids": artifact_ids,
            "status_hash": status_hash,
            "updates_hash": updates_hash,
            "status_source": status_source,
            "updates_source": updates_source,
            "binding": (
                status_artifact_id,
                updates_artifact_id,
                status_hash,
                updates_hash,
            ),
        }

    def _register_retired_watcher_binding(
        self, bound: dict[str, Any], archive_event_ids: list[str]
    ) -> None:
        source_path = bound["source_path"]
        cursor_key = (source_path, bound["next_offset"])
        prior_binding = self.retired_cursor_bindings.get(cursor_key)
        if prior_binding not in {None, bound["binding"]}:
            raise StorageRefusal(
                "identity_collision", "retired watcher cursor has conflicting scope bindings"
            )
        for artifact_id in bound["artifact_ids"]:
            prior_source = self.retired_artifact_sources.get(artifact_id)
            if prior_source not in {None, source_path}:
                raise StorageRefusal(
                    "identity_collision", "retired watcher cursor artifact is claimed by another source"
                )
        for event_id in archive_event_ids:
            prior_source = self.retired_event_sources.get(event_id)
            if prior_source not in {None, source_path}:
                raise StorageRefusal(
                    "identity_collision", "retired watcher cursor event is claimed by another source"
                )
        self.retired_cursor_bindings[cursor_key] = bound["binding"]
        for artifact_id in bound["artifact_ids"]:
            self.retired_artifact_sources[artifact_id] = source_path
        for event_id in archive_event_ids:
            self.retired_event_sources[event_id] = source_path

    def _parse_retired_watcher_archive(self, bound: dict[str, Any]) -> dict[str, Any]:
        status = _decode_object(bound["status_source"]["data"], "retired watcher cursor status")
        callsign = _text(status.get("callsign"), "retired cursor status.callsign")
        owner = _text(status.get("shotcaller"), "retired cursor status.shotcaller")
        role = _text(status.get("role"), "retired cursor status.role").lower()
        source = bound["source"]
        if (
            role not in {"champion", "worker"}
            or source.parent.name != callsign
            or source.parent.parent.name != "champions"
            or source.parent.parent.parent.name != owner
            or callsign in self.agent_by_callsign
        ):
            raise StorageRefusal(
                "identity_collision", "retired watcher cursor Roster identity does not match"
            )
        owner_id = self.agent_by_callsign.get(owner)
        if owner_id is None or self.agents[owner_id]["role"] != "shotcaller":
            raise StorageRefusal(
                "unknown_consumer", "retired watcher cursor owner is not an active Shotcaller"
            )
        status_value = _text(status.get("status"), "retired cursor status.status").lower()
        if status_value not in LIFECYCLE_STATES:
            raise StorageRefusal(
                "malformed_input", "retired cursor status lifecycle state is unsupported"
            )
        status_updated_at = _timestamp(
            status.get("updated_at"), "retired cursor status.updated_at"
        )
        status_update = _text(status.get("update"), "retired cursor status.update")
        updates_data = bound["updates_source"]["data"]
        next_offset = bound["next_offset"]
        if not updates_data or not updates_data.endswith(b"\n") or next_offset > len(updates_data):
            raise StorageRefusal(
                "identity_collision", "retired watcher cursor archive prefix is unavailable"
            )
        cursor_event_ids: list[str] = []
        archive_event_ids: list[str] = []
        archive_offset = 0
        latest: Optional[tuple[str, str, str]] = None
        prefix_complete = False
        for line_number, raw in enumerate(io.BytesIO(updates_data), 1):
            line = raw.rstrip(b"\r\n")
            transition = _decode_object(line, f"retired watcher cursor update {line_number}")
            event_at = _timestamp(transition.get("at"), "retired transition.at")
            event_status = _text(transition.get("status"), "retired transition.status").lower()
            if event_status not in LIFECYCLE_STATES:
                raise StorageRefusal("malformed_input", "retired transition status is unsupported")
            event_update = _text(transition.get("update"), "retired transition.update")
            latest = (event_status, event_at, event_update)
            event_id = _legacy_digest(
                bound["source_path"], archive_offset, line.decode("utf-8")
            )
            archive_event_ids.append(event_id)
            if archive_offset < next_offset:
                if archive_offset + len(raw) > next_offset:
                    raise StorageRefusal(
                        "identity_collision", "retired watcher cursor offset splits an event"
                    )
                cursor_event_ids.append(event_id)
                prefix_complete = archive_offset + len(raw) == next_offset
            archive_offset += len(raw)
        if latest is None or not prefix_complete or not cursor_event_ids:
            raise StorageRefusal(
                "identity_collision", "retired watcher cursor prefix is incomplete"
            )
        if latest != (status_value, status_updated_at, status_update):
            raise StorageRefusal(
                "snapshot_event_mismatch", "retained Roster status and latest transition do not match"
            )
        return {
            "cursor_event_ids": cursor_event_ids,
            "archive_event_ids": archive_event_ids,
        }

    def _classify_retired_watcher_cursor(
        self,
        bound: dict[str, Any],
        archive: dict[str, list[str]],
        seen: set[str],
        pending: set[str],
        delivered: set[str],
        last_active: set[str],
        last_event_id: Any,
        declared_event_ids: set[str],
    ) -> dict[str, Any]:
        cursor_event_set = set(archive["cursor_event_ids"])
        archive_event_set = set(archive["archive_event_ids"])
        suffix_event_set = archive_event_set - cursor_event_set
        if (
            not cursor_event_set <= seen
            or suffix_event_set & (seen | delivered | pending)
            or last_event_id in suffix_event_set
            or archive_event_set & pending
            or archive_event_set & declared_event_ids
        ):
            raise StorageRefusal(
                "unknown_consumer", "retired watcher cursor has unseen, pending, or ambiguous events"
            )
        status_path = str(bound["source"].with_name("status.json"))
        return {
            "source_path": bound["source_path"],
            "status_path": status_path,
            "expected_next_offset": bound["next_offset"],
            "retained_status_artifact_id": bound["status_artifact_id"],
            "retained_updates_artifact_id": bound["updates_artifact_id"],
            "expected_status_sha256": bound["status_hash"],
            "expected_updates_sha256": bound["updates_hash"],
            "legacy_event_ids": archive["cursor_event_ids"],
            "archive_event_ids": archive["archive_event_ids"],
            "cursor_event_count": len(cursor_event_set),
            "archived_event_count": len(archive_event_set),
            "seen_count": len(archive_event_set & seen),
            "delivered_count": len(archive_event_set & delivered),
            "was_last_active": status_path in last_active,
            "was_last_event": last_event_id in archive_event_set,
            "disposition": "retained_non_active_no_history_or_delivery",
        }

    def _retired_watcher_receipts(
        self,
        entry: dict[str, Any],
        value: dict[str, Any],
        data: bytes,
        retired_event_ids: set[str],
    ) -> tuple[set[str], Optional[dict[str, Any]]]:
        declaration = entry.get("retired_unbound_receipts")
        seen = set(value["seen"])
        delivered = value["delivered_events"]
        pending = set(value["pending_events"])
        known = set(self.event_aliases) | retired_event_ids
        unknown_seen = seen - known
        unknown_delivered = set(delivered) - known
        unknown = unknown_seen | unknown_delivered
        if not unknown:
            if declaration is not None:
                raise StorageRefusal(
                    "identity_collision", "retired watcher receipt declaration is stale"
                )
            return set(), None
        if declaration is None:
            raise StorageRefusal(
                "unknown_consumer", "watcher receipt has no imported durable event"
            )
        if not isinstance(declaration, dict) or set(declaration) != {
            "expected_watcher_sha256",
            "seen_only",
            "delivered",
            "disposition",
        }:
            raise StorageRefusal(
                "malformed_input", "retired watcher receipt declaration fields are unsupported"
            )
        if not entry.get("retired_cursors"):
            raise StorageRefusal(
                "unknown_consumer", "unbound watcher receipts require retired cursor evidence"
            )
        watcher_hash = _text(
            declaration["expected_watcher_sha256"], "retired watcher receipt source hash"
        )
        if (
            not re.fullmatch(r"[0-9a-f]{64}", watcher_hash)
            or watcher_hash != hashlib.sha256(data).hexdigest()
        ):
            raise StorageRefusal(
                "identity_collision", "retired watcher receipt source hash does not match"
            )
        if declaration["disposition"] != "retained_unbound_no_delivery_history":
            raise StorageRefusal(
                "malformed_input", "retired watcher receipt disposition is unsupported"
            )
        raw_seen_only = declaration["seen_only"]
        raw_delivered = declaration["delivered"]
        if (
            not isinstance(raw_seen_only, list)
            or not isinstance(raw_delivered, list)
            or len(raw_seen_only) + len(raw_delivered) > MAX_RECORDS
        ):
            raise StorageRefusal(
                "malformed_input", "retired watcher receipt lists are malformed or exceed the bound"
            )
        declared_seen_only = {
            _text(item, "retired watcher seen-only event") for item in raw_seen_only
        }
        if len(declared_seen_only) != len(raw_seen_only) or any(
            not re.fullmatch(r"[0-9a-f]{64}", item) for item in declared_seen_only
        ):
            raise StorageRefusal(
                "identity_collision", "retired watcher seen-only events are duplicated or malformed"
            )
        declared_delivered: dict[str, dict[str, Any]] = {}
        for item in raw_delivered:
            if not isinstance(item, dict) or set(item) != {
                "legacy_event_id",
                "channel",
                "seen",
            }:
                raise StorageRefusal(
                    "malformed_input", "retired watcher delivered receipt is malformed"
                )
            legacy_id = _text(item["legacy_event_id"], "retired watcher delivered event")
            channel = _text(item["channel"], "retired watcher delivered channel")
            if (
                not re.fullmatch(r"[0-9a-f]{64}", legacy_id)
                or legacy_id in declared_delivered
                or not isinstance(item["seen"], bool)
            ):
                raise StorageRefusal(
                    "identity_collision", "retired watcher delivered receipt is duplicated or malformed"
                )
            declared_delivered[legacy_id] = {
                "channel": channel,
                "seen": item["seen"],
            }
        expected_seen_only = unknown_seen - unknown_delivered
        if declared_seen_only != expected_seen_only or set(declared_delivered) != unknown_delivered:
            raise StorageRefusal(
                "identity_collision", "retired watcher receipt set does not match the source"
            )
        for legacy_id, declared in declared_delivered.items():
            receipt = delivered[legacy_id]
            if (
                not isinstance(receipt, dict)
                or receipt.get("channel") != declared["channel"]
                or (legacy_id in seen) != declared["seen"]
            ):
                raise StorageRefusal(
                    "identity_collision", "retired watcher delivered receipt evidence does not match"
                )
        if unknown & pending or value["last_event_id"] in unknown:
            raise StorageRefusal(
                "unknown_consumer", "retired watcher receipt is pending or is the current last event"
            )
        normalized = {
            "expected_watcher_sha256": watcher_hash,
            "seen_only_count": len(expected_seen_only),
            "delivered_count": len(unknown_delivered),
            "seen_delivered_count": len(unknown_seen & unknown_delivered),
            "classification_sha256": hashlib.sha256(
                _stable_json(declaration).encode("utf-8")
            ).hexdigest(),
            "disposition": "retained_unbound_no_delivery_history",
        }
        return unknown, normalized

    def _resolve_relationships(self) -> None:
        for agent in self.agents.values():
            owner = agent["shotcaller_agent_id"]
            if owner is not None:
                owner_id = self.agent_by_callsign.get(owner)
                if owner_id is None or self.agents[owner_id]["role"] != "shotcaller":
                    raise StorageRefusal("unknown_consumer", "agent owner is not an imported Shotcaller")
                agent["shotcaller_agent_id"] = owner_id
        for agent in self.agents.values():
            if agent["role"] == "shotcaller":
                squad_id = f"squad:{agent['callsign']}"
                self.squads[squad_id] = {
                    "squad_id": squad_id,
                    "shotcaller_agent_id": agent["agent_id"],
                    "state": "active",
                    "version": 1,
                    "updated_at": agent["updated_at"],
                }
        visible_entries = self.manifest["canonical_sources"]["visible_callsign_pools"]
        if len(visible_entries) > 1:
            raise StorageRefusal("identity_collision", "more than one visible callsign pool was supplied")
        hidden_entries = self.manifest["canonical_sources"]["hidden_worker_pools"]
        if len(hidden_entries) > 1:
            raise StorageRefusal("identity_collision", "more than one hidden-worker pool was supplied")
        if visible_entries:
            in_use = getattr(self, "visible_in_use", {})
            for agent in self.agents.values():
                if agent["role"] in {"shotcaller", "champion"} and agent["callsign"] not in in_use:
                    raise StorageRefusal("identity_collision", "visible active agent is absent from the in-use pool")
            for callsign, assignment in in_use.items():
                agent_id = self.agent_by_callsign.get(callsign)
                pending = isinstance(assignment, dict) and assignment.get("pending") is True
                launch = next((item for item in self.launch_attempts.values() if item["callsign"] == callsign), None)
                if agent_id is None and not (pending and launch is not None):
                    raise StorageRefusal("unknown_consumer", "in-use callsign has no exact agent or pending launch")
                legacy_record = None
                if isinstance(assignment, dict) and "record" in assignment:
                    legacy_record = _legacy_visible_record(assignment["record"])
                if agent_id is not None:
                    if callsign in self.leases:
                        raise StorageRefusal("identity_collision", "callsign has multiple live leases")
                    agent = self.agents[agent_id]
                    if legacy_record is not None:
                        record_directory = Path(legacy_record["record_directory"])
                        if record_directory.name != callsign:
                            raise StorageRefusal(
                                "identity_collision",
                                "visible assignment record does not match its active callsign",
                            )
                        if agent["role"] == "champion":
                            owner_id = agent["shotcaller_agent_id"]
                            owner_callsign = self.agents[owner_id]["callsign"]
                            if (
                                record_directory.parent.name != "champions"
                                or record_directory.parent.parent.name != owner_callsign
                            ):
                                raise StorageRefusal(
                                    "identity_collision",
                                    "visible assignment record does not match its active Roster owner",
                                )
                        metadata = json.loads(agent["metadata_json"])
                        if "legacy_visible_assignment" in metadata:
                            raise StorageRefusal(
                                "identity_collision",
                                "visible assignment record metadata collides with Roster metadata",
                            )
                        metadata["legacy_visible_assignment"] = legacy_record
                        agent["metadata_json"] = _stable_json(metadata)
                    self.leases[callsign] = {
                        "callsign": callsign,
                        "agent_id": agent_id,
                        "launch_attempt_id": None,
                        "reserved_at": self.agents[agent_id]["updated_at"],
                    }
                elif legacy_record is not None and launch is not None:
                    if legacy_record["record"] != launch["record_locator"]:
                        raise StorageRefusal(
                            "identity_collision",
                            "visible assignment record does not match its pending launch",
                        )
        elif any(agent["role"] in {"shotcaller", "champion"} for agent in self.agents.values()):
            raise StorageRefusal("unknown_consumer", "visible agents require an explicit visible callsign pool")
        for legacy_id, event_id in sorted(self.event_aliases.items()):
            self.rows["legacy_event_aliases"].append(
                {"legacy_event_id": legacy_id, "event_id": event_id, "source_order": len(self.rows["legacy_event_aliases"])}
            )

    def build(self) -> ImportPlan:
        ordered = self._ordered_entries()
        self._parse_retained()
        # Identity sources first, then coordination sources that reference them.
        dispatch = {
            "rosters": self._parse_roster,
            "visible_callsign_pools": self._parse_visible_pool,
            "hidden_worker_pools": self._parse_hidden_pool,
            "pending_launches": self._parse_pending_launch,
            "watcher_states": self._parse_watcher,
            "lead_relay_states": self._parse_relay,
            "resource_registries": self._parse_resources,
        }
        priority = {
            "rosters": 0,
            "visible_callsign_pools": 1,
            "hidden_worker_pools": 2,
            "pending_launches": 3,
            "watcher_states": 4,
            "lead_relay_states": 5,
            "resource_registries": 6,
        }
        for kind, entry in sorted(ordered, key=lambda item: (priority[item[0]], self._source_order[item[1]["artifact_id"]])):
            dispatch[kind](entry)
        self._resolve_relationships()
        self.rows["projects"] = sorted(self.projects.values(), key=lambda row: row["project_id"])
        self.rows["callsigns"] = sorted(
            self.callsigns.values(),
            key=lambda row: (row["pool_role"], row["pool_position"] is None, row["pool_position"] or 0, row["callsign"]),
        )
        self.rows["tasks"] = sorted(self.tasks.values(), key=lambda row: row["task_id"])
        self.rows["agent_instances"] = sorted(self.agents.values(), key=lambda row: row["agent_id"])
        self.rows["squads"] = sorted(self.squads.values(), key=lambda row: row["squad_id"])
        self.rows["launch_attempts"] = sorted(self.launch_attempts.values(), key=lambda row: row["attempt_id"])
        self.rows["callsign_leases"] = sorted(self.leases.values(), key=lambda row: row["callsign"])
        self.rows["events"] = sorted(self.events.values(), key=lambda row: (row["occurred_at"], row["event_id"]))
        self.rows["deliveries"] = sorted(self.deliveries.values(), key=lambda row: (row["event_id"], row["recipient_agent_id"]))
        total_records = sum(len(items) for items in self.rows.values())
        if total_records > MAX_RECORDS:
            raise StorageRefusal("input_too_large", "import plan exceeds the record bound")
        artifacts = sorted(self.artifacts, key=lambda item: item["source_order"])
        source_digest = hashlib.sha256(
            _stable_json(
                {
                    "artifacts": artifacts,
                    "retained": sorted(self.retained, key=lambda item: item["artifact_id"]),
                    "audit_coverage": AUDIT_COVERAGE,
                }
            ).encode("utf-8")
        ).hexdigest()
        target_collisions = {table: count for table, count in self.target_counts.items() if count}
        report = {
            "schema": "league.import-report.v1",
            "target_schema_version": CURRENT_SCHEMA_VERSION,
            "dry_run": True,
            "applied": False,
            "eligible": not target_collisions,
            "source_digest": source_digest,
            "artifact_counts": {
                kind: sum(1 for item in artifacts if item["kind"] == kind)
                for kind in (
                    "roster",
                    "pending_launch",
                    "watcher_state",
                    "visible_callsign_pool",
                    "hidden_worker_pool",
                    "lead_relay_state",
                    "resource_registry",
                )
            },
            "row_counts": {table: len(self.rows[table]) for table in sorted(self.rows)},
            "ordering": [
                {
                    "artifact_id": item["artifact_id"],
                    "kind": item["kind"],
                    "source_order": item["source_order"],
                    "record_count": item["record_count"],
                }
                for item in artifacts
            ],
            "retained_files": sorted(self.retained, key=lambda item: item["artifact_id"]),
            "audit_coverage": AUDIT_COVERAGE,
            "unknown_consumers": [],
            "target_collisions": target_collisions,
        }
        digest_payload = {
            "target_schema_version": CURRENT_SCHEMA_VERSION,
            "report": report,
            "rows": self.rows,
        }
        report_digest = hashlib.sha256(_stable_json(digest_payload).encode("utf-8")).hexdigest()
        report["report_digest"] = report_digest
        return {
            "report": report,
            "target_schema_version": CURRENT_SCHEMA_VERSION,
            "report_digest": report_digest,
            "source_digest": source_digest,
            "applied_at": self.manifest["captured_at"],
            "artifacts": artifacts,
            "rows": self.rows,
        }


def build_import_plan(
    source_root: Path,
    manifest_path: Path,
    *,
    target_counts: Optional[dict[str, int]] = None,
) -> ImportPlan:
    return ImportPlanner(source_root, manifest_path, target_counts=target_counts).build()
