"""Source-bound outbox dispatch, acknowledgement, and fair backlog operations."""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from .sqlite_request_ops import _time
from .storage_outbox import OutboxDispatchIdentity
from .storage_types import StorageRefusal


def _reconcile_delivered(store: Any, outbox_id: str, received_at: str) -> None:
    store.connection.execute(
        "UPDATE delivery_outbox SET state='delivered',delivered_at=? WHERE outbox_id=?",
        (received_at, outbox_id),
    )
    store.connection.execute(
        "DELETE FROM outbox_dispatch_leases WHERE outbox_id=?", (outbox_id,)
    )


def claim_outbox(
    store: Any,
    identity: OutboxDispatchIdentity,
    lease_expires_at: str,
    at: str,
) -> dict[str, Any]:
    outbox_id = identity.outbox_id
    event_id = identity.event_id
    recipient_agent_id = identity.recipient_agent_id
    dispatcher_id = identity.dispatcher_id
    attempt_id = identity.attempt_id
    now = _time(at, "dispatch claim time")
    if _time(lease_expires_at, "dispatch lease expiry") <= now:
        raise StorageRefusal("invalid_delivery", "dispatch lease expiry must be in the future")
    if not all((outbox_id, event_id, recipient_agent_id, dispatcher_id, attempt_id)):
        raise StorageRefusal("invalid_delivery", "source-bound dispatch identity is incomplete")
    try:
        with store._transaction():
            outbox = store.connection.execute(
                "SELECT * FROM delivery_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if outbox is None:
                raise StorageRefusal("delivery_unknown", "outbox row does not exist")
            if outbox["event_id"] != event_id or outbox["recipient_agent_id"] != recipient_agent_id:
                raise StorageRefusal(
                    "source_event_mismatch",
                    "dispatch source event or recipient does not match the exact outbox row",
                )
            receipt = store.connection.execute(
                "SELECT * FROM recipient_receipts WHERE event_id=? AND recipient_agent_id=?",
                (event_id, recipient_agent_id),
            ).fetchone()
            if receipt is not None:
                if outbox["state"] != "delivered":
                    _reconcile_delivered(store, outbox_id, receipt["received_at"])
                return {
                    "outbox_id": outbox_id,
                    "event_id": event_id,
                    "recipient_agent_id": recipient_agent_id,
                    "state": "delivered",
                    "fence": None,
                    "attempt": int(outbox["attempt_count"]),
                    "idempotent": True,
                }
            if outbox["state"] == "cancelled":
                raise StorageRefusal("delivery_conflict", "cancelled outbox cannot be dispatched")
            if _time(str(outbox["available_at"]), "outbox availability") > now:
                raise StorageRefusal("delivery_not_due", "outbox row is not yet due", retryable=True)
            lease = store.connection.execute(
                "SELECT * FROM outbox_dispatch_leases WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if lease is not None and _time(str(lease["leased_until"]), "stored dispatch lease") > now:
                raise StorageRefusal("delivery_claimed", "outbox has an unexpired dispatch lease")
            fence = 1 if lease is None else int(lease["fence"]) + 1
            store.connection.execute(
                """
                INSERT INTO outbox_dispatch_leases(outbox_id,dispatcher_id,leased_until,fence)
                VALUES(?,?,?,?)
                ON CONFLICT(outbox_id) DO UPDATE SET
                  dispatcher_id=excluded.dispatcher_id,
                  leased_until=excluded.leased_until,
                  fence=excluded.fence
                """,
                (outbox_id, dispatcher_id, lease_expires_at, fence),
            )
            attempt_count = int(outbox["attempt_count"]) + 1
            store.connection.execute(
                """
                UPDATE delivery_outbox
                   SET state='in_flight',attempt_count=?,
                       first_attempt_at=COALESCE(first_attempt_at,?),last_attempt_at=?
                 WHERE outbox_id=?
                """,
                (attempt_count, at, at, outbox_id),
            )
            store.connection.execute(
                """
                INSERT INTO delivery_attempts
                  (attempt_id,outbox_id,adapter_kind,started_at,finished_at,outcome)
                VALUES(?,?,'unselected',?,NULL,NULL)
                """,
                (attempt_id, outbox_id, at),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "outbox claim conflicted with canonical state") from exc
    return {
        "outbox_id": outbox_id,
        "event_id": event_id,
        "recipient_agent_id": recipient_agent_id,
        "state": "in_flight",
        "fence": fence,
        "attempt": attempt_count,
        "attempt_id": attempt_id,
        "idempotent": False,
    }


def acknowledge_outbox(
    store: Any,
    identity: OutboxDispatchIdentity,
    fence: int,
    adapter_kind: str,
    effect_kind: str,
    effect_id: str,
    at: str,
) -> dict[str, Any]:
    outbox_id = identity.outbox_id
    event_id = identity.event_id
    recipient_agent_id = identity.recipient_agent_id
    dispatcher_id = identity.dispatcher_id
    attempt_id = identity.attempt_id
    _time(at, "recipient acknowledgement time")
    if fence < 1 or not all((adapter_kind, effect_kind, effect_id)):
        raise StorageRefusal("invalid_delivery", "acknowledgement receipt is incomplete")
    try:
        with store._transaction():
            outbox = store.connection.execute(
                "SELECT * FROM delivery_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if outbox is None or outbox["event_id"] != event_id or outbox["recipient_agent_id"] != recipient_agent_id:
                raise StorageRefusal("source_event_mismatch", "acknowledgement does not match its outbox")
            existing = store.connection.execute(
                "SELECT * FROM recipient_receipts WHERE event_id=? AND recipient_agent_id=?",
                (event_id, recipient_agent_id),
            ).fetchone()
            if existing is not None:
                if existing["effect_kind"] != effect_kind or existing["effect_id"] != effect_id:
                    raise StorageRefusal("receipt_conflict", "duplicate delivery has a different recipient effect")
                _reconcile_delivered(store, outbox_id, existing["received_at"])
                return {
                    "outbox_id": outbox_id,
                    "event_id": event_id,
                    "recipient_agent_id": recipient_agent_id,
                    "state": "delivered",
                    "effect_kind": effect_kind,
                    "effect_id": effect_id,
                    "idempotent": True,
                }
            lease = store.connection.execute(
                "SELECT * FROM outbox_dispatch_leases WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if (
                lease is None
                or lease["dispatcher_id"] != dispatcher_id
                or int(lease["fence"]) != fence
            ):
                raise StorageRefusal("delivery_fenced", "acknowledgement uses a stale dispatch fence")
            event = store.connection.execute(
                "SELECT event_id FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if event is None:
                raise StorageRefusal("event_unknown", "acknowledgement source event is absent")
            store.connection.execute(
                """
                INSERT INTO recipient_receipts
                  (event_id,recipient_agent_id,received_at,effect_kind,effect_id)
                VALUES(?,?,?,?,?)
                """,
                (event_id, recipient_agent_id, at, effect_kind, effect_id),
            )
            store.connection.execute(
                """
                UPDATE delivery_outbox
                   SET state='delivered',last_outcome='acknowledged',delivered_at=?
                 WHERE outbox_id=?
                """,
                (at, outbox_id),
            )
            store.connection.execute(
                "DELETE FROM outbox_dispatch_leases WHERE outbox_id=?", (outbox_id,)
            )
            changed = store.connection.execute(
                """
                UPDATE delivery_attempts
                   SET adapter_kind=?,finished_at=?,outcome='acknowledged'
                 WHERE attempt_id=? AND outbox_id=?
                """,
                (adapter_kind, at, attempt_id, outbox_id),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("attempt_mismatch", "acknowledgement attempt is not exact")
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
        raise store._translate_database_error(exc, "recipient acknowledgement conflicted") from exc
    return {
        "outbox_id": outbox_id,
        "event_id": event_id,
        "recipient_agent_id": recipient_agent_id,
        "state": "delivered",
        "effect_kind": effect_kind,
        "effect_id": effect_id,
        "idempotent": False,
    }


def fail_outbox(
    store: Any,
    identity: OutboxDispatchIdentity,
    fence: int,
    adapter_kind: str,
    reason: str,
    retry_at: str,
    at: str,
) -> dict[str, Any]:
    outbox_id = identity.outbox_id
    event_id = identity.event_id
    recipient_agent_id = identity.recipient_agent_id
    dispatcher_id = identity.dispatcher_id
    attempt_id = identity.attempt_id
    now = _time(at, "delivery failure time")
    if _time(retry_at, "delivery retry time") < now or not reason:
        raise StorageRefusal("invalid_delivery", "delivery failure requires a bounded retry time and reason")
    try:
        with store._transaction():
            outbox = store.connection.execute(
                "SELECT * FROM delivery_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            lease = store.connection.execute(
                "SELECT * FROM outbox_dispatch_leases WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if (
                outbox is None
                or outbox["event_id"] != event_id
                or outbox["recipient_agent_id"] != recipient_agent_id
            ):
                raise StorageRefusal("source_event_mismatch", "delivery failure does not match its outbox")
            if lease is None or lease["dispatcher_id"] != dispatcher_id or int(lease["fence"]) != fence:
                raise StorageRefusal("delivery_fenced", "delivery failure uses a stale dispatch fence")
            store.connection.execute(
                """
                UPDATE delivery_outbox
                   SET state='pending',available_at=?,last_attempt_at=?,last_outcome=?
                 WHERE outbox_id=?
                """,
                (retry_at, at, reason, outbox_id),
            )
            store.connection.execute(
                "DELETE FROM outbox_dispatch_leases WHERE outbox_id=?", (outbox_id,)
            )
            changed = store.connection.execute(
                """
                UPDATE delivery_attempts SET adapter_kind=?,finished_at=?,outcome=?
                 WHERE attempt_id=? AND outbox_id=?
                """,
                (adapter_kind, at, reason, attempt_id, outbox_id),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("attempt_mismatch", "delivery failure attempt is not exact")
            store.connection.execute(
                """
                INSERT INTO obligations
                  (obligation_id,owner_agent_id,kind,aggregate_id,dedupe_key,state,
                   next_attention_at,details_json,created_at,updated_at)
                VALUES(?,?, 'delivery',?,?, 'open',?, '{}',?,?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                  state='open',next_attention_at=excluded.next_attention_at,updated_at=excluded.updated_at
                """,
                (
                    f"obligation:{outbox_id}",
                    recipient_agent_id,
                    outbox_id,
                    f"delivery:{outbox_id}",
                    retry_at,
                    at,
                    at,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "delivery failure conflicted") from exc
    return {
        "outbox_id": outbox_id,
        "event_id": event_id,
        "recipient_agent_id": recipient_agent_id,
        "state": "pending",
        "retry_at": retry_at,
    }


def pending_backlog(
    store: Any,
    at: str,
    *,
    limit: int = 100,
    per_recipient: int = 2,
    exclude_outbox_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    _time(at, "backlog observation time")
    if not 1 <= limit <= 500 or not 1 <= per_recipient <= 20:
        raise StorageRefusal("invalid_limit", "backlog bounds are invalid")
    rows = store.connection.execute(
        """
        SELECT o.outbox_id,o.event_id,o.recipient_agent_id,o.state,o.available_at,
               o.attempt_count,e.event_seq,e.event_type,e.aggregate_kind,e.aggregate_id
          FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
         WHERE o.state='pending' AND o.available_at<=?
           AND (? IS NULL OR o.outbox_id<>?)
         ORDER BY o.available_at,e.event_seq,o.outbox_id
         LIMIT ?
        """,
        (at, exclude_outbox_id, exclude_outbox_id, limit * per_recipient),
    ).fetchall()
    counts: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    for row in rows:
        recipient = str(row["recipient_agent_id"])
        if counts.get(recipient, 0) >= per_recipient:
            continue
        counts[recipient] = counts.get(recipient, 0) + 1
        result.append(dict(row))
        if len(result) == limit:
            break
    return result


def delivery_target(store: Any, recipient_agent_id: str, at: str) -> Optional[dict[str, Any]]:
    now = _time(at, "delivery target time")
    supervision = store.supervision_policy(recipient_agent_id)
    watcher = store.connection.execute(
        """
        SELECT w.watcher_id,w.runtime_instance_id,w.wake_locator,w.leased_until,w.fence,
               r.status,r.verified,r.runtime_generation
          FROM watcher_registrations w
          JOIN runtime_instances r ON r.runtime_instance_id=w.runtime_instance_id
         WHERE w.actor_agent_id=?
        """,
        (recipient_agent_id,),
    ).fetchone()
    if (
        watcher is not None
        and not (
            supervision["mode"] == "calm"
            and supervision["runtime_state"] == "paused"
        )
        and _time(str(watcher["leased_until"]), "watcher lease") > now
        and watcher["status"] in {"active", "idle"}
        and watcher["verified"]
    ):
        return {
            "channel": "watcher",
            "runtime_instance_id": watcher["runtime_instance_id"],
            "locator": watcher["wake_locator"],
            "generation": watcher["runtime_generation"],
            "fence": int(watcher["fence"]),
        }
    runtime = store.connection.execute(
        """
        SELECT r.runtime_instance_id,r.endpoint,r.runtime_generation,r.status,r.verified,
               r.backend_kind,r.session_ref,a.routing_name,a.thread_id
          FROM runtime_instances r
          JOIN agent_instances a ON a.agent_id=r.actor_agent_id
         WHERE r.actor_agent_id=? AND r.status IN ('active','idle') AND r.verified=1
           AND a.retired_at IS NULL
         ORDER BY last_seen_at DESC,runtime_instance_id
         LIMIT 1
        """,
        (recipient_agent_id,),
    ).fetchone()
    if runtime is None:
        return None
    return {
        "channel": "direct",
        "runtime_instance_id": runtime["runtime_instance_id"],
        "locator": runtime["endpoint"],
        "generation": runtime["runtime_generation"],
        "backend_kind": runtime["backend_kind"],
        "session_ref": runtime["session_ref"],
        "routing_name": runtime["routing_name"],
        "thread_id": runtime["thread_id"],
        "fence": None,
    }


def outbox_envelope(
    store: Any, outbox_id: str, event_id: str, recipient_agent_id: str
) -> dict[str, Any]:
    row = store.connection.execute(
        """
        SELECT o.outbox_id,o.event_id,o.recipient_agent_id,e.event_seq,e.event_type,
               e.aggregate_kind,e.aggregate_id,e.entity_version,e.status,e.update_text,
               e.request_id,e.task_id,COALESCE(e.agent_id,t.champion_agent_id) source_agent_id,
               json_extract(e.detail_json,'$.runtime_instance_id') source_runtime_instance_id,
               json_extract(e.detail_json,'$.runtime_generation') source_runtime_generation
          FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
          LEFT JOIN tasks t ON t.task_id=e.task_id
         WHERE o.outbox_id=?
        """,
        (outbox_id,),
    ).fetchone()
    if row is None:
        raise StorageRefusal("delivery_unknown", "outbox row does not exist")
    if row["event_id"] != event_id or row["recipient_agent_id"] != recipient_agent_id:
        raise StorageRefusal("source_event_mismatch", "outbox envelope identity does not match")
    return {
        "schema": "league.delivery-envelope.v1",
        "outbox_id": row["outbox_id"],
        "event_id": row["event_id"],
        "event_seq": int(row["event_seq"]),
        "recipient_agent_id": row["recipient_agent_id"],
        "source_agent_id": row["source_agent_id"],
        "source_runtime_instance_id": row["source_runtime_instance_id"],
        "source_runtime_generation": row["source_runtime_generation"],
        "event_type": row["event_type"],
        "aggregate_kind": row["aggregate_kind"],
        "aggregate_id": row["aggregate_id"],
        "aggregate_version": int(row["entity_version"]),
        "status": row["status"],
        "summary": row["update_text"],
        "request_id": row["request_id"],
        "task_id": row["task_id"],
    }
