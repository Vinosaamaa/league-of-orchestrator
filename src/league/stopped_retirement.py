"""Provider- and multiplexer-neutral already-stopped agent retirement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from .agent_adapters import AgentAdapterRegistry, builtin_agent_adapter_registry
from .multiplexer_adapters import (
    MultiplexerAdapterRegistry,
    builtin_multiplexer_adapter_registry,
)
from .storage import FaultInjector, Storage, StorageRefusal


MAX_ID_LENGTH = 256
MAX_KIND_LENGTH = 64
MAX_SESSION_LENGTH = 2048
MAX_GENERATION_LENGTH = 512
MAX_TIME_LENGTH = 64
MAX_PROOF_BYTES = 16_384


@dataclass(frozen=True)
class RetirementSpec:
    operation_id: str
    agent_id: str
    runtime_instance_id: str
    session_ref: str
    endpoint: str
    runtime_generation: str
    provider_kind: str
    multiplexer_kind: str
    expected_agent_version: int
    callsign_assignment_id: str
    expected_callsign_version: int
    terminal_status: str
    at: str

    def identity(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("at")
        return value

    def validate(self) -> None:
        strings = self.identity()
        bounds = {
            "operation_id": MAX_ID_LENGTH,
            "agent_id": MAX_ID_LENGTH,
            "runtime_instance_id": MAX_ID_LENGTH,
            "session_ref": MAX_SESSION_LENGTH,
            "endpoint": MAX_SESSION_LENGTH,
            "runtime_generation": MAX_GENERATION_LENGTH,
            "provider_kind": MAX_KIND_LENGTH,
            "multiplexer_kind": MAX_KIND_LENGTH,
            "callsign_assignment_id": MAX_ID_LENGTH,
        }
        for key, maximum in bounds.items():
            value = strings[key]
            if (
                not isinstance(value, str)
                or not value
                or value.strip() != value
                or len(value) > maximum
            ):
                raise StorageRefusal(
                    "stopped_retirement_invalid", "retirement identity is incomplete"
                )
        if (
            isinstance(self.expected_agent_version, bool)
            or not isinstance(self.expected_agent_version, int)
            or self.expected_agent_version < 1
            or isinstance(self.expected_callsign_version, bool)
            or not isinstance(self.expected_callsign_version, int)
            or self.expected_callsign_version < 1
            or self.terminal_status not in {"completed", "cancelled", "failed"}
        ):
            raise StorageRefusal(
                "stopped_retirement_invalid", "retirement versions or terminal status are invalid"
            )
        if not isinstance(self.at, str) or len(self.at) > MAX_TIME_LENGTH:
            raise StorageRefusal(
                "stopped_retirement_invalid",
                "retirement time must be RFC3339",
            )
        try:
            parsed_at = datetime.fromisoformat(self.at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise StorageRefusal(
                "stopped_retirement_invalid",
                "retirement time must be RFC3339",
            ) from exc
        if parsed_at.tzinfo is None:
            raise StorageRefusal(
                "stopped_retirement_invalid",
                "retirement time must include a UTC offset",
            )


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _proof_size(value: Mapping[str, Any]) -> int:
    try:
        return len(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise StorageRefusal(
            "stopped_retirement_proof_invalid",
            "adapter proof is not bounded canonical JSON",
        ) from exc


class StoppedAgentRetirement:
    def __init__(
        self,
        store: Storage,
        *,
        agent_registry: Optional[AgentAdapterRegistry] = None,
        multiplexer_registry: Optional[MultiplexerAdapterRegistry] = None,
    ) -> None:
        self.store = store
        self.agents = agent_registry or builtin_agent_adapter_registry()
        self.multiplexers = (
            multiplexer_registry or builtin_multiplexer_adapter_registry()
        )

    def retire(
        self,
        spec: RetirementSpec,
        *,
        fault: Optional[FaultInjector] = None,
    ) -> dict[str, Any]:
        spec.validate()
        requested = spec.identity()
        existing = self.store.stopped_agent_retirement(spec.operation_id)
        if existing is not None:
            existing_adapter = self.agents.adapter(str(existing["adapter_kind"]))
            request = {
                **requested,
                "provider_kind": existing_adapter.normalize_provider(
                    spec.provider_kind
                ),
            }
            request_digest = _digest(request)
            if existing["request_digest"] != request_digest:
                raise StorageRefusal(
                    "stopped_retirement_operation_conflict",
                    "retirement operation identity changed",
                )
            return {**dict(existing["receipt"]), "state": "completed", "idempotent": True}
        target = self.store.stopped_agent_retirement_adapter_identity(requested)
        adapter_kind = str(target["harness_kind"]).removesuffix("-thread")
        adapter = self.agents.adapter(adapter_kind)
        normalized_provider = adapter.normalize_provider(spec.provider_kind)
        observed_provider = adapter.normalize_provider(str(target["display_agent"]))
        if (
            normalized_provider != observed_provider
            or not adapter.accepts_provider(normalized_provider)
        ):
            raise StorageRefusal(
                "stopped_retirement_provider_mismatch",
                "canonical provider does not belong to the selected agent adapter",
            )
        request = {**requested, "provider_kind": normalized_provider}
        request_digest = _digest(request)
        multiplexer = self.multiplexers.adapter(spec.multiplexer_kind)

        def verify(canonical: Mapping[str, Any]) -> Mapping[str, Any]:
            proof = adapter.verify_stopped_retirement(
                target={**canonical, **request},
                provider_kind=normalized_provider,
                multiplexer=multiplexer,
            )
            if (
                not isinstance(proof, Mapping)
                or _proof_size(proof) > MAX_PROOF_BYTES
                or proof.get("verified") is not True
                or proof.get("endpoint_absent") is not True
                or proof.get("adapter_kind") != adapter.contract.kind
                or proof.get("provider_kind") != normalized_provider
                or proof.get("multiplexer_kind") != spec.multiplexer_kind
                or proof.get("runtime_instance_id") != spec.runtime_instance_id
                or proof.get("session_ref") != spec.session_ref
                or proof.get("endpoint") != spec.endpoint
                or proof.get("runtime_generation") != spec.runtime_generation
            ):
                raise StorageRefusal(
                    "stopped_retirement_proof_invalid",
                    "adapter did not prove the exact stopped runtime",
                )
            return proof

        return self.store.complete_stopped_agent_retirement(
            request,
            adapter_kind=adapter.contract.kind,
            verifier=verify,
            request_digest=request_digest,
            at=spec.at,
            fault=fault,
        )


__all__ = ["RetirementSpec", "StoppedAgentRetirement"]
