"""Durable intent and receipt operations for unified Pi session migration."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .storage_types import StorageRefusal


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SESSION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("pi_session_migration_time_invalid", "migration time must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("pi_session_migration_time_invalid", "migration time must include a UTC offset")


def _validate_intent(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "migration_id", "descriptor_id", "session_id",
        "source_session_path", "destination_session_path", "session_sha256",
        "parent_session_id", "parent_session_path", "cwd", "pane_id",
    }
    exact = dict(value)
    schema = exact.get("schema")
    if schema == "league.pi-session-migration-intent.v2":
        required |= {"parent_evidence_path", "parent_evidence_sha256"}
    if set(exact) != required or schema not in {
        "league.pi-session-migration-intent.v1",
        "league.pi-session-migration-intent.v2",
    }:
        raise StorageRefusal("pi_session_migration_invalid", "Pi migration intent fields are not exact")
    if (
        not SAFE_ID.fullmatch(str(exact.get("migration_id", "")))
        or not SAFE_ID.fullmatch(str(exact.get("descriptor_id", "")))
        or not SESSION_ID.fullmatch(str(exact.get("session_id", "")))
        or not SHA256.fullmatch(str(exact.get("session_sha256", "")))
        or not isinstance(exact.get("pane_id"), str)
        or not exact["pane_id"]
        or any(
            not isinstance(exact.get(key), str)
            or not Path(exact[key]).is_absolute()
            or exact[key] == "/"
            for key in ("source_session_path", "destination_session_path", "cwd")
        )
    ):
        raise StorageRefusal("pi_session_migration_invalid", "Pi migration identity is invalid")
    parent_id, parent_path = exact["parent_session_id"], exact["parent_session_path"]
    if (parent_id is None) != (parent_path is None) or (
        parent_id is not None
        and (
            not SESSION_ID.fullmatch(str(parent_id))
            or not isinstance(parent_path, str)
            or not Path(parent_path).is_absolute()
            or parent_path == "/"
        )
    ):
        raise StorageRefusal("pi_session_migration_invalid", "Pi parent lineage is incomplete")
    if schema == "league.pi-session-migration-intent.v2":
        evidence_path = exact["parent_evidence_path"]
        evidence_sha256 = exact["parent_evidence_sha256"]
        if parent_id is None:
            if evidence_path is not None or evidence_sha256 is not None:
                raise StorageRefusal(
                    "pi_session_migration_invalid",
                    "root Pi sessions cannot declare parent evidence",
                )
        elif (
            not isinstance(evidence_path, str)
            or not Path(evidence_path).is_absolute()
            or evidence_path == "/"
            or not SHA256.fullmatch(str(evidence_sha256 or ""))
        ):
            raise StorageRefusal(
                "pi_session_migration_invalid",
                "Pi parent evidence identity is incomplete",
            )
    return exact


def prepare(store: Any, intent: Mapping[str, Any], at: str) -> dict[str, Any]:
    _time(at)
    exact = _validate_intent(intent)
    digest = _digest(exact)
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM pi_session_migrations WHERE migration_id=?",
                (exact["migration_id"],),
            ).fetchone()
            if existing is not None:
                if existing["intent_digest"] != digest or json.loads(existing["intent_json"]) != exact:
                    raise StorageRefusal("pi_session_migration_conflict", "Pi migration intent changed")
                return {
                    "migration_id": exact["migration_id"], "state": existing["state"],
                    "intent_digest": digest,
                    "receipt": json.loads(existing["receipt_json"]) if existing["receipt_json"] else None,
                    "idempotent": True,
                }
            store.connection.execute(
                """
                INSERT INTO pi_session_migrations
                  (migration_id,descriptor_id,session_id,source_session_path,
                   destination_session_path,session_sha256,parent_session_id,
                   parent_session_path,cwd,pane_id,state,intent_json,intent_digest,
                   created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'intent_recorded',?,?,?,?)
                """,
                (
                    exact["migration_id"], exact["descriptor_id"], exact["session_id"],
                    exact["source_session_path"], exact["destination_session_path"],
                    exact["session_sha256"], exact["parent_session_id"],
                    exact["parent_session_path"], exact["cwd"], exact["pane_id"],
                    _json(exact), digest, at, at,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.IntegrityError as exc:
        raise StorageRefusal("pi_session_migration_conflict", "Pi session or descriptor already has a migration") from exc
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "Pi migration preparation failed") from exc
    return {"migration_id": exact["migration_id"], "state": "intent_recorded", "intent_digest": digest, "receipt": None, "idempotent": False}


def advance(
    store: Any,
    migration_id: str,
    intent_digest: str,
    expected_state: str,
    next_state: str,
    receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    _time(at)
    if (expected_state, next_state) not in {("intent_recorded", "copied"), ("copied", "bound")}:
        raise StorageRefusal("pi_session_migration_transition_invalid", "Pi migration transition is invalid")
    exact = dict(receipt)
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT * FROM pi_session_migrations WHERE migration_id=?", (migration_id,)
            ).fetchone()
            if row is None or row["intent_digest"] != intent_digest:
                raise StorageRefusal("pi_session_migration_conflict", "Pi migration intent is missing or changed")
            stored = json.loads(row["receipt_json"]) if row["receipt_json"] else None
            if row["state"] == next_state:
                if stored != exact:
                    raise StorageRefusal("pi_session_migration_conflict", "Pi migration receipt changed")
                return {"migration_id": migration_id, "state": next_state, "intent_digest": intent_digest, "receipt": stored, "idempotent": True}
            if next_state == "bound" and row["state"] == "bound":
                return {"migration_id": migration_id, "state": "bound", "intent_digest": intent_digest, "receipt": stored, "idempotent": True}
            if row["state"] != expected_state:
                raise StorageRefusal("pi_session_migration_state_conflict", "Pi migration state changed")
            store.connection.execute(
                "UPDATE pi_session_migrations SET state=?,receipt_json=?,updated_at=? WHERE migration_id=?",
                (next_state, _json(exact), at, migration_id),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "Pi migration transition failed") from exc
    return {"migration_id": migration_id, "state": next_state, "intent_digest": intent_digest, "receipt": exact, "idempotent": False}


def status(store: Any, migration_id: str) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT * FROM pi_session_migrations WHERE migration_id=?", (migration_id,)
    ).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["intent"] = json.loads(value.pop("intent_json"))
    raw_receipt = value.pop("receipt_json")
    value["receipt"] = json.loads(raw_receipt) if raw_receipt else None
    return value


__all__ = ["advance", "prepare", "status"]
