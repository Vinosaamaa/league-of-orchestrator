"""Durable receipts for exact already-stopped agent retirement."""

MIGRATION_NAME = "stopped-agent-total-retirement"

STATEMENTS = (
    """
    CREATE TABLE stopped_agent_retirements (
      operation_id TEXT PRIMARY KEY,
      agent_id TEXT NOT NULL UNIQUE REFERENCES agent_instances(agent_id),
      runtime_instance_id TEXT NOT NULL UNIQUE REFERENCES runtime_instances(runtime_instance_id),
      callsign_assignment_id TEXT NOT NULL UNIQUE
        REFERENCES callsign_assignments(callsign_assignment_id),
      adapter_kind TEXT NOT NULL,
      provider_kind TEXT NOT NULL,
      multiplexer_kind TEXT NOT NULL,
      session_ref TEXT NOT NULL,
      endpoint TEXT NOT NULL,
      runtime_generation TEXT NOT NULL,
      terminal_status TEXT NOT NULL
        CHECK (terminal_status IN ('completed','cancelled','failed')),
      request_digest TEXT NOT NULL,
      proof_digest TEXT NOT NULL,
      proof_json TEXT NOT NULL,
      receipt_json TEXT NOT NULL,
      completed_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_stopped_agent_retirements_completed ON stopped_agent_retirements(completed_at,operation_id)",
    "CREATE INDEX ix_callsign_assignments_agent_state ON callsign_assignments(agent_id,state,callsign_assignment_id)",
    "CREATE INDEX ix_task_assignments_champion_state ON task_assignments(champion_agent_id,state,task_assignment_id)",
)
