"""Persistent shuffled callsign queue operations.

All selection and lifecycle transitions run inside the caller's short SQLite
transaction. Adapter work happens before activation and before release.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from .storage_types import FaultInjector, StorageRefusal


ROLES = {"shotcaller", "champion", "hidden-worker"}
SCOPES = {"squad", "task", "worker"}
CAPABILITY = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
CALLSIGN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
RUNTIME_RECEIPT_KEYS = {
    "schema",
    "verified",
    "assignment_id",
    "agent_id",
    "callsign",
    "runtime_instance_id",
    "harness_kind",
    "backend_kind",
    "session_identity",
    "endpoint_identity",
    "endpoint_generation",
    "routing_name",
    "display_agent",
    "capabilities",
}


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("invalid_timestamp", f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("invalid_timestamp", f"{label} must include an offset")
    return value


def capabilities(values: Sequence[str]) -> tuple[str, ...]:
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or len(values) > 64
        or any(not isinstance(value, str) for value in values)
    ):
        raise StorageRefusal("invalid_capabilities", "capability requirements are invalid")
    result = tuple(sorted(set(values)))
    if len(result) != len(values) or any(not CAPABILITY.fullmatch(value) for value in result):
        raise StorageRefusal(
            "invalid_capabilities", "capabilities must be unique normalized tokens"
        )
    return result


def shuffle_key(seed: str, role: str, callsign: str) -> str:
    return hashlib.sha256(f"{seed}\0{role}\0{callsign}".encode("utf-8")).hexdigest()


def initialize_imported_callsign_state(store: Any, at: str) -> None:
    """Complete v6-derived state after a legacy import, inside its transaction."""
    timestamp(at, "import reconciliation time")
    if store.connection.execute("SELECT COUNT(*) FROM callsign_queue").fetchone()[0]:
        raise StorageRefusal(
            "import_collision", "callsign queue already contains canonical entries"
        )
    for role in sorted(ROLES):
        meta = _meta(store, role)
        rows = store.connection.execute(
            """
            SELECT c.callsign,c.pool_role,c.last_released_at,l.agent_id,l.launch_attempt_id,
                   l.reserved_at,a.kind,a.task_id
              FROM callsigns c
              LEFT JOIN callsign_leases l ON l.callsign=c.callsign
              LEFT JOIN agent_instances a ON a.agent_id=l.agent_id
             WHERE c.pool_role=?
            """,
            (role,),
        ).fetchall()
        if rows:
            store.connection.execute(
                "UPDATE callsign_queue_meta SET initialized_at=? WHERE pool_role=?",
                (at, role),
            )
        queued = [
            row
            for row in rows
            if row["agent_id"] is None
            or row["kind"] == "unbound"
        ]
        queued.sort(
            key=lambda row: (shuffle_key(meta["seed"], role, row["callsign"]), row["callsign"])
        )
        if len(queued) > 1 and [row["callsign"] for row in queued] == sorted(
            row["callsign"] for row in queued
        ):
            queued = queued[1:] + queued[:1]
        positions = {str(row["callsign"]): position for position, row in enumerate(queued)}
        for row in rows:
            has_lease = row["agent_id"] is not None or row["launch_attempt_id"] is not None
            state = (
                "available"
                if not has_lease
                else "reserved"
                if row["agent_id"] is None or row["kind"] == "unbound"
                else "active"
            )
            assignment_id = None
            if has_lease:
                assignment_id = "imported:" + hashlib.sha256(
                    f"{role}\0{row['callsign']}".encode("utf-8")
                ).hexdigest()[:24]
            store.connection.execute(
                """
                INSERT INTO callsign_queue
                  (callsign,pool_role,queue_position,state,reservation_assignment_id,
                   version,updated_at)
                VALUES(?,?,?,?,?,1,?)
                """,
                (
                    row["callsign"],
                    role,
                    None if state == "active" else positions[str(row["callsign"])],
                    state,
                    assignment_id if state == "reserved" else None,
                    row["reserved_at"] or row["last_released_at"] or at,
                ),
            )
            if not has_lease:
                continue
            agent_id = row["agent_id"]
            if role == "shotcaller" and agent_id:
                squad = store.connection.execute(
                    "SELECT squad_id FROM squads WHERE shotcaller_agent_id=?", (agent_id,)
                ).fetchone()
                scope_kind = "squad"
                scope_id = squad["squad_id"] if squad else f"legacy:{row['callsign']}"
            elif role == "champion":
                scope_kind = "task"
                scope_id = row["task_id"] or row["launch_attempt_id"] or f"legacy:{row['callsign']}"
            else:
                scope_kind = "worker"
                scope_id = row["task_id"] or row["launch_attempt_id"] or f"legacy:{row['callsign']}"
            subject_id = (
                f"agent:{agent_id}"
                if agent_id
                else f"attempt:{row['launch_attempt_id']}"
            )
            store.connection.execute(
                """
                INSERT INTO callsign_assignments
                  (callsign_assignment_id,callsign,subject_id,agent_id,runtime_instance_id,
                   role,scope_kind,
                   scope_id,state,reservation_position,queue_version,requirements_json,
                   acceptance_digest,release_receipt_digest,failure_receipt_digest,version,
                   reserved_at,activated_at,released_at)
                VALUES(?,?,?,?,NULL,?,?,?,?,?,1,'[]',NULL,NULL,NULL,1,?,?,NULL)
                """,
                (
                    assignment_id,
                    row["callsign"],
                    subject_id,
                    agent_id,
                    role,
                    scope_kind,
                    scope_id,
                    state,
                    positions.get(str(row["callsign"])),
                    row["reserved_at"] or at,
                    (row["reserved_at"] or at) if state == "active" else None,
                ),
            )
    store.connection.execute(
        """
        INSERT OR IGNORE INTO shotcaller_intake(agent_id,squad_id,state,fence,version,updated_at)
        SELECT shotcaller_agent_id,squad_id,'accepting',owner_fence,1,updated_at FROM squads
        """
    )
    store.connection.execute(
        """
        INSERT OR IGNORE INTO squad_champions(squad_id,champion_agent_id,joined_at)
        SELECT s.squad_id,a.agent_id,a.updated_at
          FROM squads s JOIN agent_instances a ON a.shotcaller_agent_id=s.shotcaller_agent_id
         WHERE a.role='champion' AND a.retired_at IS NULL
        """
    )


def _meta(store: Any, role: str) -> Any:
    row = store.connection.execute(
        "SELECT * FROM callsign_queue_meta WHERE pool_role=?", (role,)
    ).fetchone()
    if row is None:
        raise StorageRefusal("queue_uninitialized", "callsign queue has not been initialized")
    return row


def _assignment_value(row: Any, *, idempotent: bool) -> dict[str, Any]:
    return {
        "assignment_id": row["callsign_assignment_id"],
        "callsign": row["callsign"],
        "agent_id": row["agent_id"],
        "role": row["role"],
        "scope": {"kind": row["scope_kind"], "id": row["scope_id"]},
        "runtime_instance_id": row["runtime_instance_id"],
        "state": row["state"],
        "version": int(row["version"]),
        "queue_version": int(row["queue_version"]),
        "idempotent": idempotent,
    }


def callsign_status(store: Any, role: str) -> dict[str, Any]:
    if role not in ROLES:
        raise StorageRefusal("invalid_role", "callsign role is unsupported")
    meta = _meta(store, role)
    rows = store.connection.execute(
        """
        SELECT q.callsign,q.queue_position,q.state,q.version,c.enabled
          FROM callsign_queue q JOIN callsigns c ON c.callsign=q.callsign
         WHERE q.pool_role=?
         ORDER BY CASE WHEN q.queue_position IS NULL THEN 1 ELSE 0 END,
                  q.queue_position,q.callsign
        """,
        (role,),
    ).fetchall()
    counts = {"available": 0, "reserved": 0, "active": 0, "disabled": 0}
    entries: list[dict[str, Any]] = []
    for row in rows:
        counts[str(row["state"])] += 1
        if not row["enabled"]:
            counts["disabled"] += 1
        entries.append(
            {
                "callsign": row["callsign"],
                "position": row["queue_position"],
                "state": row["state"],
                "enabled": bool(row["enabled"]),
                "version": int(row["version"]),
            }
        )
    return {
        "schema": "league.callsign-queue.v1",
        "role": role,
        "seed": meta["seed"],
        "shuffle_version": int(meta["shuffle_version"]),
        "queue_version": int(meta["queue_version"]),
        "counts": counts,
        "entries": entries,
    }


def reconcile_callsign_pool(
    store: Any,
    role: str,
    expected_queue_version: int,
    seed: str,
    shuffle_version: int,
    entries: Sequence[Mapping[str, Any]],
    at: str,
) -> dict[str, Any]:
    timestamp(at, "callsign reconciliation time")
    if role not in ROLES or not seed or shuffle_version < 1 or expected_queue_version < 1:
        raise StorageRefusal("invalid_pool", "callsign pool identity or version is invalid")
    if (
        not isinstance(entries, Sequence)
        or isinstance(entries, (str, bytes))
        or len(entries) > 10_000
    ):
        raise StorageRefusal("invalid_pool", "callsign catalog exceeds its bound")
    normalized: dict[str, tuple[bool, tuple[str, ...]]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"callsign", "enabled", "capabilities"}:
            raise StorageRefusal("invalid_pool", "callsign catalog entry is invalid")
        name = entry["callsign"]
        enabled = entry["enabled"]
        if not isinstance(name, str) or not CALLSIGN.fullmatch(name) or not isinstance(enabled, bool):
            raise StorageRefusal("invalid_pool", "callsign catalog identity is invalid")
        if name in normalized:
            raise StorageRefusal("invalid_pool", "callsign catalog contains duplicates")
        normalized[name] = (enabled, capabilities(entry["capabilities"]))
    try:
        with store._transaction():
            meta = _meta(store, role)
            if meta["seed"] != seed or int(meta["shuffle_version"]) != shuffle_version:
                raise StorageRefusal(
                    "queue_identity_conflict", "persisted callsign shuffle seed/version is immutable"
                )
            if int(meta["queue_version"]) != expected_queue_version:
                raise StorageRefusal("version_conflict", "callsign queue version precondition failed")
            existing_rows = store.connection.execute(
                """
                SELECT q.callsign,c.enabled
                  FROM callsign_queue q JOIN callsigns c ON c.callsign=q.callsign
                 WHERE q.pool_role=? ORDER BY q.callsign
                """,
                (role,),
            ).fetchall()
            existing = {str(row["callsign"]): bool(row["enabled"]) for row in existing_rows}
            initial_catalog = not existing
            if set(existing) - set(normalized):
                raise StorageRefusal(
                    "callsign_history_immutable",
                    "persisted callsigns must remain in the catalog and may only be disabled",
                )
            observed_lists: dict[str, list[str]] = {name: [] for name in existing}
            for row in store.connection.execute(
                """
                SELECT cc.callsign,cc.capability
                  FROM callsign_capabilities cc
                  JOIN callsign_queue q ON q.callsign=cc.callsign
                 WHERE q.pool_role=? ORDER BY cc.callsign,cc.capability
                """,
                (role,),
            ):
                observed_lists[str(row["callsign"])].append(str(row["capability"]))
            observed = {name: tuple(values) for name, values in observed_lists.items()}
            exact = set(existing) == set(normalized) and all(
                existing[name] == normalized[name][0] and observed[name] == normalized[name][1]
                for name in existing
            )
            if exact:
                result = callsign_status(store, role)
                result["idempotent"] = True
                return result
            additions = [name for name in normalized if name not in existing]
            additions.sort(key=lambda name: (shuffle_key(seed, role, name), name))
            if len(additions) > 1 and additions == sorted(additions):
                additions = additions[1:] + additions[:1]
            tail = int(
                store.connection.execute(
                    "SELECT COALESCE(MAX(queue_position),-1) FROM callsign_queue WHERE pool_role=?",
                    (role,),
                ).fetchone()[0]
            )
            for name in additions:
                other = store.connection.execute(
                    "SELECT pool_role FROM callsigns WHERE callsign=?", (name,)
                ).fetchone()
                if other is not None and other["pool_role"] != role:
                    raise StorageRefusal("callsign_role_conflict", "callsign belongs to another role")
                if other is not None:
                    raise StorageRefusal(
                        "queue_incomplete", "persisted callsign is missing its queue entry"
                    )
                store.connection.execute(
                    """
                    INSERT INTO callsigns
                      (callsign,pool_role,enabled,pool_position,last_released_at,capability_version)
                    VALUES(?,?,?,NULL,NULL,1)
                    """,
                    (name, role, int(normalized[name][0])),
                )
                tail += 1
                store.connection.execute(
                    """
                    INSERT INTO callsign_queue
                      (callsign,pool_role,queue_position,state,reservation_assignment_id,version,updated_at)
                    VALUES(?,?,?,'available',NULL,1,?)
                    """,
                    (name, role, tail, at),
                )
            for name in existing:
                store.connection.execute(
                    "UPDATE callsigns SET enabled=? WHERE callsign=?",
                    (int(normalized[name][0]), name),
                )
            for name, (_, required) in normalized.items():
                store.connection.execute(
                    "DELETE FROM callsign_capabilities WHERE callsign=?", (name,)
                )
                store.connection.executemany(
                    "INSERT INTO callsign_capabilities(callsign,capability) VALUES(?,?)",
                    ((name, item) for item in required),
                )
                store.connection.execute(
                    "UPDATE callsigns SET capability_version=capability_version+1 WHERE callsign=?",
                    (name,),
                )
            next_version = expected_queue_version + 1
            changed = store.connection.execute(
                """
                UPDATE callsign_queue_meta SET queue_version=?,
                       initialized_at=CASE WHEN ? THEN ? ELSE initialized_at END
                 WHERE pool_role=? AND queue_version=?
                """,
                (next_version, int(initial_catalog), at, role, expected_queue_version),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("version_conflict", "callsign queue version precondition failed")
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "callsign catalog reconciliation conflicted with canonical state"
        ) from exc
    result = callsign_status(store, role)
    result["idempotent"] = False
    return result


def _availability(store: Any, role: str, required: tuple[str, ...]) -> tuple[Any, dict[str, Any]]:
    rows = store.connection.execute(
        """
        SELECT q.*,c.enabled
          FROM callsign_queue q JOIN callsigns c ON c.callsign=q.callsign
         WHERE q.pool_role=? ORDER BY q.queue_position,q.callsign
        """,
        (role,),
    ).fetchall()
    active = sum(row["state"] == "active" for row in rows)
    reserved = sum(row["state"] == "reserved" for row in rows)
    offered_by_callsign: dict[str, set[str]] = {}
    for capability_row in store.connection.execute(
        """
        SELECT cc.callsign,cc.capability
          FROM callsign_capabilities cc
          JOIN callsign_queue q ON q.callsign=cc.callsign
         WHERE q.pool_role=? ORDER BY cc.callsign,cc.capability
        """,
        (role,),
    ):
        offered_by_callsign.setdefault(str(capability_row["callsign"]), set()).add(
            str(capability_row["capability"])
        )
    reasons: dict[str, int] = {}
    incompatible = 0
    selected = None
    for row in rows:
        if row["state"] != "available":
            continue
        if not row["enabled"]:
            incompatible += 1
            reasons["disabled"] = reasons.get("disabled", 0) + 1
            continue
        offered = offered_by_callsign.get(str(row["callsign"]), set())
        missing = [item for item in required if item not in offered]
        if missing:
            incompatible += 1
            for item in missing:
                key = f"missing:{item}"
                reasons[key] = reasons.get(key, 0) + 1
            continue
        selected = row
        break
    return selected, {
        "active": active,
        "reserved": reserved,
        "incompatible": incompatible,
        "reasons": dict(sorted(reasons.items())),
    }


def _reserve_in_transaction(
    store: Any,
    assignment_id: str,
    agent_id: str,
    role: str,
    scope_kind: str,
    scope_id: str,
    required: tuple[str, ...],
    at: str,
    fault: Optional[FaultInjector] = None,
) -> dict[str, Any]:
    existing = store.connection.execute(
        "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if existing is not None:
        exact = (
            existing["subject_id"] == f"agent:{agent_id}"
            and existing["agent_id"] == agent_id
            and existing["role"] == role
            and existing["scope_kind"] == scope_kind
            and existing["scope_id"] == scope_id
            and tuple(json.loads(existing["requirements_json"])) == required
        )
        if not exact:
            raise StorageRefusal("assignment_conflict", "callsign allocation retry changed identity")
        return _assignment_value(existing, idempotent=True)
    if store.connection.execute(
        "SELECT 1 FROM agent_instances WHERE agent_id=?", (agent_id,)
    ).fetchone() is not None:
        raise StorageRefusal("agent_conflict", "callsign subject identity already exists")
    meta = _meta(store, role)
    selected, refusal = _availability(store, role, required)
    if selected is None:
        raise StorageRefusal(
            "callsign_unavailable",
            "no compatible callsign is available: " + stable_json(refusal),
        )
    queue_version = int(meta["queue_version"]) + 1
    store.connection.execute(
        """
        UPDATE callsign_queue
           SET state='reserved',reservation_assignment_id=?,version=version+1,updated_at=?
         WHERE callsign=? AND state='available'
        """,
        (assignment_id, at, selected["callsign"]),
    )
    store.connection.execute(
        "UPDATE callsign_queue_meta SET queue_version=? WHERE pool_role=?",
        (queue_version, role),
    )
    if fault:
        fault("after_callsign_queue_reservation")
    store.connection.execute(
        """
        INSERT INTO agent_instances
          (agent_id,callsign,role,shotcaller_agent_id,task_id,kind,address,thread_id,
           backend,routing_name,display_agent,repository,issue,branch,worktree,status,
           version,updated_at,update_text,blocker,next_action,metadata_json,retired_at)
        VALUES(?,?,?,NULL,NULL,'unbound',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
               'active',1,?,'callsign reserved',NULL,'Complete exact runtime acceptance',?,NULL)
        """,
        (
            agent_id,
            selected["callsign"],
            role,
            at,
            stable_json({"scope_kind": scope_kind, "scope_id": scope_id}),
        ),
    )
    store.connection.execute(
        "INSERT INTO callsign_leases(callsign,agent_id,launch_attempt_id,reserved_at) VALUES(?,?,NULL,?)",
        (selected["callsign"], agent_id, at),
    )
    store.connection.execute(
        """
        INSERT INTO callsign_assignments
          (callsign_assignment_id,callsign,subject_id,agent_id,runtime_instance_id,
           role,scope_kind,scope_id,state,
           reservation_position,queue_version,requirements_json,acceptance_digest,
           release_receipt_digest,failure_receipt_digest,version,reserved_at,activated_at,released_at)
        VALUES(?,?,?, ?,NULL,?,?,?,'reserved',?,?,?,NULL,NULL,NULL,1,?,NULL,NULL)
        """,
        (
            assignment_id,
            selected["callsign"],
            f"agent:{agent_id}",
            agent_id,
            role,
            scope_kind,
            scope_id,
            selected["queue_position"],
            queue_version,
            stable_json(required),
            at,
        ),
    )
    store.connection.execute(
        """
        INSERT INTO events
          (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
           occurred_at,detail_json,aggregate_kind,aggregate_id)
        VALUES(?, ?,NULL,NULL,1,'callsign_reserved','reserved','callsign reserved',?,?,
               'agent',?)
        """,
        (
            f"callsign:{assignment_id}:reserved",
            agent_id,
            at,
            stable_json(
                {
                    "assignment_id": assignment_id,
                    "queue_version": queue_version,
                    "requirements": list(required),
                }
            ),
            agent_id,
        ),
    )
    row = store.connection.execute(
        "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?", (assignment_id,)
    ).fetchone()
    return _assignment_value(row, idempotent=False)


def allocate_callsign(
    store: Any,
    assignment_id: str,
    agent_id: str,
    role: str,
    scope_kind: str,
    scope_id: str,
    required_capabilities: Sequence[str],
    at: str,
    *,
    fault: Optional[FaultInjector] = None,
) -> dict[str, Any]:
    timestamp(at, "callsign allocation time")
    if (
        role not in ROLES
        or scope_kind not in SCOPES
        or not all((assignment_id, agent_id, scope_id))
    ):
        raise StorageRefusal("invalid_assignment", "callsign assignment identity is invalid")
    required = capabilities(required_capabilities)
    try:
        with store._transaction():
            return _reserve_in_transaction(
                store,
                assignment_id,
                agent_id,
                role,
                scope_kind,
                scope_id,
                required,
                at,
                fault,
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "callsign allocation conflicted with canonical state"
        ) from exc


def _runtime_receipt(receipt: Mapping[str, Any], assignment: Any) -> tuple[dict[str, Any], str]:
    if set(receipt) != RUNTIME_RECEIPT_KEYS or receipt.get("schema") != "league.runtime-acceptance.v1":
        raise StorageRefusal("receipt_unverified", "runtime acceptance receipt shape is invalid")
    if receipt.get("verified") is not True:
        raise StorageRefusal("receipt_unverified", "runtime acceptance was not verified")
    required_text = RUNTIME_RECEIPT_KEYS - {"verified", "capabilities"}
    if any(not isinstance(receipt.get(key), str) or not receipt[key] for key in required_text):
        raise StorageRefusal("receipt_unverified", "runtime acceptance identity is incomplete")
    offered = capabilities(receipt["capabilities"])
    expected = tuple(json.loads(assignment["requirements_json"]))
    if any(item not in offered for item in expected):
        raise StorageRefusal(
            "capability_mismatch", "accepted runtime lacks a required declared capability"
        )
    exact = (
        receipt["assignment_id"] == assignment["callsign_assignment_id"]
        and receipt["agent_id"] == assignment["agent_id"]
        and receipt["callsign"] == assignment["callsign"]
        and receipt["routing_name"] == str(assignment["callsign"]).lower()
    )
    if not exact:
        raise StorageRefusal("receipt_mismatch", "runtime receipt changed reserved identity")
    value = dict(receipt)
    value["capabilities"] = list(offered)
    return value, digest(value)


def activate_callsign(
    store: Any,
    assignment_id: str,
    expected_version: int,
    receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    timestamp(at, "callsign activation time")
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise StorageRefusal("assignment_unknown", "callsign assignment does not exist")
            normalized, receipt_digest = _runtime_receipt(receipt, assignment)
            if assignment["state"] == "active":
                if assignment["acceptance_digest"] != receipt_digest:
                    raise StorageRefusal("receipt_conflict", "active callsign has another receipt")
                return _assignment_value(assignment, idempotent=True)
            if assignment["state"] != "reserved" or int(assignment["version"]) != expected_version:
                raise StorageRefusal("assignment_conflict", "callsign is not reserved at expected version")
            queue = store.connection.execute(
                "SELECT * FROM callsign_queue WHERE callsign=?", (assignment["callsign"],)
            ).fetchone()
            if (
                queue is None
                or queue["state"] != "reserved"
                or queue["reservation_assignment_id"] != assignment_id
            ):
                raise StorageRefusal("queue_conflict", "callsign queue reservation is not exact")
            runtime_conflict = store.connection.execute(
                """
                SELECT 1 FROM runtime_instances
                 WHERE runtime_instance_id=? OR (harness_kind=? AND session_ref=?)
                """,
                (
                    normalized["runtime_instance_id"],
                    normalized["harness_kind"],
                    normalized["session_identity"],
                ),
            ).fetchone()
            if runtime_conflict is not None:
                raise StorageRefusal("runtime_conflict", "runtime or thread identity is already bound")
            store.connection.execute(
                """
                INSERT INTO runtime_instances
                  (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,endpoint,
                   runtime_generation,status,verified,last_seen_at,capabilities_json)
                VALUES(?,?,?,?,?,?,?,'active',1,?,?)
                """,
                (
                    normalized["runtime_instance_id"],
                    assignment["agent_id"],
                    normalized["harness_kind"],
                    normalized["backend_kind"],
                    normalized["session_identity"],
                    normalized["endpoint_identity"],
                    normalized["endpoint_generation"],
                    at,
                    stable_json(normalized["capabilities"]),
                ),
            )
            agent = store.connection.execute(
                "SELECT version FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                (assignment["agent_id"],),
            ).fetchone()
            if agent is None:
                raise StorageRefusal("agent_conflict", "reserved agent identity is not live")
            agent_version = int(agent["version"]) + 1
            store.connection.execute(
                """
                UPDATE agent_instances SET kind=?,address=?,thread_id=?,backend=?,routing_name=?,
                       display_agent=?,status='working',version=?,updated_at=?,
                       update_text='runtime accepted',next_action='Accept assigned intake'
                 WHERE agent_id=?
                """,
                (
                    normalized["harness_kind"],
                    normalized["endpoint_identity"],
                    normalized["session_identity"],
                    (
                        normalized["backend_kind"]
                        if normalized["backend_kind"] in {"herdr", "tmux"}
                        else None
                    ),
                    normalized["routing_name"],
                    normalized["display_agent"],
                    agent_version,
                    at,
                    assignment["agent_id"],
                ),
            )
            meta = _meta(store, assignment["role"])
            queue_version = int(meta["queue_version"]) + 1
            store.connection.execute(
                """
                UPDATE callsign_queue SET state='active',queue_position=NULL,
                       reservation_assignment_id=NULL,version=version+1,updated_at=?
                 WHERE callsign=?
                """,
                (at, assignment["callsign"]),
            )
            store.connection.execute(
                "UPDATE callsign_queue_meta SET queue_version=? WHERE pool_role=?",
                (queue_version, assignment["role"]),
            )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE callsign_assignments SET state='active',runtime_instance_id=?,
                       acceptance_digest=?,version=?,queue_version=?,activated_at=?
                 WHERE callsign_assignment_id=?
                """,
                (
                    normalized["runtime_instance_id"],
                    receipt_digest,
                    next_version,
                    queue_version,
                    at,
                    assignment_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?, ?,NULL,NULL,?,'callsign_activated','active','callsign activated',?,?,
                       'agent',?)
                """,
                (
                    f"callsign:{assignment_id}:active",
                    assignment["agent_id"],
                    agent_version,
                    at,
                    stable_json(
                        {
                            "assignment_id": assignment_id,
                            "acceptance_digest": receipt_digest,
                            "queue_version": queue_version,
                        }
                    ),
                    assignment["agent_id"],
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "callsign activation conflicted with canonical state"
        ) from exc
    row = store.connection.execute(
        "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?", (assignment_id,)
    ).fetchone()
    return _assignment_value(row, idempotent=False)


def _rollback_reserved_in_transaction(
    store: Any, assignment: Any, expected_version: int, receipt_digest: str, at: str
) -> dict[str, Any]:
    if assignment["state"] == "rolled_back":
        if assignment["failure_receipt_digest"] != receipt_digest:
            raise StorageRefusal("receipt_conflict", "rollback receipt differs from history")
        return _assignment_value(assignment, idempotent=True)
    if assignment["state"] != "reserved" or int(assignment["version"]) != expected_version:
        raise StorageRefusal("assignment_conflict", "only an exact reservation can roll back")
    queue = store.connection.execute(
        "SELECT * FROM callsign_queue WHERE callsign=?", (assignment["callsign"],)
    ).fetchone()
    if queue["state"] != "reserved" or queue["reservation_assignment_id"] != assignment["callsign_assignment_id"]:
        raise StorageRefusal("queue_conflict", "reservation rollback does not own the queue entry")
    meta = _meta(store, assignment["role"])
    queue_version = int(meta["queue_version"]) + 1
    store.connection.execute(
        """
        UPDATE callsign_queue SET state='available',reservation_assignment_id=NULL,
               version=version+1,updated_at=? WHERE callsign=?
        """,
        (at, assignment["callsign"]),
    )
    store.connection.execute(
        "UPDATE callsign_queue_meta SET queue_version=? WHERE pool_role=?",
        (queue_version, assignment["role"]),
    )
    store.connection.execute(
        "DELETE FROM callsign_leases WHERE callsign=? AND agent_id=?",
        (assignment["callsign"], assignment["agent_id"]),
    )
    agent = store.connection.execute(
        "SELECT version FROM agent_instances WHERE agent_id=?", (assignment["agent_id"],)
    ).fetchone()
    agent_version = int(agent["version"]) + 1
    store.connection.execute(
        """
        UPDATE agent_instances SET version=?,updated_at=?,update_text='reservation rolled back',
               retired_at=? WHERE agent_id=?
        """,
        (agent_version, at, at, assignment["agent_id"]),
    )
    next_version = expected_version + 1
    store.connection.execute(
        """
        UPDATE callsign_assignments SET state='rolled_back',failure_receipt_digest=?,
               queue_version=?,version=?,released_at=? WHERE callsign_assignment_id=?
        """,
        (receipt_digest, queue_version, next_version, at, assignment["callsign_assignment_id"]),
    )
    store.connection.execute(
        """
        INSERT INTO events
          (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
           occurred_at,detail_json,aggregate_kind,aggregate_id)
        VALUES(?, ?,NULL,NULL,?,'callsign_reservation_rolled_back','rolled_back',
               'callsign reservation rolled back',?,?,'agent',?)
        """,
        (
            f"callsign:{assignment['callsign_assignment_id']}:rolled-back",
            assignment["agent_id"],
            agent_version,
            at,
            stable_json(
                {
                    "assignment_id": assignment["callsign_assignment_id"],
                    "failure_receipt_digest": receipt_digest,
                    "queue_version": queue_version,
                }
            ),
            assignment["agent_id"],
        ),
    )
    row = store.connection.execute(
        "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
        (assignment["callsign_assignment_id"],),
    ).fetchone()
    return _assignment_value(row, idempotent=False)


def rollback_callsign(
    store: Any,
    assignment_id: str,
    expected_version: int,
    failure_receipt_digest: str,
    at: str,
) -> dict[str, Any]:
    timestamp(at, "callsign rollback time")
    if not failure_receipt_digest:
        raise StorageRefusal("receipt_required", "rollback requires a bounded receipt digest")
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise StorageRefusal("assignment_unknown", "callsign assignment does not exist")
            return _rollback_reserved_in_transaction(
                store, assignment, expected_version, failure_receipt_digest, at
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "callsign rollback conflicted with canonical state"
        ) from exc


def _release_active_in_transaction(
    store: Any, assignment: Any, expected_version: int, receipt_digest: str, at: str
) -> dict[str, Any]:
    if assignment["state"] == "released":
        if assignment["release_receipt_digest"] != receipt_digest:
            raise StorageRefusal("receipt_conflict", "release receipt differs from history")
        return _assignment_value(assignment, idempotent=True)
    if assignment["state"] != "active" or int(assignment["version"]) != expected_version:
        raise StorageRefusal("assignment_conflict", "only an exact active callsign can release")
    active_runtime = store.connection.execute(
        """
        SELECT 1 FROM runtime_instances
         WHERE actor_agent_id=? AND status IN ('active','idle') LIMIT 1
        """,
        (assignment["agent_id"],),
    ).fetchone()
    if active_runtime is not None:
        raise StorageRefusal("runtime_active", "callsign release requires exact runtime cleanup proof")
    queue = store.connection.execute(
        "SELECT state FROM callsign_queue WHERE callsign=?", (assignment["callsign"],)
    ).fetchone()
    if queue is None or queue["state"] != "active":
        raise StorageRefusal("queue_conflict", "active callsign is not active in its queue")
    tail = int(
        store.connection.execute(
            "SELECT COALESCE(MAX(queue_position),-1) FROM callsign_queue WHERE pool_role=?",
            (assignment["role"],),
        ).fetchone()[0]
    ) + 1
    meta = _meta(store, assignment["role"])
    queue_version = int(meta["queue_version"]) + 1
    store.connection.execute(
        """
        UPDATE callsign_queue SET state='available',queue_position=?,version=version+1,
               updated_at=? WHERE callsign=?
        """,
        (tail, at, assignment["callsign"]),
    )
    store.connection.execute(
        "UPDATE callsign_queue_meta SET queue_version=? WHERE pool_role=?",
        (queue_version, assignment["role"]),
    )
    store.connection.execute(
        "DELETE FROM callsign_leases WHERE callsign=? AND agent_id=?",
        (assignment["callsign"], assignment["agent_id"]),
    )
    store.connection.execute(
        "UPDATE callsigns SET last_released_at=? WHERE callsign=?",
        (at, assignment["callsign"]),
    )
    agent = store.connection.execute(
        "SELECT version FROM agent_instances WHERE agent_id=?", (assignment["agent_id"],)
    ).fetchone()
    agent_version = int(agent["version"]) + 1
    store.connection.execute(
        """
        UPDATE agent_instances SET version=?,updated_at=?,update_text='callsign released',
               retired_at=? WHERE agent_id=?
        """,
        (agent_version, at, at, assignment["agent_id"]),
    )
    next_version = expected_version + 1
    store.connection.execute(
        """
        UPDATE callsign_assignments SET state='released',release_receipt_digest=?,
               queue_version=?,version=?,released_at=? WHERE callsign_assignment_id=?
        """,
        (receipt_digest, queue_version, next_version, at, assignment["callsign_assignment_id"]),
    )
    store.connection.execute(
        """
        INSERT INTO events
          (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
           occurred_at,detail_json,aggregate_kind,aggregate_id)
        VALUES(?, ?,NULL,NULL,?,'callsign_released','released','callsign released',?,?,
               'agent',?)
        """,
        (
            f"callsign:{assignment['callsign_assignment_id']}:released",
            assignment["agent_id"],
            agent_version,
            at,
            stable_json(
                {
                    "assignment_id": assignment["callsign_assignment_id"],
                    "queue_version": queue_version,
                    "release_receipt_digest": receipt_digest,
                }
            ),
            assignment["agent_id"],
        ),
    )
    row = store.connection.execute(
        "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
        (assignment["callsign_assignment_id"],),
    ).fetchone()
    return _assignment_value(row, idempotent=False)


def release_callsign(
    store: Any,
    assignment_id: str,
    expected_version: int,
    release_receipt_digest: str,
    at: str,
) -> dict[str, Any]:
    timestamp(at, "callsign release time")
    if not release_receipt_digest:
        raise StorageRefusal("receipt_required", "release requires exact cleanup receipt digest")
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise StorageRefusal("assignment_unknown", "callsign assignment does not exist")
            return _release_active_in_transaction(
                store, assignment, expected_version, release_receipt_digest, at
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "callsign release conflicted with canonical state"
        ) from exc
