"""Crash-safe Cursor prompt/interrupt intent and receipt operations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping

from .sqlite_request_ops import _time
from .storage_types import StorageRefusal


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_INTENT_KEYS = {
    "schema",
    "outbox_id",
    "event_id",
    "recipient_agent_id",
    "runtime_instance_id",
    "runtime_generation",
    "pane_id",
    "session_ref",
    "routing_name",
    "action",
    "observed_status",
    "observed_revision",
    "observed_state_change_seq",
    "prompt_sha256",
    "prompt_bytes",
}


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _object(value: str | None, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("cursor_steering_corrupt", f"stored {label} is malformed") from exc
    if not isinstance(decoded, dict):
        raise StorageRefusal("cursor_steering_corrupt", f"stored {label} is not an object")
    return decoded


def _validate_intent(intent: Mapping[str, Any]) -> tuple[str, str]:
    if set(intent) != _INTENT_KEYS or intent.get("schema") != "league.cursor-steering-intent.v1":
        raise StorageRefusal("cursor_steering_invalid", "Cursor steering intent is incomplete")
    strings = (
        "outbox_id",
        "event_id",
        "recipient_agent_id",
        "runtime_instance_id",
        "runtime_generation",
        "pane_id",
        "session_ref",
        "routing_name",
        "observed_status",
        "prompt_sha256",
    )
    if any(not isinstance(intent.get(key), str) or not intent[key] for key in strings):
        raise StorageRefusal("cursor_steering_invalid", "Cursor steering identity is incomplete")
    if intent.get("action") not in {"idle_submit", "working_steer"}:
        raise StorageRefusal("cursor_steering_invalid", "Cursor steering action is unsupported")
    if intent.get("observed_status") not in {"idle", "done", "working"}:
        raise StorageRefusal("cursor_steering_invalid", "Cursor steering status is unsupported")
    if intent["action"] == "working_steer" and intent["observed_status"] != "working":
        raise StorageRefusal("cursor_steering_invalid", "working steer requires working Cursor proof")
    if intent["action"] == "idle_submit" and intent["observed_status"] not in {"idle", "done"}:
        raise StorageRefusal("cursor_steering_invalid", "idle submit requires idle Cursor proof")
    if not _DIGEST.fullmatch(str(intent["prompt_sha256"])):
        raise StorageRefusal("cursor_steering_invalid", "Cursor steering prompt digest is invalid")
    if type(intent.get("prompt_bytes")) is not int or int(intent["prompt_bytes"]) < 1:
        raise StorageRefusal("cursor_steering_invalid", "Cursor steering prompt size is invalid")
    for key in ("observed_revision", "observed_state_change_seq"):
        if type(intent.get(key)) is not int or int(intent[key]) < 0:
            raise StorageRefusal("cursor_steering_invalid", "Cursor steering observation is invalid")
    encoded = _json(intent)
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


def _result(row: sqlite3.Row, *, idempotent: bool) -> dict[str, Any]:
    return {
        "outbox_id": row["outbox_id"],
        "event_id": row["event_id"],
        "recipient_agent_id": row["recipient_agent_id"],
        "intent_digest": row["intent_digest"],
        "state": row["effect_state"],
        "effect_id": row["effect_id"],
        "receipt": _object(row["receipt_json"], "Cursor steering receipt"),
        "idempotent": idempotent,
    }


def begin_cursor_steering(
    store: Any, intent: Mapping[str, Any], at: str
) -> dict[str, Any]:
    """Persist one exact intent before any terminal input may occur."""

    _time(at, "Cursor steering intent time")
    encoded, digest = _validate_intent(intent)
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM cursor_steering_effects WHERE outbox_id=?",
                (intent["outbox_id"],),
            ).fetchone()
            if existing is not None:
                if existing["intent_digest"] != digest or existing["intent_json"] != encoded:
                    raise StorageRefusal(
                        "cursor_steering_conflict",
                        "Cursor steering retry changed its exact target or prompt",
                    )
                return _result(existing, idempotent=True)
            outbox = store.connection.execute(
                "SELECT event_id,recipient_agent_id,state FROM delivery_outbox WHERE outbox_id=?",
                (intent["outbox_id"],),
            ).fetchone()
            if (
                outbox is None
                or outbox["event_id"] != intent["event_id"]
                or outbox["recipient_agent_id"] != intent["recipient_agent_id"]
                or outbox["state"] != "in_flight"
            ):
                raise StorageRefusal(
                    "cursor_steering_fenced", "Cursor steering outbox is not exactly in flight"
                )
            runtime = store.connection.execute(
                """
                SELECT actor_agent_id,harness_kind,backend_kind,session_ref,endpoint,
                       runtime_generation,status,verified
                  FROM runtime_instances WHERE runtime_instance_id=?
                """,
                (intent["runtime_instance_id"],),
            ).fetchone()
            if (
                runtime is None
                or runtime["actor_agent_id"] != intent["recipient_agent_id"]
                or runtime["harness_kind"] not in {"cursor", "cursor-thread"}
                or runtime["backend_kind"] != "herdr"
                or runtime["session_ref"] != intent["session_ref"]
                or runtime["endpoint"] != intent["pane_id"]
                or runtime["runtime_generation"] != intent["runtime_generation"]
                or runtime["status"] not in {"active", "idle"}
                or not runtime["verified"]
            ):
                raise StorageRefusal(
                    "cursor_runtime_changed",
                    "canonical Cursor runtime no longer matches the verified target",
                )
            store.connection.execute(
                """
                INSERT INTO cursor_steering_effects
                  (outbox_id,event_id,recipient_agent_id,runtime_instance_id,runtime_generation,
                   pane_id,session_ref,action,observed_status,prompt_sha256,prompt_bytes,
                   intent_digest,effect_state,effect_id,intent_json,receipt_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'intent_recorded',NULL,?,NULL,?,?)
                """,
                (
                    intent["outbox_id"],
                    intent["event_id"],
                    intent["recipient_agent_id"],
                    intent["runtime_instance_id"],
                    intent["runtime_generation"],
                    intent["pane_id"],
                    intent["session_ref"],
                    intent["action"],
                    intent["observed_status"],
                    intent["prompt_sha256"],
                    intent["prompt_bytes"],
                    digest,
                    encoded,
                    at,
                    at,
                ),
            )
            row = store.connection.execute(
                "SELECT * FROM cursor_steering_effects WHERE outbox_id=?",
                (intent["outbox_id"],),
            ).fetchone()
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "Cursor steering intent conflicted with canonical state"
        ) from exc
    assert row is not None
    return _result(row, idempotent=False)


def record_cursor_steering_phase(
    store: Any,
    outbox_id: str,
    intent_digest: str,
    state: str,
    receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    """Record text, refusal, or acknowledged provider input without replay."""

    _time(at, "Cursor steering receipt time")
    if state not in {"text_sent", "effect_applied", "refused"}:
        raise StorageRefusal("cursor_steering_invalid", "Cursor steering phase is unsupported")
    receipt_json = _json(receipt)
    effect_id = receipt.get("effect_id") if state == "effect_applied" else None
    if state == "effect_applied" and (
        not isinstance(effect_id, str) or not _DIGEST.fullmatch(effect_id)
    ):
        raise StorageRefusal("cursor_steering_invalid", "Cursor steering effect ID is invalid")
    allowed = {
        "intent_recorded": {"text_sent", "effect_applied", "refused"},
        "text_sent": {"effect_applied", "refused"},
    }
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT * FROM cursor_steering_effects WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None or row["intent_digest"] != intent_digest:
                raise StorageRefusal(
                    "cursor_steering_fenced", "Cursor steering receipt has no exact durable intent"
                )
            if row["effect_state"] == state:
                if row["receipt_json"] != receipt_json or row["effect_id"] != effect_id:
                    raise StorageRefusal(
                        "cursor_steering_conflict", "Cursor steering retry changed its receipt"
                    )
                return _result(row, idempotent=True)
            if row["effect_state"] not in allowed or state not in allowed[row["effect_state"]]:
                raise StorageRefusal(
                    "cursor_steering_fenced", "Cursor steering phase cannot move backward or replay"
                )
            store.connection.execute(
                """
                UPDATE cursor_steering_effects
                   SET effect_state=?,effect_id=?,receipt_json=?,updated_at=?
                 WHERE outbox_id=? AND intent_digest=?
                """,
                (state, effect_id, receipt_json, at, outbox_id, intent_digest),
            )
            updated = store.connection.execute(
                "SELECT * FROM cursor_steering_effects WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "Cursor steering receipt conflicted with canonical state"
        ) from exc
    assert updated is not None
    return _result(updated, idempotent=False)


def cursor_steering_effect(store: Any, outbox_id: str) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT * FROM cursor_steering_effects WHERE outbox_id=?", (outbox_id,)
    ).fetchone()
    return None if row is None else _result(row, idempotent=True)
