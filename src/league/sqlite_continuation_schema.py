"""Schema for issue-coupled cleanup and exact provider-thread continuation."""

MIGRATION_NAME = "issue-coupled-cleanup-and-exact-thread-continuation"

STATEMENTS = (
    "DROP INDEX ux_runtime_session_identity",
    """
    CREATE UNIQUE INDEX ux_live_runtime_session_identity
      ON runtime_instances(harness_kind,session_ref)
      WHERE status IN ('active','idle')
    """,
    """
    CREATE TABLE thread_lineages (
      lineage_id TEXT PRIMARY KEY,
      provider_kind TEXT NOT NULL,
      thread_identity TEXT NOT NULL UNIQUE,
      resume_capabilities_json TEXT NOT NULL,
      policy_digest TEXT NOT NULL CHECK (length(policy_digest)=64),
      state TEXT NOT NULL CHECK (state IN ('archived','claimed','active','unavailable')),
      version INTEGER NOT NULL CHECK (version > 0),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      CHECK (instr(thread_identity,':') > 1)
    )
    """,
    """
    CREATE TABLE thread_archives (
      archive_id TEXT PRIMARY KEY,
      lineage_id TEXT NOT NULL REFERENCES thread_lineages(lineage_id),
      task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
      owner_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      runtime_instance_id TEXT NOT NULL UNIQUE REFERENCES runtime_instances(runtime_instance_id),
      cleanup_operation_id TEXT NOT NULL UNIQUE REFERENCES cleanup_operations(operation_id),
      cleanup_receipt_id TEXT UNIQUE REFERENCES teardown_receipts(receipt_id),
      repository TEXT NOT NULL,
      issue INTEGER NOT NULL CHECK (issue > 0),
      branch TEXT NOT NULL,
      worktree TEXT NOT NULL,
      prior_callsign TEXT NOT NULL,
      instruction_digest TEXT NOT NULL CHECK (length(instruction_digest)=64),
      context_health TEXT NOT NULL CHECK (context_health IN ('healthy','degraded','unhealthy','conflicted')),
      acceptance_json TEXT NOT NULL,
      cleanup_evidence_json TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('pending_cleanup','available','claimed','resumed','unavailable')),
      version INTEGER NOT NULL CHECK (version > 0),
      archived_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE thread_incarnations (
      lineage_id TEXT NOT NULL REFERENCES thread_lineages(lineage_id),
      runtime_instance_id TEXT NOT NULL UNIQUE REFERENCES runtime_instances(runtime_instance_id),
      archive_id TEXT REFERENCES thread_archives(archive_id),
      continuation_operation_id TEXT,
      bound_at TEXT NOT NULL,
      PRIMARY KEY (lineage_id,runtime_instance_id)
    )
    """,
    """
    CREATE TABLE continuation_operations (
      operation_id TEXT PRIMARY KEY,
      archive_id TEXT NOT NULL REFERENCES thread_archives(archive_id),
      assignment_id TEXT NOT NULL UNIQUE,
      new_task_id TEXT NOT NULL UNIQUE,
      new_agent_id TEXT NOT NULL UNIQUE,
      repository TEXT NOT NULL,
      issue INTEGER NOT NULL CHECK (issue > 0),
      branch TEXT NOT NULL,
      worktree TEXT NOT NULL,
      binding_digest TEXT NOT NULL CHECK (length(binding_digest)=64),
      instruction_digest TEXT NOT NULL CHECK (length(instruction_digest)=64),
      reconciliation_digest TEXT,
      concrete_benefit TEXT NOT NULL CHECK (
        concrete_benefit IN ('same_task_recovery','same_artifact_revision','unresolved_decision_chain')
      ),
      state TEXT NOT NULL CHECK (
        state IN ('prepared','reopening_issue','issue_reopened','launching','resumed','blocked')
      ),
      version INTEGER NOT NULL CHECK (version > 0),
      fence INTEGER NOT NULL CHECK (fence >= 0),
      executor_id TEXT,
      leased_until TEXT,
      issue_receipt_json TEXT,
      runtime_instance_id TEXT UNIQUE REFERENCES runtime_instances(runtime_instance_id),
      callsign TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX ux_active_continuation_archive
      ON continuation_operations(archive_id)
      WHERE state IN ('prepared','reopening_issue','issue_reopened','launching')
    """,
    "CREATE INDEX ix_thread_archives_issue ON thread_archives(repository,issue,state,archive_id)",
    "CREATE INDEX ix_thread_incarnations_lineage ON thread_incarnations(lineage_id,bound_at,runtime_instance_id)",
    "CREATE INDEX ix_continuation_state ON continuation_operations(state,updated_at,operation_id)",
)
