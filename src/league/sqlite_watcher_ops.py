"""Verified runtime/watcher registration and bounded Shotcaller Stop decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Optional

from .sqlite_request_ops import _time
from .storage_watcher import RuntimeRegistrationCommand
from .storage_types import StorageRefusal


SUPERVISION_MODES = frozenset({"all_material", "calm"})
DEFAULT_UNREACHABLE_GRACE_SECONDS = 60
MAX_CHAMPION_STOP_GUARDS = 64
ATTENTION_STATUSES = frozenset(
    {
        "blocked",
        "failed",
        "unreachable",
        "stale",
        "ready_to_land",
        "ready_for_review",
        "ready_for_merge",
        "ready_for_release",
        "ready_for_install",
        "ready_for_deploy",
        "release_pending",
        "install_pending",
        "deploy_pending",
        "lane_idle",
        "cleanup_pending",
        "cleanup_refused",
        "preservation_ambiguous",
    }
)
ROUTINE_STATUSES = frozenset(
    {
        "accepted", "ack", "acknowledged", "active", "started", "working", "progress",
        "in_progress", "intermediate", "heartbeat", "lease", "liveness", "health",
        "healthy", "delivered", "delivery_acknowledged",
    }
)


def stop_feedback_reason(
    callsign: str, wait_generation: int, summaries: tuple[str, ...] = ()
) -> str:
    base = (
        f"League has unresolved obligations for {callsign} "
        f"at wait generation {wait_generation}."
    )
    if not summaries:
        return base
    return base + " Unresolved requests: " + " | ".join(summaries)


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
    rows = store.connection.execute(
        """
        SELECT scope_id,actor_agent_id FROM watcher_scopes
         WHERE scope_id=? OR actor_agent_id=?
         ORDER BY scope_id LIMIT 2
        """,
        (scope_id, actor_agent_id),
    ).fetchall()
    if not rows:
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
        return
    row = rows[0]
    if (
        len(rows) != 1
        or row["scope_id"] != scope_id
        or row["actor_agent_id"] not in {None, actor_agent_id}
    ):
        raise StorageRefusal(
            "scope_conflict",
            "watcher scope and Shotcaller require one exact binding",
        )
    assignments = (
        "actor_agent_id=?"
        if block_on_obligations is None
        else "actor_agent_id=?,block_on_obligations=?"
    )
    parameters = (
        (actor_agent_id, scope_id, actor_agent_id)
        if block_on_obligations is None
        else (
            actor_agent_id,
            int(block_on_obligations),
            scope_id,
            actor_agent_id,
        )
    )
    changed = store.connection.execute(
        f"""
        UPDATE watcher_scopes SET {assignments}
         WHERE scope_id=? AND (actor_agent_id IS NULL OR actor_agent_id=?)
        """,
        parameters,
    )
    if changed.rowcount != 1:
        raise StorageRefusal(
            "scope_conflict",
            "watcher scope identity changed before exact binding",
            retryable=True,
        )


def _scope_metadata(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(row["metadata_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal(
            "supervision_policy_invalid", "watcher supervision metadata is malformed"
        ) from exc
    if not isinstance(value, dict):
        raise StorageRefusal(
            "supervision_policy_invalid", "watcher supervision metadata is malformed"
        )
    return value


def _policy_from_scope(row: Any) -> dict[str, Any]:
    metadata = _scope_metadata(row)
    raw = metadata.get("supervision", {})
    if not isinstance(raw, dict):
        raise StorageRefusal(
            "supervision_policy_invalid", "watcher supervision policy is malformed"
        )
    mode = raw.get("mode", "all_material")
    grace_seconds = raw.get("unreachable_grace_seconds", DEFAULT_UNREACHABLE_GRACE_SECONDS)
    runtime_state = raw.get("runtime_state", "supervising")
    silent_cursor = raw.get("silent_event_cursor", 0)
    if (
        mode not in SUPERVISION_MODES
        or runtime_state not in {"supervising", "paused"}
        or not isinstance(grace_seconds, int)
        or not 1 <= grace_seconds <= 3600
        or not isinstance(silent_cursor, int)
        or silent_cursor < 0
    ):
        raise StorageRefusal(
            "supervision_policy_invalid", "watcher supervision policy is outside supported bounds"
        )
    return {
        "scope_id": str(row["scope_id"]),
        "actor_agent_id": str(row["actor_agent_id"]),
        "mode": str(mode),
        "runtime_state": str(runtime_state),
        "wake_policy": (
            "normal"
            if mode == "all_material"
            else "calm_paused" if runtime_state == "paused" else "calm"
        ),
        "silent_event_cursor": silent_cursor,
        "unreachable_grace_seconds": grace_seconds,
    }


def supervision_policy(store: Any, actor_agent_id: str) -> dict[str, Any]:
    row = store.connection.execute(
        "SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes WHERE actor_agent_id=? ORDER BY scope_id LIMIT 1",
        (actor_agent_id,),
    ).fetchone()
    if row is None:
        return {
            "scope_id": None,
            "actor_agent_id": actor_agent_id,
            "mode": "all_material",
            "runtime_state": "paused",
            "wake_policy": "normal",
            "silent_event_cursor": 0,
            "unreachable_grace_seconds": DEFAULT_UNREACHABLE_GRACE_SECONDS,
        }
    return _policy_from_scope(row)


def runtime_monitor_candidates(
    store: Any, owner_agent_id: str, *, limit: int = 50
) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise StorageRefusal("invalid_limit", "runtime monitor limit is invalid")
    rows = store.connection.execute(
        """
        SELECT ta.task_assignment_id assignment_id,ta.task_id,ta.request_id,
               ta.coordinator_agent_id,ta.champion_agent_id,ta.runtime_instance_id,
               a.callsign,a.routing_name,a.address,a.thread_id,a.backend,a.worktree,
               r.harness_kind,r.backend_kind,r.session_ref,r.endpoint,
               r.runtime_generation,r.status runtime_status,r.verified,r.last_seen_at
          FROM task_assignments ta
          JOIN agent_instances a ON a.agent_id=ta.champion_agent_id
          JOIN runtime_instances r ON r.runtime_instance_id=ta.runtime_instance_id
         WHERE ta.coordinator_agent_id=? AND ta.state='active'
           AND ta.assignment_role='champion' AND a.retired_at IS NULL
         ORDER BY ta.task_assignment_id LIMIT ?
        """,
        (owner_agent_id, limit + 1),
    ).fetchall()
    return {
        "owner_agent_id": owner_agent_id,
        "returned_count": min(len(rows), limit),
        "truncated": len(rows) > limit,
        "candidates": [dict(row) for row in rows[:limit]],
    }


def record_supervision_fault(
    store: Any,
    owner_agent_id: str,
    fault_kind: str,
    fault_key: str,
    at: str,
) -> dict[str, Any]:
    _time(at, "supervision fault time")
    if fault_kind not in {
        "runtime_observation_refused",
        "runtime_reconciliation_refused",
        "supervisor_lease_loss",
        "supervisor_restart_failure",
    } or not fault_key:
        raise StorageRefusal("supervision_fault_invalid", "supervision fault identity is invalid")
    digest = hashlib.sha256(f"{fault_kind}\0{fault_key}".encode("utf-8")).hexdigest()[:32]
    event_id = f"supervision-fault:{digest}"
    outbox_id = f"outbox:{event_id}"
    try:
        with store._transaction():
            owner = store.connection.execute(
                "SELECT version FROM agent_instances WHERE agent_id=? AND role='shotcaller' AND retired_at IS NULL",
                (owner_agent_id,),
            ).fetchone()
            if owner is None:
                raise StorageRefusal("owner_invalid", "supervision fault owner is not active")
            existing = store.connection.execute(
                "SELECT event_id FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is None:
                store.connection.execute(
                    """
                    INSERT INTO events
                      (event_id,agent_id,task_id,entity_version,event_type,status,update_text,
                       occurred_at,detail_json,aggregate_kind,aggregate_id)
                    VALUES(?,?,NULL,?,'supervision_fault','failed',?,?,?,'agent',?)
                    """,
                    (
                        event_id,
                        owner_agent_id,
                        owner["version"],
                        "Persistent supervision requires owner attention",
                        at,
                        json.dumps(
                            {"attention_required": True, "fault_kind": fault_kind},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        owner_agent_id,
                    ),
                )
                store.connection.execute(
                    """
                    INSERT INTO delivery_outbox
                      (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                    VALUES(?,?,?,'pending',?,0)
                    """,
                    (outbox_id, event_id, owner_agent_id, at),
                )
                store.connection.execute(
                    """
                    INSERT INTO obligations
                      (obligation_id,owner_agent_id,kind,aggregate_id,dedupe_key,state,
                       next_attention_at,details_json,created_at,updated_at)
                    VALUES(?,?,'delivery',?,?,'open',?,'{}',?,?)
                    """,
                    (
                        f"obligation:{outbox_id}", owner_agent_id, outbox_id,
                        f"delivery:{outbox_id}", at, at, at,
                    ),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "supervision fault recording conflicted with canonical state"
        ) from exc
    return {
        "event_id": event_id,
        "outbox_id": outbox_id,
        "recipient_agent_id": owner_agent_id,
        "fault_kind": fault_kind,
        "idempotent": existing is not None,
    }


def configure_supervision_policy(
    store: Any,
    scope_id: str,
    actor_agent_id: str,
    mode: str,
    unreachable_grace_seconds: int,
    at: str,
) -> dict[str, Any]:
    _time(at, "supervision policy time")
    if mode not in SUPERVISION_MODES or not 1 <= unreachable_grace_seconds <= 3600:
        raise StorageRefusal(
            "supervision_policy_invalid", "supervision mode or unreachable grace is invalid"
        )
    try:
        with store._transaction():
            actor = store.connection.execute(
                "SELECT role,retired_at FROM agent_instances WHERE agent_id=?",
                (actor_agent_id,),
            ).fetchone()
            if actor is None or actor["role"] != "shotcaller" or actor["retired_at"] is not None:
                raise StorageRefusal(
                    "owner_invalid", "supervision policy requires one active Shotcaller"
                )
            ensure_watcher_scope(
                store, scope_id, actor_agent_id, block_on_obligations=None
            )
            row = store.connection.execute(
                "SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes WHERE scope_id=?",
                (scope_id,),
            ).fetchone()
            metadata = _scope_metadata(row)
            prior = metadata.get("supervision", {})
            prior_cursor = (
                prior.get("silent_event_cursor", 0) if isinstance(prior, dict) else 0
            )
            prior_runtime_state = (
                prior.get("runtime_state", "supervising")
                if isinstance(prior, dict)
                else "supervising"
            )
            if prior_runtime_state not in {"supervising", "paused"}:
                raise StorageRefusal(
                    "supervision_policy_invalid",
                    "watcher supervision lifecycle state is malformed",
                )
            metadata["supervision"] = {
                "mode": mode,
                "runtime_state": prior_runtime_state,
                "silent_event_cursor": prior_cursor,
                "unreachable_grace_seconds": unreachable_grace_seconds,
                "updated_at": at,
            }
            store.connection.execute(
                "UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?",
                (json.dumps(metadata, sort_keys=True, separators=(",", ":")), scope_id),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "supervision policy update conflicted with canonical state"
        ) from exc
    return {
        "scope_id": scope_id,
        "actor_agent_id": actor_agent_id,
        "mode": mode,
        "runtime_state": prior_runtime_state,
        "wake_policy": (
            "normal"
            if mode == "all_material"
            else "calm" if prior_runtime_state == "supervising" else "calm_paused"
        ),
        "silent_event_cursor": prior_cursor,
        "unreachable_grace_seconds": unreachable_grace_seconds,
    }


def _tracked_champion_lane_idle(store: Any, owner_agent_id: str) -> bool:
    row = store.connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM agent_instances a
            LEFT JOIN tasks t ON t.task_id=a.task_id
           WHERE a.role='champion' AND a.shotcaller_agent_id=? AND a.retired_at IS NULL
             AND a.status IN ('active','started','working','progress','blocked','ready_to_land')
             AND (a.task_id IS NULL OR t.state IN
               ('active','pending','accepted','working','progress','in_progress','blocked','ready_to_land')))
          +
          (SELECT COUNT(*) FROM tasks
           WHERE coordinator_agent_id=? AND state IN
             ('active','pending','accepted','working','progress','in_progress','blocked','ready_to_land'))
          +
          (SELECT COUNT(*) FROM task_assignments
           WHERE coordinator_agent_id=? AND state IN ('pending','launching','cleanup_pending'))
          +
          (SELECT COUNT(*) FROM cleanup_obligations c JOIN tasks t ON t.task_id=c.task_id
           WHERE t.coordinator_agent_id=?
             AND c.cleanup_state NOT IN ('completed','cleanup_completed')) AS active_count
        """,
        (owner_agent_id,) * 4,
    ).fetchone()
    return int(row["active_count"]) == 0


def _attention_reason(store: Any, row: Any) -> Optional[str]:
    try:
        detail = json.loads(str(row["detail_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal(
            "supervision_event_invalid", "delivery event detail is malformed"
        ) from exc
    if not isinstance(detail, dict):
        raise StorageRefusal(
            "supervision_event_invalid", "delivery event detail is malformed"
        )
    event_type = str(row["event_type"] or "")
    status = str(row["status"] or "")
    if detail.get("attention_required") is True:
        return "champion_escalation"
    if event_type == "champion_unreachable":
        return "champion_unreachable"
    if status in ATTENTION_STATUSES:
        return status
    if "cleanup" in event_type or "refusal" in event_type:
        return "cleanup_or_preservation"
    if status in ROUTINE_STATUSES or any(
        marker in event_type for marker in ("heartbeat", "lease", "acknowledg")
    ):
        return None
    is_champion = row["source_role"] == "champion" or event_type == "champion_unreachable"
    if not is_champion:
        return "non_champion_material"
    if status in {"completed", "complete", "cancelled", "rejected"}:
        if row["task_id"] is not None:
            cleanup = store.connection.execute(
                """
                SELECT cleanup_state FROM cleanup_obligations
                 WHERE task_id=?
                   AND cleanup_state NOT IN ('completed','cleanup_completed')
                """,
                (row["task_id"],),
            ).fetchone()
            if cleanup is not None:
                return "cleanup_or_release"
        return (
            "tracked_lane_idle"
            if _tracked_champion_lane_idle(store, str(row["recipient_agent_id"]))
            else None
        )
    return "unclassified_material"


def apply_supervision_delivery_policy(
    store: Any,
    outbox_id: str,
    event_id: str,
    recipient_agent_id: str,
    at: str,
) -> dict[str, Any]:
    """Persist a silent receipt or permit one owner-facing material wake."""

    _time(at, "supervision delivery time")
    try:
        with store._transaction():
            row = store.connection.execute(
                """
                SELECT o.outbox_id,o.event_id,o.recipient_agent_id,o.state,o.last_outcome,
                       e.event_seq,e.event_type,e.status,e.update_text,e.detail_json,e.task_id,
                       COALESCE(e.agent_id,json_extract(e.detail_json,'$.champion_agent_id'))
                         source_agent_id,
                       a.role source_role
                  FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
                  LEFT JOIN agent_instances a ON a.agent_id=COALESCE(
                    e.agent_id,json_extract(e.detail_json,'$.champion_agent_id'))
                 WHERE o.outbox_id=?
                """,
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise StorageRefusal("delivery_unknown", "outbox row does not exist")
            if row["event_id"] != event_id or row["recipient_agent_id"] != recipient_agent_id:
                raise StorageRefusal(
                    "source_event_mismatch", "supervision policy source identity does not match"
                )
            if row["state"] == "cancelled" and row["last_outcome"] == "calm_silent":
                return {
                    "action": "silent",
                    "reason": "calm_silent",
                    "idempotent": True,
                }
            if row["state"] in {"in_flight", "awaiting_receipt"}:
                return {
                    "action": "defer",
                    "reason": "delivery_in_flight",
                    "state": str(row["state"]),
                    "idempotent": True,
                }
            scope = store.connection.execute(
                "SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes WHERE actor_agent_id=?",
                (recipient_agent_id,),
            ).fetchone()
            active_turn = None if scope is None else _shotcaller_turn(_scope_metadata(scope))
            if (
                row["state"] == "pending"
                and active_turn is not None
                and active_turn.get("active") is True
            ):
                return {
                    "action": "defer",
                    "reason": "owner_turn_active",
                    "state": "pending",
                    "idempotent": False,
                }
            policy = supervision_policy(store, recipient_agent_id)
            if policy["mode"] == "all_material" or row["state"] != "pending":
                return {"action": "wake", "reason": "all_material", "idempotent": False}
            reason = _attention_reason(store, row)
            if reason is not None:
                return {"action": "wake", "reason": reason, "idempotent": False}
            store.connection.execute(
                """
                UPDATE delivery_outbox
                   SET state='cancelled',last_outcome='calm_silent',last_attempt_at=?
                 WHERE outbox_id=? AND state='pending'
                """,
                (at, outbox_id),
            )
            store.connection.execute(
                "DELETE FROM outbox_dispatch_leases WHERE outbox_id=?", (outbox_id,)
            )
            store.connection.execute(
                """
                UPDATE obligations SET state='satisfied',updated_at=?
                 WHERE kind='delivery' AND aggregate_id=? AND state='open'
                """,
                (at, outbox_id),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "Calm delivery filtering conflicted with canonical state"
        ) from exc
    return {
        "action": "silent",
        "reason": (
            "calm_paused"
            if policy["runtime_state"] == "paused"
            else "routine_champion_update"
        ),
        "idempotent": False,
    }


def _silent_supervision_updates(
    store: Any,
    actor_agent_id: str,
    *,
    after_event_seq: Optional[int] = None,
    limit: int = 20,
    advance_cursor: bool = False,
    at: Optional[str] = None,
) -> dict[str, Any]:
    policy = supervision_policy(store, actor_agent_id)
    cursor = policy["silent_event_cursor"] if after_event_seq is None else after_event_seq
    rows = store.connection.execute(
        """
        SELECT e.event_seq,e.event_id,e.event_type,e.status,e.update_text,e.occurred_at,
               COALESCE(e.agent_id,json_extract(e.detail_json,'$.champion_agent_id')) source_agent_id
          FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
         WHERE o.recipient_agent_id=? AND o.state='cancelled'
           AND o.last_outcome='calm_silent' AND e.event_seq>?
         ORDER BY e.event_seq LIMIT ?
        """,
        (actor_agent_id, cursor, limit + 1),
    ).fetchall()
    returned = [dict(row) for row in rows[:limit]]
    next_cursor = int(returned[-1]["event_seq"]) if returned else cursor
    if advance_cursor and policy["scope_id"] is not None:
        _time(str(at), "silent reconciliation time")
        scope = store.connection.execute(
            "SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes WHERE scope_id=?",
            (policy["scope_id"],),
        ).fetchone()
        metadata = _scope_metadata(scope)
        supervision = metadata.setdefault("supervision", {})
        if not isinstance(supervision, dict):
            raise StorageRefusal(
                "supervision_policy_invalid", "watcher supervision policy is malformed"
            )
        supervision["silent_event_cursor"] = next_cursor
        supervision["reconciled_at"] = at
        store.connection.execute(
            "UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?",
            (
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                policy["scope_id"],
            ),
        )
    return {
        "actor_agent_id": actor_agent_id,
        "mode": policy["mode"],
        "runtime_state": policy["runtime_state"],
        "wake_policy": policy["wake_policy"],
        "after_event_seq": cursor,
        "returned_count": len(returned),
        "truncated": len(rows) > limit,
        "next_after_event_seq": next_cursor,
        "cursor_advanced": advance_cursor,
        "updates": returned,
    }


def silent_supervision_updates(
    store: Any,
    actor_agent_id: str,
    *,
    after_event_seq: Optional[int] = None,
    limit: int = 20,
    advance_cursor: bool = False,
    at: Optional[str] = None,
) -> dict[str, Any]:
    if (
        (after_event_seq is not None and after_event_seq < 0)
        or not 1 <= limit <= 100
        or (advance_cursor and at is None)
    ):
        raise StorageRefusal("invalid_limit", "silent-update bounds are invalid")
    if not advance_cursor:
        return _silent_supervision_updates(
            store,
            actor_agent_id,
            after_event_seq=after_event_seq,
            limit=limit,
            advance_cursor=False,
            at=at,
        )
    try:
        with store._transaction():
            return _silent_supervision_updates(
                store,
                actor_agent_id,
                after_event_seq=after_event_seq,
                limit=limit,
                advance_cursor=True,
                at=at,
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "silent supervision reconciliation conflicted with canonical state"
        ) from exc


def pause_calm_supervision(
    store: Any,
    actor_agent_id: str,
    watcher_id: str,
    fence: int,
    at: str,
) -> dict[str, Any]:
    """Pause only model-facing Calm wakes while the safety monitor stays leased."""

    _time(at, "Calm pause time")
    try:
        with store._transaction():
            registration = store.connection.execute(
                """
                SELECT w.watcher_id,w.fence,s.scope_id,s.actor_agent_id,s.metadata_json
                  FROM watcher_registrations w
                  JOIN watcher_scopes s ON s.actor_agent_id=w.actor_agent_id
                 WHERE w.actor_agent_id=?
                """,
                (actor_agent_id,),
            ).fetchone()
            if (
                registration is None
                or registration["watcher_id"] != watcher_id
                or int(registration["fence"]) != fence
            ):
                raise StorageRefusal("watcher_fenced", "Calm pause uses a stale supervisor fence")
            policy = _policy_from_scope(registration)
            if policy["mode"] != "calm":
                raise StorageRefusal(
                    "calm_mode_required", "pause is supported only while Calm mode is configured"
                )
            metadata = _scope_metadata(registration)
            supervision = metadata["supervision"]
            supervision["runtime_state"] = "paused"
            supervision["paused_at"] = at
            in_flight_count = int(
                store.connection.execute(
                    """
                    SELECT
                      (
                        SELECT COUNT(*) FROM agent_instances a
                        LEFT JOIN tasks t ON t.task_id=a.task_id
                         WHERE a.role='champion'
                           AND a.shotcaller_agent_id=?
                           AND a.retired_at IS NULL
                           AND a.status IN
                             ('active','started','working','progress','blocked','ready_to_land')
                           AND (
                             a.task_id IS NULL
                             OR t.state IN
                               ('active','pending','accepted','working','progress','in_progress','blocked','ready_to_land')
                           )
                      )
                      +
                      (
                        SELECT COUNT(*) FROM tasks t
                         WHERE t.coordinator_agent_id=?
                           AND t.state IN
                             ('active','pending','accepted','working','progress','in_progress','blocked','ready_to_land')
                           AND NOT EXISTS (
                             SELECT 1 FROM agent_instances a
                              WHERE a.task_id=t.task_id
                                AND a.role='champion'
                                AND a.shotcaller_agent_id=?
                                AND a.retired_at IS NULL
                                AND a.status IN
                                  ('active','started','working','progress','blocked','ready_to_land')
                           )
                      )
                      +
                      (SELECT COUNT(*) FROM task_assignments
                        WHERE coordinator_agent_id=?
                          AND state IN ('pending','launching','cleanup_pending'))
                    """,
                    (actor_agent_id,) * 4,
                ).fetchone()[0]
            )
            store.connection.execute(
                """
                UPDATE watcher_scopes
                   SET wait_active=0,stop_blocked=0,allow_stop_once=0,metadata_json=?
                 WHERE scope_id=?
                """,
                (
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    registration["scope_id"],
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "Calm pause conflicted with canonical supervision state"
        ) from exc
    return {
        "actor_agent_id": actor_agent_id,
        "scope_id": str(registration["scope_id"]),
        "mode": "calm",
        "runtime_state": "paused",
        "wake_policy": "calm_paused",
        "allow_stop_once": False,
        "in_flight_count": in_flight_count,
        "hooks_changed": False,
        "monitor_live": True,
        "fence": fence,
    }


def resume_calm_supervision(
    store: Any,
    actor_agent_id: str,
    watcher_id: str,
    fence: int,
    at: str,
) -> dict[str, Any]:
    """Resume Calm wakes without replacing the persistent runtime monitor."""

    _time(at, "Calm resume time")
    try:
        with store._transaction():
            registration = store.connection.execute(
                """
                SELECT w.watcher_id,w.fence,s.scope_id,s.actor_agent_id,s.metadata_json
                  FROM watcher_registrations w
                  JOIN watcher_scopes s ON s.actor_agent_id=w.actor_agent_id
                 WHERE w.actor_agent_id=?
                """,
                (actor_agent_id,),
            ).fetchone()
            if (
                registration is None
                or registration["watcher_id"] != watcher_id
                or int(registration["fence"]) != fence
            ):
                raise StorageRefusal("watcher_fenced", "Calm resume uses a stale monitor fence")
            policy = _policy_from_scope(registration)
            if policy["mode"] != "calm":
                raise StorageRefusal(
                    "calm_mode_required", "resume is supported only while Calm mode is configured"
                )
            metadata = _scope_metadata(registration)
            metadata["supervision"]["runtime_state"] = "supervising"
            metadata["supervision"]["resumed_at"] = at
            counts = _obligation_counts(store, actor_agent_id)
            store.connection.execute(
                "UPDATE watcher_scopes SET wait_active=?,metadata_json=? WHERE scope_id=?",
                (
                    int(sum(counts.values()) > 0),
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    registration["scope_id"],
                ),
            )
            reconciliation = _silent_supervision_updates(
                store, actor_agent_id, limit=20, advance_cursor=True, at=at
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "Calm resume conflicted with canonical supervision state"
        ) from exc
    return {
        "actor_agent_id": actor_agent_id,
        "scope_id": str(registration["scope_id"]),
        "mode": "calm",
        "runtime_state": "supervising",
        "wake_policy": "calm",
        "silent_reconciliation": reconciliation,
        "hooks_changed": False,
        "monitor_live": True,
        "fence": fence,
    }


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
            resuming = existing is None or _time(
                str(existing["leased_until"]), "stored watcher lease"
            ) <= now
            renewing = bool(
                existing is not None
                and not resuming
                and existing["watcher_id"] == watcher_id
                and existing["runtime_instance_id"] == runtime_instance_id
                and existing["wake_locator"] == wake_locator
            )
            if existing is not None and int(existing["fence"]) >= fence:
                exact = (
                    int(existing["fence"]) == fence
                    and existing["watcher_id"] == watcher_id
                    and existing["runtime_instance_id"] == runtime_instance_id
                    and existing["wake_locator"] == wake_locator
                    and existing["leased_until"] == leased_until
                )
                if exact:
                    policy = supervision_policy(store, actor_agent_id)
                    return {
                        "watcher_id": watcher_id,
                        "actor_agent_id": actor_agent_id,
                        "fence": fence,
                        "supervision_status": "armed",
                        "mode": policy["mode"],
                        "runtime_state": policy["runtime_state"],
                        "wake_policy": policy["wake_policy"],
                        "silent_reconciliation": None,
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
            if not renewing:
                store.connection.execute(
                    """
                    UPDATE watcher_scopes
                       SET generation=generation+1,wait_generation=wait_generation+1,wait_active=1
                     WHERE scope_id=?
                    """,
                    (scope_id,),
                )
            policy = supervision_policy(store, actor_agent_id)
            reconciliation = (
                _silent_supervision_updates(
                    store, actor_agent_id, limit=20, advance_cursor=True, at=at
                )
                if (
                    policy["mode"] == "calm"
                    and policy["runtime_state"] == "supervising"
                    and resuming
                )
                else None
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
        "mode": policy["mode"],
        "runtime_state": policy["runtime_state"],
        "wake_policy": policy["wake_policy"],
        "silent_reconciliation": reconciliation,
        "idempotent": False,
    }


def supervisor_bindings(
    store: Any, *, limit: int = 64
) -> tuple[dict[str, Any], ...]:
    if not 1 <= limit <= 256:
        raise StorageRefusal(
            "invalid_limit", "supervisor binding limit must be between 1 and 256"
        )
    owners = store.connection.execute(
        """
        SELECT s.squad_id,a.agent_id,a.callsign,a.routing_name
          FROM squads s
          JOIN agent_instances a ON a.agent_id=s.shotcaller_agent_id
         WHERE s.state='active' AND a.role='shotcaller' AND a.retired_at IS NULL
         ORDER BY s.squad_id,a.agent_id
         LIMIT ?
        """,
        (limit + 1,),
    ).fetchall()
    if len(owners) > limit:
        raise StorageRefusal(
            "supervisor_binding_capacity",
            "active Shotcaller bindings exceed the supported service bound",
        )
    owner_ids = [str(row["agent_id"]) for row in owners]
    if len(set(owner_ids)) != len(owner_ids):
        raise StorageRefusal(
            "supervisor_binding_invalid",
            "one Shotcaller cannot own multiple active Squad bindings",
        )
    if not owners:
        return ()
    placeholders = ",".join("?" for _ in owner_ids)
    runtime_rows = store.connection.execute(
        f"""
        SELECT actor_agent_id,runtime_instance_id,runtime_generation,
               backend_kind,endpoint,session_ref
          FROM runtime_instances
         WHERE actor_agent_id IN ({placeholders})
           AND status IN ('active','idle') AND verified=1
         ORDER BY actor_agent_id,runtime_instance_id
        """,
        owner_ids,
    ).fetchall()
    scope_rows = store.connection.execute(
        f"""
        SELECT actor_agent_id,scope_id,generation FROM watcher_scopes
         WHERE actor_agent_id IN ({placeholders})
         ORDER BY actor_agent_id,scope_id
        """,
        owner_ids,
    ).fetchall()
    runtimes_by_owner: dict[str, list[Any]] = {owner_id: [] for owner_id in owner_ids}
    scopes_by_owner: dict[str, list[Any]] = {owner_id: [] for owner_id in owner_ids}
    for runtime in runtime_rows:
        runtimes_by_owner[str(runtime["actor_agent_id"])].append(runtime)
    for scope in scope_rows:
        scopes_by_owner[str(scope["actor_agent_id"])].append(scope)
    bindings: list[dict[str, Any]] = []
    for row in owners:
        owner_id = str(row["agent_id"])
        runtimes = runtimes_by_owner[owner_id]
        if len(runtimes) != 1:
            raise StorageRefusal(
                "supervisor_binding_invalid",
                "each active Squad requires one exact verified Shotcaller runtime",
            )
        runtime = runtimes[0]
        scopes = scopes_by_owner[owner_id]
        if len(scopes) > 1:
            raise StorageRefusal(
                "supervisor_binding_invalid",
                "each Shotcaller requires one exact watcher scope",
            )
        bindings.append(
            {
                "squad_id": str(row["squad_id"]),
                "actor_agent_id": str(row["agent_id"]),
                "callsign": str(row["callsign"]),
                "routing_name": row["routing_name"],
                "runtime_instance_id": str(runtime["runtime_instance_id"]),
                "runtime_generation": str(runtime["runtime_generation"]),
                "backend_kind": str(runtime["backend_kind"]),
                "endpoint": str(runtime["endpoint"]),
                "session_ref": str(runtime["session_ref"]),
                "scope_id": (
                    str(scopes[0]["scope_id"])
                    if scopes
                    else f"watcher:{row['callsign']}"
                ),
                "fence_floor": int(scopes[0]["generation"]) if scopes else 0,
            }
        )
    return tuple(bindings)


def supervisor_binding(store: Any, callsign: Optional[str] = None) -> dict[str, Any]:
    bindings = tuple(
        binding
        for binding in supervisor_bindings(store)
        if callsign is None or binding["callsign"] == callsign
    )
    if len(bindings) != 1:
        raise StorageRefusal(
            "supervisor_binding_invalid",
            "persistent supervision requires one exact verified Shotcaller runtime",
        )
    return bindings[0]


def supervision_owner(store: Any, actor_agent_id: str) -> Optional[str]:
    row = store.connection.execute(
        """
        SELECT role,shotcaller_agent_id FROM agent_instances
         WHERE agent_id=? AND retired_at IS NULL
        """,
        (actor_agent_id,),
    ).fetchone()
    if row is None:
        return None
    if row["role"] == "shotcaller":
        return actor_agent_id
    return (
        str(row["shotcaller_agent_id"])
        if row["shotcaller_agent_id"] is not None
        else None
    )


def _shotcaller_turn(metadata: dict[str, Any]) -> dict[str, Any] | None:
    value = metadata.get("shotcaller_turn")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise StorageRefusal(
            "shotcaller_turn_invalid", "Shotcaller turn metadata is malformed"
        )
    return value


def begin_shotcaller_turn(
    store: Any, actor_agent_id: str, turn_token: str, at: str
) -> dict[str, Any]:
    _time(at, "Shotcaller turn begin time")
    if not actor_agent_id or not 1 <= len(turn_token.encode("utf-8")) <= 256:
        raise StorageRefusal(
            "shotcaller_turn_invalid", "Shotcaller turn identity is incomplete"
        )
    token_digest = hashlib.sha256(turn_token.encode("utf-8")).hexdigest()
    try:
        with store._transaction():
            actor = store.connection.execute(
                """
                SELECT a.callsign FROM agent_instances a JOIN squads s
                  ON s.shotcaller_agent_id=a.agent_id
                 WHERE a.agent_id=? AND a.role='shotcaller' AND a.retired_at IS NULL
                   AND s.state='active'
                """,
                (actor_agent_id,),
            ).fetchone()
            if actor is None:
                raise StorageRefusal(
                    "shotcaller_turn_invalid",
                    "Shotcaller turn requires one active Squad owner",
                )
            scopes = store.connection.execute(
                """
                SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes
                 WHERE actor_agent_id=? ORDER BY scope_id LIMIT 2
                """,
                (actor_agent_id,),
            ).fetchall()
            if len(scopes) > 1:
                raise StorageRefusal(
                    "shotcaller_turn_conflict",
                    "Shotcaller turn requires one exact owner scope",
                )
            scope_id = (
                str(scopes[0]["scope_id"])
                if scopes
                else f"watcher:{actor['callsign']}"
            )
            ensure_watcher_scope(
                store, scope_id, actor_agent_id, block_on_obligations=None
            )
            scope = store.connection.execute(
                """
                SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes
                 WHERE scope_id=? AND actor_agent_id=?
                """,
                (scope_id, actor_agent_id),
            ).fetchone()
            if scope is None:
                raise StorageRefusal(
                    "shotcaller_turn_conflict",
                    "Shotcaller turn begin requires one exact owner scope",
                )
            metadata = _scope_metadata(scope)
            existing = _shotcaller_turn(metadata)
            if existing is not None and existing.get("active") is True:
                if existing.get("token_digest") != token_digest:
                    raise StorageRefusal(
                        "shotcaller_turn_active",
                        "another Shotcaller turn is already active",
                        retryable=True,
                    )
                return {
                    "actor_agent_id": actor_agent_id,
                    "scope_id": str(scope["scope_id"]),
                    "active": True,
                    "committed": existing.get("committed") is True,
                    "idempotent": True,
                }
            metadata["shotcaller_turn"] = {
                "active": True,
                "committed": False,
                "token_digest": token_digest,
                "opened_at": at,
            }
            store.connection.execute(
                "UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?",
                (
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    scope["scope_id"],
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "Shotcaller turn begin conflicted with canonical state"
        ) from exc
    return {
        "actor_agent_id": actor_agent_id,
        "scope_id": str(scope["scope_id"]),
        "active": True,
        "committed": False,
        "idempotent": False,
    }


def commit_shotcaller_turn(
    store: Any, actor_agent_id: str, turn_token: str, at: str
) -> dict[str, Any]:
    _time(at, "Shotcaller turn commit time")
    token_digest = hashlib.sha256(turn_token.encode("utf-8")).hexdigest()
    try:
        with store._transaction():
            scope = store.connection.execute(
                "SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes WHERE actor_agent_id=?",
                (actor_agent_id,),
            ).fetchone()
            metadata = _scope_metadata(scope)
            active = _shotcaller_turn(metadata)
            if (
                active is None
                or active.get("active") is not True
                or active.get("token_digest") != token_digest
            ):
                raise StorageRefusal(
                    "shotcaller_turn_conflict",
                    "Shotcaller turn commit does not match the active turn",
                )
            if active.get("committed") is True:
                return {
                    "actor_agent_id": actor_agent_id,
                    "scope_id": str(scope["scope_id"]),
                    "active": True,
                    "committed": True,
                    "idempotent": True,
                }
            active["committed"] = True
            active["committed_at"] = at
            store.connection.execute(
                "UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?",
                (
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    scope["scope_id"],
                ),
            )
    except StorageRefusal:
        raise
    except (AttributeError, sqlite3.DatabaseError) as exc:
        if isinstance(exc, sqlite3.DatabaseError):
            raise store._translate_database_error(
                exc, "Shotcaller turn commit conflicted with canonical state"
            ) from exc
        raise StorageRefusal(
            "shotcaller_turn_invalid", "Shotcaller turn identity is incomplete"
        ) from exc
    return {
        "actor_agent_id": actor_agent_id,
        "scope_id": str(scope["scope_id"]),
        "active": True,
        "committed": True,
        "idempotent": False,
    }


def abort_shotcaller_turn(
    store: Any, actor_agent_id: str, turn_token: str, at: str
) -> dict[str, Any]:
    _time(at, "Shotcaller turn abort time")
    token_digest = hashlib.sha256(turn_token.encode("utf-8")).hexdigest()
    try:
        with store._transaction():
            scope = store.connection.execute(
                "SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes WHERE actor_agent_id=?",
                (actor_agent_id,),
            ).fetchone()
            if scope is None:
                raise StorageRefusal(
                    "shotcaller_turn_conflict",
                    "Shotcaller turn abort requires an active turn scope",
                )
            metadata = _scope_metadata(scope)
            active = _shotcaller_turn(metadata)
            if (
                active is None
                or active.get("active") is not True
                or active.get("token_digest") != token_digest
            ):
                raise StorageRefusal(
                    "shotcaller_turn_conflict",
                    "Shotcaller turn abort does not match the active turn",
                )
            if active.get("committed") is True:
                raise StorageRefusal(
                    "shotcaller_turn_conflict",
                    "a committed Shotcaller turn cannot be aborted",
                )
            active["active"] = False
            active["aborted_at"] = at
            store.connection.execute(
                "UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?",
                (
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    scope["scope_id"],
                ),
            )
    except StorageRefusal:
        raise
    except (AttributeError, sqlite3.DatabaseError) as exc:
        if isinstance(exc, sqlite3.DatabaseError):
            raise store._translate_database_error(
                exc, "Shotcaller turn abort conflicted with canonical state"
            ) from exc
        raise StorageRefusal(
            "shotcaller_turn_invalid", "Shotcaller turn identity is incomplete"
        ) from exc
    return {
        "actor_agent_id": actor_agent_id,
        "scope_id": str(scope["scope_id"]),
        "active": False,
        "committed": False,
        "idempotent": False,
    }


def watcher_registration(
    store: Any, actor_agent_id: str
) -> Optional[dict[str, Any]]:
    row = store.connection.execute(
        """
        SELECT watcher_id,actor_agent_id,runtime_instance_id,wake_locator,
               leased_until,fence,registered_at
          FROM watcher_registrations
         WHERE actor_agent_id=?
        """,
        (actor_agent_id,),
    ).fetchone()
    return None if row is None else dict(row)


def watcher_registrations(
    store: Any, actor_agent_ids: tuple[str, ...], *, limit: int = 64
) -> dict[str, dict[str, Any]]:
    """Batch one bounded registration snapshot for aggregate service status."""

    if (
        not 0 <= len(actor_agent_ids) <= limit <= 256
        or len(set(actor_agent_ids)) != len(actor_agent_ids)
        or any(not actor_agent_id for actor_agent_id in actor_agent_ids)
    ):
        raise StorageRefusal(
            "supervisor_binding_invalid",
            "aggregate watcher status requires unique bounded actor identities",
        )
    if not actor_agent_ids:
        return {}
    placeholders = ",".join("?" for _ in actor_agent_ids)
    rows = store.connection.execute(
        f"""
        SELECT watcher_id,actor_agent_id,runtime_instance_id,wake_locator,
               leased_until,fence,registered_at
          FROM watcher_registrations
         WHERE actor_agent_id IN ({placeholders})
         ORDER BY actor_agent_id
        """,
        actor_agent_ids,
    ).fetchall()
    return {str(row["actor_agent_id"]): dict(row) for row in rows}


def watcher_readiness(
    store: Any, actor_agent_id: str
) -> Optional[dict[str, Any]]:
    """Return one bounded registration/scope readiness observation."""

    row = store.connection.execute(
        """
        SELECT w.watcher_id,w.actor_agent_id,w.runtime_instance_id,w.wake_locator,
               w.leased_until,w.fence,s.wait_active,s.wait_generation
          FROM watcher_registrations w
          JOIN watcher_scopes s ON s.actor_agent_id=w.actor_agent_id
         WHERE w.actor_agent_id=?
         LIMIT 1
        """,
        (actor_agent_id,),
    ).fetchone()
    return None if row is None else dict(row)


def release_watcher(
    store: Any,
    watcher_id: str,
    actor_agent_id: str,
    fence: int,
    at: str,
) -> dict[str, Any]:
    _time(at, "watcher release time")
    if not watcher_id or not actor_agent_id or fence < 1:
        raise StorageRefusal("invalid_watcher", "watcher release identity is incomplete")
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT watcher_id,fence FROM watcher_registrations WHERE actor_agent_id=?",
                (actor_agent_id,),
            ).fetchone()
            if row is None:
                return {
                    "watcher_id": watcher_id,
                    "actor_agent_id": actor_agent_id,
                    "fence": fence,
                    "supervision_status": "stopped",
                    "idempotent": True,
                }
            if row["watcher_id"] != watcher_id or int(row["fence"]) != fence:
                raise StorageRefusal(
                    "watcher_fenced", "watcher release identity is stale"
                )
            store.connection.execute(
                "DELETE FROM watcher_registrations WHERE actor_agent_id=?",
                (actor_agent_id,),
            )
            scope = store.connection.execute(
                "SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes WHERE actor_agent_id=?",
                (actor_agent_id,),
            ).fetchone()
            metadata = _scope_metadata(scope)
            store.connection.execute(
                "UPDATE watcher_scopes SET wait_active=0,metadata_json=? WHERE actor_agent_id=?",
                (
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    actor_agent_id,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "watcher release conflicted with canonical state"
        ) from exc
    return {
        "watcher_id": watcher_id,
        "actor_agent_id": actor_agent_id,
        "fence": fence,
        "supervision_status": "stopped",
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


def champion_stop_decision(
    store: Any,
    champion_agent_id: str,
    terminal_generation: str,
    at: str,
) -> dict[str, Any]:
    """Require one fresh durable transition before a Champion with active work stops."""

    _time(at, "Champion Stop decision time")
    if not terminal_generation:
        raise StorageRefusal("invalid_stop", "Champion Stop requires a terminal generation")
    try:
        with store._transaction():
            champion = store.connection.execute(
                """
                SELECT a.callsign,a.shotcaller_agent_id,a.task_id,t.summary,t.state
                  FROM agent_instances a LEFT JOIN tasks t ON t.task_id=a.task_id
                 WHERE a.agent_id=? AND a.role='champion' AND a.retired_at IS NULL
                """,
                (champion_agent_id,),
            ).fetchone()
            active_states = {
                "active", "pending", "accepted", "working", "progress",
                "in_progress", "blocked", "ready_to_land",
            }
            if (
                champion is None
                or champion["task_id"] is None
                or champion["state"] not in active_states
                or champion["shotcaller_agent_id"] is None
            ):
                return {
                    "decision": "allow",
                    "status": "no_active_work",
                    "champion_agent_id": champion_agent_id,
                }
            owner_agent_id = str(champion["shotcaller_agent_id"])
            scope = store.connection.execute(
                "SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes WHERE actor_agent_id=? ORDER BY scope_id LIMIT 1",
                (owner_agent_id,),
            ).fetchone()
            if scope is None:
                scope_id = f"watcher:{owner_agent_id}"
                ensure_watcher_scope(
                    store, scope_id, owner_agent_id, block_on_obligations=None
                )
                scope = store.connection.execute(
                    "SELECT scope_id,actor_agent_id,metadata_json FROM watcher_scopes WHERE scope_id=?",
                    (scope_id,),
                ).fetchone()
            metadata = _scope_metadata(scope)
            guards = metadata.setdefault("champion_stop_guards", {})
            if not isinstance(guards, dict):
                raise StorageRefusal(
                    "champion_stop_invalid", "Champion Stop guard metadata is malformed"
                )
            latest = store.connection.execute(
                """
                SELECT COALESCE(MAX(event_seq),0) event_seq FROM events
                 WHERE (task_id=? OR agent_id=?)
                   AND event_type IN ('task_transition','agent_transition')
                """,
                (champion["task_id"], champion_agent_id),
            ).fetchone()
            latest_seq = int(latest["event_seq"])
            previous = guards.get(champion_agent_id)
            if previous is not None and not isinstance(previous, dict):
                raise StorageRefusal(
                    "champion_stop_invalid", "Champion Stop guard metadata is malformed"
                )
            previous_seq = -1 if previous is None else int(previous.get("last_event_seq", -1))
            previous_terminal = None if previous is None else previous.get("terminal_generation")
            if previous is not None and latest_seq > previous_seq:
                decision = "allow"
                status = "fresh_transition"
            elif previous_terminal == terminal_generation:
                decision = "allow"
                status = "blocked_once"
            else:
                decision = "block"
                status = "transition_required"
            if champion_agent_id not in guards and len(guards) >= MAX_CHAMPION_STOP_GUARDS:
                raise StorageRefusal(
                    "champion_stop_capacity",
                    "Champion Stop guard capacity is exhausted; reconcile inactive Champions first",
                )
            guards[champion_agent_id] = {
                "last_event_seq": latest_seq,
                "terminal_generation": terminal_generation,
                "updated_at": at,
            }
            store.connection.execute(
                "UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?",
                (
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    scope["scope_id"],
                ),
            )
    except StorageRefusal:
        raise
    except (TypeError, ValueError, sqlite3.DatabaseError) as exc:
        if isinstance(exc, sqlite3.DatabaseError):
            raise store._translate_database_error(
                exc, "Champion Stop guard conflicted with canonical state"
            ) from exc
        raise StorageRefusal(
            "champion_stop_invalid", "Champion Stop guard metadata is malformed"
        ) from exc
    return {
        "decision": decision,
        "status": status,
        "champion_agent_id": champion_agent_id,
        "callsign": str(champion["callsign"]),
        "task_id": str(champion["task_id"]),
        "task_summary": " ".join(str(champion["summary"]).split())[:160],
        "latest_transition_event_seq": latest_seq,
    }


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


def _paused_actionable_counts(store: Any, actor_agent_id: str) -> dict[str, int]:
    row = store.connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM prompts
            WHERE current_owner_agent_id=? AND triage_state='untriaged') untriaged_prompts,
          (SELECT COUNT(*) FROM requests r
            WHERE r.owner_agent_id=? AND r.state NOT IN ('answered','cancelled')
              AND (
                r.state IN ('awaiting_user','blocked')
                OR NOT EXISTS (
                  SELECT 1 FROM tasks t
                   WHERE t.request_id=r.request_id
                     AND t.state IN
                       ('active','pending','accepted','working','progress','in_progress','blocked','ready_to_land')
                )
              )) owner_decisions,
          (SELECT COUNT(*) FROM delivery_outbox
            WHERE recipient_agent_id=? AND state='pending'
              AND attempt_count>0 AND last_outcome IS NOT NULL
              AND last_outcome!='calm_silent') failed_deliveries,
          (SELECT COUNT(*) FROM cleanup_obligations c JOIN tasks t ON t.task_id=c.task_id
            WHERE t.coordinator_agent_id=?
              AND c.cleanup_state IN ('awaiting_authority','blocked')) cleanup_decisions
        """,
        (actor_agent_id,) * 4,
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
            policy = _policy_from_scope(scope)
            turn = _shotcaller_turn(_scope_metadata(scope))
            turn_active = turn is not None and turn.get("active") is True
            paused_calm = (
                policy["mode"] == "calm" and policy["runtime_state"] == "paused"
            )
            immediate_counts = (
                _paused_actionable_counts(store, actor_agent_id)
                if turn_active or paused_calm
                else {}
            )
            if (
                turn_active
                and turn is not None
                and turn.get("committed") is not True
            ):
                immediate_counts["turn_commit_pending"] = 1
            if (
                turn_active
                and turn is not None
                and turn.get("committed") is True
                and policy["runtime_state"] == "supervising"
                and sum(immediate_counts.values()) == 0
            ):
                registration = store.connection.execute(
                    """
                    SELECT w.watcher_id,w.runtime_instance_id,w.leased_until,w.fence,
                           r.status,r.verified
                      FROM watcher_registrations w JOIN runtime_instances r
                        ON r.runtime_instance_id=w.runtime_instance_id
                     WHERE w.actor_agent_id=?
                    """,
                    (actor_agent_id,),
                ).fetchone()
                if (
                    registration is not None
                    and _time(str(registration["leased_until"]), "watcher lease")
                    > _time(at, "Stop decision time")
                    and registration["status"] in {"active", "idle"}
                    and registration["verified"]
                ):
                    metadata = _scope_metadata(scope)
                    active_turn = _shotcaller_turn(metadata)
                    assert active_turn is not None
                    active_turn["active"] = False
                    active_turn["handed_off_at"] = at
                    store.connection.execute(
                        """
                        UPDATE watcher_scopes
                           SET stop_blocked=0,wait_active=1,last_terminal_generation=?,
                               pending_stop_feedback_digest=NULL,
                               pending_stop_terminal_generation=NULL,
                               pending_stop_wait_generation=NULL,metadata_json=?
                         WHERE scope_id=?
                        """,
                        (
                            terminal_generation,
                            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                            scope_id,
                        ),
                    )
                    return {
                        "scope_id": scope_id,
                        "status": "supervision_handoff",
                        "decision": "allow",
                        "priority": "supervisor",
                        "wait_generation": int(scope["wait_generation"]),
                        "terminal_fresh": terminal_fresh,
                        "obligations": _obligation_counts(store, actor_agent_id),
                        "immediate_actions": immediate_counts,
                        "supervision_mode": policy["mode"],
                        "supervision_state": policy["runtime_state"],
                        "unresolved_summaries": [],
                        "supervision_handoff": True,
                    }
            effective_counts = (
                immediate_counts
                if paused_calm
                else _obligation_counts(store, actor_agent_id)
            )
            if turn_active and turn is not None and turn.get("committed") is not True:
                effective_counts["turn_commit_pending"] = 1
            total = sum(effective_counts.values())
            if effective_counts.get("unresolved_requests", 0) or effective_counts.get(
                "owner_decisions", 0
            ):
                summary_rows = store.connection.execute(
                    """
                    SELECT summary FROM requests
                     WHERE owner_agent_id=? AND state NOT IN ('answered','cancelled')
                     ORDER BY updated_at DESC,request_id LIMIT 10
                    """,
                    (actor_agent_id,),
                ).fetchall()
                summaries = tuple(
                    " ".join(str(row["summary"]).split())[:160]
                    for row in summary_rows
                )
            else:
                summaries = ()
            common = {
                "scope_id": scope_id,
                "wait_generation": int(scope["wait_generation"]),
                "terminal_fresh": terminal_fresh,
                "obligations": effective_counts,
                "supervision_mode": policy["mode"],
                "supervision_state": policy["runtime_state"],
                "unresolved_summaries": list(summaries),
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
            # A provider terminal identifier scopes Stop retries; it is not a
            # user-event identity.  Only a durable wait rearm (including a
            # newly captured prompt event) earns another one-shot block.
            should_block = int(scope["last_blocked_wait_generation"]) < wait_generation
            if should_block:
                reason_digest = hashlib.sha256(
                    stop_feedback_reason(
                        actor["callsign"], wait_generation, summaries
                    ).encode("utf-8")
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
