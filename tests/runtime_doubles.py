"""Deterministic isolated adapter doubles; never real-runtime evidence."""

from __future__ import annotations

from collections import deque
from typing import Any, Mapping

from league.adapter_types import (
    BACKEND_CAPABILITIES,
    AdapterContract,
    AdapterInstruction,
    AdapterReceipt,
    OpaqueIdentity,
    RuntimeObservation,
)


class DeterministicBackend:
    def __init__(
        self,
        kind: str = "fixture",
        capabilities: frozenset[str] = BACKEND_CAPABILITIES,
    ) -> None:
        self.contract = AdapterContract(
            kind,
            "backend",
            frozenset(capabilities),
            "isolated-double",
            "available",
            "Deterministic in-memory runtime double.",
        )
        self.endpoints: dict[str, dict[str, Any]] = {}
        self.operations: deque[tuple[str, str]] = deque(maxlen=4096)
        self.sequence = 0

    def allocate(self, specification: Mapping[str, Any]) -> AdapterReceipt:
        self.sequence += 1
        identity = OpaqueIdentity(self.contract.kind, f"endpoint-{self.sequence}")
        self.endpoints[identity.encoded] = {
            "state": "idle",
            "generation": f"generation-{self.sequence}",
            "session_identity": None,
            "title": None,
        }
        self.operations.append(("allocate", identity.encoded))
        return AdapterReceipt("allocate", identity, "idle", {"isolated_double": True})

    def input(self, endpoint: OpaqueIdentity, instruction: AdapterInstruction) -> AdapterReceipt:
        state = self.endpoints[endpoint.encoded]
        operation = instruction.operation
        self.operations.append((operation, endpoint.encoded))
        if operation == "create":
            state["session_identity"] = f"{instruction.payload['harness']}:session-{self.sequence}"
            state["state"] = "active"
        elif operation == "title":
            state["title"] = instruction.payload["title"]
        elif operation in {"prompt", "hook"}:
            state.setdefault("messages", []).append(dict(instruction.payload))
        elif operation == "interrupt":
            state["state"] = "idle"
        elif operation == "resume":
            state["state"] = "active"
        elif operation == "exit":
            state["state"] = "closed"
        return AdapterReceipt(operation, endpoint, state["state"], {"isolated_double": True})

    def inspect(self, endpoint: OpaqueIdentity) -> RuntimeObservation:
        state = self.endpoints[endpoint.encoded]
        self.operations.append(("inspect", endpoint.encoded))
        details = {}
        if state["session_identity"] is not None:
            details["session_identity"] = state["session_identity"]
        return RuntimeObservation(endpoint, state["state"], state["generation"], details)

    def close(self, endpoint: OpaqueIdentity) -> AdapterReceipt:
        state = self.endpoints[endpoint.encoded]
        state["state"] = "missing"
        state["session_identity"] = None
        state["title"] = None
        state.pop("messages", None)
        self.operations.append(("close", endpoint.encoded))
        return AdapterReceipt("close", endpoint, "missing", {"isolated_double": True})


class StateCleanupAdapter:
    def __init__(self, kind: str, states: dict[str, dict[str, Any]], effects: list[str]) -> None:
        self.kind = kind
        self.states = states
        self.effects = effects

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(self.states[str(action["action_id"])])

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        action_id = str(action["action_id"])
        self.effects.append(action_id)
        self.states[action_id] = dict(action["intended_state"])
        return {"isolated_double": True, "action_id": action_id}

    def intended(self, action: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
        return dict(observation) == dict(action["intended_state"])
