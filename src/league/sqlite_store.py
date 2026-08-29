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
from typing import Any, Iterable, Optional, Sequence

from . import sqlite_runtime_ops
from .sqlite_artifact_ops import declare as declare_repository_artifact_operation
from .sqlite_artifact_ops import publish as record_repository_publication_operation
from .sqlite_artifact_ops import status as task_artifacts_operation
from .sqlite_artifact_ops import unresolved as unresolved_repository_publications_operation
from .sqlite_core import SQLiteTransactionCore
from .sqlite_assignment_ops import activate_assignment as activate_assignment_operation
from .sqlite_assignment_ops import block_assignment as block_assignment_operation
from .sqlite_assignment_ops import finish_hidden_assignment as finish_hidden_assignment_operation
from .sqlite_assignment_ops import mark_assignment_launching as mark_assignment_launching_operation
from .sqlite_assignment_ops import prepare_assignment as prepare_assignment_operation
from .sqlite_assignment_ops import reconcile_assignment_runtime as reconcile_assignment_runtime_operation
from .sqlite_assignment_ops import transition_task as transition_task_operation
from .sqlite_callsign_ops import activate_callsign as activate_callsign_operation
from .sqlite_callsign_ops import allocate_callsign as allocate_callsign_operation
from .sqlite_callsign_ops import callsign_status as callsign_status_operation
from .sqlite_callsign_ops import initialize_imported_callsign_state
from .sqlite_callsign_ops import reconcile_callsign_pool as reconcile_callsign_pool_operation
from .sqlite_callsign_ops import release_callsign as release_callsign_operation
from .sqlite_callsign_ops import rollback_callsign as rollback_callsign_operation
from .sqlite_callsign_ops import shuffle_key as callsign_shuffle_key
from .sqlite_delivery_ops import claim_delivery as claim_delivery_operation
from .sqlite_delivery_ops import finish_delivery as finish_delivery_operation
from .sqlite_lifecycle_ops import agent_status as agent_status_operation
from .sqlite_lifecycle_ops import transfer_task_owner as transfer_task_owner_operation
from .sqlite_lifecycle_ops import transition as transition_operation
from .sqlite_project_ops import canonical_repository
from .sqlite_project_ops import list_projects as list_projects_operation
from .sqlite_project_ops import project_advice as project_advice_operation
from .sqlite_project_ops import orchestration_decision as orchestration_decision_operation
from .sqlite_project_ops import put_project as put_project_operation
from .sqlite_project_ops import resolve_project as resolve_project_operation
from .sqlite_project_ops import set_project_suggestions as set_project_suggestions_operation
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
from .sqlite_progress_ops import emit_request_progress as emit_request_progress_operation
from .sqlite_progress_ops import reconcile_request_progress as reconcile_request_progress_operation
from .sqlite_rollover_ops import abort_rollover as abort_rollover_operation
from .sqlite_rollover_ops import acknowledge_rollover as acknowledge_rollover_operation
from .sqlite_rollover_ops import commit_rollover as commit_rollover_operation
from .sqlite_rollover_ops import complete_rollover_drain as complete_rollover_drain_operation
from .sqlite_rollover_ops import prepare_rollover as prepare_rollover_operation
from .sqlite_rollover_ops import rollover_bindings as rollover_bindings_operation
from .sqlite_rollover_ops import rollover_status as rollover_status_operation
from .sqlite_rollover_ops import rollover_cleanup_target as rollover_cleanup_target_operation
from .sqlite_roster_ops import roster_snapshot as roster_snapshot_operation
from .sqlite_report_ops import generate_report as generate_report_operation
from .sqlite_report_ops import record_activity_evidence as record_activity_evidence_operation
from .sqlite_report_ops import report_spec as report_spec_operation
from .sqlite_squad_ops import accept_squad as accept_squad_operation
from .sqlite_squad_ops import register_squad as register_squad_operation
from .sqlite_squad_ops import squad_status as squad_status_operation
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
from .storage_assignment import FinishHiddenAssignmentCommand, PrepareAssignmentCommand
from .storage_outbox import OutboxDispatchIdentity
from .storage_request import (
    AnswerRequestCommand,
    DispatchRequestCommand,
    RequestProgressCommand,
    RequestResultCommand,
)
from .storage_watcher import RuntimeRegistrationCommand
from .storage_types import LIFECYCLE_STATES
from .sqlite_handoff_schema import MIGRATION_NAME as HANDOFF_MIGRATION_NAME
from .sqlite_handoff_schema import STATEMENTS as HANDOFF_MIGRATION_STATEMENTS
from .sqlite_routing_policy_schema import MIGRATION_NAME as ROUTING_POLICY_MIGRATION_NAME
from .sqlite_routing_policy_schema import STATEMENTS as ROUTING_POLICY_MIGRATION_STATEMENTS


WAL_MINIMUM = (3, 51, 3)
CURRENT_SCHEMA_VERSION = 11
DATABASE_NAME = "league.sqlite3"
DEFAULT_BUSY_TIMEOUT_MS = 500
MAX_BUSY_TIMEOUT_MS = 10_000
MAX_EXPORT_RECORDS = 10_000
MAX_EXPORT_PAYLOAD_BYTES = 16 * 1024 * 1024

@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    rebuilds_foreign_keys: bool = False

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
            "CREATE INDEX ix_outbox_recipient_state ON delivery_outbox(recipient_agent_id,state,available_at)",
            "CREATE INDEX ix_assignments_state ON task_assignments(coordinator_agent_id,state,updated_at)",
            "CREATE INDEX ix_tasks_coordinator_state ON tasks(coordinator_agent_id,state,task_id)",
            "CREATE INDEX ix_obligations_due ON obligations(owner_agent_id,state,next_attention_at,created_at)",
        ),
    ),
    Migration(
        4,
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
              endpoint_generation TEXT NOT NULL,
              capabilities_json TEXT NOT NULL,
              state TEXT NOT NULL CHECK (state IN ('active','idle','interrupted','closing','closed','failed')),
              version INTEGER NOT NULL CHECK (version > 0),
              exit_fence INTEGER NOT NULL DEFAULT 0 CHECK (exit_fence >= 0),
              exit_executor_id TEXT,
              exit_leased_until TEXT,
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
            "ALTER TABLE cleanup_obligations RENAME TO cleanup_obligations_v3",
            """
            CREATE TABLE cleanup_obligations (
              cleanup_obligation_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
              cleanup_state TEXT NOT NULL CHECK (
                cleanup_state IN ('not_due','pending','cleanup_pending','awaiting_authority','verifying','planned','executing','blocked','completed','cleanup_completed')
              ),
              required_policy TEXT NOT NULL,
              next_action TEXT NOT NULL,
              version INTEGER NOT NULL CHECK (version > 0),
              updated_at TEXT NOT NULL,
              owner_id TEXT,
              task_class TEXT CHECK (
                task_class IS NULL OR task_class IN ('analysis','local_git','pr_ci','deployed_service')
              ),
              disposition TEXT CHECK (
                disposition IS NULL OR disposition IN ('completed','rejected','cancelled','failed')
              ),
              CHECK (
                (owner_id IS NULL AND task_class IS NULL AND disposition IS NULL)
                OR (owner_id IS NOT NULL AND task_class IS NOT NULL AND disposition IS NOT NULL)
              )
            )
            """,
            """
            INSERT INTO cleanup_obligations
              (cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,version,updated_at)
            SELECT cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,version,updated_at
              FROM cleanup_obligations_v3
            """,
            "DROP TABLE cleanup_obligations_v3",
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
            "CREATE UNIQUE INDEX ux_routing_escalation_child ON model_routing_decisions(prior_decision_id) WHERE prior_decision_id IS NOT NULL",
            "CREATE INDEX ix_routing_outcomes ON model_routing_outcomes(decision_id,recorded_at)",
            "CREATE INDEX ix_task_resources_task ON task_resources(task_id,state,resource_id)",
            "CREATE INDEX ix_cleanup_state ON cleanup_obligations(cleanup_state,updated_at)",
            "CREATE INDEX ix_cleanup_actions ON cleanup_actions(operation_id,ordinal,state)",
        ),
    ),
    Migration(
        5,
        "advisory-project-catalog-and-roster-indexes",
        (
            "ALTER TABLE projects ADD COLUMN summary TEXT NOT NULL DEFAULT 'Imported project'",
            "ALTER TABLE projects ADD COLUMN root_path TEXT",
            "ALTER TABLE projects ADD COLUMN repository_key TEXT",
            "ALTER TABLE projects ADD COLUMN root_key TEXT",
            "ALTER TABLE projects ADD COLUMN code TEXT",
            "ALTER TABLE projects ADD COLUMN code_key TEXT",
            "UPDATE projects SET repository_key=league_repository_key(repository) WHERE repository_key IS NULL",
            """
            CREATE TABLE project_aliases (
              project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
              alias TEXT NOT NULL,
              alias_key TEXT NOT NULL,
              position INTEGER NOT NULL CHECK (position >= 0),
              PRIMARY KEY (project_id, alias_key),
              UNIQUE (project_id, position)
            )
            """,
            """
            CREATE TABLE project_squad_suggestions (
              project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
              squad_id TEXT NOT NULL REFERENCES squads(squad_id),
              position INTEGER NOT NULL CHECK (position >= 0),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (project_id, squad_id),
              UNIQUE (project_id, position)
            )
            """,
            "CREATE INDEX ix_projects_repository_key ON projects(repository_key,project_id)",
            "CREATE INDEX ix_projects_root_key ON projects(root_key,project_id)",
            "CREATE INDEX ix_projects_code_key ON projects(code_key,project_id)",
            "CREATE INDEX ix_project_alias_lookup ON project_aliases(alias_key,project_id)",
            "CREATE INDEX ix_project_suggestion_squad ON project_squad_suggestions(squad_id,project_id)",
            "CREATE INDEX ix_roster_tasks_project_state ON tasks(project_id,state,updated_at,task_id)",
            "CREATE INDEX ix_roster_agents_task_state ON agent_instances(task_id,status,updated_at,agent_id)",
            "CREATE INDEX ix_roster_requests_state ON requests(state,updated_at,request_id)",
        ),
    ),
    Migration(
        6,
        HANDOFF_MIGRATION_NAME,
        HANDOFF_MIGRATION_STATEMENTS,
        rebuilds_foreign_keys=True,
    ),
    Migration(
        7,
        "bounded-reporting-and-outbound-privacy",
        (
            "ALTER TABLE projects ADD COLUMN repository_visibility TEXT NOT NULL DEFAULT 'unknown' CHECK (repository_visibility IN ('public','private','unknown'))",
            "ALTER TABLE projects ADD COLUMN export_policy TEXT NOT NULL DEFAULT 'deny' CHECK (export_policy IN ('deny','metadata_only','public_repository'))",
            "ALTER TABLE projects ADD COLUMN root_classification TEXT NOT NULL DEFAULT 'local_only' CHECK (root_classification='local_only')",
            "ALTER TABLE projects ADD COLUMN repository_classification TEXT NOT NULL DEFAULT 'local_only' CHECK (repository_classification IN ('local_only','outbound_safe'))",
            "CREATE INDEX ix_projects_visibility_policy ON projects(repository_visibility,export_policy,project_id)",
            "CREATE INDEX ix_report_prompts_created ON prompts(created_at,prompt_id)",
            "CREATE INDEX ix_report_requests_updated ON requests(updated_at,request_id)",
            "CREATE INDEX ix_report_dispatches_decided ON request_dispatches(decided_at,dispatch_id)",
            "CREATE INDEX ix_report_tasks_updated ON tasks(updated_at,task_id)",
            "CREATE INDEX ix_report_assignments_created ON task_assignments(created_at,task_assignment_id)",
            "CREATE INDEX ix_report_assignments_updated ON task_assignments(updated_at,task_assignment_id)",
            "CREATE INDEX ix_report_callsign_reserved ON callsign_assignments(reserved_at,callsign_assignment_id)",
            "CREATE INDEX ix_report_callsign_activated ON callsign_assignments(activated_at,callsign_assignment_id) WHERE activated_at IS NOT NULL",
            "CREATE INDEX ix_report_callsign_released ON callsign_assignments(released_at,callsign_assignment_id) WHERE released_at IS NOT NULL",
            "CREATE INDEX ix_report_transitions_created ON task_transitions(created_at,transition_id)",
            "CREATE INDEX ix_report_rollovers_updated ON rollover_operations(updated_at,operation_id)",
            "CREATE INDEX ix_report_routing_chosen ON model_routing_decisions(chosen_at,decision_id)",
            "CREATE INDEX ix_report_runtime_created ON runtime_bindings(created_at,binding_id)",
            "CREATE INDEX ix_report_runtime_updated ON runtime_bindings(updated_at,binding_id)",
            "CREATE INDEX ix_report_resources_registered ON task_resources(registered_at,resource_id)",
            "CREATE INDEX ix_report_resources_updated ON task_resources(updated_at,resource_id)",
            "CREATE INDEX ix_report_cleanup_updated ON cleanup_obligations(updated_at,cleanup_obligation_id)",
            "CREATE INDEX ix_report_cleanup_receipts_recorded ON cleanup_action_receipts(recorded_at,action_id)",
            "CREATE INDEX ix_report_teardown_completed ON teardown_receipts(completed_at,receipt_id)",
            "CREATE INDEX ix_report_outbox_available ON delivery_outbox(available_at,outbox_id)",
            "CREATE INDEX ix_report_obligations_created ON obligations(created_at,obligation_id)",
            """
            CREATE TABLE activity_evidence (
              evidence_id TEXT PRIMARY KEY,
              evidence_kind TEXT NOT NULL CHECK (
                evidence_kind IN ('issue','commit','pull_request','check','merge','install',
                                  'deployment','smoke','rollback','teardown','resource','authority',
                                  'handoff','continuation','repair')
              ),
              action TEXT NOT NULL,
              owner_agent_id TEXT REFERENCES agent_instances(agent_id),
              squad_id TEXT REFERENCES squads(squad_id),
              project_id TEXT REFERENCES projects(project_id),
              request_id TEXT REFERENCES requests(request_id),
              task_id TEXT REFERENCES tasks(task_id),
              state TEXT NOT NULL CHECK (state IN ('pending','succeeded','failed','cancelled','blocked')),
              verification TEXT NOT NULL CHECK (verification IN ('verified','unverified','unknown')),
              summary TEXT NOT NULL,
              summary_classification TEXT NOT NULL DEFAULT 'outbound_safe'
                CHECK (summary_classification='outbound_safe'),
              public_url TEXT,
              object_hash TEXT,
              local_evidence_ref TEXT,
              local_evidence_json TEXT,
              local_evidence_hash TEXT,
              local_evidence_classification TEXT NOT NULL DEFAULT 'local_only'
                CHECK (local_evidence_classification='local_only'),
              stable_repair_id TEXT,
              repair_phase TEXT CHECK (
                repair_phase IS NULL OR repair_phase IN ('failure','attempt','fix','final')
              ),
              root_cause_tag TEXT,
              owning_issue_url TEXT,
              required_for_completion INTEGER NOT NULL DEFAULT 0
                CHECK (required_for_completion IN (0,1)),
              occurred_at TEXT NOT NULL,
              CHECK (evidence_kind!='repair' OR stable_repair_id IS NOT NULL),
              CHECK ((local_evidence_ref IS NULL) = (local_evidence_hash IS NULL)),
              CHECK ((local_evidence_json IS NULL) = (local_evidence_hash IS NULL))
            )
            """,
            "CREATE INDEX ix_activity_evidence_time ON activity_evidence(occurred_at,evidence_id)",
            "CREATE INDEX ix_activity_evidence_owner ON activity_evidence(owner_agent_id,occurred_at,evidence_id)",
            "CREATE INDEX ix_activity_evidence_squad ON activity_evidence(squad_id,occurred_at,evidence_id)",
            "CREATE INDEX ix_activity_evidence_project ON activity_evidence(project_id,occurred_at,evidence_id)",
            "CREATE INDEX ix_activity_evidence_task ON activity_evidence(task_id,occurred_at,evidence_id)",
            "CREATE INDEX ix_activity_evidence_repair ON activity_evidence(stable_repair_id,occurred_at,evidence_id)",
            """
            CREATE TABLE report_specs (
              report_id TEXT PRIMARY KEY,
              report_schema TEXT NOT NULL CHECK (report_schema='league.report.v1'),
              from_at TEXT NOT NULL,
              to_at TEXT NOT NULL,
              timezone TEXT NOT NULL,
              from_inclusive INTEGER NOT NULL CHECK (from_inclusive IN (0,1)),
              scope_kind TEXT NOT NULL CHECK (scope_kind IN ('owner','squad','project','all')),
              scope_id TEXT,
              event_watermark INTEGER NOT NULL CHECK (event_watermark >= 0),
              source_watermark TEXT NOT NULL CHECK (length(source_watermark)=64),
              created_at TEXT NOT NULL,
              spec_hash TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              fact_count INTEGER NOT NULL CHECK (fact_count >= 0),
              CHECK ((scope_kind='all') = (scope_id IS NULL))
            )
            """,
            "CREATE INDEX ix_report_specs_created ON report_specs(created_at,report_id)",
        ),
    ),
    Migration(
        8,
        ROUTING_POLICY_MIGRATION_NAME,
        ROUTING_POLICY_MIGRATION_STATEMENTS,
        rebuilds_foreign_keys=True,
    ),
    Migration(
        9,
        "repository-owned-artifact-publication",
        (
            """
            CREATE TABLE repository_artifacts (
              artifact_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              name TEXT NOT NULL,
              classification TEXT NOT NULL CHECK (classification='repository_owned'),
              repository TEXT NOT NULL,
              issue INTEGER NOT NULL CHECK (issue > 0),
              worktree TEXT NOT NULL,
              branch TEXT NOT NULL CHECK (lower(branch) NOT IN ('main','master')),
              repository_path TEXT NOT NULL,
              pull_request_number INTEGER CHECK (pull_request_number IS NULL OR pull_request_number > 0),
              pull_request_url TEXT,
              tested_head TEXT,
              merge_commit TEXT,
              merge_url TEXT,
              merge_receipt_json TEXT,
              state TEXT NOT NULL CHECK (state IN ('pending','published')),
              version INTEGER NOT NULL CHECK (version > 0),
              declared_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              CHECK (
                state!='published'
                OR (pull_request_number IS NOT NULL AND pull_request_url IS NOT NULL
                    AND tested_head IS NOT NULL AND length(tested_head)=40
                    AND merge_commit IS NOT NULL AND length(merge_commit)=40
                    AND merge_url IS NOT NULL AND merge_receipt_json IS NOT NULL)
              )
            )
            """,
            "CREATE INDEX ix_repository_artifacts_task ON repository_artifacts(task_id,state,artifact_id)",
        ),
    ),
    Migration(
        10,
        "prompt-runtime-quarantine",
        (
            """
            CREATE TABLE prompt_quarantine (
              prompt_id TEXT PRIMARY KEY,
              adapter_kind TEXT NOT NULL,
              session_ref TEXT NOT NULL,
              source_event_key TEXT NOT NULL,
              body TEXT NOT NULL,
              body_hash TEXT NOT NULL,
              byte_count INTEGER NOT NULL CHECK (byte_count > 0),
              state TEXT NOT NULL CHECK (state IN ('quarantined','bound')),
              reason TEXT NOT NULL CHECK (reason='runtime_unverified'),
              bound_actor_id TEXT REFERENCES agent_instances(agent_id),
              bound_runtime_instance_id TEXT REFERENCES runtime_instances(runtime_instance_id),
              created_at TEXT NOT NULL,
              bound_at TEXT,
              UNIQUE (adapter_kind,session_ref,source_event_key)
            )
            """,
            "CREATE INDEX ix_prompt_quarantine_state ON prompt_quarantine(state,created_at,prompt_id)",
        ),
    ),
    Migration(
        11,
        "prompt-quarantine-watcher-generation",
        (
            "ALTER TABLE prompt_quarantine ADD COLUMN wake_actor_id TEXT REFERENCES agent_instances(agent_id)",
            "ALTER TABLE prompt_quarantine ADD COLUMN wake_scope_id TEXT",
            "ALTER TABLE prompt_quarantine ADD COLUMN wake_committed INTEGER NOT NULL DEFAULT 0 CHECK (wake_committed IN (0,1))",
        ),
    ),
)


_IMPORT_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": (
        "project_id", "repository", "state", "version", "updated_at", "summary",
        "root_path", "repository_key", "root_key", "code", "code_key",
        "repository_visibility", "export_policy", "root_classification",
        "repository_classification",
    ),
    "project_aliases": ("project_id", "alias", "alias_key", "position"),
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
    "project_squad_suggestions": (
        "project_id", "squad_id", "position", "created_at", "updated_at",
    ),
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
    "activity_evidence": (
        "evidence_id", "evidence_kind", "action", "owner_agent_id", "squad_id",
        "project_id", "request_id", "task_id", "state", "verification", "summary",
        "summary_classification", "public_url", "object_hash", "local_evidence_ref",
        "local_evidence_json", "local_evidence_hash", "local_evidence_classification", "stable_repair_id",
        "repair_phase", "root_cause_tag", "owning_issue_url",
        "required_for_completion", "occurred_at",
    ),
    "report_specs": (
        "report_id", "report_schema", "from_at", "to_at", "timezone",
        "from_inclusive", "scope_kind", "scope_id", "event_watermark", "source_watermark", "created_at",
        "spec_hash", "content_hash", "fact_count",
    ),
}

_IMPORT_ORDER = tuple(_IMPORT_COLUMNS)
_EXPORT_TABLES = (
    "schema_migrations",
    "projects",
    "project_aliases",
    "tasks",
    "callsigns",
    "callsign_queue_meta",
    "callsign_capabilities",
    "callsign_queue",
    "agent_instances",
    "squads",
    "shotcaller_intake",
    "squad_champions",
    "squad_capabilities",
    "squad_registration_offers",
    "project_squad_suggestions",
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
    "prompt_quarantine",
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
    "rollover_operations",
    "active_champion_snapshots",
    "active_champion_snapshot_rows",
    "task_assignments",
    "task_transitions",
    "delivery_outbox",
    "outbox_dispatch_leases",
    "delivery_attempts",
    "recipient_receipts",
    "watcher_registrations",
    "obligations",
    "cleanup_obligations",
    "runtime_bindings",
    "model_routing_decisions",
    "model_routing_outcomes",
    "request_progress_events",
    "request_progress_buffers",
    "task_resources",
    "cleanup_operations",
    "cleanup_actions",
    "cleanup_action_receipts",
    "teardown_receipts",
    "activity_evidence",
    "report_specs",
    "repository_artifacts",
)

_EXPORT_ORDER = {
    "schema_migrations": "version",
    "projects": "project_id",
    "project_aliases": "project_id,position,alias_key",
    "tasks": "task_id",
    "callsigns": "pool_role,pool_position,callsign",
    "callsign_queue_meta": "pool_role",
    "callsign_capabilities": "callsign,capability",
    "callsign_queue": "pool_role,queue_position,callsign",
    "agent_instances": "agent_id",
    "squads": "squad_id",
    "shotcaller_intake": "squad_id,agent_id",
    "squad_champions": "squad_id,champion_agent_id",
    "squad_capabilities": "squad_id,capability",
    "squad_registration_offers": "registered_at,registration_id",
    "project_squad_suggestions": "project_id,position,squad_id",
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
    "prompt_quarantine": "created_at,prompt_id",
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
    "rollover_operations": "created_at,operation_id",
    "active_champion_snapshots": "created_at,snapshot_id",
    "active_champion_snapshot_rows": "snapshot_id,ordinal",
    "task_assignments": "created_at,task_assignment_id",
    "task_transitions": "created_at,transition_id",
    "delivery_outbox": "available_at,outbox_id",
    "outbox_dispatch_leases": "outbox_id",
    "delivery_attempts": "started_at,attempt_id",
    "recipient_receipts": "received_at,event_id,recipient_agent_id",
    "watcher_registrations": "actor_agent_id,watcher_id",
    "obligations": "created_at,obligation_id",
    "cleanup_obligations": "task_id",
    "runtime_bindings": "binding_id",
    "model_routing_decisions": "chosen_at,decision_id",
    "model_routing_outcomes": "recorded_at,outcome_id",
    "request_progress_events": "emitted_at,progress_id",
    "request_progress_buffers": "due_at,request_id,recipient_agent_id",
    "task_resources": "task_id,resource_id",
    "cleanup_operations": "cleanup_obligation_id,cleanup_revision",
    "cleanup_actions": "operation_id,ordinal",
    "cleanup_action_receipts": "operation_id,action_id",
    "teardown_receipts": "task_id,receipt_id",
    "activity_evidence": "occurred_at,evidence_id",
    "report_specs": "created_at,report_id",
    "repository_artifacts": "task_id,artifact_id",
}

_INSPECTION_REDACTIONS = {
    "projects": {"repository", "root_path", "repository_key", "root_key", "summary"},
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
    "prompt_quarantine": {"session_ref", "body"},
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
    "task_assignments": {
        "acceptance_receipt_json",
        "failure_class",
        "bounded_subtask",
        "model",
        "result_summary",
        "cleanup_receipt",
        "unpublished_state_receipt",
    },
    "rollover_operations": {"plan_json"},
    "task_transitions": {"update_text", "next_action", "blocker"},
    "delivery_attempts": {"outcome"},
    "watcher_registrations": {"wake_locator"},
    "obligations": {"details_json"},
    "runtime_bindings": {
        "session_identity",
        "endpoint_identity",
        "endpoint_generation",
        "capabilities_json",
        "last_receipt_json",
    },
    "model_routing_decisions": {"model", "reason"},
    "request_progress_events": {"current_phase", "deadline_change", "next_action"},
    "request_progress_buffers": {"current_phase", "deadline_change", "next_action"},
    "task_resources": {"expected_identity_json", "applicability_reason"},
    "cleanup_actions": {"expected_identity_json", "intended_state_json"},
    "cleanup_action_receipts": {"before_json", "after_json", "adapter_receipt_json"},
    "activity_evidence": {"local_evidence_ref", "local_evidence_json"},
    "repository_artifacts": {"worktree"},
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
            self.connection.create_function(
                "league_repository_key",
                1,
                lambda value: canonical_repository(str(value))[1],
                deterministic=True,
            )
            self.connection.create_function(
                "league_shuffle_key",
                3,
                callsign_shuffle_key,
                deterministic=True,
            )
            if not self._database_existed:
                os.chmod(self.database, 0o600)
            loaded = tuple(int(item) for item in sqlite3.sqlite_version_info[:3])
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            if allow_create:
                requested_mode, refusal = journal_policy(
                    loaded, request_wal=request_wal
                )
                actual_mode = str(
                    self.connection.execute(
                        f"PRAGMA journal_mode={requested_mode}"
                    ).fetchone()[0]
                ).upper()
                wal_allowed = requested_mode == "WAL"
            else:
                actual_mode = str(
                    self.connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).upper()
                if actual_mode not in {"DELETE", "WAL"}:
                    raise StorageRefusal(
                        "journal_mode_unsupported",
                        f"established canonical journal mode {actual_mode} is unsupported",
                    )
                if actual_mode == "WAL" and loaded < WAL_MINIMUM:
                    raise StorageRefusal(
                        "wal_runtime_unsupported",
                        "established WAL mode requires SQLite 3.51.3 or newer",
                    )
                wal_allowed = actual_mode == "WAL"
                refusal = None if wal_allowed else "canonical_delete_mode"
            self.connection.execute("PRAGMA synchronous=FULL")
            foreign_keys = bool(self.connection.execute("PRAGMA foreign_keys").fetchone()[0])
            synchronous = int(self.connection.execute("PRAGMA synchronous").fetchone()[0])
        except StorageRefusal:
            if hasattr(self, "connection"):
                self.connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise self._translate_database_error(exc, "storage open failed") from exc
        if not foreign_keys:
            self.connection.close()
            raise StorageRefusal("foreign_keys_unavailable", "foreign-key enforcement could not be enabled")
        if allow_create and actual_mode != requested_mode:
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
            wal_allowed=wal_allowed,
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
        rebuilds_foreign_keys = any(item.rebuilds_foreign_keys for item in pending)
        try:
            if rebuilds_foreign_keys:
                self.connection.execute("PRAGMA foreign_keys=OFF")
                if self.connection.execute("PRAGMA foreign_keys").fetchone()[0]:
                    raise StorageRefusal(
                        "migration_policy_refused",
                        "foreign-key rebuild mode could not be entered",
                    )
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
                if rebuilds_foreign_keys:
                    violations = list(self.connection.execute("PRAGMA foreign_key_check"))
                    if violations:
                        raise StorageRefusal(
                            "migration_foreign_key_violation",
                            "migration produced invalid foreign-key references",
                        )
        except StorageRefusal:
            raise
        except sqlite3.DatabaseError as exc:
            raise self._translate_database_error(exc, "transactional migration failed") from exc
        finally:
            if rebuilds_foreign_keys:
                self.connection.execute("PRAGMA foreign_keys=ON")
                if not self.connection.execute("PRAGMA foreign_keys").fetchone()[0]:
                    raise StorageRefusal(
                        "foreign_keys_unavailable",
                        "foreign-key enforcement could not be restored after migration",
                    )
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

    def reconcile_callsign_pool(
        self,
        role: str,
        expected_queue_version: int,
        seed: str,
        shuffle_version: int,
        entries: Sequence[dict[str, Any]],
        at: str,
    ) -> dict[str, Any]:
        return reconcile_callsign_pool_operation(
            self,
            role,
            expected_queue_version,
            seed,
            shuffle_version,
            entries,
            at,
        )

    def allocate_callsign(
        self,
        assignment_id: str,
        agent_id: str,
        role: str,
        scope_kind: str,
        scope_id: str,
        required_capabilities: Sequence[str],
        at: str,
        *,
        fault: Optional[FaultInjector] = None,
    ) -> dict[str, Any]:
        return allocate_callsign_operation(
            self,
            assignment_id,
            agent_id,
            role,
            scope_kind,
            scope_id,
            required_capabilities,
            at,
            fault=fault,
        )

    def activate_callsign(
        self,
        assignment_id: str,
        expected_version: int,
        receipt: dict[str, Any],
        at: str,
    ) -> dict[str, Any]:
        return activate_callsign_operation(
            self, assignment_id, expected_version, receipt, at
        )

    def rollback_callsign(
        self,
        assignment_id: str,
        expected_version: int,
        failure_receipt_digest: str,
        at: str,
    ) -> dict[str, Any]:
        return rollback_callsign_operation(
            self,
            assignment_id,
            expected_version,
            failure_receipt_digest,
            at,
        )

    def release_callsign(
        self,
        assignment_id: str,
        expected_version: int,
        release_receipt_digest: str,
        at: str,
    ) -> dict[str, Any]:
        return release_callsign_operation(
            self,
            assignment_id,
            expected_version,
            release_receipt_digest,
            at,
        )

    def callsign_status(self, role: str) -> dict[str, Any]:
        return callsign_status_operation(self, role)

    def prepare_rollover(
        self,
        operation_id: str,
        squad_id: str,
        predecessor_agent_id: str,
        successor_agent_id: str,
        callsign_assignment_id: str,
        expected_owner_version: int,
        expected_owner_fence: int,
        authority_kind: str,
        authority_digest: str,
        required_capabilities: Sequence[str],
        plan: dict[str, Any],
        at: str,
    ) -> dict[str, Any]:
        return prepare_rollover_operation(
            self,
            operation_id,
            squad_id,
            predecessor_agent_id,
            successor_agent_id,
            callsign_assignment_id,
            expected_owner_version,
            expected_owner_fence,
            authority_kind,
            authority_digest,
            required_capabilities,
            plan,
            at,
        )

    def rollover_bindings(
        self,
        operation_id: str,
        at: str,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        return rollover_bindings_operation(
            self, operation_id, at, cursor=cursor, limit=limit
        )

    def acknowledge_rollover(
        self,
        operation_id: str,
        successor_agent_id: str,
        runtime_instance_id: str,
        handoff_digest: str,
        snapshot_version: int,
        snapshot_count: int,
        snapshot_digest: str,
        pages: Sequence[dict[str, Any]],
        at: str,
    ) -> dict[str, Any]:
        return acknowledge_rollover_operation(
            self,
            operation_id,
            successor_agent_id,
            runtime_instance_id,
            handoff_digest,
            snapshot_version,
            snapshot_count,
            snapshot_digest,
            pages,
            at,
        )

    def commit_rollover(
        self,
        operation_id: str,
        expected_owner_version: int,
        expected_owner_fence: int,
        owner_event_id: str,
        owner_outbox_id: str,
        at: str,
        *,
        fault: Optional[FaultInjector] = None,
    ) -> dict[str, Any]:
        return commit_rollover_operation(
            self,
            operation_id,
            expected_owner_version,
            expected_owner_fence,
            owner_event_id,
            owner_outbox_id,
            at,
            fault=fault,
        )

    def abort_rollover(
        self,
        operation_id: str,
        expected_version: int,
        cleanup_receipt: dict[str, Any],
        at: str,
    ) -> dict[str, Any]:
        return abort_rollover_operation(
            self, operation_id, expected_version, cleanup_receipt, at
        )

    def complete_rollover_drain(
        self,
        operation_id: str,
        expected_version: int,
        cleanup_receipt: dict[str, Any],
        at: str,
    ) -> dict[str, Any]:
        return complete_rollover_drain_operation(
            self, operation_id, expected_version, cleanup_receipt, at
        )

    def rollover_status(self, operation_id: str) -> Optional[dict[str, Any]]:
        return rollover_status_operation(self, operation_id)

    def rollover_cleanup_target(self, operation_id: str) -> Optional[dict[str, Any]]:
        return rollover_cleanup_target_operation(self, operation_id)

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

    def put_project(
        self,
        project_id: str,
        *,
        expected_version: int,
        summary: str,
        repository: str,
        root: str,
        code: Optional[str],
        aliases: Sequence[str],
        state: str,
        repository_visibility: str,
        export_policy: str,
        at: str,
    ) -> dict[str, Any]:
        return put_project_operation(
            self,
            project_id,
            expected_version=expected_version,
            summary=summary,
            repository=repository,
            root=root,
            code=code,
            aliases=aliases,
            state=state,
            repository_visibility=repository_visibility,
            export_policy=export_policy,
            at=at,
        )

    def set_project_suggestions(
        self,
        project_id: str,
        expected_version: int,
        squad_ids: Sequence[str],
        at: str,
    ) -> dict[str, Any]:
        return set_project_suggestions_operation(
            self, project_id, expected_version, squad_ids, at
        )

    def resolve_project(
        self,
        repository: Optional[str] = None,
        *,
        project_id: Optional[str] = None,
        root: Optional[str] = None,
        code: Optional[str] = None,
        alias: Optional[str] = None,
        visibility: str = "local",
    ) -> Optional[dict[str, Any]]:
        return resolve_project_operation(
            self,
            repository,
            project_id=project_id,
            root=root,
            code=code,
            alias=alias,
            visibility=visibility,
        )

    def list_projects(
        self, *, visibility: str = "local", limit: int = 200
    ) -> dict[str, Any]:
        return list_projects_operation(self, visibility=visibility, limit=limit)

    def project_advice(
        self,
        project_id: str,
        *,
        explicit_squad_id: Optional[str] = None,
        visibility: str = "local",
    ) -> dict[str, Any]:
        return project_advice_operation(
            self,
            project_id,
            explicit_squad_id=explicit_squad_id,
            visibility=visibility,
        )

    def orchestration_decision(
        self,
        signals: Any,
        *,
        project_ids: Sequence[str] = (),
        explicit_squad_id: Optional[str] = None,
        continuation_squad_id: Optional[str] = None,
        required_capabilities: Sequence[str] = (),
    ) -> dict[str, object]:
        return orchestration_decision_operation(
            self,
            signals,
            project_ids=project_ids,
            explicit_squad_id=explicit_squad_id,
            continuation_squad_id=continuation_squad_id,
            required_capabilities=required_capabilities,
        )

    def roster_snapshot(
        self,
        *,
        as_of: str,
        recent_since: str,
        stale_before: str,
        limit: int = 500,
        visibility: str = "outbound",
    ) -> dict[str, Any]:
        return roster_snapshot_operation(
            self,
            as_of=as_of,
            recent_since=recent_since,
            stale_before=stale_before,
            limit=limit,
            visibility=visibility,
        )

    def record_activity_evidence(self, evidence: dict[str, Any]) -> dict[str, Any]:
        return record_activity_evidence_operation(self, evidence)

    def declare_repository_artifact(
        self, declaration: dict[str, Any], at: str
    ) -> dict[str, Any]:
        return declare_repository_artifact_operation(self, declaration, at)

    def record_repository_publication(
        self,
        artifact_id: str,
        expected_version: int,
        receipt: dict[str, Any],
        at: str,
    ) -> dict[str, Any]:
        return record_repository_publication_operation(
            self, artifact_id, expected_version, receipt, at
        )

    def task_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        return task_artifacts_operation(self, task_id)

    def unresolved_repository_publications(
        self, task_id: str
    ) -> list[dict[str, Any]]:
        return unresolved_repository_publications_operation(self, task_id)

    def generate_report(
        self,
        *,
        from_at: str,
        to_at: str,
        timezone_name: str,
        from_inclusive: bool,
        scope_kind: str,
        scope_id: Optional[str],
        limit: int,
        cursor: Optional[str],
        local_diagnostic: bool,
        report_id: Optional[str] = None,
        event_watermark: Optional[int] = None,
        source_watermark: Optional[str] = None,
        persist: bool = True,
        expected_content_hash: Optional[str] = None,
    ) -> dict[str, Any]:
        return generate_report_operation(
            self,
            from_at=from_at,
            to_at=to_at,
            timezone_name=timezone_name,
            from_inclusive=from_inclusive,
            scope_kind=scope_kind,
            scope_id=scope_id,
            limit=limit,
            cursor=cursor,
            local_diagnostic=local_diagnostic,
            report_id=report_id,
            event_watermark=event_watermark,
            source_watermark=source_watermark,
            persist=persist,
            expected_content_hash=expected_content_hash,
        )

    def report_spec(self, report_id: str) -> Optional[dict[str, Any]]:
        return report_spec_operation(self, report_id)

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
        *,
        wake_scope_id: Optional[str] = None,
        wake: bool = True,
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
            wake_scope_id=wake_scope_id,
            wake=wake,
        )

    def triage_prompt(
        self, prompt_id: str, items: list[dict[str, Any]], at: str
    ) -> dict[str, Any]:
        return triage_prompt_operation(self, prompt_id, items, at)

    def quarantine_prompt(
        self,
        prompt_id: str,
        adapter_kind: str,
        session_ref: str,
        source_event_key: str,
        body: str,
        at: str,
        *,
        wake_actor_id: Optional[str] = None,
        wake_scope_id: Optional[str] = None,
    ) -> dict[str, Any]:
        from .sqlite_request_ops import quarantine_prompt

        return quarantine_prompt(
            self, prompt_id, adapter_kind, session_ref, source_event_key, body, at,
            wake_actor_id=wake_actor_id, wake_scope_id=wake_scope_id,
        )

    def bind_quarantined_prompt(
        self,
        prompt_id: str,
        intake_actor_id: str,
        runtime_instance_id: str,
        at: str,
        *,
        wake_scope_id: Optional[str] = None,
        wake: bool = True,
    ) -> dict[str, Any]:
        from .sqlite_request_ops import bind_quarantined_prompt

        return bind_quarantined_prompt(
            self,
            prompt_id,
            intake_actor_id,
            runtime_instance_id,
            at,
            wake_scope_id=wake_scope_id,
            wake=wake,
        )

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

    def dispatch_request(self, command: DispatchRequestCommand) -> dict[str, Any]:
        return dispatch_request_operation(self, command)

    def emit_request_progress(self, command: RequestProgressCommand) -> dict[str, Any]:
        return emit_request_progress_operation(self, command)

    def reconcile_request_progress(self, owner_agent_id: str, at: str) -> dict[str, Any]:
        return reconcile_request_progress_operation(self, owner_agent_id, at)

    def route_request(
        self,
        request_id: str,
        claim_token: str,
        expected_version: int,
        recipient_agent_id: str,
        event_id: str,
        outbox_id: str,
        at: str,
        *,
        recipient_squad_id: Optional[str] = None,
        route_reason_code: str = "explicit_squad",
        route_policy_version: str = "league.orchestration.v1",
        route_confidence: str = "explicit",
        required_capabilities: tuple[str, ...] = (),
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
            recipient_squad_id=recipient_squad_id,
            route_reason_code=route_reason_code,
            route_policy_version=route_policy_version,
            route_confidence=route_confidence,
            required_capabilities=required_capabilities,
        )

    def register_squad(self, **command: Any) -> dict[str, Any]:
        return register_squad_operation(self, **command)

    def accept_squad(self, **command: Any) -> dict[str, Any]:
        return accept_squad_operation(self, **command)

    def squad_status(self, **query: Any) -> dict[str, Any]:
        return squad_status_operation(self, **query)

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

    def record_request_result(self, command: RequestResultCommand) -> dict[str, Any]:
        return record_request_result_operation(self, command)

    def answer_request(self, command: AnswerRequestCommand) -> dict[str, Any]:
        return answer_request_operation(self, command)

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

    def prepare_assignment(self, command: PrepareAssignmentCommand) -> dict[str, Any]:
        return prepare_assignment_operation(self, command)

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

    def finish_hidden_assignment(
        self, command: FinishHiddenAssignmentCommand
    ) -> dict[str, Any]:
        return finish_hidden_assignment_operation(self, command)

    def reconcile_assignment_runtime(
        self, assignment_id: str, at: str
    ) -> dict[str, Any]:
        return reconcile_assignment_runtime_operation(self, assignment_id, at)

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
        identity: OutboxDispatchIdentity,
        lease_expires_at: str,
        at: str,
    ) -> dict[str, Any]:
        return claim_outbox_operation(self, identity, lease_expires_at, at)

    def acknowledge_outbox(
        self,
        identity: OutboxDispatchIdentity,
        fence: int,
        adapter_kind: str,
        effect_kind: str,
        effect_id: str,
        at: str,
    ) -> dict[str, Any]:
        return acknowledge_outbox_operation(
            self,
            identity,
            fence,
            adapter_kind,
            effect_kind,
            effect_id,
            at,
        )

    def fail_outbox(
        self,
        identity: OutboxDispatchIdentity,
        fence: int,
        adapter_kind: str,
        reason: str,
        retry_at: str,
        at: str,
    ) -> dict[str, Any]:
        return fail_outbox_operation(
            self,
            identity,
            fence,
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

    def register_runtime(self, command: RuntimeRegistrationCommand) -> dict[str, Any]:
        return register_runtime_operation(self, command)

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
        *,
        block_on_fresh_terminal: bool = False,
    ) -> dict[str, Any]:
        return stop_decision_operation(
            self,
            scope_id,
            actor_agent_id,
            terminal_generation,
            at,
            block_on_fresh_terminal=block_on_fresh_terminal,
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
            current_schema_version=CURRENT_SCHEMA_VERSION,
            columns_by_table=_IMPORT_COLUMNS,
            table_order=_IMPORT_ORDER,
            post_import=initialize_imported_callsign_state,
            fault=fault,
        )

    def export_bytes(self, *, format_name: str, purpose: str, max_records: int) -> bytes:
        return export_operation(
            self,
            format_name=format_name,
            purpose=purpose,
            max_records=max_records,
            maximum_records=MAX_EXPORT_RECORDS,
            maximum_payload_bytes=MAX_EXPORT_PAYLOAD_BYTES,
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
        endpoint_generation: str,
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
            endpoint_generation,
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

    def claim_runtime_exit(
        self,
        binding_id: str,
        expected_version: int,
        expected_fence: int,
        executor_id: str,
        leased_until: str,
        at: str,
    ) -> dict[str, Any]:
        return sqlite_runtime_ops.claim_runtime_exit(
            self,
            binding_id,
            expected_version,
            expected_fence,
            executor_id,
            leased_until,
            at,
        )

    def finalize_runtime_exit(
        self,
        binding_id: str,
        expected_version: int,
        fence: int,
        at: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        return sqlite_runtime_ops.finalize_runtime_exit(
            self, binding_id, expected_version, fence, at, receipt
        )

    def close_runtime_for_cleanup(
        self,
        runtime_instance_id: str,
        endpoint_identity: str,
        runtime_generation: str,
        at: str,
    ) -> dict[str, Any]:
        return sqlite_runtime_ops.close_runtime_for_cleanup(
            self,
            runtime_instance_id,
            endpoint_identity,
            runtime_generation,
            at,
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

    def cleanup_execution_context(self, operation_id: str) -> dict[str, Any]:
        return sqlite_runtime_ops.cleanup_execution_context(self, operation_id)

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

    def block_cleanup_operation(
        self,
        operation_id: str,
        fence: int,
        action_id: Optional[str],
        refusal_code: str,
        receipt: dict[str, Any],
        at: str,
    ) -> dict[str, Any]:
        return sqlite_runtime_ops.block_cleanup_operation(
            self, operation_id, fence, action_id, refusal_code, receipt, at
        )

    def release_resource_lease_for_cleanup(
        self, expected: dict[str, Any]
    ) -> dict[str, Any]:
        return sqlite_runtime_ops.release_resource_lease_for_cleanup(self, expected)

    def resource_lease_for_cleanup(
        self, resource_id: str
    ) -> Optional[dict[str, Any]]:
        return sqlite_runtime_ops.resource_lease_for_cleanup(self, resource_id)
