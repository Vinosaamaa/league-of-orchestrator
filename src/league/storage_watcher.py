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
        expected_watcher_id: str | None = None,
        expected_fence: int | None = None,
    ) -> dict[str, Any]: ...

    def supervisor_binding(self, callsign: Optional[str] = None) -> dict[str, Any]: ...

    def supervisor_bindings(
        self, *, limit: int = 64
    ) -> tuple[dict[str, Any], ...]: ...

    def resolve_supervisor_scope(
        self, actor_agent_id: str, callsign: Optional[str] = None
    ) -> dict[str, Any]: ...

    def supervision_owner(self, actor_agent_id: str) -> Optional[str]: ...

    def begin_shotcaller_turn(
        self, actor_agent_id: str, turn_token: str, at: str
    ) -> dict[str, Any]: ...

    def commit_shotcaller_turn(
        self, actor_agent_id: str, turn_token: str, at: str
    ) -> dict[str, Any]: ...

    def abort_shotcaller_turn(
        self, actor_agent_id: str, turn_token: str, at: str
    ) -> dict[str, Any]: ...

    def watcher_registration(
        self, actor_agent_id: str
    ) -> Optional[dict[str, Any]]: ...

    def watcher_registrations(
        self, actor_agent_ids: tuple[str, ...], *, limit: int = 64
    ) -> dict[str, dict[str, Any]]: ...

    def watcher_readiness(
        self, actor_agent_id: str
    ) -> Optional[dict[str, Any]]: ...

    def supervision_policy(self, actor_agent_id: str) -> dict[str, Any]: ...

    def runtime_monitor_candidates(
        self, owner_agent_id: str, *, limit: int = 50
    ) -> dict[str, Any]: ...

    def record_supervision_fault(
        self,
        owner_agent_id: str,
        fault_kind: str,
        fault_key: str,
        at: str,
    ) -> dict[str, Any]: ...

    def configure_supervision_policy(
        self,
        scope_id: str,
        actor_agent_id: str,
        mode: str,
        unreachable_grace_seconds: int,
        at: str,
    ) -> dict[str, Any]: ...

    def set_supervision_attachment(
        self,
        scope_id: str,
        actor_agent_id: str,
        mode: str,
        at: str,
        *,
        expected_watcher_id: Optional[str] = None,
        expected_fence: Optional[int] = None,
    ) -> dict[str, Any]: ...

    def apply_supervision_delivery_policy(
        self,
        outbox_id: str,
        event_id: str,
        recipient_agent_id: str,
        at: str,
    ) -> dict[str, Any]: ...

    def silent_supervision_updates(
        self,
        actor_agent_id: str,
        *,
        after_event_seq: Optional[int] = None,
        limit: int = 20,
        advance_cursor: bool = False,
        at: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def pause_calm_supervision(
        self,
        actor_agent_id: str,
        watcher_id: str,
        fence: int,
        at: str,
    ) -> dict[str, Any]: ...

    def resume_calm_supervision(
        self,
        actor_agent_id: str,
        watcher_id: str,
        fence: int,
        at: str,
    ) -> dict[str, Any]: ...

    def champion_stop_decision(
        self, champion_agent_id: str, terminal_generation: str, at: str
    ) -> dict[str, Any]: ...

    def release_watcher(
        self,
        watcher_id: str,
        actor_agent_id: str,
        fence: int,
        at: str,
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

    def prepare_owner_stop_control(
        self,
        actor_agent_id: str,
        control_id: str,
        prompt_id: str,
        interrupt_delegates: bool,
        at: str,
    ) -> dict[str, Any]: ...

    def finalize_owner_stop_control(
        self, actor_agent_id: str, control_id: str, at: str
    ) -> dict[str, Any]: ...

    def fail_owner_stop_control(
        self, actor_agent_id: str, control_id: str, reason: str, at: str
    ) -> dict[str, Any]: ...

    def stop_decision(
        self,
        scope_id: str,
        actor_agent_id: str,
        terminal_generation: str,
        at: str,
        *,
        block_on_fresh_terminal: bool = False,
    ) -> dict[str, Any]: ...
