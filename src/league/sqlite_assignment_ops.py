"""Recoverable Champion assignment and task-transition operations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .sqlite_request_ops import _active_claim, _request_row, _time
from .storage_types import LIFECYCLE_STATES, StorageRefusal


ASSIGNMENT_STATES = {"pending", "launching", "active", "blocked", "cleanup_pending"}
TASK_TERMINAL_STATES = {
    "blocked",
    "ready_to_land",
    "completed",
    "rejected",
    "failed",
    "cancelled",
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def prepare_assignment(
    store: Any,
    assignment_id: str,
    request_id: str,
    claim_token: str,
    task_id: str,
    task_summary: str,
    coordinator_agent_id: str,
    champion_agent_id: str,
    callsign: str,
    repository: str,
    issue: int,
    branch: str,
    worktree: str,
    at: str,
) -> dict[str, Any]:
    _time(at, "assignment preparation time")
    if not all(
        (
            assignment_id,
            request_id,
            task_id,
            task_summary,
            coordinator_agent_id,
            champion_agent_id,
            callsign,
            repository,
            branch,
            worktree,
        )
    ) or issue < 1:
        raise StorageRefusal("invalid_assignment", "assignment identity is incomplete")
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?", (assignment_id,)
            ).fetchone()
            if existing is not None:
                exact = (
                    existing["request_id"] == request_id
                    and existing["task_id"] == task_id
                    and existing["coordinator_agent_id"] == coordinator_agent_id
                    and existing["champion_agent_id"] == champion_agent_id
                    and existing["callsign"] == callsign
                )
                if not exact:
                    raise StorageRefusal("assignment_conflict", "assignment retry has different identity")
                return {
                    "assignment_id": assignment_id,
                    "task_id": task_id,
                    "state": existing["state"],
                    "version": int(existing["version"]),
                    "idempotent": True,
                }
            request = _request_row(store, request_id)
            _active_claim(store, request_id, token=claim_token, at=at)
            if request["owner_agent_id"] != coordinator_agent_id:
                raise StorageRefusal("owner_mismatch", "assignment coordinator does not own the request")
            if request["execution_mode"] != "champion" or request["state"] != "in_progress":
                raise StorageRefusal(
                    "dispatch_required", "request must be explicitly dispatched to Champion mode first"
                )
            callsign_row = store.connection.execute(
                "SELECT pool_role,enabled FROM callsigns WHERE callsign=?", (callsign,)
            ).fetchone()
            lease = store.connection.execute(
                "SELECT 1 FROM callsign_leases WHERE callsign=?", (callsign,)
            ).fetchone()
            if (
                callsign_row is None
                or callsign_row["pool_role"] != "champion"
                or not callsign_row["enabled"]
                or lease is not None
            ):
                raise StorageRefusal("callsign_unavailable", "Champion callsign is not available")
            project = store.connection.execute(
                "SELECT project_id FROM projects WHERE repository=? AND state='active'", (repository,)
            ).fetchone()
            project_id = project["project_id"] if project is not None else None
            store.connection.execute(
                """
                INSERT INTO agent_instances
                  (agent_id,callsign,role,shotcaller_agent_id,task_id,kind,address,thread_id,
                   backend,routing_name,display_agent,repository,issue,branch,worktree,status,
                   version,updated_at,update_text,blocker,next_action,metadata_json,retired_at)
                VALUES(?,?,'champion',?,?, 'unbound',NULL,NULL,NULL,NULL,NULL,?,?,?,?,
                       'active',1,?,'assignment reserved',NULL,'Await verified launch receipt','{}',NULL)
                """,
                (
                    champion_agent_id,
                    callsign,
                    coordinator_agent_id,
                    task_id,
                    repository,
                    issue,
                    branch,
                    worktree,
                    at,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO tasks
                  (task_id,project_id,summary,state,version,current_owner_agent_id,
                   current_owner_squad_id,updated_at,request_id,coordinator_agent_id,
                   champion_agent_id,result_summary)
                VALUES(?,?,?,'pending',1,?,NULL,?,?,?,?,NULL)
                """,
                (
                    task_id,
                    project_id,
                    task_summary,
                    champion_agent_id,
                    at,
                    request_id,
                    coordinator_agent_id,
                    champion_agent_id,
                ),
            )
            store.connection.execute(
                "INSERT INTO callsign_leases(callsign,agent_id,launch_attempt_id,reserved_at) VALUES(?,?,NULL,?)",
                (callsign, champion_agent_id, at),
            )
            store.connection.execute(
                """
                INSERT INTO callsign_assignments
                  (callsign_assignment_id,callsign,task_id,agent_id,state,reserved_at,activated_at,released_at)
                VALUES(?,?,?,?,'reserved',?,NULL,NULL)
                """,
                (f"callsign-assignment:{assignment_id}", callsign, task_id, champion_agent_id, at),
            )
            store.connection.execute(
                """
                INSERT INTO task_assignments
                  (task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
                   runtime_instance_id,callsign,state,acceptance_receipt_json,failure_class,
                   cleanup_required,version,created_at,updated_at)
                VALUES(?,?,?,?,?,NULL,?,'pending',NULL,NULL,0,1,?,?)
                """,
                (
                    assignment_id,
                    task_id,
                    request_id,
                    coordinator_agent_id,
                    champion_agent_id,
                    callsign,
                    at,
                    at,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,
                   detail_json,request_id,aggregate_kind,aggregate_id)
                VALUES(?,NULL,?,1,'assignment_pending','pending','Champion assignment reserved',?,
                       ?,?,'assignment',?)
                """,
                (
                    f"assignment:{assignment_id}:1",
                    task_id,
                    at,
                    _json({"assignment_id": assignment_id, "callsign": callsign}),
                    request_id,
                    assignment_id,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "assignment reservation conflicted with canonical state") from exc
    return {
        "assignment_id": assignment_id,
        "task_id": task_id,
        "state": "pending",
        "version": 1,
        "idempotent": False,
    }


def mark_assignment_launching(store: Any, assignment_id: str, expected_version: int, at: str) -> dict[str, Any]:
    _time(at, "launch start time")
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT task_id,state,version FROM task_assignments WHERE task_assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is None:
                raise StorageRefusal("assignment_unknown", "assignment does not exist")
            if row["state"] == "launching" and int(row["version"]) == expected_version + 1:
                return {
                    "assignment_id": assignment_id,
                    "state": "launching",
                    "version": int(row["version"]),
                    "idempotent": True,
                }
            if row["state"] != "pending" or int(row["version"]) != expected_version:
                raise StorageRefusal("assignment_conflict", "assignment is not pending at the expected version")
            next_version = expected_version + 1
            store.connection.execute(
                "UPDATE task_assignments SET state='launching',version=?,updated_at=? WHERE task_assignment_id=?",
                (next_version, at, assignment_id),
            )
            store.connection.execute(
                "UPDATE tasks SET state='accepted',version=version+1,updated_at=? WHERE task_id=?",
                (at, row["task_id"]),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "assignment launch state conflicted") from exc
    return {"assignment_id": assignment_id, "state": "launching", "version": next_version, "idempotent": False}


def activate_assignment(
    store: Any,
    assignment_id: str,
    expected_version: int,
    receipt: dict[str, Any],
    event_id: str,
    outbox_id: str,
    at: str,
) -> dict[str, Any]:
    _time(at, "assignment activation time")
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
    }
    if set(receipt) != required or receipt.get("verified") is not True:
        raise StorageRefusal("receipt_unverified", "assignment activation requires one exact verified receipt")
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?", (assignment_id,)
            ).fetchone()
            if assignment is None:
                raise StorageRefusal("assignment_unknown", "assignment does not exist")
            if assignment["state"] == "active":
                stored = json.loads(assignment["acceptance_receipt_json"])
                if stored != receipt:
                    raise StorageRefusal("receipt_conflict", "active assignment has a different receipt")
                committed = store.connection.execute(
                    """
                    SELECT e.event_id,o.outbox_id
                      FROM events e JOIN delivery_outbox o ON o.event_id=e.event_id
                     WHERE e.aggregate_kind='assignment' AND e.aggregate_id=?
                       AND e.event_type='assignment_active'
                    """,
                    (assignment_id,),
                ).fetchone()
                if committed is None:
                    raise StorageRefusal(
                        "assignment_incomplete", "active assignment is missing its committed delivery"
                    )
                return {
                    "assignment_id": assignment_id,
                    "task_id": assignment["task_id"],
                    "state": "active",
                    "version": int(assignment["version"]),
                    "runtime_instance_id": assignment["runtime_instance_id"],
                    "event_id": committed["event_id"],
                    "outbox_id": committed["outbox_id"],
                    "idempotent": True,
                }
            if assignment["state"] != "launching" or int(assignment["version"]) != expected_version:
                raise StorageRefusal("assignment_conflict", "assignment is not launching at the expected version")
            agent = store.connection.execute(
                "SELECT * FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                (assignment["champion_agent_id"],),
            ).fetchone()
            exact = (
                receipt["assignment_id"] == assignment_id
                and receipt["task_id"] == assignment["task_id"]
                and receipt["champion_agent_id"] == assignment["champion_agent_id"]
                and receipt["callsign"] == assignment["callsign"]
                and receipt["repository"] == agent["repository"]
                and receipt["issue"] == agent["issue"]
                and receipt["branch"] == agent["branch"]
                and receipt["worktree"] == agent["worktree"]
                and receipt["routing_name"] == str(assignment["callsign"]).lower()
                and receipt["backend_kind"] in {"herdr", "tmux"}
                and all(isinstance(receipt[name], str) and receipt[name] for name in required - {"verified", "issue"})
            )
            if not exact:
                raise StorageRefusal("receipt_mismatch", "launch receipt does not match the reserved Champion identity")
            runtime_conflict = store.connection.execute(
                "SELECT 1 FROM runtime_instances WHERE runtime_instance_id=?",
                (receipt["runtime_instance_id"],),
            ).fetchone()
            if runtime_conflict is not None:
                raise StorageRefusal("runtime_conflict", "launch receipt runtime identity is already registered")
            store.connection.execute(
                """
                INSERT INTO runtime_instances
                  (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,endpoint,
                   runtime_generation,status,verified,last_seen_at)
                VALUES(?,?,?,?,?,?,?,'active',1,?)
                """,
                (
                    receipt["runtime_instance_id"],
                    assignment["champion_agent_id"],
                    receipt["harness_kind"],
                    receipt["backend_kind"],
                    receipt["thread_id"],
                    receipt["endpoint"],
                    receipt["runtime_generation"],
                    at,
                ),
            )
            store.connection.execute(
                """
                UPDATE agent_instances
                   SET kind=?,address=?,thread_id=?,backend=?,routing_name=?,display_agent=?,
                       status='working',version=version+1,updated_at=?,update_text='assignment accepted',
                       next_action='Perform the assigned task'
                 WHERE agent_id=?
                """,
                (
                    receipt["harness_kind"],
                    receipt["endpoint"],
                    receipt["thread_id"],
                    receipt["backend_kind"],
                    receipt["routing_name"],
                    receipt["display_agent"],
                    at,
                    assignment["champion_agent_id"],
                ),
            )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE task_assignments
                   SET runtime_instance_id=?,state='active',acceptance_receipt_json=?,version=?,updated_at=?
                 WHERE task_assignment_id=?
                """,
                (receipt["runtime_instance_id"], _json(receipt), next_version, at, assignment_id),
            )
            store.connection.execute(
                "UPDATE callsign_assignments SET state='active',activated_at=? WHERE task_id=?",
                (at, assignment["task_id"]),
            )
            store.connection.execute(
                "UPDATE tasks SET state='in_progress',version=version+1,updated_at=? WHERE task_id=?",
                (at, assignment["task_id"]),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,
                   detail_json,request_id,aggregate_kind,aggregate_id)
                VALUES(?,NULL,?,?,'assignment_active','active','verified Champion accepted assignment',?,
                       ?,?,'assignment',?)
                """,
                (
                    event_id,
                    assignment["task_id"],
                    next_version,
                    at,
                    _json({"assignment_id": assignment_id, "runtime_instance_id": receipt["runtime_instance_id"]}),
                    assignment["request_id"],
                    assignment_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO delivery_outbox
                  (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                VALUES(?,?,?,'pending',?,0)
                """,
                (outbox_id, event_id, assignment["champion_agent_id"], at),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "assignment activation conflicted with canonical state") from exc
    return {
        "assignment_id": assignment_id,
        "task_id": assignment["task_id"],
        "state": "active",
        "version": next_version,
        "runtime_instance_id": receipt["runtime_instance_id"],
        "event_id": event_id,
        "outbox_id": outbox_id,
        "idempotent": False,
    }


def block_assignment(
    store: Any,
    assignment_id: str,
    expected_version: int,
    failure_class: str,
    cleanup_required: bool,
    cleanup_proven: bool,
    at: str,
) -> dict[str, Any]:
    _time(at, "assignment failure time")
    if not failure_class:
        raise StorageRefusal("invalid_assignment_failure", "assignment failure class is required")
    state = "cleanup_pending" if cleanup_required and not cleanup_proven else "blocked"
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?", (assignment_id,)
            ).fetchone()
            if assignment is None or int(assignment["version"]) != expected_version:
                raise StorageRefusal("assignment_conflict", "assignment failure expected-version failed")
            if assignment["state"] not in {"pending", "launching", "cleanup_pending"}:
                raise StorageRefusal("assignment_conflict", "assignment is not fail-recoverable")
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE task_assignments SET state=?,failure_class=?,cleanup_required=?,version=?,updated_at=?
                 WHERE task_assignment_id=?
                """,
                (state, failure_class, int(cleanup_required), next_version, at, assignment_id),
            )
            store.connection.execute(
                "UPDATE tasks SET state='blocked',version=version+1,updated_at=? WHERE task_id=?",
                (at, assignment["task_id"]),
            )
            if state == "blocked":
                store.connection.execute(
                    "DELETE FROM callsign_leases WHERE callsign=? AND agent_id=?",
                    (assignment["callsign"], assignment["champion_agent_id"]),
                )
                store.connection.execute(
                    """
                    UPDATE callsign_assignments SET state='blocked',released_at=?
                     WHERE task_id=? AND state='reserved'
                    """,
                    (at, assignment["task_id"]),
                )
                store.connection.execute(
                    "UPDATE agent_instances SET retired_at=?,updated_at=?,update_text='launch blocked' WHERE agent_id=?",
                    (at, at, assignment["champion_agent_id"]),
                )
            else:
                store.connection.execute(
                    """
                    INSERT INTO cleanup_obligations
                      (cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,version,updated_at)
                    VALUES(?,?,'pending','failed_launch','Verify and clean only the exact partial runtime',1,?)
                    ON CONFLICT(task_id) DO UPDATE SET cleanup_state='pending',updated_at=excluded.updated_at
                    """,
                    (f"cleanup:{assignment['task_id']}", assignment["task_id"], at),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "assignment failure conflicted with canonical state") from exc
    return {"assignment_id": assignment_id, "state": state, "version": next_version}


def transition_task(
    store: Any,
    task_id: str,
    runtime_instance_id: str,
    expected_version: int,
    state: str,
    update: str,
    next_action: str,
    blocker: Optional[str],
    transition_id: str,
    transition_key: str,
    event_id: str,
    outbox_id: str,
    recipient_agent_id: str,
    at: str,
) -> dict[str, Any]:
    _time(at, "task transition time")
    if state not in LIFECYCLE_STATES and state not in {"rejected"}:
        raise StorageRefusal("invalid_task_transition", "task transition state is invalid")
    if not all((update, next_action, transition_id, transition_key, event_id, outbox_id)):
        raise StorageRefusal("invalid_task_transition", "task transition fields are incomplete")
    try:
        with store._transaction():
            duplicate = store.connection.execute(
                "SELECT transition_id,event_id,task_id,to_state FROM task_transitions WHERE transition_key=?",
                (transition_key,),
            ).fetchone()
            if duplicate is not None:
                if duplicate["task_id"] != task_id or duplicate["to_state"] != state:
                    raise StorageRefusal("transition_conflict", "transition key has different task content")
                outbox = store.connection.execute(
                    "SELECT outbox_id,recipient_agent_id FROM delivery_outbox WHERE event_id=?",
                    (duplicate["event_id"],),
                ).fetchone()
                if outbox is None or outbox["recipient_agent_id"] != recipient_agent_id:
                    raise StorageRefusal(
                        "recipient_mismatch", "transition retry recipient is not the committed coordinator"
                    )
                return {
                    "task_id": task_id,
                    "state": state,
                    "event_id": duplicate["event_id"],
                    "outbox_id": outbox["outbox_id"],
                    "idempotent": True,
                }
            task = store.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            assignment = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None or assignment is None or assignment["state"] != "active":
                raise StorageRefusal("assignment_inactive", "task has no active verified assignment")
            if assignment["runtime_instance_id"] != runtime_instance_id:
                raise StorageRefusal("runtime_mismatch", "task transition runtime does not own the assignment")
            runtime = store.connection.execute(
                """
                SELECT actor_agent_id,runtime_generation,status,verified
                  FROM runtime_instances WHERE runtime_instance_id=?
                """,
                (runtime_instance_id,),
            ).fetchone()
            if (
                runtime is None
                or runtime["actor_agent_id"] != assignment["champion_agent_id"]
                or runtime["status"] not in {"active", "idle"}
                or not runtime["verified"]
            ):
                raise StorageRefusal(
                    "runtime_unverified", "task transition runtime generation is not verified and live"
                )
            if int(task["version"]) != expected_version:
                raise StorageRefusal("version_conflict", "task transition expected-version failed")
            if recipient_agent_id != assignment["coordinator_agent_id"]:
                raise StorageRefusal("recipient_mismatch", "task transition recipient is not its coordinator")
            next_version = expected_version + 1
            result_summary = update if state in {"completed", "ready_to_land"} else task["result_summary"]
            store.connection.execute(
                """
                UPDATE tasks SET state=?,result_summary=?,version=?,updated_at=?
                 WHERE task_id=? AND version=?
                """,
                (state, result_summary, next_version, at, task_id, expected_version),
            )
            store.connection.execute(
                """
                INSERT INTO task_transitions
                  (transition_id,transition_key,task_id,from_state,to_state,update_text,next_action,
                   blocker,created_at,event_id)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    transition_id,
                    transition_key,
                    task_id,
                    task["state"],
                    state,
                    update,
                    next_action,
                    blocker,
                    at,
                    event_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,
                   detail_json,request_id,aggregate_kind,aggregate_id,source_event_id)
                VALUES(?,NULL,?,?,'task_transition',?,?,?,?,?,'task',?,NULL)
                """,
                (
                    event_id,
                    task_id,
                    next_version,
                    state,
                    update,
                    at,
                    _json(
                        {
                            "champion_agent_id": assignment["champion_agent_id"],
                            "runtime_instance_id": runtime_instance_id,
                            "runtime_generation": runtime["runtime_generation"],
                        }
                    ),
                    task["request_id"],
                    task_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO delivery_outbox
                  (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                VALUES(?,?,?,'pending',?,0)
                """,
                (outbox_id, event_id, recipient_agent_id, at),
            )
            store.connection.execute(
                """
                INSERT INTO obligations
                  (obligation_id,owner_agent_id,kind,aggregate_id,dedupe_key,state,
                   next_attention_at,details_json,created_at,updated_at)
                VALUES(?,?, 'delivery',?,?, 'open',?, '{}',?,?)
                """,
                (f"obligation:{outbox_id}", recipient_agent_id, outbox_id, f"delivery:{outbox_id}", at, at, at),
            )
            if state in TASK_TERMINAL_STATES:
                cleanup_state = "awaiting_authority" if state == "ready_to_land" else "pending"
                store.connection.execute(
                    """
                    INSERT INTO cleanup_obligations
                      (cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,version,updated_at)
                    VALUES(?,?,?,?,?,1,?)
                    ON CONFLICT(task_id) DO NOTHING
                    """,
                    (
                        f"cleanup:{task_id}",
                        task_id,
                        cleanup_state,
                        "ready_to_land" if state == "ready_to_land" else "terminal_task",
                        "Await exact landing/release proof" if state == "ready_to_land" else "Reconcile exact task resources",
                        at,
                    ),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "task transition conflicted with canonical state") from exc
    return {
        "task_id": task_id,
        "state": state,
        "version": next_version,
        "event_id": event_id,
        "outbox_id": outbox_id,
        "idempotent": False,
    }
