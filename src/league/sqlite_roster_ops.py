"""One-transaction bounded read-only project-grouped Roster snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from .sqlite_project_ops import (
    _alias_map,
    _project_value,
    _squad_map,
    _squad_unavailable_reason,
)
from .storage_types import StorageRefusal


MAX_ROSTER_ITEMS = 500
TERMINAL_STATES = {
    "ready_to_land",
    "completed",
    "complete",
    "rejected",
    "failed",
    "cancelled",
    "canceled",
}
NEEDS_ACTION_STATES = {"blocked", "failed", "rejected", "awaiting_user", "awaiting_requester"}


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("invalid_roster_window", f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("invalid_roster_window", f"{label} must include an offset")
    return parsed


def _link(table: str, key_name: str, value: str, version: int | None = None) -> dict[str, Any]:
    locator = f"league://{table}/{quote(value, safe='')}"
    if version is not None:
        locator = f"{locator}?version={version}"
    result: dict[str, Any] = {
        "table": table,
        "key": {key_name: value},
        "locator": locator,
    }
    if version is not None:
        result["version"] = version
    return result


def _private(value: Any, visibility: str) -> Any:
    if value is None:
        return None
    return value if visibility == "local" else "[redacted]"


def _latest_events(
    store: Any, column: str, identities: list[str], as_of: str
) -> dict[str, dict[str, Any]]:
    if not identities:
        return {}
    placeholders = ",".join("?" for _ in identities)
    rows = store.connection.execute(
        f"""
        SELECT event_id,entity_id,event_type,status,update_text,occurred_at,event_seq
          FROM (
            SELECT event_id,{column} entity_id,event_type,status,update_text,occurred_at,
                   event_seq,ROW_NUMBER() OVER (
                     PARTITION BY {column} ORDER BY event_seq DESC,event_id DESC
                   ) ordinal
              FROM events
             WHERE {column} IN ({placeholders})
               AND julianday(occurred_at)<=julianday(?)
          )
         WHERE ordinal=1 ORDER BY entity_id
        """,
        (*identities, as_of),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result.setdefault(str(row["entity_id"]), dict(row))
    return result


def _latest_task_transitions(
    store: Any, identities: list[str], as_of: str
) -> dict[str, dict[str, Any]]:
    if not identities:
        return {}
    placeholders = ",".join("?" for _ in identities)
    rows = store.connection.execute(
        f"""
        SELECT task_id,transition_id,event_id,update_text,next_action,blocker,created_at
          FROM (
            SELECT task_id,transition_id,event_id,update_text,next_action,blocker,created_at,
                   ROW_NUMBER() OVER (
                     PARTITION BY task_id ORDER BY created_at DESC,transition_id DESC
                   ) ordinal
              FROM task_transitions
             WHERE task_id IN ({placeholders})
               AND julianday(created_at)<=julianday(?)
          )
         WHERE ordinal=1 ORDER BY task_id
        """,
        (*identities, as_of),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result.setdefault(str(row["task_id"]), dict(row))
    return result


def _classify(
    primary_state: str,
    related_states: set[str],
    updated: datetime,
    recent_time: datetime,
    stale_time: datetime,
    *,
    unresolved: bool,
) -> tuple[str | None, bool]:
    terminal = primary_state in TERMINAL_STATES
    stale = updated < stale_time and not terminal
    if {primary_state, *related_states} & NEEDS_ACTION_STATES or stale:
        return "needs_action", stale
    if terminal:
        return ("recently_finished" if updated >= recent_time else None), False
    return ("unresolved" if unresolved else "underway"), stale


def _agent_item(row: Any, event: Any, visibility: str, stale: bool) -> dict[str, Any]:
    agent_id = str(row["agent_id"])
    evidence = [_link("agent_instances", "agent_id", agent_id, int(row["version"]))]
    if event is not None:
        evidence.append(_link("events", "event_id", str(event["event_id"])))
    return {
        "kind": "agent",
        "agent_id": agent_id,
        "callsign": row["callsign"],
        "role": row["role"],
        "status": row["status"],
        "updated_at": row["updated_at"],
        "stale": stale,
        "update": _private(row["update_text"], visibility),
        "blocker": _private(row["blocker"], visibility),
        "next_action": _private(row["next_action"], visibility),
        "evidence_links": evidence,
    }


def _request_item(row: Any, visibility: str, stale: bool) -> dict[str, Any]:
    request_id = str(row["request_id"])
    evidence = [_link("requests", "request_id", request_id, int(row["version"]))]
    if row["last_route_event_id"] is not None:
        evidence.append(_link("events", "event_id", str(row["last_route_event_id"])))
    return {
        "kind": "request",
        "request_id": request_id,
        "summary": _private(row["summary"], visibility),
        "status": row["state"],
        "updated_at": row["updated_at"],
        "stale": stale,
        "owner_agent_id": row["owner_agent_id"],
        "return_to_agent_id": row["return_to_agent_id"],
        "evidence_links": evidence,
    }


def roster_snapshot(
    store: Any,
    *,
    as_of: str,
    recent_since: str,
    stale_before: str,
    limit: int = 500,
    visibility: str = "outbound",
) -> dict[str, Any]:
    if visibility not in {"local", "outbound"}:
        raise StorageRefusal("invalid_visibility", "visibility must be local or outbound")
    if not 1 <= limit <= MAX_ROSTER_ITEMS:
        raise StorageRefusal("invalid_limit", f"Roster limit must be between 1 and {MAX_ROSTER_ITEMS}")
    as_of_time = _time(as_of, "snapshot time")
    recent_time = _time(recent_since, "recent boundary")
    stale_time = _time(stale_before, "stale boundary")
    if recent_time > as_of_time or stale_time > as_of_time:
        raise StorageRefusal("invalid_roster_window", "Roster boundaries cannot be after the snapshot time")

    with store._read_transaction():
        project_rows = store.connection.execute(
            """
            SELECT * FROM projects
             WHERE julianday(updated_at)<=julianday(?)
             ORDER BY project_id LIMIT ?
            """,
            (as_of, MAX_ROSTER_ITEMS + 1),
        ).fetchall()
        task_rows = store.connection.execute(
            """
            SELECT * FROM tasks
             WHERE julianday(updated_at)<=julianday(?)
               AND NOT EXISTS (
                 SELECT 1 FROM task_assignments ta
                  WHERE ta.task_id=tasks.task_id AND ta.assignment_role='hidden-worker'
               )
             ORDER BY updated_at DESC,task_id LIMIT ?
            """,
            (as_of, limit + 1),
        ).fetchall()
        agent_rows = store.connection.execute(
            """
            SELECT agent_id,callsign,role,shotcaller_agent_id,task_id,status,version,
                   updated_at,update_text,blocker,next_action,retired_at
              FROM agent_instances
             WHERE retired_at IS NULL AND role<>'hidden-worker'
               AND julianday(updated_at)<=julianday(?)
             ORDER BY updated_at DESC,agent_id LIMIT ?
            """,
            (as_of, limit + 1),
        ).fetchall()
        request_rows = store.connection.execute(
            """
            SELECT r.request_id,r.summary,r.state,r.version,r.updated_at,r.owner_agent_id,
                   r.return_to_agent_id,r.last_route_event_id,r.resolution_summary,t.task_id,t.project_id
              FROM requests r LEFT JOIN tasks t
                ON t.request_id=r.request_id
               AND julianday(t.updated_at)<=julianday(?)
             WHERE r.state NOT IN ('answered','cancelled')
               AND julianday(r.updated_at)<=julianday(?)
             ORDER BY r.updated_at DESC,r.request_id LIMIT ?
            """,
            (as_of, as_of, limit + 1),
        ).fetchall()
        squad_rows = store.connection.execute(
            """
            SELECT s.squad_id,s.state squad_state,s.version,s.updated_at,
                   a.agent_id shotcaller_agent_id,
                   a.callsign shotcaller,a.status shotcaller_status,a.retired_at
              FROM squads s JOIN agent_instances a ON a.agent_id=s.shotcaller_agent_id
             WHERE julianday(s.updated_at)<=julianday(?)
               AND julianday(a.updated_at)<=julianday(?)
             ORDER BY s.squad_id LIMIT ?
            """,
            (as_of, as_of, MAX_ROSTER_ITEMS + 1),
        ).fetchall()
        task_sample = list(task_rows[:limit])
        agent_sample = list(agent_rows[:limit])
        task_ids = [str(row["task_id"]) for row in task_sample]
        task_events = _latest_events(store, "task_id", task_ids, as_of)
        task_transitions = _latest_task_transitions(
            store, task_ids, as_of
        )
        agent_events = _latest_events(
            store, "agent_id", [str(row["agent_id"]) for row in agent_sample], as_of
        )
        linked_request_rows: list[Any] = []
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            linked_request_rows = list(
                store.connection.execute(
                    f"""
                    SELECT r.request_id,r.summary,r.state,r.version,r.updated_at,
                           r.owner_agent_id,r.return_to_agent_id,r.last_route_event_id,
                           r.resolution_summary,t.task_id,t.project_id
                      FROM tasks t JOIN requests r ON r.request_id=t.request_id
                     WHERE t.task_id IN ({placeholders})
                       AND julianday(r.updated_at)<=julianday(?)
                     ORDER BY t.task_id,r.request_id
                    """,
                    (*task_ids, as_of),
                ).fetchall()
            )
        transition_limit = min(limit, 100)
        recent_event_rows = store.connection.execute(
            """
            SELECT event_id,event_type,status,update_text,occurred_at,event_seq
             FROM events
             WHERE julianday(occurred_at)<=julianday(?)
               AND NOT (
                 aggregate_kind='assignment' AND aggregate_id IN (
                   SELECT task_assignment_id FROM task_assignments
                    WHERE assignment_role='hidden-worker'
                 )
               )
             ORDER BY event_seq DESC,event_id DESC LIMIT ?
            """,
            (as_of, transition_limit + 1),
        ).fetchall()

        project_sample = list(project_rows[:MAX_ROSTER_ITEMS])
        project_ids = [str(row["project_id"]) for row in project_sample]
        aliases = _alias_map(store, project_ids)
        suggestions = _squad_map(store, project_ids)
        projects: dict[str, dict[str, Any]] = {}
        for row in project_sample:
            project_id = str(row["project_id"])
            project = _project_value(
                store, row, visibility, aliases=aliases[project_id]
            )
            project["suggested_squads"] = suggestions[project_id]
            projects[project_id] = {
                "project": project,
                "groups": {
                    "needs_action": [],
                    "recently_finished": [],
                    "underway": [],
                    "unresolved": [],
                },
            }
        unresolved_project = {
            "project": None,
            "project_key": "unresolved-project",
            "groups": {
                "needs_action": [],
                "recently_finished": [],
                "underway": [],
                "unresolved": [],
            },
        }

        agents_by_task: dict[str, list[Any]] = {}
        for row in agent_sample:
            if row["task_id"] is not None:
                agents_by_task.setdefault(str(row["task_id"]), []).append(row)
        request_by_task = {
            str(row["task_id"]): row
            for row in linked_request_rows
            if row["task_id"] is not None
        }
        represented_agents: set[str] = set()
        represented_requests: set[str] = set()
        items: list[tuple[str | None, str, dict[str, Any]]] = []

        for row in task_sample:
            task_id = str(row["task_id"])
            associated = agents_by_task.get(task_id, [])
            represented_agents.update(str(agent["agent_id"]) for agent in associated)
            request = request_by_task.get(task_id)
            if request is not None:
                represented_requests.add(str(request["request_id"]))
            updated = _time(str(row["updated_at"]), "task update")
            related_states = {str(agent["status"]) for agent in associated}
            if request is not None:
                related_states.add(str(request["state"]))
            category, stale = _classify(
                str(row["state"]),
                related_states,
                updated,
                recent_time,
                stale_time,
                unresolved=row["project_id"] is None,
            )
            if category is None:
                continue
            evidence = [_link("tasks", "task_id", task_id, int(row["version"]))]
            if request is not None:
                evidence.append(
                    _link(
                        "requests",
                        "request_id",
                        str(request["request_id"]),
                        int(request["version"]),
                    )
                )
            event = task_events.get(task_id)
            if event is not None:
                evidence.append(_link("events", "event_id", str(event["event_id"])))
            transition = task_transitions.get(task_id)
            if transition is not None:
                evidence.append(
                    _link("task_transitions", "transition_id", str(transition["transition_id"]))
                )
            agent_values = []
            for agent in sorted(associated, key=lambda value: (value["role"], value["callsign"], value["agent_id"])):
                agent_id = str(agent["agent_id"])
                evidence.append(_link("agent_instances", "agent_id", agent_id, int(agent["version"])))
                agent_values.append(
                    {
                        "agent_id": agent_id,
                        "callsign": agent["callsign"],
                        "role": agent["role"],
                        "status": agent["status"],
                        "updated_at": agent["updated_at"],
                        "update": _private(agent["update_text"], visibility),
                        "blocker": _private(agent["blocker"], visibility),
                        "next_action": _private(agent["next_action"], visibility),
                    }
                )
            item = {
                "kind": "task",
                "task_id": task_id,
                "summary": _private(row["summary"], visibility),
                "status": row["state"],
                "updated_at": row["updated_at"],
                "stale": stale,
                "owner": (
                    {"kind": "agent", "id": row["current_owner_agent_id"]}
                    if row["current_owner_agent_id"] is not None
                    else {"kind": "squad", "id": row["current_owner_squad_id"]}
                    if row["current_owner_squad_id"] is not None
                    else None
                ),
                "request_id": request["request_id"] if request is not None else None,
                "update": _private(
                    transition["update_text"] if transition is not None else associated[0]["update_text"] if associated else None,
                    visibility,
                ),
                "blocker": _private(
                    transition["blocker"] if transition is not None else associated[0]["blocker"] if associated else None,
                    visibility,
                ),
                "next_action": _private(
                    transition["next_action"] if transition is not None else associated[0]["next_action"] if associated else None,
                    visibility,
                ),
                "agents": agent_values,
                "evidence_links": evidence,
            }
            items.append((row["project_id"], category, item))

        for row in agent_sample:
            agent_id = str(row["agent_id"])
            if agent_id in represented_agents:
                continue
            updated = _time(str(row["updated_at"]), "agent update")
            category, stale = _classify(
                str(row["status"]),
                set(),
                updated,
                recent_time,
                stale_time,
                unresolved=row["role"] != "shotcaller",
            )
            if category is None:
                continue
            event = agent_events.get(agent_id)
            items.append(
                (
                    None,
                    category,
                    _agent_item(row, event, visibility, stale),
                )
            )

        for row in request_rows[:limit]:
            request_id = str(row["request_id"])
            if request_id in represented_requests:
                continue
            category = "needs_action" if row["state"] in NEEDS_ACTION_STATES else "unresolved"
            stale = _time(str(row["updated_at"]), "request update") < stale_time
            items.append(
                (
                    row["project_id"],
                    category,
                    _request_item(row, visibility, stale),
                )
            )

        items.sort(
            key=lambda entry: (
                _time(str(entry[2]["updated_at"]), "Roster item update"),
                str(entry[2].get("task_id") or entry[2].get("agent_id") or entry[2].get("request_id")),
            ),
            reverse=True,
        )
        combined_truncated = len(items) > limit
        for project_id, category, item in items[:limit]:
            target = projects.get(str(project_id)) if project_id is not None else None
            (target or unresolved_project)["groups"][category].append(item)

        recent_transitions: list[dict[str, Any]] = []
        for event in recent_event_rows[:transition_limit]:
            if _time(str(event["occurred_at"]), "event time") < recent_time:
                continue
            recent_transitions.append(
                {
                    "event_id": event["event_id"],
                    "event_type": event["event_type"],
                    "status": event["status"],
                    "occurred_at": event["occurred_at"],
                    "update": _private(event["update_text"], visibility),
                    "evidence_link": _link("events", "event_id", str(event["event_id"])),
                }
            )

        groups = [projects[key] for key in sorted(projects)]
        if any(unresolved_project["groups"].values()):
            groups.append(unresolved_project)
        squads = []
        for row in squad_rows[:MAX_ROSTER_ITEMS]:
            available = _squad_unavailable_reason(row) is None
            squads.append(
                {
                    "squad_id": row["squad_id"],
                    "state": row["squad_state"],
                    "available": available,
                    "shotcaller": {
                        "agent_id": row["shotcaller_agent_id"],
                        "callsign": row["shotcaller"],
                        "status": row["shotcaller_status"],
                    },
                    "evidence_link": _link("squads", "squad_id", str(row["squad_id"]), int(row["version"])),
                }
            )

    counts = {name: 0 for name in ("needs_action", "recently_finished", "underway", "unresolved")}
    for group in groups:
        for name in counts:
            counts[name] += len(group["groups"][name])
    truncated = {
        "items": combined_truncated or len(task_rows) > limit or len(agent_rows) > limit or len(request_rows) > limit,
        "projects": len(project_rows) > MAX_ROSTER_ITEMS,
        "squads": len(squad_rows) > MAX_ROSTER_ITEMS,
        "transitions": len(recent_event_rows) > transition_limit,
    }
    return {
        "schema": "league.roster-snapshot.v1",
        "canonical": False,
        "read_only": True,
        "visibility": visibility,
        "snapshot": {
            "as_of": as_of,
            "recent_since": recent_since,
            "stale_before": stale_before,
            "transaction": "one-bounded-read",
        },
        "bounds": {"item_limit": limit, "transition_limit": transition_limit},
        "truncated": truncated,
        "counts": {**counts, "projects": len(groups), "squads": len(squads)},
        "squads": squads,
        "projects": groups,
        "recent_transitions": recent_transitions,
    }
