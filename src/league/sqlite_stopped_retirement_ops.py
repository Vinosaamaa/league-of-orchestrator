"""Atomic canonical settlement for one exactly proven stopped agent."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable, Mapping, Optional

from .sqlite_callsign_ops import _release_active_in_transaction
from .storage_types import FaultInjector, StorageRefusal


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _row(value: Any) -> Optional[dict[str, Any]]:
    return None if value is None else dict(value)


def status(store: Any, operation_id: str) -> Optional[dict[str, Any]]:
    row = _row(
        store.connection.execute(
            "SELECT * FROM stopped_agent_retirements WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    )
    if row is None:
        return None
    row["proof"] = json.loads(row.pop("proof_json"))
    row["receipt"] = json.loads(row.pop("receipt_json"))
    return row


def adapter_identity(store: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve only the adapter/provider identity before write ownership."""

    row = store.connection.execute(
        """
        SELECT r.harness_kind,a.display_agent
          FROM runtime_instances r
          JOIN agent_instances a ON a.agent_id=r.actor_agent_id
         WHERE r.runtime_instance_id=? AND r.actor_agent_id=?
           AND r.session_ref=? AND r.endpoint=? AND r.runtime_generation=?
           AND r.backend_kind=? AND r.status IN ('active','idle') AND r.verified=1
           AND a.version=? AND a.retired_at IS NULL AND a.role='champion'
           AND a.address=r.endpoint AND a.thread_id=r.session_ref
           AND a.backend=r.backend_kind
        """,
        (
            request["runtime_instance_id"],
            request["agent_id"],
            request["session_ref"],
            request["endpoint"],
            request["runtime_generation"],
            request["multiplexer_kind"],
            request["expected_agent_version"],
        ),
    ).fetchone()
    if row is None:
        raise StorageRefusal(
            "stopped_retirement_identity_mismatch",
            "retirement does not match one active exact canonical runtime",
        )
    return dict(row)


def target(store: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve one live canonical identity without changing its stale state."""

    runtime = store.connection.execute(
        """
        SELECT r.*,a.callsign,a.role,a.kind AS agent_kind,a.address,a.thread_id,
               a.backend,a.routing_name,a.display_agent,a.repository,a.branch,a.worktree,
               a.task_id,a.status AS agent_status,a.version AS agent_version,a.retired_at
          FROM runtime_instances r
          JOIN agent_instances a ON a.agent_id=r.actor_agent_id
         WHERE r.runtime_instance_id=?
        """,
        (request["runtime_instance_id"],),
    ).fetchone()
    exact_runtime = bool(
        runtime is not None
        and runtime["actor_agent_id"] == request["agent_id"]
        and runtime["session_ref"] == request["session_ref"]
        and runtime["endpoint"] == request["endpoint"]
        and runtime["runtime_generation"] == request["runtime_generation"]
        and runtime["backend_kind"] == request["multiplexer_kind"]
        and int(runtime["agent_version"]) == request["expected_agent_version"]
        and runtime["retired_at"] is None
        and runtime["role"] == "champion"
        and runtime["status"] in {"active", "idle"}
        and bool(runtime["verified"])
        and runtime["address"] == request["endpoint"]
        and runtime["thread_id"] == request["session_ref"]
        and runtime["backend"] == request["multiplexer_kind"]
    )
    if not exact_runtime:
        raise StorageRefusal(
            "stopped_retirement_identity_mismatch",
            "retirement does not match one active exact canonical runtime",
        )
    other_runtime = store.connection.execute(
        """
        SELECT runtime_instance_id FROM runtime_instances
         WHERE actor_agent_id=? AND status IN ('active','idle')
           AND runtime_instance_id<>? LIMIT 1
        """,
        (request["agent_id"], request["runtime_instance_id"]),
    ).fetchone()
    if other_runtime is not None:
        raise StorageRefusal(
            "stopped_retirement_identity_ambiguous",
            "agent owns another active runtime",
        )
    assignment = store.connection.execute(
        """
        SELECT * FROM callsign_assignments
         WHERE callsign_assignment_id=?
        """,
        (request["callsign_assignment_id"],),
    ).fetchone()
    active_assignments = store.connection.execute(
        """
        SELECT callsign_assignment_id FROM callsign_assignments
         WHERE agent_id=? AND state='active'
         ORDER BY callsign_assignment_id LIMIT 2
        """,
        (request["agent_id"],),
    ).fetchall()
    if (
        assignment is None
        or len(active_assignments) != 1
        or active_assignments[0]["callsign_assignment_id"]
        != request["callsign_assignment_id"]
        or assignment["agent_id"] != request["agent_id"]
        or assignment["callsign"] != runtime["callsign"]
        or assignment["state"] != "active"
        or int(assignment["version"]) != request["expected_callsign_version"]
        or assignment["runtime_instance_id"]
        not in {None, request["runtime_instance_id"]}
    ):
        raise StorageRefusal(
            "stopped_retirement_identity_mismatch",
            "retirement callsign ownership is missing or ambiguous",
        )
    if runtime["task_id"] is not None:
        task_owner = store.connection.execute(
            "SELECT current_owner_agent_id FROM tasks WHERE task_id=?",
            (runtime["task_id"],),
        ).fetchone()
        if task_owner is not None and task_owner["current_owner_agent_id"] == request["agent_id"]:
            raise StorageRefusal(
                "stopped_retirement_work_untransferred",
                "agent still owns canonical task work",
            )
    active_task_assignment = store.connection.execute(
        """
        SELECT task_assignment_id FROM task_assignments
         WHERE champion_agent_id=? AND state IN ('pending','launching','active','cleanup_pending')
         LIMIT 1
        """,
        (request["agent_id"],),
    ).fetchone()
    if active_task_assignment is not None:
        raise StorageRefusal(
            "stopped_retirement_work_untransferred",
            "agent still owns an active task assignment",
        )
    return {
        **dict(runtime),
        "callsign_assignment_id": assignment["callsign_assignment_id"],
        "callsign_version": int(assignment["version"]),
    }


def complete(
    store: Any,
    request: Mapping[str, Any],
    *,
    adapter_kind: str,
    verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    request_digest: str,
    at: str,
    fault: Optional[FaultInjector] = None,
) -> dict[str, Any]:
    """Commit runtime, agent, membership, and callsign settlement together."""

    receipt: dict[str, Any]
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT request_digest,receipt_json FROM stopped_agent_retirements WHERE operation_id=?",
                (request["operation_id"],),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise StorageRefusal(
                        "stopped_retirement_operation_conflict",
                        "retirement operation identity changed",
                    )
                value = json.loads(existing["receipt_json"])
                value.update({"state": "completed", "idempotent": True})
                return value
            canonical = target(store, request)
            if canonical["agent_kind"].removesuffix("-thread") != adapter_kind:
                raise StorageRefusal(
                    "stopped_retirement_identity_mismatch",
                    "canonical agent adapter changed before settlement",
                )
            proof = verifier(canonical)
            proof_digest = _digest(proof)
            receipt = {
                "schema": "league.stopped-agent-retirement-receipt.v1",
                "verified": True,
                "operation_id": request["operation_id"],
                "agent_id": request["agent_id"],
                "runtime_instance_id": request["runtime_instance_id"],
                "callsign_assignment_id": request["callsign_assignment_id"],
                "callsign": canonical["callsign"],
                "adapter_kind": adapter_kind,
                "provider_kind": request["provider_kind"],
                "multiplexer_kind": request["multiplexer_kind"],
                "session_ref": request["session_ref"],
                "endpoint": request["endpoint"],
                "runtime_generation": request["runtime_generation"],
                "terminal_status": request["terminal_status"],
                "repository_cleanup": False,
                "proof_digest": proof_digest,
                "completed_at": at,
            }
            release_digest = _digest(
                {
                    "schema": "league.stopped-agent-release.v1",
                    "operation_id": request["operation_id"],
                    "request_digest": request_digest,
                    "proof_digest": proof_digest,
                }
            )
            changed = store.connection.execute(
                """
                UPDATE runtime_instances
                   SET status='closed',verified=0,last_seen_at=?
                 WHERE runtime_instance_id=? AND actor_agent_id=?
                   AND session_ref=? AND endpoint=? AND runtime_generation=?
                   AND status IN ('active','idle') AND verified=1
                """,
                (
                    at,
                    request["runtime_instance_id"],
                    request["agent_id"],
                    request["session_ref"],
                    request["endpoint"],
                    request["runtime_generation"],
                ),
            )
            if changed.rowcount != 1:
                raise StorageRefusal(
                    "stopped_retirement_version_conflict",
                    "runtime changed after stopped-endpoint proof",
                )
            if fault is not None:
                fault("after_runtime_closed")
            assignment = store.connection.execute(
                "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                (request["callsign_assignment_id"],),
            ).fetchone()
            _release_active_in_transaction(
                store,
                assignment,
                request["expected_callsign_version"],
                release_digest,
                at,
            )
            if fault is not None:
                fault("after_callsign_released")
            agent = store.connection.execute(
                "SELECT version,retired_at FROM agent_instances WHERE agent_id=?",
                (request["agent_id"],),
            ).fetchone()
            if agent is None or agent["retired_at"] != at:
                raise StorageRefusal(
                    "stopped_retirement_version_conflict",
                    "callsign release did not retire the exact agent",
                )
            agent_version = int(agent["version"]) + 1
            agent_changed = store.connection.execute(
                """
                UPDATE agent_instances
                   SET status=?,version=?,updated_at=?,
                       update_text='stopped endpoint retired',blocker=NULL,
                       next_action='Retain immutable retirement history'
                 WHERE agent_id=? AND retired_at=? AND version=?
                """,
                (
                    request["terminal_status"],
                    agent_version,
                    at,
                    request["agent_id"],
                    at,
                    int(agent["version"]),
                ),
            )
            if agent_changed.rowcount != 1:
                raise StorageRefusal(
                    "stopped_retirement_version_conflict",
                    "agent changed during atomic retirement",
                )
            store.connection.execute(
                "DELETE FROM squad_champions WHERE champion_agent_id=?",
                (request["agent_id"],),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
                   update_text,occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,?,NULL,NULL,?,'stopped_agent_retired',?,
                       'stopped endpoint retired',?,?,'agent',?)
                """,
                (
                    f"retirement:{request['operation_id']}",
                    request["agent_id"],
                    agent_version,
                    request["terminal_status"],
                    at,
                    _json(
                        {
                            "operation_id": request["operation_id"],
                            "runtime_instance_id": request["runtime_instance_id"],
                            "proof_digest": proof_digest,
                            "repository_cleanup": False,
                        }
                    ),
                    request["agent_id"],
                ),
            )
            store.connection.execute(
                """
                INSERT INTO stopped_agent_retirements
                  (operation_id,agent_id,runtime_instance_id,callsign_assignment_id,
                   adapter_kind,provider_kind,multiplexer_kind,session_ref,endpoint,
                   runtime_generation,terminal_status,request_digest,proof_digest,
                   proof_json,receipt_json,completed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    request["operation_id"],
                    request["agent_id"],
                    request["runtime_instance_id"],
                    request["callsign_assignment_id"],
                    adapter_kind,
                    request["provider_kind"],
                    request["multiplexer_kind"],
                    request["session_ref"],
                    request["endpoint"],
                    request["runtime_generation"],
                    request["terminal_status"],
                    request_digest,
                    proof_digest,
                    _json(proof),
                    _json(receipt),
                    at,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "stopped-agent retirement conflicted with canonical state"
        ) from exc
    return {**receipt, "state": "completed", "idempotent": False}
