"""Read-only proof of a frozen Champion retired by a completed handoff.

This is evidence validation, not a retirement or ownership writer. Historical
snapshot rows remain the original rows; a replacement is never a new member.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .sqlite_callsign_ops import digest
from .storage_types import StorageRefusal


def _require(condition: Any) -> None:
    if not condition:
        raise StorageRefusal(
            "snapshot_terminal_proof_invalid",
            "retired original lacks an exact completed handoff and current terminal proof",
        )


def _one(store: Any, table: str, key: str, identity: str) -> dict[str, Any]:
    row = store.connection.execute(
        f"SELECT * FROM {table} WHERE {key}=?", (identity,)
    ).fetchone()
    _require(row is not None)
    return dict(row)


def _binding(agent: Mapping[str, Any], runtime: Mapping[str, Any]) -> str:
    value = {key: agent[key] for key in (
        "agent_id", "task_id", "shotcaller_agent_id", "kind", "thread_id",
        "backend", "routing_name", "display_agent", "repository", "issue",
        "branch", "worktree",
    )}
    value.update({key: runtime[key] for key in (
        "runtime_instance_id", "session_ref", "endpoint", "runtime_generation",
    )})
    value["capabilities"] = runtime["capabilities_json"]
    return digest(value)


def retired_original(
    store: Any, operation: Mapping[str, Any], frozen: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a completed replacement's immutable intent to the frozen row."""
    rows = store.connection.execute(
        "SELECT * FROM runtime_replacements WHERE predecessor_agent_id=? LIMIT 2",
        (frozen["champion_agent_id"],),
    ).fetchall()
    if not rows:
        return _continued_original(store, operation, frozen)
    _require(len(rows) == 1)
    replacement = dict(rows[0])
    try:
        intent = json.loads(replacement["intent_json"])
        snapshot = intent["snapshot"]
        old_agent, old_runtime = snapshot["agent"], snapshot["runtime"]
        successor_receipt = json.loads(replacement["successor_receipt_json"])
        retirement = json.loads(replacement["retirement_receipt_json"])
        completion = json.loads(replacement["completion_receipt_json"])
        _require(
            replacement["state"] == "completed"
            and replacement["rollback_receipt_json"] is None
            and digest(intent) == replacement["intent_digest"]
            and intent["request"]["operation_id"] == replacement["operation_id"]
            and old_agent["agent_id"] == frozen["champion_agent_id"]
            and old_agent["task_id"] == replacement["task_id"] == frozen["task_id"]
            and old_agent["callsign"] == frozen["callsign"]
            and old_agent["shotcaller_agent_id"] == operation["predecessor_agent_id"]
            and old_agent["retired_at"] is None
            and _binding(old_agent, old_runtime) == frozen["binding_digest"]
            and any(m["squad_id"] == operation["squad_id"]
                    and m["champion_agent_id"] == old_agent["agent_id"]
                    for m in snapshot["squad_memberships"])
        )
        agent = _one(store, "agent_instances", "agent_id", old_agent["agent_id"])
        runtime = _one(store, "runtime_instances", "runtime_instance_id", old_runtime["runtime_instance_id"])
        successor = _one(store, "agent_instances", "agent_id", replacement["successor_agent_id"])
        successor_runtime = _one(store, "runtime_instances", "runtime_instance_id", replacement["successor_runtime_instance_id"])
        task = _one(store, "tasks", "task_id", frozen["task_id"])
        assignment = _one(store, "task_assignments", "task_assignment_id", replacement["assignment_id"])
        callsign = _one(store, "callsign_assignments", "callsign_assignment_id", snapshot["callsign_assignment"]["callsign_assignment_id"])
        event = _one(store, "events", "event_id", replacement["handoff_event_id"])
        outbox = _one(store, "delivery_outbox", "outbox_id", replacement["handoff_outbox_id"])
        detail = json.loads(event["detail_json"])
        _require(
            agent["retired_at"] is not None
            and agent["version"] == old_agent["version"] + 1
            and agent["routing_name"] is None and agent["display_agent"] is None
            and agent["status"] in {"completed", "complete", "cancelled", "canceled"}
            and all(agent[k] == old_agent[k] for k in (
                "agent_id", "task_id", "callsign", "kind", "thread_id", "backend",
                "address", "repository", "issue", "branch", "worktree", "shotcaller_agent_id",
            ))
            and runtime["status"] == "closed" and not runtime["verified"]
            and all(runtime[k] == old_runtime[k] for k in (
                "runtime_instance_id", "actor_agent_id", "harness_kind", "backend_kind",
                "session_ref", "endpoint", "runtime_generation", "capabilities_json",
            ))
            and retirement["verified"] is True and retirement["state"] == "retired"
            and retirement["operation_id"] == replacement["operation_id"]
            and retirement["agent_id"] == agent["agent_id"]
            and all(retirement[k] == runtime[k] for k in (
                "runtime_instance_id", "session_ref", "endpoint", "runtime_generation",
            ))
            and event["event_type"] == "runtime_replacement_handoff"
            and event["aggregate_id"] == replacement["operation_id"]
            and event["task_id"] == frozen["task_id"]
            and completion["event_id"] == event["event_id"]
            and completion["outbox_id"] == outbox["outbox_id"]
            and completion["handoff_digest"] == digest(detail)
            and outbox["event_id"] == event["event_id"]
            and outbox["recipient_agent_id"] == successor["agent_id"]
            and detail["intent_digest"] == replacement["intent_digest"]
            and detail["successor_receipt_digest"] == digest(successor_receipt)
            and detail["retirement_receipt_digest"] == digest(retirement)
            and detail["predecessor_agent_id"] == agent["agent_id"]
            and detail["successor_agent_id"] == successor["agent_id"]
            and successor_receipt["verified"] is True
            and successor["retired_at"] is None
            and successor["task_id"] == task["task_id"]
            and task["champion_agent_id"] == task["current_owner_agent_id"] == successor["agent_id"]
            and assignment["champion_agent_id"] == successor["agent_id"]
            and assignment["runtime_instance_id"] == successor_runtime["runtime_instance_id"]
            and assignment["state"] == "active"
            and successor_runtime["actor_agent_id"] == successor["agent_id"]
            and successor_runtime["status"] in {"active", "idle"} and successor_runtime["verified"]
            and successor_runtime["session_ref"] == successor["thread_id"] == successor_receipt["thread_id"]
            and successor_runtime["endpoint"] == successor["address"] == successor_receipt["endpoint"]
            and successor_runtime["runtime_generation"] == successor_receipt["runtime_generation"]
            and all(successor[k] == old_agent[k] == successor_receipt[k]
                    for k in ("repository", "issue", "branch", "worktree", "callsign"))
            and callsign["state"] == "active" and callsign["agent_id"] == successor["agent_id"]
            and callsign["runtime_instance_id"] == successor_runtime["runtime_instance_id"]
        )
        accepted = dict(successor_receipt)
        accepted["routing_name"] = replacement["canonical_routing_name"]
        _require(
            json.loads(assignment["acceptance_receipt_json"]) == accepted
            and callsign["acceptance_digest"] == digest(accepted)
            and successor_runtime["capabilities_json"] == json.dumps(accepted["capabilities"], sort_keys=True, separators=(",", ":"))
            and successor["role"] == "champion"
            and successor["shotcaller_agent_id"] == assignment["coordinator_agent_id"] == task["coordinator_agent_id"]
            and successor["routing_name"] == replacement["canonical_routing_name"]
            and successor["kind"] == successor_runtime["harness_kind"] == accepted["harness_kind"]
            and successor["backend"] == successor_runtime["backend_kind"] == accepted["backend_kind"]
            and successor["display_agent"] == accepted["display_agent"]
            and successor["agent_id"] == accepted["champion_agent_id"]
            and successor_runtime["runtime_instance_id"] == accepted["runtime_instance_id"]
            and completion["operation_id"] == replacement["operation_id"]
            and completion["recipient_agent_id"] == successor["agent_id"]
        )
        _require(not store.connection.execute(
            "SELECT 1 FROM runtime_instances WHERE actor_agent_id=? AND status IN ('active','idle') LIMIT 1",
            (agent["agent_id"],),
        ).fetchone())
        _require(not store.connection.execute(
            "SELECT 1 FROM callsign_assignments WHERE agent_id=? AND state IN ('active','reserved') LIMIT 1",
            (agent["agent_id"],),
        ).fetchone())
        # Include mutable canonical versions/identities in the read-CAS digest.
        proof = dict(replacement=replacement, agent=agent, runtime=runtime,
                     successor=successor, successor_runtime=successor_runtime,
                     task=task, assignment=assignment, callsign=callsign,
                     event=event, outbox=outbox)
        return {
            "champion_agent_id": frozen["champion_agent_id"], "task_id": frozen["task_id"],
            "state": "retired_handoff_satisfied", "reconciliation_id": replacement["operation_id"],
            "receipt_digest": digest({"intent": intent, "completion": completion}),
            "canonical_digest": digest(proof), "successor_agent_id": successor["agent_id"],
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageRefusal("snapshot_terminal_proof_invalid", "retired handoff proof is malformed") from exc


def _continued_original(store: Any, operation: Mapping[str, Any], frozen: Mapping[str, Any]) -> dict[str, Any]:
    """Require actual cleanup, issue reopen and exact-thread acceptance receipts."""
    from .cleanup import cleanup_action_digest
    from .sqlite_continuation_ops import _decode_thread_identity, _require_resumable_issue_binding
    rows = store.connection.execute(
        "SELECT * FROM thread_archives WHERE owner_agent_id=? AND task_id=? LIMIT 2",
        (frozen["champion_agent_id"], frozen["task_id"]),
    ).fetchall()
    _require(len(rows) == 1)
    archive = dict(rows[0])
    try:
        continuations = store.connection.execute(
            "SELECT * FROM continuation_operations WHERE archive_id=? LIMIT 2", (archive["archive_id"],)
        ).fetchall()
        _require(len(continuations) == 1)
        continued = dict(continuations[0])
        _require(archive["state"] == "resumed" and continued["state"] == "resumed")
        agent = _one(store, "agent_instances", "agent_id", archive["owner_agent_id"])
        runtime = _one(store, "runtime_instances", "runtime_instance_id", archive["runtime_instance_id"])
        lineage = _one(store, "thread_lineages", "lineage_id", archive["lineage_id"])
        task = _one(store, "tasks", "task_id", archive["task_id"])
        successor = _one(store, "agent_instances", "agent_id", continued["new_agent_id"])
        successor_runtime = _one(store, "runtime_instances", "runtime_instance_id", continued["runtime_instance_id"])
        assignment = _one(store, "task_assignments", "task_assignment_id", continued["assignment_id"])
        successor_task = _one(store, "tasks", "task_id", continued["new_task_id"])
        receipt = json.loads(assignment["acceptance_receipt_json"])
        event = _one(store, "events", "event_id", f"event:{continued['operation_id']}:resumed")
        successor_callsigns = [dict(r) for r in store.connection.execute(
            "SELECT * FROM callsign_assignments WHERE agent_id=? AND state IN ('active','reserved') LIMIT 2",
            (successor["agent_id"],),
        )]
        _require(len(successor_callsigns) == 1)
        callsign = successor_callsigns[0]
        incarnations = [dict(r) for r in store.connection.execute(
            "SELECT * FROM thread_incarnations WHERE lineage_id=? ORDER BY runtime_instance_id LIMIT 3",
            (lineage["lineage_id"],),
        )]
        _require(len(incarnations) == 2 and
            {r["runtime_instance_id"] for r in incarnations} == {runtime["runtime_instance_id"], successor_runtime["runtime_instance_id"]})
        for incarnation in incarnations:
            original = incarnation["runtime_instance_id"] == runtime["runtime_instance_id"]
            _require(incarnation["archive_id"] == (archive["archive_id"] if original else None)
                and incarnation["continuation_operation_id"] == (None if original else continued["operation_id"]))
        _require(
            agent["retired_at"] is not None
            and agent["shotcaller_agent_id"] == operation["predecessor_agent_id"]
            and agent["callsign"] == archive["prior_callsign"] == frozen["callsign"]
            and agent["task_id"] == archive["task_id"] == frozen["task_id"]
            and runtime["actor_agent_id"] == agent["agent_id"]
            and runtime["status"] == "closed" and not runtime["verified"]
            and _binding(agent, runtime) == frozen["binding_digest"]
            and task["state"] in {"completed", "complete"}
            and lineage["state"] == "active"
            and json.loads(lineage["resume_capabilities_json"]).get("exact_resume") is True
            and json.loads(lineage["resume_capabilities_json"]).get("safe_worktree_rebind") is True
            and runtime["harness_kind"] == successor_runtime["harness_kind"]
            and runtime["harness_kind"] in {lineage["provider_kind"], lineage["provider_kind"] + "-thread"}
            and _decode_thread_identity(lineage["provider_kind"], lineage["thread_identity"]) == runtime["session_ref"]
            and successor_runtime["session_ref"] == runtime["session_ref"] == successor["thread_id"] == receipt["thread_id"]
            and successor_runtime["status"] in {"active", "idle"} and successor_runtime["verified"]
            and successor_runtime["actor_agent_id"] == successor["agent_id"] == receipt["champion_agent_id"]
            and successor["retired_at"] is None and successor["role"] == "champion"
            and assignment["state"] == "active" and assignment["champion_agent_id"] == successor["agent_id"]
            and assignment["runtime_instance_id"] == successor_runtime["runtime_instance_id"] == receipt["runtime_instance_id"]
            and successor_task["current_owner_agent_id"] == successor_task["champion_agent_id"] == successor["agent_id"]
            and successor["task_id"] == successor_task["task_id"] == receipt["task_id"]
            and assignment["task_id"] == successor_task["task_id"]
            and successor["shotcaller_agent_id"] == assignment["coordinator_agent_id"] == successor_task["coordinator_agent_id"]
            and receipt["verified"] is True
            and callsign["state"] == "active" and callsign["role"] == "champion"
            and callsign["scope_kind"] == "task" and callsign["scope_id"] == successor_task["task_id"]
            and callsign["callsign"] == successor["callsign"] == assignment["callsign"] == continued["callsign"] == receipt["callsign"]
            and callsign["runtime_instance_id"] in {None, successor_runtime["runtime_instance_id"]}
            and callsign["acceptance_digest"] == digest(receipt)
            and successor["kind"] == successor_runtime["harness_kind"] == receipt["harness_kind"]
            and successor["backend"] == successor_runtime["backend_kind"] == receipt["backend_kind"]
            and successor["routing_name"] == receipt["routing_name"]
            and successor["display_agent"] == receipt["display_agent"]
            and json.loads(successor_runtime["capabilities_json"]) == receipt["capabilities"]
            and successor_runtime["endpoint"] == successor["address"] == receipt["endpoint"]
            and successor_runtime["runtime_generation"] == receipt["runtime_generation"]
            and all(agent[k] == archive[k] for k in ("repository", "issue", "branch", "worktree"))
            and all(successor[k] == continued[k] == receipt[k] for k in ("repository", "issue", "branch", "worktree"))
            and archive["repository"] == continued["repository"] and archive["issue"] == continued["issue"]
            and event["event_type"] == "thread_resumed" and event["aggregate_id"] == continued["operation_id"]
            and event["task_id"] == successor_task["task_id"]
            and json.loads(event["detail_json"]) == {"archive_id": archive["archive_id"], "lineage_id": lineage["lineage_id"]}
        )
        _require_resumable_issue_binding(store, task_id=task["task_id"], owner_agent_id=agent["agent_id"],
            runtime_instance_id=runtime["runtime_instance_id"], repository=archive["repository"], issue=archive["issue"], callsign=archive["prior_callsign"])
        _require_resumable_issue_binding(store, task_id=successor_task["task_id"], owner_agent_id=successor["agent_id"],
            runtime_instance_id=successor_runtime["runtime_instance_id"], repository=continued["repository"], issue=continued["issue"], callsign=successor["callsign"])
        cleanup = store.cleanup_execution_context(archive["cleanup_operation_id"])
        _require(cleanup["operation"]["state"] == "completed" and cleanup["disposition"] == "completed")
        archived_identity = cleanup["operation"]["actions"][0]["intended_state"]["identity"]
        _require(archived_identity == {"owner_id": agent["agent_id"], "task_id": task["task_id"]}
            and json.loads(archive["cleanup_evidence_json"]) == cleanup["proof"]
            and json.loads(archive["acceptance_json"])["required_gates_complete"] is True)
        teardown = _one(store, "teardown_receipts", "receipt_id", archive["cleanup_receipt_id"])
        action_receipts = [dict(r) for r in store.connection.execute(
            "SELECT * FROM cleanup_action_receipts WHERE operation_id=? ORDER BY action_id", (archive["cleanup_operation_id"],)
        )]
        _require(len(action_receipts) == len(cleanup["operation"]["actions"]))
        for item in action_receipts:
            _require(item["receipt_hash"] == digest(dict(outcome=item["outcome"], before=json.loads(item["before_json"]),
                after=json.loads(item["after_json"]), adapter=json.loads(item["adapter_receipt_json"]))))
        _require(teardown["receipt_hash"] == digest({"operation_id": archive["cleanup_operation_id"], "receipts": [r["receipt_hash"] for r in action_receipts]}))
        close_actions = [a for a in cleanup["operation"]["actions"] if a["action_kind"] == "issue_close"]
        _require(len(close_actions) == 1)
        close_receipt = next(r for r in action_receipts if r["action_id"] == close_actions[0]["action_id"])
        _require(json.loads(close_receipt["after_json"]) == {"repository": archive["repository"], "issue": archive["issue"], "state": "closed"})
        reopen = json.loads(continued["issue_receipt_json"])
        _require(reopen["outcome"] in {"applied", "already_applied"} and isinstance(reopen["receipt"], dict) and bool(reopen["receipt"]))
        reopened_event = _one(store, "events", "event_id", f"event:{continued['operation_id']}:issue-reopened")
        _require(reopened_event["event_type"] == "issue_reopened" and reopened_event["aggregate_id"] == continued["operation_id"]
            and reopened_event["task_id"] == task["task_id"]
            and json.loads(reopened_event["detail_json"]) == {"outcome": reopen["outcome"]})
        old_callsigns = [dict(r) for r in store.connection.execute("SELECT * FROM callsign_assignments WHERE agent_id=? LIMIT 2", (agent["agent_id"],))]
        release_actions = [a for a in cleanup["operation"]["actions"] if a["action_kind"] == "callsign_release"]
        _require(len(release_actions) == 1 and len(old_callsigns) == 1)
        release = release_actions[0]
        released = old_callsigns[0]
        _require(released["state"] == "released"
            and released["release_receipt_digest"] == cleanup_action_digest(release)
            and released["callsign_assignment_id"] == release["expected_identity"]["assignment_id"]
            and released["callsign"] == release["expected_identity"]["callsign"] == archive["prior_callsign"]
            and released["version"] == release["expected_identity"]["expected_version"] + 1)
        _require(not store.connection.execute("SELECT 1 FROM runtime_instances WHERE actor_agent_id=? AND status IN ('active','idle') LIMIT 1", (agent["agent_id"],)).fetchone())
        proof = dict(archive=archive, continued=continued, agent=agent, runtime=runtime, lineage=lineage,
            task=task, successor=successor, successor_runtime=successor_runtime, assignment=assignment,
            successor_task=successor_task, cleanup=cleanup, teardown=teardown, action_receipts=action_receipts,
            old_callsigns=old_callsigns, event=event, reopened_event=reopened_event,
            successor_callsigns=successor_callsigns, incarnations=incarnations)
        return dict(champion_agent_id=frozen["champion_agent_id"], task_id=frozen["task_id"], state="retired_handoff_satisfied",
            reconciliation_id=continued["operation_id"], receipt_digest=digest({"teardown": teardown, "resumed_event": event}),
            canonical_digest=digest(proof), successor_agent_id=successor["agent_id"])
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration) as exc:
        raise StorageRefusal("snapshot_terminal_proof_invalid", "retired continuation proof is malformed") from exc
