"""Runtime, cleanup, resource, and routing portion of the storage contract."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol


class RuntimeBindingStorage(Protocol):
    """Canonical binding contract consumed by runtime orchestration."""

    def register_runtime_binding(
        self,
        binding_id: str,
        task_id: str,
        harness_kind: str,
        backend_kind: str,
        session_identity: str,
        endpoint_identity: str,
        endpoint_generation: str,
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

    def claim_runtime_exit(
        self,
        binding_id: str,
        expected_version: int,
        expected_fence: int,
        executor_id: str,
        leased_until: str,
        at: str,
    ) -> dict[str, Any]: ...

    def finalize_runtime_exit(
        self,
        binding_id: str,
        expected_version: int,
        fence: int,
        at: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]: ...

class RuntimeCleanupStorage(Protocol):
    """Runtime mutation needed only by proof-gated cleanup execution."""

    def close_runtime_for_cleanup(
        self,
        runtime_instance_id: str,
        endpoint_identity: str,
        runtime_generation: str,
        at: str,
    ) -> dict[str, Any]: ...


class RuntimeLifecycleStorage(RuntimeBindingStorage, RuntimeCleanupStorage, Protocol):
    """Composite protocol exposed by the SQLite facade."""

    def record_routing_decision(self, decision: Mapping[str, Any]) -> dict[str, Any]: ...
    def routing_decision(self, decision_id: str) -> Optional[dict[str, Any]]: ...
    def record_routing_outcome(self, outcome: Mapping[str, Any]) -> dict[str, Any]: ...

    def register_task_resource(self, resource: Mapping[str, Any], at: str) -> dict[str, Any]: ...
    def task_resources(self, task_id: str) -> list[dict[str, Any]]: ...
    def plan_cleanup(self, plan: Mapping[str, Any]) -> dict[str, Any]: ...
    def cleanup_operation(self, operation_id: str) -> Optional[dict[str, Any]]: ...
    def cleanup_execution_context(self, operation_id: str) -> dict[str, Any]: ...
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
    def block_cleanup_operation(
        self,
        operation_id: str,
        fence: int,
        action_id: Optional[str],
        refusal_code: str,
        receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...
    def release_resource_lease_for_cleanup(
        self, expected: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    def resource_lease_for_cleanup(
        self, resource_id: str
    ) -> Optional[dict[str, Any]]: ...
