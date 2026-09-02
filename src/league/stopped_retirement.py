"""Provider- and multiplexer-neutral already-stopped agent retirement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from .agent_adapters import AgentAdapterRegistry, builtin_agent_adapter_registry
from .multiplexer_adapters import (
    MultiplexerAdapterRegistry,
    builtin_multiplexer_adapter_registry,
)
from .storage import Storage, StorageRefusal


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
        for key in (
            "operation_id",
            "agent_id",
            "runtime_instance_id",
            "session_ref",
            "endpoint",
            "runtime_generation",
            "provider_kind",
            "multiplexer_kind",
            "callsign_assignment_id",
        ):
            value = strings[key]
            if not isinstance(value, str) or not value or value.strip() != value:
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
        fault: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        spec.validate()
        request = spec.identity()
        request_digest = _digest(request)
        existing = self.store.stopped_agent_retirement(spec.operation_id)
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise StorageRefusal(
                    "stopped_retirement_operation_conflict",
                    "retirement operation identity changed",
                )
            return {**dict(existing["receipt"]), "state": "completed", "idempotent": True}
        target = self.store.stopped_agent_retirement_target(request)
        adapter_kind = str(target["harness_kind"]).removesuffix("-thread")
        adapter = self.agents.adapter(adapter_kind)
        normalized_provider = adapter.normalize_provider(spec.provider_kind)
        observed_provider = adapter.normalize_provider(str(target["display_agent"]))
        if (
            spec.provider_kind != normalized_provider
            or normalized_provider != observed_provider
            or not adapter.accepts_provider(normalized_provider)
        ):
            raise StorageRefusal(
                "stopped_retirement_provider_mismatch",
                "canonical provider does not belong to the selected agent adapter",
            )
        multiplexer = self.multiplexers.adapter(spec.multiplexer_kind)
        proof = adapter.verify_stopped_retirement(
            target={**target, **request},
            provider_kind=normalized_provider,
            multiplexer=multiplexer,
        )
        if (
            not isinstance(proof, Mapping)
            or proof.get("verified") is not True
            or proof.get("runtime_instance_id") != spec.runtime_instance_id
            or proof.get("session_ref") != spec.session_ref
            or proof.get("endpoint") != spec.endpoint
            or proof.get("runtime_generation") != spec.runtime_generation
        ):
            raise StorageRefusal(
                "stopped_retirement_proof_invalid",
                "adapter did not prove the exact stopped runtime",
            )
        return self.store.complete_stopped_agent_retirement(
            request,
            adapter_kind=adapter.contract.kind,
            proof=proof,
            request_digest=request_digest,
            at=spec.at,
            fault=fault,
        )


__all__ = ["RetirementSpec", "StoppedAgentRetirement"]
