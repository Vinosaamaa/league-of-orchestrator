"""Reviewed v8 schema statements for the issue-36 routing-policy slice."""

from __future__ import annotations


MIGRATION_NAME = "bounded-routing-policy-and-request-progress"


STATEMENTS = (
    "ALTER TABLE requests ADD COLUMN owner_squad_id TEXT REFERENCES squads(squad_id)",
    "ALTER TABLE requests ADD COLUMN pending_owner_agent_id TEXT REFERENCES agent_instances(agent_id)",
    "ALTER TABLE requests ADD COLUMN pending_owner_squad_id TEXT REFERENCES squads(squad_id)",
    "ALTER TABLE requests ADD COLUMN route_reason_code TEXT",
    "ALTER TABLE requests ADD COLUMN route_policy_version TEXT",
    "ALTER TABLE requests ADD COLUMN route_confidence TEXT CHECK (route_confidence IS NULL OR route_confidence IN ('explicit','continuation','strong'))",
    """
    CREATE TABLE squad_capabilities (
      squad_id TEXT NOT NULL REFERENCES squads(squad_id),
      capability TEXT NOT NULL,
      PRIMARY KEY (squad_id,capability)
    )
    """,
    """
    CREATE TABLE squad_registration_offers (
      registration_id TEXT PRIMARY KEY,
      squad_id TEXT NOT NULL,
      requester_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      shotcaller_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      runtime_instance_id TEXT NOT NULL REFERENCES runtime_instances(runtime_instance_id),
      project_ids_json TEXT NOT NULL DEFAULT '[]',
      capabilities_json TEXT NOT NULL DEFAULT '[]',
      state TEXT NOT NULL CHECK (state IN ('pending','accepted','rejected','expired')),
      expires_at TEXT NOT NULL,
      offer_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
      offer_outbox_id TEXT NOT NULL UNIQUE REFERENCES delivery_outbox(outbox_id),
      response_event_id TEXT UNIQUE REFERENCES events(event_id),
      response_outbox_id TEXT UNIQUE REFERENCES delivery_outbox(outbox_id),
      registered_at TEXT NOT NULL,
      responded_at TEXT
    )
    """,
    "CREATE UNIQUE INDEX ux_pending_squad_registration ON squad_registration_offers(squad_id) WHERE state='pending'",
    "CREATE UNIQUE INDEX ux_pending_shotcaller_registration ON squad_registration_offers(shotcaller_agent_id) WHERE state='pending'",
    "ALTER TABLE request_dispatches ADD COLUMN reason_code TEXT NOT NULL DEFAULT 'legacy_dispatch'",
    "ALTER TABLE task_assignments RENAME TO task_assignments_v7",
    """
    CREATE TABLE task_assignments (
      task_assignment_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
      request_id TEXT NOT NULL REFERENCES requests(request_id),
      coordinator_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      champion_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      runtime_instance_id TEXT REFERENCES runtime_instances(runtime_instance_id),
      callsign TEXT NOT NULL REFERENCES callsigns(callsign),
      assignment_role TEXT NOT NULL CHECK (assignment_role IN ('champion','hidden-worker')),
      dispatch_id TEXT REFERENCES request_dispatches(dispatch_id),
      bounded_subtask TEXT,
      model TEXT,
      effort TEXT,
      routing_reason_code TEXT,
      time_budget_minutes INTEGER CHECK (time_budget_minutes IS NULL OR time_budget_minutes BETWEEN 1 AND 5),
      scope_budget_actions INTEGER CHECK (scope_budget_actions IS NULL OR scope_budget_actions BETWEEN 1 AND 2),
      state TEXT NOT NULL CHECK (
        state IN ('pending','launching','active','blocked','cleanup_pending','completed','failed','promotion_required')
      ),
      acceptance_receipt_json TEXT,
      failure_class TEXT,
      cleanup_required INTEGER NOT NULL DEFAULT 0 CHECK (cleanup_required IN (0,1)),
      result_summary TEXT,
      cleanup_receipt TEXT,
      unpublished_state_receipt TEXT,
      promoted_from_assignment_id TEXT REFERENCES task_assignments(task_assignment_id),
      promoted_to_assignment_id TEXT REFERENCES task_assignments(task_assignment_id),
      terminal_event_id TEXT UNIQUE REFERENCES events(event_id),
      version INTEGER NOT NULL CHECK (version > 0),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO task_assignments
      (task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
       runtime_instance_id,callsign,assignment_role,state,acceptance_receipt_json,
       failure_class,cleanup_required,version,created_at,updated_at)
    SELECT task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
           runtime_instance_id,callsign,'champion',state,acceptance_receipt_json,
           failure_class,cleanup_required,version,created_at,updated_at
      FROM task_assignments_v7
    """,
    "DROP TABLE task_assignments_v7",
    "CREATE INDEX ix_assignments_state ON task_assignments(coordinator_agent_id,state,updated_at)",
    "CREATE INDEX ix_report_assignments_created ON task_assignments(created_at,task_assignment_id)",
    "CREATE INDEX ix_report_assignments_updated ON task_assignments(updated_at,task_assignment_id)",
    "CREATE UNIQUE INDEX ux_hidden_dispatch_assignment ON task_assignments(dispatch_id) WHERE assignment_role='hidden-worker'",
    "ALTER TABLE model_routing_decisions ADD COLUMN provider TEXT NOT NULL DEFAULT 'legacy'",
    "ALTER TABLE model_routing_decisions ADD COLUMN provider_config_version TEXT NOT NULL DEFAULT 'legacy'",
    "ALTER TABLE model_routing_decisions ADD COLUMN policy_version TEXT NOT NULL DEFAULT 'legacy'",
    "ALTER TABLE model_routing_decisions ADD COLUMN reason_code TEXT NOT NULL DEFAULT 'legacy_route'",
    "ALTER TABLE model_routing_decisions ADD COLUMN explicit_provider INTEGER NOT NULL DEFAULT 0 CHECK (explicit_provider IN (0,1))",
    "ALTER TABLE model_routing_decisions ADD COLUMN operator_override_id TEXT",
    "ALTER TABLE model_routing_decisions ADD COLUMN fallback_from_provider TEXT",
    "ALTER TABLE model_routing_decisions ADD COLUMN required_capabilities_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE model_routing_decisions ADD COLUMN signals_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE model_routing_outcomes RENAME TO model_routing_outcomes_v7",
    """
    CREATE TABLE model_routing_outcomes (
      outcome_id TEXT PRIMARY KEY,
      decision_id TEXT NOT NULL REFERENCES model_routing_decisions(decision_id),
      success INTEGER NOT NULL CHECK (success IN (0,1)),
      corrections INTEGER NOT NULL CHECK (corrections >= 0),
      latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
      cost_microunits INTEGER CHECK (cost_microunits IS NULL OR cost_microunits >= 0),
      recorded_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO model_routing_outcomes
      (outcome_id,decision_id,success,corrections,latency_ms,cost_microunits,recorded_at)
    SELECT outcome_id,decision_id,success,corrections,latency_ms,cost_microunits,recorded_at
      FROM model_routing_outcomes_v7
    """,
    "DROP TABLE model_routing_outcomes_v7",
    "CREATE INDEX ix_routing_outcomes ON model_routing_outcomes(decision_id,recorded_at)",
    """
    CREATE TABLE request_progress_events (
      progress_id TEXT PRIMARY KEY,
      request_id TEXT NOT NULL REFERENCES requests(request_id),
      request_generation INTEGER NOT NULL CHECK (request_generation > 0),
      progress_generation INTEGER NOT NULL CHECK (progress_generation > 0),
      owner_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      recipient_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      urgency TEXT NOT NULL CHECK (urgency IN ('routine','immediate','overdue')),
      reason_code TEXT NOT NULL,
      content_digest TEXT NOT NULL,
      settled_count INTEGER NOT NULL CHECK (settled_count >= 0),
      total_count INTEGER NOT NULL CHECK (total_count >= 0),
      current_phase TEXT NOT NULL,
      blocker_count INTEGER NOT NULL CHECK (blocker_count >= 0),
      blocker_severity TEXT NOT NULL CHECK (blocker_severity IN ('none','low','medium','high','critical')),
      user_action_required INTEGER NOT NULL CHECK (user_action_required IN (0,1)),
      deadline_change TEXT,
      next_action TEXT NOT NULL,
      event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
      outbox_id TEXT NOT NULL UNIQUE REFERENCES delivery_outbox(outbox_id),
      emitted_at TEXT NOT NULL,
      UNIQUE (request_id,progress_generation,recipient_agent_id),
      CHECK (settled_count <= total_count),
      CHECK ((blocker_count=0 AND blocker_severity='none') OR blocker_count>0)
    )
    """,
    "CREATE INDEX ix_request_progress_latest ON request_progress_events(request_id,recipient_agent_id,emitted_at,progress_generation)",
    """
    CREATE TABLE request_progress_buffers (
      request_id TEXT NOT NULL REFERENCES requests(request_id),
      recipient_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      request_generation INTEGER NOT NULL CHECK (request_generation > 0),
      progress_generation INTEGER NOT NULL CHECK (progress_generation > 0),
      owner_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      progress_id TEXT NOT NULL,
      event_id TEXT NOT NULL,
      outbox_id TEXT NOT NULL,
      content_digest TEXT NOT NULL,
      settled_count INTEGER NOT NULL CHECK (settled_count >= 0),
      total_count INTEGER NOT NULL CHECK (total_count >= 0),
      current_phase TEXT NOT NULL,
      blocker_count INTEGER NOT NULL CHECK (blocker_count >= 0),
      blocker_severity TEXT NOT NULL CHECK (blocker_severity IN ('none','low','medium','high','critical')),
      user_action_required INTEGER NOT NULL CHECK (user_action_required IN (0,1)),
      deadline_change TEXT,
      next_action TEXT NOT NULL,
      due_at TEXT NOT NULL,
      grace_expires_at TEXT NOT NULL,
      promised_checkpoint_at TEXT,
      state TEXT NOT NULL CHECK (state IN ('pending','due','emitted','superseded','escalated')),
      buffered_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (request_id,recipient_agent_id)
    )
    """,
    "CREATE INDEX ix_request_progress_due ON request_progress_buffers(owner_agent_id,state,due_at,grace_expires_at)",
)
