"""Schema migration for exact autonomous authority propagation into protected gates."""

from __future__ import annotations


MIGRATION_NAME = "autonomous-protected-gate-authority-propagation"

STATEMENTS = (
    """
    CREATE TABLE protected_gate_uses (
      action_use_id TEXT PRIMARY KEY REFERENCES autonomous_action_uses(action_use_id),
      gate_name TEXT NOT NULL,
      action_kind TEXT NOT NULL,
      gate_scope_digest TEXT NOT NULL CHECK (length(gate_scope_digest)=64),
      use_receipt_digest TEXT NOT NULL UNIQUE CHECK (length(use_receipt_digest)=64),
      binding_digest TEXT NOT NULL UNIQUE CHECK (length(binding_digest)=64),
      started_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE protected_gate_settlements (
      action_use_id TEXT PRIMARY KEY REFERENCES protected_gate_uses(action_use_id),
      outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed')),
      result_receipt_digest TEXT NOT NULL CHECK (length(result_receipt_digest)=64),
      failure_class TEXT,
      settlement_digest TEXT NOT NULL UNIQUE CHECK (length(settlement_digest)=64),
      settled_at TEXT NOT NULL,
      CHECK ((outcome='failed') = (failure_class IS NOT NULL))
    )
    """,
    "CREATE INDEX ix_protected_gate_uses_name_scope ON protected_gate_uses(gate_name,gate_scope_digest,started_at)",
    """
    CREATE TRIGGER protected_gate_uses_immutable_update
    BEFORE UPDATE ON protected_gate_uses
    BEGIN SELECT RAISE(ABORT,'protected_gate_use_immutable'); END
    """,
    """
    CREATE TRIGGER protected_gate_uses_immutable_delete
    BEFORE DELETE ON protected_gate_uses
    BEGIN SELECT RAISE(ABORT,'protected_gate_use_immutable'); END
    """,
    """
    CREATE TRIGGER protected_gate_settlements_immutable_update
    BEFORE UPDATE ON protected_gate_settlements
    BEGIN SELECT RAISE(ABORT,'protected_gate_settlement_immutable'); END
    """,
    """
    CREATE TRIGGER protected_gate_settlements_immutable_delete
    BEFORE DELETE ON protected_gate_settlements
    BEGIN SELECT RAISE(ABORT,'protected_gate_settlement_immutable'); END
    """,
)
