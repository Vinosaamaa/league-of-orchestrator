"""Schema migration for scoped autonomous delivery and issue-first assignment."""

from __future__ import annotations


MIGRATION_NAME = "scoped-autonomous-delivery-and-issue-first-assignment"

STATEMENTS = (
    """
    CREATE TABLE authorization_grants (
      grant_id TEXT PRIMARY KEY,
      goal_id TEXT NOT NULL,
      revision INTEGER NOT NULL CHECK (revision > 0),
      issuer_kind TEXT NOT NULL CHECK (issuer_kind='summoner'),
      issuer_id TEXT NOT NULL,
      shotcaller_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      exact_goal TEXT NOT NULL,
      scope_json TEXT NOT NULL,
      allowed_actions_json TEXT NOT NULL,
      exclusions_json TEXT NOT NULL,
      sensitive_inclusions_json TEXT NOT NULL,
      resource_boundary_json TEXT NOT NULL,
      starts_at TEXT NOT NULL,
      expires_at TEXT,
      limits_json TEXT NOT NULL,
      canonical_digest TEXT NOT NULL UNIQUE CHECK (length(canonical_digest)=64),
      version INTEGER NOT NULL CHECK (version=1),
      created_at TEXT NOT NULL,
      UNIQUE (goal_id,revision)
    )
    """,
    """
    CREATE TABLE delivery_goals (
      goal_id TEXT PRIMARY KEY,
      active_grant_id TEXT NOT NULL UNIQUE REFERENCES authorization_grants(grant_id),
      state TEXT NOT NULL CHECK (state IN (
        'awaiting_authority','implementing','ready_to_land','landing','deploying',
        'verifying','repair_pending','delivered','cleanup_pending','cleaned'
      )),
      next_irreversible_action TEXT NOT NULL,
      version INTEGER NOT NULL CHECK (version > 0),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE authorization_revocations (
      grant_id TEXT PRIMARY KEY REFERENCES authorization_grants(grant_id),
      revoked_by TEXT NOT NULL,
      reason TEXT NOT NULL,
      revoked_at TEXT NOT NULL,
      receipt_digest TEXT NOT NULL UNIQUE CHECK (length(receipt_digest)=64)
    )
    """,
    """
    CREATE TABLE autonomous_action_uses (
      action_use_id TEXT PRIMARY KEY,
      idempotency_key TEXT NOT NULL UNIQUE,
      goal_id TEXT NOT NULL REFERENCES delivery_goals(goal_id),
      grant_id TEXT NOT NULL REFERENCES authorization_grants(grant_id),
      grant_revision INTEGER NOT NULL CHECK (grant_revision > 0),
      external_owner_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      action_kind TEXT NOT NULL,
      action_scope_json TEXT NOT NULL,
      risk_categories_json TEXT NOT NULL,
      sensitive_categories_json TEXT NOT NULL,
      resource_use_json TEXT NOT NULL,
      attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
      cost_microunits INTEGER NOT NULL CHECK (cost_microunits >= 0),
      changed_files INTEGER NOT NULL CHECK (changed_files >= 0),
      duration_seconds INTEGER NOT NULL CHECK (duration_seconds >= 0),
      state TEXT NOT NULL CHECK (state IN ('in_progress','succeeded','failed')),
      use_receipt_digest TEXT NOT NULL UNIQUE CHECK (length(use_receipt_digest)=64),
      result_receipt_digest TEXT CHECK (result_receipt_digest IS NULL OR length(result_receipt_digest)=64),
      failure_class TEXT,
      started_at TEXT NOT NULL,
      settled_at TEXT,
      CHECK ((state='in_progress') = (settled_at IS NULL)),
      CHECK ((state='failed') = (failure_class IS NOT NULL))
    )
    """,
    """
    CREATE TABLE autonomous_repair_obligations (
      repair_id TEXT PRIMARY KEY,
      goal_id TEXT NOT NULL REFERENCES delivery_goals(goal_id),
      failed_action_use_id TEXT NOT NULL UNIQUE REFERENCES autonomous_action_uses(action_use_id),
      state TEXT NOT NULL CHECK (state IN ('pending','in_progress','completed','blocked')),
      attempts_used INTEGER NOT NULL CHECK (attempts_used >= 0),
      max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
      failure_class TEXT NOT NULL,
      version INTEGER NOT NULL CHECK (version > 0),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE repository_issue_bindings (
      task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
      assignment_id TEXT NOT NULL UNIQUE REFERENCES task_assignments(task_assignment_id),
      request_id TEXT NOT NULL REFERENCES requests(request_id),
      repository TEXT NOT NULL,
      issue INTEGER NOT NULL CHECK (issue > 0),
      issue_url TEXT NOT NULL,
      issue_state TEXT NOT NULL CHECK (issue_state IN ('open','closed')),
      issue_title TEXT NOT NULL,
      issue_body_digest TEXT NOT NULL CHECK (length(issue_body_digest)=64),
      task_scope_digest TEXT NOT NULL CHECK (length(task_scope_digest)=64),
      reopen_action_receipt_digest TEXT,
      verifier_kind TEXT NOT NULL CHECK (verifier_kind='github-api'),
      verified_at TEXT NOT NULL,
      receipt_digest TEXT NOT NULL UNIQUE CHECK (length(receipt_digest)=64),
      CHECK (reopen_action_receipt_digest IS NULL OR length(reopen_action_receipt_digest)=64)
    )
    """,
    "CREATE INDEX ix_grants_goal_revision ON authorization_grants(goal_id,revision)",
    "CREATE INDEX ix_mode_actions_goal_state ON autonomous_action_uses(goal_id,state,started_at)",
    "CREATE INDEX ix_mode_repairs_goal_state ON autonomous_repair_obligations(goal_id,state,updated_at)",
    "CREATE INDEX ix_issue_bindings_repository_issue ON repository_issue_bindings(repository,issue)",
    """
    CREATE TRIGGER authorization_grants_immutable_update
    BEFORE UPDATE ON authorization_grants
    BEGIN SELECT RAISE(ABORT,'authorization_grant_immutable'); END
    """,
    """
    CREATE TRIGGER authorization_grants_immutable_delete
    BEFORE DELETE ON authorization_grants
    BEGIN SELECT RAISE(ABORT,'authorization_grant_immutable'); END
    """,
    """
    CREATE TRIGGER repository_issue_bindings_immutable_update
    BEFORE UPDATE ON repository_issue_bindings
    BEGIN SELECT RAISE(ABORT,'repository_issue_binding_immutable'); END
    """,
    """
    CREATE TRIGGER repository_issue_bindings_immutable_delete
    BEFORE DELETE ON repository_issue_bindings
    BEGIN SELECT RAISE(ABORT,'repository_issue_binding_immutable'); END
    """,
)
