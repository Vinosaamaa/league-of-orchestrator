"""Capability and opaque-identity types shared by runtime adapters.

The lifecycle core validates only the namespace envelope.  The adapter that
owns a namespace is solely responsible for interpreting its opaque value.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .storage_types import StorageRefusal


NAMESPACE = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")
MAX_OPAQUE_BYTES = 1024

HARNESS_CAPABILITIES = frozenset(
    {"create", "identify", "title", "prompt", "status", "hook", "interrupt", "resume", "exit"}
)
BACKEND_CAPABILITIES = frozenset({"allocate", "input", "inspect", "close"})
ADAPTER_CAPABILITIES = {
    "harness": HARNESS_CAPABILITIES,
    "backend": BACKEND_CAPABILITIES,
}
EVIDENCE_LEVELS = ("unverified", "inherited-contract", "isolated-double", "real-canary")
ADAPTER_AVAILABILITY = frozenset({"available", "contract-only"})


@dataclass(frozen=True)
class OpaqueIdentity:
    """A namespaced identity whose value is never parsed by League core."""

    namespace: str
    value: str

    def __post_init__(self) -> None:
        if not NAMESPACE.fullmatch(self.namespace):
            raise StorageRefusal("invalid_identity", "identity namespace is invalid")
        if not isinstance(self.value, str) or not self.value or self.value.strip() != self.value:
            raise StorageRefusal("invalid_identity", "opaque identity value is empty or not exact")
        encoded = self.value.encode("utf-8")
        if len(encoded) > MAX_OPAQUE_BYTES or any(ord(character) < 32 for character in self.value):
            raise StorageRefusal("invalid_identity", "opaque identity value is unsafe or too large")

    @property
    def encoded(self) -> str:
        return f"{self.namespace}:{self.value}"

    @classmethod
    def decode(cls, encoded: str) -> "OpaqueIdentity":
        if not isinstance(encoded, str) or ":" not in encoded:
            raise StorageRefusal("invalid_identity", "identity must be a namespaced opaque string")
        namespace, value = encoded.split(":", 1)
        return cls(namespace=namespace, value=value)


@dataclass(frozen=True)
class AdapterInstruction:
    """Harness-owned semantics transported by a backend adapter."""

    operation: str
    payload: Mapping[str, Any]

    def as_json(self) -> str:
        return json.dumps(dict(self.payload), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class RuntimeObservation:
    endpoint_identity: OpaqueIdentity
    state: str
    generation: str
    details: Mapping[str, Any]


@dataclass(frozen=True)
class AdapterReceipt:
    operation: str
    identity: OpaqueIdentity
    observed_state: str
    detail: Mapping[str, Any]


@dataclass(frozen=True)
class AdapterContract:
    kind: str
    category: str
    capabilities: frozenset[str]
    evidence: str
    availability: str
    notes: str

    def __post_init__(self) -> None:
        if not isinstance(self.category, str):
            raise StorageRefusal("adapter_contract_invalid", "adapter category is unsupported")
        allowed = ADAPTER_CAPABILITIES.get(self.category)
        if allowed is None:
            raise StorageRefusal("adapter_contract_invalid", "adapter category is unsupported")
        if not isinstance(self.kind, str) or not NAMESPACE.fullmatch(self.kind):
            raise StorageRefusal("adapter_contract_invalid", "adapter kind is invalid")
        if not isinstance(self.capabilities, frozenset) or not self.capabilities <= allowed:
            raise StorageRefusal("adapter_contract_invalid", "adapter capabilities conflict with its category")
        if not isinstance(self.evidence, str) or self.evidence not in EVIDENCE_LEVELS:
            raise StorageRefusal("adapter_contract_invalid", "adapter evidence label is unsupported")
        if not isinstance(self.availability, str) or self.availability not in ADAPTER_AVAILABILITY:
            raise StorageRefusal("adapter_contract_invalid", "adapter availability is unsupported")
        if not isinstance(self.notes, str) or not self.notes:
            raise StorageRefusal("adapter_contract_invalid", "adapter contract notes are required")

    def require(self, capability: str) -> None:
        if capability not in ADAPTER_CAPABILITIES[self.category]:
            raise StorageRefusal("unknown_capability", f"capability is not part of this adapter contract: {capability}")
        if capability not in self.capabilities:
            raise StorageRefusal(
                "unsupported_capability",
                f"adapter {self.kind} does not declare capability {capability}",
            )
