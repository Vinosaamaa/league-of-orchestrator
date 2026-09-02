"""Shared provider-neutral lifecycle policy and adapter contract.

Adapters translate native provider events into this vocabulary.  They never
decide ownership, authorization, retry, supervision, or cleanup policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from ..adapter_types import AdapterContract, AdapterInstruction, OpaqueIdentity, RuntimeObservation
from ..storage_types import StorageRefusal


ADAPTER_OPERATIONS = frozenset(
    {
        "prompt_intake",
        "pre_tool_authorization",
        "stop_supervision",
        "launch",
        "resume",
        "steer",
        "title",
        "delivery",
        "retirement",
        "cleanup",
        "replacement",
    }
)

OPERATION_METHODS = {
    "prompt_intake": ("translate_event",),
    "pre_tool_authorization": ("translate_event",),
    "stop_supervision": ("translate_event",),
    "launch": ("create",),
    "resume": ("resume",),
    "steer": ("steer",),
    "title": ("title",),
    "delivery": ("deliver",),
    "retirement": ("exit",),
    "cleanup": ("exit",),
    "replacement": (
        "recover_replacement",
        "verify_replacement",
        "retire_replacement",
        "replacement_descriptor_actions",
    ),
}

OPERATION_CAPABILITIES = {
    "launch": frozenset({"create"}),
    "resume": frozenset({"resume"}),
    "steer": frozenset({"prompt"}),
    "title": frozenset({"title"}),
    "delivery": frozenset({"prompt"}),
    "retirement": frozenset({"exit"}),
    "cleanup": frozenset({"exit"}),
    "replacement": frozenset({"create", "exit"}),
}


def declared_lifecycle_operations(
    contract: AdapterContract, native_events: Mapping[str, str]
) -> frozenset[str]:
    operations = set(native_events.values())
    for operation, required in OPERATION_CAPABILITIES.items():
        if required <= contract.capabilities:
            operations.add(operation)
    return frozenset(operations)


@dataclass(frozen=True)
class LifecycleEvent:
    operation: str
    provider_kind: str
    native_event: str
    session_ref: str
    source_event_key: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.operation not in ADAPTER_OPERATIONS:
            raise StorageRefusal("adapter_event_invalid", "agent lifecycle operation is unsupported")
        for value in (self.provider_kind, self.native_event, self.session_ref, self.source_event_key):
            if not isinstance(value, str) or not value or value.strip() != value:
                raise StorageRefusal("adapter_event_invalid", "agent lifecycle event identity is incomplete")


@dataclass(frozen=True)
class LifecycleDecision:
    operation: str
    outcome: str
    reason_code: str


class SharedLifecyclePolicy:
    """One decision seam consumed by all provider adapters, including #81."""

    def decide(
        self,
        event: LifecycleEvent,
        *,
        authorized: bool = True,
        exact_binding: bool = True,
        actor_role: str | None = None,
        delegated_by_shotcaller: bool = False,
        mutation_fenced: bool = False,
    ) -> LifecycleDecision:
        if not exact_binding:
            return LifecycleDecision(event.operation, "refuse", "runtime_binding_mismatch")
        if event.operation == "pre_tool_authorization":
            if mutation_fenced:
                return LifecycleDecision(
                    event.operation, "refuse", "runtime_replacement_fenced"
                )
            role_authorized = actor_role == "shotcaller" or (
                actor_role in {"champion", "hidden-worker"}
                and delegated_by_shotcaller
            )
            if not role_authorized:
                return LifecycleDecision(
                    event.operation, "refuse", "shotcaller_delegation_unverified"
                )
            if not authorized:
                return LifecycleDecision(event.operation, "refuse", "tool_not_authorized")
        return LifecycleDecision(event.operation, "accept", "policy_accepted")


class AgentLifecycleAdapter(Protocol):
    contract: AdapterContract
    lifecycle_operations: frozenset[str]
    hook_profile: Mapping[str, Mapping[str, Any]]
    visible_launch_factory: Any
    multiplexer_requirements: Mapping[str, frozenset[str]]
    process_names: frozenset[str]

    def accepts_provider(self, provider_kind: str) -> bool: ...
    def normalize_provider(self, provider_kind: str) -> str: ...
    def canonical_presentation(self, **inputs: Any) -> Mapping[str, Any]: ...
    def canonical_assignment(self, *, store: Any, row: Mapping[str, Any]) -> str: ...
    def recover_replacement(self, **inputs: Any) -> Mapping[str, Any] | None: ...
    def verify_replacement(self, **inputs: Any) -> Mapping[str, Any]: ...
    def retire_replacement(self, **inputs: Any) -> Mapping[str, Any]: ...
    def replacement_descriptor_actions(self, **inputs: Any) -> tuple[Mapping[str, Any], ...]: ...

    def translate_event(self, native_event: str, payload: Mapping[str, Any]) -> LifecycleEvent: ...
    def visible_launch(self, **inputs: Any) -> Any: ...
    def restored_presentation(
        self, descriptor: Mapping[str, Any], observation: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...
    def create(self, specification: Mapping[str, Any]) -> AdapterInstruction: ...
    def identify(self, observation: RuntimeObservation) -> OpaqueIdentity: ...
    def title(self, session: OpaqueIdentity, title: str) -> AdapterInstruction: ...
    def prompt(self, session: OpaqueIdentity, prompt: str) -> AdapterInstruction: ...
    def steer(self, session: OpaqueIdentity, prompt: str) -> AdapterInstruction: ...
    def deliver(self, **inputs: Any) -> Any: ...
    def status(self, session: OpaqueIdentity, observation: RuntimeObservation) -> str: ...
    def hook(self, session: OpaqueIdentity, event: str) -> AdapterInstruction: ...
    def interrupt(self, session: OpaqueIdentity) -> AdapterInstruction: ...
    def resume(self, session: OpaqueIdentity) -> AdapterInstruction: ...
    def exit(self, session: OpaqueIdentity) -> AdapterInstruction: ...
