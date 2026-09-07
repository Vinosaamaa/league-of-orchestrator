"""Schema support for exact imported descendant reconciliation after rollover."""

from __future__ import annotations


MIGRATION_NAME = "nullable-request-rollover-descendant-assignments"

STATEMENTS = (
    "PRAGMA defer_foreign_keys=ON",
    "PRAGMA legacy_alter_table=ON",
    "ALTER TABLE task_assignments RENAME TO task_assignments_v12",
    """
    CREATE TABLE task_assignments (
      task_assignment_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
      request_id TEXT REFERENCES requests(request_id),
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
       runtime_instance_id,callsign,assignment_role,dispatch_id,bounded_subtask,model,
       effort,routing_reason_code,time_budget_minutes,scope_budget_actions,state,
       acceptance_receipt_json,failure_class,cleanup_required,result_summary,
       cleanup_receipt,unpublished_state_receipt,promoted_from_assignment_id,
       promoted_to_assignment_id,terminal_event_id,version,created_at,updated_at)
    SELECT task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
           runtime_instance_id,callsign,assignment_role,dispatch_id,bounded_subtask,model,
           effort,routing_reason_code,time_budget_minutes,scope_budget_actions,state,
           acceptance_receipt_json,failure_class,cleanup_required,result_summary,
           cleanup_receipt,unpublished_state_receipt,promoted_from_assignment_id,
           promoted_to_assignment_id,terminal_event_id,version,created_at,updated_at
      FROM task_assignments_v12
    """,
    "DROP TABLE task_assignments_v12",
    "CREATE INDEX ix_assignments_state ON task_assignments(coordinator_agent_id,state,updated_at)",
    "CREATE INDEX ix_report_assignments_created ON task_assignments(created_at,task_assignment_id)",
    "CREATE INDEX ix_report_assignments_updated ON task_assignments(updated_at,task_assignment_id)",
    "CREATE UNIQUE INDEX ux_hidden_dispatch_assignment ON task_assignments(dispatch_id) WHERE assignment_role='hidden-worker'",
    "PRAGMA legacy_alter_table=OFF",
)
