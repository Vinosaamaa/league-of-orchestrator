"""Schema for one adapter-neutral active Champion runtime replacement."""

from __future__ import annotations


MIGRATION_NAME = "adapter-neutral-champion-runtime-replacement"

STATEMENTS = (
    # The original backend check encoded the two bootstrap implementations in
    # canonical storage.  Adapter registration is the service boundary; SQLite
    # retains the exact non-empty multiplexer kind without predicting plugins.
    "PRAGMA defer_foreign_keys=ON",
    """
    CREATE TABLE agent_instances_v23 (
      agent_id TEXT PRIMARY KEY,
      callsign TEXT NOT NULL REFERENCES callsigns(callsign),
      role TEXT NOT NULL CHECK (role IN ('shotcaller','champion','hidden-worker')),
      shotcaller_agent_id TEXT REFERENCES agent_instances_v23(agent_id)
        DEFERRABLE INITIALLY DEFERRED,
      task_id TEXT REFERENCES tasks(task_id) DEFERRABLE INITIALLY DEFERRED,
      kind TEXT NOT NULL,
      address TEXT,
      thread_id TEXT,
      backend TEXT,
      routing_name TEXT,
      display_agent TEXT,
      repository TEXT,
      issue INTEGER CHECK (issue IS NULL OR issue > 0),
      branch TEXT,
      worktree TEXT,
      status TEXT NOT NULL CHECK (status IN ('active','started','working','progress','blocked','ready_to_land','completed','complete','failed','cancelled','canceled')),
      version INTEGER NOT NULL CHECK (version > 0),
      updated_at TEXT NOT NULL,
      update_text TEXT NOT NULL,
      blocker TEXT,
      next_action TEXT NOT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      retired_at TEXT,
      CHECK ((routing_name IS NULL) = (display_agent IS NULL)),
      CHECK (backend IS NULL OR length(backend) > 0)
    )
    """,
    """
    INSERT INTO agent_instances_v23
      (agent_id,callsign,role,shotcaller_agent_id,task_id,kind,address,thread_id,
       backend,routing_name,display_agent,repository,issue,branch,worktree,status,
       version,updated_at,update_text,blocker,next_action,metadata_json,retired_at)
    SELECT agent_id,callsign,role,shotcaller_agent_id,task_id,kind,address,thread_id,
           backend,routing_name,display_agent,repository,issue,branch,worktree,status,
           version,updated_at,update_text,blocker,next_action,metadata_json,retired_at
      FROM agent_instances
    """,
    "DROP TABLE agent_instances",
    "ALTER TABLE agent_instances_v23 RENAME TO agent_instances",
    "CREATE UNIQUE INDEX ux_live_callsign ON agent_instances(callsign) WHERE retired_at IS NULL",
    "CREATE INDEX ix_roster_agents_task_state ON agent_instances(task_id,status,updated_at,agent_id)",
    """
    CREATE TABLE runtime_replacements (
      operation_id TEXT PRIMARY KEY,
      assignment_id TEXT NOT NULL REFERENCES task_assignments(task_assignment_id),
      task_id TEXT NOT NULL REFERENCES tasks(task_id),
      predecessor_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      predecessor_runtime_instance_id TEXT NOT NULL REFERENCES runtime_instances(runtime_instance_id),
      successor_agent_id TEXT NOT NULL,
      successor_runtime_instance_id TEXT NOT NULL,
      successor_adapter_kind TEXT NOT NULL,
      successor_provider_kind TEXT NOT NULL,
      multiplexer_kind TEXT NOT NULL,
      canonical_routing_name TEXT NOT NULL,
      staging_routing_name TEXT NOT NULL,
      state TEXT NOT NULL CHECK (
        state IN ('prepared','successor_verified','activated','predecessor_retired','completed','rolled_back','recovery_required')
      ),
      intent_json TEXT NOT NULL,
      intent_digest TEXT NOT NULL CHECK (length(intent_digest)=64),
      successor_receipt_json TEXT,
      route_receipt_json TEXT,
      retirement_receipt_json TEXT,
      completion_receipt_json TEXT,
      handoff_event_id TEXT,
      handoff_outbox_id TEXT,
      rollback_receipt_json TEXT,
      failure_code TEXT,
      version INTEGER NOT NULL CHECK (version > 0),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX ux_runtime_replacement_open_assignment
      ON runtime_replacements(assignment_id)
     WHERE state IN ('prepared','successor_verified','activated','predecessor_retired','recovery_required')
    """,
    "CREATE UNIQUE INDEX ux_runtime_replacement_successor_agent ON runtime_replacements(successor_agent_id)",
    "CREATE UNIQUE INDEX ux_runtime_replacement_successor_runtime ON runtime_replacements(successor_runtime_instance_id)",
)
