"""Atomic canonical ownership operations for adapter-neutral runtime replacement."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from .storage_types import StorageRefusal


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$")
SAFE_ROUTE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal(
            "runtime_replacement_invalid", f"{label} must be RFC3339"
        ) from exc
    if parsed.tzinfo is None:
        raise StorageRefusal(
            "runtime_replacement_invalid", f"{label} must include a UTC offset"
        )
    return parsed


def _object(value: Any, code: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else dict(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StorageRefusal(code, "runtime replacement evidence is malformed") from exc
    if not isinstance(parsed, dict):
        raise StorageRefusal(code, "runtime replacement evidence is malformed")
    return parsed


def _request(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "operation_id",
        "assignment_id",
        "predecessor_agent_id",
        "predecessor_runtime_instance_id",
        "successor_agent_id",
        "successor_runtime_instance_id",
        "successor_adapter_kind",
        "successor_harness_kind",
        "successor_provider_kind",
        "multiplexer_kind",
        "canonical_routing_name",
        "staging_routing_name",
        "routing_decision_id",
        "model",
        "effort",
        "expected_assignment_version",
        "expected_agent_version",
        "expected_task_version",
    }
    request = dict(value)
    strings = required - {
        "routing_decision_id",
        "expected_assignment_version",
        "expected_agent_version",
        "expected_task_version",
    }
    if (
        set(request) != required
        or request.get("schema") != "league.runtime-replacement-request.v1"
        or any(
            not isinstance(request.get(key), str)
            or not request[key]
            or len(request[key].encode("utf-8")) > 1024
            for key in strings
        )
        or any(
            not isinstance(request.get(key), int) or request[key] < 1
            for key in (
                "expected_assignment_version",
                "expected_agent_version",
                "expected_task_version",
            )
        )
        or not all(
            SAFE_ID.fullmatch(request[key])
            for key in (
                "operation_id",
                "assignment_id",
                "predecessor_agent_id",
                "predecessor_runtime_instance_id",
                "successor_agent_id",
                "successor_runtime_instance_id",
                "successor_adapter_kind",
                "successor_harness_kind",
                "successor_provider_kind",
                "multiplexer_kind",
            )
        )
        or not SAFE_ROUTE.fullmatch(request["canonical_routing_name"])
        or not SAFE_ROUTE.fullmatch(request["staging_routing_name"])
        or request["canonical_routing_name"] == request["staging_routing_name"]
        or request["successor_agent_id"] == request["predecessor_agent_id"]
        or request["successor_runtime_instance_id"]
        == request["predecessor_runtime_instance_id"]
        or (
            request["routing_decision_id"] is not None
            and (
                not isinstance(request["routing_decision_id"], str)
                or not SAFE_ID.fullmatch(request["routing_decision_id"])
            )
        )
    ):
        raise StorageRefusal(
            "runtime_replacement_invalid",
            "runtime replacement request identity is incomplete",
        )
    return request


def _row(store: Any, table: str, column: str, value: str) -> sqlite3.Row:
    row = store.connection.execute(
        f"SELECT * FROM {table} WHERE {column}=?", (value,)
    ).fetchone()
    if row is None:
        raise StorageRefusal(
            "runtime_replacement_identity_mismatch",
            "runtime replacement canonical identity is missing",
        )
    return row


def _requirements(store: Any, assignment_id: str) -> tuple[str, ...]:
    row = _row(
        store,
        "callsign_assignments",
        "callsign_assignment_id",
        f"callsign-assignment:{assignment_id}",
    )
    try:
        result = tuple(json.loads(row["requirements_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal(
            "runtime_replacement_identity_mismatch",
            "runtime replacement callsign capabilities are malformed",
        ) from exc
    if any(not isinstance(item, str) or not item for item in result):
        raise StorageRefusal(
            "runtime_replacement_identity_mismatch",
            "runtime replacement callsign capabilities are malformed",
        )
    return result


def _pi_descriptor(
    store: Any,
    *,
    assignment_id: str,
    session_ref: str,
    states: tuple[str, ...] = ("active",),
) -> sqlite3.Row:
    placeholders = ",".join("?" for _ in states)
    rows = store.connection.execute(
        f"""
        SELECT * FROM provider_launch_descriptors
         WHERE assignment_id=? AND session_path=? AND state IN ({placeholders})
         ORDER BY descriptor_id
        """,
        (assignment_id, session_ref, *states),
    ).fetchall()
    if len(rows) != 1:
        raise StorageRefusal(
            "runtime_replacement_descriptor_ambiguous",
            "Pi runtime replacement requires one exact provider descriptor",
        )
    return rows[0]


def _settle_pi_descriptors_for_activation(
    store: Any,
    row: Mapping[str, Any],
    intent: Mapping[str, Any],
    successor: Mapping[str, Any],
    at: str,
) -> None:
    predecessor_kind = str(intent["predecessor_adapter_kind"])
    if predecessor_kind == "pi":
        predecessor = _pi_descriptor(
            store,
            assignment_id=str(row["assignment_id"]),
            session_ref=str(intent["snapshot"]["runtime"]["session_ref"]),
        )
        store.connection.execute(
            """
            UPDATE provider_launch_descriptors
               SET state='blocked',version=version+1,updated_at=?
             WHERE descriptor_id=? AND state='active'
            """,
            (at, predecessor["descriptor_id"]),
        )
    if row["successor_adapter_kind"] == "pi":
        successor_descriptor = _pi_descriptor(
            store,
            assignment_id=str(row["assignment_id"]),
            session_ref=str(successor["thread_id"]),
        )
        expected_id = f"runtime-replacement:{row['operation_id']}"
        if successor_descriptor["descriptor_id"] != expected_id:
            raise StorageRefusal(
                "runtime_replacement_descriptor_mismatch",
                "successor Pi descriptor is not the exact replacement launch",
            )


def _settle_pi_descriptors_for_rollback(
    store: Any,
    row: Mapping[str, Any],
    intent: Mapping[str, Any],
    *,
    activated: bool,
    at: str,
) -> None:
    if row["successor_adapter_kind"] == "pi":
        expected_id = f"runtime-replacement:{row['operation_id']}"
        store.connection.execute(
            """
            UPDATE provider_launch_descriptors
               SET state='blocked',version=version+1,updated_at=?
             WHERE descriptor_id=? AND assignment_id=? AND state='active'
            """,
            (at, expected_id, row["assignment_id"]),
        )
    if activated and str(intent["predecessor_adapter_kind"]) == "pi":
        predecessor = _pi_descriptor(
            store,
            assignment_id=str(row["assignment_id"]),
            session_ref=str(intent["snapshot"]["runtime"]["session_ref"]),
            states=("blocked",),
        )
        store.connection.execute(
            """
            UPDATE provider_launch_descriptors
               SET state='active',version=version+1,updated_at=?
             WHERE descriptor_id=? AND state='blocked'
            """,
            (at, predecessor["descriptor_id"]),
        )


def prepare_runtime_replacement(
    store: Any, request: Mapping[str, Any], at: str
) -> dict[str, Any]:
    """Freeze exact predecessor ownership behind one durable open-operation fence."""

    _time(at, "runtime replacement preparation time")
    exact = _request(request)
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM runtime_replacements WHERE operation_id=?",
                (exact["operation_id"],),
            ).fetchone()
            if existing is not None:
                stored = _object(existing["intent_json"], "runtime_replacement_conflict")
                if stored.get("request") != exact:
                    raise StorageRefusal(
                        "runtime_replacement_conflict",
                        "runtime replacement retry changed immutable identity",
                    )
                return _prepared_result(existing, stored, idempotent=True)

            assignment = _row(
                store, "task_assignments", "task_assignment_id", exact["assignment_id"]
            )
            agent = _row(
                store, "agent_instances", "agent_id", exact["predecessor_agent_id"]
            )
            runtime = _row(
                store,
                "runtime_instances",
                "runtime_instance_id",
                exact["predecessor_runtime_instance_id"],
            )
            task = _row(store, "tasks", "task_id", str(assignment["task_id"]))
            callsign = _row(
                store,
                "callsign_assignments",
                "callsign_assignment_id",
                f"callsign-assignment:{exact['assignment_id']}",
            )
            lease = _row(store, "callsign_leases", "callsign", str(assignment["callsign"]))
            squads = [
                dict(item)
                for item in store.connection.execute(
                    "SELECT * FROM squad_champions WHERE champion_agent_id=? ORDER BY squad_id",
                    (exact["predecessor_agent_id"],),
                ).fetchall()
            ]
            acceptance = _object(
                assignment["acceptance_receipt_json"],
                "runtime_replacement_identity_mismatch",
            )
            predecessor_adapter_kind = str(agent["kind"]).removesuffix("-thread")
            predecessor_provider_kind = acceptance.get("provider_kind")
            if not isinstance(predecessor_provider_kind, str) or not predecessor_provider_kind:
                predecessor_provider_kind = agent["display_agent"]
            exact_owner = bool(
                assignment["assignment_role"] == "champion"
                and assignment["state"] == "active"
                and int(assignment["version"])
                == exact["expected_assignment_version"]
                and assignment["champion_agent_id"]
                == exact["predecessor_agent_id"]
                and assignment["runtime_instance_id"]
                == exact["predecessor_runtime_instance_id"]
                and agent["role"] == "champion"
                and agent["retired_at"] is None
                and int(agent["version"]) == exact["expected_agent_version"]
                and agent["task_id"] == assignment["task_id"]
                and agent["callsign"] == assignment["callsign"]
                and agent["routing_name"] == exact["canonical_routing_name"]
                and runtime["actor_agent_id"] == exact["predecessor_agent_id"]
                and runtime["status"] in {"active", "idle"}
                and bool(runtime["verified"])
                and runtime["session_ref"] == agent["thread_id"]
                and runtime["endpoint"] == agent["address"]
                and runtime["harness_kind"] == agent["kind"]
                and runtime["backend_kind"] == agent["backend"]
                and int(task["version"]) == exact["expected_task_version"]
                and task["champion_agent_id"] == exact["predecessor_agent_id"]
                and callsign["state"] == "active"
                and callsign["agent_id"] == exact["predecessor_agent_id"]
                and callsign["runtime_instance_id"]
                in {None, exact["predecessor_runtime_instance_id"]}
                and callsign["callsign"] == assignment["callsign"]
                and lease["agent_id"] == exact["predecessor_agent_id"]
                and acceptance.get("verified") is True
                and acceptance.get("thread_id") == runtime["session_ref"]
                and acceptance.get("runtime_generation")
                == runtime["runtime_generation"]
                and acceptance.get("endpoint") == runtime["endpoint"]
                and acceptance.get("backend_kind") == exact["multiplexer_kind"]
                and predecessor_adapter_kind
                and isinstance(predecessor_provider_kind, str)
                and bool(predecessor_provider_kind)
            )
            if not exact_owner:
                raise StorageRefusal(
                    "runtime_replacement_identity_mismatch",
                    "predecessor is not the exact active assignment owner and runtime",
                )
            if exact["multiplexer_kind"] != runtime["backend_kind"]:
                raise StorageRefusal(
                    "runtime_replacement_multiplexer_mismatch",
                    "successor must use the assignment's exact active multiplexer",
                )
            if store.connection.execute(
                "SELECT 1 FROM agent_instances WHERE agent_id=?",
                (exact["successor_agent_id"],),
            ).fetchone() is not None or store.connection.execute(
                "SELECT 1 FROM runtime_instances WHERE runtime_instance_id=?",
                (exact["successor_runtime_instance_id"],),
            ).fetchone() is not None:
                raise StorageRefusal(
                    "runtime_replacement_successor_conflict",
                    "successor agent or runtime identity already exists",
                )
            snapshot = {
                "agent": dict(agent),
                "runtime": dict(runtime),
                "assignment": dict(assignment),
                "task": dict(task),
                "callsign_assignment": dict(callsign),
                "callsign_lease": dict(lease),
                "squad_memberships": squads,
            }
            intent = {
                "schema": "league.runtime-replacement-intent.v1",
                "request": exact,
                "predecessor_adapter_kind": predecessor_adapter_kind,
                "predecessor_provider_kind": predecessor_provider_kind,
                "snapshot": snapshot,
            }
            digest = _digest(intent)
            store.connection.execute(
                """
                INSERT INTO runtime_replacements
                  (operation_id,assignment_id,task_id,predecessor_agent_id,
                   predecessor_runtime_instance_id,successor_agent_id,
                   successor_runtime_instance_id,successor_adapter_kind,
                   successor_provider_kind,multiplexer_kind,canonical_routing_name,
                   staging_routing_name,state,intent_json,intent_digest,version,
                   created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'prepared',?,?,1,?,?)
                """,
                (
                    exact["operation_id"],
                    exact["assignment_id"],
                    assignment["task_id"],
                    exact["predecessor_agent_id"],
                    exact["predecessor_runtime_instance_id"],
                    exact["successor_agent_id"],
                    exact["successor_runtime_instance_id"],
                    exact["successor_adapter_kind"],
                    exact["successor_provider_kind"],
                    exact["multiplexer_kind"],
                    exact["canonical_routing_name"],
                    exact["staging_routing_name"],
                    _json(intent),
                    digest,
                    at,
                    at,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.IntegrityError as exc:
        raise StorageRefusal(
            "runtime_replacement_conflict",
            "another open replacement owns this assignment or successor",
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "runtime replacement preparation conflicted with canonical state"
        ) from exc
    row = store.connection.execute(
        "SELECT * FROM runtime_replacements WHERE operation_id=?",
        (exact["operation_id"],),
    ).fetchone()
    assert row is not None
    return _prepared_result(row, intent, idempotent=False)


_EFFECT_TRANSITIONS = {
    "launch": ("prepared", "launching"),
    "route_swap": ("successor_verified", "route_swapping"),
    "retirement": ("activated", "retiring"),
}


def begin_runtime_replacement_effect(
    store: Any,
    operation_id: str,
    expected_version: int,
    intent_digest: str,
    effect: str,
    at: str,
) -> dict[str, Any]:
    """Durably fence an external effect before it can touch a native runtime."""

    _time(at, "runtime replacement effect time")
    if effect not in _EFFECT_TRANSITIONS:
        raise StorageRefusal(
            "runtime_replacement_effect_invalid",
            "runtime replacement effect is unsupported",
        )
    source, target = _EFFECT_TRANSITIONS[effect]
    with store._transaction():
        row = _row(store, "runtime_replacements", "operation_id", operation_id)
        if row["intent_digest"] != intent_digest:
            raise StorageRefusal(
                "runtime_replacement_conflict",
                "runtime replacement intent digest changed",
            )
        if row["state"] == target:
            return {
                "operation_id": operation_id,
                "state": target,
                "version": int(row["version"]),
                "idempotent": True,
            }
        if row["state"] != source or int(row["version"]) != expected_version:
            raise StorageRefusal(
                "runtime_replacement_effect_conflict",
                "runtime replacement is not at the exact effect boundary",
            )
        next_version = expected_version + 1
        store.connection.execute(
            """
            UPDATE runtime_replacements
               SET state=?,version=?,updated_at=?
             WHERE operation_id=? AND state=? AND version=?
            """,
            (target, next_version, at, operation_id, source, expected_version),
        )
    return {
        "operation_id": operation_id,
        "state": target,
        "version": next_version,
        "idempotent": False,
    }


def assert_runtime_replacement_mutation_allowed(
    store: Any,
    *,
    agent_id: str | None = None,
    task_id: str | None = None,
    assignment_id: str | None = None,
) -> None:
    """Fence predecessor writes while an exact replacement owns the assignment."""

    if not any((agent_id, task_id, assignment_id)):
        return
    row = store.connection.execute(
        """
        SELECT operation_id FROM runtime_replacements
         WHERE state IN (
           'prepared','launching','successor_verified','route_swapping',
           'activated','retiring','predecessor_retired','recovery_required'
         )
           AND ((? IS NOT NULL AND predecessor_agent_id=?)
             OR (? IS NOT NULL AND task_id=?)
             OR (? IS NOT NULL AND assignment_id=?))
         LIMIT 1
        """,
        (agent_id, agent_id, task_id, task_id, assignment_id, assignment_id),
    ).fetchone()
    if row is not None:
        raise StorageRefusal(
            "runtime_replacement_fenced",
            "an open runtime replacement owns this predecessor mutation boundary",
        )


def runtime_replacement_mutation_fenced(store: Any, agent_id: str) -> bool:
    return store.connection.execute(
        """
        SELECT 1 FROM runtime_replacements r
          JOIN agent_instances a ON a.task_id=r.task_id
         WHERE a.agent_id=? AND a.retired_at IS NULL
           AND r.state IN (
             'prepared','launching','successor_verified','route_swapping',
             'activated','retiring','predecessor_retired','recovery_required'
           )
         LIMIT 1
        """,
        (agent_id,),
    ).fetchone() is not None


def _prepared_result(
    row: Mapping[str, Any], intent: Mapping[str, Any], *, idempotent: bool
) -> dict[str, Any]:
    snapshot = intent["snapshot"]
    agent = snapshot["agent"]
    runtime = snapshot["runtime"]
    assignment = snapshot["assignment"]
    callsign = snapshot["callsign_assignment"]
    return {
        "operation_id": row["operation_id"],
        "assignment_id": row["assignment_id"],
        "task_id": row["task_id"],
        "state": row["state"],
        "version": int(row["version"]),
        "intent_digest": row["intent_digest"],
        "predecessor": {
            "agent_id": row["predecessor_agent_id"],
            "runtime_instance_id": row["predecessor_runtime_instance_id"],
            "adapter_kind": intent["predecessor_adapter_kind"],
            "provider_kind": intent["predecessor_provider_kind"],
            "multiplexer_kind": runtime["backend_kind"],
            "session_ref": runtime["session_ref"],
            "endpoint": runtime["endpoint"],
            "runtime_generation": runtime["runtime_generation"],
            "cwd": agent["worktree"],
            "routing_name": agent["routing_name"],
        },
        "successor": {
            "agent_id": row["successor_agent_id"],
            "runtime_instance_id": row["successor_runtime_instance_id"],
            "adapter_kind": row["successor_adapter_kind"],
            "provider_kind": row["successor_provider_kind"],
            "multiplexer_kind": row["multiplexer_kind"],
            "harness_kind": intent["request"]["successor_harness_kind"],
            "routing_name": row["staging_routing_name"],
        },
        "launch": {
            "request_id": assignment["request_id"],
            "task_summary": snapshot["task"]["summary"],
            "coordinator_agent_id": assignment["coordinator_agent_id"],
            "callsign": assignment["callsign"],
            "repository": agent["repository"],
            "issue": int(agent["issue"]),
            "branch": agent["branch"],
            "worktree": agent["worktree"],
            "required_capabilities": list(
                json.loads(callsign["requirements_json"])
            ),
        },
        "successor_receipt": (
            _object(
                row["successor_receipt_json"],
                "runtime_replacement_successor_unverified",
            )
            if row["successor_receipt_json"]
            else None
        ),
        "route_receipt": (
            _object(
                row["route_receipt_json"],
                "runtime_replacement_route_conflict",
            )
            if row["route_receipt_json"]
            else None
        ),
        "retirement_receipt": (
            _object(
                row["retirement_receipt_json"],
                "runtime_replacement_retirement_conflict",
            )
            if row["retirement_receipt_json"]
            else None
        ),
        "recovery_state": row["recovery_state"],
        "idempotent": idempotent,
    }


def record_successor_verified(
    store: Any,
    operation_id: str,
    expected_version: int,
    intent_digest: str,
    receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    _time(at, "successor verification time")
    observed = dict(receipt)
    try:
        with store._transaction():
            row = _row(store, "runtime_replacements", "operation_id", operation_id)
            intent = _object(row["intent_json"], "runtime_replacement_conflict")
            request = intent["request"]
            if row["intent_digest"] != intent_digest:
                raise StorageRefusal(
                    "runtime_replacement_conflict",
                    "runtime replacement intent digest changed",
                )
            if row["state"] in {
                "successor_verified",
                "route_swapping",
                "activated",
                "retiring",
                "predecessor_retired",
                "completed",
            }:
                stored = _object(
                    row["successor_receipt_json"], "runtime_replacement_receipt_conflict"
                )
                if stored != observed:
                    raise StorageRefusal(
                        "runtime_replacement_receipt_conflict",
                        "successor retry changed exact launch identity",
                    )
                return {
                    "operation_id": operation_id,
                    "state": row["state"],
                    "version": int(row["version"]),
                    "receipt_digest": _digest(stored),
                    "idempotent": True,
                }
            required = {
                "verified",
                "assignment_id",
                "task_id",
                "champion_agent_id",
                "callsign",
                "runtime_instance_id",
                "thread_id",
                "endpoint",
                "runtime_generation",
                "harness_kind",
                "backend_kind",
                "routing_name",
                "display_agent",
                "repository",
                "issue",
                "branch",
                "worktree",
                "capabilities",
            }
            launch = _prepared_result(row, intent, idempotent=True)["launch"]
            capabilities = observed.get("capabilities")
            exact = bool(
                row["state"] == "launching"
                and int(row["version"]) == expected_version
                and required <= set(observed)
                and observed.get("verified") is True
                and observed.get("assignment_id") == row["assignment_id"]
                and observed.get("task_id") == row["task_id"]
                and observed.get("champion_agent_id") == row["successor_agent_id"]
                and observed.get("callsign") == launch["callsign"]
                and observed.get("runtime_instance_id")
                == row["successor_runtime_instance_id"]
                and isinstance(observed.get("thread_id"), str)
                and bool(observed["thread_id"])
                and observed.get("harness_kind")
                == request["successor_harness_kind"]
                and observed.get("backend_kind") == row["multiplexer_kind"]
                and observed.get("routing_name") == row["staging_routing_name"]
                and observed.get("display_agent") == row["successor_provider_kind"]
                and observed.get("repository") == launch["repository"]
                and observed.get("issue") == launch["issue"]
                and observed.get("branch") == launch["branch"]
                and observed.get("worktree") == launch["worktree"]
                and isinstance(observed.get("endpoint"), str)
                and bool(observed["endpoint"])
                and isinstance(observed.get("runtime_generation"), str)
                and bool(observed["runtime_generation"])
                and isinstance(capabilities, list)
                and all(isinstance(item, str) and item for item in capabilities)
                and set(launch["required_capabilities"]) <= set(capabilities)
            )
            if not exact:
                raise StorageRefusal(
                    "runtime_replacement_successor_unverified",
                    "successor launch receipt does not match the frozen replacement",
                )
            collision = store.connection.execute(
                """
                SELECT runtime_instance_id FROM runtime_instances
                 WHERE runtime_instance_id=? OR (harness_kind=? AND session_ref=? AND status IN ('active','idle'))
                """,
                (
                    row["successor_runtime_instance_id"],
                    observed["harness_kind"],
                    observed["thread_id"],
                ),
            ).fetchone()
            if collision is not None:
                raise StorageRefusal(
                    "runtime_replacement_successor_conflict",
                    "successor runtime or native session already has canonical ownership",
                )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE runtime_replacements
                   SET state='successor_verified',successor_receipt_json=?,version=?,updated_at=?
                 WHERE operation_id=? AND state='launching' AND version=?
                """,
                (_json(observed), next_version, at, operation_id, expected_version),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "successor verification conflicted with canonical state"
        ) from exc
    return {
        "operation_id": operation_id,
        "state": "successor_verified",
        "version": next_version,
        "receipt_digest": _digest(observed),
        "idempotent": False,
    }


def activate_runtime_replacement(
    store: Any,
    operation_id: str,
    expected_version: int,
    intent_digest: str,
    route_receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    """Atomically move all canonical ownership only after B is proven."""

    _time(at, "runtime replacement activation time")
    route = dict(route_receipt)
    try:
        with store._transaction():
            row = _row(store, "runtime_replacements", "operation_id", operation_id)
            intent = _object(row["intent_json"], "runtime_replacement_conflict")
            successor = _object(
                row["successor_receipt_json"], "runtime_replacement_successor_unverified"
            )
            if row["intent_digest"] != intent_digest:
                raise StorageRefusal(
                    "runtime_replacement_conflict",
                    "runtime replacement intent digest changed",
                )
            if row["state"] in {
                "activated", "retiring", "predecessor_retired", "completed"
            }:
                stored = _object(
                    row["route_receipt_json"], "runtime_replacement_route_conflict"
                )
                if stored != route:
                    raise StorageRefusal(
                        "runtime_replacement_route_conflict",
                        "replacement route retry changed exact identity",
                    )
                return {
                    "operation_id": operation_id,
                    "state": row["state"],
                    "version": int(row["version"]),
                    "idempotent": True,
                }
            snapshot = intent["snapshot"]
            request = intent["request"]
            old_agent = snapshot["agent"]
            old_runtime = snapshot["runtime"]
            old_assignment = snapshot["assignment"]
            old_task = snapshot["task"]
            old_callsign = snapshot["callsign_assignment"]
            route_exact = bool(
                row["state"] == "route_swapping"
                and int(row["version"]) == expected_version
                and route.get("verified") is True
                and route.get("operation_id") == operation_id
                and route.get("predecessor_agent_id") == row["predecessor_agent_id"]
                and route.get("successor_agent_id") == row["successor_agent_id"]
                and route.get("canonical_routing_name")
                == row["canonical_routing_name"]
                and route.get("successor_previous_routing_name")
                == row["staging_routing_name"]
                and isinstance(route.get("predecessor_staging_routing_name"), str)
                and bool(route["predecessor_staging_routing_name"])
            )
            assignment = _row(
                store, "task_assignments", "task_assignment_id", row["assignment_id"]
            )
            agent = _row(
                store, "agent_instances", "agent_id", row["predecessor_agent_id"]
            )
            runtime = _row(
                store,
                "runtime_instances",
                "runtime_instance_id",
                row["predecessor_runtime_instance_id"],
            )
            task = _row(store, "tasks", "task_id", row["task_id"])
            callsign = _row(
                store,
                "callsign_assignments",
                "callsign_assignment_id",
                f"callsign-assignment:{row['assignment_id']}",
            )
            unchanged = bool(
                route_exact
                and int(assignment["version"])
                == request["expected_assignment_version"]
                and assignment["champion_agent_id"] == row["predecessor_agent_id"]
                and assignment["runtime_instance_id"]
                == row["predecessor_runtime_instance_id"]
                and int(agent["version"]) == request["expected_agent_version"]
                and agent["retired_at"] is None
                and runtime["status"] in {"active", "idle"}
                and bool(runtime["verified"])
                and int(task["version"]) == request["expected_task_version"]
                and task["champion_agent_id"] == row["predecessor_agent_id"]
                and callsign["agent_id"] == row["predecessor_agent_id"]
                and callsign["runtime_instance_id"]
                in {None, row["predecessor_runtime_instance_id"]}
            )
            if not unchanged:
                raise StorageRefusal(
                    "runtime_replacement_version_conflict",
                    "predecessor ownership changed before the atomic switch",
                )
            if store.connection.execute(
                "SELECT 1 FROM agent_instances WHERE agent_id=?",
                (row["successor_agent_id"],),
            ).fetchone() is not None:
                raise StorageRefusal(
                    "runtime_replacement_successor_conflict",
                    "successor agent identity became occupied before activation",
                )
            successor_acceptance = dict(successor)
            successor_acceptance["routing_name"] = row["canonical_routing_name"]
            if isinstance(successor_acceptance.get("routing"), dict):
                successor_acceptance["routing"] = dict(successor_acceptance["routing"])
            capabilities = successor.get("capabilities", [])
            # Release the live callsign uniqueness fence before inserting B.
            # Both statements remain inside this one transaction, so readers
            # can observe only the complete A-to-B ownership switch.
            store.connection.execute(
                """
                UPDATE agent_instances
                   SET status='completed',routing_name=NULL,display_agent=NULL,
                       version=version+1,updated_at=?,update_text='ownership replaced',
                       next_action='Retain immutable predecessor history',retired_at=?
                 WHERE agent_id=? AND version=? AND retired_at IS NULL
                """,
                (
                    at,
                    at,
                    row["predecessor_agent_id"],
                    request["expected_agent_version"],
                ),
            )
            store.connection.execute(
                """
                INSERT INTO agent_instances
                  (agent_id,callsign,role,shotcaller_agent_id,task_id,kind,address,
                   thread_id,backend,routing_name,display_agent,repository,issue,branch,
                   worktree,status,version,updated_at,update_text,blocker,next_action,
                   metadata_json,retired_at)
                VALUES(?,?,'champion',?,?,?,?,?,?,?,?,?,?,?,?, 'active',1,?,
                       'replacement successor verified',NULL,
                       'Await predecessor retirement and exact handoff','{}',NULL)
                """,
                (
                    row["successor_agent_id"],
                    old_agent["callsign"],
                    old_agent["shotcaller_agent_id"],
                    row["task_id"],
                    successor["harness_kind"],
                    successor["endpoint"],
                    successor["thread_id"],
                    successor["backend_kind"],
                    row["canonical_routing_name"],
                    successor["display_agent"],
                    old_agent["repository"],
                    old_agent["issue"],
                    old_agent["branch"],
                    old_agent["worktree"],
                    at,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO runtime_instances
                  (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,
                   session_ref,endpoint,runtime_generation,status,verified,last_seen_at,
                   capabilities_json)
                VALUES(?,?,?,?,?,?,?,'active',1,?,?)
                """,
                (
                    row["successor_runtime_instance_id"],
                    row["successor_agent_id"],
                    successor["harness_kind"],
                    successor["backend_kind"],
                    successor["thread_id"],
                    successor["endpoint"],
                    successor["runtime_generation"],
                    at,
                    _json(capabilities),
                ),
            )
            store.connection.execute(
                """
                UPDATE runtime_instances
                   SET status='closed',verified=0,last_seen_at=?
                 WHERE runtime_instance_id=? AND actor_agent_id=?
                """,
                (
                    at,
                    row["predecessor_runtime_instance_id"],
                    row["predecessor_agent_id"],
                ),
            )
            next_assignment_version = int(assignment["version"]) + 1
            store.connection.execute(
                """
                UPDATE task_assignments
                   SET champion_agent_id=?,runtime_instance_id=?,acceptance_receipt_json=?,
                       version=?,updated_at=?
                 WHERE task_assignment_id=?
                """,
                (
                    row["successor_agent_id"],
                    row["successor_runtime_instance_id"],
                    _json(successor_acceptance),
                    next_assignment_version,
                    at,
                    row["assignment_id"],
                ),
            )
            store.connection.execute(
                """
                UPDATE tasks
                   SET champion_agent_id=?,
                       current_owner_agent_id=CASE WHEN current_owner_agent_id=? THEN ? ELSE current_owner_agent_id END,
                       version=version+1,updated_at=?
                 WHERE task_id=?
                """,
                (
                    row["successor_agent_id"],
                    row["predecessor_agent_id"],
                    row["successor_agent_id"],
                    at,
                    row["task_id"],
                ),
            )
            acceptance_digest = _digest(successor_acceptance)
            store.connection.execute(
                """
                UPDATE callsign_assignments
                   SET subject_id=?,agent_id=?,runtime_instance_id=?,acceptance_digest=?,
                       version=version+1
                 WHERE callsign_assignment_id=?
                """,
                (
                    f"agent:{row['successor_agent_id']}",
                    row["successor_agent_id"],
                    row["successor_runtime_instance_id"],
                    acceptance_digest,
                    f"callsign-assignment:{row['assignment_id']}",
                ),
            )
            store.connection.execute(
                "UPDATE callsign_leases SET agent_id=? WHERE callsign=? AND agent_id=?",
                (
                    row["successor_agent_id"],
                    old_agent["callsign"],
                    row["predecessor_agent_id"],
                ),
            )
            store.connection.execute(
                "UPDATE squad_champions SET champion_agent_id=? WHERE champion_agent_id=?",
                (row["successor_agent_id"], row["predecessor_agent_id"]),
            )
            # Pending source history follows the current owner. Delivered rows
            # and their immutable recipient receipts remain predecessor history.
            store.connection.execute(
                """
                UPDATE delivery_outbox SET recipient_agent_id=?
                 WHERE recipient_agent_id=? AND state='pending'
                   AND NOT EXISTS (
                     SELECT 1 FROM delivery_outbox other
                      WHERE other.event_id=delivery_outbox.event_id
                        AND other.recipient_agent_id=?
                   )
                """,
                (
                    row["successor_agent_id"],
                    row["predecessor_agent_id"],
                    row["successor_agent_id"],
                ),
            )
            _settle_pi_descriptors_for_activation(
                store, row, intent, successor, at
            )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE runtime_replacements
                   SET state='activated',route_receipt_json=?,version=?,updated_at=?
                 WHERE operation_id=? AND state='route_swapping' AND version=?
                """,
                (_json(route), next_version, at, operation_id, expected_version),
            )
    except StorageRefusal:
        raise
    except sqlite3.IntegrityError as exc:
        raise StorageRefusal(
            "runtime_replacement_activation_conflict",
            "atomic successor ownership switch conflicted",
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "atomic runtime replacement conflicted with canonical state"
        ) from exc
    return {
        "operation_id": operation_id,
        "assignment_id": row["assignment_id"],
        "task_id": row["task_id"],
        "state": "activated",
        "version": next_version,
        "successor_agent_id": row["successor_agent_id"],
        "successor_runtime_instance_id": row["successor_runtime_instance_id"],
        "assignment_version": next_assignment_version,
        "idempotent": False,
    }


def complete_runtime_replacement(
    store: Any,
    operation_id: str,
    expected_version: int,
    intent_digest: str,
    retirement_receipt: Mapping[str, Any],
    event_id: str,
    outbox_id: str,
    at: str,
) -> dict[str, Any]:
    """Release B's exactly-once handoff only after exact A retirement."""

    _time(at, "runtime replacement completion time")
    retirement = dict(retirement_receipt)
    try:
        with store._transaction():
            row = _row(store, "runtime_replacements", "operation_id", operation_id)
            intent = _object(row["intent_json"], "runtime_replacement_conflict")
            old_runtime = intent["snapshot"]["runtime"]
            if row["intent_digest"] != intent_digest:
                raise StorageRefusal(
                    "runtime_replacement_conflict",
                    "runtime replacement intent digest changed",
                )
            if row["state"] == "completed":
                stored = _object(
                    row["retirement_receipt_json"],
                    "runtime_replacement_retirement_conflict",
                )
                if (
                    stored != retirement
                    or row["handoff_event_id"] != event_id
                    or row["handoff_outbox_id"] != outbox_id
                ):
                    raise StorageRefusal(
                        "runtime_replacement_retirement_conflict",
                        "completed replacement retry changed its exact receipt",
                    )
                return {
                    "operation_id": operation_id,
                    "state": "completed",
                    "version": int(row["version"]),
                    "event_id": event_id,
                    "outbox_id": outbox_id,
                    "recipient_agent_id": row["successor_agent_id"],
                    "idempotent": True,
                }
            stored_retirement = (
                _object(
                    row["retirement_receipt_json"],
                    "runtime_replacement_retirement_conflict",
                )
                if row["retirement_receipt_json"]
                else None
            )
            exact = bool(
                row["state"] == "predecessor_retired"
                and int(row["version"]) == expected_version
                and stored_retirement == retirement
                and retirement.get("verified") is True
                and retirement.get("operation_id") == operation_id
                and retirement.get("agent_id") == row["predecessor_agent_id"]
                and retirement.get("runtime_instance_id")
                == row["predecessor_runtime_instance_id"]
                and retirement.get("session_ref") == old_runtime["session_ref"]
                and retirement.get("endpoint") == old_runtime["endpoint"]
                and retirement.get("runtime_generation")
                == old_runtime["runtime_generation"]
                and retirement.get("state") == "retired"
                and isinstance(event_id, str)
                and bool(event_id)
                and isinstance(outbox_id, str)
                and bool(outbox_id)
            )
            if not exact:
                raise StorageRefusal(
                    "runtime_replacement_retirement_unverified",
                    "predecessor retirement receipt is not exact",
                )
            assignment = _row(
                store, "task_assignments", "task_assignment_id", row["assignment_id"]
            )
            if (
                assignment["champion_agent_id"] != row["successor_agent_id"]
                or assignment["runtime_instance_id"]
                != row["successor_runtime_instance_id"]
                or assignment["state"] != "active"
            ):
                raise StorageRefusal(
                    "runtime_replacement_version_conflict",
                    "successor lost canonical ownership before handoff",
                )
            next_agent_version = int(
                _row(store, "agent_instances", "agent_id", row["successor_agent_id"])[
                    "version"
                ]
            ) + 1
            store.connection.execute(
                """
                UPDATE agent_instances
                   SET status='working',version=?,updated_at=?,
                       update_text='runtime replacement handoff committed',
                       next_action='Continue the exact issue-owned task'
                 WHERE agent_id=? AND retired_at IS NULL
                """,
                (next_agent_version, at, row["successor_agent_id"]),
            )
            detail = {
                "schema": "league.runtime-replacement-handoff.v1",
                "operation_id": operation_id,
                "predecessor_agent_id": row["predecessor_agent_id"],
                "predecessor_runtime_instance_id": row[
                    "predecessor_runtime_instance_id"
                ],
                "successor_agent_id": row["successor_agent_id"],
                "successor_runtime_instance_id": row[
                    "successor_runtime_instance_id"
                ],
                "intent_digest": row["intent_digest"],
                "successor_receipt_digest": _digest(
                    _object(
                        row["successor_receipt_json"],
                        "runtime_replacement_successor_unverified",
                    )
                ),
                "retirement_receipt_digest": _digest(retirement),
                "runtime_instance_id": row["successor_runtime_instance_id"],
                "runtime_generation": _object(
                    row["successor_receipt_json"],
                    "runtime_replacement_successor_unverified",
                )["runtime_generation"],
            }
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,
                   update_text,occurred_at,detail_json,request_id,aggregate_kind,
                   aggregate_id)
                VALUES(?,NULL,?,?,'runtime_replacement_handoff','working',?, ?,?,NULL,
                       'runtime_replacement',?)
                """,
                (
                    event_id,
                    row["task_id"],
                    int(assignment["version"]),
                    "Verified predecessor retired; continue the exact preserved task history.",
                    at,
                    _json(detail),
                    operation_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO delivery_outbox
                  (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                VALUES(?,?,?,'pending',?,0)
                """,
                (outbox_id, event_id, row["successor_agent_id"], at),
            )
            completion = {
                "schema": "league.runtime-replacement-completion.v1",
                "operation_id": operation_id,
                "event_id": event_id,
                "outbox_id": outbox_id,
                "recipient_agent_id": row["successor_agent_id"],
                "handoff_digest": _digest(detail),
            }
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE runtime_replacements
                   SET state='completed',completion_receipt_json=?,
                       handoff_event_id=?,handoff_outbox_id=?,version=?,updated_at=?
                 WHERE operation_id=? AND state='predecessor_retired' AND version=?
                """,
                (
                    _json(completion),
                    event_id,
                    outbox_id,
                    next_version,
                    at,
                    operation_id,
                    expected_version,
                ),
            )
            store.connection.execute(
                """
                UPDATE obligations
                   SET state='satisfied',next_attention_at=NULL,updated_at=?
                 WHERE dedupe_key=? AND state='open'
                """,
                (at, f"runtime-replacement:{operation_id}"),
            )
    except StorageRefusal:
        raise
    except sqlite3.IntegrityError as exc:
        raise StorageRefusal(
            "runtime_replacement_completion_conflict",
            "replacement handoff identity is already occupied",
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "runtime replacement completion conflicted with canonical state"
        ) from exc
    return {
        "operation_id": operation_id,
        "state": "completed",
        "version": next_version,
        "event_id": event_id,
        "outbox_id": outbox_id,
        "recipient_agent_id": row["successor_agent_id"],
        "idempotent": False,
    }


def record_predecessor_retired(
    store: Any,
    operation_id: str,
    expected_version: int,
    intent_digest: str,
    retirement_receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    """Persist physical A retirement before releasing any successor work."""

    _time(at, "runtime replacement retirement time")
    retirement = dict(retirement_receipt)
    try:
        with store._transaction():
            row = _row(store, "runtime_replacements", "operation_id", operation_id)
            intent = _object(row["intent_json"], "runtime_replacement_conflict")
            old_runtime = intent["snapshot"]["runtime"]
            if row["intent_digest"] != intent_digest:
                raise StorageRefusal(
                    "runtime_replacement_conflict",
                    "runtime replacement intent digest changed",
                )
            if row["state"] in {"predecessor_retired", "completed"}:
                stored = _object(
                    row["retirement_receipt_json"],
                    "runtime_replacement_retirement_conflict",
                )
                if stored != retirement:
                    raise StorageRefusal(
                        "runtime_replacement_retirement_conflict",
                        "retirement retry changed exact evidence",
                    )
                return {
                    "operation_id": operation_id,
                    "state": row["state"],
                    "version": int(row["version"]),
                    "retirement_receipt_digest": _digest(stored),
                    "idempotent": True,
                }
            exact = bool(
                row["state"] == "retiring"
                and int(row["version"]) == expected_version
                and retirement.get("verified") is True
                and retirement.get("operation_id") == operation_id
                and retirement.get("agent_id") == row["predecessor_agent_id"]
                and retirement.get("runtime_instance_id")
                == row["predecessor_runtime_instance_id"]
                and retirement.get("session_ref") == old_runtime["session_ref"]
                and retirement.get("endpoint") == old_runtime["endpoint"]
                and retirement.get("runtime_generation")
                == old_runtime["runtime_generation"]
                and retirement.get("state") == "retired"
            )
            if not exact:
                raise StorageRefusal(
                    "runtime_replacement_retirement_unverified",
                    "predecessor retirement receipt is not exact",
                )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE runtime_replacements
                   SET state='predecessor_retired',retirement_receipt_json=?,
                       version=?,updated_at=?
                 WHERE operation_id=? AND state='retiring' AND version=?
                """,
                (
                    _json(retirement),
                    next_version,
                    at,
                    operation_id,
                    expected_version,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "runtime replacement retirement receipt conflicted"
        ) from exc
    return {
        "operation_id": operation_id,
        "state": "predecessor_retired",
        "version": next_version,
        "retirement_receipt_digest": _digest(retirement),
        "idempotent": False,
    }


def rollback_runtime_replacement(
    store: Any,
    operation_id: str,
    expected_version: int,
    intent_digest: str,
    failure_code: str,
    receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    """Settle a verified pre-switch rollback; A was never mutated canonically."""

    _time(at, "runtime replacement rollback time")
    rollback = dict(receipt)
    if (
        not isinstance(failure_code, str)
        or not failure_code
        or rollback.get("verified") is not True
        or rollback.get("successor_cleanup_verified") is not True
        or rollback.get("predecessor_authoritative") is not True
    ):
        raise StorageRefusal(
            "runtime_replacement_rollback_unverified",
            "rollback must prove successor cleanup and predecessor authority",
        )
    try:
        with store._transaction():
            row = _row(store, "runtime_replacements", "operation_id", operation_id)
            if row["intent_digest"] != intent_digest:
                raise StorageRefusal(
                    "runtime_replacement_conflict",
                    "runtime replacement intent digest changed",
                )
            if row["state"] == "rolled_back":
                stored = _object(
                    row["rollback_receipt_json"],
                    "runtime_replacement_rollback_conflict",
                )
                if stored != rollback or row["failure_code"] != failure_code:
                    raise StorageRefusal(
                        "runtime_replacement_rollback_conflict",
                        "rollback retry changed exact evidence",
                    )
                return {
                    "operation_id": operation_id,
                    "state": "rolled_back",
                    "version": int(row["version"]),
                    "idempotent": True,
                }
            rollback_states = {
                "prepared",
                "launching",
                "successor_verified",
                "route_swapping",
                "activated",
                "retiring",
            }
            if row["state"] not in rollback_states or int(row["version"]) != expected_version:
                raise StorageRefusal(
                    "runtime_replacement_rollback_conflict",
                    "replacement is not at the exact rollback boundary",
                )
            intent = _object(row["intent_json"], "runtime_replacement_conflict")
            request = intent["request"]
            assignment = _row(
                store, "task_assignments", "task_assignment_id", row["assignment_id"]
            )
            activated = row["state"] in {"activated", "retiring"}
            if activated:
                if rollback.get("route_rollback_verified") is not True:
                    raise StorageRefusal(
                        "runtime_replacement_rollback_unverified",
                        "post-switch rollback requires exact route compensation",
                    )
                _compensate_activated_replacement(
                    store, row, intent, assignment, at
                )
            else:
                agent = _row(
                    store, "agent_instances", "agent_id", row["predecessor_agent_id"]
                )
                runtime = _row(
                    store,
                    "runtime_instances",
                    "runtime_instance_id",
                    row["predecessor_runtime_instance_id"],
                )
                if not (
                    assignment["state"] == "active"
                    and int(assignment["version"])
                    == request["expected_assignment_version"]
                    and assignment["champion_agent_id"] == row["predecessor_agent_id"]
                    and int(agent["version"]) == request["expected_agent_version"]
                    and agent["retired_at"] is None
                    and runtime["status"] in {"active", "idle"}
                    and bool(runtime["verified"])
                ):
                    raise StorageRefusal(
                        "runtime_replacement_rollback_unverified",
                        "predecessor authority changed before rollback settlement",
                    )
            _settle_pi_descriptors_for_rollback(
                store, row, intent, activated=activated, at=at
            )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE runtime_replacements
                   SET state='rolled_back',rollback_receipt_json=?,failure_code=?,
                       version=?,updated_at=?
                 WHERE operation_id=? AND version=?
                """,
                (
                    _json(rollback),
                    failure_code,
                    next_version,
                    at,
                    operation_id,
                    expected_version,
                ),
            )
            store.connection.execute(
                """
                UPDATE obligations
                   SET state='satisfied',next_attention_at=NULL,updated_at=?
                 WHERE dedupe_key=? AND state='open'
                """,
                (at, f"runtime-replacement:{operation_id}"),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "runtime replacement rollback conflicted with canonical state"
        ) from exc
    return {
        "operation_id": operation_id,
        "state": "rolled_back",
        "version": next_version,
        "failure_code": failure_code,
        "idempotent": False,
    }


def _compensate_activated_replacement(
    store: Any,
    row: Mapping[str, Any],
    intent: Mapping[str, Any],
    assignment: Mapping[str, Any],
    at: str,
) -> None:
    """Restore A after a verified physical compensation and before B is deleted."""

    snapshot = intent["snapshot"]
    old_agent = snapshot["agent"]
    old_runtime = snapshot["runtime"]
    old_assignment = snapshot["assignment"]
    old_task = snapshot["task"]
    old_callsign = snapshot["callsign_assignment"]
    successor_agent = _row(
        store, "agent_instances", "agent_id", row["successor_agent_id"]
    )
    successor_runtime = _row(
        store,
        "runtime_instances",
        "runtime_instance_id",
        row["successor_runtime_instance_id"],
    )
    if not (
        assignment["state"] == "active"
        and assignment["champion_agent_id"] == row["successor_agent_id"]
        and assignment["runtime_instance_id"] == row["successor_runtime_instance_id"]
        and successor_agent["retired_at"] is None
        and successor_runtime["actor_agent_id"] == row["successor_agent_id"]
        and successor_runtime["status"] in {"active", "idle"}
        and bool(successor_runtime["verified"])
    ):
        raise StorageRefusal(
            "runtime_replacement_rollback_unverified",
            "successor canonical state changed before compensation",
        )
    # Release B's live-callsign uniqueness fence before restoring A.  The
    # successor row remains addressable until all dependent ownership rows
    # have been compensated later in this same transaction.
    store.connection.execute(
        """
        UPDATE agent_instances
           SET status='failed',routing_name=NULL,display_agent=NULL,
               retired_at=?,version=version+1,updated_at=?,
               update_text='runtime replacement compensation',
               next_action='Delete compensated successor ownership'
         WHERE agent_id=? AND retired_at IS NULL
        """,
        (at, at, row["successor_agent_id"]),
    )
    store.connection.execute(
        """
        UPDATE delivery_outbox SET recipient_agent_id=?
         WHERE recipient_agent_id=? AND state='pending'
           AND NOT EXISTS (
             SELECT 1 FROM delivery_outbox other
              WHERE other.event_id=delivery_outbox.event_id
                AND other.recipient_agent_id=?
           )
        """,
        (
            row["predecessor_agent_id"],
            row["successor_agent_id"],
            row["predecessor_agent_id"],
        ),
    )
    store.connection.execute(
        "UPDATE squad_champions SET champion_agent_id=? WHERE champion_agent_id=?",
        (row["predecessor_agent_id"], row["successor_agent_id"]),
    )
    store.connection.execute(
        "UPDATE callsign_leases SET agent_id=? WHERE callsign=? AND agent_id=?",
        (
            row["predecessor_agent_id"],
            old_agent["callsign"],
            row["successor_agent_id"],
        ),
    )
    store.connection.execute(
        """
        UPDATE callsign_assignments
           SET subject_id=?,agent_id=?,runtime_instance_id=?,acceptance_digest=?,
               version=version+1
         WHERE callsign_assignment_id=? AND agent_id=?
        """,
        (
            old_callsign["subject_id"],
            row["predecessor_agent_id"],
            row["predecessor_runtime_instance_id"],
            old_callsign["acceptance_digest"],
            f"callsign-assignment:{row['assignment_id']}",
            row["successor_agent_id"],
        ),
    )
    store.connection.execute(
        """
        UPDATE tasks
           SET champion_agent_id=?,
               current_owner_agent_id=CASE WHEN current_owner_agent_id=? THEN ? ELSE current_owner_agent_id END,
               version=version+1,updated_at=?
         WHERE task_id=? AND champion_agent_id=?
        """,
        (
            row["predecessor_agent_id"],
            row["successor_agent_id"],
            row["predecessor_agent_id"],
            at,
            row["task_id"],
            row["successor_agent_id"],
        ),
    )
    store.connection.execute(
        """
        UPDATE task_assignments
           SET champion_agent_id=?,runtime_instance_id=?,acceptance_receipt_json=?,
               version=version+1,updated_at=?
         WHERE task_assignment_id=? AND champion_agent_id=?
        """,
        (
            row["predecessor_agent_id"],
            row["predecessor_runtime_instance_id"],
            old_assignment["acceptance_receipt_json"],
            at,
            row["assignment_id"],
            row["successor_agent_id"],
        ),
    )
    store.connection.execute(
        """
        UPDATE runtime_instances
           SET status=?,verified=?,last_seen_at=?
         WHERE runtime_instance_id=? AND actor_agent_id=?
        """,
        (
            old_runtime["status"],
            old_runtime["verified"],
            at,
            row["predecessor_runtime_instance_id"],
            row["predecessor_agent_id"],
        ),
    )
    store.connection.execute(
        """
        UPDATE agent_instances
           SET routing_name=?,display_agent=?,status=?,version=version+1,updated_at=?,
               update_text='runtime replacement rolled back',blocker=?,next_action=?,retired_at=NULL
         WHERE agent_id=?
        """,
        (
            old_agent["routing_name"],
            old_agent["display_agent"],
            old_agent["status"],
            at,
            old_agent["blocker"],
            old_agent["next_action"],
            row["predecessor_agent_id"],
        ),
    )
    store.connection.execute(
        "DELETE FROM runtime_instances WHERE runtime_instance_id=? AND actor_agent_id=?",
        (row["successor_runtime_instance_id"], row["successor_agent_id"]),
    )
    store.connection.execute(
        "DELETE FROM agent_instances WHERE agent_id=?",
        (row["successor_agent_id"],),
    )


def record_runtime_replacement_recovery(
    store: Any,
    operation_id: str,
    expected_version: int,
    intent_digest: str,
    failure_code: str,
    at: str,
) -> dict[str, Any]:
    """Fail closed when native route or retirement state is ambiguous."""

    _time(at, "runtime replacement recovery time")
    with store._transaction():
        row = _row(store, "runtime_replacements", "operation_id", operation_id)
        if row["intent_digest"] != intent_digest:
            raise StorageRefusal(
                "runtime_replacement_conflict",
                "runtime replacement intent digest changed",
            )
        if row["state"] == "recovery_required":
            return {
                "operation_id": operation_id,
                "state": "recovery_required",
                "version": int(row["version"]),
                "recovery_state": row["recovery_state"],
                "idempotent": True,
            }
        if int(row["version"]) != expected_version or row["state"] not in {
            "prepared",
            "launching",
            "successor_verified",
            "route_swapping",
            "activated",
            "retiring",
            "predecessor_retired",
        }:
            raise StorageRefusal(
                "runtime_replacement_recovery_conflict",
                "replacement recovery state changed",
            )
        next_version = expected_version + 1
        recovery_state = str(row["state"])
        store.connection.execute(
            """
            UPDATE runtime_replacements
               SET state='recovery_required',failure_code=?,recovery_state=?,
                   version=?,updated_at=?
             WHERE operation_id=? AND version=?
            """,
            (
                failure_code,
                recovery_state,
                next_version,
                at,
                operation_id,
                expected_version,
            ),
        )
        obligation_id = f"obligation:runtime-replacement:{_digest(operation_id)[:24]}"
        store.connection.execute(
            """
            INSERT INTO obligations
              (obligation_id,owner_agent_id,kind,aggregate_id,dedupe_key,state,
               next_attention_at,details_json,created_at,updated_at)
            VALUES(?,?, 'runtime_replacement',?,?, 'open',?,?,?,?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
              state='open',next_attention_at=excluded.next_attention_at,
              details_json=excluded.details_json,updated_at=excluded.updated_at
            """,
            (
                obligation_id,
                row["predecessor_agent_id"],
                operation_id,
                f"runtime-replacement:{operation_id}",
                at,
                _json(
                    {
                        "schema": "league.runtime-replacement-recovery.v1",
                        "operation_id": operation_id,
                        "failure_code": failure_code,
                        "next_action": "prove exact predecessor and successor native state before retry or rollback",
                    }
                ),
                at,
                at,
            ),
        )
    return {
        "operation_id": operation_id,
        "state": "recovery_required",
        "version": next_version,
        "recovery_state": recovery_state,
        "obligation_id": obligation_id,
        "idempotent": False,
    }


def resume_runtime_replacement_recovery(
    store: Any,
    operation_id: str,
    expected_version: int,
    intent_digest: str,
    at: str,
) -> dict[str, Any]:
    """Resume the exact pre-failure state; the same request remains the authority."""

    _time(at, "runtime replacement recovery resume time")
    with store._transaction():
        row = _row(store, "runtime_replacements", "operation_id", operation_id)
        if row["intent_digest"] != intent_digest:
            raise StorageRefusal(
                "runtime_replacement_conflict",
                "runtime replacement intent digest changed",
            )
        if row["state"] != "recovery_required" or int(row["version"]) != expected_version:
            raise StorageRefusal(
                "runtime_replacement_recovery_conflict",
                "replacement is not at the exact recovery boundary",
            )
        recovery_state = row["recovery_state"]
        if recovery_state not in {
            "prepared",
            "launching",
            "successor_verified",
            "route_swapping",
            "activated",
            "retiring",
            "predecessor_retired",
        }:
            raise StorageRefusal(
                "runtime_replacement_recovery_conflict",
                "replacement recovery state is invalid",
            )
        next_version = expected_version + 1
        store.connection.execute(
            """
            UPDATE runtime_replacements
               SET state=?,recovery_state=NULL,failure_code=NULL,version=?,updated_at=?
             WHERE operation_id=? AND state='recovery_required' AND version=?
            """,
            (recovery_state, next_version, at, operation_id, expected_version),
        )
    return {
        "operation_id": operation_id,
        "state": recovery_state,
        "version": next_version,
        "idempotent": False,
    }


def runtime_replacement_status(store: Any, operation_id: str) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT * FROM runtime_replacements WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "operation_id": row["operation_id"],
        "assignment_id": row["assignment_id"],
        "task_id": row["task_id"],
        "predecessor_agent_id": row["predecessor_agent_id"],
        "predecessor_runtime_instance_id": row["predecessor_runtime_instance_id"],
        "successor_agent_id": row["successor_agent_id"],
        "successor_runtime_instance_id": row["successor_runtime_instance_id"],
        "successor_adapter_kind": row["successor_adapter_kind"],
        "successor_provider_kind": row["successor_provider_kind"],
        "multiplexer_kind": row["multiplexer_kind"],
        "state": row["state"],
        "version": int(row["version"]),
        "intent_digest": row["intent_digest"],
        "successor_receipt_digest": (
            _digest(_object(row["successor_receipt_json"], "runtime_replacement_status_invalid"))
            if row["successor_receipt_json"]
            else None
        ),
        "handoff_event_id": row["handoff_event_id"],
        "handoff_outbox_id": row["handoff_outbox_id"],
        "failure_code": row["failure_code"],
        "recovery_state": row["recovery_state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def runtime_replacement_launch_context(
    store: Any, assignment_id: str
) -> dict[str, Any]:
    """Return immutable routing inputs for one active Champion assignment."""

    assignment = _row(
        store, "task_assignments", "task_assignment_id", assignment_id
    )
    task = _row(store, "tasks", "task_id", str(assignment["task_id"]))
    agent = _row(
        store, "agent_instances", "agent_id", str(assignment["champion_agent_id"])
    )
    if not (
        assignment["assignment_role"] == "champion"
        and assignment["state"] == "active"
        and assignment["runtime_instance_id"]
        and agent["retired_at"] is None
        and agent["role"] == "champion"
        and agent["task_id"] == task["task_id"]
    ):
        raise StorageRefusal(
            "runtime_replacement_identity_mismatch",
            "replacement routing context is not one active Champion",
        )
    return {
        "assignment_id": assignment_id,
        "request_id": assignment["request_id"],
        "task_id": task["task_id"],
        "task_summary": task["summary"],
        "coordinator_agent_id": assignment["coordinator_agent_id"],
        "required_capabilities": list(_requirements(store, assignment_id)),
        "repository": agent["repository"],
        "issue": int(agent["issue"]),
        "branch": agent["branch"],
        "worktree": agent["worktree"],
        "callsign": agent["callsign"],
        "routing_name": agent["routing_name"],
    }


__all__ = [
    "activate_runtime_replacement",
    "assert_runtime_replacement_mutation_allowed",
    "begin_runtime_replacement_effect",
    "complete_runtime_replacement",
    "prepare_runtime_replacement",
    "record_runtime_replacement_recovery",
    "record_predecessor_retired",
    "record_successor_verified",
    "resume_runtime_replacement_recovery",
    "rollback_runtime_replacement",
    "runtime_replacement_status",
    "runtime_replacement_launch_context",
]
