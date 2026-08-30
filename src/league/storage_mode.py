"""Stable storage boundary for scoped autonomous delivery mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


PROTECTED_GATE_ACTIONS = {
    "assign.reconcile-legacy-display": "live_reconcile",
    "assign.reconcile-runtime": "live_reconcile",
    "callsign.release": "retire",
    "cleanup.execute": "teardown",
    "cleanup.reconcile": "teardown",
    "rollover.commit": "live_reconcile",
    "rollover.drain": "retire",
    "rollover.prepare": "live_reconcile",
    "rollover.reconcile-descendant": "live_reconcile",
    "rollover.reconcile-intake": "live_reconcile",
    "shotcaller.create": "shotcaller_create",
    "squad.accept": "squad_register",
    "squad.register": "squad_register",
}


@dataclass(frozen=True)
class BeginProtectedGateCommand:
    gate_name: str
    gate_scope: dict[str, Any]
    action: dict[str, Any]
    expected_goal_version: int
    at: str


@dataclass(frozen=True)
class SettleProtectedGateCommand:
    action_use_id: str
    gate_name: str
    gate_scope_digest: str
    expected_goal_version: int
    use_receipt_digest: str
    outcome: str
    result_receipt_digest: str
    failure_class: str | None
    at: str


@dataclass(frozen=True)
class SettleModeActionCommand:
    action_use_id: str
    goal_id: str
    expected_goal_version: int
    use_receipt_digest: str
    outcome: str
    result_receipt_digest: str
    failure_class: str | None
    at: str


class ModeStorage(Protocol):
    def authorize_mode(
        self, grant: dict[str, Any], expected_goal_version: int, at: str
    ) -> dict[str, Any]: ...

    def mode_status(self, goal_id: str, at: str) -> dict[str, Any]: ...

    def use_mode_action(
        self, action: dict[str, Any], expected_goal_version: int, at: str
    ) -> dict[str, Any]: ...

    def settle_mode_action(self, command: SettleModeActionCommand) -> dict[str, Any]: ...

    def begin_protected_gate(
        self, command: BeginProtectedGateCommand
    ) -> dict[str, Any]: ...

    def settle_protected_gate(
        self, command: SettleProtectedGateCommand
    ) -> dict[str, Any]: ...

    def transition_mode_goal(
        self, goal_id: str, expected_goal_version: int, state: str, at: str
    ) -> dict[str, Any]: ...

    def revoke_mode_grant(
        self,
        grant_id: str,
        revoked_by: str,
        reason: str,
        expected_goal_version: int,
        at: str,
    ) -> dict[str, Any]: ...
