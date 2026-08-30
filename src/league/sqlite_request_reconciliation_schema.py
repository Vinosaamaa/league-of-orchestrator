"""Reviewed v18 schema for agent-authored duplicate-request reconciliation."""

MIGRATION_NAME = "agent-authored-request-reconciliation"

STATEMENTS = (
    """
    CREATE TABLE request_reconciliations (
      duplicate_request_id TEXT PRIMARY KEY REFERENCES requests(request_id),
      canonical_request_id TEXT NOT NULL REFERENCES requests(request_id),
      actor_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      duplicate_version_before INTEGER NOT NULL CHECK (duplicate_version_before > 0),
      canonical_version_at_link INTEGER NOT NULL CHECK (canonical_version_at_link > 0),
      event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
      reconciled_at TEXT NOT NULL,
      CHECK (duplicate_request_id <> canonical_request_id)
    )
    """,
    "CREATE INDEX ix_request_reconciliations_canonical ON request_reconciliations(canonical_request_id,duplicate_request_id)",
)
