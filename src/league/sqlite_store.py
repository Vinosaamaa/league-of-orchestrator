"""One SQLite implementation composed over a shared transaction core.

This facade owns connection policy and reviewed migrations; cohesive operation
modules own lifecycle, delivery, import, and export SQL. Callers use the
storage interface or command facade and never SQL.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .sqlite_core import SQLiteTransactionCore
from .sqlite_delivery_ops import claim_delivery as claim_delivery_operation
from .sqlite_delivery_ops import finish_delivery as finish_delivery_operation
from .sqlite_lifecycle_ops import agent_status as agent_status_operation
from .sqlite_lifecycle_ops import release_callsign as release_callsign_operation
from .sqlite_lifecycle_ops import reserve_callsign as reserve_callsign_operation
from .sqlite_lifecycle_ops import resolve_project as resolve_project_operation
from .sqlite_lifecycle_ops import transfer_task_owner as transfer_task_owner_operation
from .sqlite_lifecycle_ops import transition as transition_operation
from . import sqlite_runtime_ops
from .sqlite_transfer_ops import (
    apply_import as apply_import_operation,
    canonical_counts,
    export_bytes as export_operation,
)
from .storage import ConnectionPolicy, FaultInjector, ImportPlan, StorageRefusal
from .storage_types import LIFECYCLE_STATES


WAL_MINIMUM = (3, 51, 3)
CURRENT_SCHEMA_VERSION = 3
DATABASE_NAME = "league.sqlite3"
DEFAULT_BUSY_TIMEOUT_MS = 500
MAX_BUSY_TIMEOUT_MS = 10_000
MAX_EXPORT_RECORDS = 10_000

@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        normalized = "\n".join(" ".join(item.split()) for item in self.statements)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


MIGRATIONS = (
    Migration(
        1,
        "core-identities-events-and-delivery",
        (
            """
            CREATE TABLE schema_migrations (
              version INTEGER PRIMARY KEY,
              name TEXT NOT NULL UNIQUE,
              checksum TEXT NOT NULL,
              applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE projects (
              project_id TEXT PRIMARY KEY,
              repository TEXT NOT NULL UNIQUE,
              state TEXT NOT NULL CHECK (state IN ('active','retired')),
              version INTEGER NOT NULL CHECK (version > 0),
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE callsigns (
              callsign TEXT PRIMARY KEY,
              pool_role TEXT NOT NULL
                CHECK (pool_role IN ('shotcaller','champion','hidden-worker')),
              enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
              pool_position INTEGER CHECK (pool_position >= 0),
              last_released_at TEXT,
              UNIQUE (pool_role, pool_position)
            )
            """,
            f"""
            CREATE TABLE tasks (
              task_id TEXT PRIMARY KEY,
              project_id TEXT REFERENCES projects(project_id),
              summary TEXT NOT NULL,
              state TEXT NOT NULL,
              version INTEGER NOT NULL CHECK (version > 0),
              current_owner_agent_id TEXT REFERENCES agent_instances(agent_id)
                DEFERRABLE INITIALLY DEFERRED,
              current_owner_squad_id TEXT REFERENCES squads(squad_id)
                DEFERRABLE INITIALLY DEFERRED,
              updated_at TEXT NOT NULL,
              CHECK (current_owner_agent_id IS NULL OR current_owner_squad_id IS NULL)
            )
            """,
            f"""
            CREATE TABLE agent_instances (
              agent_id TEXT PRIMARY KEY,
              callsign TEXT NOT NULL REFERENCES callsigns(callsign),
              role TEXT NOT NULL
                CHECK (role IN ('shotcaller','champion','hidden-worker')),
              shotcaller_agent_id TEXT REFERENCES agent_instances(agent_id)
                DEFERRABLE INITIALLY DEFERRED,
              task_id TEXT REFERENCES tasks(task_id) DEFERRABLE INITIALLY DEFERRED,
              kind TEXT NOT NULL,
              address TEXT,
              thread_id TEXT,
              backend TEXT CHECK (backend IS NULL OR backend IN ('herdr','tmux')),
              routing_name TEXT,
              display_agent TEXT,
              repository TEXT,
              issue INTEGER CHECK (issue IS NULL OR issue > 0),
              branch TEXT,
              worktree TEXT,
              status TEXT NOT NULL CHECK (status IN {LIFECYCLE_STATES}),
              version INTEGER NOT NULL CHECK (version > 0),
              updated_at TEXT NOT NULL,
              update_text TEXT NOT NULL,
              blocker TEXT,
              next_action TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{{}}',
              retired_at TEXT,
              CHECK ((routing_name IS NULL) = (display_agent IS NULL))
            )
            """,
            """
            CREATE UNIQUE INDEX ux_live_callsign
              ON agent_instances(callsign) WHERE retired_at IS NULL
            """,
            """
            CREATE TABLE squads (
              squad_id TEXT PRIMARY KEY,
              shotcaller_agent_id TEXT NOT NULL UNIQUE
                REFERENCES agent_instances(agent_id) DEFERRABLE INITIALLY DEFERRED,
              state TEXT NOT NULL CHECK (state IN ('active','retired')),
              version INTEGER NOT NULL CHECK (version > 0),
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE events (
              event_id TEXT PRIMARY KEY,
              agent_id TEXT REFERENCES agent_instances(agent_id),
              task_id TEXT REFERENCES tasks(task_id),
              entity_version INTEGER NOT NULL CHECK (entity_version > 0),
              event_type TEXT NOT NULL,
              status TEXT,
              update_text TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              detail_json TEXT NOT NULL DEFAULT '{}',
              CHECK ((agent_id IS NOT NULL) + (task_id IS NOT NULL) = 1)
            )
            """,
            """
            CREATE UNIQUE INDEX ux_agent_event_version
              ON events(agent_id, entity_version)
              WHERE agent_id IS NOT NULL AND event_type IN
                ('agent_transition','callsign_reserved','callsign_released','legacy_transition')
            """,
            """
            CREATE UNIQUE INDEX ux_task_event_version
              ON events(task_id, entity_version)
              WHERE task_id IS NOT NULL AND event_type='task_owner_transferred'
            """,
            """
            CREATE TABLE legacy_event_aliases (
              legacy_event_id TEXT PRIMARY KEY,
              event_id TEXT NOT NULL REFERENCES events(event_id),
              source_order INTEGER NOT NULL CHECK (source_order >= 0)
            )
            """,
            """
            CREATE TABLE deliveries (
              event_id TEXT NOT NULL REFERENCES events(event_id),
              recipient_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              state TEXT NOT NULL CHECK (
                state IN ('pending','claimed','accepted','acknowledged','failed','superseded')
              ),
              attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
              claim_token TEXT,
              claim_expires_at TEXT,
              accepted_at TEXT,
              acknowledged_at TEXT,
              failed_at TEXT,
              last_error TEXT,
              PRIMARY KEY (event_id, recipient_agent_id),
              CHECK (state != 'claimed' OR claim_token IS NOT NULL)
            )
            """,
            """
            CREATE TABLE assignment_receipts (
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              task_version INTEGER NOT NULL CHECK (task_version > 0),
              owner_agent_id TEXT REFERENCES agent_instances(agent_id),
              owner_squad_id TEXT REFERENCES squads(squad_id),
              received_at TEXT NOT NULL,
              PRIMARY KEY (task_id, task_version),
              CHECK ((owner_agent_id IS NOT NULL) + (owner_squad_id IS NOT NULL) = 1)
            )
            """,
            "CREATE INDEX ix_projects_repository ON projects(repository)",
            "CREATE INDEX ix_deliveries_state ON deliveries(recipient_agent_id,state)",
        ),
    ),
    Migration(
        2,
        "launch-watcher-resource-and-import-domains",
        (
            """
            CREATE TABLE launch_attempts (
              attempt_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              callsign TEXT NOT NULL REFERENCES callsigns(callsign),
              phase TEXT NOT NULL CHECK (phase IN ('reserved','started','failed','activated')),
              routing_name TEXT NOT NULL,
              display_agent TEXT NOT NULL,
              address TEXT NOT NULL,
              pool TEXT NOT NULL,
              record_locator TEXT NOT NULL,
              runtime_generation TEXT,
              started_at TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE callsign_leases (
              callsign TEXT PRIMARY KEY REFERENCES callsigns(callsign),
              agent_id TEXT UNIQUE REFERENCES agent_instances(agent_id),
              launch_attempt_id TEXT UNIQUE REFERENCES launch_attempts(attempt_id),
              reserved_at TEXT NOT NULL,
              CHECK ((agent_id IS NOT NULL) + (launch_attempt_id IS NOT NULL) = 1)
            )
            """,
            """
            CREATE TABLE watcher_scopes (
              scope_id TEXT PRIMARY KEY,
              schema_version INTEGER NOT NULL,
              enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
              allow_stop_once INTEGER NOT NULL CHECK (allow_stop_once IN (0,1)),
              stop_blocked INTEGER NOT NULL CHECK (stop_blocked IN (0,1)),
              generation INTEGER NOT NULL CHECK (generation >= 0),
              initialized INTEGER NOT NULL CHECK (initialized IN (0,1)),
              user_message_generation INTEGER NOT NULL CHECK (user_message_generation >= 0),
              wait_active INTEGER NOT NULL CHECK (wait_active IN (0,1)),
              wait_generation INTEGER NOT NULL CHECK (wait_generation >= 0),
              wait_pid INTEGER,
              wait_process_start TEXT,
              last_event_id TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE watcher_cursors (
              scope_id TEXT NOT NULL REFERENCES watcher_scopes(scope_id),
              source_id TEXT NOT NULL,
              next_offset INTEGER NOT NULL CHECK (next_offset >= 0),
              PRIMARY KEY (scope_id, source_id)
            )
            """,
            """
            CREATE TABLE watcher_seen (
              scope_id TEXT NOT NULL REFERENCES watcher_scopes(scope_id),
              legacy_event_id TEXT NOT NULL REFERENCES legacy_event_aliases(legacy_event_id),
              PRIMARY KEY (scope_id, legacy_event_id)
            )
            """,
            """
            CREATE TABLE runtime_reconciliation (
              scope_id TEXT NOT NULL REFERENCES watcher_scopes(scope_id),
              agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              condition TEXT NOT NULL,
              consecutive_count INTEGER NOT NULL CHECK (consecutive_count > 0),
              record_updated_at TEXT,
              evidence_json TEXT NOT NULL DEFAULT '{}',
              PRIMARY KEY (scope_id, agent_id)
            )
            """,
            """
            CREATE TABLE resource_leases (
              resource_id TEXT PRIMARY KEY,
              task_id TEXT REFERENCES tasks(task_id),
              owner_agent_id TEXT REFERENCES agent_instances(agent_id),
              kind TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              generation TEXT NOT NULL,
              process_pid INTEGER,
              process_start TEXT,
              state TEXT NOT NULL CHECK (state IN ('active','releasing','released','stale')),
              metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE relay_receipts (
              scope_id TEXT NOT NULL,
              digest TEXT NOT NULL,
              source_order INTEGER NOT NULL CHECK (source_order >= 0),
              PRIMARY KEY (scope_id, digest)
            )
            """,
            """
            CREATE TABLE import_runs (
              run_id TEXT PRIMARY KEY,
              report_digest TEXT NOT NULL UNIQUE,
              source_digest TEXT NOT NULL,
              applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE imported_artifacts (
              artifact_id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              digest TEXT NOT NULL,
              record_count INTEGER NOT NULL CHECK (record_count >= 0),
              source_order INTEGER NOT NULL CHECK (source_order >= 0),
              import_run_id TEXT NOT NULL REFERENCES import_runs(run_id)
            )
            """,
            "CREATE INDEX ix_events_occurred ON events(occurred_at,event_id)",
            "CREATE INDEX ix_resources_owner ON resource_leases(owner_agent_id,state)",
        ),
    ),
    Migration(
        3,
        "adapter-runtime-cleanup-and-routing",
        (
            """
            CREATE TABLE runtime_bindings (
              binding_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              harness_kind TEXT NOT NULL,
              backend_kind TEXT NOT NULL,
              session_identity TEXT NOT NULL UNIQUE,
              endpoint_identity TEXT NOT NULL UNIQUE,
              capabilities_json TEXT NOT NULL,
              state TEXT NOT NULL CHECK (state IN ('active','idle','interrupted','closed','failed')),
              version INTEGER NOT NULL CHECK (version > 0),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_receipt_json TEXT NOT NULL DEFAULT '{}',
              CHECK (instr(session_identity, ':') > 1),
              CHECK (instr(endpoint_identity, ':') > 1)
            )
            """,
            """
            CREATE TABLE model_routing_decisions (
              decision_id TEXT PRIMARY KEY,
              subject_kind TEXT NOT NULL,
              subject_id TEXT NOT NULL,
              role TEXT NOT NULL,
              tier TEXT NOT NULL CHECK (tier IN ('COORDINATOR','WORKER_FAST','WORKER_STRONG')),
              model TEXT NOT NULL,
              effort TEXT NOT NULL,
              reason TEXT NOT NULL,
              explicit_model INTEGER NOT NULL CHECK (explicit_model IN (0,1)),
              explicit_effort INTEGER NOT NULL CHECK (explicit_effort IN (0,1)),
              state TEXT NOT NULL CHECK (state IN ('selected','escalated','blocked')),
              escalation_count INTEGER NOT NULL CHECK (escalation_count BETWEEN 0 AND 1),
              prior_decision_id TEXT REFERENCES model_routing_decisions(decision_id),
              failure_class TEXT,
              chosen_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE model_routing_outcomes (
              outcome_id TEXT PRIMARY KEY,
              decision_id TEXT NOT NULL REFERENCES model_routing_decisions(decision_id),
              success INTEGER NOT NULL CHECK (success IN (0,1)),
              corrections INTEGER NOT NULL CHECK (corrections >= 0),
              latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
              cost_microunits INTEGER NOT NULL CHECK (cost_microunits >= 0),
              recorded_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE task_resources (
              resource_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              owner_id TEXT NOT NULL,
              owner_role TEXT NOT NULL CHECK (owner_role IN ('champion','hidden-worker','task')),
              resource_type TEXT NOT NULL,
              lifetime TEXT NOT NULL CHECK (lifetime IN ('task_owned','shared_lease','persistent_retain')),
              expected_identity_json TEXT NOT NULL,
              cleanup_action TEXT NOT NULL,
              adapter_kind TEXT NOT NULL,
              applicable INTEGER NOT NULL CHECK (applicable IN (0,1)),
              applicability_reason TEXT NOT NULL,
              state TEXT NOT NULL CHECK (state IN ('active','released','retained','stale')),
              version INTEGER NOT NULL CHECK (version > 0),
              registered_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE cleanup_obligations (
              cleanup_obligation_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
              owner_id TEXT NOT NULL,
              task_class TEXT NOT NULL CHECK (task_class IN ('analysis','local_git','pr_ci','deployed_service')),
              disposition TEXT NOT NULL CHECK (disposition IN ('completed','rejected','cancelled','failed')),
              cleanup_state TEXT NOT NULL CHECK (cleanup_state IN ('cleanup_pending','awaiting_authority','verifying','planned','executing','blocked','cleanup_completed')),
              required_policy TEXT NOT NULL,
              next_action TEXT NOT NULL,
              version INTEGER NOT NULL CHECK (version > 0),
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE cleanup_operations (
              operation_id TEXT PRIMARY KEY,
              cleanup_obligation_id TEXT NOT NULL REFERENCES cleanup_obligations(cleanup_obligation_id),
              cleanup_revision INTEGER NOT NULL CHECK (cleanup_revision > 0),
              plan_digest TEXT NOT NULL,
              state TEXT NOT NULL CHECK (state IN ('planned','executing','blocked','completed')),
              fence INTEGER NOT NULL CHECK (fence >= 0),
              executor_id TEXT,
              leased_until TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE (cleanup_obligation_id, cleanup_revision)
            )
            """,
            """
            CREATE TABLE cleanup_actions (
              action_id TEXT PRIMARY KEY,
              operation_id TEXT NOT NULL REFERENCES cleanup_operations(operation_id),
              ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
              action_kind TEXT NOT NULL,
              adapter_kind TEXT NOT NULL,
              resource_id TEXT REFERENCES task_resources(resource_id),
              state TEXT NOT NULL CHECK (state IN ('planned','executing','blocked','completed')),
              expected_identity_json TEXT NOT NULL,
              intended_state_json TEXT NOT NULL,
              UNIQUE (operation_id, ordinal)
            )
            """,
            """
            CREATE TABLE cleanup_action_receipts (
              action_id TEXT PRIMARY KEY REFERENCES cleanup_actions(action_id),
              operation_id TEXT NOT NULL REFERENCES cleanup_operations(operation_id),
              fence INTEGER NOT NULL CHECK (fence > 0),
              outcome TEXT NOT NULL CHECK (outcome IN ('applied','already_applied')),
              before_json TEXT NOT NULL,
              after_json TEXT NOT NULL,
              adapter_receipt_json TEXT NOT NULL,
              receipt_hash TEXT NOT NULL,
              recorded_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE teardown_receipts (
              receipt_id TEXT PRIMARY KEY,
              operation_id TEXT NOT NULL UNIQUE REFERENCES cleanup_operations(operation_id),
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              policy_version TEXT NOT NULL,
              receipt_hash TEXT NOT NULL UNIQUE,
              completed_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX ix_runtime_task_state ON runtime_bindings(task_id,state)",
            "CREATE INDEX ix_routing_subject ON model_routing_decisions(subject_kind,subject_id,chosen_at)",
            "CREATE INDEX ix_routing_outcomes ON model_routing_outcomes(decision_id,recorded_at)",
            "CREATE INDEX ix_task_resources_task ON task_resources(task_id,state)",
            "CREATE INDEX ix_cleanup_state ON cleanup_obligations(cleanup_state,updated_at)",
            "CREATE INDEX ix_cleanup_actions ON cleanup_actions(operation_id,ordinal,state)",
        ),
    ),
)


_IMPORT_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": ("project_id", "repository", "state", "version", "updated_at"),
    "callsigns": ("callsign", "pool_role", "enabled", "pool_position", "last_released_at"),
    "tasks": (
        "task_id",
        "project_id",
        "summary",
        "state",
        "version",
        "current_owner_agent_id",
        "current_owner_squad_id",
        "updated_at",
    ),
    "agent_instances": (
        "agent_id",
        "callsign",
        "role",
        "shotcaller_agent_id",
        "task_id",
        "kind",
        "address",
        "thread_id",
        "backend",
        "routing_name",
        "display_agent",
        "repository",
        "issue",
        "branch",
        "worktree",
        "status",
        "version",
        "updated_at",
        "update_text",
        "blocker",
        "next_action",
        "metadata_json",
        "retired_at",
    ),
    "squads": ("squad_id", "shotcaller_agent_id", "state", "version", "updated_at"),
    "launch_attempts": (
        "attempt_id",
        "task_id",
        "callsign",
        "phase",
        "routing_name",
        "display_agent",
        "address",
        "pool",
        "record_locator",
        "runtime_generation",
        "started_at",
        "metadata_json",
    ),
    "callsign_leases": ("callsign", "agent_id", "launch_attempt_id", "reserved_at"),
    "events": (
        "event_id",
        "agent_id",
        "task_id",
        "entity_version",
        "event_type",
        "status",
        "update_text",
        "occurred_at",
        "detail_json",
    ),
    "legacy_event_aliases": ("legacy_event_id", "event_id", "source_order"),
    "deliveries": (
        "event_id",
        "recipient_agent_id",
        "state",
        "attempt_count",
        "claim_token",
        "claim_expires_at",
        "accepted_at",
        "acknowledged_at",
        "failed_at",
        "last_error",
    ),
    "assignment_receipts": (
        "task_id",
        "task_version",
        "owner_agent_id",
        "owner_squad_id",
        "received_at",
    ),
    "watcher_scopes": (
        "scope_id",
        "schema_version",
        "enabled",
        "allow_stop_once",
        "stop_blocked",
        "generation",
        "initialized",
        "user_message_generation",
        "wait_active",
        "wait_generation",
        "wait_pid",
        "wait_process_start",
        "last_event_id",
        "metadata_json",
    ),
    "watcher_cursors": ("scope_id", "source_id", "next_offset"),
    "watcher_seen": ("scope_id", "legacy_event_id"),
    "runtime_reconciliation": (
        "scope_id",
        "agent_id",
        "condition",
        "consecutive_count",
        "record_updated_at",
        "evidence_json",
    ),
    "resource_leases": (
        "resource_id",
        "task_id",
        "owner_agent_id",
        "kind",
        "endpoint",
        "generation",
        "process_pid",
        "process_start",
        "state",
        "metadata_json",
    ),
    "relay_receipts": ("scope_id", "digest", "source_order"),
}

_IMPORT_ORDER = tuple(_IMPORT_COLUMNS)
_EXPORT_TABLES = (
    "schema_migrations",
    "projects",
    "tasks",
    "callsigns",
    "agent_instances",
    "squads",
    "callsign_leases",
    "launch_attempts",
    "events",
    "legacy_event_aliases",
    "deliveries",
    "assignment_receipts",
    "watcher_scopes",
    "watcher_cursors",
    "watcher_seen",
    "runtime_reconciliation",
    "resource_leases",
    "relay_receipts",
    "import_runs",
    "imported_artifacts",
    "runtime_bindings",
    "model_routing_decisions",
    "model_routing_outcomes",
    "task_resources",
    "cleanup_obligations",
    "cleanup_operations",
    "cleanup_actions",
    "cleanup_action_receipts",
    "teardown_receipts",
)

_EXPORT_ORDER = {
    "schema_migrations": "version",
    "projects": "project_id",
    "tasks": "task_id",
    "callsigns": "pool_role,pool_position,callsign",
    "agent_instances": "agent_id",
    "squads": "squad_id",
    "callsign_leases": "callsign",
    "launch_attempts": "attempt_id",
    "events": "occurred_at,event_id",
    "legacy_event_aliases": "source_order,legacy_event_id",
    "deliveries": "event_id,recipient_agent_id",
    "assignment_receipts": "task_id,task_version",
    "watcher_scopes": "scope_id",
    "watcher_cursors": "scope_id,source_id",
    "watcher_seen": "scope_id,legacy_event_id",
    "runtime_reconciliation": "scope_id,agent_id",
    "resource_leases": "resource_id",
    "relay_receipts": "scope_id,source_order,digest",
    "import_runs": "run_id",
    "imported_artifacts": "source_order,artifact_id",
    "runtime_bindings": "binding_id",
    "model_routing_decisions": "chosen_at,decision_id",
    "model_routing_outcomes": "recorded_at,outcome_id",
    "task_resources": "task_id,resource_id",
    "cleanup_obligations": "task_id",
    "cleanup_operations": "cleanup_obligation_id,cleanup_revision",
    "cleanup_actions": "operation_id,ordinal",
    "cleanup_action_receipts": "operation_id,action_id",
    "teardown_receipts": "task_id,receipt_id",
}

_INSPECTION_REDACTIONS = {
    "projects": {"repository"},
    "tasks": {"summary"},
    "agent_instances": {
        "address",
        "thread_id",
        "repository",
        "branch",
        "worktree",
        "update_text",
        "blocker",
        "next_action",
        "metadata_json",
    },
    "events": {"update_text", "detail_json"},
    "deliveries": {"claim_token", "last_error"},
    "launch_attempts": {
        "address",
        "record_locator",
        "runtime_generation",
        "metadata_json",
    },
    "watcher_scopes": {"wait_pid", "wait_process_start", "metadata_json"},
    "watcher_cursors": {"source_id"},
    "runtime_reconciliation": {"evidence_json"},
    "resource_leases": {"endpoint", "process_pid", "process_start", "metadata_json"},
    "runtime_bindings": {"session_identity", "endpoint_identity", "capabilities_json", "last_receipt_json"},
    "model_routing_decisions": {"model", "reason"},
    "task_resources": {"expected_identity_json", "applicability_reason"},
    "cleanup_actions": {"expected_identity_json", "intended_state_json"},
    "cleanup_action_receipts": {"before_json", "after_json", "adapter_receipt_json"},
}


def journal_policy(
    loaded_runtime: Optional[Iterable[int]], *, request_wal: bool = True
) -> tuple[str, Optional[str]]:
    """Return the required mode and an explicit WAL-refusal reason."""
    if not request_wal:
        return "DELETE", "wal_not_requested"
    if loaded_runtime is None:
        return "DELETE", "loaded_sqlite_version_unverifiable"
    try:
        parts = tuple(int(part) for part in loaded_runtime)
    except (TypeError, ValueError):
        return "DELETE", "loaded_sqlite_version_unverifiable"
    if len(parts) < 3:
        return "DELETE", "loaded_sqlite_version_unverifiable"
    if parts[:3] < WAL_MINIMUM:
        return "DELETE", "loaded_sqlite_below_3.51.3"
    return "WAL", None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SQLiteStorage(SQLiteTransactionCore):
    """The sole SQLite-backed implementation of :class:`Storage`."""

    def __init__(
        self,
        state_root: Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        request_wal: bool = True,
        allow_create: bool = False,
        require_current: bool = True,
    ) -> None:
        root = Path(state_root)
        if not root.is_absolute():
            raise StorageRefusal("invalid_root", "state root must be an explicit absolute path")
        if not root.is_dir():
            raise StorageRefusal("invalid_root", "state root must be an existing directory")
        if root.is_symlink():
            raise StorageRefusal("invalid_root", "state root cannot be a symbolic link")
        if root.resolve() == Path("/"):
            raise StorageRefusal("invalid_root", "filesystem root cannot be a League state root")
        if not 1 <= busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS:
            raise StorageRefusal(
                "invalid_timeout", f"busy timeout must be between 1 and {MAX_BUSY_TIMEOUT_MS} milliseconds"
            )
        self.state_root = root.resolve()
        self.database = self.state_root / DATABASE_NAME
        if self.database.is_symlink():
            raise StorageRefusal("invalid_root", "League database cannot be a symbolic link")
        if not allow_create and not self.database.is_file():
            raise StorageRefusal("store_missing", "League storage has not been migrated")
        self._database_existed = self.database.exists()
        try:
            self.connection = sqlite3.connect(
                self.database,
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
            self.connection.row_factory = sqlite3.Row
            if not self._database_existed:
                os.chmod(self.database, 0o600)
            loaded = tuple(int(item) for item in sqlite3.sqlite_version_info[:3])
            requested_mode, refusal = journal_policy(loaded, request_wal=request_wal)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            actual_mode = str(
                self.connection.execute(f"PRAGMA journal_mode={requested_mode}").fetchone()[0]
            ).upper()
            self.connection.execute("PRAGMA synchronous=FULL")
            foreign_keys = bool(self.connection.execute("PRAGMA foreign_keys").fetchone()[0])
            synchronous = int(self.connection.execute("PRAGMA synchronous").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise self._translate_database_error(exc, "storage open failed") from exc
        if not foreign_keys:
            self.connection.close()
            raise StorageRefusal("foreign_keys_unavailable", "foreign-key enforcement could not be enabled")
        if actual_mode != requested_mode:
            self.connection.close()
            raise StorageRefusal(
                "journal_mode_refused",
                f"journal mode {requested_mode} was required but SQLite selected {actual_mode}",
            )
        if synchronous != 2:
            self.connection.close()
            raise StorageRefusal("synchronous_policy_refused", "SQLite synchronous FULL could not be verified")
        self.policy = ConnectionPolicy(
            loaded_runtime=loaded,
            journal_mode=actual_mode,
            wal_allowed=requested_mode == "WAL",
            wal_refusal=refusal,
            busy_timeout_ms=busy_timeout_ms,
            foreign_keys=True,
            synchronous="FULL",
        )
        if require_current:
            try:
                self._require_schema_current()
            except Exception:
                self.connection.close()
                raise

    @classmethod
    def for_migration(
        cls,
        state_root: Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        request_wal: bool = True,
    ) -> "SQLiteStorage":
        return cls(
            state_root,
            busy_timeout_ms=busy_timeout_ms,
            request_wal=request_wal,
            allow_create=True,
            require_current=False,
        )

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _current_version(self, *, validate: bool = True) -> int:
        try:
            version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise self._translate_database_error(exc, "schema version could not be read") from exc
        if version > CURRENT_SCHEMA_VERSION:
            raise StorageRefusal(
                "schema_newer", f"database schema version {version} is newer than supported version {CURRENT_SCHEMA_VERSION}"
            )
        if not validate:
            return version
        if version == 0:
            tables = [
                row[0]
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            if tables:
                raise StorageRefusal("schema_unversioned", "unversioned database objects refuse migration")
            return 0
        try:
            rows = self.connection.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise StorageRefusal("migration_ledger_missing", "schema marker exists without a migration ledger") from exc
        expected = list(range(1, version + 1))
        observed = [int(row["version"]) for row in rows]
        if observed != expected:
            raise StorageRefusal("migration_gap", "migration ledger has a gap or unexpected entry")
        for row, migration in zip(rows, MIGRATIONS):
            if row["name"] != migration.name or row["checksum"] != migration.checksum:
                raise StorageRefusal("migration_drift", "migration ledger checksum or name drifted")
        return version

    def _require_schema_current(self) -> None:
        version = self._current_version()
        if version != CURRENT_SCHEMA_VERSION:
            raise StorageRefusal(
                "migration_required",
                f"database schema version {version} requires migration to {CURRENT_SCHEMA_VERSION}",
            )

    def _resolve_output(self, name: str, *, must_not_exist: bool = True) -> Path:
        relative = Path(name)
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise StorageRefusal("invalid_output", "output name must be a safe path relative to the state root")
        destination = self.state_root.joinpath(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent = destination.parent.resolve()
        try:
            parent.relative_to(self.state_root)
        except ValueError as exc:
            raise StorageRefusal("invalid_output", "output name escapes the state root") from exc
        if destination.is_symlink() or (must_not_exist and destination.exists()):
            raise StorageRefusal("output_collision", "output destination already exists or is unsafe")
        return destination

    def _verified_backup(
        self, destination: Path, *, fault: Optional[FaultInjector] = None
    ) -> dict[str, Any]:
        try:
            target = sqlite3.connect(destination)
            try:
                self.connection.backup(target)
                if fault:
                    fault("after_backup_copy")
            except sqlite3.DatabaseError as exc:
                raise self._translate_database_error(exc, "backup could not be created") from exc
            finally:
                target.close()
            os.chmod(destination, 0o600)
            check = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
            try:
                check.execute("PRAGMA foreign_keys=ON")
                integrity = [row[0] for row in check.execute("PRAGMA integrity_check")]
                foreign_keys = [tuple(row) for row in check.execute("PRAGMA foreign_key_check")]
                version = int(check.execute("PRAGMA user_version").fetchone()[0])
            except sqlite3.DatabaseError as exc:
                raise StorageRefusal("backup_invalid", "backup verification could not complete") from exc
            finally:
                check.close()
            if integrity != ["ok"] or foreign_keys:
                raise StorageRefusal("backup_invalid", "backup failed integrity or foreign-key verification")
            return {
                "schema": "league.backup.v1",
                "sha256": _sha256_file(destination),
                "database_schema_version": version,
                "integrity": "ok",
                "foreign_key_violations": 0,
            }
        except BaseException:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def migrate(
        self,
        *,
        backup_name: Optional[str] = None,
        fault: Optional[FaultInjector] = None,
        target_version: int = CURRENT_SCHEMA_VERSION,
    ) -> dict[str, Any]:
        before = self._current_version()
        if target_version < before or target_version > CURRENT_SCHEMA_VERSION:
            raise StorageRefusal("migration_target_invalid", "migration target version is unsupported")
        pending = [
            migration for migration in MIGRATIONS if before < migration.version <= target_version
        ]
        if not pending:
            return {
                "schema": "league.migration.v1",
                "from_version": before,
                "to_version": before,
                "applied": [],
                "backup": None,
                "policy": self._policy_result(),
            }
        backup_receipt = None
        if before > 0:
            if not backup_name:
                raise StorageRefusal("backup_required", "an existing database requires a verified pre-migration backup")
            backup_receipt = self._verified_backup(self._resolve_output(backup_name))
        try:
            with self._transaction():
                for migration in pending:
                    if migration.version != self._current_version(validate=False) + 1:
                        raise StorageRefusal("migration_gap", "migration sequence is not contiguous")
                    for statement in migration.statements:
                        self.connection.execute(statement)
                    self.connection.execute(
                        "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                        (
                            migration.version,
                            migration.name,
                            migration.checksum,
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        ),
                    )
                    self.connection.execute(f"PRAGMA user_version={migration.version}")
                    if fault:
                        fault(f"after_migration_{migration.version}")
        except StorageRefusal:
            raise
        except sqlite3.DatabaseError as exc:
            raise self._translate_database_error(exc, "transactional migration failed") from exc
        if target_version == CURRENT_SCHEMA_VERSION:
            self._require_schema_current()
        elif self._current_version() != target_version:
            raise StorageRefusal("migration_failed", "migration did not reach its requested target")
        return {
            "schema": "league.migration.v1",
            "from_version": before,
            "to_version": target_version,
            "applied": [migration.version for migration in pending],
            "backup": backup_receipt,
            "policy": self._policy_result(),
        }

    def _policy_result(self) -> dict[str, Any]:
        return {
            "loaded_sqlite": ".".join(str(part) for part in self.policy.loaded_runtime),
            "journal_mode": self.policy.journal_mode,
            "wal_allowed": self.policy.wal_allowed,
            "wal_refusal": self.policy.wal_refusal,
            "busy_timeout_ms": self.policy.busy_timeout_ms,
            "foreign_keys": self.policy.foreign_keys,
            "synchronous": self.policy.synchronous,
        }

    def integrity(self) -> dict[str, Any]:
        try:
            integrity = [row[0] for row in self.connection.execute("PRAGMA integrity_check")]
            foreign_keys = [tuple(row) for row in self.connection.execute("PRAGMA foreign_key_check")]
        except sqlite3.DatabaseError as exc:
            raise StorageRefusal("integrity_failed", "database integrity checks could not complete") from exc
        return {
            "schema": "league.integrity.v1",
            "integrity": integrity,
            "foreign_key_violations": [
                {"table": row[0], "rowid": row[1], "parent": row[2], "constraint": row[3]}
                for row in foreign_keys
            ],
            "ok": integrity == ["ok"] and not foreign_keys,
            "policy": self._policy_result(),
        }

    def backup(
        self, name: str, *, fault: Optional[FaultInjector] = None
    ) -> dict[str, Any]:
        receipt = self._verified_backup(self._resolve_output(name), fault=fault)
        receipt["policy"] = self._policy_result()
        return receipt

    def agent_status(self, agent_id: str) -> Optional[dict[str, Any]]:
        return agent_status_operation(self, agent_id)

    def transition(
        self,
        agent_id: str,
        expected_version: int,
        status: str,
        update: str,
        at: str,
        *,
        fault: Optional[FaultInjector] = None,
    ) -> dict[str, Any]:
        return transition_operation(
            self,
            agent_id,
            expected_version,
            status,
            update,
            at,
            fault=fault,
        )

    def reserve_callsign(
        self,
        callsign: str,
        agent_id: str,
        task_id: str,
        role: str,
        status: str,
        update: str,
        at: str,
    ) -> dict[str, Any]:
        return reserve_callsign_operation(
            self, callsign, agent_id, task_id, role, status, update, at
        )

    def release_callsign(
        self, callsign: str, agent_id: str, expected_version: int, at: str
    ) -> dict[str, Any]:
        return release_callsign_operation(
            self, callsign, agent_id, expected_version, at
        )

    def claim_delivery(
        self,
        event_id: str,
        recipient_agent_id: str,
        claim_token: str,
        claim_expires_at: str,
        at: str,
    ) -> dict[str, Any]:
        return claim_delivery_operation(
            self,
            event_id,
            recipient_agent_id,
            claim_token,
            claim_expires_at,
            at,
        )

    def acknowledge_delivery(
        self, event_id: str, recipient_agent_id: str, claim_token: str, at: str
    ) -> dict[str, Any]:
        return self._finish_delivery(event_id, recipient_agent_id, claim_token, "acknowledged", at, None)

    def fail_delivery(
        self,
        event_id: str,
        recipient_agent_id: str,
        claim_token: str,
        reason: str,
        at: str,
    ) -> dict[str, Any]:
        if not reason:
            raise StorageRefusal("invalid_delivery", "delivery failure reason is required")
        return self._finish_delivery(event_id, recipient_agent_id, claim_token, "failed", at, reason)

    def _finish_delivery(
        self,
        event_id: str,
        recipient_agent_id: str,
        claim_token: str,
        state: str,
        at: str,
        reason: Optional[str],
    ) -> dict[str, Any]:
        return finish_delivery_operation(
            self, event_id, recipient_agent_id, claim_token, state, at, reason
        )

    def resolve_project(self, repository: str) -> Optional[dict[str, Any]]:
        return resolve_project_operation(self, repository)

    def transfer_task_owner(
        self,
        task_id: str,
        expected_version: int,
        owner_kind: str,
        owner_id: str,
        at: str,
    ) -> dict[str, Any]:
        return transfer_task_owner_operation(
            self, task_id, expected_version, owner_kind, owner_id, at
        )

    def _canonical_counts(self) -> dict[str, int]:
        return canonical_counts(self, _IMPORT_ORDER)

    def import_target_counts(self) -> dict[str, int]:
        """Return bounded table counts for dry-run collision reporting."""
        return self._canonical_counts()

    def apply_import(
        self,
        plan: ImportPlan,
        expected_digest: str,
        *,
        fault: Optional[FaultInjector] = None,
    ) -> dict[str, Any]:
        return apply_import_operation(
            self,
            plan,
            expected_digest,
            columns_by_table=_IMPORT_COLUMNS,
            table_order=_IMPORT_ORDER,
            fault=fault,
        )

    def export_bytes(self, *, format_name: str, purpose: str, max_records: int) -> bytes:
        return export_operation(
            self,
            format_name=format_name,
            purpose=purpose,
            max_records=max_records,
            maximum_records=MAX_EXPORT_RECORDS,
            current_schema_version=CURRENT_SCHEMA_VERSION,
            export_tables=_EXPORT_TABLES,
            export_order=_EXPORT_ORDER,
            redactions=_INSPECTION_REDACTIONS,
        )

    def write_restricted(self, name: str, payload: bytes) -> Path:
        destination = self._resolve_output(name)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def register_runtime_binding(
        self,
        binding_id: str,
        task_id: str,
        harness_kind: str,
        backend_kind: str,
        session_identity: str,
        endpoint_identity: str,
        capabilities: dict[str, Any],
        at: str,
    ) -> dict[str, Any]:
        return sqlite_runtime_ops.register_runtime_binding(
            self,
            binding_id,
            task_id,
            harness_kind,
            backend_kind,
            session_identity,
            endpoint_identity,
            capabilities,
            at,
        )

    def runtime_binding(self, binding_id: str) -> Optional[dict[str, Any]]:
        return sqlite_runtime_ops.runtime_binding(self, binding_id)

    def update_runtime_binding(
        self,
        binding_id: str,
        expected_version: int,
        state: str,
        at: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        return sqlite_runtime_ops.update_runtime_binding(
            self, binding_id, expected_version, state, at, receipt
        )

    def record_routing_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        return sqlite_runtime_ops.record_routing_decision(self, decision)

    def routing_decision(self, decision_id: str) -> Optional[dict[str, Any]]:
        return sqlite_runtime_ops.routing_decision(self, decision_id)

    def record_routing_outcome(self, outcome: dict[str, Any]) -> dict[str, Any]:
        return sqlite_runtime_ops.record_routing_outcome(self, outcome)

    def register_task_resource(self, resource: dict[str, Any], at: str) -> dict[str, Any]:
        return sqlite_runtime_ops.register_task_resource(self, resource, at)

    def task_resources(self, task_id: str) -> list[dict[str, Any]]:
        return sqlite_runtime_ops.task_resources(self, task_id)

    def plan_cleanup(self, plan: dict[str, Any]) -> dict[str, Any]:
        return sqlite_runtime_ops.plan_cleanup(self, plan)

    def cleanup_operation(self, operation_id: str) -> Optional[dict[str, Any]]:
        return sqlite_runtime_ops.cleanup_operation(self, operation_id)

    def claim_cleanup_operation(
        self,
        operation_id: str,
        expected_fence: int,
        executor_id: str,
        leased_until: str,
        at: str,
    ) -> dict[str, Any]:
        return sqlite_runtime_ops.claim_cleanup_operation(
            self, operation_id, expected_fence, executor_id, leased_until, at
        )

    def record_cleanup_action_receipt(
        self,
        action_id: str,
        operation_id: str,
        fence: int,
        outcome: str,
        before: dict[str, Any],
        after: dict[str, Any],
        adapter_receipt: dict[str, Any],
        at: str,
    ) -> dict[str, Any]:
        return sqlite_runtime_ops.record_cleanup_action_receipt(
            self,
            action_id,
            operation_id,
            fence,
            outcome,
            before,
            after,
            adapter_receipt,
            at,
        )

    def finalize_cleanup(self, operation_id: str, fence: int, at: str) -> dict[str, Any]:
        return sqlite_runtime_ops.finalize_cleanup(self, operation_id, fence, at)
