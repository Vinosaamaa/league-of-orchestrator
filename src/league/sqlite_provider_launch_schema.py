"""Durable Pi/provider launch descriptors and restart effects."""

MIGRATION_NAME = "pi-provider-launch-descriptor"

STATEMENTS = (
    """
    CREATE TABLE provider_launch_descriptors (
      descriptor_id TEXT PRIMARY KEY,
      assignment_id TEXT,
      runtime_kind TEXT NOT NULL CHECK (runtime_kind='pi'),
      provider_kind TEXT NOT NULL CHECK (provider_kind IN ('cursor','codex')),
      role TEXT NOT NULL CHECK (role IN ('shotcaller','champion')),
      placement TEXT NOT NULL CHECK (placement IN ('sibling_pane','new_tab')),
      launch_mode TEXT NOT NULL CHECK (launch_mode IN ('create','fork','resume')),
      cwd TEXT NOT NULL,
      parent_session_id TEXT,
      parent_session_path TEXT,
      session_id TEXT,
      session_path TEXT,
      workspace_id TEXT,
      tab_id TEXT,
      pane_id TEXT,
      terminal_id TEXT,
      state TEXT NOT NULL CHECK (state IN ('prepared','active','blocked')),
      descriptor_json TEXT NOT NULL,
      descriptor_digest TEXT NOT NULL,
      launch_receipt_json TEXT,
      version INTEGER NOT NULL CHECK (version > 0),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE (runtime_kind,session_id),
      UNIQUE (runtime_kind,session_path)
    )
    """,
    """
    CREATE UNIQUE INDEX ux_provider_launch_one_project_fork
      ON provider_launch_descriptors(runtime_kind,parent_session_path,cwd)
     WHERE launch_mode='fork'
    """,
    """
    CREATE TABLE provider_restart_effects (
      descriptor_id TEXT NOT NULL REFERENCES provider_launch_descriptors(descriptor_id),
      restart_id TEXT NOT NULL,
      pane_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      session_path TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('intent_recorded','effect_applied','refused')),
      intent_digest TEXT NOT NULL,
      effect_digest TEXT,
      receipt_json TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (descriptor_id,restart_id)
    )
    """,
    "CREATE INDEX ix_provider_restart_state ON provider_restart_effects(state,updated_at,descriptor_id)",
    """
    CREATE TABLE pi_session_migrations (
      migration_id TEXT PRIMARY KEY,
      descriptor_id TEXT NOT NULL UNIQUE REFERENCES provider_launch_descriptors(descriptor_id),
      session_id TEXT NOT NULL UNIQUE,
      source_session_path TEXT NOT NULL,
      destination_session_path TEXT NOT NULL UNIQUE,
      session_sha256 TEXT NOT NULL,
      parent_session_id TEXT,
      parent_session_path TEXT,
      cwd TEXT NOT NULL,
      pane_id TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('intent_recorded','copied','bound')),
      intent_json TEXT NOT NULL,
      intent_digest TEXT NOT NULL,
      receipt_json TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_pi_session_migration_state ON pi_session_migrations(state,updated_at,migration_id)",
)
