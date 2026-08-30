"""Guarded role-neutral rollover portion of the stable storage contract."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, Sequence

from .storage_types import FaultInjector


class RolloverStorage(Protocol):
    def prepare_rollover(
        self,
        operation_id: str,
        squad_id: str,
        predecessor_agent_id: str,
        successor_agent_id: str,
        callsign_assignment_id: str,
        expected_owner_version: int,
        expected_owner_fence: int,
        authority_kind: str,
        authority_digest: str,
        required_capabilities: Sequence[str],
        plan: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def rollover_bindings(
        self,
        operation_id: str,
        at: str,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]: ...

    def acknowledge_rollover(
        self,
        operation_id: str,
        successor_agent_id: str,
        runtime_instance_id: str,
        handoff_digest: str,
        snapshot_version: int,
        snapshot_count: int,
        snapshot_digest: str,
        pages: Sequence[Mapping[str, Any]],
        at: str,
    ) -> dict[str, Any]: ...

    def commit_rollover(
        self,
        operation_id: str,
        expected_owner_version: int,
        expected_owner_fence: int,
        owner_event_id: str,
        owner_outbox_id: str,
        at: str,
        *,
        fault: Optional[FaultInjector] = None,
    ) -> dict[str, Any]: ...

    def reconcile_rollover_descendant(
        self,
        operation_id: str,
        reconciliation_id: str,
        champion_agent_id: str,
        task_id: str,
        runtime_instance_id: str,
        snapshot_digest: str,
        snapshot_row_digest: str,
        expected_rollover_version: int,
        expected_agent_version: int,
        expected_task_version: int,
        expected_assignment_version: int,
        expected_callsign_assignment_version: int,
        runtime_receipt: Optional[Mapping[str, Any]],
        pending_outbox_ids: Sequence[str],
        at: str,
    ) -> dict[str, Any]: ...

    def rollover_descendant_target(
        self,
        operation_id: str,
        reconciliation_id: str,
        champion_agent_id: str,
        task_id: str,
        snapshot_digest: str,
        snapshot_row_digest: str,
        expected_rollover_version: int,
        expected_agent_version: int,
        expected_task_version: int,
        expected_assignment_version: int,
        expected_callsign_assignment_version: int,
    ) -> dict[str, Any]: ...

    def reconcile_rollover_intake(
        self,
        operation_id: str,
        reconciliation_id: str,
        snapshot_digest: str,
        expected_rollover_version: int,
        plan: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def rollover_intake_plan(
        self,
        operation_id: str,
        snapshot_digest: str,
        expected_rollover_version: int,
    ) -> dict[str, Any]: ...

    def abort_rollover(
        self,
        operation_id: str,
        expected_version: int,
        cleanup_receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def complete_rollover_drain(
        self,
        operation_id: str,
        expected_version: int,
        cleanup_receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def rollover_status(self, operation_id: str) -> Optional[dict[str, Any]]: ...
    def rollover_cleanup_target(self, operation_id: str) -> Optional[dict[str, Any]]: ...
