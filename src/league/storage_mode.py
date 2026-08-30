"""Stable storage boundary for scoped autonomous delivery mode."""

from __future__ import annotations

from typing import Any, Protocol


class ModeStorage(Protocol):
    def authorize_mode(
        self, grant: dict[str, Any], expected_goal_version: int, at: str
    ) -> dict[str, Any]: ...

    def mode_status(self, goal_id: str, at: str) -> dict[str, Any]: ...

    def use_mode_action(
        self, action: dict[str, Any], expected_goal_version: int, at: str
    ) -> dict[str, Any]: ...

    def settle_mode_action(
        self,
        action_use_id: str,
        goal_id: str,
        expected_goal_version: int,
        use_receipt_digest: str,
        outcome: str,
        result_receipt_digest: str,
        failure_class: str | None,
        at: str,
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
