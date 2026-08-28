"""SQLite delivery claim and acknowledgement operations."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Optional

from .storage_types import StorageRefusal


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("invalid_delivery", f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("invalid_delivery", f"{label} must include a UTC offset")
    return parsed


def claim_delivery(
    store: Any,
    event_id: str,
    recipient_agent_id: str,
    claim_token: str,
    claim_expires_at: str,
    at: str,
) -> dict[str, Any]:
    if not claim_token:
        raise StorageRefusal("invalid_delivery", "delivery claim token is required")
    claimed_at = _time(at, "claim time")
    expires_at = _time(claim_expires_at, "claim expiry")
    if expires_at <= claimed_at:
        raise StorageRefusal("invalid_delivery", "delivery claim expiry must be after claim time")
    idempotent = False
    try:
        with store._transaction():
            row = store.connection.execute(
                """
                SELECT state,attempt_count,claim_token,claim_expires_at,accepted_at
                  FROM deliveries WHERE event_id=? AND recipient_agent_id=?
                """,
                (event_id, recipient_agent_id),
            ).fetchone()
            if row is None:
                store.connection.execute(
                    """
                    INSERT INTO deliveries
                      (event_id,recipient_agent_id,state,attempt_count,claim_token,claim_expires_at,
                       accepted_at,acknowledged_at,failed_at,last_error)
                    VALUES(?,?,'claimed',1,?,?,?,NULL,NULL,NULL)
                    """,
                    (event_id, recipient_agent_id, claim_token, claim_expires_at, at),
                )
                attempt = 1
            elif (
                row["state"] == "claimed"
                and row["claim_token"] == claim_token
                and row["claim_expires_at"] == claim_expires_at
                and row["accepted_at"] == at
            ):
                attempt = int(row["attempt_count"])
                idempotent = True
            else:
                expired = (
                    row["state"] == "claimed"
                    and row["claim_expires_at"] is not None
                    and _time(row["claim_expires_at"], "stored claim expiry") <= claimed_at
                )
                if row["state"] not in {"pending", "failed"} and not expired:
                    raise StorageRefusal("delivery_conflict", "delivery is not claimable")
                attempt = int(row["attempt_count"]) + 1
                store.connection.execute(
                    """
                    UPDATE deliveries SET state='claimed',attempt_count=?,claim_token=?,claim_expires_at=?,
                      accepted_at=?,acknowledged_at=NULL,failed_at=NULL,last_error=NULL
                     WHERE event_id=? AND recipient_agent_id=?
                    """,
                    (
                        attempt,
                        claim_token,
                        claim_expires_at,
                        at,
                        event_id,
                        recipient_agent_id,
                    ),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "delivery claim conflicted with canonical state"
        ) from exc
    return {
        "event_id": event_id,
        "recipient_agent_id": recipient_agent_id,
        "state": "claimed",
        "attempt": attempt,
        "idempotent": idempotent,
    }


def finish_delivery(
    store: Any,
    event_id: str,
    recipient_agent_id: str,
    claim_token: str,
    state: str,
    at: str,
    reason: Optional[str],
) -> dict[str, Any]:
    _time(at, "delivery completion time")
    time_column = "acknowledged_at" if state == "acknowledged" else "failed_at"
    try:
        with store._transaction():
            changed = store.connection.execute(
                f"""
                UPDATE deliveries SET state=?,{time_column}=?,last_error=?
                 WHERE event_id=? AND recipient_agent_id=? AND state='claimed' AND claim_token=?
                """,
                (state, at, reason, event_id, recipient_agent_id, claim_token),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("delivery_conflict", f"delivery {state} precondition failed")
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, f"delivery {state} failed") from exc
    return {"event_id": event_id, "recipient_agent_id": recipient_agent_id, "state": state}
