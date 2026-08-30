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
    _herdr_interactive_ready,
    _public_descendant_locator,
    _session,
)
from .storage import Storage, StorageRefusal
from .visible_launch import CommandRunner, SubprocessRunner


class RolloverSnapshotAdapter(Protocol):
    def observe(self, descendants: list[dict[str, Any]]) -> list[dict[str, Any]]: ...


_EXPLICIT_ROUTE_FIELDS = ("name", "routing_name", "routing_alias")


def _explicit_routes(agent: Mapping[str, Any], locator: str) -> set[str]:
    """Return non-empty explicit routes; absent, null, and empty mean unset."""

    routes: set[str] = set()
    for field in _EXPLICIT_ROUTE_FIELDS:
        value = agent.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise StorageRefusal(
                "snapshot_refresh_live_mismatch",
                f"{locator}: Herdr explicit route evidence is malformed",
            )
        routes.add(value)
    return routes


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
        inventory = [dict(agent) for agent in agents]
        by_pane: dict[str, set[int]] = {}
        by_route: dict[str, set[int]] = {}
        by_session: dict[str, set[int]] = {}
        for index, agent in enumerate(inventory):
            for value, lookup in (
                (agent.get("pane_id"), by_pane),
                (_session(agent), by_session),
            ):
                if isinstance(value, str) and value:
                    lookup.setdefault(value, set()).add(index)
            for field in _EXPLICIT_ROUTE_FIELDS:
                value = agent.get(field)
                if isinstance(value, str) and value:
                    by_route.setdefault(value, set()).add(index)
        observations: list[dict[str, Any]] = []
        used_panes: set[str] = set()
        used_routes: set[str] = set()
        used_sessions: set[str] = set()
        for target in descendants:
            locator = _public_descendant_locator(target)
            pane = target.get("address")
            route = target.get("routing_name")
            adoption = target.get("route_adoption")
            if (
                (not isinstance(route, str) or not route)
                and isinstance(adoption, Mapping)
            ):
                route = adoption.get("routing_name")
            thread = target.get("thread_id")
            if (
                not isinstance(pane, str)
                or not pane
                or not isinstance(route, str)
                or not route
                or not isinstance(thread, str)
                or THREAD_UUID.fullmatch(thread) is None
            ):
                raise StorageRefusal(
                    "snapshot_refresh_live_mismatch",
                    f"{locator}: canonical Herdr identity is incomplete",
                )
            related_indexes = (
                by_pane.get(pane, set())
                | by_route.get(route, set())
                | by_session.get(thread, set())
            )
            related = [inventory[index] for index in sorted(related_indexes)]
            if len(related) > 1:
                raise StorageRefusal(
                    "snapshot_refresh_live_ambiguous",
                    f"{locator}: multiple Herdr endpoints overlap one descendant "
                    "identity",
                )
            if not related:
                raise StorageRefusal(
                    "snapshot_refresh_live_missing",
                    f"{locator}: frozen descendant is absent from Herdr inventory",
                )
            agent = related[0]
            status = agent.get("agent_status")
            if status in CLOSED_STATUSES:
                raise StorageRefusal(
                    "snapshot_refresh_live_closed",
                    f"{locator}: frozen descendant Herdr endpoint is closed",
                )
            worktree = Path(str(target.get("worktree", "")))
            terminal_id = agent.get("terminal_id")
            state_change_seq = agent.get("state_change_seq")
            explicit_routes = _explicit_routes(agent, locator)
            exact = (
                target.get("kind") == "codex-thread"
                and target.get("backend") == "herdr"
                and agent.get("agent") == "codex"
                and _herdr_interactive_ready(agent)
                and agent.get("pane_id") == pane
                and agent.get("name") == route
                and explicit_routes == {route}
                and _session(agent) == thread
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
                    f"{locator}: live Herdr identity or readiness differs",
                )
            if pane in used_panes or route in used_routes or thread in used_sessions:
                raise StorageRefusal(
                    "snapshot_refresh_live_ambiguous",
                    f"{locator}: one Herdr endpoint, route, or session overlaps "
                    "descendants",
                )
            used_panes.add(str(pane))
            used_routes.add(str(route))
            used_sessions.add(str(thread))
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
            self.adapter.observe,
        )
