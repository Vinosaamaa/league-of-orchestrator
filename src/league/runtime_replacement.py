"""Adapter-neutral orchestration for one fenced active Champion A-to-B switch."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .canonical_delivery import dispatch_event
from .request_services import AssignmentSpec, LaunchAdapterError
from .storage_types import StorageRefusal


@dataclass(frozen=True)
class RuntimeReplacementSpec:
    request: Mapping[str, Any]
    launch_options: Any
    launch_inputs: Mapping[str, Any]
    startup_timeout_ms: int = 120_000


class RuntimeReplacementService:
    """Keep B quiescent until A is retired and one handoff outbox commits."""

    def __init__(
        self,
        store: Any,
        agent_registry: Any,
        multiplexer_registry: Any,
        clock: Any,
        delivery_adapter: Any | None = None,
    ) -> None:
        self.store = store
        self.agent_registry = agent_registry
        self.multiplexer_registry = multiplexer_registry
        self.clock = clock
        self.delivery_adapter = delivery_adapter

    @staticmethod
    def _target(
        identity: Mapping[str, Any], receipt: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        target = dict(identity)
        if receipt is not None:
            target.update(
                {
                    "session_ref": receipt["thread_id"],
                    "endpoint": receipt["endpoint"],
                    "runtime_generation": receipt["runtime_generation"],
                    "cwd": receipt["worktree"],
                    "routing_name": receipt["routing_name"],
                }
            )
        return target

    @staticmethod
    def _failure_code(exc: BaseException) -> str:
        if isinstance(exc, StorageRefusal):
            return exc.code
        if isinstance(exc, LaunchAdapterError):
            return exc.failure_class
        return f"runtime_replacement_{type(exc).__name__.lower()}"

    @staticmethod
    def _launch_target(prepared: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **dict(prepared["successor"]),
            "assignment_id": prepared["assignment_id"],
            "task_id": prepared["task_id"],
            "callsign": prepared["launch"]["callsign"],
            "harness_kind": prepared["successor"]["harness_kind"],
            "repository": prepared["launch"]["repository"],
            "issue": prepared["launch"]["issue"],
            "branch": prepared["launch"]["branch"],
            "cwd": prepared["launch"]["worktree"],
            "required_capabilities": list(
                prepared["launch"]["required_capabilities"]
            ),
        }

    def _descriptor_transactions(
        self,
        *,
        prepared: Mapping[str, Any],
        predecessor_adapter: Any,
        successor_adapter: Any,
        successor_receipt: Mapping[str, Any] | None,
        phase: str,
        activated: bool,
    ) -> tuple[Any, ...]:
        common = {
            "phase": phase,
            "operation_id": str(prepared["operation_id"]),
            "assignment_id": str(prepared["assignment_id"]),
            "activated": activated,
        }
        predecessor_transactions = (
            predecessor_adapter.replacement_descriptor_transactions(
                participant="predecessor",
                target=prepared["predecessor"],
                **common,
            )
        )
        successor_transactions = (
            successor_adapter.replacement_descriptor_transactions(
                participant="successor",
                target=self._target(prepared["successor"], successor_receipt),
                **common,
            )
        )
        if phase == "rollback":
            return tuple((*successor_transactions, *predecessor_transactions))
        return tuple((*predecessor_transactions, *successor_transactions))

    def _dispatch_completed(self, result: Mapping[str, Any]) -> dict[str, Any]:
        event_id = result.get("event_id") or result.get("handoff_event_id")
        outbox_id = result.get("outbox_id") or result.get("handoff_outbox_id")
        recipient = result.get("recipient_agent_id") or result.get(
            "successor_agent_id"
        )
        if any(not isinstance(value, str) or not value for value in (
            event_id,
            outbox_id,
            recipient,
        )):
            raise StorageRefusal(
                "runtime_replacement_completion_invalid",
                "completed replacement lacks its exact handoff identity",
            )
        delivery = dispatch_event(
            self.store,
            outbox_id=outbox_id,
            event_id=event_id,
            recipient_agent_id=recipient,
            at=self.clock.now(),
            adapter=self.delivery_adapter,
        )
        return {
            **dict(result),
            "event_id": event_id,
            "outbox_id": outbox_id,
            "recipient_agent_id": recipient,
            "delivery": delivery,
        }

    def _rollback(
        self,
        *,
        prepared: Mapping[str, Any],
        successor_adapter: Any,
        multiplexer: Any,
        failure: BaseException,
        route_receipt: Mapping[str, Any] | None,
        successor_receipt: Mapping[str, Any] | None,
        driver: Any | None,
        activated: bool,
    ) -> dict[str, Any]:
        # A missing receipt after the launch effect began is not evidence that
        # no successor exists.  Recovery ambiguity must retain the canonical
        # fence and an open recovery obligation until the native endpoint is
        # proven absent or can be bound exactly.
        if successor_receipt is None and driver is None:
            return self.store.record_runtime_replacement_recovery(
                str(prepared["operation_id"]),
                int(prepared["version"]),
                str(prepared["intent_digest"]),
                self._failure_code(failure),
                self.clock.now(),
            )
        route_rollback_verified = route_receipt is None
        successor_cleanup_verified = False
        try:
            if route_receipt is not None:
                route_rollback = multiplexer.replacement_route_rollback(
                    route_receipt=route_receipt
                )
                route_rollback_verified = route_rollback.get("verified") is True
            if successor_receipt is not None:
                target = self._target(prepared["successor"], successor_receipt)
                retired = successor_adapter.retire_replacement(
                    operation_id=str(prepared["operation_id"]),
                    target=target,
                    multiplexer=multiplexer,
                )
                successor_cleanup_verified = (
                    retired.get("verified") is True
                    and retired.get("state") == "retired"
                )
            elif driver is not None:
                if getattr(driver, "created_endpoint", False):
                    successor_cleanup_verified = bool(
                        driver.cleanup(getattr(driver, "launch_receipt", None))
                    )
                else:
                    successor_cleanup_verified = True
            predecessor = self.agent_registry.adapter(
                str(prepared["predecessor"]["adapter_kind"])
            )
            predecessor_exact = predecessor.verify_replacement(
                target=prepared["predecessor"], multiplexer=multiplexer
            )
            predecessor_authoritative = predecessor_exact.get("verified") is True
            descriptor_receipt = successor_receipt
            if descriptor_receipt is None and driver is not None:
                possible_receipt = getattr(driver, "launch_receipt", None)
                if isinstance(possible_receipt, Mapping):
                    descriptor_receipt = possible_receipt
            descriptor_transactions = self._descriptor_transactions(
                prepared=prepared,
                predecessor_adapter=predecessor,
                successor_adapter=successor_adapter,
                successor_receipt=descriptor_receipt,
                phase="rollback",
                activated=activated,
            )
        except Exception as rollback_exc:
            return self.store.record_runtime_replacement_recovery(
                str(prepared["operation_id"]),
                int(prepared["version"]),
                str(prepared["intent_digest"]),
                self._failure_code(rollback_exc),
                self.clock.now(),
            )
        if not (
            route_rollback_verified
            and successor_cleanup_verified
            and predecessor_authoritative
        ):
            return self.store.record_runtime_replacement_recovery(
                str(prepared["operation_id"]),
                int(prepared["version"]),
                str(prepared["intent_digest"]),
                "runtime_replacement_rollback_unverified",
                self.clock.now(),
            )
        receipt = {
            "schema": "league.runtime-replacement-rollback.v1",
            "verified": True,
            "operation_id": prepared["operation_id"],
            "predecessor_authoritative": True,
            "successor_cleanup_verified": True,
            "route_rollback_verified": route_rollback_verified,
            "post_switch": activated,
        }
        return self.store.rollback_runtime_replacement(
            str(prepared["operation_id"]),
            int(prepared["version"]),
            str(prepared["intent_digest"]),
            self._failure_code(failure),
            receipt,
            descriptor_transactions,
            self.clock.now(),
        )

    def replace(self, spec: RuntimeReplacementSpec) -> dict[str, Any]:
        request = dict(spec.request)
        # Registry/capability refusal occurs before the first canonical write.
        successor_adapter = self.agent_registry.adapter(
            str(request.get("successor_adapter_kind", ""))
        )
        multiplexer = self.multiplexer_registry.adapter(
            str(request.get("multiplexer_kind", ""))
        )
        if (
            "replacement" not in successor_adapter.lifecycle_operations
            or not successor_adapter.accepts_provider(
                str(request.get("successor_provider_kind", ""))
            )
        ):
            raise StorageRefusal(
                "runtime_replacement_adapter_unsupported",
                "successor adapter/provider does not support runtime replacement",
            )
        if "runtime_replacement" not in multiplexer.capabilities:
            raise StorageRefusal(
                "runtime_replacement_multiplexer_unsupported",
                "selected multiplexer cannot prove and compensate replacement",
            )

        prepared = self.store.prepare_runtime_replacement(request, self.clock.now())
        if prepared["state"] == "completed":
            status = self.store.runtime_replacement_status(
                str(prepared["operation_id"])
            )
            assert isinstance(status, Mapping)
            return self._dispatch_completed(status)
        if prepared["state"] == "rolled_back":
            return self.store.runtime_replacement_status(
                str(prepared["operation_id"])
            )
        if prepared["state"] == "recovery_required":
            resumed = self.store.resume_runtime_replacement_recovery(
                str(prepared["operation_id"]),
                int(prepared["version"]),
                str(prepared["intent_digest"]),
                self.clock.now(),
            )
            prepared = {**prepared, **resumed, "recovery_state": None}
        predecessor_adapter = self.agent_registry.adapter(
            str(prepared["predecessor"]["adapter_kind"])
        )
        if (
            "replacement" not in predecessor_adapter.lifecycle_operations
            or not predecessor_adapter.accepts_provider(
                str(prepared["predecessor"]["provider_kind"])
            )
        ):
            raise StorageRefusal(
                "runtime_replacement_adapter_unsupported",
                "predecessor adapter/provider does not support runtime replacement",
            )
        successor_receipt = prepared.get("successor_receipt")
        route_receipt = prepared.get("route_receipt")
        retirement_receipt = prepared.get("retirement_receipt")
        driver = None
        launched_here = False
        if prepared["state"] == "prepared":
            effect = self.store.begin_runtime_replacement_effect(
                str(prepared["operation_id"]),
                int(prepared["version"]),
                str(prepared["intent_digest"]),
                "launch",
                self.clock.now(),
            )
            prepared = {**prepared, **effect}
            launched_here = effect["idempotent"] is False

        if prepared["state"] == "launching":
            launch = {
                **dict(spec.launch_inputs),
                "assignment_id": prepared["assignment_id"],
                "task_id": prepared["task_id"],
                "champion_agent_id": prepared["successor"]["agent_id"],
                "repository": prepared["launch"]["repository"],
                "issue": prepared["launch"]["issue"],
                "branch": prepared["launch"]["branch"],
                "worktree": prepared["launch"]["worktree"],
                "provider_kind": prepared["successor"]["provider_kind"],
                "launch_descriptor_id": (
                    f"runtime-replacement:{prepared['operation_id']}"
                ),
                "at": self.clock.now(),
            }
            try:
                if launched_here:
                    driver = successor_adapter.visible_launch(
                        store=self.store,
                        options=spec.launch_options,
                        multiplexer=multiplexer,
                        startup_timeout_ms=spec.startup_timeout_ms,
                        launch=launch,
                    )
                    assignment = AssignmentSpec(
                        assignment_id=str(prepared["assignment_id"]),
                        request_id=str(prepared["launch"]["request_id"]),
                        claim_token="replacement-fenced",
                        task_id=str(prepared["task_id"]),
                        task_summary=str(prepared["launch"]["task_summary"]),
                        coordinator_agent_id=str(
                            prepared["launch"]["coordinator_agent_id"]
                        ),
                        champion_agent_id=str(prepared["successor"]["agent_id"]),
                        repository=str(prepared["launch"]["repository"]),
                        issue=int(prepared["launch"]["issue"]),
                        branch=str(prepared["launch"]["branch"]),
                        worktree=str(prepared["launch"]["worktree"]),
                        issue_receipt=None,
                        required_capabilities=tuple(
                            prepared["launch"]["required_capabilities"]
                        ),
                        callsign=str(prepared["launch"]["callsign"]),
                        routing_name=str(prepared["successor"]["routing_name"]),
                        launch_operation_id=str(prepared["operation_id"]),
                    )
                    successor_receipt = driver.launch(assignment)
                    if isinstance(successor_receipt, dict):
                        successor_receipt["runtime_instance_id"] = prepared[
                            "successor"
                        ]["runtime_instance_id"]
                else:
                    successor_receipt = successor_adapter.recover_replacement(
                        target=self._launch_target(prepared),
                        multiplexer=multiplexer,
                    )
                    if successor_receipt is None:
                        return self._rollback(
                            prepared=prepared,
                            successor_adapter=successor_adapter,
                            multiplexer=multiplexer,
                            failure=StorageRefusal(
                                "runtime_replacement_launch_interrupted",
                                "no exact staged successor survived the launch boundary",
                            ),
                            route_receipt=None,
                            successor_receipt=None,
                            driver=None,
                            activated=False,
                        )
                successor_target = self._target(
                    prepared["successor"], successor_receipt
                )
                successor_verification = successor_adapter.verify_replacement(
                    target=successor_target, multiplexer=multiplexer
                )
                if successor_verification.get("verified") is not True:
                    raise StorageRefusal(
                        "runtime_replacement_successor_unverified",
                        "successor adapter did not prove exact native identity",
                    )
                recorded = self.store.record_replacement_successor_verified(
                    str(prepared["operation_id"]),
                    int(prepared["version"]),
                    str(prepared["intent_digest"]),
                    successor_receipt,
                    self.clock.now(),
                )
                prepared = {
                    **prepared,
                    "state": recorded["state"],
                    "version": recorded["version"],
                    "successor_receipt": successor_receipt,
                }
            except Exception as exc:
                return self._rollback(
                    prepared=prepared,
                    successor_adapter=successor_adapter,
                    multiplexer=multiplexer,
                    failure=exc,
                    route_receipt=None,
                    successor_receipt=successor_receipt,
                    driver=driver,
                    activated=False,
                )

        if prepared["state"] == "successor_verified":
            effect = self.store.begin_runtime_replacement_effect(
                str(prepared["operation_id"]),
                int(prepared["version"]),
                str(prepared["intent_digest"]),
                "route_swap",
                self.clock.now(),
            )
            prepared = {**prepared, **effect}

        if prepared["state"] == "route_swapping":
            assert isinstance(successor_receipt, Mapping)
            successor_target = self._target(prepared["successor"], successor_receipt)
            try:
                route_receipt = multiplexer.replacement_route_swap(
                    operation_id=prepared["operation_id"],
                    predecessor=prepared["predecessor"],
                    successor=successor_target,
                    predecessor_adapter_kind=predecessor_adapter.contract.kind,
                    predecessor_provider_kind=prepared["predecessor"][
                        "provider_kind"
                    ],
                    predecessor_process_names=predecessor_adapter.process_names,
                    successor_adapter_kind=successor_adapter.contract.kind,
                    successor_provider_kind=prepared["successor"]["provider_kind"],
                    successor_process_names=successor_adapter.process_names,
                )
                descriptor_transactions = self._descriptor_transactions(
                    prepared=prepared,
                    predecessor_adapter=predecessor_adapter,
                    successor_adapter=successor_adapter,
                    successor_receipt=successor_receipt,
                    phase="activation",
                    activated=False,
                )
                activated = self.store.activate_runtime_replacement(
                    str(prepared["operation_id"]),
                    int(prepared["version"]),
                    str(prepared["intent_digest"]),
                    route_receipt,
                    descriptor_transactions,
                    self.clock.now(),
                )
                prepared = {
                    **prepared,
                    **activated,
                    "successor_receipt": successor_receipt,
                    "route_receipt": route_receipt,
                }
            except Exception as exc:
                return self._rollback(
                    prepared=prepared,
                    successor_adapter=successor_adapter,
                    multiplexer=multiplexer,
                    failure=exc,
                    route_receipt=route_receipt,
                    successor_receipt=successor_receipt,
                    driver=driver,
                    activated=False,
                )

        if prepared["state"] == "activated":
            effect = self.store.begin_runtime_replacement_effect(
                str(prepared["operation_id"]),
                int(prepared["version"]),
                str(prepared["intent_digest"]),
                "retirement",
                self.clock.now(),
            )
            prepared = {**prepared, **effect}

        if prepared["state"] == "retiring":
            assert isinstance(route_receipt, Mapping)
            predecessor_target = {
                **prepared["predecessor"],
                "routing_name": route_receipt[
                    "predecessor_staging_routing_name"
                ],
            }
            try:
                retirement_receipt = predecessor_adapter.retire_replacement(
                    operation_id=str(prepared["operation_id"]),
                    target=predecessor_target,
                    multiplexer=multiplexer,
                )
                retired = self.store.record_replacement_predecessor_retired(
                    str(prepared["operation_id"]),
                    int(prepared["version"]),
                    str(prepared["intent_digest"]),
                    retirement_receipt,
                    self.clock.now(),
                )
                prepared = {
                    **prepared,
                    "state": retired["state"],
                    "version": retired["version"],
                    "retirement_receipt": retirement_receipt,
                }
            except Exception as exc:
                return self._rollback(
                    prepared=prepared,
                    successor_adapter=successor_adapter,
                    multiplexer=multiplexer,
                    failure=exc,
                    route_receipt=route_receipt,
                    successor_receipt=successor_receipt,
                    driver=driver,
                    activated=True,
                )

        if prepared["state"] == "predecessor_retired":
            assert isinstance(retirement_receipt, Mapping)
            completed = self.store.complete_runtime_replacement(
                str(prepared["operation_id"]),
                int(prepared["version"]),
                str(prepared["intent_digest"]),
                retirement_receipt,
                f"event:{prepared['operation_id']}:handoff",
                f"outbox:{prepared['operation_id']}:handoff",
                self.clock.now(),
            )
            return self._dispatch_completed(completed)
        return self.store.runtime_replacement_status(str(prepared["operation_id"]))


__all__ = ["RuntimeReplacementService", "RuntimeReplacementSpec"]
