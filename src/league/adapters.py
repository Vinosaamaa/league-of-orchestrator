"""Registered harness-session and terminal-backend adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .adapter_types import (
    BACKEND_CAPABILITIES,
    EVIDENCE_LEVELS,
    HARNESS_CAPABILITIES,
    AdapterContract,
    AdapterInstruction,
    AdapterReceipt,
    OpaqueIdentity,
    RuntimeObservation,
)
from .storage_types import StorageRefusal


class HarnessAdapter(Protocol):
    contract: AdapterContract

    def create(self, specification: Mapping[str, Any]) -> AdapterInstruction: ...
    def identify(self, observation: RuntimeObservation) -> OpaqueIdentity: ...
    def title(self, session: OpaqueIdentity, title: str) -> AdapterInstruction: ...
    def prompt(self, session: OpaqueIdentity, prompt: str) -> AdapterInstruction: ...
    def status(self, session: OpaqueIdentity, observation: RuntimeObservation) -> str: ...
    def hook(self, session: OpaqueIdentity, event: str) -> AdapterInstruction: ...
    def interrupt(self, session: OpaqueIdentity) -> AdapterInstruction: ...
    def resume(self, session: OpaqueIdentity) -> AdapterInstruction: ...
    def exit(self, session: OpaqueIdentity) -> AdapterInstruction: ...


class BackendAdapter(Protocol):
    contract: AdapterContract

    def allocate(self, specification: Mapping[str, Any]) -> AdapterReceipt: ...
    def input(self, endpoint: OpaqueIdentity, instruction: AdapterInstruction) -> AdapterReceipt: ...
    def inspect(self, endpoint: OpaqueIdentity) -> RuntimeObservation: ...
    def close(self, endpoint: OpaqueIdentity) -> AdapterReceipt: ...


@dataclass(frozen=True)
class DeclaredHarnessAdapter:
    """Provider-neutral harness semantics with no process or terminal code."""

    contract: AdapterContract

    def _instruction(
        self, capability: str, session: OpaqueIdentity | None = None, **payload: Any
    ) -> AdapterInstruction:
        self.contract.require(capability)
        if session is not None:
            if session.namespace != self.contract.kind:
                raise StorageRefusal("identity_mismatch", "session namespace does not match harness adapter")
            payload = {"session_identity": session.encoded, **payload}
        return AdapterInstruction(capability, {"harness": self.contract.kind, **payload})

    def create(self, specification: Mapping[str, Any]) -> AdapterInstruction:
        return self._instruction("create", specification=dict(specification))

    def identify(self, observation: RuntimeObservation) -> OpaqueIdentity:
        self.contract.require("identify")
        encoded = observation.details.get("session_identity")
        identity = OpaqueIdentity.decode(str(encoded))
        if identity.namespace != self.contract.kind:
            raise StorageRefusal("identity_mismatch", "observed session belongs to another harness")
        return identity

    def title(self, session: OpaqueIdentity, title: str) -> AdapterInstruction:
        if not title.strip():
            raise StorageRefusal("invalid_title", "runtime title cannot be empty")
        return self._instruction("title", session, title=title)

    def prompt(self, session: OpaqueIdentity, prompt: str) -> AdapterInstruction:
        if not prompt.strip():
            raise StorageRefusal("invalid_prompt", "runtime prompt cannot be empty")
        return self._instruction("prompt", session, prompt=prompt)

    def status(self, session: OpaqueIdentity, observation: RuntimeObservation) -> str:
        self.contract.require("status")
        if session.namespace != self.contract.kind:
            raise StorageRefusal("identity_mismatch", "session namespace does not match harness adapter")
        observed = observation.details.get("session_identity")
        if observed is not None and str(observed) != session.encoded:
            raise StorageRefusal("identity_mismatch", "runtime observation belongs to another session")
        return observation.state

    def hook(self, session: OpaqueIdentity, event: str) -> AdapterInstruction:
        if not event.strip():
            raise StorageRefusal("invalid_hook", "hook event cannot be empty")
        return self._instruction("hook", session, event=event)

    def interrupt(self, session: OpaqueIdentity) -> AdapterInstruction:
        return self._instruction("interrupt", session)

    def resume(self, session: OpaqueIdentity) -> AdapterInstruction:
        return self._instruction("resume", session)

    def exit(self, session: OpaqueIdentity) -> AdapterInstruction:
        return self._instruction("exit", session)


class AdapterRegistry:
    """Small explicit registry; lifecycle code contains no kind-specific branch."""

    def __init__(self) -> None:
        self._harnesses: dict[str, HarnessAdapter] = {}
        self._backends: dict[str, BackendAdapter] = {}

    def register_harness(self, adapter: HarnessAdapter) -> None:
        if adapter.contract.category != "harness":
            raise StorageRefusal("adapter_contract_invalid", "harness registry requires a harness contract")
        self._register(self._harnesses, adapter.contract.kind, adapter)

    def register_backend(self, adapter: BackendAdapter) -> None:
        if adapter.contract.category != "backend":
            raise StorageRefusal("adapter_contract_invalid", "backend registry requires a backend contract")
        self._register(self._backends, adapter.contract.kind, adapter)

    @staticmethod
    def _register(registry: dict[str, Any], kind: str, adapter: Any) -> None:
        if kind in registry:
            raise StorageRefusal("adapter_conflict", f"adapter is already registered: {kind}")
        registry[kind] = adapter

    def harness(self, kind: str) -> HarnessAdapter:
        try:
            return self._harnesses[kind]
        except KeyError as exc:
            raise StorageRefusal("adapter_unknown", f"harness adapter is not registered: {kind}") from exc

    def backend(self, kind: str) -> BackendAdapter:
        try:
            return self._backends[kind]
        except KeyError as exc:
            raise StorageRefusal("adapter_unknown", f"backend adapter is not registered: {kind}") from exc

    def capability_matrix(self) -> dict[str, Any]:
        pairs: list[dict[str, Any]] = []
        for harness in sorted(self._harnesses.values(), key=lambda item: item.contract.kind):
            for backend in sorted(self._backends.values(), key=lambda item: item.contract.kind):
                pair_evidence = min(
                    (harness.contract.evidence, backend.contract.evidence),
                    key=EVIDENCE_LEVELS.index,
                )
                driver_available = backend.contract.availability == "available"

                def status(capability: str, declared: frozenset[str]) -> str:
                    if capability not in declared:
                        return "unsupported"
                    if not driver_available:
                        return "driver_unavailable"
                    return "unverified" if pair_evidence == "unverified" else "supported"

                operations = {
                    capability: status(capability, harness.contract.capabilities)
                    for capability in sorted(HARNESS_CAPABILITIES)
                }
                operations.update(
                    {
                        f"backend.{capability}": status(capability, backend.contract.capabilities)
                        for capability in sorted(BACKEND_CAPABILITIES)
                    }
                )
                pairs.append(
                    {
                        "harness": harness.contract.kind,
                        "backend": backend.contract.kind,
                        "evidence": pair_evidence,
                        "availability": (
                            "operational" if driver_available else "contract-only"
                        ),
                        "operations": operations,
                    }
                )
        return {"schema": "league.adapter-matrix.v1", "pairs": pairs}


def builtin_harness_contracts() -> tuple[DeclaredHarnessAdapter, ...]:
    # Compatibility facade: established RuntimeLifecycle callers keep this
    # import while contracts now come from the explicit provider registry.
    from .agent_adapters import builtin_agent_adapter_registry

    return tuple(builtin_agent_adapter_registry().adapters())


@dataclass(frozen=True)
class ContractOnlyBackendAdapter:
    """Expose an honest built-in matrix without touching a real multiplexer."""

    contract: AdapterContract

    def _unavailable(self, capability: str) -> Any:
        self.contract.require(capability)
        raise StorageRefusal(
            "runtime_driver_unavailable",
            f"backend {self.contract.kind} requires the separately gated runtime driver",
        )

    def allocate(self, specification: Mapping[str, Any]) -> AdapterReceipt:
        return self._unavailable("allocate")

    def input(self, endpoint: OpaqueIdentity, instruction: AdapterInstruction) -> AdapterReceipt:
        return self._unavailable("input")

    def inspect(self, endpoint: OpaqueIdentity) -> RuntimeObservation:
        return self._unavailable("inspect")

    def close(self, endpoint: OpaqueIdentity) -> AdapterReceipt:
        return self._unavailable("close")


def builtin_backend_contracts() -> tuple[ContractOnlyBackendAdapter, ...]:
    return (
        ContractOnlyBackendAdapter(
            AdapterContract(
                "herdr",
                "backend",
                frozenset(BACKEND_CAPABILITIES),
                "inherited-contract",
                "contract-only",
                "Compatibility contract for inherited Herdr allocation/input/inspection/close behavior.",
            )
        ),
        ContractOnlyBackendAdapter(
            AdapterContract(
                "tmux",
                "backend",
                frozenset(BACKEND_CAPABILITIES - {"allocate"}),
                "inherited-contract",
                "contract-only",
                "Compatibility contract for inherited tmux input/inspection/close behavior; allocation remains unsupported.",
            )
        ),
    )


def builtin_registry(backends: tuple[BackendAdapter, ...]) -> AdapterRegistry:
    registry = AdapterRegistry()
    for harness in builtin_harness_contracts():
        registry.register_harness(harness)
    for backend in backends:
        registry.register_backend(backend)
    return registry


def builtin_contract_registry() -> AdapterRegistry:
    return builtin_registry(tuple(builtin_backend_contracts()))


def production_capability_matrix() -> dict[str, Any]:
    """Report low-level compatibility honestly and list semantic facades separately.

    ``RuntimeLifecycle`` still consumes the original low-level harness/backend
    contract, so its operations remain ``driver_unavailable`` for built-ins.
    Repository lifecycle commands use the explicit semantic registries exposed
    in ``lifecycle_operations``; the two surfaces must not be conflated.
    """

    from .agent_adapters import builtin_agent_adapter_registry
    from .multiplexer_adapters import builtin_multiplexer_adapter_registry

    pairs = builtin_contract_registry().capability_matrix()["pairs"]
    agents = builtin_agent_adapter_registry()
    multiplexers = builtin_multiplexer_adapter_registry()
    mux_requirements = {
        "launch": frozenset({"visible_launch"}),
        "resume": frozenset({"visible_launch"}),
        "steer": frozenset({"delivery"}),
        "title": frozenset({"title"}),
        "delivery": frozenset({"delivery"}),
        "retirement": frozenset({"production_cleanup"}),
        "cleanup": frozenset({"production_cleanup"}),
    }
    for pair in pairs:
        multiplexer = multiplexers.adapter(str(pair["backend"]))
        agent = agents.adapter(str(pair["harness"]))
        pair["lifecycle_operations"] = {}
        for operation in sorted(agent.lifecycle_operations):
            required = mux_requirements.get(operation, frozenset())
            pair["lifecycle_operations"][operation] = (
                "supported"
                if required <= multiplexer.capabilities
                else "driver_unavailable"
            )
        pair["multiplexer_operations"] = {
            operation: "supported"
            for operation in sorted(multiplexer.capabilities)
        }
        pair["semantic_availability"] = (
            "operational"
            if "visible_launch" in multiplexer.capabilities
            else "contract-only"
        )
    return {
        "schema": "league.adapter-matrix.v1",
        "driver": "low-level-contract+explicit-semantic-facades",
        "pairs": pairs,
    }
