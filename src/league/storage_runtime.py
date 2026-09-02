"""Runtime, cleanup, resource, and routing portion of the storage contract."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Protocol

from .storage_types import FaultInjector


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

    def stopped_agent_retirement_adapter_identity(
        self, request: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def stopped_agent_retirement(
        self, operation_id: str
    ) -> Optional[dict[str, Any]]: ...

    def complete_stopped_agent_retirement(
        self,
        request: Mapping[str, Any],
        *,
        adapter_kind: str,
        verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        request_digest: str,
        at: str,
        fault: Optional[FaultInjector] = None,
    ) -> dict[str, Any]: ...

    def prepare_provider_launch(
        self, descriptor: Mapping[str, Any], at: str
    ) -> dict[str, Any]: ...

    def bind_provider_launch(
        self,
        descriptor_id: str,
        expected_version: int,
        observation: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def provider_launch_descriptor(
        self, descriptor_id: str
    ) -> Optional[dict[str, Any]]: ...

    def claim_provider_restart(
        self,
        descriptor_id: str,
        restart_id: str,
        pane_id: str,
        at: str,
    ) -> dict[str, Any]: ...

    def complete_provider_restart(
        self,
        descriptor_id: str,
        restart_id: str,
        intent_digest: str,
        receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def prepare_runtime_replacement(
        self, request: Mapping[str, Any], at: str
    ) -> dict[str, Any]: ...

    def begin_runtime_replacement_effect(
        self,
        operation_id: str,
        expected_version: int,
        intent_digest: str,
        effect: str,
        at: str,
    ) -> dict[str, Any]: ...

    def record_replacement_successor_verified(
        self,
        operation_id: str,
        expected_version: int,
        intent_digest: str,
        receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def activate_runtime_replacement(
        self,
        operation_id: str,
        expected_version: int,
        intent_digest: str,
        route_receipt: Mapping[str, Any],
        descriptor_transactions: tuple[Any, ...],
        at: str,
    ) -> dict[str, Any]: ...

    def complete_runtime_replacement(
        self,
        operation_id: str,
        expected_version: int,
        intent_digest: str,
        retirement_receipt: Mapping[str, Any],
        event_id: str,
        outbox_id: str,
        at: str,
    ) -> dict[str, Any]: ...

    def record_replacement_predecessor_retired(
        self,
        operation_id: str,
        expected_version: int,
        intent_digest: str,
        retirement_receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def rollback_runtime_replacement(
        self,
        operation_id: str,
        expected_version: int,
        intent_digest: str,
        failure_code: str,
        receipt: Mapping[str, Any],
        descriptor_transactions: tuple[Any, ...],
        at: str,
    ) -> dict[str, Any]: ...

    def record_runtime_replacement_recovery(
        self,
        operation_id: str,
        expected_version: int,
        intent_digest: str,
        failure_code: str,
        at: str,
    ) -> dict[str, Any]: ...

    def resume_runtime_replacement_recovery(
        self,
        operation_id: str,
        expected_version: int,
        intent_digest: str,
        at: str,
    ) -> dict[str, Any]: ...

    def runtime_replacement_status(
        self, operation_id: str
    ) -> Optional[dict[str, Any]]: ...

    def runtime_replacement_launch_context(
        self, assignment_id: str
    ) -> dict[str, Any]: ...

    def prepare_pi_session_migration(
        self, intent: Mapping[str, Any], at: str
    ) -> dict[str, Any]: ...

    def advance_pi_session_migration(
        self,
        migration_id: str,
        intent_digest: str,
        expected_state: str,
        next_state: str,
        receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def pi_session_migration(self, migration_id: str) -> Optional[dict[str, Any]]: ...

    def runtime_binding(self, binding_id: str) -> Optional[dict[str, Any]]: ...

    def reconcile_restored_runtime(
        self,
        runtime_instance_id: str,
        actor_agent_id: str,
        thread_id: str,
        session_ref: str,
        backend_kind: str,
        expected_endpoint: str,
        expected_generation: str,
        observed_endpoint: str,
        observed_generation: str,
        at: str,
    ) -> dict[str, Any]: ...

    def record_restored_runtime_recovery(
        self,
        runtime_instance_id: str,
        actor_agent_id: str,
        failure_code: str,
        at: str,
    ) -> dict[str, Any]: ...

    def satisfy_restored_runtime_recovery(
        self, runtime_instance_id: str, at: str
    ) -> dict[str, Any]: ...

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
