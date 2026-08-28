"""One-transaction bounded read-only project-grouped Roster snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from .sqlite_project_ops import _project_value, _squad_rows
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
RESOLVED_REQUEST_STATES = {"answered", "cancelled"}


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


def _latest_events(store: Any, column: str, identities: list[str], limit: int) -> dict[str, dict[str, Any]]:
    if not identities:
        return {}
    placeholders = ",".join("?" for _ in identities)
    rows = store.connection.execute(
        f"""
        SELECT event_id,{column} entity_id,event_type,status,update_text,occurred_at,event_seq
          FROM events WHERE {column} IN ({placeholders})
         ORDER BY event_seq DESC,event_id DESC LIMIT ?
        """,
        (*identities, limit),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result.setdefault(str(row["entity_id"]), dict(row))
    return result


def _latest_task_transitions(
    store: Any, identities: list[str], limit: int
) -> dict[str, dict[str, Any]]:
    if not identities:
        return {}
    placeholders = ",".join("?" for _ in identities)
    rows = store.connection.execute(
        f"""
        SELECT task_id,transition_id,event_id,update_text,next_action,blocker,created_at
          FROM task_transitions WHERE task_id IN ({placeholders})
         ORDER BY created_at DESC,transition_id DESC LIMIT ?
        """,
        (*identities, limit),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result.setdefault(str(row["task_id"]), dict(row))
    return result


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
            "SELECT * FROM projects ORDER BY project_id LIMIT ?", (MAX_ROSTER_ITEMS + 1,)
        ).fetchall()
        task_rows = store.connection.execute(
            "SELECT * FROM tasks ORDER BY updated_at DESC,task_id LIMIT ?", (limit + 1,)
        ).fetchall()
        agent_rows = store.connection.execute(
            """
            SELECT agent_id,callsign,role,shotcaller_agent_id,task_id,status,version,
                   updated_at,update_text,blocker,next_action,retired_at
              FROM agent_instances WHERE retired_at IS NULL
             ORDER BY updated_at DESC,agent_id LIMIT ?
            """,
            (limit + 1,),
        ).fetchall()
        request_rows = store.connection.execute(
            """
            SELECT r.request_id,r.summary,r.state,r.version,r.updated_at,r.owner_agent_id,
                   r.return_to_agent_id,r.last_route_event_id,r.resolution_summary,t.task_id,t.project_id
              FROM requests r LEFT JOIN tasks t ON t.request_id=r.request_id
             WHERE r.state NOT IN ('answered','cancelled')
             ORDER BY r.updated_at DESC,r.request_id LIMIT ?
            """,
            (limit + 1,),
        ).fetchall()
        squad_rows = store.connection.execute(
            """
            SELECT s.squad_id,s.state,s.version,s.updated_at,a.agent_id shotcaller_agent_id,
                   a.callsign shotcaller,a.status shotcaller_status,a.retired_at
              FROM squads s JOIN agent_instances a ON a.agent_id=s.shotcaller_agent_id
             ORDER BY s.squad_id LIMIT ?
            """,
            (MAX_ROSTER_ITEMS + 1,),
        ).fetchall()
        task_sample = list(task_rows[:limit])
        agent_sample = list(agent_rows[:limit])
        task_events = _latest_events(store, "task_id", [str(row["task_id"]) for row in task_sample], limit)
        task_transitions = _latest_task_transitions(
            store, [str(row["task_id"]) for row in task_sample], limit
        )
        agent_events = _latest_events(store, "agent_id", [str(row["agent_id"]) for row in agent_sample], limit)
        transition_limit = min(limit, 100)
        recent_event_rows = store.connection.execute(
            """
            SELECT event_id,event_type,status,update_text,occurred_at,event_seq
              FROM events ORDER BY event_seq DESC,event_id DESC LIMIT ?
            """,
            (transition_limit + 1,),
        ).fetchall()

        projects: dict[str, dict[str, Any]] = {}
        for row in project_rows[:MAX_ROSTER_ITEMS]:
            project = _project_value(store, row, visibility)
            project["suggested_squads"] = _squad_rows(store, row["project_id"])
            projects[str(row["project_id"])] = {
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
        request_by_task = {str(row["task_id"]): row for row in request_rows[:limit] if row["task_id"] is not None}
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
            states = {str(row["state"]), *(str(agent["status"]) for agent in associated)}
            if request is not None:
                states.add(str(request["state"]))
            stale = updated < stale_time and not states & TERMINAL_STATES
            if states & NEEDS_ACTION_STATES or stale:
                category = "needs_action"
            elif str(row["state"]) in TERMINAL_STATES:
                if updated < recent_time:
                    continue
                category = "recently_finished"
            elif row["project_id"] is None:
                category = "unresolved"
            else:
                category = "underway"
            evidence = [_link("tasks", "task_id", task_id, int(row["version"]))]
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
            stale = updated < stale_time and str(row["status"]) not in TERMINAL_STATES
            if str(row["status"]) in NEEDS_ACTION_STATES or stale:
                category = "needs_action"
            elif str(row["status"]) in TERMINAL_STATES:
                if updated < recent_time:
                    continue
                category = "recently_finished"
            elif row["role"] == "shotcaller":
                category = "underway"
            else:
                category = "unresolved"
            evidence = [_link("agent_instances", "agent_id", agent_id, int(row["version"]))]
            event = agent_events.get(agent_id)
            if event is not None:
                evidence.append(_link("events", "event_id", str(event["event_id"])))
            items.append(
                (
                    None,
                    category,
                    {
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
                    },
                )
            )

        for row in request_rows[:limit]:
            request_id = str(row["request_id"])
            if request_id in represented_requests:
                continue
            category = "needs_action" if row["state"] in NEEDS_ACTION_STATES else "unresolved"
            evidence = [_link("requests", "request_id", request_id, int(row["version"]))]
            if row["last_route_event_id"] is not None:
                evidence.append(_link("events", "event_id", str(row["last_route_event_id"])))
            items.append(
                (
                    row["project_id"],
                    category,
                    {
                        "kind": "request",
                        "request_id": request_id,
                        "summary": _private(row["summary"], visibility),
                        "status": row["state"],
                        "updated_at": row["updated_at"],
                        "stale": _time(str(row["updated_at"]), "request update") < stale_time,
                        "owner_agent_id": row["owner_agent_id"],
                        "return_to_agent_id": row["return_to_agent_id"],
                        "evidence_links": evidence,
                    },
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
            available = row["state"] == "active" and row["retired_at"] is None and row["shotcaller_status"] not in TERMINAL_STATES
            squads.append(
                {
                    "squad_id": row["squad_id"],
                    "state": row["state"],
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
