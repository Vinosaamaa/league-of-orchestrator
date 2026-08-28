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
    capabilities: frozenset[str]
    evidence: str
    notes: str

    def require(self, capability: str, allowed: frozenset[str]) -> None:
        if capability not in allowed:
            raise StorageRefusal("unknown_capability", f"capability is not part of this adapter contract: {capability}")
        if capability not in self.capabilities:
            raise StorageRefusal(
                "unsupported_capability",
                f"adapter {self.kind} does not declare capability {capability}",
            )
