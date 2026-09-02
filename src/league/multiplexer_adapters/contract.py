"""Provider-neutral multiplexer lifecycle contract."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from typing import Any, Mapping, Protocol, Sequence


MULTIPLEXER_OPERATIONS = frozenset(
    {
        "server_generation",
        "calling_context",
        "discover",
        "routing",
        "placement",
        "metadata",
        "title",
        "focus",
        "delivery",
        "steering_delivery",
        "close",
        "visible_launch",
        "shotcaller_bootstrap",
        "rollover_reconciliation",
        "production_cleanup",
        "provider_session_lifecycle",
        "runtime_replacement",
    }
)
MULTIPLEXER_OPERATION_METHODS = {
    "server_generation": ("server_generation",),
    "calling_context": ("calling_context",),
    "discover": ("discover", "endpoint", "inspect_restored", "runtime_generation"),
    "routing": ("routing",),
    "placement": ("placement",),
    "metadata": ("metadata",),
    "title": ("title",),
    "focus": ("focus",),
    "delivery": ("delivery",),
    "steering_delivery": ("steering_delivery",),
    "close": ("close",),
    "visible_launch": ("visible_launch_driver",),
    "shotcaller_bootstrap": ("shotcaller_bootstrap_driver",),
    "rollover_reconciliation": (
        "rollover_snapshot_driver",
        "rollover_descendant_driver",
    ),
    "production_cleanup": ("cleanup_drivers",),
    "provider_session_lifecycle": (
        "resume_provider_session",
        "migrate_provider_session",
    ),
    "runtime_replacement": (
        "replacement_recover",
        "replacement_verify",
        "replacement_route_swap",
        "replacement_route_rollback",
        "replacement_retire",
    ),
}


@dataclass(frozen=True)
class RestoredEndpoint:
    descriptor_id: str
    workspace_id: str
    tab_id: str
    pane_id: str
    terminal_id: str


class MultiplexerAdapter(Protocol):
    kind: str
    capabilities: frozenset[str]

    def server_generation(self) -> str: ...

    def calling_context(self) -> Mapping[str, str]: ...

    def discover(self) -> list[Mapping[str, Any]]: ...

    def endpoint(
        self, descriptor_id: str, item: Mapping[str, Any]
    ) -> RestoredEndpoint: ...

    def inspect_restored(
        self,
        descriptor: Mapping[str, Any],
        endpoint: RestoredEndpoint,
    ) -> dict[str, Any]: ...

    def runtime_generation(
        self, item: Mapping[str, Any], session_ref: str
    ) -> str: ...

    def routing(
        self,
        descriptor: Mapping[str, Any],
        endpoint: RestoredEndpoint,
    ) -> dict[str, Any]: ...

    def metadata(
        self,
        presentation: Mapping[str, Any],
        endpoint: RestoredEndpoint,
        first_sequence: int,
    ) -> dict[str, Any]: ...

    def placement(self, specification: Mapping[str, Any]) -> RestoredEndpoint: ...

    def title(self, endpoint: RestoredEndpoint, title: str) -> dict[str, Any]: ...

    def focus(self, endpoint: RestoredEndpoint) -> dict[str, Any]: ...

    def delivery(
        self, target: str, body: str, *, wait: bool = False
    ) -> dict[str, Any]: ...

    def steering_delivery(self, **inputs: Any) -> Any: ...

    def close(
        self, endpoint: RestoredEndpoint, *, placement: str = "pane"
    ) -> dict[str, Any]: ...

    def visible_launch_driver(self, agent_kind: str, **inputs: Any) -> Any: ...

    def shotcaller_bootstrap_driver(self, options: Any) -> Any: ...

    def rollover_snapshot_driver(self) -> Any: ...

    def rollover_descendant_driver(self) -> Any: ...

    def cleanup_drivers(self, **inputs: Any) -> tuple[Any, Any]: ...

    def resume_provider_session(self, **inputs: Any) -> Mapping[str, Any]: ...

    def migrate_provider_session(self, **inputs: Any) -> Mapping[str, Any]: ...

    def replacement_recover(self, **inputs: Any) -> Mapping[str, Any] | None: ...

    def replacement_verify(self, **inputs: Any) -> Mapping[str, Any]: ...

    def replacement_route_swap(self, **inputs: Any) -> Mapping[str, Any]: ...

    def replacement_route_rollback(self, **inputs: Any) -> Mapping[str, Any]: ...

    def replacement_retire(self, **inputs: Any) -> Mapping[str, Any]: ...


class CommandRunner(Protocol):
    def run(
        self, arguments: Sequence[str], timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]: ...


__all__ = [
    "CommandRunner", "MULTIPLEXER_OPERATIONS", "MULTIPLEXER_OPERATION_METHODS",
    "MultiplexerAdapter", "RestoredEndpoint"
]
