"""Requester-facing progress coalescing, delivery, and overdue reconciliation."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from .sqlite_request_ops import (
    IMMEDIATE_PROGRESS_REASONS,
    PROGRESS_REASONS,
    _active_claim,
    _bounded_public_text,
    _digest,
    _insert_request_event,
    _json,
    _request_row,
    _time,
)
from .storage_request import RequestProgressCommand
from .storage_types import StorageRefusal


def _payload(command: RequestProgressCommand) -> tuple[dict[str, Any], str]:
    if (
        command.reason_code not in PROGRESS_REASONS
        or not all((command.progress_id, command.event_id, command.outbox_id))
        or isinstance(command.expected_version, bool)
        or command.expected_version < 1
        or isinstance(command.progress_generation, bool)
        or command.progress_generation < 1
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (command.settled_count, command.total_count, command.blocker_count)
        )
        or command.settled_count > command.total_count
        or command.blocker_severity not in {"none", "low", "medium", "high", "critical"}
        or (command.blocker_count == 0) != (command.blocker_severity == "none")
        or not isinstance(command.user_action_required, bool)
        or isinstance(command.minimum_interval_seconds, bool)
        or not 0 <= command.minimum_interval_seconds <= 86_400
        or isinstance(command.grace_seconds, bool)
        or not 0 <= command.grace_seconds <= 3_600
    ):
        raise StorageRefusal("request_progress_invalid", "request progress fields are invalid")
    value = {
        "settled_count": command.settled_count,
        "total_count": command.total_count,
        "current_phase": _bounded_public_text(command.current_phase, "current phase", maximum=128),
        "blocker_count": command.blocker_count,
        "blocker_severity": command.blocker_severity,
        "user_action_required": command.user_action_required,
        "deadline_change": (
            None
            if command.deadline_change is None
            else _bounded_public_text(command.deadline_change, "deadline change", maximum=128)
        ),
        "next_action": _bounded_public_text(command.next_action, "next action", maximum=512),
    }
    if command.promised_checkpoint_at is not None:
        _time(command.promised_checkpoint_at, "promised checkpoint")
    return value, _digest(_json(value))


def _insert_progress(
    store: Any,
    *,
    progress_id: str,
    request: sqlite3.Row,
    recipient: str,
    request_generation: int,
    progress_generation: int,
    urgency: str,
    reason_code: str,
    value: dict[str, Any],
    digest: str,
    event_id: str,
    outbox_id: str,
    at: str,
) -> None:
    update = (
        "Request progress checkpoint overdue"
        if urgency == "overdue"
        else f"{value['current_phase']}: {value['next_action']}"
    )
    _insert_request_event(
        store,
        event_id=event_id,
        request_id=str(request["request_id"]),
        actor_id=request["owner_agent_id"],
        request_version=request_generation,
        event_type="request_progress_overdue" if urgency == "overdue" else "request_progress",
        state=str(request["state"]),
        update=update,
        at=at,
        detail={"reason_code": reason_code, "progress_generation": progress_generation, **value},
    )
    store.connection.execute(
        """
        INSERT INTO delivery_outbox
          (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
        VALUES(?,?,?,'pending',?,0)
        """,
        (outbox_id, event_id, recipient, at),
    )
    store.connection.execute(
        """
        INSERT INTO request_progress_events
          (progress_id,request_id,request_generation,progress_generation,owner_agent_id,
           recipient_agent_id,urgency,reason_code,content_digest,settled_count,total_count,
           current_phase,blocker_count,blocker_severity,user_action_required,deadline_change,
           next_action,event_id,outbox_id,emitted_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            progress_id,
            request["request_id"],
            request_generation,
            progress_generation,
            request["owner_agent_id"],
            recipient,
            urgency,
            reason_code,
            digest,
            value["settled_count"],
            value["total_count"],
            value["current_phase"],
            value["blocker_count"],
            value["blocker_severity"],
            int(value["user_action_required"]),
            value["deadline_change"],
            value["next_action"],
            event_id,
            outbox_id,
            at,
        ),
    )


def emit_request_progress(store: Any, command: RequestProgressCommand) -> dict[str, Any]:
    """Emit immediate progress or buffer one changed routine aggregate."""

    now = _time(command.at, "request progress time")
    value, digest = _payload(command)
    urgency = "immediate" if command.reason_code in IMMEDIATE_PROGRESS_REASONS else "routine"
    try:
        with store._transaction():
            request = _request_row(store, command.request_id)
            _active_claim(store, command.request_id, token=command.claim_token, at=command.at)
            if int(request["version"]) != command.expected_version:
                raise StorageRefusal("version_conflict", "request progress expected-version failed")
            recipient = str(request["requester_agent_id"])
            duplicate = store.connection.execute(
                """
                SELECT * FROM request_progress_events
                 WHERE request_id=? AND progress_generation=? AND recipient_agent_id=?
                """,
                (command.request_id, command.progress_generation, recipient),
            ).fetchone()
            if duplicate is not None:
                if not (
                    duplicate["progress_id"] == command.progress_id
                    and duplicate["content_digest"] == digest
                    and duplicate["reason_code"] == command.reason_code
                    and duplicate["event_id"] == command.event_id
                    and duplicate["outbox_id"] == command.outbox_id
                ):
                    raise StorageRefusal(
                        "request_progress_conflict",
                        "request progress generation already has different evidence",
                    )
                return {
                    "request_id": command.request_id,
                    "progress_generation": command.progress_generation,
                    "emitted": True,
                    "buffered": False,
                    "idempotent": True,
                }
            latest = store.connection.execute(
                """
                SELECT * FROM request_progress_events
                 WHERE request_id=? AND recipient_agent_id=?
                 ORDER BY emitted_at DESC,progress_generation DESC LIMIT 1
                """,
                (command.request_id, recipient),
            ).fetchone()
            buffer = store.connection.execute(
                "SELECT * FROM request_progress_buffers WHERE request_id=? AND recipient_agent_id=?",
                (command.request_id, recipient),
            ).fetchone()
            buffered_unchanged = (
                buffer is not None
                and buffer["state"] in {"pending", "due"}
                and buffer["content_digest"] == digest
            )
            if urgency == "routine" and (
                (
                    buffered_unchanged
                    and now < _time(str(buffer["due_at"]), "buffered progress due time")
                )
                or (
                    not buffered_unchanged
                    and latest is not None
                    and latest["content_digest"] == digest
                )
            ):
                return {
                    "request_id": command.request_id,
                    "progress_generation": command.progress_generation,
                    "emitted": False,
                    "buffered": False,
                    "suppressed": "unchanged",
                    "idempotent": False,
                }
            if urgency == "routine":
                if buffer is not None and buffer["state"] in {"pending", "due"}:
                    due = _time(str(buffer["due_at"]), "buffered progress due time")
                elif latest is not None:
                    due = _time(str(latest["emitted_at"]), "prior progress time") + timedelta(
                        seconds=command.minimum_interval_seconds
                    )
                else:
                    due = now + timedelta(seconds=command.minimum_interval_seconds)
                if command.promised_checkpoint_at is not None:
                    due = min(due, _time(command.promised_checkpoint_at, "promised checkpoint"))
                grace = due + timedelta(seconds=command.grace_seconds)
                store.connection.execute(
                    """
                    INSERT INTO request_progress_buffers
                      (request_id,recipient_agent_id,request_generation,progress_generation,
                       owner_agent_id,progress_id,event_id,outbox_id,content_digest,settled_count,
                       total_count,current_phase,blocker_count,blocker_severity,user_action_required,
                       deadline_change,next_action,due_at,grace_expires_at,promised_checkpoint_at,
                       state,buffered_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending',?,?)
                    ON CONFLICT(request_id,recipient_agent_id) DO UPDATE SET
                      request_generation=excluded.request_generation,
                      progress_generation=excluded.progress_generation,
                      owner_agent_id=excluded.owner_agent_id,
                      progress_id=excluded.progress_id,event_id=excluded.event_id,
                      outbox_id=excluded.outbox_id,content_digest=excluded.content_digest,
                      settled_count=excluded.settled_count,total_count=excluded.total_count,
                      current_phase=excluded.current_phase,blocker_count=excluded.blocker_count,
                      blocker_severity=excluded.blocker_severity,
                      user_action_required=excluded.user_action_required,
                      deadline_change=excluded.deadline_change,next_action=excluded.next_action,
                      due_at=excluded.due_at,grace_expires_at=excluded.grace_expires_at,
                      promised_checkpoint_at=excluded.promised_checkpoint_at,state='pending',
                      updated_at=excluded.updated_at
                    """,
                    (
                        command.request_id,
                        recipient,
                        command.expected_version,
                        command.progress_generation,
                        request["owner_agent_id"],
                        command.progress_id,
                        command.event_id,
                        command.outbox_id,
                        digest,
                        value["settled_count"],
                        value["total_count"],
                        value["current_phase"],
                        value["blocker_count"],
                        value["blocker_severity"],
                        int(value["user_action_required"]),
                        value["deadline_change"],
                        value["next_action"],
                        due.isoformat(),
                        grace.isoformat(),
                        command.promised_checkpoint_at,
                        command.at,
                        command.at,
                    ),
                )
                if now < due:
                    return {
                        "request_id": command.request_id,
                        "progress_generation": command.progress_generation,
                        "emitted": False,
                        "buffered": True,
                        "due_at": due.isoformat(),
                        "idempotent": False,
                    }
            elif buffer is not None and buffer["state"] in {"pending", "due"}:
                store.connection.execute(
                    """
                    UPDATE request_progress_buffers SET state='superseded',updated_at=?
                     WHERE request_id=? AND recipient_agent_id=?
                    """,
                    (command.at, command.request_id, recipient),
                )
            _insert_progress(
                store,
                progress_id=command.progress_id,
                request=request,
                recipient=recipient,
                request_generation=command.expected_version,
                progress_generation=command.progress_generation,
                urgency=urgency,
                reason_code=command.reason_code,
                value=value,
                digest=digest,
                event_id=command.event_id,
                outbox_id=command.outbox_id,
                at=command.at,
            )
            if urgency == "routine":
                store.connection.execute(
                    """
                    UPDATE request_progress_buffers SET state='emitted',updated_at=?
                     WHERE request_id=? AND recipient_agent_id=?
                    """,
                    (command.at, command.request_id, recipient),
                )
            store.connection.execute(
                """
                UPDATE obligations SET state='satisfied',updated_at=?
                 WHERE owner_agent_id=? AND kind='request_progress_due'
                   AND aggregate_id=? AND state='open'
                """,
                (command.at, request["owner_agent_id"], command.request_id),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "request progress conflicted with canonical state") from exc
    return {
        "request_id": command.request_id,
        "progress_generation": command.progress_generation,
        "reason_code": command.reason_code,
        "event_id": command.event_id,
        "outbox_id": command.outbox_id,
        "emitted": True,
        "buffered": False,
        "immediate": urgency == "immediate",
        "idempotent": False,
    }


def reconcile_request_progress(store: Any, owner_agent_id: str, at: str) -> dict[str, Any]:
    """Create a due obligation only for buffered change, then escalate once."""

    now = _time(at, "request progress reconciliation time")
    if not owner_agent_id:
        raise StorageRefusal("request_progress_reconciliation_invalid", "progress owner is required")
    rows = store.connection.execute(
        """
        SELECT b.*,r.state request_state,
               EXISTS(SELECT 1 FROM runtime_instances rt
                       WHERE rt.actor_agent_id=b.owner_agent_id
                         AND rt.status IN ('active','idle') AND rt.verified=1) owner_live,
               rc.leased_until,rc.released_at
          FROM request_progress_buffers b JOIN requests r ON r.request_id=b.request_id
          LEFT JOIN request_claims rc ON rc.request_id=b.request_id
         WHERE b.owner_agent_id=? AND b.state IN ('pending','due')
         ORDER BY b.due_at,b.request_id LIMIT 501
        """,
        (owner_agent_id,),
    ).fetchall()
    if len(rows) > 500:
        raise StorageRefusal(
            "request_progress_reconciliation_too_large",
            "progress reconciliation exceeds the bounded request set",
        )
    created = existing = escalated = 0
    try:
        with store._transaction():
            for row in rows:
                due = _time(str(row["due_at"]), "progress due time")
                grace = _time(str(row["grace_expires_at"]), "progress grace time")
                lease_stalled = (
                    not bool(row["owner_live"])
                    or row["leased_until"] is None
                    or row["released_at"] is not None
                    or _time(str(row["leased_until"]), "request claim expiry") <= now
                )
                if now >= due:
                    obligation_id = (
                        f"request-progress-due:{row['request_id']}:{row['recipient_agent_id']}:"
                        f"{row['progress_generation']}"
                    )
                    result = store.connection.execute(
                        """
                        INSERT INTO obligations
                          (obligation_id,owner_agent_id,kind,aggregate_id,dedupe_key,state,
                           next_attention_at,details_json,created_at,updated_at)
                        VALUES(?,?,'request_progress_due',?,?, 'open',?,?,?,?)
                        ON CONFLICT(dedupe_key) DO NOTHING
                        """,
                        (
                            obligation_id,
                            owner_agent_id,
                            row["request_id"],
                            obligation_id,
                            row["grace_expires_at"],
                            _json(
                                {
                                    "request_generation": int(row["request_generation"]),
                                    "progress_generation": int(row["progress_generation"]),
                                }
                            ),
                            at,
                            at,
                        ),
                    )
                    created += int(bool(result.rowcount))
                    existing += int(not bool(result.rowcount))
                    store.connection.execute(
                        """
                        UPDATE request_progress_buffers SET state='due',updated_at=?
                         WHERE request_id=? AND recipient_agent_id=?
                        """,
                        (at, row["request_id"], row["recipient_agent_id"]),
                    )
                if not lease_stalled and now <= grace:
                    continue
                value = {
                    "settled_count": int(row["settled_count"]),
                    "total_count": int(row["total_count"]),
                    "current_phase": str(row["current_phase"]),
                    "blocker_count": max(1, int(row["blocker_count"])),
                    "blocker_severity": (
                        "high" if row["blocker_severity"] == "none" else row["blocker_severity"]
                    ),
                    "user_action_required": False,
                    "deadline_change": row["deadline_change"],
                    "next_action": "Owner must reconcile the stalled request checkpoint",
                }
                _insert_progress(
                    store,
                    progress_id=f"{row['progress_id']}:overdue",
                    request=row,
                    recipient=str(row["recipient_agent_id"]),
                    request_generation=int(row["request_generation"]),
                    progress_generation=int(row["progress_generation"]),
                    urgency="overdue",
                    reason_code="request_stalled",
                    value=value,
                    digest=_digest(_json(value)),
                    event_id=f"request:{row['request_id']}:progress:{row['progress_generation']}:overdue",
                    outbox_id=f"outbox:{row['request_id']}:progress:{row['progress_generation']}:overdue",
                    at=at,
                )
                store.connection.execute(
                    """
                    UPDATE request_progress_buffers SET state='escalated',updated_at=?
                     WHERE request_id=? AND recipient_agent_id=?
                    """,
                    (at, row["request_id"], row["recipient_agent_id"]),
                )
                store.connection.execute(
                    """
                    UPDATE obligations SET state='satisfied',updated_at=?
                     WHERE owner_agent_id=? AND kind='request_progress_due'
                       AND aggregate_id=? AND state='open'
                    """,
                    (at, owner_agent_id, row["request_id"]),
                )
                escalated += 1
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "request progress reconciliation conflicted with canonical state"
        ) from exc
    return {
        "owner_agent_id": owner_agent_id,
        "created": created,
        "existing": existing,
        "escalated": escalated,
        "examined": len(rows),
        "invented_progress_events": 0,
    }
