"""Runtime, cleanup, resource, and routing portion of the storage contract."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol


class RuntimeLifecycleStorage(Protocol):
    def register_runtime_binding(
        self,
        binding_id: str,
        task_id: str,
        harness_kind: str,
        backend_kind: str,
        session_identity: str,
        endpoint_identity: str,
        capabilities: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def runtime_binding(self, binding_id: str) -> Optional[dict[str, Any]]: ...

    def update_runtime_binding(
        self,
        binding_id: str,
        expected_version: int,
        state: str,
        at: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def record_routing_decision(self, decision: Mapping[str, Any]) -> dict[str, Any]: ...
    def routing_decision(self, decision_id: str) -> Optional[dict[str, Any]]: ...
    def record_routing_outcome(self, outcome: Mapping[str, Any]) -> dict[str, Any]: ...

    def register_task_resource(self, resource: Mapping[str, Any], at: str) -> dict[str, Any]: ...
    def task_resources(self, task_id: str) -> list[dict[str, Any]]: ...
    def plan_cleanup(self, plan: Mapping[str, Any]) -> dict[str, Any]: ...
    def cleanup_operation(self, operation_id: str) -> Optional[dict[str, Any]]: ...
    def claim_cleanup_operation(
        self, operation_id: str, expected_fence: int, executor_id: str, leased_until: str, at: str
    ) -> dict[str, Any]: ...
    def record_cleanup_action_receipt(
        self,
        action_id: str,
        operation_id: str,
        fence: int,
        outcome: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        adapter_receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...
    def finalize_cleanup(self, operation_id: str, fence: int, at: str) -> dict[str, Any]: ...
