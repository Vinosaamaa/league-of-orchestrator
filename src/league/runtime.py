"""Adapter-neutral runtime lifecycle orchestration.

This module coordinates harness semantics and backend transport.  It owns no
request, assignment, outbox, or Stop-hook state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

from .adapter_types import (
    BACKEND_CAPABILITIES,
    HARNESS_CAPABILITIES,
    AdapterReceipt,
    OpaqueIdentity,
)
from .adapters import AdapterRegistry
from .storage_types import StorageRefusal


class RuntimeStorage(Protocol):
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


@dataclass(frozen=True)
class RuntimeCreateSpec:
    binding_id: str
    task_id: str
    harness_kind: str
    backend_kind: str
    title: str
    at: str
    harness: Mapping[str, Any]
    backend: Mapping[str, Any]


class RuntimeLifecycle:
    """One generic create/identify/route/wake/resume/exit flow."""

    def __init__(self, storage: RuntimeStorage, registry: AdapterRegistry) -> None:
        self.storage = storage
        self.registry = registry

    def create(self, specification: RuntimeCreateSpec) -> dict[str, Any]:
        harness = self.registry.harness(specification.harness_kind)
        backend = self.registry.backend(specification.backend_kind)
        for capability in ("create", "identify", "title", "status", "exit"):
            harness.contract.require(capability, HARNESS_CAPABILITIES)
        for capability in ("allocate", "input", "inspect", "close"):
            backend.contract.require(capability, BACKEND_CAPABILITIES)

        allocation = backend.allocate(specification.backend)
        endpoint = allocation.identity
        session: OpaqueIdentity | None = None
        endpoint_generation: str | None = None
        try:
            if endpoint.namespace != specification.backend_kind:
                raise StorageRefusal("identity_mismatch", "allocated endpoint namespace conflicts with backend")
            backend.input(endpoint, harness.create(specification.harness))
            observation = self._inspect_exact(backend, endpoint)
            endpoint_generation = observation.generation
            if not endpoint_generation or endpoint_generation.strip() != endpoint_generation:
                raise StorageRefusal("identity_mismatch", "runtime endpoint generation is not exact")
            session = harness.identify(observation)
            if harness.status(session, observation) not in {"active", "idle", "working"}:
                raise StorageRefusal("runtime_not_active", "created runtime did not reach an active state")
            backend.input(endpoint, harness.title(session, specification.title))
            capabilities = {
                "harness": sorted(harness.contract.capabilities),
                "backend": sorted(backend.contract.capabilities),
                "evidence": {
                    "harness": harness.contract.evidence,
                    "backend": backend.contract.evidence,
                },
            }
            binding = self.storage.register_runtime_binding(
                specification.binding_id,
                specification.task_id,
                specification.harness_kind,
                specification.backend_kind,
                session.encoded,
                endpoint.encoded,
                endpoint_generation,
                capabilities,
                specification.at,
            )
            return {**binding, "session_identity": session.encoded, "endpoint_identity": endpoint.encoded}
        except BaseException as original:
            try:
                observation = backend.inspect(endpoint)
                if observation.endpoint_identity != endpoint:
                    raise StorageRefusal("identity_mismatch", "partial runtime endpoint identity changed")
                if endpoint_generation is not None and observation.generation != endpoint_generation:
                    raise StorageRefusal("identity_mismatch", "partial runtime endpoint generation changed")
                if session is not None and observation.state not in {"closed", "missing"}:
                    if harness.identify(observation) != session:
                        raise StorageRefusal("identity_mismatch", "partial runtime session identity changed")
                    backend.input(endpoint, harness.exit(session))
                    observation = backend.inspect(endpoint)
                if observation.state != "missing":
                    backend.close(endpoint)
                if backend.inspect(endpoint).state not in {"closed", "missing"}:
                    raise StorageRefusal("runtime_rollback_failed", "partial runtime endpoint remained active")
            except BaseException as rollback:
                raise StorageRefusal(
                    "runtime_rollback_failed",
                    "partial runtime could not be rolled back with exact identity",
                ) from rollback
            raise original

    @staticmethod
    def _inspect_exact(backend: Any, endpoint: OpaqueIdentity, generation: str | None = None) -> Any:
        observation = backend.inspect(endpoint)
        if observation.endpoint_identity != endpoint:
            raise StorageRefusal("identity_mismatch", "runtime inspection returned another endpoint")
        if generation is not None and observation.generation != generation:
            raise StorageRefusal("identity_mismatch", "runtime endpoint generation changed")
        return observation

    def _bound(
        self, binding_id: str
    ) -> tuple[dict[str, Any], Any, Any, OpaqueIdentity, OpaqueIdentity, str]:
        binding = self.storage.runtime_binding(binding_id)
        if binding is None:
            raise StorageRefusal("binding_unknown", "runtime binding does not exist")
        harness = self.registry.harness(str(binding["harness_kind"]))
        backend = self.registry.backend(str(binding["backend_kind"]))
        session = OpaqueIdentity.decode(str(binding["session_identity"]))
        endpoint = OpaqueIdentity.decode(str(binding["endpoint_identity"]))
        if session.namespace != harness.contract.kind or endpoint.namespace != backend.contract.kind:
            raise StorageRefusal("identity_mismatch", "persisted runtime namespaces conflict with adapters")
        generation = str(binding["endpoint_generation"])
        if not generation or generation.strip() != generation:
            raise StorageRefusal("identity_mismatch", "persisted endpoint generation is invalid")
        return binding, harness, backend, session, endpoint, generation

    def _guard_input(
        self,
        harness: Any,
        backend: Any,
        session: OpaqueIdentity,
        endpoint: OpaqueIdentity,
        generation: str,
    ) -> None:
        observation = self._inspect_exact(backend, endpoint, generation)
        if harness.identify(observation) != session:
            raise StorageRefusal("identity_mismatch", "runtime input target is not the persisted session")

    def prompt(self, binding_id: str, prompt: str) -> AdapterReceipt:
        _, harness, backend, session, endpoint, generation = self._bound(binding_id)
        for capability in ("identify", "prompt"):
            harness.contract.require(capability, HARNESS_CAPABILITIES)
        for capability in ("inspect", "input"):
            backend.contract.require(capability, BACKEND_CAPABILITIES)
        self._guard_input(harness, backend, session, endpoint, generation)
        return backend.input(endpoint, harness.prompt(session, prompt))

    def wake(self, binding_id: str, event: str) -> AdapterReceipt:
        _, harness, backend, session, endpoint, generation = self._bound(binding_id)
        for capability in ("identify", "hook"):
            harness.contract.require(capability, HARNESS_CAPABILITIES)
        for capability in ("inspect", "input"):
            backend.contract.require(capability, BACKEND_CAPABILITIES)
        self._guard_input(harness, backend, session, endpoint, generation)
        return backend.input(endpoint, harness.hook(session, event))

    def resume(self, binding_id: str) -> AdapterReceipt:
        _, harness, backend, session, endpoint, generation = self._bound(binding_id)
        for capability in ("identify", "resume"):
            harness.contract.require(capability, HARNESS_CAPABILITIES)
        for capability in ("inspect", "input"):
            backend.contract.require(capability, BACKEND_CAPABILITIES)
        observation = self._inspect_exact(backend, endpoint, generation)
        observed = harness.identify(observation)
        if observed != session:
            raise StorageRefusal("identity_mismatch", "resume observation conflicts with persisted session")
        return backend.input(endpoint, harness.resume(session))

    def interrupt(self, binding_id: str) -> AdapterReceipt:
        _, harness, backend, session, endpoint, generation = self._bound(binding_id)
        for capability in ("identify", "interrupt"):
            harness.contract.require(capability, HARNESS_CAPABILITIES)
        for capability in ("inspect", "input"):
            backend.contract.require(capability, BACKEND_CAPABILITIES)
        self._guard_input(harness, backend, session, endpoint, generation)
        return backend.input(endpoint, harness.interrupt(session))

    def status(self, binding_id: str) -> str:
        _, harness, backend, session, endpoint, generation = self._bound(binding_id)
        harness.contract.require("status", HARNESS_CAPABILITIES)
        backend.contract.require("inspect", BACKEND_CAPABILITIES)
        return harness.status(session, self._inspect_exact(backend, endpoint, generation))

    def guarded_exit(self, binding_id: str, expected_version: int, at: str) -> dict[str, Any]:
        binding, harness, backend, session, endpoint, generation = self._bound(binding_id)
        for capability in ("identify", "exit"):
            harness.contract.require(capability, HARNESS_CAPABILITIES)
        for capability in ("input", "inspect", "close"):
            backend.contract.require(capability, BACKEND_CAPABILITIES)
        if int(binding["version"]) != expected_version:
            raise StorageRefusal("version_conflict", "runtime binding version changed")
        observation = self._inspect_exact(backend, endpoint, generation)
        if observation.state not in {"closed", "missing"}:
            if harness.identify(observation) != session:
                raise StorageRefusal("identity_mismatch", "exit observation conflicts with persisted session")
            backend.input(endpoint, harness.exit(session))
        after_exit = self._inspect_exact(backend, endpoint, generation)
        if after_exit.state not in {"closed", "idle", "missing"}:
            raise StorageRefusal("runtime_exit_unverified", "harness exit did not reach a safe state")
        if after_exit.state != "missing":
            backend.close(endpoint)
        closed = self._inspect_exact(backend, endpoint, generation)
        if closed.state not in {"closed", "missing"}:
            raise StorageRefusal("endpoint_close_unverified", "backend endpoint close was not verified")
        return self.storage.update_runtime_binding(
            binding_id,
            expected_version,
            "closed",
            at,
            {
                "session_identity": session.encoded,
                "endpoint_identity": endpoint.encoded,
                "observed_state": closed.state,
            },
        )
