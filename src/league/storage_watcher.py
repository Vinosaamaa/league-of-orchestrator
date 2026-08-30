"""Runtime, watcher, and Stop-hook portion of the stable storage contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class RuntimeRegistrationCommand:
    runtime_instance_id: str
    actor_agent_id: str
    harness_kind: str
    backend_kind: str
    session_ref: str
    endpoint: str
    runtime_generation: str
    status: str
    verified: bool
    at: str
    capabilities: Optional[tuple[str, ...]] = None


class WatcherStorage(Protocol):
    def register_runtime(self, command: RuntimeRegistrationCommand) -> dict[str, Any]: ...
    def register_watcher(
        self,
        scope_id: str,
        watcher_id: str,
        actor_agent_id: str,
        runtime_instance_id: str,
        wake_locator: str,
        leased_until: str,
        fence: int,
        at: str,
        *,
        block_on_obligations: bool = True,
    ) -> dict[str, Any]: ...

    def note_user_message(
        self, scope_id: str, actor_agent_id: str, at: str
    ) -> dict[str, Any]: ...

    def consume_stop_feedback(
        self,
        scope_id: str,
        actor_agent_id: str,
        terminal_generation: str,
        body: str,
    ) -> bool: ...

    def rearm_wait(
        self, scope_id: str, actor_agent_id: str, event_id: str, at: str
    ) -> dict[str, Any]: ...

    def set_allow_stop_once(self, scope_id: str, actor_agent_id: str) -> dict[str, Any]: ...

    def stop_decision(
        self,
        scope_id: str,
        actor_agent_id: str,
        terminal_generation: str,
        at: str,
        *,
        block_on_fresh_terminal: bool = False,
    ) -> dict[str, Any]: ...
