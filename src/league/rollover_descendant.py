"""Exact live Herdr verification for imported rollover descendants."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Protocol

from .storage import Storage, StorageRefusal
from .visible_launch import CommandRunner, SubprocessRunner


THREAD_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
LIVE_STATUSES = {"active", "blocked", "idle", "waiting", "working"}
CLOSED_STATUSES = {"closed", "completed", "done", "failed", "stopped"}


class DescendantRuntimeAdapter(Protocol):
    def verify(
        self, target: Mapping[str, Any], runtime_instance_id: str
    ) -> dict[str, Any]: ...


def _session(agent: Mapping[str, Any]) -> str | None:
    value = agent.get("agent_session")
    if isinstance(value, Mapping):
        value = value.get("value")
    return value if isinstance(value, str) else None


class HerdrDescendantRuntimeAdapter:
    """Observe one already-running Champion without creating or changing layout."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or SubprocessRunner()

    def verify(
        self, target: Mapping[str, Any], runtime_instance_id: str
    ) -> dict[str, Any]:
        completed = self.runner.run(("herdr", "agent", "list"), timeout_seconds=30)
        try:
            envelope = json.loads(completed.stdout)
            result = envelope["result"]
            agents = result["agents"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise StorageRefusal(
                "descendant_runtime_unverified", "Herdr agent inventory is malformed"
            ) from exc
        if completed.returncode != 0 or not isinstance(agents, list) or any(
            not isinstance(item, Mapping) for item in agents
        ):
            raise StorageRefusal(
                "descendant_runtime_unverified", "Herdr agent inventory refused verification"
            )
        expected_pane = target.get("address")
        expected_route = target.get("routing_name")
        expected_thread = target.get("thread_id")
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
                "multiple Herdr endpoints overlap the frozen Champion identity",
            )
        if not related:
            raise StorageRefusal(
                "descendant_runtime_missing", "frozen Champion is absent from Herdr inventory"
            )
        agent = related[0]
        status = agent.get("agent_status")
        if status in CLOSED_STATUSES:
            raise StorageRefusal(
                "descendant_runtime_closed", "frozen Champion Herdr endpoint is closed"
            )
        worktree = Path(str(target.get("worktree", "")))
        terminal_id = agent.get("terminal_id")
        state_change_seq = agent.get("state_change_seq")
        exact = (
            target.get("kind") == "codex-thread"
            and target.get("backend") == "herdr"
            and agent.get("agent") == "codex"
            and agent.get("pane_id") == expected_pane
            and agent.get("name") == expected_route
            and _session(agent) == expected_thread
            and isinstance(expected_thread, str)
            and THREAD_UUID.fullmatch(expected_thread) is not None
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
                "Herdr endpoint, route, thread, terminal, or worktree differs from the frozen Champion",
            )
        runtime_status = "idle" if status == "idle" else "active"
        generation = "herdr:" + hashlib.sha256(
            f"{terminal_id}\0{expected_thread}".encode("utf-8")
        ).hexdigest()[:24]
        return {
            "schema": "league.rollover-descendant-runtime.v1",
            "verified": True,
            "champion_agent_id": target["champion_agent_id"],
            "task_id": target["task_id"],
            "runtime_instance_id": runtime_instance_id,
            "harness_kind": "codex-thread",
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
