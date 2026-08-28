"""Registered harness-session and terminal-backend adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .adapter_types import (
    BACKEND_CAPABILITIES,
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
        self.contract.require(capability, HARNESS_CAPABILITIES)
        if session is not None:
            if session.namespace != self.contract.kind:
                raise StorageRefusal("identity_mismatch", "session namespace does not match harness adapter")
            payload = {"session_identity": session.encoded, **payload}
        return AdapterInstruction(capability, {"harness": self.contract.kind, **payload})

    def create(self, specification: Mapping[str, Any]) -> AdapterInstruction:
        return self._instruction("create", specification=dict(specification))

    def identify(self, observation: RuntimeObservation) -> OpaqueIdentity:
        self.contract.require("identify", HARNESS_CAPABILITIES)
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
        self.contract.require("status", HARNESS_CAPABILITIES)
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
        self._register(self._harnesses, adapter.contract.kind, adapter)

    def register_backend(self, adapter: BackendAdapter) -> None:
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
                    key=("unverified", "inherited-contract", "isolated-double", "real-canary").index,
                )
                operations = {
                    capability: (
                        "unsupported"
                        if capability not in harness.contract.capabilities
                        else ("unverified" if pair_evidence == "unverified" else "supported")
                    )
                    for capability in sorted(HARNESS_CAPABILITIES)
                }
                operations.update(
                    {
                        f"backend.{capability}": (
                            "unsupported"
                            if capability not in backend.contract.capabilities
                            else ("unverified" if pair_evidence == "unverified" else "supported")
                        )
                        for capability in sorted(BACKEND_CAPABILITIES)
                    }
                )
                pairs.append(
                    {
                        "harness": harness.contract.kind,
                        "backend": backend.contract.kind,
                        "evidence": pair_evidence,
                        "operations": operations,
                    }
                )
        return {"schema": "league.adapter-matrix.v1", "pairs": pairs}


def builtin_harness_contracts() -> tuple[DeclaredHarnessAdapter, ...]:
    return (
        DeclaredHarnessAdapter(
            AdapterContract(
                "codex",
                frozenset(HARNESS_CAPABILITIES - {"resume"}),
                "inherited-contract",
                "Compatibility contract for the proven Codex watcher behavior; real cutover canary is issue #23.",
            )
        ),
        DeclaredHarnessAdapter(
            AdapterContract(
                "pi",
                frozenset(HARNESS_CAPABILITIES),
                "unverified",
                "Non-Codex contract exercised only through deterministic isolated doubles until issue #23.",
            )
        ),
    )


@dataclass(frozen=True)
class ContractOnlyBackendAdapter:
    """Expose an honest built-in matrix without touching a real multiplexer."""

    contract: AdapterContract

    def _unavailable(self, capability: str) -> Any:
        self.contract.require(capability, BACKEND_CAPABILITIES)
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
                frozenset(BACKEND_CAPABILITIES),
                "inherited-contract",
                "Compatibility contract for inherited Herdr allocation/input/inspection/close behavior.",
            )
        ),
        ContractOnlyBackendAdapter(
            AdapterContract(
                "tmux",
                frozenset(BACKEND_CAPABILITIES - {"allocate"}),
                "inherited-contract",
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
