"""Exact live Herdr verification for imported rollover descendants."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Protocol

from .adapter_types import OpaqueIdentity
from .agent_adapters import adapter_kind_from_runtime, builtin_agent_adapter_registry
from .multiplexer_adapters import builtin_multiplexer_adapter_registry
from .storage import Storage, StorageRefusal
from .visible_launch import CommandRunner, SubprocessRunner


LIVE_STATUSES = {"active", "blocked", "done", "idle", "waiting", "working"}
CLOSED_STATUSES = {"closed", "completed", "failed", "stopped"}


def _herdr_runtime_generation(terminal_id: str, thread_id: str) -> str:
    return "herdr:" + hashlib.sha256(
        f"{terminal_id}\0{thread_id}".encode("utf-8")
    ).hexdigest()[:24]


class DescendantRuntimeAdapter(Protocol):
    def verify(
        self, target: Mapping[str, Any], runtime_instance_id: str
    ) -> dict[str, Any]: ...


def _session(agent: Mapping[str, Any]) -> str | None:
    value = agent.get("agent_session")
    if isinstance(value, Mapping):
        value = value.get("value")
    return value if isinstance(value, str) else None


def _herdr_interactive_ready(agent: Mapping[str, Any]) -> bool:
    """Honor affirmative readiness or legacy settled-state readiness only."""

    if "interactive_ready" in agent:
        return agent.get("interactive_ready") is True
    return agent.get("agent_status") in {"done", "idle"}


def _public_descendant_locator(target: Mapping[str, Any]) -> str:
    callsign = target.get("callsign")
    if (
        isinstance(callsign, str)
        and 1 <= len(callsign) <= 64
        and callsign[0].isalpha()
        and all(
            character.isalnum() or character in {"_", "-"}
            for character in callsign
        )
    ):
        return callsign
    opaque = hashlib.sha256(
        str(target.get("champion_agent_id", "unknown")).encode("utf-8")
    ).hexdigest()[:12]
    return f"descendant-{opaque}"


class HerdrDescendantRuntimeAdapter:
    """Observe one already-running Champion without creating or changing layout."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or SubprocessRunner()
        self.multiplexer = builtin_multiplexer_adapter_registry(
            herdr_runner=self.runner, herdr_binary="herdr"
        ).adapter("herdr")

    def verify(
        self, target: Mapping[str, Any], runtime_instance_id: str
    ) -> dict[str, Any]:
        try:
            agents = self.multiplexer.discover()
        except StorageRefusal as exc:
            raise StorageRefusal(
                "descendant_runtime_unverified", "Herdr agent inventory is malformed"
            ) from exc
        expected_pane = target.get("address")
        expected_route = target.get("routing_name")
        expected_thread = target.get("thread_id")
        locator = _public_descendant_locator(target)
        related = [
            dict(agent)
            for agent in agents
            if agent.get("pane_id") == expected_pane
            or agent.get("name") == expected_route
            or _session(agent) == expected_thread
        ]
        if len(related) > 1:
            raise StorageRefusal(
                "descendant_runtime_ambiguous",
                f"{locator}: multiple Herdr endpoints overlap the frozen "
                "Champion identity",
            )
        if not related:
            raise StorageRefusal(
                "descendant_runtime_missing",
                f"{locator}: frozen Champion is absent from Herdr inventory",
            )
        agent = related[0]
        status = agent.get("agent_status")
        if status in CLOSED_STATUSES:
            raise StorageRefusal(
                "descendant_runtime_closed",
                f"{locator}: frozen Champion Herdr endpoint is closed",
            )
        worktree = Path(str(target.get("worktree", "")))
        terminal_id = agent.get("terminal_id")
        state_change_seq = agent.get("state_change_seq")
        try:
            adapter_kind = adapter_kind_from_runtime(str(target.get("kind", "")))
            agent_adapter = builtin_agent_adapter_registry().adapter(adapter_kind)
            agent_adapter.contract.require("identify")
            OpaqueIdentity(adapter_kind, str(expected_thread))
        except StorageRefusal as exc:
            raise StorageRefusal(
                "descendant_runtime_mismatch",
                f"{locator}: canonical agent adapter identity is unsupported",
            ) from exc
        exact = (
            target.get("kind") == agent_adapter.launch_profile.runtime_kind
            and target.get("backend") == "herdr"
            and agent.get("agent") == adapter_kind
            and _herdr_interactive_ready(agent)
            and agent.get("pane_id") == expected_pane
            and agent.get("name") == expected_route
            and _session(agent) == expected_thread
            and isinstance(expected_thread, str)
            and bool(expected_thread)
            and worktree.is_absolute()
            and worktree.is_dir()
            and not worktree.is_symlink()
            and agent.get("cwd") == str(worktree.resolve())
            and agent.get("foreground_cwd") == str(worktree.resolve())
            and isinstance(terminal_id, str)
            and bool(terminal_id)
            and type(state_change_seq) is int
            and state_change_seq >= 0
            and status in LIVE_STATUSES
        )
        if not exact:
            raise StorageRefusal(
                "descendant_runtime_mismatch",
                f"{locator}: live Herdr identity or readiness differs from the "
                "frozen Champion",
            )
        runtime_status = "idle" if status in {"done", "idle"} else "active"
        generation = _herdr_runtime_generation(terminal_id, expected_thread)
        return {
            "schema": "league.rollover-descendant-runtime.v1",
            "verified": True,
            "champion_agent_id": target["champion_agent_id"],
            "task_id": target["task_id"],
            "runtime_instance_id": runtime_instance_id,
            "harness_kind": agent_adapter.launch_profile.runtime_kind,
            "backend_kind": "herdr",
            "session_ref": expected_thread,
            "endpoint": expected_pane,
            "runtime_generation": generation,
            "status": runtime_status,
            "callsign": target["callsign"],
            "routing_name": expected_route,
            "display_agent": target["display_agent"],
            "worktree": str(worktree.resolve()),
            "terminal_id": terminal_id,
            "state_change_seq": state_change_seq,
            "snapshot_row_digest": target["snapshot_row_digest"],
            "capabilities": list(target["capabilities"]),
        }


class RolloverDescendantService:
    def __init__(self, store: Storage, adapter: DescendantRuntimeAdapter) -> None:
        self.store = store
        self.adapter = adapter

    def reconcile(
        self,
        *,
        operation_id: str,
        reconciliation_id: str,
        champion_agent_id: str,
        task_id: str,
        runtime_instance_id: str,
        snapshot_digest: str,
        snapshot_row_digest: str,
        expected_rollover_version: int,
        expected_agent_version: int,
        expected_task_version: int,
        expected_assignment_version: int,
        expected_callsign_assignment_version: int,
        pending_outbox_ids: tuple[str, ...],
        at: str,
    ) -> dict[str, Any]:
        target = self.store.rollover_descendant_target(
            operation_id,
            reconciliation_id,
            champion_agent_id,
            task_id,
            snapshot_digest,
            snapshot_row_digest,
            expected_rollover_version,
            expected_agent_version,
            expected_task_version,
            expected_assignment_version,
            expected_callsign_assignment_version,
        )
        receipt = (
            None
            if target.get("reconciled") is True
            else self.adapter.verify(target, runtime_instance_id)
        )
        return self.store.reconcile_rollover_descendant(
            operation_id,
            reconciliation_id,
            champion_agent_id,
            task_id,
            runtime_instance_id,
            snapshot_digest,
            snapshot_row_digest,
            expected_rollover_version,
            expected_agent_version,
            expected_task_version,
            expected_assignment_version,
            expected_callsign_assignment_version,
            receipt,
            pending_outbox_ids,
            at,
        )
