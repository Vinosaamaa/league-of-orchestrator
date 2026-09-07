"""Preserve prompt capture provenance while allowing explicit current-owner transfer."""

from __future__ import annotations


MIGRATION_NAME = "immutable-prompt-provenance-current-owner"

STATEMENTS = (
    "ALTER TABLE prompts ADD COLUMN current_owner_agent_id TEXT REFERENCES agent_instances(agent_id)",
    "ALTER TABLE prompts ADD COLUMN current_owner_runtime_instance_id TEXT REFERENCES runtime_instances(runtime_instance_id)",
    "UPDATE prompts SET current_owner_agent_id=intake_actor_id WHERE current_owner_agent_id IS NULL",
    "UPDATE prompts SET current_owner_runtime_instance_id=runtime_instance_id WHERE current_owner_runtime_instance_id IS NULL",
    "CREATE INDEX ix_prompts_current_owner_untriaged ON prompts(current_owner_agent_id,triage_state,created_at,prompt_id)",
    """
    CREATE TRIGGER prompts_current_owner_required_insert
    BEFORE INSERT ON prompts WHEN NEW.current_owner_agent_id IS NULL OR NEW.current_owner_runtime_instance_id IS NULL
    BEGIN SELECT RAISE(ABORT,'prompt_current_owner_required'); END
    """,
    """
    CREATE TRIGGER prompts_current_owner_required_update
    BEFORE UPDATE ON prompts WHEN NEW.current_owner_agent_id IS NULL OR NEW.current_owner_runtime_instance_id IS NULL
    BEGIN SELECT RAISE(ABORT,'prompt_current_owner_required'); END
    """,
    """
    CREATE TRIGGER prompts_capture_provenance_immutable
    BEFORE UPDATE ON prompts WHEN
      NEW.intake_actor_id<>OLD.intake_actor_id OR
      NEW.runtime_instance_id<>OLD.runtime_instance_id OR
      NEW.adapter_kind<>OLD.adapter_kind OR
      NEW.session_ref<>OLD.session_ref OR
      NEW.source_event_key<>OLD.source_event_key OR
      NEW.created_at<>OLD.created_at
    BEGIN SELECT RAISE(ABORT,'prompt_capture_provenance_immutable'); END
    """,
)
