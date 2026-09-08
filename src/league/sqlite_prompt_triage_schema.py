"""Prompt-only triage policy; never changes Champion lifecycle policy."""

MIGRATION_NAME = "dedicated-prompt-triage-policy"

STATEMENTS = (
    """
    CREATE TABLE prompt_triage_settings (
      owner_agent_id TEXT PRIMARY KEY REFERENCES agent_instances(agent_id),
      mode TEXT NOT NULL CHECK(mode IN ('background','off')),
      version INTEGER NOT NULL CHECK(version > 0),
      updated_at TEXT NOT NULL
    )
    """,
    "ALTER TABLE prompts ADD COLUMN triage_mode TEXT NOT NULL DEFAULT 'inline' CHECK(triage_mode IN ('inline','background','off'))",
    "CREATE INDEX ix_prompts_triage_mode ON prompts(triage_mode,triage_state,created_at,prompt_id)",
)
