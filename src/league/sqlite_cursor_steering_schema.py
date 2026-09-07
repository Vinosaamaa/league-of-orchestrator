"""Durable Cursor delivery-effect fencing."""

MIGRATION_NAME = "cursor-steering-intent-receipt"

STATEMENTS = (
    """
    CREATE TABLE cursor_steering_effects (
      outbox_id TEXT PRIMARY KEY REFERENCES delivery_outbox(outbox_id),
      event_id TEXT NOT NULL REFERENCES events(event_id),
      recipient_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      runtime_instance_id TEXT NOT NULL REFERENCES runtime_instances(runtime_instance_id),
      runtime_generation TEXT NOT NULL,
      pane_id TEXT NOT NULL,
      session_ref TEXT NOT NULL,
      action TEXT NOT NULL CHECK (action IN ('idle_submit','working_steer')),
      observed_status TEXT NOT NULL CHECK (observed_status IN ('idle','done','working')),
      prompt_sha256 TEXT NOT NULL,
      prompt_bytes INTEGER NOT NULL CHECK (prompt_bytes > 0),
      intent_digest TEXT NOT NULL,
      effect_state TEXT NOT NULL CHECK (
        effect_state IN ('intent_recorded','text_sent','effect_applied','refused','acknowledged')
      ),
      effect_id TEXT,
      intent_json TEXT NOT NULL,
      receipt_json TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE (event_id,recipient_agent_id)
    )
    """,
    "CREATE INDEX ix_cursor_steering_state ON cursor_steering_effects(effect_state,updated_at,outbox_id)",
)
