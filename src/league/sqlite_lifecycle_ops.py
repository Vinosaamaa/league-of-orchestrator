"""SQLite agent, callsign, project, and task-ownership operations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .storage_types import FaultInjector, LIFECYCLE_STATES, StorageRefusal
from .sqlite_runtime_replacement_ops import assert_runtime_replacement_mutation_allowed


def agent_status(store: Any, agent_id: str) -> Optional[dict[str, Any]]:
    row = store.connection.execute(
        """
        SELECT agent_id,callsign,role,shotcaller_agent_id,task_id,kind,address,thread_id,
               backend,routing_name,display_agent,status,version,updated_at,update_text,
               blocker,next_action,retired_at
          FROM agent_instances WHERE agent_id=?
        """,
        (agent_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def transition(
    store: Any,
    agent_id: str,
    expected_version: int,
    status: str,
    update: str,
    at: str,
    *,
    fault: Optional[FaultInjector] = None,
) -> dict[str, Any]:
    if status not in LIFECYCLE_STATES or expected_version < 1 or not update or not at:
        raise StorageRefusal("invalid_transition", "transition fields are invalid")
    next_version = expected_version + 1
    event_id = f"agent:{agent_id}:{next_version}"
    try:
        with store._transaction():
            assert_runtime_replacement_mutation_allowed(store, agent_id=agent_id)
            current = store.connection.execute(
                """
                SELECT version,role,shotcaller_agent_id
                  FROM agent_instances WHERE agent_id=? AND retired_at IS NULL
                """,
                (agent_id,),
            ).fetchone()
            if current is None or int(current["version"]) != expected_version:
                raise StorageRefusal("version_conflict", "transition expected-version precondition failed")
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,detail_json)
                VALUES(?,?,NULL,?,'agent_transition',?,?,?,'{}')
                """,
                (event_id, agent_id, next_version, status, update, at),
            )
            if fault:
                fault("after_event_insert")
            changed = store.connection.execute(
                """
                UPDATE agent_instances SET status=?,version=?,updated_at=?,update_text=?
                 WHERE agent_id=? AND version=? AND retired_at IS NULL
                """,
                (status, next_version, at, update, agent_id, expected_version),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("version_conflict", "transition expected-version precondition failed")
            recipient_agent_id = (
                str(current["shotcaller_agent_id"])
                if current["role"] == "champion" and current["shotcaller_agent_id"]
                else None
            )
            outbox_id = None
            if recipient_agent_id is not None:
                outbox_id = f"outbox:{event_id}"
                store.connection.execute(
                    """
                    INSERT INTO delivery_outbox
                      (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                    VALUES(?,?,?,'pending',?,0)
                    """,
                    (outbox_id, event_id, recipient_agent_id, at),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "transition conflicted with canonical state") from exc
    return {
        "event_id": event_id,
        "outbox_id": outbox_id,
        "recipient_agent_id": recipient_agent_id,
        "agent_id": agent_id,
        "version": next_version,
        "status": status,
    }


def reserve_callsign(
    store: Any,
    callsign: str,
    agent_id: str,
    task_id: str,
    role: str,
    status: str,
    update: str,
    at: str,
) -> dict[str, Any]:
    if role not in {"shotcaller", "champion", "hidden-worker"} or status not in LIFECYCLE_STATES:
        raise StorageRefusal("invalid_reservation", "callsign reservation fields are invalid")
    event_id = f"agent:{agent_id}:1"
    try:
        with store._transaction():
            existing = store.connection.execute(
                """
                SELECT a.agent_id,a.task_id,a.role,a.status,a.update_text,a.updated_at,a.retired_at
                  FROM callsign_leases l
                  JOIN agent_instances a ON a.agent_id=l.agent_id
                 WHERE l.callsign=?
                """,
                (callsign,),
            ).fetchone()
            if existing is not None:
                same_identity = (
                    existing["agent_id"] == agent_id
                    and existing["task_id"] == task_id
                    and existing["role"] == role
                    and existing["retired_at"] is None
                )
                if not same_identity:
                    raise StorageRefusal("callsign_unavailable", "callsign already has a different live lease")
                if (
                    existing["status"] != status
                    or existing["update_text"] != update
                    or existing["updated_at"] != at
                ):
                    raise StorageRefusal(
                        "reservation_mismatch",
                        "idempotent callsign retry differs from the persisted reservation",
                    )
                return {
                    "event_id": event_id,
                    "callsign": callsign,
                    "agent_id": agent_id,
                    "version": 1,
                    "idempotent": True,
                }
            callsign_row = store.connection.execute(
                "SELECT pool_role,enabled FROM callsigns WHERE callsign=?", (callsign,)
            ).fetchone()
            if callsign_row is None or not callsign_row["enabled"] or callsign_row["pool_role"] != role:
                raise StorageRefusal("callsign_unavailable", "callsign is unavailable for the requested role")
            store.connection.execute(
                """
                INSERT INTO agent_instances
                  (agent_id,callsign,role,shotcaller_agent_id,task_id,kind,address,thread_id,
                   backend,routing_name,display_agent,repository,issue,branch,worktree,status,
                   version,updated_at,update_text,blocker,next_action,metadata_json,retired_at)
                VALUES(?,?,?,NULL,?,'unbound',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?,1,?,?,NULL,
                       'Complete launch binding through an adapter','{}',NULL)
                """,
                (agent_id, callsign, role, task_id, status, at, update),
            )
            store.connection.execute(
                "INSERT INTO callsign_leases(callsign,agent_id,launch_attempt_id,reserved_at) VALUES(?,?,NULL,?)",
                (callsign, agent_id, at),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,detail_json)
                VALUES(?,?,NULL,1,'callsign_reserved',?,?,?,'{}')
                """,
                (event_id, agent_id, status, update, at),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "callsign reservation conflicted with canonical state"
        ) from exc
    return {
        "event_id": event_id,
        "callsign": callsign,
        "agent_id": agent_id,
        "version": 1,
        "idempotent": False,
    }


def release_callsign(
    store: Any, callsign: str, agent_id: str, expected_version: int, at: str
) -> dict[str, Any]:
    next_version = expected_version + 1
    event_id = f"agent:{agent_id}:{next_version}"
    try:
        with store._transaction():
            current = store.connection.execute(
                """
                SELECT a.status,a.version FROM agent_instances a
                JOIN callsign_leases l ON l.agent_id=a.agent_id
                WHERE a.agent_id=? AND a.callsign=? AND l.callsign=? AND a.retired_at IS NULL
                """,
                (agent_id, callsign, callsign),
            ).fetchone()
            if current is None or int(current["version"]) != expected_version:
                raise StorageRefusal("version_conflict", "callsign release precondition failed")
            store.connection.execute("DELETE FROM callsign_leases WHERE callsign=?", (callsign,))
            store.connection.execute(
                "UPDATE callsigns SET last_released_at=? WHERE callsign=?", (at, callsign)
            )
            store.connection.execute(
                """
                UPDATE agent_instances SET version=?,updated_at=?,update_text='callsign released',retired_at=?
                 WHERE agent_id=? AND version=?
                """,
                (next_version, at, at, agent_id, expected_version),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,detail_json)
                VALUES(?,?,NULL,?,'callsign_released',?,'callsign released',?,'{}')
                """,
                (event_id, agent_id, next_version, current["status"], at),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "callsign release conflicted with canonical state"
        ) from exc
    return {"event_id": event_id, "callsign": callsign, "agent_id": agent_id, "version": next_version}


def resolve_project(store: Any, repository: str) -> Optional[dict[str, Any]]:
    row = store.connection.execute(
        "SELECT project_id,repository,state,version,updated_at FROM projects WHERE repository=?",
        (repository,),
    ).fetchone()
    return dict(row) if row is not None else None


def transfer_task_owner(
    store: Any,
    task_id: str,
    expected_version: int,
    owner_kind: str,
    owner_id: str,
    at: str,
) -> dict[str, Any]:
    if owner_kind not in {"agent", "squad"}:
        raise StorageRefusal("invalid_owner", "owner kind must be agent or squad")
    next_version = expected_version + 1
    event_id = f"task:{task_id}:{next_version}"
    agent_owner = owner_id if owner_kind == "agent" else None
    squad_owner = owner_id if owner_kind == "squad" else None
    try:
        with store._transaction():
            if owner_kind == "agent":
                owner = store.connection.execute(
                    "SELECT 1 FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                    (owner_id,),
                ).fetchone()
            else:
                owner = store.connection.execute(
                    "SELECT 1 FROM squads WHERE squad_id=? AND state='active'", (owner_id,)
                ).fetchone()
            if owner is None:
                raise StorageRefusal("owner_unknown", "new owner is not active")
            changed = store.connection.execute(
                """
                UPDATE tasks SET current_owner_agent_id=?,current_owner_squad_id=?,version=?,updated_at=?
                 WHERE task_id=? AND version=?
                """,
                (agent_owner, squad_owner, next_version, at, task_id, expected_version),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("version_conflict", "task owner expected-version precondition failed")
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,detail_json)
                VALUES(NULLIF(?,''),NULL,?,?,'task_owner_transferred','active','owner transferred',?,?)
                """,
                (event_id, task_id, next_version, at, json.dumps({"owner_kind": owner_kind}, sort_keys=True)),
            )
            store.connection.execute(
                """
                INSERT INTO assignment_receipts
                  (task_id,task_version,owner_agent_id,owner_squad_id,received_at)
                VALUES(?,?,?,?,?)
                """,
                (task_id, next_version, agent_owner, squad_owner, at),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "task owner transfer conflicted with canonical state"
        ) from exc
    return {
        "event_id": event_id,
        "task_id": task_id,
        "version": next_version,
        "owner": {"kind": owner_kind, "id": owner_id},
    }
