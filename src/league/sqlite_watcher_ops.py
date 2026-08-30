"""Verified runtime/watcher registration and bounded Shotcaller Stop decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Optional

from .sqlite_request_ops import _time
from .storage_watcher import RuntimeRegistrationCommand
from .storage_types import StorageRefusal


def stop_feedback_reason(callsign: str, wait_generation: int) -> str:
    return (
        f"League has unresolved obligations for {callsign} "
        f"at wait generation {wait_generation}."
    )


def consume_stop_feedback(
    store: Any,
    scope_id: str,
    actor_agent_id: str,
    terminal_generation: str,
    body: str,
) -> bool:
    """Consume only the exact one-time feedback emitted by the last Stop block."""

    body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    try:
        with store._transaction():
            changed = store.connection.execute(
                """
                UPDATE watcher_scopes
                   SET pending_stop_feedback_digest=NULL,
                       pending_stop_terminal_generation=NULL,
                       pending_stop_wait_generation=NULL
                 WHERE scope_id=? AND actor_agent_id=?
                   AND pending_stop_feedback_digest=?
                   AND pending_stop_terminal_generation=?
                   AND pending_stop_wait_generation=last_blocked_wait_generation
                """,
                (scope_id, actor_agent_id, body_digest, terminal_generation),
            )
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "Stop feedback suppression conflicted with canonical state"
        ) from exc
    return changed.rowcount == 1


def register_runtime(
    store: Any,
    command: RuntimeRegistrationCommand,
) -> dict[str, Any]:
    runtime_instance_id = command.runtime_instance_id
    actor_agent_id = command.actor_agent_id
    harness_kind = command.harness_kind
    backend_kind = command.backend_kind
    session_ref = command.session_ref
    endpoint = command.endpoint
    runtime_generation = command.runtime_generation
    status = command.status
    verified = command.verified
    at = command.at
    capabilities = command.capabilities
    _time(at, "runtime observation time")
    if status not in {"active", "idle", "closed", "failed"} or not all(
        (runtime_instance_id, actor_agent_id, harness_kind, backend_kind, session_ref, endpoint, runtime_generation)
    ):
        raise StorageRefusal("invalid_runtime", "runtime identity is incomplete")
    if capabilities is not None and (
        any(not isinstance(item, str) or not item for item in capabilities)
        or len(set(capabilities)) != len(capabilities)
    ):
        raise StorageRefusal("invalid_runtime", "runtime capabilities are empty or duplicated")
    capabilities_json = (
        None
        if capabilities is None
        else json.dumps(sorted(capabilities), separators=(",", ":"))
    )
    try:
        with store._transaction():
            actor = store.connection.execute(
                "SELECT 1 FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                (actor_agent_id,),
            ).fetchone()
            if actor is None:
                raise StorageRefusal("actor_unknown", "runtime actor is not active")
            existing = store.connection.execute(
                "SELECT * FROM runtime_instances WHERE runtime_instance_id=?",
                (runtime_instance_id,),
            ).fetchone()
            if existing is not None:
                immutable = (
                    existing["actor_agent_id"] == actor_agent_id
                    and existing["harness_kind"] == harness_kind
                    and existing["backend_kind"] == backend_kind
                    and existing["session_ref"] == session_ref
                    and existing["endpoint"] == endpoint
                    and existing["runtime_generation"] == runtime_generation
                )
                if not immutable:
                    raise StorageRefusal("runtime_conflict", "runtime retry changed immutable identity")
                cleanup_closed = existing["status"] == "closed" and not bool(
                    existing["verified"]
                )
                if cleanup_closed:
                    if status != "closed":
                        raise StorageRefusal(
                            "runtime_closed",
                            "a cleanup-closed runtime cannot be reopened by a stale observation",
                        )
                    return {
                        "runtime_instance_id": runtime_instance_id,
                        "actor_agent_id": actor_agent_id,
                        "status": "closed",
                        "verified": False,
                        "capabilities": sorted(json.loads(existing["capabilities_json"])),
                        "idempotent": True,
                    }
                if capabilities_json is None:
                    store.connection.execute(
                        "UPDATE runtime_instances SET status=?,verified=?,last_seen_at=? WHERE runtime_instance_id=?",
                        (status, int(verified), at, runtime_instance_id),
                    )
                else:
                    store.connection.execute(
                        """
                        UPDATE runtime_instances
                           SET status=?,verified=?,last_seen_at=?,capabilities_json=?
                         WHERE runtime_instance_id=?
                        """,
                        (status, int(verified), at, capabilities_json, runtime_instance_id),
                    )
                return {
                    "runtime_instance_id": runtime_instance_id,
                    "actor_agent_id": actor_agent_id,
                    "status": status,
                    "verified": verified,
                    "capabilities": sorted(
                        json.loads(
                            capabilities_json
                            if capabilities_json is not None
                            else existing["capabilities_json"]
                        )
                    ),
                    "idempotent": True,
                }
            store.connection.execute(
                """
                INSERT INTO runtime_instances
                  (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,
                   endpoint,runtime_generation,status,verified,last_seen_at,capabilities_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    runtime_instance_id,
                    actor_agent_id,
                    harness_kind,
                    backend_kind,
                    session_ref,
                    endpoint,
                    runtime_generation,
                    status,
                    int(verified),
                    at,
                    capabilities_json or "[]",
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "runtime registration conflicted") from exc
    return {
        "runtime_instance_id": runtime_instance_id,
        "actor_agent_id": actor_agent_id,
        "status": status,
        "verified": verified,
        "capabilities": json.loads(capabilities_json or "[]"),
        "idempotent": False,
    }


def ensure_watcher_scope(
    store: Any,
    scope_id: str,
    actor_agent_id: str,
    *,
    block_on_obligations: Optional[bool],
) -> None:
    row = store.connection.execute(
        "SELECT actor_agent_id FROM watcher_scopes WHERE scope_id=?", (scope_id,)
    ).fetchone()
    if row is None:
        store.connection.execute(
            """
            INSERT INTO watcher_scopes
              (scope_id,schema_version,enabled,allow_stop_once,stop_blocked,generation,
               initialized,user_message_generation,wait_active,wait_generation,wait_pid,
               wait_process_start,last_event_id,metadata_json,actor_agent_id,
               block_on_obligations,last_blocked_wait_generation,last_user_priority_generation,
               last_terminal_generation)
            VALUES(?,3,1,0,0,1,1,0,0,1,NULL,NULL,NULL,'{}',?,?,-1,0,NULL)
            """,
            (scope_id, actor_agent_id, int(True if block_on_obligations is None else block_on_obligations)),
        )
    elif row["actor_agent_id"] not in {None, actor_agent_id}:
        raise StorageRefusal("scope_conflict", "watcher scope belongs to another Shotcaller")
    elif block_on_obligations is not None:
        store.connection.execute(
            "UPDATE watcher_scopes SET actor_agent_id=?,block_on_obligations=? WHERE scope_id=?",
            (actor_agent_id, int(block_on_obligations), scope_id),
        )


def register_watcher(
    store: Any,
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
) -> dict[str, Any]:
    now = _time(at, "watcher registration time")
    if _time(leased_until, "watcher lease expiry") <= now or fence < 1 or not wake_locator:
        raise StorageRefusal("invalid_watcher", "watcher registration is incomplete")
    try:
        with store._transaction():
            runtime = store.connection.execute(
                "SELECT actor_agent_id,status,verified FROM runtime_instances WHERE runtime_instance_id=?",
                (runtime_instance_id,),
            ).fetchone()
            if (
                runtime is None
                or runtime["actor_agent_id"] != actor_agent_id
                or runtime["status"] not in {"active", "idle"}
                or not runtime["verified"]
            ):
                raise StorageRefusal("runtime_unverified", "watcher runtime is not a verified live owner endpoint")
            ensure_watcher_scope(
                store, scope_id, actor_agent_id, block_on_obligations=block_on_obligations
            )
            existing = store.connection.execute(
                "SELECT * FROM watcher_registrations WHERE actor_agent_id=?", (actor_agent_id,)
            ).fetchone()
            if existing is not None and int(existing["fence"]) >= fence:
                exact = (
                    int(existing["fence"]) == fence
                    and existing["watcher_id"] == watcher_id
                    and existing["runtime_instance_id"] == runtime_instance_id
                    and existing["wake_locator"] == wake_locator
                    and existing["leased_until"] == leased_until
                )
                if exact:
                    return {
                        "watcher_id": watcher_id,
                        "actor_agent_id": actor_agent_id,
                        "fence": fence,
                        "supervision_status": "armed",
                        "idempotent": True,
                    }
                raise StorageRefusal("watcher_fenced", "watcher registration fence is stale")
            store.connection.execute(
                "DELETE FROM watcher_registrations WHERE actor_agent_id=?", (actor_agent_id,)
            )
            store.connection.execute(
                """
                INSERT INTO watcher_registrations
                  (watcher_id,actor_agent_id,runtime_instance_id,wake_locator,leased_until,fence,registered_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (watcher_id, actor_agent_id, runtime_instance_id, wake_locator, leased_until, fence, at),
            )
            store.connection.execute(
                """
                UPDATE watcher_scopes
                   SET generation=generation+1,wait_generation=wait_generation+1,wait_active=1
                 WHERE scope_id=?
                """,
                (scope_id,),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "watcher registration conflicted") from exc
    return {
        "watcher_id": watcher_id,
        "actor_agent_id": actor_agent_id,
        "fence": fence,
        "supervision_status": "armed",
        "idempotent": False,
    }


def note_user_message(store: Any, scope_id: str, actor_agent_id: str, at: str) -> dict[str, Any]:
    _time(at, "user message time")
    try:
        with store._transaction():
            ensure_watcher_scope(store, scope_id, actor_agent_id, block_on_obligations=None)
            store.connection.execute(
                """
                UPDATE watcher_scopes
                   SET user_message_generation=user_message_generation+1,
                       wait_generation=wait_generation+1,stop_blocked=0,wait_active=0,
                       pending_stop_feedback_digest=NULL,
                       pending_stop_terminal_generation=NULL,
                       pending_stop_wait_generation=NULL
                 WHERE scope_id=?
                """,
                (scope_id,),
            )
            row = store.connection.execute(
                "SELECT user_message_generation,wait_generation FROM watcher_scopes WHERE scope_id=?",
                (scope_id,),
            ).fetchone()
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "user-message generation update conflicted") from exc
    return {
        "scope_id": scope_id,
        "priority": "user",
        "user_message_generation": int(row["user_message_generation"]),
        "wait_generation": int(row["wait_generation"]),
    }


def rearm_wait(store: Any, scope_id: str, actor_agent_id: str, event_id: str, at: str) -> dict[str, Any]:
    _time(at, "wait rearm time")
    try:
        with store._transaction():
            ensure_watcher_scope(store, scope_id, actor_agent_id, block_on_obligations=None)
            store.connection.execute(
                """
                UPDATE watcher_scopes
                   SET wait_generation=wait_generation+1,wait_active=1,stop_blocked=0,last_event_id=?
                 WHERE scope_id=?
                """,
                (event_id, scope_id),
            )
            generation = int(
                store.connection.execute(
                    "SELECT wait_generation FROM watcher_scopes WHERE scope_id=?", (scope_id,)
                ).fetchone()[0]
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "wait generation rearm conflicted") from exc
    return {
        "scope_id": scope_id,
        "wait_generation": generation,
        "event_id": event_id,
        "supervision_status": "waiting",
    }


def set_allow_stop_once(store: Any, scope_id: str, actor_agent_id: str) -> dict[str, Any]:
    try:
        with store._transaction():
            ensure_watcher_scope(store, scope_id, actor_agent_id, block_on_obligations=None)
            store.connection.execute(
                "UPDATE watcher_scopes SET allow_stop_once=1 WHERE scope_id=?", (scope_id,)
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "allow-stop update conflicted") from exc
    return {"scope_id": scope_id, "allow_stop_once": True}


def _obligation_counts(store: Any, actor_agent_id: str) -> dict[str, int]:
    row = store.connection.execute(
        """
        SELECT
          (
            SELECT COUNT(*) FROM agent_instances a
            LEFT JOIN tasks t ON t.task_id=a.task_id
             WHERE a.role='champion'
               AND a.shotcaller_agent_id=?
               AND a.retired_at IS NULL
               AND a.status IN ('active','started','working','progress','blocked','ready_to_land')
               AND (
                 a.task_id IS NULL
                 OR t.state IN ('active','pending','accepted','working','progress','in_progress','blocked','ready_to_land')
               )
          ) + (
            SELECT COUNT(*) FROM tasks t
             WHERE t.coordinator_agent_id=?
               AND t.state IN ('active','pending','accepted','working','progress','in_progress','blocked','ready_to_land')
               AND NOT EXISTS (
                 SELECT 1 FROM agent_instances a
                  WHERE a.task_id=t.task_id
                    AND a.role='champion'
                    AND a.shotcaller_agent_id=?
                    AND a.retired_at IS NULL
                    AND a.status IN ('active','started','working','progress','blocked','ready_to_land')
               )
          ) active_champions,
          (SELECT COUNT(*) FROM task_assignments
            WHERE coordinator_agent_id=?
              AND state IN ('pending','launching','cleanup_pending')) pending_assignments,
          (SELECT COUNT(*) FROM requests
            WHERE owner_agent_id=? AND state NOT IN ('answered','cancelled'))
          + (SELECT COUNT(*) FROM prompts
              WHERE current_owner_agent_id=? AND triage_state='untriaged') unresolved_requests,
          (SELECT COUNT(*) FROM delivery_outbox
            WHERE recipient_agent_id=?
              AND state IN ('pending','in_flight','awaiting_receipt')) pending_deliveries,
          (SELECT COUNT(*) FROM cleanup_obligations c JOIN tasks t ON t.task_id=c.task_id
            WHERE t.coordinator_agent_id=?
              AND c.cleanup_state NOT IN ('completed','cleanup_completed')) cleanup_obligations
        """,
        (actor_agent_id,) * 8,
    ).fetchone()
    return {name: int(row[name]) for name in row.keys()}


def stop_decision(
    store: Any,
    scope_id: str,
    actor_agent_id: str,
    terminal_generation: str,
    at: str,
    *,
    block_on_fresh_terminal: bool = False,
) -> dict[str, Any]:
    _time(at, "Stop decision time")
    if not terminal_generation:
        raise StorageRefusal("invalid_stop", "Stop decision requires an observed terminal generation")
    try:
        with store._transaction():
            actor = store.connection.execute(
                "SELECT role,callsign FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                (actor_agent_id,),
            ).fetchone()
            if actor is None or actor["role"] != "shotcaller":
                return {
                    "scope_id": scope_id,
                    "status": "not_shotcaller",
                    "decision": "allow",
                    "priority": None,
                    "wait_generation": None,
                    "terminal_fresh": True,
                    "obligations": {},
                }
            ensure_watcher_scope(store, scope_id, actor_agent_id, block_on_obligations=None)
            scope = store.connection.execute(
                "SELECT * FROM watcher_scopes WHERE scope_id=?", (scope_id,)
            ).fetchone()
            terminal_fresh = scope["last_terminal_generation"] != terminal_generation
            counts = _obligation_counts(store, actor_agent_id)
            total = sum(counts.values())
            common = {
                "scope_id": scope_id,
                "wait_generation": int(scope["wait_generation"]),
                "terminal_fresh": terminal_fresh,
                "obligations": counts,
            }
            store.connection.execute(
                "UPDATE watcher_scopes SET last_terminal_generation=? WHERE scope_id=?",
                (terminal_generation, scope_id),
            )
            if int(scope["user_message_generation"]) > int(scope["last_user_priority_generation"]):
                store.connection.execute(
                    """
                    UPDATE watcher_scopes
                       SET last_user_priority_generation=user_message_generation
                     WHERE scope_id=?
                    """,
                    (scope_id,),
                )
            if total == 0:
                store.connection.execute(
                    """
                    UPDATE watcher_scopes SET stop_blocked=0,wait_active=0,
                           pending_stop_feedback_digest=NULL,
                           pending_stop_terminal_generation=NULL,
                           pending_stop_wait_generation=NULL
                     WHERE scope_id=?
                    """,
                    (scope_id,),
                )
                return {**common, "status": "allowed", "decision": "allow", "priority": None}
            if not scope["enabled"] or not scope["block_on_obligations"]:
                return {**common, "status": "unavailable", "decision": "allow", "priority": None}
            if scope["allow_stop_once"]:
                store.connection.execute(
                    """
                    UPDATE watcher_scopes SET allow_stop_once=0,stop_blocked=0,
                           pending_stop_feedback_digest=NULL,
                           pending_stop_terminal_generation=NULL,
                           pending_stop_wait_generation=NULL
                     WHERE scope_id=?
                    """,
                    (scope_id,),
                )
                return {
                    **common,
                    "status": "allowed",
                    "decision": "allow",
                    "priority": "explicit_allow_stop_once",
                }
            wait_generation = int(scope["wait_generation"])
            should_block = int(scope["last_blocked_wait_generation"]) < wait_generation or (
                block_on_fresh_terminal and terminal_fresh
            )
            if should_block:
                reason_digest = hashlib.sha256(
                    stop_feedback_reason(actor["callsign"], wait_generation).encode("utf-8")
                ).hexdigest()
                store.connection.execute(
                    """
                    UPDATE watcher_scopes
                       SET last_blocked_wait_generation=?,stop_blocked=1,wait_active=1,
                           pending_stop_feedback_digest=?,
                           pending_stop_terminal_generation=?,
                           pending_stop_wait_generation=?
                     WHERE scope_id=?
                    """,
                    (
                        wait_generation,
                        reason_digest,
                        terminal_generation,
                        wait_generation,
                        scope_id,
                    ),
                )
                return {**common, "status": "blocked_once", "decision": "block", "priority": None}
            store.connection.execute(
                """
                UPDATE watcher_scopes SET stop_blocked=0,wait_active=0,
                       pending_stop_feedback_digest=NULL,
                       pending_stop_terminal_generation=NULL,
                       pending_stop_wait_generation=NULL
                 WHERE scope_id=?
                """,
                (scope_id,),
            )
            return {**common, "status": "allowed", "decision": "allow", "priority": None}
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "Stop decision conflicted with canonical state") from exc
