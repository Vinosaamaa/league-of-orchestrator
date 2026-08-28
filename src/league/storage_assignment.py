"""Champion assignment portion of the stable storage contract."""

from __future__ import annotations

from typing import Any, Optional, Protocol


class AssignmentStorage(Protocol):
    def prepare_assignment(
        self,
        assignment_id: str,
        request_id: str,
        claim_token: str,
        task_id: str,
        task_summary: str,
        coordinator_agent_id: str,
        champion_agent_id: str,
        callsign: str,
        repository: str,
        issue: int,
        branch: str,
        worktree: str,
        at: str,
    ) -> dict[str, Any]: ...
    def mark_assignment_launching(
        self, assignment_id: str, expected_version: int, at: str
    ) -> dict[str, Any]: ...

    def activate_assignment(
        self,
        assignment_id: str,
        expected_version: int,
        receipt: dict[str, Any],
        event_id: str,
        outbox_id: str,
        at: str,
    ) -> dict[str, Any]: ...

    def block_assignment(
        self,
        assignment_id: str,
        expected_version: int,
        failure_class: str,
        cleanup_required: bool,
        cleanup_proven: bool,
        at: str,
    ) -> dict[str, Any]: ...

    def transition_task(
        self,
        task_id: str,
        runtime_instance_id: str,
        expected_version: int,
        state: str,
        update: str,
        next_action: str,
        blocker: Optional[str],
        transition_id: str,
        transition_key: str,
        event_id: str,
        outbox_id: str,
        recipient_agent_id: str,
        at: str,
    ) -> dict[str, Any]: ...
