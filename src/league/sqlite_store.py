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
from .sqlite_assignment_ops import activate_assignment as activate_assignment_operation
from .sqlite_assignment_ops import block_assignment as block_assignment_operation
from .sqlite_assignment_ops import mark_assignment_launching as mark_assignment_launching_operation
from .sqlite_assignment_ops import prepare_assignment as prepare_assignment_operation
from .sqlite_assignment_ops import transition_task as transition_task_operation
from .sqlite_delivery_ops import claim_delivery as claim_delivery_operation
from .sqlite_delivery_ops import finish_delivery as finish_delivery_operation
from .sqlite_lifecycle_ops import agent_status as agent_status_operation
from .sqlite_lifecycle_ops import release_callsign as release_callsign_operation
from .sqlite_lifecycle_ops import reserve_callsign as reserve_callsign_operation
from .sqlite_lifecycle_ops import resolve_project as resolve_project_operation
from .sqlite_lifecycle_ops import transfer_task_owner as transfer_task_owner_operation
from .sqlite_lifecycle_ops import transition as transition_operation
from .sqlite_outbox_ops import acknowledge_outbox as acknowledge_outbox_operation
from .sqlite_outbox_ops import claim_outbox as claim_outbox_operation
from .sqlite_outbox_ops import delivery_target as delivery_target_operation
from .sqlite_outbox_ops import fail_outbox as fail_outbox_operation
from .sqlite_outbox_ops import outbox_envelope as outbox_envelope_operation
from .sqlite_outbox_ops import pending_backlog as pending_backlog_operation
from .sqlite_request_ops import answer_request as answer_request_operation
from .sqlite_request_ops import claim_request as claim_request_operation
from .sqlite_request_ops import dispatch_request as dispatch_request_operation
from .sqlite_request_ops import intake_prompt as intake_prompt_operation
from .sqlite_request_ops import record_request_result as record_request_result_operation
from .sqlite_request_ops import release_request_claim as release_request_claim_operation
from .sqlite_request_ops import route_request as route_request_operation
from .sqlite_request_ops import set_request_state as set_request_state_operation
from .sqlite_request_ops import triage_prompt as triage_prompt_operation
from .sqlite_request_ops import unresolved_requests as unresolved_requests_operation
from .sqlite_transfer_ops import (
    apply_import as apply_import_operation,
    canonical_counts,
    export_bytes as export_operation,
)
from .sqlite_watcher_ops import note_user_message as note_user_message_operation
from .sqlite_watcher_ops import rearm_wait as rearm_wait_operation
from .sqlite_watcher_ops import register_runtime as register_runtime_operation
from .sqlite_watcher_ops import register_watcher as register_watcher_operation
from .sqlite_watcher_ops import set_allow_stop_once as set_allow_stop_once_operation
from .sqlite_watcher_ops import stop_decision as stop_decision_operation
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
        "request-assignment-outbox-and-stop-lifecycle",
        (
            """
            CREATE TABLE runtime_instances (
              runtime_instance_id TEXT PRIMARY KEY,
              actor_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              harness_kind TEXT NOT NULL,
              backend_kind TEXT NOT NULL,
              session_ref TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              runtime_generation TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('active','idle','closed','failed')),
              verified INTEGER NOT NULL CHECK (verified IN (0,1)),
              last_seen_at TEXT NOT NULL,
              UNIQUE (actor_agent_id, runtime_generation)
            )
            """,
            """
            CREATE TABLE prompts (
              prompt_id TEXT PRIMARY KEY,
              intake_actor_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              runtime_instance_id TEXT NOT NULL REFERENCES runtime_instances(runtime_instance_id),
              adapter_kind TEXT NOT NULL,
              session_ref TEXT NOT NULL,
              source_event_key TEXT NOT NULL,
              triage_state TEXT NOT NULL CHECK (triage_state IN ('untriaged','complete')),
              triage_digest TEXT,
              created_at TEXT NOT NULL,
              UNIQUE (adapter_kind, session_ref, source_event_key)
            )
            """,
            """
            CREATE TABLE prompt_payloads (
              prompt_id TEXT PRIMARY KEY REFERENCES prompts(prompt_id),
              body TEXT,
              body_hash TEXT NOT NULL,
              byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
              pruned_at TEXT
            )
            """,
            """
            CREATE TABLE requests (
              request_id TEXT PRIMARY KEY,
              summary TEXT NOT NULL,
              requester_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              owner_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              return_to_agent_id TEXT REFERENCES agent_instances(agent_id),
              execution_mode TEXT CHECK (
                execution_mode IS NULL OR execution_mode IN ('direct','hidden','champion')
              ),
              state TEXT NOT NULL CHECK (
                state IN ('open','routed','accepted','in_progress','awaiting_user','blocked',
                          'awaiting_requester','deferred','answered','cancelled')
              ),
              latest_result_id TEXT REFERENCES request_results(result_id),
              last_route_event_id TEXT REFERENCES events(event_id),
              resolution_summary TEXT,
              next_attention_at TEXT,
              version INTEGER NOT NULL CHECK (version > 0),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE prompt_items (
              prompt_item_id TEXT PRIMARY KEY,
              prompt_id TEXT NOT NULL REFERENCES prompts(prompt_id),
              ordinal INTEGER NOT NULL CHECK (ordinal > 0),
              summary TEXT NOT NULL,
              disposition TEXT NOT NULL CHECK (
                disposition IN ('new_request','follow_up','context','acknowledgement','duplicate','deferred')
              ),
              UNIQUE (prompt_id, ordinal)
            )
            """,
            """
            CREATE TABLE request_sources (
              request_id TEXT NOT NULL REFERENCES requests(request_id),
              prompt_item_id TEXT NOT NULL REFERENCES prompt_items(prompt_item_id),
              source_role TEXT NOT NULL CHECK (source_role IN ('origin','follow_up','duplicate')),
              PRIMARY KEY (request_id, prompt_item_id)
            )
            """,
            """
            CREATE TABLE request_claims (
              request_id TEXT PRIMARY KEY REFERENCES requests(request_id),
              runtime_instance_id TEXT NOT NULL REFERENCES runtime_instances(runtime_instance_id),
              claim_proof_hash TEXT NOT NULL,
              leased_until TEXT NOT NULL,
              claim_version INTEGER NOT NULL CHECK (claim_version > 0),
              claimed_at TEXT NOT NULL,
              released_at TEXT
            )
            """,
            """
            CREATE TABLE request_dispatches (
              dispatch_id TEXT PRIMARY KEY,
              request_id TEXT NOT NULL REFERENCES requests(request_id),
              request_version INTEGER NOT NULL,
              work_kind TEXT NOT NULL,
              execution_mode TEXT NOT NULL CHECK (execution_mode IN ('direct','hidden','champion')),
              reason TEXT NOT NULL,
              requested_mode TEXT,
              requested_model TEXT,
              requested_effort TEXT,
              explicit_route TEXT,
              input_json TEXT NOT NULL,
              decided_at TEXT NOT NULL,
              UNIQUE (request_id, request_version)
            )
            """,
            """
            CREATE TABLE request_results (
              result_id TEXT PRIMARY KEY,
              request_id TEXT NOT NULL REFERENCES requests(request_id),
              produced_by_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              outcome TEXT NOT NULL,
              summary TEXT NOT NULL,
              payload_hash TEXT,
              idempotency_key TEXT NOT NULL,
              return_event_id TEXT REFERENCES events(event_id) DEFERRABLE INITIALLY DEFERRED,
              return_outbox_id TEXT REFERENCES delivery_outbox(outbox_id) DEFERRABLE INITIALLY DEFERRED,
              created_at TEXT NOT NULL,
              UNIQUE (request_id, idempotency_key)
            )
            """,
            """
            CREATE TABLE request_result_sources (
              result_id TEXT NOT NULL REFERENCES request_results(result_id),
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              source_kind TEXT NOT NULL,
              PRIMARY KEY (result_id, task_id)
            )
            """,
            """
            CREATE TABLE response_references (
              response_ref_id TEXT PRIMARY KEY,
              request_id TEXT NOT NULL REFERENCES requests(request_id),
              runtime_instance_id TEXT NOT NULL REFERENCES runtime_instances(runtime_instance_id),
              adapter_kind TEXT NOT NULL,
              session_locator TEXT NOT NULL,
              response_locator TEXT NOT NULL,
              durability TEXT NOT NULL CHECK (durability IN ('durable','ephemeral')),
              content_hash TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE (request_id, content_hash)
            )
            """,
            "ALTER TABLE tasks ADD COLUMN request_id TEXT REFERENCES requests(request_id)",
            "ALTER TABLE tasks ADD COLUMN coordinator_agent_id TEXT REFERENCES agent_instances(agent_id)",
            "ALTER TABLE tasks ADD COLUMN champion_agent_id TEXT REFERENCES agent_instances(agent_id)",
            "ALTER TABLE tasks ADD COLUMN result_summary TEXT",
            """
            CREATE TABLE callsign_assignments (
              callsign_assignment_id TEXT PRIMARY KEY,
              callsign TEXT NOT NULL REFERENCES callsigns(callsign),
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              state TEXT NOT NULL CHECK (state IN ('reserved','active','released','blocked')),
              reserved_at TEXT NOT NULL,
              activated_at TEXT,
              released_at TEXT
            )
            """,
            """
            CREATE TABLE task_assignments (
              task_assignment_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
              request_id TEXT NOT NULL REFERENCES requests(request_id),
              coordinator_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              champion_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              runtime_instance_id TEXT REFERENCES runtime_instances(runtime_instance_id),
              callsign TEXT NOT NULL REFERENCES callsigns(callsign),
              state TEXT NOT NULL CHECK (state IN ('pending','launching','active','blocked','cleanup_pending')),
              acceptance_receipt_json TEXT,
              failure_class TEXT,
              cleanup_required INTEGER NOT NULL DEFAULT 0 CHECK (cleanup_required IN (0,1)),
              version INTEGER NOT NULL CHECK (version > 0),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE task_transitions (
              transition_id TEXT PRIMARY KEY,
              transition_key TEXT NOT NULL UNIQUE,
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              from_state TEXT NOT NULL,
              to_state TEXT NOT NULL,
              update_text TEXT NOT NULL,
              next_action TEXT NOT NULL,
              blocker TEXT,
              created_at TEXT NOT NULL,
              event_id TEXT NOT NULL UNIQUE
            )
            """,
            "ALTER TABLE events ADD COLUMN request_id TEXT REFERENCES requests(request_id)",
            "ALTER TABLE events ADD COLUMN aggregate_kind TEXT",
            "ALTER TABLE events ADD COLUMN aggregate_id TEXT",
            "ALTER TABLE events ADD COLUMN event_seq INTEGER",
            "ALTER TABLE events ADD COLUMN source_event_id TEXT",
            "UPDATE events SET aggregate_kind=CASE WHEN task_id IS NOT NULL THEN 'task' ELSE 'agent' END, aggregate_id=COALESCE(task_id,agent_id), event_seq=rowid WHERE aggregate_kind IS NULL",
            "CREATE UNIQUE INDEX ux_events_event_seq ON events(event_seq)",
            "CREATE INDEX ix_events_aggregate ON events(aggregate_kind,aggregate_id,event_seq)",
            """
            CREATE TRIGGER events_fill_sequence
            AFTER INSERT ON events WHEN NEW.event_seq IS NULL
            BEGIN
              UPDATE events SET event_seq=NEW.rowid WHERE rowid=NEW.rowid;
            END
            """,
            """
            CREATE TABLE delivery_outbox (
              outbox_id TEXT PRIMARY KEY,
              event_id TEXT NOT NULL REFERENCES events(event_id),
              recipient_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              state TEXT NOT NULL CHECK (state IN ('pending','in_flight','awaiting_receipt','delivered','cancelled')),
              available_at TEXT NOT NULL,
              attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
              first_attempt_at TEXT,
              last_attempt_at TEXT,
              last_outcome TEXT,
              delivered_at TEXT,
              UNIQUE (event_id, recipient_agent_id)
            )
            """,
            """
            CREATE TABLE outbox_dispatch_leases (
              outbox_id TEXT PRIMARY KEY REFERENCES delivery_outbox(outbox_id),
              dispatcher_id TEXT NOT NULL,
              leased_until TEXT NOT NULL,
              fence INTEGER NOT NULL CHECK (fence > 0)
            )
            """,
            """
            CREATE TABLE delivery_attempts (
              attempt_id TEXT PRIMARY KEY,
              outbox_id TEXT NOT NULL REFERENCES delivery_outbox(outbox_id),
              adapter_kind TEXT NOT NULL,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              outcome TEXT
            )
            """,
            """
            CREATE TABLE recipient_receipts (
              event_id TEXT NOT NULL REFERENCES events(event_id),
              recipient_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              received_at TEXT NOT NULL,
              effect_kind TEXT NOT NULL,
              effect_id TEXT NOT NULL,
              PRIMARY KEY (event_id, recipient_agent_id)
            )
            """,
            """
            CREATE TABLE watcher_registrations (
              watcher_id TEXT PRIMARY KEY,
              actor_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              runtime_instance_id TEXT NOT NULL REFERENCES runtime_instances(runtime_instance_id),
              wake_locator TEXT NOT NULL,
              leased_until TEXT NOT NULL,
              fence INTEGER NOT NULL CHECK (fence > 0),
              registered_at TEXT NOT NULL
            )
            """,
            "CREATE UNIQUE INDEX ux_watcher_actor ON watcher_registrations(actor_agent_id)",
            "ALTER TABLE watcher_scopes ADD COLUMN actor_agent_id TEXT REFERENCES agent_instances(agent_id)",
            "ALTER TABLE watcher_scopes ADD COLUMN block_on_obligations INTEGER NOT NULL DEFAULT 1 CHECK (block_on_obligations IN (0,1))",
            "ALTER TABLE watcher_scopes ADD COLUMN last_blocked_wait_generation INTEGER NOT NULL DEFAULT -1",
            "ALTER TABLE watcher_scopes ADD COLUMN last_user_priority_generation INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE watcher_scopes ADD COLUMN last_terminal_generation TEXT",
            """
            CREATE TABLE obligations (
              obligation_id TEXT PRIMARY KEY,
              owner_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
              kind TEXT NOT NULL,
              aggregate_id TEXT NOT NULL,
              dedupe_key TEXT NOT NULL UNIQUE,
              state TEXT NOT NULL CHECK (state IN ('open','satisfied','cancelled')),
              next_attention_at TEXT,
              details_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE cleanup_obligations (
              cleanup_obligation_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
              cleanup_state TEXT NOT NULL CHECK (
                cleanup_state IN ('not_due','pending','awaiting_authority','verifying','planned','executing','blocked','completed')
              ),
              required_policy TEXT NOT NULL,
              next_action TEXT NOT NULL,
              version INTEGER NOT NULL CHECK (version > 0),
              updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX ix_prompts_untriaged ON prompts(intake_actor_id,triage_state,created_at)",
            "CREATE INDEX ix_requests_unresolved ON requests(owner_agent_id,state,next_attention_at,updated_at)",
            "CREATE INDEX ix_outbox_pending ON delivery_outbox(state,available_at,outbox_id)",
            "CREATE INDEX ix_assignments_state ON task_assignments(coordinator_agent_id,state,updated_at)",
            "CREATE INDEX ix_obligations_due ON obligations(owner_agent_id,state,next_attention_at,created_at)",
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
    "runtime_instances",
    "prompts",
    "prompt_payloads",
    "prompt_items",
    "requests",
    "request_sources",
    "request_claims",
    "request_dispatches",
    "request_results",
    "request_result_sources",
    "response_references",
    "callsign_assignments",
    "task_assignments",
    "task_transitions",
    "delivery_outbox",
    "outbox_dispatch_leases",
    "delivery_attempts",
    "recipient_receipts",
    "watcher_registrations",
    "obligations",
    "cleanup_obligations",
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
    "runtime_instances": "runtime_instance_id",
    "prompts": "created_at,prompt_id",
    "prompt_payloads": "prompt_id",
    "prompt_items": "prompt_id,ordinal,prompt_item_id",
    "requests": "created_at,request_id",
    "request_sources": "request_id,prompt_item_id",
    "request_claims": "request_id",
    "request_dispatches": "decided_at,dispatch_id",
    "request_results": "created_at,result_id",
    "request_result_sources": "result_id,task_id",
    "response_references": "created_at,response_ref_id",
    "callsign_assignments": "reserved_at,callsign_assignment_id",
    "task_assignments": "created_at,task_assignment_id",
    "task_transitions": "created_at,transition_id",
    "delivery_outbox": "available_at,outbox_id",
    "outbox_dispatch_leases": "outbox_id",
    "delivery_attempts": "started_at,attempt_id",
    "recipient_receipts": "received_at,event_id,recipient_agent_id",
    "watcher_registrations": "actor_agent_id,watcher_id",
    "obligations": "created_at,obligation_id",
    "cleanup_obligations": "task_id",
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
    "runtime_instances": {"session_ref", "endpoint", "runtime_generation"},
    "prompt_payloads": {"body"},
    "prompt_items": {"summary"},
    "requests": {"summary", "resolution_summary"},
    "request_claims": {"claim_proof_hash"},
    "request_dispatches": {
        "reason",
        "requested_model",
        "requested_effort",
        "explicit_route",
        "input_json",
    },
    "request_results": {"summary", "payload_hash", "idempotency_key"},
    "response_references": {"session_locator", "response_locator", "content_hash"},
    "task_assignments": {"acceptance_receipt_json", "failure_class"},
    "task_transitions": {"update_text", "next_action", "blocker"},
    "delivery_attempts": {"outcome"},
    "watcher_registrations": {"wake_locator"},
    "obligations": {"details_json"},
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

    def intake_prompt(
        self,
        prompt_id: str,
        intake_actor_id: str,
        runtime_instance_id: str,
        adapter_kind: str,
        session_ref: str,
        source_event_key: str,
        body: str,
        at: str,
    ) -> dict[str, Any]:
        return intake_prompt_operation(
            self,
            prompt_id,
            intake_actor_id,
            runtime_instance_id,
            adapter_kind,
            session_ref,
            source_event_key,
            body,
            at,
        )

    def triage_prompt(
        self, prompt_id: str, items: list[dict[str, Any]], at: str
    ) -> dict[str, Any]:
        return triage_prompt_operation(self, prompt_id, items, at)

    def claim_request(
        self,
        request_id: str,
        runtime_instance_id: str,
        claim_token: str,
        leased_until: str,
        at: str,
    ) -> dict[str, Any]:
        return claim_request_operation(
            self, request_id, runtime_instance_id, claim_token, leased_until, at
        )

    def release_request_claim(
        self, request_id: str, runtime_instance_id: str, claim_token: str, at: str
    ) -> dict[str, Any]:
        return release_request_claim_operation(
            self, request_id, runtime_instance_id, claim_token, at
        )

    def dispatch_request(
        self,
        request_id: str,
        claim_token: str,
        dispatch_id: str,
        work_kind: str,
        requested_mode: Optional[str],
        hidden_supported: bool,
        requested_model: Optional[str],
        requested_effort: Optional[str],
        explicit_route: Optional[str],
        at: str,
    ) -> dict[str, Any]:
        return dispatch_request_operation(
            self,
            request_id,
            claim_token,
            dispatch_id,
            work_kind,
            requested_mode,
            hidden_supported,
            requested_model,
            requested_effort,
            explicit_route,
            at,
        )

    def route_request(
        self,
        request_id: str,
        claim_token: str,
        expected_version: int,
        recipient_agent_id: str,
        event_id: str,
        outbox_id: str,
        at: str,
    ) -> dict[str, Any]:
        return route_request_operation(
            self,
            request_id,
            claim_token,
            expected_version,
            recipient_agent_id,
            event_id,
            outbox_id,
            at,
        )

    def set_request_state(
        self,
        request_id: str,
        claim_token: str,
        expected_version: int,
        state: str,
        summary: str,
        event_id: str,
        at: str,
        *,
        next_attention_at: Optional[str] = None,
    ) -> dict[str, Any]:
        return set_request_state_operation(
            self,
            request_id,
            claim_token,
            expected_version,
            state,
            summary,
            event_id,
            at,
            next_attention_at=next_attention_at,
        )

    def record_request_result(
        self,
        request_id: str,
        claim_token: str,
        expected_version: int,
        result_id: str,
        idempotency_key: str,
        outcome: str,
        summary: str,
        task_ids: Iterable[str],
        at: str,
        *,
        return_to_requester: bool,
        event_id: Optional[str] = None,
        outbox_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return record_request_result_operation(
            self,
            request_id,
            claim_token,
            expected_version,
            result_id,
            idempotency_key,
            outcome,
            summary,
            task_ids,
            at,
            return_to_requester=return_to_requester,
            event_id=event_id,
            outbox_id=outbox_id,
        )

    def answer_request(
        self,
        request_id: str,
        claim_token: str,
        expected_version: int,
        response_ref_id: str,
        adapter_kind: str,
        session_locator: str,
        response_locator: str,
        durability: str,
        content_hash: str,
        resolution_summary: str,
        event_id: str,
        at: str,
    ) -> dict[str, Any]:
        return answer_request_operation(
            self,
            request_id,
            claim_token,
            expected_version,
            response_ref_id,
            adapter_kind,
            session_locator,
            response_locator,
            durability,
            content_hash,
            resolution_summary,
            event_id,
            at,
        )

    def unresolved_requests(
        self,
        owner_agent_id: str,
        *,
        limit: int = 100,
        before_action: Optional[str] = None,
    ) -> dict[str, Any]:
        return unresolved_requests_operation(
            self, owner_agent_id, limit=limit, before_action=before_action
        )

    def prepare_assignment(
        self,
        assignment_id: str,
        request_id: str,
        claim_token: str,
        task_id: str,
        task_summary: str,
        coordinator_agent_id: str,
        champion_agent_id: str,
        callsign: str,
        repository: str,
        issue: int,
        branch: str,
        worktree: str,
        at: str,
    ) -> dict[str, Any]:
        return prepare_assignment_operation(
            self,
            assignment_id,
            request_id,
            claim_token,
            task_id,
            task_summary,
            coordinator_agent_id,
            champion_agent_id,
            callsign,
            repository,
            issue,
            branch,
            worktree,
            at,
        )

    def mark_assignment_launching(
        self, assignment_id: str, expected_version: int, at: str
    ) -> dict[str, Any]:
        return mark_assignment_launching_operation(
            self, assignment_id, expected_version, at
        )

    def activate_assignment(
        self,
        assignment_id: str,
        expected_version: int,
        receipt: dict[str, Any],
        event_id: str,
        outbox_id: str,
        at: str,
    ) -> dict[str, Any]:
        return activate_assignment_operation(
            self, assignment_id, expected_version, receipt, event_id, outbox_id, at
        )

    def block_assignment(
        self,
        assignment_id: str,
        expected_version: int,
        failure_class: str,
        cleanup_required: bool,
        cleanup_proven: bool,
        at: str,
    ) -> dict[str, Any]:
        return block_assignment_operation(
            self,
            assignment_id,
            expected_version,
            failure_class,
            cleanup_required,
            cleanup_proven,
            at,
        )

    def transition_task(
        self,
        task_id: str,
        runtime_instance_id: str,
        expected_version: int,
        state: str,
        update: str,
        next_action: str,
        blocker: Optional[str],
        transition_id: str,
        transition_key: str,
        event_id: str,
        outbox_id: str,
        recipient_agent_id: str,
        at: str,
    ) -> dict[str, Any]:
        return transition_task_operation(
            self,
            task_id,
            runtime_instance_id,
            expected_version,
            state,
            update,
            next_action,
            blocker,
            transition_id,
            transition_key,
            event_id,
            outbox_id,
            recipient_agent_id,
            at,
        )

    def claim_outbox(
        self,
        outbox_id: str,
        event_id: str,
        recipient_agent_id: str,
        dispatcher_id: str,
        attempt_id: str,
        lease_expires_at: str,
        at: str,
    ) -> dict[str, Any]:
        return claim_outbox_operation(
            self,
            outbox_id,
            event_id,
            recipient_agent_id,
            dispatcher_id,
            attempt_id,
            lease_expires_at,
            at,
        )

    def acknowledge_outbox(
        self,
        outbox_id: str,
        event_id: str,
        recipient_agent_id: str,
        dispatcher_id: str,
        fence: int,
        attempt_id: str,
        adapter_kind: str,
        effect_kind: str,
        effect_id: str,
        at: str,
    ) -> dict[str, Any]:
        return acknowledge_outbox_operation(
            self,
            outbox_id,
            event_id,
            recipient_agent_id,
            dispatcher_id,
            fence,
            attempt_id,
            adapter_kind,
            effect_kind,
            effect_id,
            at,
        )

    def fail_outbox(
        self,
        outbox_id: str,
        event_id: str,
        recipient_agent_id: str,
        dispatcher_id: str,
        fence: int,
        attempt_id: str,
        adapter_kind: str,
        reason: str,
        retry_at: str,
        at: str,
    ) -> dict[str, Any]:
        return fail_outbox_operation(
            self,
            outbox_id,
            event_id,
            recipient_agent_id,
            dispatcher_id,
            fence,
            attempt_id,
            adapter_kind,
            reason,
            retry_at,
            at,
        )

    def pending_backlog(
        self,
        at: str,
        *,
        limit: int = 100,
        per_recipient: int = 2,
        exclude_outbox_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return pending_backlog_operation(
            self,
            at,
            limit=limit,
            per_recipient=per_recipient,
            exclude_outbox_id=exclude_outbox_id,
        )

    def delivery_target(
        self, recipient_agent_id: str, at: str
    ) -> Optional[dict[str, Any]]:
        return delivery_target_operation(self, recipient_agent_id, at)

    def outbox_envelope(
        self, outbox_id: str, event_id: str, recipient_agent_id: str
    ) -> dict[str, Any]:
        return outbox_envelope_operation(
            self, outbox_id, event_id, recipient_agent_id
        )

    def register_runtime(
        self,
        runtime_instance_id: str,
        actor_agent_id: str,
        harness_kind: str,
        backend_kind: str,
        session_ref: str,
        endpoint: str,
        runtime_generation: str,
        status: str,
        verified: bool,
        at: str,
    ) -> dict[str, Any]:
        return register_runtime_operation(
            self,
            runtime_instance_id,
            actor_agent_id,
            harness_kind,
            backend_kind,
            session_ref,
            endpoint,
            runtime_generation,
            status,
            verified,
            at,
        )

    def register_watcher(
        self,
        scope_id: str,
        watcher_id: str,
        actor_agent_id: str,
        runtime_instance_id: str,
        wake_locator: str,
        leased_until: str,
        fence: int,
        at: str,
        *,
        block_on_obligations: bool = True,
    ) -> dict[str, Any]:
        return register_watcher_operation(
            self,
            scope_id,
            watcher_id,
            actor_agent_id,
            runtime_instance_id,
            wake_locator,
            leased_until,
            fence,
            at,
            block_on_obligations=block_on_obligations,
        )

    def note_user_message(
        self, scope_id: str, actor_agent_id: str, at: str
    ) -> dict[str, Any]:
        return note_user_message_operation(self, scope_id, actor_agent_id, at)

    def rearm_wait(
        self, scope_id: str, actor_agent_id: str, event_id: str, at: str
    ) -> dict[str, Any]:
        return rearm_wait_operation(self, scope_id, actor_agent_id, event_id, at)

    def set_allow_stop_once(
        self, scope_id: str, actor_agent_id: str
    ) -> dict[str, Any]:
        return set_allow_stop_once_operation(self, scope_id, actor_agent_id)

    def stop_decision(
        self,
        scope_id: str,
        actor_agent_id: str,
        terminal_generation: str,
        at: str,
    ) -> dict[str, Any]:
        return stop_decision_operation(
            self, scope_id, actor_agent_id, terminal_generation, at
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
