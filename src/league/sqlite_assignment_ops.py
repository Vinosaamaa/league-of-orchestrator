"""Recoverable Champion assignment and task-transition operations."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .sqlite_request_ops import _active_claim, _bounded_public_text, _request_row, _time
from .sqlite_project_ops import canonical_repository, resolve_project_routing_identity
from .sqlite_callsign_ops import (
    _reserve_in_transaction,
    _rollback_reserved_in_transaction,
    capabilities,
    stable_json,
)
from .storage_assignment import (
    FinishHiddenAssignmentCommand,
    LegacyDisplayReconciliationCommand,
    PrepareAssignmentCommand,
)
from .storage_types import LIFECYCLE_STATES, StorageRefusal
from .issue_first import (
    issue_scope_digest,
    normalize_issue_title,
    task_issue_semantic_binding_digest,
    validate_issue_receipt,
)


ASSIGNMENT_STATES = {
    "pending",
    "launching",
    "active",
    "blocked",
    "cleanup_pending",
    "completed",
    "failed",
    "promotion_required",
}
TASK_TERMINAL_STATES = {
    "ready_to_land",
    "completed",
    "complete",
    "rejected",
    "failed",
    "cancelled",
    "canceled",
}
TASK_CLEANUP_STATES = TASK_TERMINAL_STATES | {"blocked"}
TASK_SETTLEMENT_TRANSITIONS = frozenset(
    {
        "blocked",
        "completed",
        "complete",
        "ready_to_land",
        "rejected",
        "cancelled",
        "canceled",
        "failed",
    }
)
TASK_TRANSITIONS = {
    "pending": frozenset(
        {"accepted", "blocked", "rejected", "cancelled", "canceled", "failed"}
    ),
    "accepted": frozenset(
        {"in_progress", "blocked", "rejected", "cancelled", "canceled", "failed"}
    ),
    "active": TASK_SETTLEMENT_TRANSITIONS | {"started", "working", "progress"},
    "started": TASK_SETTLEMENT_TRANSITIONS | {"in_progress", "working", "progress"},
    "in_progress": TASK_SETTLEMENT_TRANSITIONS | {"working", "progress"},
    "working": TASK_SETTLEMENT_TRANSITIONS | {"working", "progress"},
    "progress": TASK_SETTLEMENT_TRANSITIONS | {"working", "progress"},
    "blocked": TASK_SETTLEMENT_TRANSITIONS | {"in_progress", "working", "progress"},
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _stored_object(value: Any, code: str, message: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal(code, message) from exc
    if not isinstance(parsed, dict):
        raise StorageRefusal(code, message)
    return parsed


def _legacy_result_receipt(row: Any) -> dict[str, Any]:
    detail = _stored_object(
        row["detail_json"],
        "legacy_display_ambiguous",
        "legacy display reconciliation history is malformed",
    )
    receipt = detail.get("receipt")
    receipt_keys = {
        "schema",
        "reconciliation_id",
        "assignment_id",
        "champion_agent_id",
        "runtime_instance_id",
        "source",
        "applies_to_source",
        "state_change_seq",
        "sidebar_name",
        "task_label",
        "thread_title",
        "terminal_title",
        "observation_digest",
    }
    string_keys = receipt_keys - {"state_change_seq"}
    exact = bool(
        set(detail) == {"schema", "intent_digest", "receipt"}
        and detail.get("schema")
        == "league.legacy-display-reconciliation-result.v1"
        and isinstance(detail.get("intent_digest"), str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", detail["intent_digest"]))
        and isinstance(receipt, dict)
        and set(receipt) == receipt_keys
        and all(
            isinstance(receipt.get(key), str) and receipt[key]
            for key in string_keys
        )
        and receipt.get("schema") == "league.legacy-display-reconciliation.v1"
        and type(receipt.get("state_change_seq")) is int
        and receipt["state_change_seq"] >= 0
        and bool(re.fullmatch(r"[0-9a-f]{64}", receipt["observation_digest"]))
    )
    if not exact:
        raise StorageRefusal(
            "legacy_display_ambiguous",
            "legacy display reconciliation history has no exact final receipt",
        )
    return receipt


def _physical_worktree_exact(stored: Any, expected: Any) -> bool:
    if (
        not isinstance(stored, str)
        or not stored
        or not isinstance(expected, str)
        or not expected
    ):
        return False
    stored_path = Path(stored)
    expected_path = Path(expected)
    if not stored_path.is_absolute() or not expected_path.is_absolute():
        return False
    try:
        return (
            stored_path.resolve(strict=True) == expected_path.resolve(strict=True)
            and stored_path.is_dir()
            and expected_path.is_dir()
        )
    except OSError:
        return False


def _validate_assignment_command(command: PrepareAssignmentCommand) -> None:
    _time(command.at, "assignment preparation time")
    if command.assignment_role not in {"champion", "hidden-worker"} or not all(
        (
            command.assignment_id,
            command.request_id,
            command.task_id,
            command.task_summary,
            command.coordinator_agent_id,
            command.champion_agent_id,
        )
    ):
        raise StorageRefusal("invalid_assignment", "assignment identity is incomplete")
    if command.assignment_role == "champion" and (
        not all((command.repository, command.branch, command.worktree)) or command.issue < 1
    ):
        raise StorageRefusal(
            "invalid_assignment", "visible Champion assignment requires issue and worktree identity"
        )
    if command.assignment_role == "champion" and command.issue_receipt is None:
        raise StorageRefusal(
            "issue_verification_required",
            "visible Champion assignment requires exact owner-API issue evidence",
        )
    if command.assignment_role == "hidden-worker" and (
        not command.dispatch_id
        or any((command.repository, command.branch, command.worktree))
        or command.issue != 0
        or command.promoted_from_assignment_id is not None
    ):
        raise StorageRefusal(
            "invalid_hidden_assignment",
            "hidden scientist assignment requires one dispatch and no issue or worktree lifecycle",
        )
    capabilities(command.required_capabilities)


def _assignment_retry(
    store: Any, command: PrepareAssignmentCommand
) -> Optional[dict[str, Any]]:
    existing = store.connection.execute(
        """
        SELECT a.*,t.summary task_summary,i.repository,i.issue,i.branch,i.worktree,
               ca.requirements_json callsign_requirements_json
          FROM task_assignments a
          JOIN tasks t ON t.task_id=a.task_id
          JOIN agent_instances i ON i.agent_id=a.champion_agent_id
          JOIN callsign_assignments ca
            ON ca.callsign_assignment_id='callsign-assignment:'||a.task_assignment_id
         WHERE a.task_assignment_id=?
        """,
        (command.assignment_id,),
    ).fetchone()
    if existing is None:
        return None
    exact = (
        existing["request_id"] == command.request_id
        and existing["task_id"] == command.task_id
        and existing["coordinator_agent_id"] == command.coordinator_agent_id
        and existing["champion_agent_id"] == command.champion_agent_id
        and existing["task_summary"] == command.task_summary
        and existing["assignment_role"] == command.assignment_role
        and existing["dispatch_id"] == command.dispatch_id
        and existing["promoted_from_assignment_id"] == command.promoted_from_assignment_id
        and (existing["repository"] or "") == command.repository
        and int(existing["issue"] or 0) == command.issue
        and (existing["branch"] or "") == command.branch
        and (existing["worktree"] or "") == command.worktree
        and tuple(json.loads(existing["callsign_requirements_json"]))
            == capabilities(command.required_capabilities)
    )
    if not exact:
        raise StorageRefusal("assignment_conflict", "assignment retry has different identity")
    if existing["assignment_role"] == "champion":
        _verify_assignment_issue_binding(store, existing, command)
    return {
        "assignment_id": command.assignment_id,
        "task_id": command.task_id,
        "state": existing["state"],
        "version": int(existing["version"]),
        "callsign": existing["callsign"],
        "idempotent": True,
    }


def _verify_assignment_issue_binding(
    store: Any, existing: sqlite3.Row, command: PrepareAssignmentCommand
) -> None:
    receipt = validate_issue_receipt(command.issue_receipt or {})
    binding = store.connection.execute(
        """
        SELECT b.*,s.task_id selection_task_id,s.task_summary selection_task_summary,
               s.repository selection_repository,s.repository_key selection_repository_key,
               s.issue selection_issue,s.issue_url selection_issue_url,
               s.issue_state selection_issue_state,s.issue_title selection_issue_title,
               s.normalized_title selection_normalized_title,
               s.semantic_scope_digest selection_semantic_scope_digest,
               s.issue_body_digest selection_issue_body_digest,
               s.task_scope_digest selection_task_scope_digest
          FROM repository_issue_bindings b
          JOIN repository_issue_selection_receipts s
            ON s.receipt_digest=b.issue_selection_receipt_digest
         WHERE b.assignment_id=?
        """,
        (command.assignment_id,),
    ).fetchone()
    if binding is None:
        raise StorageRefusal(
            "assignment_issue_reconciliation_required",
            "active Champion assignment predates its migration-18 issue binding",
        )
    semantic_binding = task_issue_semantic_binding_digest(
        command.repository,
        command.issue,
        command.task_id,
        existing["task_summary"],
        binding["issue_title"],
        binding["selection_semantic_scope_digest"],
    )
    stable_exact = (
        binding["task_id"] == command.task_id
        and binding["assignment_id"] == command.assignment_id
        and binding["request_id"] == command.request_id
        and binding["repository"] == command.repository
        and int(binding["issue"]) == command.issue
        and binding["issue_state"] == "open"
        and binding["semantic_binding_digest"] == semantic_binding
        and binding["selection_task_id"] == command.task_id
        and binding["selection_task_summary"] == existing["task_summary"]
        and binding["selection_repository"] == command.repository
        and binding["selection_repository_key"] == receipt["repository_key"]
        and int(binding["selection_issue"]) == command.issue
        and binding["selection_issue_state"] == "open"
        and binding["selection_normalized_title"]
        == normalize_issue_title(existing["task_summary"])
        and binding["selection_task_scope_digest"] == binding["task_scope_digest"]
        and receipt["repository"] == binding["repository"]
        and receipt["issue"] == int(binding["issue"])
        and receipt["issue_url"] == binding["issue_url"]
        and receipt["issue_title"] == binding["issue_title"]
        and receipt["issue_body_digest"] == binding["issue_body_digest"]
        and receipt["semantic_scope_digest"]
        == binding["selection_semantic_scope_digest"]
        and receipt["task_scope_digest"] == binding["task_scope_digest"]
        and receipt["issue_selection_receipt_digest"]
        == binding["issue_selection_receipt_digest"]
        and receipt["verifier_kind"] == binding["verifier_kind"]
    )
    if not stable_exact:
        raise StorageRefusal(
            "assignment_issue_reverification_failed",
            "active Champion issue no longer matches its canonical migration-18 binding",
        )


def _validate_assignment_reservation(
    store: Any, command: PrepareAssignmentCommand
) -> tuple[Optional[str], Optional[sqlite3.Row]]:
    request = _request_row(store, command.request_id)
    _active_claim(store, command.request_id, token=command.claim_token, at=command.at)
    if request["owner_agent_id"] != command.coordinator_agent_id:
        raise StorageRefusal("owner_mismatch", "assignment coordinator does not own the request")
    if request["state"] != "in_progress":
        raise StorageRefusal(
            "dispatch_required", "request must be explicitly dispatched before assignment"
        )
    if command.assignment_role == "hidden-worker":
        dispatch = store.connection.execute(
            "SELECT * FROM request_dispatches WHERE dispatch_id=? AND request_id=?",
            (command.dispatch_id, command.request_id),
        ).fetchone()
        if (
            request["execution_mode"] != "hidden"
            or dispatch is None
            or dispatch["execution_mode"] != "hidden"
            or dispatch["requested_model"] is None
            or dispatch["requested_effort"] is None
            or dispatch["reason_code"] != "hidden_scientist"
        ):
            raise StorageRefusal(
                "hidden_dispatch_required", "hidden scientist assignment must match its exact dispatch"
            )
        owner = store.connection.execute(
            "SELECT role,retired_at FROM agent_instances WHERE agent_id=?",
            (command.coordinator_agent_id,),
        ).fetchone()
        if owner is None or owner["role"] != "shotcaller" or owner["retired_at"] is not None:
            raise StorageRefusal(
                "hidden_owner_invalid", "hidden scientist assignment owner must be a live Shotcaller"
            )
        return None, dispatch
    if request["execution_mode"] != "champion":
        source = store.connection.execute(
            "SELECT * FROM task_assignments WHERE task_assignment_id=? AND request_id=?",
            (command.promoted_from_assignment_id, command.request_id),
        ).fetchone()
        if (
            request["execution_mode"] != "hidden"
            or source is None
            or source["assignment_role"] != "hidden-worker"
            or source["state"] != "active"
        ):
            raise StorageRefusal(
                "champion_dispatch_required",
                "Champion assignment requires Champion dispatch or an active hidden-scientist promotion",
            )
    project = resolve_project_routing_identity(store, command.repository)
    return (project[0] if project is not None and project[1] == "active" else None), None


def _persist_assignment_reservation(
    store: Any,
    command: PrepareAssignmentCommand,
    project_id: Optional[str],
    hidden_dispatch: Optional[sqlite3.Row],
) -> str:
    callsign_assignment_id = f"callsign-assignment:{command.assignment_id}"
    selected = _reserve_in_transaction(
        store,
        callsign_assignment_id,
        command.champion_agent_id,
        command.assignment_role,
        "task",
        command.task_id,
        capabilities(command.required_capabilities),
        command.at,
    )
    hidden_input = json.loads(hidden_dispatch["input_json"]) if hidden_dispatch is not None else {}
    hidden_signals = hidden_input.get("signals", {}) if isinstance(hidden_input, dict) else {}
    hidden_contract = hidden_input.get("hidden_scientist", {}) if isinstance(hidden_input, dict) else {}
    store.connection.execute(
        """
        INSERT INTO tasks
          (task_id,project_id,summary,state,version,current_owner_agent_id,
           current_owner_squad_id,updated_at,request_id,coordinator_agent_id,
           champion_agent_id,result_summary)
        VALUES(?,?,?,'pending',1,?,NULL,?,?,?,?,NULL)
        """,
        (
            command.task_id,
            project_id,
            command.task_summary,
            command.champion_agent_id,
            command.at,
            command.request_id,
            command.coordinator_agent_id,
            command.champion_agent_id,
        ),
    )
    store.connection.execute(
        """
        UPDATE agent_instances
           SET shotcaller_agent_id=?,task_id=?,repository=?,issue=?,branch=?,worktree=?,
               next_action='Await verified launch receipt'
         WHERE agent_id=?
        """,
        (
            command.coordinator_agent_id,
            command.task_id,
            command.repository or None,
            command.issue or None,
            command.branch or None,
            command.worktree or None,
            command.champion_agent_id,
        ),
    )
    store.connection.execute(
        """
        INSERT INTO task_assignments
          (task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
           runtime_instance_id,callsign,assignment_role,dispatch_id,bounded_subtask,model,
           effort,routing_reason_code,time_budget_minutes,scope_budget_actions,state,
           acceptance_receipt_json,failure_class,cleanup_required,promoted_from_assignment_id,
           version,created_at,updated_at)
        VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,0,?,1,?,?)
        """,
        (
            command.assignment_id,
            command.task_id,
            command.request_id,
            command.coordinator_agent_id,
            command.champion_agent_id,
            selected["callsign"],
            command.assignment_role,
            command.dispatch_id,
            hidden_contract.get("hidden_subtask"),
            hidden_dispatch["requested_model"] if hidden_dispatch is not None else None,
            hidden_dispatch["requested_effort"] if hidden_dispatch is not None else None,
            hidden_dispatch["reason_code"] if hidden_dispatch is not None else None,
            hidden_signals.get("expected_minutes"),
            hidden_signals.get("expected_task_action_calls"),
            command.promoted_from_assignment_id,
            command.at,
            command.at,
        ),
    )
    if command.promoted_from_assignment_id is not None:
        store.connection.execute(
            """
            UPDATE task_assignments SET promoted_to_assignment_id=?,updated_at=?
             WHERE task_assignment_id=?
            """,
            (command.assignment_id, command.at, command.promoted_from_assignment_id),
        )
    if command.issue_receipt is not None:
        _insert_issue_binding(store, command)
    return str(selected["callsign"])


def _insert_issue_binding(store: Any, command: PrepareAssignmentCommand) -> None:
    receipt = validate_issue_receipt(command.issue_receipt or {})
    expected_scope = issue_scope_digest(
        command.repository, command.issue, command.task_id, command.task_summary
    )
    exact = (
        receipt["repository_key"] == canonical_repository(command.repository)[1]
        and int(receipt["issue"]) == command.issue
        and receipt["task_scope_digest"] == expected_scope
    )
    if not exact:
        raise StorageRefusal(
            "issue_scope_mismatch",
            "verified repository issue does not match the canonical task scope",
        )
    selection = store.connection.execute(
        """
        SELECT * FROM repository_issue_selection_receipts WHERE receipt_digest=?
        """,
        (receipt["issue_selection_receipt_digest"],),
    ).fetchone()
    selection_exact = selection is not None and (
        selection["task_id"] == command.task_id
        and selection["task_summary"] == command.task_summary
        and normalize_issue_title(selection["issue_title"])
        == normalize_issue_title(command.task_summary)
        and selection["coordinator_agent_id"] == command.coordinator_agent_id
        and selection["repository"] == receipt["repository"]
        and selection["repository_key"] == canonical_repository(command.repository)[1]
        and int(selection["issue"]) == command.issue
        and selection["issue_url"] == receipt["issue_url"]
        and selection["issue_state"] == "open"
        and selection["issue_title"] == receipt["issue_title"]
        and selection["normalized_title"] == receipt["normalized_title"]
        and selection["semantic_scope_digest"] == receipt["semantic_scope_digest"]
        and selection["issue_body_digest"] == receipt["issue_body_digest"]
        and selection["task_scope_digest"] == receipt["task_scope_digest"]
    )
    if not selection_exact:
        raise StorageRefusal(
            "issue_selection_unproven",
            "assignment has no exact durable duplicate-preflight selection receipt",
        )
    assert selection is not None
    semantic_binding = task_issue_semantic_binding_digest(
        command.repository,
        command.issue,
        command.task_id,
        command.task_summary,
        receipt["issue_title"],
        receipt["semantic_scope_digest"],
    )
    if (
        receipt["verifier_kind"] == "synthetic-fixture"
        and not receipt["repository_key"].partition("/")[0].endswith(".invalid")
    ):
        raise StorageRefusal(
            "issue_selection_unproven",
            "synthetic issue evidence cannot bind a live repository",
        )
    reopen_digest = selection["reopen_action_receipt_digest"]
    store.connection.execute(
        """
        INSERT INTO repository_issue_bindings
          (task_id,assignment_id,request_id,repository,issue,issue_url,issue_state,
           issue_title,issue_body_digest,semantic_binding_digest,task_scope_digest,
           issue_selection_receipt_digest,
           reopen_action_receipt_digest,verifier_kind,verified_at,receipt_digest)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            command.task_id,
            command.assignment_id,
            command.request_id,
            command.repository,
            command.issue,
            receipt["issue_url"],
            receipt["issue_state"],
            receipt["issue_title"],
            receipt["issue_body_digest"],
            semantic_binding,
            receipt["task_scope_digest"],
            receipt["issue_selection_receipt_digest"],
            reopen_digest,
            receipt["verifier_kind"],
            receipt["verified_at"],
            receipt["receipt_digest"],
        ),
    )


def _insert_pending_assignment_event(
    store: Any, command: PrepareAssignmentCommand, callsign: str
) -> None:
    store.connection.execute(
        """
        INSERT INTO events
          (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,
           detail_json,request_id,aggregate_kind,aggregate_id)
        VALUES(?,NULL,?,1,'assignment_pending','pending',?, ?,
               ?,?,'assignment',?)
        """,
        (
            f"assignment:{command.assignment_id}:1",
            command.task_id,
            (
                "Hidden scientist assignment reserved"
                if command.assignment_role == "hidden-worker"
                else "Champion assignment reserved"
            ),
            command.at,
            _json(
                {
                    "assignment_id": command.assignment_id,
                    "assignment_role": command.assignment_role,
                    "callsign": callsign,
                }
            ),
            command.request_id,
            command.assignment_id,
        ),
    )


def prepare_assignment(
    store: Any,
    command: PrepareAssignmentCommand,
) -> dict[str, Any]:
    _validate_assignment_command(command)
    try:
        with store._transaction():
            retry = _assignment_retry(store, command)
            if retry is not None:
                return retry
            project_id, hidden_dispatch = _validate_assignment_reservation(store, command)
            callsign = _persist_assignment_reservation(
                store, command, project_id, hidden_dispatch
            )
            _insert_pending_assignment_event(store, command, callsign)
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "assignment reservation conflicted with canonical state") from exc
    return {
        "assignment_id": command.assignment_id,
        "task_id": command.task_id,
        "state": "pending",
        "version": 1,
        "callsign": callsign,
        "assignment_role": command.assignment_role,
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
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?", (assignment_id,)
            ).fetchone()
            if assignment is None:
                raise StorageRefusal("assignment_unknown", "assignment does not exist")
            common_required = {
                "verified",
                "assignment_id",
                "task_id",
                "callsign",
                "runtime_instance_id",
                "thread_id",
                "endpoint",
                "runtime_generation",
                "harness_kind",
                "backend_kind",
                "routing_name",
                "display_agent",
                "capabilities",
            }
            champion_required = common_required | {
                "champion_agent_id",
                "repository",
                "issue",
                "branch",
                "worktree",
            }
            hidden_required = common_required | {
                "hidden_worker_agent_id",
                "bounded_subtask",
                "model",
                "effort",
                "routing_reason_code",
                "time_budget_minutes",
                "scope_budget_actions",
            }
            required = (
                hidden_required
                if assignment["assignment_role"] == "hidden-worker"
                else champion_required
            )
            if set(receipt) != required or receipt.get("verified") is not True:
                raise StorageRefusal(
                    "receipt_unverified", "assignment activation requires one exact role-specific receipt"
                )
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
            callsign_assignment = store.connection.execute(
                "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                (f"callsign-assignment:{assignment_id}",),
            ).fetchone()
            if callsign_assignment is None:
                raise StorageRefusal(
                    "assignment_incomplete", "assignment has no durable callsign reservation"
                )
            declared_capabilities = capabilities(receipt["capabilities"])
            required_capabilities = tuple(
                json.loads(callsign_assignment["requirements_json"])
            )
            assignee_key = (
                "hidden_worker_agent_id"
                if assignment["assignment_role"] == "hidden-worker"
                else "champion_agent_id"
            )
            role_exact = (
                receipt["bounded_subtask"] == assignment["bounded_subtask"]
                and receipt["model"] == assignment["model"]
                and receipt["effort"] == assignment["effort"]
                and receipt["routing_reason_code"] == assignment["routing_reason_code"]
                and receipt["time_budget_minutes"] == assignment["time_budget_minutes"]
                and receipt["scope_budget_actions"] == assignment["scope_budget_actions"]
            ) if assignment["assignment_role"] == "hidden-worker" else (
                receipt["repository"] == agent["repository"]
                and receipt["issue"] == agent["issue"]
                and receipt["branch"] == agent["branch"]
                and receipt["worktree"] == agent["worktree"]
            )
            exact = (
                receipt["assignment_id"] == assignment_id
                and receipt["task_id"] == assignment["task_id"]
                and receipt[assignee_key] == assignment["champion_agent_id"]
                and receipt["callsign"] == assignment["callsign"]
                and agent["role"] == assignment["assignment_role"]
                and role_exact
                and receipt["routing_name"] == str(assignment["callsign"]).lower()
                and receipt["backend_kind"] in {"herdr", "tmux"}
                and all(
                    isinstance(receipt[name], str) and receipt[name]
                    for name in required
                    - {"verified", "issue", "capabilities", "time_budget_minutes", "scope_budget_actions"}
                )
                and all(item in declared_capabilities for item in required_capabilities)
                and callsign_assignment["state"] == "reserved"
            )
            if not exact:
                raise StorageRefusal(
                    "receipt_mismatch", "launch receipt does not match the reserved role identity"
                )
            runtime_conflict = store.connection.execute(
                """
                SELECT 1 FROM runtime_instances
                 WHERE runtime_instance_id=? OR (harness_kind=? AND session_ref=?)
                """,
                (
                    receipt["runtime_instance_id"],
                    receipt["harness_kind"],
                    receipt["thread_id"],
                ),
            ).fetchone()
            if runtime_conflict is not None:
                raise StorageRefusal("runtime_conflict", "launch receipt runtime identity is already registered")
            agent_version = int(agent["version"]) + 1
            store.connection.execute(
                """
                INSERT INTO runtime_instances
                  (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,endpoint,
                   runtime_generation,status,verified,last_seen_at,capabilities_json)
                VALUES(?,?,?,?,?,?,?,'active',1,?,?)
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
                    stable_json(declared_capabilities),
                ),
            )
            store.connection.execute(
                """
                UPDATE agent_instances
                   SET shotcaller_agent_id=?,task_id=?,kind=?,address=?,thread_id=?,backend=?,routing_name=?,display_agent=?,
                       status='working',version=?,updated_at=?,update_text='assignment accepted',
                       next_action=?
                 WHERE agent_id=?
                """,
                (
                    assignment["coordinator_agent_id"],
                    assignment["task_id"],
                    receipt["harness_kind"],
                    receipt["endpoint"],
                    receipt["thread_id"],
                    receipt["backend_kind"],
                    receipt["routing_name"],
                    receipt["display_agent"],
                    agent_version,
                    at,
                    (
                        "Perform only the bounded hidden scientist subtask"
                        if assignment["assignment_role"] == "hidden-worker"
                        else "Perform the assigned task"
                    ),
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
            queue_meta = store.connection.execute(
                "SELECT queue_version FROM callsign_queue_meta WHERE pool_role=?",
                (assignment["assignment_role"],),
            ).fetchone()
            queue_version = int(queue_meta["queue_version"]) + 1
            receipt_digest = hashlib.sha256(_json(receipt).encode("utf-8")).hexdigest()
            queue_changed = store.connection.execute(
                """
                UPDATE callsign_queue SET state='active',queue_position=NULL,
                       reservation_assignment_id=NULL,version=version+1,updated_at=?
                 WHERE callsign=? AND state='reserved'
                   AND reservation_assignment_id=?
                """,
                (at, assignment["callsign"], f"callsign-assignment:{assignment_id}"),
            )
            if queue_changed.rowcount != 1:
                raise StorageRefusal(
                    "queue_conflict", "assignment callsign queue reservation is not exact"
                )
            store.connection.execute(
                "UPDATE callsign_queue_meta SET queue_version=? WHERE pool_role=?",
                (queue_version, assignment["assignment_role"]),
            )
            store.connection.execute(
                """
                UPDATE callsign_assignments SET state='active',acceptance_digest=?,
                       queue_version=?,version=version+1,activated_at=?
                 WHERE callsign_assignment_id=?
                """,
                (
                    receipt_digest,
                    queue_version,
                    at,
                    f"callsign-assignment:{assignment_id}",
                ),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
                   update_text,occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?, ?,NULL,NULL,?,'callsign_activated','active',
                       'callsign activated',?,?,'agent',?)
                """,
                (
                    f"callsign:callsign-assignment:{assignment_id}:active",
                    assignment["champion_agent_id"],
                    agent_version,
                    at,
                    _json(
                        {
                            "assignment_id": f"callsign-assignment:{assignment_id}",
                            "acceptance_digest": receipt_digest,
                            "queue_version": queue_version,
                        }
                    ),
                    assignment["champion_agent_id"],
                ),
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
                VALUES(?,NULL,?,?,'assignment_active','active',?, ?,
                       ?,?,'assignment',?)
                """,
                (
                    event_id,
                    assignment["task_id"],
                    next_version,
                    (
                        "verified hidden scientist accepted assignment"
                        if assignment["assignment_role"] == "hidden-worker"
                        else "verified Champion accepted assignment"
                    ),
                    at,
                    _json(
                        {
                            "assignment_id": assignment_id,
                            "assignment_role": assignment["assignment_role"],
                            "runtime_instance_id": receipt["runtime_instance_id"],
                        }
                    ),
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


def finish_hidden_assignment(
    store: Any, command: FinishHiddenAssignmentCommand
) -> dict[str, Any]:
    """Deliver one terminal hidden-scientist result after exact cleanup."""

    _time(command.at, "hidden scientist terminal time")
    if command.status not in {"completed", "blocked", "failed", "promotion_required"}:
        raise StorageRefusal(
            "hidden_terminal_invalid", "hidden scientist delivery must be terminal"
        )
    result = _bounded_public_text(command.result_summary, "hidden result", maximum=1024)
    digest_pattern = re.compile(r"^[0-9a-f]{64}$")
    if (
        not all((command.assignment_id, command.runtime_instance_id, command.transition_id,
                 command.transition_key, command.event_id, command.outbox_id))
        or not digest_pattern.fullmatch(command.cleanup_receipt)
        or not digest_pattern.fullmatch(command.unpublished_state_receipt)
    ):
        raise StorageRefusal(
            "hidden_terminal_receipt_invalid",
            "hidden scientist terminal delivery requires exact cleanup receipt digests",
        )
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?",
                (command.assignment_id,),
            ).fetchone()
            if assignment is None or assignment["assignment_role"] != "hidden-worker":
                raise StorageRefusal(
                    "hidden_assignment_unknown", "assignment is not a hidden scientist"
                )
            if assignment["state"] == command.status:
                outbox = store.connection.execute(
                    "SELECT outbox_id FROM delivery_outbox WHERE event_id=?",
                    (command.event_id,),
                ).fetchone()
                exact = (
                    int(assignment["version"]) == command.expected_version + 1
                    and assignment["runtime_instance_id"] == command.runtime_instance_id
                    and assignment["result_summary"] == result
                    and assignment["cleanup_receipt"] == command.cleanup_receipt
                    and assignment["unpublished_state_receipt"] == command.unpublished_state_receipt
                    and assignment["terminal_event_id"] == command.event_id
                    and outbox is not None
                    and outbox["outbox_id"] == command.outbox_id
                )
                if not exact:
                    raise StorageRefusal(
                        "hidden_terminal_conflict", "hidden terminal retry changed evidence"
                    )
                return {
                    "assignment_id": command.assignment_id,
                    "state": command.status,
                    "version": int(assignment["version"]),
                    "event_id": command.event_id,
                    "outbox_id": command.outbox_id,
                    "idempotent": True,
                }
            if assignment["state"] not in {"active", "cleanup_pending"} or int(assignment["version"]) != command.expected_version:
                raise StorageRefusal(
                    "hidden_terminal_conflict",
                    "hidden scientist is not active or cleanup-pending at the expected version",
                )
            if assignment["runtime_instance_id"] != command.runtime_instance_id:
                raise StorageRefusal("runtime_mismatch", "hidden terminal runtime is not exact")
            released = store.connection.execute(
                """
                SELECT * FROM callsign_assignments
                 WHERE callsign_assignment_id=? AND role='hidden-worker'
                """,
                (f"callsign-assignment:{command.assignment_id}",),
            ).fetchone()
            if (
                released is None
                or released["state"] != "released"
                or released["release_receipt_digest"] != command.cleanup_receipt
            ):
                raise StorageRefusal(
                    "hidden_cleanup_unproven",
                    "hidden scientist runtime and callsign must be released before terminal delivery",
                )
            if command.status == "promotion_required":
                promoted = store.connection.execute(
                    """
                    SELECT 1 FROM task_assignments
                     WHERE task_assignment_id=? AND request_id=?
                       AND assignment_role='champion' AND state IN ('pending','launching','active')
                    """,
                    (assignment["promoted_to_assignment_id"], assignment["request_id"]),
                ).fetchone()
                if promoted is None:
                    raise StorageRefusal(
                        "champion_assignment_required",
                        "promotion requires a separate durable visible Champion assignment",
                    )
            task = store.connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (assignment["task_id"],)
            ).fetchone()
            if task is None:
                raise StorageRefusal("assignment_incomplete", "hidden assignment task is missing")
            next_task_version = int(task["version"]) + 1
            next_assignment_version = command.expected_version + 1
            store.connection.execute(
                """
                UPDATE tasks SET state=?,result_summary=?,version=?,updated_at=? WHERE task_id=?
                """,
                (command.status, result, next_task_version, command.at, assignment["task_id"]),
            )
            store.connection.execute(
                """
                INSERT INTO task_transitions
                  (transition_id,transition_key,task_id,from_state,to_state,update_text,
                   next_action,blocker,created_at,event_id)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    command.transition_id,
                    command.transition_key,
                    assignment["task_id"],
                    task["state"],
                    command.status,
                    result,
                    (
                        "Continue with the new visible Champion assignment"
                        if command.status == "promotion_required"
                        else "Reconcile the parent request"
                    ),
                    result if command.status in {"blocked", "failed"} else None,
                    command.at,
                    command.event_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,request_id,aggregate_kind,aggregate_id)
                VALUES(?,NULL,?,?,'hidden_scientist_terminal',?,?,?, ?,?,'assignment',?)
                """,
                (
                    command.event_id,
                    assignment["task_id"],
                    next_assignment_version,
                    command.status,
                    result,
                    command.at,
                    _json(
                        {
                            "assignment_id": command.assignment_id,
                            "promoted_to_assignment_id": assignment["promoted_to_assignment_id"],
                        }
                    ),
                    assignment["request_id"],
                    command.assignment_id,
                ),
            )
            store.connection.execute(
                """
                UPDATE cleanup_obligations
                   SET cleanup_state='cleanup_completed',next_action='None',
                       version=version+1,updated_at=?
                 WHERE task_id=? AND cleanup_state IN ('pending','cleanup_pending')
                """,
                (command.at, assignment["task_id"]),
            )
            store.connection.execute(
                """
                UPDATE task_assignments
                   SET state=?,result_summary=?,cleanup_receipt=?,unpublished_state_receipt=?,
                       terminal_event_id=?,version=?,updated_at=?
                 WHERE task_assignment_id=?
                """,
                (
                    command.status,
                    result,
                    command.cleanup_receipt,
                    command.unpublished_state_receipt,
                    command.event_id,
                    next_assignment_version,
                    command.at,
                    command.assignment_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO delivery_outbox
                  (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                VALUES(?,?,?,'pending',?,0)
                """,
                (
                    command.outbox_id,
                    command.event_id,
                    assignment["coordinator_agent_id"],
                    command.at,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "hidden scientist terminal delivery conflicted with canonical state"
        ) from exc
    return {
        "assignment_id": command.assignment_id,
        "state": command.status,
        "version": next_assignment_version,
        "event_id": command.event_id,
        "outbox_id": command.outbox_id,
        "idempotent": False,
    }


def reconcile_assignment_runtime(
    store: Any, assignment_id: str, at: str
) -> dict[str, Any]:
    """Fence one stale active runtime without inventing worker progress."""

    _time(at, "assignment runtime reconciliation time")
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise StorageRefusal("assignment_unknown", "assignment does not exist")
            if assignment["state"] == "cleanup_pending" and assignment["failure_class"] == "stale_runtime":
                return {
                    "assignment_id": assignment_id,
                    "assignment_role": assignment["assignment_role"],
                    "state": "cleanup_pending",
                    "version": int(assignment["version"]),
                    "runtime_status": "stale",
                    "idempotent": True,
                }
            if assignment["state"] != "active":
                return {
                    "assignment_id": assignment_id,
                    "assignment_role": assignment["assignment_role"],
                    "state": assignment["state"],
                    "version": int(assignment["version"]),
                    "runtime_status": "not_active",
                    "idempotent": True,
                }
            runtime = store.connection.execute(
                "SELECT actor_agent_id,status,verified FROM runtime_instances WHERE runtime_instance_id=?",
                (assignment["runtime_instance_id"],),
            ).fetchone()
            if (
                runtime is not None
                and runtime["actor_agent_id"] == assignment["champion_agent_id"]
                and runtime["status"] in {"active", "idle"}
                and bool(runtime["verified"])
            ):
                return {
                    "assignment_id": assignment_id,
                    "assignment_role": assignment["assignment_role"],
                    "state": "active",
                    "version": int(assignment["version"]),
                    "runtime_status": "live",
                    "idempotent": True,
                }
            next_version = int(assignment["version"]) + 1
            store.connection.execute(
                """
                UPDATE task_assignments
                   SET state='cleanup_pending',failure_class='stale_runtime',cleanup_required=1,
                       version=?,updated_at=?
                 WHERE task_assignment_id=?
                """,
                (next_version, at, assignment_id),
            )
            task = store.connection.execute(
                "SELECT state FROM tasks WHERE task_id=?", (assignment["task_id"],)
            ).fetchone()
            if task is None:
                raise StorageRefusal("task_unknown", "assignment task does not exist")
            if task["state"] not in TASK_TERMINAL_STATES:
                store.connection.execute(
                    "UPDATE tasks SET state='blocked',version=version+1,updated_at=? WHERE task_id=?",
                    (at, assignment["task_id"]),
                )
            store.connection.execute(
                """
                INSERT INTO cleanup_obligations
                  (cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,version,updated_at)
                VALUES(?,?,'pending','stale_assignment_runtime',
                       'Verify and clean only the exact stale assignment runtime',1,?)
                ON CONFLICT(task_id) DO UPDATE SET
                  cleanup_state='pending',required_policy='stale_assignment_runtime',
                  next_action='Verify and clean only the exact stale assignment runtime',
                  version=cleanup_obligations.version+1,updated_at=excluded.updated_at
                """,
                (f"cleanup:{assignment['task_id']}", assignment["task_id"], at),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "assignment runtime reconciliation conflicted with canonical state"
        ) from exc
    return {
        "assignment_id": assignment_id,
        "assignment_role": assignment["assignment_role"],
        "state": "cleanup_pending",
        "version": next_version,
        "runtime_status": "stale",
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
                callsign_assignment = store.connection.execute(
                    "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                    (f"callsign-assignment:{assignment_id}",),
                ).fetchone()
                if callsign_assignment is None:
                    raise StorageRefusal(
                        "assignment_incomplete", "assignment has no callsign reservation"
                    )
                failure_digest = hashlib.sha256(
                    stable_json(
                        {
                            "assignment_id": assignment_id,
                            "failure_class": failure_class,
                            "cleanup_proven": cleanup_proven,
                        }
                    ).encode("utf-8")
                ).hexdigest()
                _rollback_reserved_in_transaction(
                    store,
                    callsign_assignment,
                    int(callsign_assignment["version"]),
                    failure_digest,
                    at,
                )
                if assignment["assignment_role"] == "hidden-worker":
                    event_id = f"assignment:{assignment_id}:{next_version}:blocked"
                    outbox_id = f"outbox:{assignment_id}:{next_version}:blocked"
                    store.connection.execute(
                        """
                        UPDATE task_assignments
                           SET result_summary=?,cleanup_receipt=?,unpublished_state_receipt=?,
                               terminal_event_id=? WHERE task_assignment_id=?
                        """,
                        (failure_class, failure_digest, failure_digest, event_id, assignment_id),
                    )
                    store.connection.execute(
                        """
                        INSERT INTO events
                          (event_id,agent_id,task_id,entity_version,event_type,status,update_text,
                           occurred_at,detail_json,request_id,aggregate_kind,aggregate_id)
                        VALUES(?,NULL,?,?,'hidden_scientist_terminal','blocked',?,?,'{}',?,
                               'assignment',?)
                        """,
                        (
                            event_id,
                            assignment["task_id"],
                            next_version,
                            failure_class,
                            at,
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
                        (outbox_id, event_id, assignment["coordinator_agent_id"], at),
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
    return {
        "assignment_id": assignment_id,
        "state": state,
        "version": next_version,
        "failure_class": failure_class,
        "cleanup_required": cleanup_required,
        "cleanup_proven": cleanup_proven,
    }


def assignment_launch_context(store: Any, assignment_id: str) -> dict[str, Any]:
    assignment = store.connection.execute(
        """
        SELECT task_assignment_id,state,version,runtime_instance_id,callsign,
               acceptance_receipt_json,failure_class
          FROM task_assignments WHERE task_assignment_id=?
        """,
        (assignment_id,),
    ).fetchone()
    if assignment is None:
        raise StorageRefusal("assignment_unknown", "assignment does not exist")
    delivery = store.connection.execute(
        """
        SELECT event_id,occurred_at,detail_json
          FROM events
         WHERE aggregate_kind='assignment' AND aggregate_id=?
           AND event_type='assignment_context_delivered'
         ORDER BY occurred_at,event_id LIMIT 2
        """,
        (assignment_id,),
    ).fetchall()
    revalidation = store.connection.execute(
        """
        SELECT event_id,occurred_at,detail_json
          FROM events
         WHERE aggregate_kind='assignment' AND aggregate_id=?
           AND event_type='assignment_title_revalidated'
         ORDER BY event_seq DESC LIMIT 1
        """,
        (assignment_id,),
    ).fetchone()
    legacy_intents = store.connection.execute(
        """
        SELECT event_id,occurred_at,detail_json FROM events
         WHERE aggregate_kind='assignment' AND aggregate_id=?
           AND event_type='assignment_legacy_display_reconciliation_intent'
         ORDER BY event_seq LIMIT 2
        """,
        (assignment_id,),
    ).fetchall()
    legacy_results = store.connection.execute(
        """
        SELECT event_id,occurred_at,detail_json FROM events
         WHERE aggregate_kind='assignment' AND aggregate_id=?
           AND event_type='assignment_legacy_display_reconciled'
         ORDER BY event_seq LIMIT 2
        """,
        (assignment_id,),
    ).fetchall()
    if (
        len(legacy_intents) > 1
        or len(legacy_results) > 1
        or (legacy_results and not legacy_intents)
    ):
        raise StorageRefusal(
            "legacy_display_ambiguous",
            "assignment has ambiguous legacy display reconciliation history",
        )
    if len(delivery) > 1:
        raise StorageRefusal(
            "assignment_context_ambiguous",
            "assignment has more than one bounded context receipt",
        )
    receipt = (
        json.loads(assignment["acceptance_receipt_json"])
        if assignment["acceptance_receipt_json"] is not None
        else None
    )
    delivered = None
    if delivery:
        detail = json.loads(delivery[0]["detail_json"])
        delivered = {
            "event_id": delivery[0]["event_id"],
            "at": delivery[0]["occurred_at"],
            **detail,
        }
        if revalidation is not None:
            revalidated = json.loads(revalidation["detail_json"])
            if set(revalidated) != {"display_receipt"} or not isinstance(
                revalidated["display_receipt"], dict
            ):
                raise StorageRefusal(
                    "assignment_context_ambiguous",
                    "assignment title revalidation receipt is malformed",
                )
            delivered["display_receipt"] = dict(
                revalidated["display_receipt"]
            )
    legacy_reconciliation = None
    if legacy_intents:
        legacy_intent = _stored_object(
            legacy_intents[0]["detail_json"],
            "legacy_display_ambiguous",
            "legacy display reconciliation history is malformed",
        )
        legacy_receipt = (
            _legacy_result_receipt(legacy_results[0]) if legacy_results else None
        )
        if legacy_receipt is not None:
            result_detail = _stored_object(
                legacy_results[0]["detail_json"],
                "legacy_display_ambiguous",
                "legacy display reconciliation history is malformed",
            )
            intent_digest = hashlib.sha256(
                _json(legacy_intent).encode("utf-8")
            ).hexdigest()
            reconciliation_id = f"legacy-display:{intent_digest[:24]}"
            expected_source = f"league-legacy-{intent_digest[:24]}"
            expected_sequence = legacy_intent.get("expected_state_change_seq")
            intent_keys = {
                "schema",
                "assignment_id",
                "expected_version",
                "champion_agent_id",
                "runtime_instance_id",
                "callsign",
                "pane_id",
                "terminal_id",
                "thread_id",
                "worktree",
                "routing_name",
                "expected_presentation_source",
                "expected_title",
                "expected_state_change_seq",
                "target_task_label",
                "target_title",
                "owner_authorized",
            }
            exact_result = bool(
                set(legacy_intent) == intent_keys
                and legacy_intent.get("schema")
                == "league.legacy-display-reconciliation-intent.v1"
                and legacy_intent.get("owner_authorized") is True
                and type(legacy_intent.get("expected_version")) is int
                and legacy_intent["expected_version"] >= 1
                and type(expected_sequence) is int
                and expected_sequence >= 0
                and result_detail["intent_digest"] == intent_digest
                and legacy_receipt["reconciliation_id"] == reconciliation_id
                and legacy_receipt["assignment_id"]
                == legacy_intent.get("assignment_id")
                and legacy_receipt["champion_agent_id"]
                == legacy_intent.get("champion_agent_id")
                and legacy_receipt["runtime_instance_id"]
                == legacy_intent.get("runtime_instance_id")
                and legacy_receipt["source"] == expected_source
                and legacy_receipt["sidebar_name"] == legacy_intent.get("callsign")
                and legacy_receipt["task_label"]
                == legacy_intent.get("target_task_label")
                and legacy_receipt["thread_title"]
                == legacy_intent.get("target_title")
                and legacy_receipt["terminal_title"]
                == legacy_intent.get("target_title")
                and legacy_receipt["state_change_seq"] == expected_sequence + 1
            )
            if not exact_result:
                raise StorageRefusal(
                    "legacy_display_ambiguous",
                    "legacy display reconciliation result does not bind its exact intent",
                )
        legacy_reconciliation = {
            "intent": legacy_intent,
            "receipt": legacy_receipt,
        }
    return {
        "assignment_id": assignment_id,
        "state": assignment["state"],
        "version": int(assignment["version"]),
        "runtime_instance_id": assignment["runtime_instance_id"],
        "callsign": assignment["callsign"],
        "failure_class": assignment["failure_class"],
        "acceptance_receipt": receipt,
        "context_delivery": delivered,
        "legacy_display_reconciliation": legacy_reconciliation,
    }


def _legacy_display_detail(
    command: LegacyDisplayReconciliationCommand,
) -> dict[str, Any]:
    return {
        "schema": "league.legacy-display-reconciliation-intent.v1",
        "assignment_id": command.assignment_id,
        "expected_version": command.expected_version,
        "champion_agent_id": command.champion_agent_id,
        "runtime_instance_id": command.runtime_instance_id,
        "callsign": command.callsign,
        "pane_id": command.pane_id,
        "terminal_id": command.terminal_id,
        "thread_id": command.thread_id,
        "worktree": command.worktree,
        "routing_name": command.routing_name,
        "expected_presentation_source": command.expected_presentation_source,
        "expected_title": command.expected_title,
        "expected_state_change_seq": command.expected_state_change_seq,
        "target_task_label": command.target_task_label,
        "target_title": f"{command.callsign} · {command.target_task_label}",
        "owner_authorized": command.owner_authorized,
    }


def _validate_legacy_display_command(
    store: Any, command: LegacyDisplayReconciliationCommand
) -> tuple[Any, dict[str, Any], str, dict[str, Any]]:
    _time(command.at, "legacy display reconciliation time")
    tuple_supplied = all(
        (
            isinstance(command.expected_presentation_source, str)
            and bool(command.expected_presentation_source),
            isinstance(command.expected_title, str) and bool(command.expected_title),
            isinstance(command.expected_state_change_seq, int)
            and not isinstance(command.expected_state_change_seq, bool)
            and command.expected_state_change_seq >= 0,
        )
    )
    identity = (
        command.assignment_id,
        command.champion_agent_id,
        command.runtime_instance_id,
        command.callsign,
        command.pane_id,
        command.terminal_id,
        command.thread_id,
        command.worktree,
        command.routing_name,
    )
    if (
        command.owner_authorized is not True
        or not all(identity)
        or not _physical_worktree_exact(command.worktree, command.worktree)
        or command.expected_version < 1
        or not tuple_supplied
        or len(command.target_task_label.split()) != 2
        or " ".join(command.target_task_label.split()) != command.target_task_label
        or len(command.target_task_label) > 48
        or command.routing_name != command.callsign.lower()
    ):
        raise StorageRefusal(
            "legacy_display_invalid",
            "legacy display reconciliation requires exact owner-authorized identity, observation, and two-word target",
        )
    assignment = store.connection.execute(
        "SELECT * FROM task_assignments WHERE task_assignment_id=?",
        (command.assignment_id,),
    ).fetchone()
    if (
        assignment is None
        or assignment["assignment_role"] != "champion"
        or assignment["state"] != "active"
        or int(assignment["version"]) != command.expected_version
        or assignment["champion_agent_id"] != command.champion_agent_id
        or assignment["runtime_instance_id"] != command.runtime_instance_id
        or assignment["callsign"] != command.callsign
    ):
        raise StorageRefusal(
            "legacy_display_conflict",
            "legacy display reconciliation requires the exact active Champion assignment",
        )
    agent = store.connection.execute(
        "SELECT * FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
        (command.champion_agent_id,),
    ).fetchone()
    runtime_rows = store.connection.execute(
        """
        SELECT runtime_instance_id,session_ref,endpoint,runtime_generation,verified
          FROM runtime_instances
         WHERE actor_agent_id=? AND status IN ('active','idle') LIMIT 2
        """,
        (command.champion_agent_id,),
    ).fetchall()
    receipt = (
        _stored_object(
            assignment["acceptance_receipt_json"],
            "legacy_display_ambiguous",
            "legacy display reconciliation acceptance receipt is malformed",
        )
        if assignment["acceptance_receipt_json"] is not None
        else None
    )
    expected_generation = "herdr:" + hashlib.sha256(
        f"{command.terminal_id}\0{command.thread_id}".encode("utf-8")
    ).hexdigest()[:24]
    runtime = runtime_rows[0] if len(runtime_rows) == 1 else None
    exact = bool(
        agent is not None
        and runtime is not None
        and isinstance(receipt, dict)
        and agent["role"] == "champion"
        and agent["callsign"] == command.callsign
        and agent["address"] == command.pane_id
        and agent["thread_id"] == command.thread_id
        and _physical_worktree_exact(agent["worktree"], command.worktree)
        and agent["routing_name"] == command.routing_name
        and agent["backend"] == "herdr"
        and runtime["runtime_instance_id"] == command.runtime_instance_id
        and runtime["session_ref"] == command.thread_id
        and runtime["endpoint"] == command.pane_id
        and runtime["runtime_generation"] == expected_generation
        and bool(runtime["verified"])
        and receipt.get("champion_agent_id") == command.champion_agent_id
        and receipt.get("runtime_instance_id") == command.runtime_instance_id
        and receipt.get("callsign") == command.callsign
        and receipt.get("endpoint") == command.pane_id
        and receipt.get("thread_id") == command.thread_id
        and _physical_worktree_exact(receipt.get("worktree"), command.worktree)
        and receipt.get("routing_name") == command.routing_name
        and receipt.get("runtime_generation") == expected_generation
        and receipt.get("backend_kind") == "herdr"
    )
    if not exact:
        raise StorageRefusal(
            "legacy_display_conflict",
            "legacy display reconciliation identity or route is ambiguous or mismatched",
        )
    contexts = store.connection.execute(
        """
        SELECT detail_json FROM events
         WHERE aggregate_kind='assignment' AND aggregate_id=?
           AND event_type='assignment_context_delivered'
         LIMIT 2
        """,
        (command.assignment_id,),
    ).fetchall()
    revalidations = store.connection.execute(
        """
        SELECT 1 FROM events
         WHERE aggregate_kind='assignment' AND aggregate_id=?
           AND event_type='assignment_title_revalidated'
         LIMIT 1
        """,
        (command.assignment_id,),
    ).fetchall()
    if len(contexts) != 1:
        raise StorageRefusal(
            "legacy_display_ambiguous",
            "legacy display reconciliation requires one exact context history",
        )
    context_detail = _stored_object(
        contexts[0]["detail_json"],
        "legacy_display_ambiguous",
        "legacy display reconciliation context history is malformed",
    )
    if revalidations:
        raise StorageRefusal(
            "legacy_display_modern",
            "modern launch-title ownership receipts must use normal exact retry",
        )
    if "display_receipt" in context_detail:
        modern = context_detail["display_receipt"]
        if (
            isinstance(modern, dict)
            and isinstance(modern.get("source"), str)
            and modern["source"].startswith("league-launch-")
            and type(modern.get("state_change_seq")) is int
            and modern["state_change_seq"] >= 0
        ):
            raise StorageRefusal(
                "legacy_display_modern",
                "modern launch-title ownership receipts must use normal exact retry",
            )
        raise StorageRefusal(
            "legacy_display_ambiguous",
            "legacy display reconciliation context has malformed display ownership evidence",
        )
    detail = _legacy_display_detail(command)
    reconciliation_id = "legacy-display:" + hashlib.sha256(
        _json(detail).encode("utf-8")
    ).hexdigest()[:24]
    return assignment, detail, reconciliation_id, receipt


def begin_legacy_display_reconciliation(
    store: Any, command: LegacyDisplayReconciliationCommand
) -> dict[str, Any]:
    try:
        with store._transaction():
            assignment, detail, reconciliation_id, _ = _validate_legacy_display_command(
                store, command
            )
            intents = store.connection.execute(
                "SELECT event_id,detail_json FROM events WHERE aggregate_kind='assignment' AND aggregate_id=? AND event_type='assignment_legacy_display_reconciliation_intent' LIMIT 2",
                (command.assignment_id,),
            ).fetchall()
            results = store.connection.execute(
                "SELECT detail_json FROM events WHERE aggregate_kind='assignment' AND aggregate_id=? AND event_type='assignment_legacy_display_reconciled' LIMIT 2",
                (command.assignment_id,),
            ).fetchall()
            if len(results) > 1 or (results and not intents):
                raise StorageRefusal(
                    "legacy_display_ambiguous",
                    "legacy display reconciliation has orphaned or ambiguous final receipts",
                )
            if intents:
                if len(intents) != 1 or _stored_object(
                    intents[0]["detail_json"],
                    "legacy_display_ambiguous",
                    "legacy display reconciliation intent is malformed",
                ) != detail:
                    raise StorageRefusal(
                        "legacy_display_conflict",
                        "legacy display reconciliation retry changed its exact intent",
                    )
            else:
                store.connection.execute(
                    """
                    INSERT INTO events
                      (event_id,agent_id,task_id,entity_version,event_type,status,update_text,
                       occurred_at,detail_json,aggregate_kind,aggregate_id)
                    VALUES(?,?,?,?,'assignment_legacy_display_reconciliation_intent','active',
                           'owner-authorized legacy Champion display reconciliation intent',?,?,'assignment',?)
                    """,
                    (
                        f"event:{reconciliation_id}:intent",
                        None,
                        assignment["task_id"],
                        command.expected_version,
                        command.at,
                        _json(detail),
                        command.assignment_id,
                    ),
                )
            final_receipt = (
                _legacy_result_receipt(results[0]) if results else None
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "legacy display reconciliation intent conflicted with canonical state"
        ) from exc
    return {
        "assignment_id": command.assignment_id,
        "reconciliation_id": reconciliation_id,
        "state": "reconciled" if final_receipt is not None else "intent_recorded",
        "receipt": final_receipt,
        "idempotent": bool(intents),
    }


def finalize_legacy_display_reconciliation(
    store: Any,
    command: LegacyDisplayReconciliationCommand,
    receipt: dict[str, Any],
    at: str,
) -> dict[str, Any]:
    _time(at, "legacy display reconciliation final observation time")
    try:
        with store._transaction():
            assignment, detail, reconciliation_id, _ = _validate_legacy_display_command(
                store, command
            )
            intents = store.connection.execute(
                "SELECT detail_json FROM events WHERE aggregate_kind='assignment' AND aggregate_id=? AND event_type='assignment_legacy_display_reconciliation_intent' LIMIT 2",
                (command.assignment_id,),
            ).fetchall()
            if len(intents) != 1 or _stored_object(
                intents[0]["detail_json"],
                "legacy_display_ambiguous",
                "legacy display reconciliation intent is malformed",
            ) != detail:
                raise StorageRefusal(
                    "legacy_display_conflict",
                    "legacy display reconciliation has no exact durable intent",
                )
            expected_keys = {
                "schema",
                "reconciliation_id",
                "assignment_id",
                "champion_agent_id",
                "runtime_instance_id",
                "source",
                "applies_to_source",
                "state_change_seq",
                "sidebar_name",
                "task_label",
                "thread_title",
                "terminal_title",
                "observation_digest",
            }
            target = f"{command.callsign} · {command.target_task_label}"
            expected_source = f"league-legacy-{reconciliation_id.rsplit(':', 1)[-1]}"
            valid = bool(
                set(receipt) == expected_keys
                and receipt.get("schema") == "league.legacy-display-reconciliation.v1"
                and receipt.get("reconciliation_id") == reconciliation_id
                and receipt.get("assignment_id") == command.assignment_id
                and receipt.get("champion_agent_id") == command.champion_agent_id
                and receipt.get("runtime_instance_id") == command.runtime_instance_id
                and receipt.get("sidebar_name") == command.callsign
                and receipt.get("task_label") == command.target_task_label
                and receipt.get("thread_title") == target
                and receipt.get("terminal_title") == target
                and isinstance(receipt.get("source"), str)
                and bool(receipt.get("source"))
                and isinstance(receipt.get("applies_to_source"), str)
                and bool(receipt.get("applies_to_source"))
                and type(receipt.get("state_change_seq")) is int
                and int(receipt["state_change_seq"]) >= 0
                and isinstance(receipt.get("observation_digest"), str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", receipt["observation_digest"]))
                and receipt.get("source") == expected_source
                and int(receipt["state_change_seq"])
                == int(command.expected_state_change_seq) + 1
            )
            if not valid:
                raise StorageRefusal(
                    "legacy_display_unverified",
                    "legacy display reconciliation final observation is invalid",
                )
            final_detail = {
                "schema": "league.legacy-display-reconciliation-result.v1",
                "intent_digest": hashlib.sha256(_json(detail).encode("utf-8")).hexdigest(),
                "receipt": dict(receipt),
            }
            results = store.connection.execute(
                "SELECT event_id,detail_json FROM events WHERE aggregate_kind='assignment' AND aggregate_id=? AND event_type='assignment_legacy_display_reconciled' LIMIT 2",
                (command.assignment_id,),
            ).fetchall()
            if results:
                if len(results) != 1 or _stored_object(
                    results[0]["detail_json"],
                    "legacy_display_ambiguous",
                    "legacy display reconciliation result is malformed",
                ) != final_detail:
                    raise StorageRefusal(
                        "legacy_display_conflict",
                        "legacy display reconciliation final receipt conflicts with history",
                    )
            else:
                store.connection.execute(
                    """
                    INSERT INTO events
                      (event_id,agent_id,task_id,entity_version,event_type,status,update_text,
                       occurred_at,detail_json,aggregate_kind,aggregate_id)
                    VALUES(?,?,?,?,'assignment_legacy_display_reconciled','active',
                           'legacy Champion display reconciled with stable final observation',?,?,'assignment',?)
                    """,
                    (
                        f"event:{reconciliation_id}:final",
                        None,
                        assignment["task_id"],
                        command.expected_version,
                        at,
                        _json(final_detail),
                        command.assignment_id,
                    ),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "legacy display reconciliation final receipt conflicted with canonical state"
        ) from exc
    return {
        "assignment_id": command.assignment_id,
        "reconciliation_id": reconciliation_id,
        "state": "reconciled",
        "receipt": dict(receipt),
        "idempotent": bool(results),
    }


def _settle_assignment_activation_delivery(
    store: Any,
    assignment_id: str,
    effect_sha256: str,
    at: str,
) -> dict[str, str]:
    rows = store.connection.execute(
        """
        SELECT o.outbox_id,o.event_id,o.recipient_agent_id,o.state,
               r.effect_kind,r.effect_id
          FROM events e
          JOIN delivery_outbox o ON o.event_id=e.event_id
          LEFT JOIN recipient_receipts r
            ON r.event_id=o.event_id AND r.recipient_agent_id=o.recipient_agent_id
         WHERE e.aggregate_kind='assignment' AND e.aggregate_id=?
           AND e.event_type='assignment_active'
        """,
        (assignment_id,),
    ).fetchall()
    if len(rows) != 1:
        raise StorageRefusal(
            "assignment_incomplete",
            "assignment context has no exact activation delivery",
        )
    row = rows[0]
    if row["effect_kind"] is not None:
        if (
            row["effect_kind"] != "assignment_context"
            or row["effect_id"] != effect_sha256
        ):
            raise StorageRefusal(
                "assignment_context_conflict",
                "assignment activation already has a different recipient effect",
            )
        if row["state"] != "delivered":
            raise StorageRefusal(
                "assignment_context_conflict",
                "assignment activation receipt and outbox state disagree",
            )
    else:
        if row["state"] != "pending":
            raise StorageRefusal(
                "assignment_context_conflict",
                "assignment activation is not available for exact context delivery",
            )
        store.connection.execute(
            """
            INSERT INTO recipient_receipts
              (event_id,recipient_agent_id,received_at,effect_kind,effect_id)
            VALUES(?,?,?,'assignment_context',?)
            """,
            (row["event_id"], row["recipient_agent_id"], at, effect_sha256),
        )
        store.connection.execute(
            """
            UPDATE delivery_outbox
               SET state='delivered',last_outcome='assignment_context_delivered',delivered_at=?
             WHERE outbox_id=? AND state='pending'
            """,
            (at, row["outbox_id"]),
        )
        store.connection.execute(
            """
            UPDATE obligations SET state='satisfied',updated_at=?
             WHERE kind='delivery' AND aggregate_id=? AND state='open'
            """,
            (at, row["outbox_id"]),
        )
    return {"outbox_id": str(row["outbox_id"]), "delivery_state": "delivered"}


def record_assignment_context_delivery(
    store: Any,
    assignment_id: str,
    expected_version: int,
    context_sha256: str,
    byte_count: int,
    effect_sha256: str,
    display_receipt: dict[str, Any],
    event_id: str,
    at: str,
) -> dict[str, Any]:
    _time(at, "assignment context delivery time")
    digest_pattern = re.compile(r"^[0-9a-f]{64}$")
    display_keys = {
        "source",
        "applies_to_source",
        "state_change_seq",
        "sidebar_name",
        "task_label",
        "thread_title",
        "terminal_title",
    }
    if (
        not digest_pattern.fullmatch(context_sha256)
        or not digest_pattern.fullmatch(effect_sha256)
        or not event_id
        or byte_count < 1
        or byte_count > 4096
        or set(display_receipt) != display_keys
        or not all(
            isinstance(display_receipt[key], str) and display_receipt[key]
            for key in display_keys - {"state_change_seq"}
        )
        or not isinstance(display_receipt["state_change_seq"], int)
        or display_receipt["state_change_seq"] < 0
    ):
        raise StorageRefusal(
            "assignment_context_invalid",
            "bounded assignment context receipt is invalid",
        )
    detail = {
        "bytes": byte_count,
        "context_sha256": context_sha256,
        "effect_sha256": effect_sha256,
        "display_receipt": dict(display_receipt),
    }
    try:
        with store._transaction():
            existing = store.connection.execute(
                """
                SELECT event_id,detail_json FROM events
                 WHERE aggregate_kind='assignment' AND aggregate_id=?
                   AND event_type='assignment_context_delivered'
                """,
                (assignment_id,),
            ).fetchall()
            if existing:
                if len(existing) != 1 or json.loads(existing[0]["detail_json"]) != detail:
                    raise StorageRefusal(
                        "assignment_context_conflict",
                        "assignment context was already delivered with different bytes",
                    )
                activation = _settle_assignment_activation_delivery(
                    store, assignment_id, effect_sha256, at
                )
                return {
                    "assignment_id": assignment_id,
                    "state": "active",
                    "version": expected_version,
                    "event_id": existing[0]["event_id"],
                    "context_sha256": context_sha256,
                    "bytes": byte_count,
                    "effect_sha256": effect_sha256,
                    "display_receipt": dict(display_receipt),
                    **activation,
                    "idempotent": True,
                }
            assignment = store.connection.execute(
                "SELECT task_id,state,version FROM task_assignments WHERE task_assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if (
                assignment is None
                or assignment["state"] != "active"
                or int(assignment["version"]) != expected_version
            ):
                raise StorageRefusal(
                    "assignment_conflict",
                    "assignment context requires the exact active assignment version",
                )
            next_version = expected_version + 1
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,NULL,?,?,'assignment_context_delivered','active',
                       'bounded League context delivered',?,?,'assignment',?)
                """,
                (
                    event_id,
                    assignment["task_id"],
                    next_version,
                    at,
                    _json(detail),
                    assignment_id,
                ),
            )
            store.connection.execute(
                "UPDATE task_assignments SET version=?,updated_at=? WHERE task_assignment_id=?",
                (next_version, at, assignment_id),
            )
            activation = _settle_assignment_activation_delivery(
                store, assignment_id, effect_sha256, at
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "assignment context receipt conflicted with canonical state"
        ) from exc
    return {
        "assignment_id": assignment_id,
        "state": "active",
        "version": next_version,
        "event_id": event_id,
        "context_sha256": context_sha256,
        "bytes": byte_count,
        "effect_sha256": effect_sha256,
        "display_receipt": dict(display_receipt),
        **activation,
        "idempotent": False,
    }


def record_assignment_title_revalidation(
    store: Any,
    assignment_id: str,
    expected_version: int,
    display_receipt: dict[str, Any],
    event_id: str,
    at: str,
) -> dict[str, Any]:
    """Append one idempotent fresh visible-title observation after context delivery."""

    _time(at, "assignment title revalidation time")
    display_keys = {
        "source",
        "applies_to_source",
        "state_change_seq",
        "sidebar_name",
        "task_label",
        "thread_title",
        "terminal_title",
    }
    if (
        not event_id
        or set(display_receipt) != display_keys
        or not all(
            isinstance(display_receipt[key], str) and display_receipt[key]
            for key in display_keys - {"state_change_seq"}
        )
        or not isinstance(display_receipt["state_change_seq"], int)
        or display_receipt["state_change_seq"] < 0
    ):
        raise StorageRefusal(
            "assignment_context_invalid",
            "assignment title revalidation receipt is invalid",
        )
    detail = {"display_receipt": dict(display_receipt)}
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT task_id,state,version FROM task_assignments WHERE task_assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if (
                assignment is None
                or assignment["state"] != "active"
                or int(assignment["version"]) != expected_version
            ):
                raise StorageRefusal(
                    "assignment_conflict",
                    "title revalidation requires the exact active assignment version",
                )
            context = store.connection.execute(
                """
                SELECT 1 FROM events
                 WHERE aggregate_kind='assignment' AND aggregate_id=?
                   AND event_type='assignment_context_delivered'
                """,
                (assignment_id,),
            ).fetchall()
            if len(context) != 1:
                raise StorageRefusal(
                    "assignment_incomplete",
                    "title revalidation requires one exact context receipt",
                )
            existing = store.connection.execute(
                "SELECT aggregate_kind,aggregate_id,event_type,detail_json FROM events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["aggregate_kind"] != "assignment"
                    or existing["aggregate_id"] != assignment_id
                    or existing["event_type"] != "assignment_title_revalidated"
                    or existing["detail_json"] != _json(detail)
                ):
                    raise StorageRefusal(
                        "assignment_context_conflict",
                        "assignment title revalidation event conflicts with history",
                    )
                return {
                    "assignment_id": assignment_id,
                    "version": expected_version,
                    "event_id": event_id,
                    "display_receipt": dict(display_receipt),
                    "idempotent": True,
                }
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,NULL,?,?,'assignment_title_revalidated','active',
                       'Champion display metadata revalidated',?,?,'assignment',?)
                """,
                (
                    event_id,
                    assignment["task_id"],
                    expected_version,
                    at,
                    _json(detail),
                    assignment_id,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "assignment title revalidation conflicted with canonical state"
        ) from exc
    return {
        "assignment_id": assignment_id,
        "version": expected_version,
        "event_id": event_id,
        "display_receipt": dict(display_receipt),
        "idempotent": False,
    }


def fail_assignment_context_delivery(
    store: Any,
    assignment_id: str,
    expected_version: int,
    failure_class: str,
    event_id: str,
    outbox_id: str,
    at: str,
) -> dict[str, Any]:
    _time(at, "assignment context failure time")
    if not all((failure_class, event_id, outbox_id)):
        raise StorageRefusal(
            "invalid_assignment_failure", "assignment context failure identity is incomplete"
        )
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise StorageRefusal("assignment_unknown", "assignment does not exist")
            if (
                assignment["state"] == "cleanup_pending"
                and assignment["failure_class"] == failure_class
            ):
                return {
                    "assignment_id": assignment_id,
                    "state": "cleanup_pending",
                    "version": int(assignment["version"]),
                    "event_id": event_id,
                    "outbox_id": outbox_id,
                    "idempotent": True,
                }
            delivered = store.connection.execute(
                """
                SELECT 1 FROM events
                 WHERE aggregate_kind='assignment' AND aggregate_id=?
                   AND event_type='assignment_context_delivered'
                """,
                (assignment_id,),
            ).fetchone()
            if (
                assignment["state"] != "active"
                or int(assignment["version"]) != expected_version
                or delivered is not None
            ):
                raise StorageRefusal(
                    "assignment_conflict",
                    "assignment context failure does not match an undelivered active assignment",
                )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE task_assignments
                   SET state='cleanup_pending',failure_class=?,cleanup_required=1,
                       version=?,updated_at=?
                 WHERE task_assignment_id=?
                """,
                (failure_class, next_version, at, assignment_id),
            )
            store.connection.execute(
                "UPDATE tasks SET state='blocked',version=version+1,updated_at=? WHERE task_id=?",
                (at, assignment["task_id"]),
            )
            store.connection.execute(
                """
                INSERT INTO cleanup_obligations
                  (cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,
                   version,updated_at)
                VALUES(?,?,'pending','failed_launch_context',
                       'Clean only the exact activated launch runtime and callsign',1,?)
                ON CONFLICT(task_id) DO UPDATE SET
                  cleanup_state='pending',required_policy='failed_launch_context',
                  next_action='Clean only the exact activated launch runtime and callsign',
                  version=cleanup_obligations.version+1,updated_at=excluded.updated_at
                """,
                (f"cleanup:{assignment['task_id']}", assignment["task_id"], at),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,request_id,aggregate_kind,aggregate_id)
                VALUES(?,NULL,?,?,'assignment_launch_failed','cleanup_pending',?,?,?, ?,
                       'assignment',?)
                """,
                (
                    event_id,
                    assignment["task_id"],
                    next_version,
                    failure_class,
                    at,
                    _json({"failure_class": failure_class}),
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
                (outbox_id, event_id, assignment["coordinator_agent_id"], at),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "assignment context failure conflicted with canonical state"
        ) from exc
    return {
        "assignment_id": assignment_id,
        "state": "cleanup_pending",
        "version": next_version,
        "event_id": event_id,
        "outbox_id": outbox_id,
        "idempotent": False,
    }


def fail_assignment_title_validation(
    store: Any,
    assignment_id: str,
    expected_version: int,
    failure_class: str,
    event_id: str,
    outbox_id: str,
    at: str,
) -> dict[str, Any]:
    _time(at, "assignment title validation failure time")
    if not all((failure_class, event_id, outbox_id)):
        raise StorageRefusal(
            "invalid_assignment_failure",
            "assignment title validation failure identity is incomplete",
        )
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise StorageRefusal("assignment_unknown", "assignment does not exist")
            if (
                assignment["state"] == "cleanup_pending"
                and assignment["failure_class"] == failure_class
            ):
                return {
                    "assignment_id": assignment_id,
                    "state": "cleanup_pending",
                    "version": int(assignment["version"]),
                    "event_id": event_id,
                    "outbox_id": outbox_id,
                    "idempotent": True,
                }
            delivered = store.connection.execute(
                """
                SELECT 1 FROM events
                 WHERE aggregate_kind='assignment' AND aggregate_id=?
                   AND event_type='assignment_context_delivered'
                """,
                (assignment_id,),
            ).fetchone()
            if (
                assignment["state"] != "active"
                or int(assignment["version"]) != expected_version
                or delivered is None
            ):
                raise StorageRefusal(
                    "assignment_conflict",
                    "title validation failure does not match a delivered active assignment",
                )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE task_assignments
                   SET state='cleanup_pending',failure_class=?,cleanup_required=1,
                       version=?,updated_at=?
                 WHERE task_assignment_id=?
                """,
                (failure_class, next_version, at, assignment_id),
            )
            store.connection.execute(
                "UPDATE tasks SET state='blocked',version=version+1,updated_at=? WHERE task_id=?",
                (at, assignment["task_id"]),
            )
            store.connection.execute(
                """
                INSERT INTO cleanup_obligations
                  (cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,
                   version,updated_at)
                VALUES(?,?,'pending','failed_launch_title_validation',
                       'Preserve and reconcile only the exact active runtime display owner',1,?)
                ON CONFLICT(task_id) DO UPDATE SET
                  cleanup_state='pending',required_policy='failed_launch_title_validation',
                  next_action='Preserve and reconcile only the exact active runtime display owner',
                  version=cleanup_obligations.version+1,updated_at=excluded.updated_at
                """,
                (f"cleanup:{assignment['task_id']}", assignment["task_id"], at),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,request_id,aggregate_kind,aggregate_id)
                VALUES(?,NULL,?,?,'assignment_title_validation_failed','cleanup_pending',
                       ?,?,?,?,'assignment',?)
                """,
                (
                    event_id,
                    assignment["task_id"],
                    next_version,
                    failure_class,
                    at,
                    _json({"failure_class": failure_class}),
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
                (outbox_id, event_id, assignment["coordinator_agent_id"], at),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "assignment title validation failure conflicted with canonical state"
        ) from exc
    return {
        "assignment_id": assignment_id,
        "state": "cleanup_pending",
        "version": next_version,
        "event_id": event_id,
        "outbox_id": outbox_id,
        "failure_class": failure_class,
        "cleanup_required": True,
        "idempotent": False,
    }


def settle_assignment_launch_cleanup(
    store: Any,
    assignment_id: str,
    expected_version: int,
    cleanup_receipt_digest: str,
    at: str,
) -> dict[str, Any]:
    _time(at, "assignment launch cleanup settlement time")
    if not re.fullmatch(r"[0-9a-f]{64}", cleanup_receipt_digest):
        raise StorageRefusal(
            "receipt_required", "launch cleanup requires an exact receipt digest"
        )
    try:
        with store._transaction():
            assignment = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise StorageRefusal("assignment_unknown", "assignment does not exist")
            if assignment["state"] == "blocked":
                if assignment["cleanup_receipt"] != cleanup_receipt_digest:
                    raise StorageRefusal(
                        "receipt_conflict", "settled launch cleanup has a different receipt"
                    )
                return {
                    "assignment_id": assignment_id,
                    "state": "blocked",
                    "version": int(assignment["version"]),
                    "cleanup_receipt": cleanup_receipt_digest,
                    "idempotent": True,
                }
            if (
                assignment["state"] != "cleanup_pending"
                or int(assignment["version"]) != expected_version
                or not assignment["runtime_instance_id"]
            ):
                raise StorageRefusal(
                    "assignment_conflict", "launch cleanup is not pending at the expected version"
                )
            runtime = store.connection.execute(
                "SELECT status FROM runtime_instances WHERE runtime_instance_id=?",
                (assignment["runtime_instance_id"],),
            ).fetchone()
            callsign = store.connection.execute(
                """
                SELECT state,release_receipt_digest FROM callsign_assignments
                 WHERE callsign_assignment_id=?
                """,
                (f"callsign-assignment:{assignment_id}",),
            ).fetchone()
            if (
                runtime is None
                or runtime["status"] != "closed"
                or callsign is None
                or callsign["state"] != "released"
                or callsign["release_receipt_digest"] != cleanup_receipt_digest
            ):
                raise StorageRefusal(
                    "cleanup_unproven",
                    "exact runtime close and callsign release are required before settlement",
                )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE task_assignments
                   SET state='blocked',cleanup_required=0,cleanup_receipt=?,version=?,updated_at=?
                 WHERE task_assignment_id=?
                """,
                (cleanup_receipt_digest, next_version, at, assignment_id),
            )
            store.connection.execute(
                """
                UPDATE cleanup_obligations
                   SET cleanup_state='cleanup_completed',next_action='None',
                       version=version+1,updated_at=?
                 WHERE task_id=? AND cleanup_state IN ('pending','cleanup_pending')
                """,
                (at, assignment["task_id"]),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "assignment launch cleanup settlement conflicted with canonical state"
        ) from exc
    return {
        "assignment_id": assignment_id,
        "state": "blocked",
        "version": next_version,
        "cleanup_receipt": cleanup_receipt_digest,
        "idempotent": False,
    }


def _task_transition_retry(
    store: Any,
    *,
    task_id: str,
    state: str,
    transition_key: str,
    recipient_agent_id: str,
) -> Optional[dict[str, Any]]:
    duplicate = store.connection.execute(
        "SELECT transition_id,event_id,task_id,to_state FROM task_transitions WHERE transition_key=?",
        (transition_key,),
    ).fetchone()
    if duplicate is None:
        return None
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


def _validated_transition_context(
    store: Any,
    *,
    task_id: str,
    runtime_instance_id: str,
    expected_version: int,
    state: str,
    recipient_agent_id: str,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    task = store.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    assignment = store.connection.execute(
        "SELECT * FROM task_assignments WHERE task_id=?", (task_id,)
    ).fetchone()
    if task is None or assignment is None or assignment["state"] != "active":
        raise StorageRefusal("assignment_inactive", "task has no active verified assignment")
    if assignment["assignment_role"] == "hidden-worker":
        raise StorageRefusal(
            "hidden_terminal_only",
            "hidden scientists emit no routine progress; use the cleanup-gated terminal operation",
        )
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
    if task["state"] in TASK_TERMINAL_STATES:
        raise StorageRefusal("task_terminal", "terminal task state cannot produce another transition")
    if state not in TASK_TRANSITIONS.get(str(task["state"]), frozenset()):
        raise StorageRefusal("invalid_task_transition", "task state progression is not permitted")
    if recipient_agent_id != assignment["coordinator_agent_id"]:
        raise StorageRefusal("recipient_mismatch", "task transition recipient is not its coordinator")
    return task, assignment, runtime


def _persist_task_transition(
    store: Any,
    *,
    task: sqlite3.Row,
    assignment: sqlite3.Row,
    runtime: sqlite3.Row,
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
) -> int:
    next_version = expected_version + 1
    result_summary = update if state in {"completed", "complete", "ready_to_land"} else task["result_summary"]
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
        (
            f"obligation:{outbox_id}",
            recipient_agent_id,
            outbox_id,
            f"delivery:{outbox_id}",
            at,
            at,
            at,
        ),
    )
    return next_version


def _persist_task_cleanup(store: Any, *, task_id: str, state: str, at: str) -> None:
    if state not in TASK_CLEANUP_STATES:
        return
    ready_to_land = state == "ready_to_land"
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
            "awaiting_authority" if ready_to_land else "pending",
            "ready_to_land" if ready_to_land else "terminal_task",
            "Await exact landing/release proof" if ready_to_land else "Reconcile exact task resources",
            at,
        ),
    )


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
            retry = _task_transition_retry(
                store,
                task_id=task_id,
                state=state,
                transition_key=transition_key,
                recipient_agent_id=recipient_agent_id,
            )
            if retry is not None:
                return retry
            task, assignment, runtime = _validated_transition_context(
                store,
                task_id=task_id,
                runtime_instance_id=runtime_instance_id,
                expected_version=expected_version,
                state=state,
                recipient_agent_id=recipient_agent_id,
            )
            next_version = _persist_task_transition(
                store,
                task=task,
                assignment=assignment,
                runtime=runtime,
                task_id=task_id,
                runtime_instance_id=runtime_instance_id,
                expected_version=expected_version,
                state=state,
                update=update,
                next_action=next_action,
                blocker=blocker,
                transition_id=transition_id,
                transition_key=transition_key,
                event_id=event_id,
                outbox_id=outbox_id,
                recipient_agent_id=recipient_agent_id,
                at=at,
            )
            _persist_task_cleanup(store, task_id=task_id, state=state, at=at)
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
