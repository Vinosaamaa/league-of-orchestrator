"""Adapter-backed refresh of one expired switched-rollover snapshot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from .rollover_descendant import (
    CLOSED_STATUSES,
    LIVE_STATUSES,
    THREAD_UUID,
    _herdr_runtime_generation,
    _session,
)
from .storage import Storage, StorageRefusal
from .visible_launch import CommandRunner, SubprocessRunner


class RolloverSnapshotAdapter(Protocol):
    def observe(self, descendants: list[dict[str, Any]]) -> list[dict[str, Any]]: ...


class HerdrRolloverSnapshotAdapter:
    """Verify every descendant against one bounded Herdr inventory read."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or SubprocessRunner()

    def observe(self, descendants: list[dict[str, Any]]) -> list[dict[str, Any]]:
        completed = self.runner.run(("herdr", "agent", "list"), timeout_seconds=30)
        try:
            envelope = json.loads(completed.stdout)
            agents = envelope["result"]["agents"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise StorageRefusal(
                "snapshot_refresh_live_unverified", "Herdr agent inventory is malformed"
            ) from exc
        if completed.returncode != 0 or not isinstance(agents, list) or any(
            not isinstance(item, Mapping) for item in agents
        ):
            raise StorageRefusal(
                "snapshot_refresh_live_unverified",
                "Herdr agent inventory refused snapshot refresh verification",
            )
        observations: list[dict[str, Any]] = []
        used_panes: set[str] = set()
        for target in descendants:
            pane = target.get("address")
            route = target.get("routing_name")
            thread = target.get("thread_id")
            related = [
                dict(agent)
                for agent in agents
                if agent.get("pane_id") == pane
                or agent.get("name") == route
                or _session(agent) == thread
            ]
            if len(related) > 1:
                raise StorageRefusal(
                    "snapshot_refresh_live_ambiguous",
                    "multiple Herdr endpoints overlap one descendant identity",
                )
            if not related:
                raise StorageRefusal(
                    "snapshot_refresh_live_missing",
                    "a frozen descendant is absent from Herdr inventory",
                )
            agent = related[0]
            status = agent.get("agent_status")
            if status in CLOSED_STATUSES:
                raise StorageRefusal(
                    "snapshot_refresh_live_closed",
                    "a frozen descendant Herdr endpoint is closed",
                )
            worktree = Path(str(target.get("worktree", "")))
            terminal_id = agent.get("terminal_id")
            state_change_seq = agent.get("state_change_seq")
            exact = (
                target.get("kind") == "codex-thread"
                and target.get("backend") == "herdr"
                and agent.get("agent") == "codex"
                and agent.get("interactive_ready") is True
                and agent.get("pane_id") == pane
                and agent.get("name") == route
                and _session(agent) == thread
                and isinstance(thread, str)
                and THREAD_UUID.fullmatch(thread) is not None
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
                    "snapshot_refresh_live_mismatch",
                    "Herdr endpoint, route, thread, terminal, or worktree differs",
                )
            if pane in used_panes:
                raise StorageRefusal(
                    "snapshot_refresh_live_ambiguous",
                    "one Herdr endpoint overlaps multiple descendants",
                )
            used_panes.add(str(pane))
            observations.append(
                {
                    "schema": "league.rollover-snapshot-observation.v1",
                    "verified": True,
                    "champion_agent_id": target["champion_agent_id"],
                    "task_id": target["task_id"],
                    "callsign": target["callsign"],
                    "thread_id": thread,
                    "endpoint": pane,
                    "routing_name": route,
                    "worktree": str(worktree.resolve()),
                    "terminal_id": terminal_id,
                    "state_change_seq": state_change_seq,
                    "runtime_generation": _herdr_runtime_generation(
                        terminal_id, thread
                    ),
                    "status": "idle" if status in {"done", "idle"} else "active",
                    "canonical_row_digest": target["canonical_row_digest"],
                }
            )
        return observations


class RolloverSnapshotRefreshService:
    def __init__(self, store: Storage, adapter: RolloverSnapshotAdapter) -> None:
        self.store = store
        self.adapter = adapter

    def refresh(
        self,
        *,
        operation_id: str,
        refresh_id: str,
        squad_id: str,
        predecessor_agent_id: str,
        successor_agent_id: str,
        expected_rollover_version: int,
        expected_snapshot_version: int,
        expected_snapshot_digest: str,
        expires_at: str,
        at: str,
    ) -> dict[str, Any]:
        target = self.store.rollover_snapshot_refresh_target(
            operation_id,
            refresh_id,
            squad_id,
            predecessor_agent_id,
            successor_agent_id,
            expected_rollover_version,
            expected_snapshot_version,
            expected_snapshot_digest,
            expires_at,
            at,
        )
        observations = (
            [] if target["refreshed"] else self.adapter.observe(target["descendants"])
        )
        return self.store.refresh_rollover_snapshot(
            operation_id,
            refresh_id,
            squad_id,
            predecessor_agent_id,
            successor_agent_id,
            expected_rollover_version,
            expected_snapshot_version,
            expected_snapshot_digest,
            expires_at,
            at,
            target.get("canonical_digest", ""),
            observations,
        )
