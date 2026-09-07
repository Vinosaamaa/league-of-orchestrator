"""Exact, bounded, public-safe startup context reads."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from .sqlite_callsign_ops import capabilities, digest
from .sqlite_rollover_ops import _safe_plan_value, rollover_status
from .agent_adapters import adapter_kind_from_runtime, builtin_agent_adapter_registry
from .multiplexer_adapters import builtin_multiplexer_adapter_registry
from .storage_types import StorageRefusal


MAX_STARTUP_OBLIGATIONS = 128
TERMINAL_REQUESTS = {"answered", "cancelled"}
TERMINAL_TASKS = {
    "completed",
    "complete",
    "ready_to_land",
    "rejected",
    "failed",
    "cancelled",
    "canceled",
}


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("startup_context_invalid", "startup observation time must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("startup_context_invalid", "startup observation time must include an offset")
    return parsed


def _runtime(
    store: Any, agent_id: str, runtime_instance_id: str, *, closed_ok: bool = False
) -> tuple[Any, tuple[str, ...]]:
    rows = store.connection.execute(
        """
        SELECT r.*,a.callsign,a.role,a.shotcaller_agent_id,a.task_id,a.thread_id,
               a.address,a.backend,a.routing_name,a.display_agent,a.retired_at,a.kind agent_kind
          FROM runtime_instances r JOIN agent_instances a ON a.agent_id=r.actor_agent_id
         WHERE r.actor_agent_id=? AND (r.status IN ('active','idle') OR r.runtime_instance_id=?)
         ORDER BY r.runtime_instance_id LIMIT 2
        """,
        (agent_id, runtime_instance_id),
    ).fetchall()
    if len(rows) != 1:
        raise StorageRefusal(
            "startup_identity_ambiguous",
            "startup identity does not resolve to exactly one live runtime",
        )
    row = rows[0]
    if row["runtime_instance_id"] != runtime_instance_id:
        raise StorageRefusal("startup_identity_stale", "startup runtime identity changed")
    if (
        (row["retired_at"] is not None and not closed_ok)
        or (row["status"] not in ('active', 'idle') and not (closed_ok and row["status"] == 'closed'))
        or not bool(row["verified"])
        or row["session_ref"] != row["thread_id"]
        or row["endpoint"] != row["address"]
        or (row["backend"] is not None and row["backend_kind"] != row["backend"])
    ):
        raise StorageRefusal(
            "startup_identity_stale",
            "startup runtime is stale or does not match the exact agent incarnation",
        )
    assignment = store.connection.execute(
        """
        SELECT callsign_assignment_id FROM callsign_assignments
         WHERE agent_id=? AND runtime_instance_id=? AND callsign=? AND role=?
           AND (state='active' OR (? AND state='released'))
           AND acceptance_digest IS NOT NULL AND acceptance_digest<>''
         ORDER BY callsign_assignment_id LIMIT 2
        """,
        (agent_id, runtime_instance_id, row["callsign"], row["role"], closed_ok),
    ).fetchall()
    if len(assignment) != 1:
        raise StorageRefusal(
            "startup_identity_stale",
            "startup callsign acceptance is missing or ambiguous for the exact runtime",
        )
    try:
        declared = capabilities(json.loads(row["capabilities_json"]))
    except (json.JSONDecodeError, TypeError, StorageRefusal) as exc:
        raise StorageRefusal(
            "startup_capability_invalid", "startup runtime capabilities are invalid"
        ) from exc
    return row, declared


def _registered_runtime(row: Any, *, rollover: bool = False) -> None:
    """Use the actual installed-source adapter inventory; never alias Cursor to Codex."""
    kind = adapter_kind_from_runtime(str(row["harness_kind"]))
    adapter = builtin_agent_adapter_registry().adapter(kind)
    if (
        row["agent_kind"] != adapter.launch_profile.runtime_kind
        or row["harness_kind"] != adapter.launch_profile.runtime_kind
    ):
        raise StorageRefusal("startup_identity_stale", "agent and runtime provider kinds differ")
    if adapter.contract.availability != "available":
        raise StorageRefusal("unsupported_capability", "runtime adapter is contract-only")
    for capability in (("identify", "status", "exit") if rollover else ("identify", "status")):
        adapter.contract.require(capability)
    multiplexer = builtin_multiplexer_adapter_registry().adapter(str(row["backend_kind"]))
    required = {"discover", "production_cleanup"} if rollover else {"discover"}
    if not required <= multiplexer.capabilities:
        raise StorageRefusal("unsupported_capability", "multiplexer lacks required lifecycle support")


def _agent_ref(
    store: Any, agent_id: str, *, live_required: bool = True
) -> dict[str, str]:
    row = store.connection.execute(
        "SELECT agent_id,callsign,retired_at FROM agent_instances WHERE agent_id=? AND role='shotcaller'",
        (agent_id,),
    ).fetchone()
    if row is not None and live_required and row["retired_at"] is not None:
        row = None
    if row is None:
        raise StorageRefusal("startup_identity_stale", "startup Shotcaller identity is unavailable")
    return {"agent_id": str(row["agent_id"]), "callsign": str(row["callsign"])}


def _runtime_public(row: Any, declared: tuple[str, ...]) -> dict[str, Any]:
    return {
        "runtime_instance_id": row["runtime_instance_id"],
        "harness_kind": row["harness_kind"],
        "backend_kind": row["backend_kind"],
        "runtime_generation_digest": hashlib.sha256(
            str(row["runtime_generation"]).encode("utf-8")
        ).hexdigest(),
        "capabilities": list(declared),
        "verified": True,
    }


def _bounded_obligations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) > MAX_STARTUP_OBLIGATIONS:
        raise StorageRefusal(
            "startup_context_too_large",
            f"startup obligations exceed the {MAX_STARTUP_OBLIGATIONS}-item bound",
        )
    return items


def _champion_context(store: Any, runtime: Any) -> dict[str, Any]:
    assignment_rows = store.connection.execute(
        """
        SELECT ta.*,t.state task_state,t.version task_version,t.project_id,
               r.state request_state,r.version request_version,r.execution_mode,
               r.route_reason_code,r.route_policy_version,r.route_confidence,r.requester_agent_id,
               t.current_owner_agent_id,t.coordinator_agent_id task_coordinator_agent_id
          FROM task_assignments ta
          JOIN tasks t ON t.task_id=ta.task_id
          JOIN requests r ON r.request_id=ta.request_id AND t.request_id=r.request_id
         WHERE ta.champion_agent_id=? AND ta.task_id=? AND ta.runtime_instance_id=?
           AND ta.assignment_role='champion' AND ta.state='active' LIMIT 2
        """,
        (runtime["actor_agent_id"], runtime["task_id"], runtime["runtime_instance_id"]),
    ).fetchall()
    if len(assignment_rows) != 1:
        raise StorageRefusal(
            "startup_identity_stale", "Champion startup assignment is missing or ambiguous"
        )
    assignment = assignment_rows[0]
    squad_rows = store.connection.execute(
        """
        SELECT s.squad_id,s.shotcaller_agent_id,s.version,s.owner_fence
          FROM squad_champions sc JOIN squads s ON s.squad_id=sc.squad_id
         WHERE sc.champion_agent_id=? AND s.state='active' LIMIT 2
        """,
        (runtime["actor_agent_id"],),
    ).fetchall()
    if len(squad_rows) != 1:
        raise StorageRefusal(
            "startup_identity_ambiguous", "Champion startup Squad is missing or ambiguous"
        )
    squad = squad_rows[0]
    if (
        runtime["shotcaller_agent_id"] != squad["shotcaller_agent_id"]
        or assignment["coordinator_agent_id"] != squad["shotcaller_agent_id"]
        or assignment["task_coordinator_agent_id"] != squad["shotcaller_agent_id"]
        or assignment["current_owner_agent_id"] != runtime["actor_agent_id"]
    ):
        raise StorageRefusal(
            "startup_owner_unreconciled", "exact Champion owner bindings require reconciliation"
        )
    obligations: list[dict[str, Any]] = []
    if assignment["task_state"] not in TERMINAL_TASKS:
        obligations.append(
            {"kind": "task", "id": assignment["task_id"], "state": assignment["task_state"]}
        )
    if assignment["request_state"] not in TERMINAL_REQUESTS:
        obligations.append(
            {"kind": "request", "id": assignment["request_id"], "state": assignment["request_state"]}
        )
    for row in store.connection.execute(
        """
        SELECT obligation_id,kind,state FROM obligations
         WHERE owner_agent_id=? AND state='open' ORDER BY obligation_id LIMIT ?
        """,
        (runtime["actor_agent_id"], MAX_STARTUP_OBLIGATIONS + 1),
    ):
        obligations.append({"kind": row["kind"], "id": row["obligation_id"], "state": row["state"]})
    cleanup = store.connection.execute(
        "SELECT cleanup_obligation_id,cleanup_state FROM cleanup_obligations WHERE task_id=?",
        (assignment["task_id"],),
    ).fetchone()
    if cleanup is not None and cleanup["cleanup_state"] not in {"completed", "cleanup_completed"}:
        obligations.append(
            {
                "kind": "cleanup",
                "id": cleanup["cleanup_obligation_id"],
                "state": cleanup["cleanup_state"],
            }
        )
    pending_delivery = store.connection.execute(
        """
        SELECT COUNT(*) FROM delivery_outbox
         WHERE recipient_agent_id=? AND state NOT IN ('delivered','cancelled')
        """,
        (runtime["actor_agent_id"],),
    ).fetchone()[0]
    if pending_delivery:
        obligations.append(
            {"kind": "delivery", "id": "pending-delivery", "state": "pending", "count": pending_delivery}
        )
    permitted = [] if assignment["task_state"] in TERMINAL_TASKS else [
        "task.transition", "request.progress", "request.result"
    ]
    return {
        "owning_shotcaller": _agent_ref(store, str(squad["shotcaller_agent_id"])),
        "requesting_shotcaller": _agent_ref(
            store, str(assignment["requester_agent_id"]), live_required=False
        ),
        "task": {
            "task_id": assignment["task_id"],
            "state": assignment["task_state"],
            "version": int(assignment["task_version"]),
        },
        "request": {
            "request_id": assignment["request_id"],
            "state": assignment["request_state"],
            "version": int(assignment["request_version"]),
            "execution_mode": assignment["execution_mode"],
        },
        "squad": {
            "squad_id": squad["squad_id"],
            "owner_version": int(squad["version"]),
            "owner_fence": int(squad["owner_fence"]),
        },
        "routing": {
            "project_id": assignment["project_id"],
            "reason_code": assignment["route_reason_code"],
            "policy_version": assignment["route_policy_version"],
            "confidence": assignment["route_confidence"],
            "routing_name": runtime["routing_name"],
            "display_agent": runtime["display_agent"],
        },
        "permitted_next_actions": permitted,
        "pending_obligations": _bounded_obligations(obligations),
    }


def _successor_context(store: Any, runtime: Any, at: datetime) -> dict[str, Any]:
    operations = store.connection.execute(
        """
        SELECT o.*,s.shotcaller_agent_id,s.version owner_version,s.owner_fence,
               i.state intake_state,i.fence intake_fence,s.state squad_state,
               snap.expires_at snapshot_expires_at
          FROM rollover_operations o JOIN squads s ON s.squad_id=o.squad_id
          JOIN shotcaller_intake i
            ON i.agent_id=o.successor_agent_id AND i.squad_id=o.squad_id
          JOIN active_champion_snapshots snap ON snap.snapshot_id=o.snapshot_id
         WHERE o.successor_agent_id=? AND o.state IN ('prepared','acknowledged','switched')
         ORDER BY o.operation_id LIMIT 2
        """,
        (runtime["actor_agent_id"],),
    ).fetchall()
    if len(operations) != 1:
        raise StorageRefusal(
            "startup_identity_ambiguous",
            "successor startup identity does not resolve to one active rollover",
        )
    operation = operations[0]
    if not set(json.loads(operation["required_capabilities_json"])) <= set(json.loads(runtime["capabilities_json"])):
        raise StorageRefusal("startup_capability_invalid", "successor lacks required handoff capabilities")
    plan = json.loads(operation["plan_json"])
    accepted = store.connection.execute(
        "SELECT runtime_instance_id FROM callsign_assignments WHERE callsign_assignment_id=? AND state='active'",
        (operation["callsign_assignment_id"],),
    ).fetchone()
    expected_runtime_id = operation["successor_runtime_instance_id"] or (
        accepted["runtime_instance_id"] if accepted is not None else None
    )
    if expected_runtime_id != runtime["runtime_instance_id"]:
        raise StorageRefusal(
            "startup_identity_stale", "successor runtime differs from the durable handoff identity"
        )
    if _time(operation["snapshot_expires_at"]) < at:
        raise StorageRefusal("startup_identity_stale", "successor handoff context expired")
    switched = operation["state"] == "switched"
    if (
        operation["squad_state"] != "active"
        or operation["shotcaller_agent_id"] != operation["successor_agent_id" if switched else "predecessor_agent_id"]
        or operation["intake_state"] != ("accepting" if switched else "draining")
        or operation["intake_fence"] != operation["owner_fence"]
        or operation["owner_version"] != operation["expected_owner_version"] + int(switched)
        or operation["owner_fence"] != operation["expected_owner_fence"] + int(switched)
    ):
        raise StorageRefusal("startup_identity_stale", "successor owner or intake fence changed")
    request_rows = store.connection.execute(
        """
        SELECT request_id,state FROM requests
         WHERE state NOT IN ('answered','cancelled')
           AND (owner_squad_id=? OR pending_owner_squad_id=? OR owner_agent_id=? OR pending_owner_agent_id=?)
         ORDER BY request_id LIMIT ?
        """,
        (
            operation["squad_id"],
            operation["squad_id"],
            runtime["actor_agent_id"],
            runtime["actor_agent_id"],
            MAX_STARTUP_OBLIGATIONS + 1,
        ),
    ).fetchall()
    plan_obligations = (plan["unresolved"], plan["pending_decisions"], plan["obligations"])
    if any(not isinstance(items, list) for items in plan_obligations) or sum(
        len(items) for items in plan_obligations
    ) > MAX_STARTUP_OBLIGATIONS:
        raise StorageRefusal(
            "startup_context_too_large", "startup handoff obligations exceed their bound"
        )
    obligations = []
    for items in plan_obligations:
        for detail in items:
            obligations.append(
                {
                    "kind": "handoff",
                    "id": f"{operation['operation_id']}:{len(obligations) + 1}",
                    "state": "pending",
                }
            )
    obligations.extend(
        {"kind": "request", "id": row["request_id"], "state": row["state"]}
        for row in request_rows
    )
    for row in store.connection.execute(
        "SELECT obligation_id,kind,state FROM obligations WHERE owner_agent_id=? AND state='open' ORDER BY obligation_id LIMIT ?",
        (runtime["actor_agent_id"], MAX_STARTUP_OBLIGATIONS + 1),
    ):
        obligations.append({"kind": row["kind"], "id": row["obligation_id"], "state": row["state"]})
    untriaged = store.connection.execute(
        "SELECT COUNT(*) FROM prompts WHERE current_owner_agent_id=? AND triage_state='untriaged'",
        (runtime["actor_agent_id"],),
    ).fetchone()[0]
    if untriaged:
        obligations.append({"kind": "prompt", "id": "untriaged-prompts", "state": "pending", "count": untriaged})
    actions = {
        "prepared": ["rollover.acknowledge"],
        "acknowledged": ["rollover.commit"],
        "switched": ["request.intake", "rollover.reconcile-descendant", "rollover.intake-plan", "rollover.reconcile-intake"],
    }[operation["state"]]
    return {
        "owning_shotcaller": _agent_ref(store, str(operation["shotcaller_agent_id"])),
        "requesting_shotcaller": _agent_ref(store, str(operation["predecessor_agent_id"]), live_required=False),
        "task": None,
        "request": {
            "pending_count": len(request_rows),
            "digest": digest(
                [{"request_id": row["request_id"], "state": row["state"]} for row in request_rows]
            ),
        },
        "squad": {
            "squad_id": operation["squad_id"],
            "owner_version": int(operation["owner_version"]),
            "owner_fence": int(operation["owner_fence"]),
        },
        "routing": {
            "operation_id": operation["operation_id"],
            "rollover_state": operation["state"],
            "intake_state": operation["intake_state"],
            "intake_fence": int(operation["intake_fence"]),
            "routing_name": runtime["routing_name"],
            "display_agent": runtime["display_agent"],
        },
        "permitted_next_actions": actions,
        "pending_obligations": _bounded_obligations(obligations),
    }


def startup_context(
    store: Any,
    agent_id: str,
    runtime_instance_id: str,
    at: str,
) -> dict[str, Any]:
    observed_at = _time(at)
    if not agent_id or not runtime_instance_id:
        raise StorageRefusal("startup_context_invalid", "startup identity is required")
    with store._read_transaction():
        runtime, declared = _runtime(store, agent_id, runtime_instance_id)
        _registered_runtime(runtime)
        if runtime["role"] == "champion":
            role_context = _champion_context(store, runtime)
        elif runtime["role"] == "shotcaller":
            role_context = _successor_context(store, runtime, observed_at)
        else:
            raise StorageRefusal(
                "startup_role_unsupported",
                "startup context is available only to Champions and successor Shotcallers",
            )
        result = {
            "schema": "league.startup-context.v1",
            "verified": True,
            "observed_at": at,
            "identity": {
                "agent_id": runtime["actor_agent_id"],
                "callsign": runtime["callsign"],
                "role": runtime["role"],
            },
            "runtime": _runtime_public(runtime, declared),
            **role_context,
        }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _safe_plan_value(result)
    if len(encoded) > 65_536:
        raise StorageRefusal("startup_context_too_large", "startup context exceeds 65536 bytes")
    return result


def rollover_run_context(store: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only retry preflight, including terminal retries after original plan expiry."""
    with store._read_transaction():
        operation = store.connection.execute(
            "SELECT * FROM rollover_operations WHERE operation_id=?", (manifest["operation_id"],)
        ).fetchone()
        if operation is not None:
            keys = (
                "squad_id", "predecessor_agent_id", "successor_agent_id", "callsign_assignment_id",
                "expected_owner_version", "expected_owner_fence", "authority_kind", "authority_digest",
            )
            if (
                any(operation[key] != manifest[key] for key in keys)
                or operation["plan_digest"] != digest(manifest["plan"])
                or tuple(json.loads(operation["required_capabilities_json"])) != capabilities(manifest["required_capabilities"])
                or (operation["successor_runtime_instance_id"] is not None
                    and operation["successor_runtime_instance_id"] != manifest["successor_runtime_instance_id"])
                or (operation["owner_event_id"] is not None and (
                    operation["owner_event_id"] != manifest["owner_event_id"]
                    or operation["owner_outbox_id"] != manifest["owner_outbox_id"]))
            ):
                raise StorageRefusal("rollover_conflict", "rollover retry changed durable identity")
        for participant in ("predecessor", "successor"):
            actor = manifest[f"{participant}_agent_id"]
            runtime_id = manifest[f"{participant}_runtime_instance_id"]
            exists = store.connection.execute(
                "SELECT 1 FROM runtime_instances WHERE actor_agent_id=? LIMIT 1", (actor,)
            ).fetchone()
            if participant == "successor" and exists is None:
                assignment = store.connection.execute(
                    "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                    (manifest["callsign_assignment_id"],),
                ).fetchone()
                if (
                    assignment is not None and assignment["agent_id"] == actor
                    and assignment["state"] in ("reserved", "rolled_back")
                    and assignment["runtime_instance_id"] is None
                ):
                    continue
            closed_ok = operation is not None and (
                operation["state"] in ("switched", "completed", "aborted") or participant == "successor"
            )
            row, _ = _runtime(store, actor, runtime_id, closed_ok=closed_ok)
            if row["role"] != "shotcaller":
                raise StorageRefusal("startup_role_unsupported", "rollover participants must be Shotcallers")
            _registered_runtime(row, rollover=True)
        return {"operation": rollover_status(store, manifest["operation_id"])}
