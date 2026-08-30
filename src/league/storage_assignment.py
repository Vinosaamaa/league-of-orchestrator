"""Champion assignment portion of the stable storage contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class PrepareAssignmentCommand:
    assignment_id: str
    request_id: str
    claim_token: str
    task_id: str
    task_summary: str
    coordinator_agent_id: str
    champion_agent_id: str
    repository: str
    issue: int
    branch: str
    worktree: str
    at: str
    required_capabilities: tuple[str, ...] = ()
    assignment_role: str = "champion"
    dispatch_id: Optional[str] = None
    promoted_from_assignment_id: Optional[str] = None


@dataclass(frozen=True)
class FinishHiddenAssignmentCommand:
    assignment_id: str
    runtime_instance_id: str
    expected_version: int
    status: str
    result_summary: str
    cleanup_receipt: str
    unpublished_state_receipt: str
    transition_id: str
    transition_key: str
    event_id: str
    outbox_id: str
    at: str


class AssignmentStorage(Protocol):
    def prepare_assignment(self, command: PrepareAssignmentCommand) -> dict[str, Any]: ...
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

    def assignment_launch_context(self, assignment_id: str) -> dict[str, Any]: ...

    def record_assignment_context_delivery(
        self,
        assignment_id: str,
        expected_version: int,
        context_sha256: str,
        byte_count: int,
        effect_sha256: str,
        display_receipt: dict[str, Any],
        event_id: str,
        at: str,
    ) -> dict[str, Any]: ...

    def fail_assignment_context_delivery(
        self,
        assignment_id: str,
        expected_version: int,
        failure_class: str,
        event_id: str,
        outbox_id: str,
        at: str,
    ) -> dict[str, Any]: ...

    def fail_assignment_title_validation(
        self,
        assignment_id: str,
        expected_version: int,
        failure_class: str,
        event_id: str,
        outbox_id: str,
        at: str,
    ) -> dict[str, Any]: ...

    def settle_assignment_launch_cleanup(
        self,
        assignment_id: str,
        expected_version: int,
        cleanup_receipt_digest: str,
        at: str,
    ) -> dict[str, Any]: ...

    def finish_hidden_assignment(
        self, command: FinishHiddenAssignmentCommand
    ) -> dict[str, Any]: ...

    def reconcile_assignment_runtime(
        self, assignment_id: str, at: str
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
