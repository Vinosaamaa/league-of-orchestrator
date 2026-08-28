"""Pending-offer registration for one stable Squad and one live primary Shotcaller."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Optional, Sequence

from .storage_types import StorageRefusal


SQUAD_ID = re.compile(r"^squad:[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("invalid_time", f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("invalid_time", f"{label} must include a UTC offset")
    return parsed


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _exact_runtime(store: Any, agent_id: str, runtime_instance_id: str) -> sqlite3.Row:
    row = store.connection.execute(
        """
        SELECT r.*,a.role,a.retired_at FROM runtime_instances r
        JOIN agent_instances a ON a.agent_id=r.actor_agent_id
        WHERE r.runtime_instance_id=? AND r.actor_agent_id=?
        """,
        (runtime_instance_id, agent_id),
    ).fetchone()
    if (
        row is None
        or row["role"] != "shotcaller"
        or row["retired_at"] is not None
        or row["status"] not in {"active", "idle"}
        or not row["verified"]
    ):
        raise StorageRefusal(
            "squad_runtime_mismatch", "Squad registration requires the exact live Shotcaller runtime"
        )
    return row


def _bounded(values: Sequence[str], label: str, limit: int) -> tuple[str, ...]:
    result = tuple(values)
    if len(result) > limit or len(set(result)) != len(result) or any(not item for item in result):
        raise StorageRefusal("squad_registration_invalid", f"{label} are empty, duplicated, or unbounded")
    return result


def register_squad(
    store: Any,
    *,
    registration_id: str,
    squad_id: str,
    requester_agent_id: str,
    shotcaller_agent_id: str,
    runtime_instance_id: str,
    project_ids: Sequence[str],
    capabilities: Sequence[str],
    expires_at: str,
    event_id: str,
    outbox_id: str,
    at: str,
) -> dict[str, Any]:
    now = _time(at, "Squad registration time")
    expiry = _time(expires_at, "Squad registration expiry")
    projects = _bounded(project_ids, "project IDs", 32)
    required = _bounded(capabilities, "Squad capabilities", 64)
    if (
        not all((registration_id, requester_agent_id, shotcaller_agent_id, runtime_instance_id, event_id, outbox_id))
        or not SQUAD_ID.fullmatch(squad_id)
        or expiry <= now
    ):
        raise StorageRefusal("squad_registration_invalid", "Squad registration identity or expiry is invalid")
    try:
        with store._transaction():
            _exact_runtime(store, shotcaller_agent_id, runtime_instance_id)
            requester = store.connection.execute(
                "SELECT 1 FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                (requester_agent_id,),
            ).fetchone()
            if requester is None:
                raise StorageRefusal("squad_requester_unknown", "Squad requester is not active")
            existing = store.connection.execute(
                "SELECT * FROM squad_registration_offers WHERE registration_id=?",
                (registration_id,),
            ).fetchone()
            if existing is not None:
                exact = (
                    existing["squad_id"] == squad_id
                    and existing["requester_agent_id"] == requester_agent_id
                    and existing["shotcaller_agent_id"] == shotcaller_agent_id
                    and existing["runtime_instance_id"] == runtime_instance_id
                    and tuple(json.loads(existing["project_ids_json"])) == projects
                    and tuple(json.loads(existing["capabilities_json"])) == required
                    and existing["expires_at"] == expires_at
                    and existing["offer_event_id"] == event_id
                    and existing["offer_outbox_id"] == outbox_id
                )
                if not exact:
                    raise StorageRefusal("squad_registration_conflict", "registration retry changed identity")
                return {
                    "registration_id": registration_id,
                    "squad_id": squad_id,
                    "state": existing["state"],
                    "event_id": existing["offer_event_id"],
                    "outbox_id": existing["offer_outbox_id"],
                    "idempotent": True,
                }
            if store.connection.execute(
                "SELECT 1 FROM squads WHERE squad_id=? OR shotcaller_agent_id=?",
                (squad_id, shotcaller_agent_id),
            ).fetchone() is not None:
                raise StorageRefusal(
                    "squad_active_conflict", "Squad ID or primary Shotcaller is already active"
                )
            if projects:
                placeholders = ",".join("?" for _ in projects)
                known = {
                    str(row["project_id"])
                    for row in store.connection.execute(
                        f"SELECT project_id FROM projects WHERE project_id IN ({placeholders}) AND state='active'",
                        projects,
                    )
                }
                if known != set(projects):
                    raise StorageRefusal("project_unknown", "Squad registration project is not active")
            conflict = store.connection.execute(
                """
                SELECT 1 FROM squad_registration_offers
                 WHERE state='pending' AND (squad_id=? OR shotcaller_agent_id=?)
                """,
                (squad_id, shotcaller_agent_id),
            ).fetchone()
            if conflict is not None:
                raise StorageRefusal("squad_registration_conflict", "another pending registration owns this identity")
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,?,NULL,NULL,1,'squad_registration_offered','pending',?,?,?,
                       'squad_registration',?)
                """,
                (
                    event_id,
                    shotcaller_agent_id,
                    "Squad registration offered",
                    at,
                    _json({"registration_id": registration_id, "squad_id": squad_id}),
                    registration_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO delivery_outbox
                  (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                VALUES(?,?,?,'pending',?,0)
                """,
                (outbox_id, event_id, shotcaller_agent_id, at),
            )
            store.connection.execute(
                """
                INSERT INTO squad_registration_offers
                  (registration_id,squad_id,requester_agent_id,shotcaller_agent_id,
                   runtime_instance_id,project_ids_json,capabilities_json,state,expires_at,
                   offer_event_id,offer_outbox_id,registered_at)
                VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)
                """,
                (
                    registration_id,
                    squad_id,
                    requester_agent_id,
                    shotcaller_agent_id,
                    runtime_instance_id,
                    _json(list(projects)),
                    _json(list(required)),
                    expires_at,
                    event_id,
                    outbox_id,
                    at,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "Squad registration conflicted with canonical state") from exc
    return {
        "registration_id": registration_id,
        "squad_id": squad_id,
        "state": "pending",
        "event_id": event_id,
        "outbox_id": outbox_id,
        "idempotent": False,
    }


def accept_squad(
    store: Any,
    *,
    registration_id: str,
    shotcaller_agent_id: str,
    runtime_instance_id: str,
    decision: str,
    event_id: str,
    outbox_id: str,
    at: str,
) -> dict[str, Any]:
    now = _time(at, "Squad registration response time")
    if decision not in {"accept", "reject"} or not all(
        (registration_id, shotcaller_agent_id, runtime_instance_id, event_id, outbox_id)
    ):
        raise StorageRefusal("squad_registration_invalid", "Squad response is invalid")
    expired = False
    result: dict[str, Any] = {}
    try:
        with store._transaction():
            _exact_runtime(store, shotcaller_agent_id, runtime_instance_id)
            offer = store.connection.execute(
                "SELECT * FROM squad_registration_offers WHERE registration_id=?",
                (registration_id,),
            ).fetchone()
            if (
                offer is None
                or offer["shotcaller_agent_id"] != shotcaller_agent_id
                or offer["runtime_instance_id"] != runtime_instance_id
            ):
                raise StorageRefusal("squad_registration_unknown", "registration is not bound to this runtime")
            terminal = "accepted" if decision == "accept" else "rejected"
            if offer["state"] == terminal:
                if offer["response_event_id"] != event_id or offer["response_outbox_id"] != outbox_id:
                    raise StorageRefusal("squad_registration_conflict", "response retry changed identity")
                return {
                    "registration_id": registration_id,
                    "squad_id": offer["squad_id"],
                    "state": terminal,
                    "event_id": event_id,
                    "outbox_id": outbox_id,
                    "idempotent": True,
                }
            if offer["state"] != "pending":
                raise StorageRefusal("squad_registration_conflict", "registration is already terminal")
            if _time(str(offer["expires_at"]), "stored Squad registration expiry") <= now:
                store.connection.execute(
                    "UPDATE squad_registration_offers SET state='expired',responded_at=? WHERE registration_id=?",
                    (at, registration_id),
                )
                expired = True
            else:
                if decision == "accept":
                    if store.connection.execute(
                        "SELECT 1 FROM squads WHERE squad_id=? OR shotcaller_agent_id=?",
                        (offer["squad_id"], shotcaller_agent_id),
                    ).fetchone() is not None:
                        raise StorageRefusal(
                            "squad_active_conflict", "Squad ID or primary Shotcaller is already active"
                        )
                    store.connection.execute(
                        """
                        INSERT INTO squads(squad_id,shotcaller_agent_id,state,version,updated_at,owner_fence)
                        VALUES(?,?,'active',1,?,1)
                        """,
                        (offer["squad_id"], shotcaller_agent_id, at),
                    )
                    store.connection.execute(
                        """
                        INSERT INTO shotcaller_intake(agent_id,squad_id,state,fence,version,updated_at)
                        VALUES(?,?,'accepting',1,1,?)
                        """,
                        (shotcaller_agent_id, offer["squad_id"], at),
                    )
                    store.connection.executemany(
                        "INSERT INTO squad_capabilities(squad_id,capability) VALUES(?,?)",
                        (
                            (offer["squad_id"], capability)
                            for capability in json.loads(offer["capabilities_json"])
                        ),
                    )
                    for project_id in json.loads(offer["project_ids_json"]):
                        position = int(
                            store.connection.execute(
                                "SELECT COALESCE(MAX(position),-1)+1 FROM project_squad_suggestions WHERE project_id=?",
                                (project_id,),
                            ).fetchone()[0]
                        )
                        store.connection.execute(
                            """
                            INSERT INTO project_squad_suggestions
                              (project_id,squad_id,position,created_at,updated_at)
                            VALUES(?,?,?,?,?)
                            """,
                            (project_id, offer["squad_id"], position, at, at),
                        )
                        store.connection.execute(
                            "UPDATE projects SET version=version+1,updated_at=? WHERE project_id=?",
                            (at, project_id),
                        )
                    event_agent_id: Optional[str] = None
                    event_squad_id: Optional[str] = str(offer["squad_id"])
                else:
                    event_agent_id = shotcaller_agent_id
                    event_squad_id = None
                store.connection.execute(
                    """
                    INSERT INTO events
                      (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
                       occurred_at,detail_json,aggregate_kind,aggregate_id,source_event_id)
                    VALUES(?,?,NULL,?,1,?,?,?,?,?, 'squad_registration',?,?)
                    """,
                    (
                        event_id,
                        event_agent_id,
                        event_squad_id,
                        f"squad_registration_{terminal}",
                        terminal,
                        f"Squad registration {terminal}",
                        at,
                        _json({"registration_id": registration_id, "squad_id": offer["squad_id"]}),
                        registration_id,
                        offer["offer_event_id"],
                    ),
                )
                store.connection.execute(
                    """
                    INSERT INTO delivery_outbox
                      (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                    VALUES(?,?,?,'pending',?,0)
                    """,
                    (outbox_id, event_id, offer["requester_agent_id"], at),
                )
                store.connection.execute(
                    """
                    UPDATE squad_registration_offers
                       SET state=?,response_event_id=?,response_outbox_id=?,responded_at=?
                     WHERE registration_id=?
                    """,
                    (terminal, event_id, outbox_id, at, registration_id),
                )
                result = {
                    "registration_id": registration_id,
                    "squad_id": offer["squad_id"],
                    "state": terminal,
                    "event_id": event_id,
                    "outbox_id": outbox_id,
                    "idempotent": False,
                }
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "Squad response conflicted with canonical state") from exc
    if expired:
        raise StorageRefusal("squad_registration_expired", "Squad registration offer expired")
    return result


def squad_status(
    store: Any,
    *,
    registration_id: Optional[str] = None,
    squad_id: Optional[str] = None,
    at: str,
) -> dict[str, Any]:
    now = _time(at, "Squad status time")
    if (registration_id is None) == (squad_id is None):
        raise StorageRefusal("squad_status_invalid", "select exactly one registration or Squad")
    offer = store.connection.execute(
        "SELECT * FROM squad_registration_offers WHERE "
        + ("registration_id=?" if registration_id is not None else "squad_id=? ORDER BY registered_at DESC LIMIT 1"),
        (registration_id if registration_id is not None else squad_id,),
    ).fetchone()
    squad = store.connection.execute(
        """
        SELECT s.*,i.state intake_state,i.fence intake_fence
          FROM squads s LEFT JOIN shotcaller_intake i
            ON i.squad_id=s.squad_id AND i.agent_id=s.shotcaller_agent_id
         WHERE s.squad_id=?
        """,
        (squad_id or (offer["squad_id"] if offer is not None else ""),),
    ).fetchone()
    effective = None
    if offer is not None:
        effective = str(offer["state"])
        if effective == "pending" and _time(str(offer["expires_at"]), "stored Squad expiry") <= now:
            effective = "expired"
    return {
        "registration": None
        if offer is None
        else {
            "registration_id": offer["registration_id"],
            "squad_id": offer["squad_id"],
            "state": effective,
            "shotcaller_agent_id": offer["shotcaller_agent_id"],
            "runtime_instance_id": offer["runtime_instance_id"],
            "project_ids": json.loads(offer["project_ids_json"]),
            "expires_at": offer["expires_at"],
        },
        "squad": None
        if squad is None
        else {
            "squad_id": squad["squad_id"],
            "state": squad["state"],
            "shotcaller_agent_id": squad["shotcaller_agent_id"],
            "owner_fence": int(squad["owner_fence"]),
            "intake_state": squad["intake_state"],
        },
    }
