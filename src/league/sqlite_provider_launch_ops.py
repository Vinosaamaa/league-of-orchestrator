"""Canonical Pi/provider descriptor and exact restart-effect operations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .storage_types import StorageRefusal


SESSION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$")
SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SAFE_PROJECT = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,15}$")


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _absolute(value: Any) -> bool:
    return isinstance(value, str) and Path(value).is_absolute() and value != "/"


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal(
            "provider_launch_time_invalid", f"{label} must be RFC3339"
        ) from exc
    if parsed.tzinfo is None:
        raise StorageRefusal(
            "provider_launch_time_invalid", f"{label} must include a UTC offset"
        )
    return parsed


def _validate_descriptor(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "descriptor_id",
        "assignment_id",
        "runtime_kind",
        "provider_kind",
        "model",
        "effort",
        "cwd",
        "worktree_binding",
        "role",
        "placement",
        "callsign",
        "project_code",
        "task_label",
        "routing_name",
        "workspace_id",
        "creator_pane_id",
        "state_root",
        "release_root",
        "launch_mode",
        "requested_session_id",
        "requested_session_path",
        "parent_session_id",
        "parent_session_path",
    }
    descriptor = dict(value)
    if set(descriptor) != required or descriptor.get("schema") != "league.pi-launch-descriptor.v1":
        raise StorageRefusal("provider_launch_descriptor_invalid", "Pi launch descriptor fields are not exact")
    if (
        not isinstance(descriptor["descriptor_id"], str)
        or not SAFE_ID.fullmatch(descriptor["descriptor_id"])
        or descriptor["runtime_kind"] != "pi"
        or descriptor["provider_kind"] not in {"cursor", "codex"}
        or descriptor["role"] not in {"shotcaller", "champion"}
        or descriptor["placement"] not in {"sibling_pane", "new_tab"}
        or descriptor["launch_mode"] not in {"create", "fork", "resume"}
        or not isinstance(descriptor["model"], str)
        or not descriptor["model"]
        or not isinstance(descriptor["effort"], str)
        or not descriptor["effort"]
        or not all(_absolute(descriptor[key]) for key in ("cwd", "state_root", "release_root"))
        or not re.fullmatch(r"[0-9a-f]{64}", str(descriptor["worktree_binding"]))
        or not isinstance(descriptor["callsign"], str)
        or not descriptor["callsign"]
        or not SAFE_NAME.fullmatch(str(descriptor["routing_name"]))
        or not SAFE_PROJECT.fullmatch(str(descriptor["project_code"]))
        or len(str(descriptor["task_label"]).split()) != 2
        or " ".join(str(descriptor["task_label"]).split()) != descriptor["task_label"]
        or not isinstance(descriptor["workspace_id"], str)
        or not descriptor["workspace_id"]
    ):
        raise StorageRefusal("provider_launch_descriptor_invalid", "Pi launch descriptor identity is invalid")
    if descriptor["role"] == "champion":
        if descriptor["placement"] != "new_tab" or descriptor["creator_pane_id"] is not None:
            raise StorageRefusal("provider_launch_placement_invalid", "Champion Pi launch requires a distinct new tab")
    elif descriptor["placement"] != "sibling_pane" or not isinstance(descriptor["creator_pane_id"], str) or not descriptor["creator_pane_id"]:
        raise StorageRefusal("provider_launch_placement_invalid", "Shotcaller Pi launch requires one exact sibling-pane source")
    session_id = descriptor["requested_session_id"]
    session_path = descriptor["requested_session_path"]
    parent_id = descriptor["parent_session_id"]
    parent_path = descriptor["parent_session_path"]
    if descriptor["launch_mode"] == "create":
        exact = SESSION_ID.fullmatch(str(session_id or "")) and all(
            item is None for item in (session_path, parent_id, parent_path)
        )
    elif descriptor["launch_mode"] == "resume":
        exact = (
            SESSION_ID.fullmatch(str(session_id or ""))
            and _absolute(session_path)
            and (
                (parent_id is None and parent_path is None)
                or (SESSION_ID.fullmatch(str(parent_id or "")) and _absolute(parent_path))
            )
        )
    else:
        exact = session_id is None and session_path is None and SESSION_ID.fullmatch(str(parent_id or "")) and _absolute(parent_path)
    if not exact:
        raise StorageRefusal("provider_launch_session_invalid", "Pi create, fork, or resume identity is incomplete")
    return descriptor


def prepare_provider_launch(store: Any, descriptor: Mapping[str, Any], at: str) -> dict[str, Any]:
    _time(at, "provider launch preparation time")
    exact = _validate_descriptor(descriptor)
    digest = _digest(exact)
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM provider_launch_descriptors WHERE descriptor_id=?",
                (exact["descriptor_id"],),
            ).fetchone()
            if existing is not None:
                if existing["descriptor_digest"] != digest or json.loads(existing["descriptor_json"]) != exact:
                    raise StorageRefusal("provider_launch_descriptor_conflict", "Pi launch descriptor identity changed")
                return {
                    "descriptor_id": exact["descriptor_id"],
                    "state": existing["state"],
                    "version": int(existing["version"]),
                    "descriptor_digest": digest,
                    "idempotent": True,
                }
            store.connection.execute(
                """
                INSERT INTO provider_launch_descriptors
                  (descriptor_id,assignment_id,runtime_kind,provider_kind,role,placement,launch_mode,cwd,
                   parent_session_id,parent_session_path,state,descriptor_json,descriptor_digest,version,
                   created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'prepared',?,?,1,?,?)
                """,
                (
                    exact["descriptor_id"],
                    exact["assignment_id"],
                    exact["runtime_kind"],
                    exact["provider_kind"],
                    exact["role"],
                    exact["placement"],
                    exact["launch_mode"],
                    exact["cwd"],
                    exact["parent_session_id"],
                    exact["parent_session_path"],
                    _json(exact),
                    digest,
                    at,
                    at,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.IntegrityError as exc:
        raise StorageRefusal("provider_launch_lineage_conflict", "Pi fork lineage or session identity already exists") from exc
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "Pi launch descriptor preparation failed") from exc
    return {"descriptor_id": exact["descriptor_id"], "state": "prepared", "version": 1, "descriptor_digest": digest, "idempotent": False}


def bind_provider_launch(
    store: Any,
    descriptor_id: str,
    expected_version: int,
    observation: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    _time(at, "provider launch bind time")
    required = {
        "schema", "runtime_kind", "provider_kind", "session_id", "session_path",
        "parent_session_path", "cwd", "role", "placement", "callsign", "project_code",
        "task_label", "routing_name", "workspace_id", "tab_id", "pane_id", "terminal_id",
    }
    observed = dict(observation)
    if set(observed) != required or observed.get("schema") != "league.pi-launch-observation.v1":
        raise StorageRefusal("provider_launch_observation_invalid", "Pi launch observation fields are not exact")
    if not SESSION_ID.fullmatch(str(observed.get("session_id", ""))) or not _absolute(observed.get("session_path")):
        raise StorageRefusal("provider_launch_observation_invalid", "Pi session ID and path are required")
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT * FROM provider_launch_descriptors WHERE descriptor_id=?", (descriptor_id,)
            ).fetchone()
            if row is None:
                raise StorageRefusal("provider_launch_unknown", "Pi launch descriptor does not exist")
            descriptor = json.loads(row["descriptor_json"])
            receipt = json.loads(row["launch_receipt_json"]) if row["launch_receipt_json"] else None
            if row["state"] == "active":
                if receipt != observed:
                    raise StorageRefusal("provider_launch_observation_conflict", "active Pi launch has different identity")
                return {"descriptor_id": descriptor_id, "state": "active", "version": int(row["version"]), "descriptor_digest": row["descriptor_digest"], "receipt": receipt, "idempotent": True}
            if row["state"] != "prepared" or int(row["version"]) != expected_version:
                raise StorageRefusal("provider_launch_version_conflict", "Pi launch descriptor is not prepared at the expected version")
            for key in ("runtime_kind", "provider_kind", "cwd", "role", "placement", "callsign", "project_code", "task_label", "routing_name", "workspace_id"):
                if observed[key] != descriptor[key]:
                    raise StorageRefusal("provider_launch_observation_mismatch", "Pi launch observation changed an immutable descriptor field")
            mode = descriptor["launch_mode"]
            if mode == "create":
                identity_exact = observed["session_id"] == descriptor["requested_session_id"] and observed["parent_session_path"] is None
            elif mode == "resume":
                identity_exact = (
                    observed["session_id"] == descriptor["requested_session_id"]
                    and observed["session_path"] == descriptor["requested_session_path"]
                    and observed["parent_session_path"] == descriptor["parent_session_path"]
                )
            else:
                identity_exact = (
                    observed["session_id"] != descriptor["parent_session_id"]
                    and observed["session_path"] != descriptor["parent_session_path"]
                    and observed["parent_session_path"] == descriptor["parent_session_path"]
                )
            if not identity_exact:
                raise StorageRefusal("provider_launch_session_mismatch", "Pi create, fork, or resume identity did not verify")
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE provider_launch_descriptors
                   SET session_id=?,session_path=?,workspace_id=?,tab_id=?,pane_id=?,terminal_id=?,
                       state='active',launch_receipt_json=?,version=?,updated_at=?
                 WHERE descriptor_id=?
                """,
                (
                    observed["session_id"], observed["session_path"], observed["workspace_id"],
                    observed["tab_id"], observed["pane_id"], observed["terminal_id"],
                    _json(observed), next_version, at, descriptor_id,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.IntegrityError as exc:
        raise StorageRefusal("provider_launch_session_conflict", "Pi session is already bound to another descriptor") from exc
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "Pi launch binding failed") from exc
    return {"descriptor_id": descriptor_id, "state": "active", "version": next_version, "descriptor_digest": row["descriptor_digest"], "receipt": observed, "idempotent": False}


def provider_launch_descriptor(store: Any, descriptor_id: str) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT * FROM provider_launch_descriptors WHERE descriptor_id=?", (descriptor_id,)
    ).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["descriptor"] = json.loads(value.pop("descriptor_json"))
    value["launch_receipt"] = json.loads(value.pop("launch_receipt_json")) if value["launch_receipt_json"] else None
    return value


def claim_provider_restart(store: Any, descriptor_id: str, restart_id: str, pane_id: str, at: str) -> dict[str, Any]:
    _time(at, "provider restart claim time")
    if not SAFE_ID.fullmatch(restart_id) or not pane_id:
        raise StorageRefusal("provider_restart_identity_invalid", "Pi restart identity is invalid")
    try:
        with store._transaction():
            descriptor = store.connection.execute(
                "SELECT * FROM provider_launch_descriptors WHERE descriptor_id=?", (descriptor_id,)
            ).fetchone()
            if descriptor is None or descriptor["state"] != "active" or not descriptor["session_id"] or not descriptor["session_path"]:
                raise StorageRefusal("provider_restart_unavailable", "Pi launch descriptor is not active and resumable")
            if descriptor["pane_id"] != pane_id:
                raise StorageRefusal("provider_restart_pane_mismatch", "Pi restart pane differs from the durable launch descriptor")
            intent = {
                "schema": "league.pi-restart-intent.v1",
                "descriptor_id": descriptor_id,
                "restart_id": restart_id,
                "pane_id": pane_id,
                "session_id": descriptor["session_id"],
                "session_path": descriptor["session_path"],
                "descriptor_digest": descriptor["descriptor_digest"],
            }
            digest = _digest(intent)
            existing = store.connection.execute(
                "SELECT * FROM provider_restart_effects WHERE descriptor_id=? AND restart_id=?",
                (descriptor_id, restart_id),
            ).fetchone()
            if existing is not None:
                if existing["intent_digest"] != digest:
                    raise StorageRefusal("provider_restart_conflict", "Pi restart intent changed")
                return {"descriptor_id": descriptor_id, "restart_id": restart_id, "state": existing["state"], "intent_digest": digest, "descriptor": json.loads(descriptor["descriptor_json"]), "session_id": descriptor["session_id"], "session_path": descriptor["session_path"], "receipt": json.loads(existing["receipt_json"]) if existing["receipt_json"] else None, "idempotent": True}
            store.connection.execute(
                """
                INSERT INTO provider_restart_effects
                  (descriptor_id,restart_id,pane_id,session_id,session_path,state,intent_digest,created_at,updated_at)
                VALUES(?,?,?,?,?,'intent_recorded',?,?,?)
                """,
                (descriptor_id, restart_id, pane_id, descriptor["session_id"], descriptor["session_path"], digest, at, at),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "Pi restart claim failed") from exc
    return {"descriptor_id": descriptor_id, "restart_id": restart_id, "state": "intent_recorded", "intent_digest": digest, "descriptor": json.loads(descriptor["descriptor_json"]), "session_id": descriptor["session_id"], "session_path": descriptor["session_path"], "receipt": None, "idempotent": False}


def complete_provider_restart(store: Any, descriptor_id: str, restart_id: str, intent_digest: str, receipt: Mapping[str, Any], at: str) -> dict[str, Any]:
    _time(at, "provider restart completion time")
    exact = dict(receipt)
    effect_digest = _digest(exact)
    try:
        with store._transaction():
            effect = store.connection.execute(
                "SELECT * FROM provider_restart_effects WHERE descriptor_id=? AND restart_id=?",
                (descriptor_id, restart_id),
            ).fetchone()
            descriptor = store.connection.execute(
                "SELECT * FROM provider_launch_descriptors WHERE descriptor_id=?", (descriptor_id,)
            ).fetchone()
            if effect is None or descriptor is None or effect["intent_digest"] != intent_digest:
                raise StorageRefusal("provider_restart_conflict", "Pi restart intent is missing or changed")
            if effect["state"] == "effect_applied":
                stored = json.loads(effect["receipt_json"])
                if stored != exact:
                    raise StorageRefusal("provider_restart_conflict", "Pi restart receipt changed")
                return {"descriptor_id": descriptor_id, "restart_id": restart_id, "state": "effect_applied", "effect_digest": effect["effect_digest"], "receipt": stored, "idempotent": True}
            if effect["state"] != "intent_recorded" or exact.get("session_id") != descriptor["session_id"] or exact.get("session_path") != descriptor["session_path"] or exact.get("pane_id") != descriptor["pane_id"]:
                raise StorageRefusal("provider_restart_identity_mismatch", "Pi restart did not restore the exact session and pane")
            store.connection.execute(
                """
                UPDATE provider_restart_effects
                   SET state='effect_applied',effect_digest=?,receipt_json=?,updated_at=?
                 WHERE descriptor_id=? AND restart_id=?
                """,
                (effect_digest, _json(exact), at, descriptor_id, restart_id),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "Pi restart completion failed") from exc
    return {"descriptor_id": descriptor_id, "restart_id": restart_id, "state": "effect_applied", "effect_digest": effect_digest, "receipt": exact, "idempotent": False}


__all__ = [
    "bind_provider_launch",
    "claim_provider_restart",
    "complete_provider_restart",
    "prepare_provider_launch",
    "provider_launch_descriptor",
]
