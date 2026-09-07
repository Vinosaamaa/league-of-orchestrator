"""SQLite operations for durable duplicate-preflight issue selection."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .sqlite_project_ops import canonical_repository
from .storage_issue import BeginIssueSelectionCommand, CompleteIssueSelectionCommand
from .storage_types import StorageRefusal


RECEIPT_SCHEMA = "league.issue-selection-receipt.v1"
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_TOKEN.fullmatch(value):
        raise StorageRefusal("issue_selection_invalid", f"{label} is invalid")
    return value


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > maximum
        or any(character in value for character in "\x00\r")
    ):
        raise StorageRefusal("issue_selection_invalid", f"{label} is invalid")
    return value


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("issue_selection_invalid", f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("issue_selection_invalid", f"{label} needs a timezone")
    return parsed.astimezone(timezone.utc)


def _prior_linkage(store: Any, repository_key: str, issue: int) -> dict[str, str] | None:
    row = store.connection.execute(
        """
        SELECT b.task_id,b.assignment_id,a.champion_agent_id,a.runtime_instance_id,
               r.session_ref
          FROM repository_issue_bindings b
          JOIN repository_issue_selection_receipts s
            ON s.receipt_digest=b.issue_selection_receipt_digest
          JOIN task_assignments a ON a.task_assignment_id=b.assignment_id
          LEFT JOIN runtime_instances r ON r.runtime_instance_id=a.runtime_instance_id
         WHERE s.repository_key=? AND b.issue=?
         ORDER BY s.created_at DESC,s.selection_receipt_id DESC LIMIT 1
        """,
        (repository_key, issue),
    ).fetchone()
    if row is None:
        return None
    return {
        "task_id": row["task_id"],
        "assignment_id": row["assignment_id"],
        "champion_agent_id": row["champion_agent_id"],
        "runtime_instance_id": row["runtime_instance_id"],
        "session_ref": row["session_ref"],
    }


def _receipt(row: sqlite3.Row, *, idempotent: bool) -> dict[str, Any]:
    prior = None
    if row["prior_task_id"] is not None:
        prior = {
            "task_id": row["prior_task_id"],
            "assignment_id": row["prior_assignment_id"],
            "champion_agent_id": row["prior_champion_agent_id"],
            "runtime_instance_id": row["prior_runtime_instance_id"],
            "session_ref": row["prior_session_ref"],
        }
    return {
        "schema": RECEIPT_SCHEMA,
        "selection_receipt_id": row["selection_receipt_id"],
        "selection_key": row["selection_key"],
        "selection_version": int(row["selection_version"]),
        "task_id": row["task_id"],
        "task_summary": row["task_summary"],
        "coordinator_agent_id": row["coordinator_agent_id"],
        "repository": row["repository"],
        "repository_key": row["repository_key"],
        "normalized_title": row["normalized_title"],
        "semantic_scope_digest": row["semantic_scope_digest"],
        "decision": row["decision"],
        "issue": int(row["issue"]),
        "issue_url": row["issue_url"],
        "issue_state": row["issue_state"],
        "issue_title": row["issue_title"],
        "issue_body_digest": row["issue_body_digest"],
        "duplicate_matches": int(row["duplicate_matches"]),
        "prior_linkage": prior,
        "reopen_action_receipt_digest": row["reopen_action_receipt_digest"],
        "task_scope_digest": row["task_scope_digest"],
        "selected_at": row["created_at"],
        "receipt_digest": row["receipt_digest"],
        "idempotent": idempotent,
    }


def begin_issue_selection(store: Any, command: BeginIssueSelectionCommand) -> dict[str, Any]:
    _token(command.selection_key, "selection key")
    _token(command.task_id, "task id")
    _text(command.task_summary, "task summary")
    _token(command.coordinator_agent_id, "coordinator id")
    _token(command.owner_attempt_id, "selection attempt")
    _, repository_key = canonical_repository(command.repository)
    if repository_key != command.repository_key:
        raise StorageRefusal("issue_selection_invalid", "repository key is invalid")
    _text(command.normalized_title, "normalized title", 512)
    if not DIGEST.fullmatch(command.semantic_scope_digest):
        raise StorageRefusal("issue_selection_invalid", "semantic scope digest is invalid")
    instant = _time(command.at, "selection time")
    lease = _time(command.lease_expires_at, "selection lease")
    if lease <= instant:
        raise StorageRefusal("issue_selection_invalid", "selection lease must be future")
    try:
        with store._transaction():
            existing_receipt = store.connection.execute(
                "SELECT * FROM repository_issue_selection_receipts WHERE task_id=?",
                (command.task_id,),
            ).fetchone()
            if existing_receipt is not None:
                if (
                    existing_receipt["selection_key"] != command.selection_key
                    or existing_receipt["task_summary"] != command.task_summary
                    or existing_receipt["coordinator_agent_id"]
                    != command.coordinator_agent_id
                ):
                    raise StorageRefusal(
                        "issue_selection_conflict",
                        "task already has a different issue-selection receipt",
                    )
                return {
                    "state": "completed",
                    "receipt": _receipt(existing_receipt, idempotent=True),
                }
            owner = store.connection.execute(
                "SELECT role,retired_at FROM agent_instances WHERE agent_id=?",
                (command.coordinator_agent_id,),
            ).fetchone()
            if owner is None or owner["role"] != "shotcaller" or owner["retired_at"] is not None:
                raise StorageRefusal(
                    "issue_selection_owner_invalid",
                    "issue selection requires one live canonical Shotcaller",
                )
            row = store.connection.execute(
                "SELECT * FROM repository_issue_selection_leases WHERE selection_key=?",
                (command.selection_key,),
            ).fetchone()
            if row is None:
                store.connection.execute(
                    """
                    INSERT INTO repository_issue_selection_leases
                      (selection_key,repository,repository_key,normalized_title,
                       semantic_scope_digest,state,owner_attempt_id,current_task_id,
                       current_task_summary,current_coordinator_agent_id,
                       lease_expires_at,version,created_at,updated_at)
                    VALUES(?,?,?,?,?,'selecting',?,?,?,?,?,1,?,?)
                    """,
                    (
                        command.selection_key,
                        command.repository,
                        command.repository_key,
                        command.normalized_title,
                        command.semantic_scope_digest,
                        command.owner_attempt_id,
                        command.task_id,
                        command.task_summary,
                        command.coordinator_agent_id,
                        command.lease_expires_at,
                        command.at,
                        command.at,
                    ),
                )
                version = 1
            else:
                exact = (
                    row["repository_key"] == command.repository_key
                    and row["normalized_title"] == command.normalized_title
                    and row["semantic_scope_digest"] == command.semantic_scope_digest
                )
                if not exact:
                    raise StorageRefusal(
                        "issue_selection_conflict", "selection key names different scope"
                    )
                if row["state"] == "selecting" and _time(
                    row["lease_expires_at"], "active selection lease"
                ) > instant:
                    raise StorageRefusal(
                        "issue_selection_busy",
                        "equivalent issue selection is already in progress",
                    )
                version = int(row["version"]) + 1
                updated = store.connection.execute(
                    """
                    UPDATE repository_issue_selection_leases
                       SET state='selecting',owner_attempt_id=?,current_task_id=?,
                           current_task_summary=?,current_coordinator_agent_id=?,
                           lease_expires_at=?,version=?,updated_at=?
                     WHERE selection_key=? AND version=?
                    """,
                    (
                        command.owner_attempt_id,
                        command.task_id,
                        command.task_summary,
                        command.coordinator_agent_id,
                        command.lease_expires_at,
                        version,
                        command.at,
                        command.selection_key,
                        int(row["version"]),
                    ),
                )
                if updated.rowcount != 1:
                    raise StorageRefusal(
                        "issue_selection_busy", "issue selection lease changed concurrently"
                    )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "issue selection lease conflicted") from exc
    return {"state": "selecting", "selection_key": command.selection_key, "version": version}


def release_issue_selection(
    store: Any,
    selection_key: str,
    owner_attempt_id: str,
    expected_version: int,
    at: str,
) -> dict[str, Any]:
    _token(selection_key, "selection key")
    _token(owner_attempt_id, "selection attempt")
    _time(at, "selection release time")
    try:
        with store._transaction():
            updated = store.connection.execute(
                """
                UPDATE repository_issue_selection_leases
                   SET state='available',owner_attempt_id=NULL,lease_expires_at=NULL,
                       version=version+1,updated_at=?
                 WHERE selection_key=? AND owner_attempt_id=? AND version=? AND state='selecting'
                """,
                (at, selection_key, owner_attempt_id, expected_version),
            )
            if updated.rowcount != 1:
                raise StorageRefusal(
                    "issue_selection_conflict", "issue selection release lost its owner fence"
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "issue selection release conflicted") from exc
    return {"selection_key": selection_key, "state": "available", "released": True}


def verify_issue_reopen_authority(
    store: Any,
    receipt_digest: str,
    coordinator_agent_id: str,
    repository: str,
    issue: int,
) -> dict[str, Any]:
    if not DIGEST.fullmatch(receipt_digest):
        raise StorageRefusal("issue_reopen_authority_invalid", "reopen receipt is invalid")
    _token(coordinator_agent_id, "coordinator id")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise StorageRefusal("issue_reopen_authority_invalid", "reopen issue is invalid")
    row = store.connection.execute(
        """
        SELECT * FROM autonomous_action_uses
         WHERE result_receipt_digest=? AND action_kind='issue_reopen' AND state='succeeded'
        """,
        (receipt_digest,),
    ).fetchone()
    if row is None or row["external_owner_agent_id"] != coordinator_agent_id:
        raise StorageRefusal(
            "issue_reopen_authority_invalid",
            "closed issue has no exact settled Shotcaller reopen authority",
        )
    action_scope = json.loads(row["action_scope_json"])
    resources = json.loads(row["resource_use_json"])
    if (
        canonical_repository(str(action_scope.get("repository")))[1]
        != canonical_repository(repository)[1]
        or resources.get("issue") != issue
    ):
        raise StorageRefusal(
            "issue_reopen_authority_invalid",
            "reopen authority does not match the exact repository issue",
        )
    return {
        "action_use_id": row["action_use_id"],
        "receipt_digest": receipt_digest,
        "issue": issue,
    }


def complete_issue_selection(store: Any, command: CompleteIssueSelectionCommand) -> dict[str, Any]:
    from .issue_first import issue_scope_digest, normalize_issue_title

    _token(command.selection_key, "selection key")
    _token(command.owner_attempt_id, "selection attempt")
    _token(command.task_id, "task id")
    _text(command.task_summary, "task summary")
    _token(command.coordinator_agent_id, "coordinator id")
    _, repository_key = canonical_repository(command.repository)
    if repository_key != command.repository_key:
        raise StorageRefusal("issue_selection_invalid", "repository key is invalid")
    _text(command.normalized_title, "normalized title", 512)
    _text(command.issue_title, "selected issue title", 512)
    if normalize_issue_title(command.issue_title) != command.normalized_title:
        raise StorageRefusal(
            "issue_selection_invalid", "selected issue title changed normalized identity"
        )
    if command.decision not in {"reuse_open", "reopen_closed", "create_distinct"}:
        raise StorageRefusal("issue_selection_invalid", "selection decision is invalid")
    if isinstance(command.issue, bool) or not isinstance(command.issue, int) or command.issue < 1:
        raise StorageRefusal("issue_selection_invalid", "selected issue is invalid")
    if (
        isinstance(command.duplicate_matches, bool)
        or not isinstance(command.duplicate_matches, int)
        or command.duplicate_matches < 0
    ):
        raise StorageRefusal("issue_selection_invalid", "duplicate count is invalid")
    for value, label in (
        (command.semantic_scope_digest, "semantic scope digest"),
        (command.issue_body_digest, "issue body digest"),
    ):
        if not DIGEST.fullmatch(value):
            raise StorageRefusal("issue_selection_invalid", f"{label} is invalid")
    if command.reopen_action_receipt_digest is not None and not DIGEST.fullmatch(
        command.reopen_action_receipt_digest
    ):
        raise StorageRefusal("issue_selection_invalid", "reopen receipt digest is invalid")
    if command.decision == "reopen_closed":
        if command.reopen_action_receipt_digest is None:
            raise StorageRefusal(
                "issue_selection_invalid", "reopened selection needs its exact action receipt"
            )
        verify_issue_reopen_authority(
            store,
            command.reopen_action_receipt_digest,
            command.coordinator_agent_id,
            command.repository,
            command.issue,
        )
    elif command.reopen_action_receipt_digest is not None:
        raise StorageRefusal(
            "issue_selection_invalid", "non-reopen selection cannot carry reopen authority"
        )
    if command.decision == "create_distinct" and command.duplicate_matches != 0:
        raise StorageRefusal(
            "issue_selection_invalid", "distinct creation cannot retain equivalent matches"
        )
    if command.decision != "create_distinct" and command.duplicate_matches < 1:
        raise StorageRefusal(
            "issue_selection_invalid", "reuse or reopen requires an equivalent match"
        )
    host, _, path = command.repository_key.partition("/")
    expected_url = f"https://{host}/{path}/issues/{command.issue}"
    if command.issue_url != expected_url:
        raise StorageRefusal("issue_selection_invalid", "selected issue URL is invalid")
    _time(command.at, "selection completion time")
    prior = _prior_linkage(store, command.repository_key, command.issue)
    task_scope = issue_scope_digest(
        command.repository, command.issue, command.task_id, command.task_summary
    )
    selection_receipt_id = f"issue-selection:{_digest({'selection_key': command.selection_key, 'task_id': command.task_id})[:32]}"
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "selection_receipt_id": selection_receipt_id,
        "selection_key": command.selection_key,
        "selection_version": command.expected_version,
        "task_id": command.task_id,
        "task_summary": command.task_summary,
        "coordinator_agent_id": command.coordinator_agent_id,
        "repository": command.repository,
        "repository_key": command.repository_key,
        "normalized_title": command.normalized_title,
        "semantic_scope_digest": command.semantic_scope_digest,
        "decision": command.decision,
        "issue": command.issue,
        "issue_url": command.issue_url,
        "issue_state": "open",
        "issue_title": command.issue_title,
        "issue_body_digest": command.issue_body_digest,
        "duplicate_matches": command.duplicate_matches,
        "prior_linkage": prior,
        "reopen_action_receipt_digest": command.reopen_action_receipt_digest,
        "task_scope_digest": task_scope,
        "selected_at": command.at,
    }
    receipt_digest = _digest(receipt)
    try:
        with store._transaction():
            lease = store.connection.execute(
                "SELECT * FROM repository_issue_selection_leases WHERE selection_key=?",
                (command.selection_key,),
            ).fetchone()
            if (
                lease is None
                or lease["state"] != "selecting"
                or lease["owner_attempt_id"] != command.owner_attempt_id
                or lease["current_task_id"] != command.task_id
                or lease["current_task_summary"] != command.task_summary
                or lease["current_coordinator_agent_id"]
                != command.coordinator_agent_id
                or lease["repository_key"] != command.repository_key
                or lease["normalized_title"] != command.normalized_title
                or lease["semantic_scope_digest"] != command.semantic_scope_digest
                or int(lease["version"]) != command.expected_version
            ):
                raise StorageRefusal(
                    "issue_selection_conflict", "selection completion lost its owner fence"
                )
            owner = store.connection.execute(
                "SELECT role,retired_at FROM agent_instances WHERE agent_id=?",
                (command.coordinator_agent_id,),
            ).fetchone()
            if owner is None or owner["role"] != "shotcaller" or owner["retired_at"] is not None:
                raise StorageRefusal(
                    "issue_selection_owner_invalid",
                    "issue selection owner retired before receipt settlement",
                )
            store.connection.execute(
                """
                INSERT INTO repository_issue_selection_receipts
                  (selection_receipt_id,selection_key,selection_version,task_id,task_summary,
                   coordinator_agent_id,repository,repository_key,normalized_title,
                   semantic_scope_digest,decision,
                   issue,issue_url,issue_state,issue_title,issue_body_digest,duplicate_matches,
                   prior_task_id,prior_assignment_id,prior_champion_agent_id,
                   prior_runtime_instance_id,prior_session_ref,
                   reopen_action_receipt_digest,task_scope_digest,receipt_digest,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    selection_receipt_id,
                    command.selection_key,
                    command.expected_version,
                    command.task_id,
                    command.task_summary,
                    command.coordinator_agent_id,
                    command.repository,
                    command.repository_key,
                    command.normalized_title,
                    command.semantic_scope_digest,
                    command.decision,
                    command.issue,
                    command.issue_url,
                    "open",
                    command.issue_title,
                    command.issue_body_digest,
                    command.duplicate_matches,
                    None if prior is None else prior["task_id"],
                    None if prior is None else prior["assignment_id"],
                    None if prior is None else prior["champion_agent_id"],
                    None if prior is None else prior["runtime_instance_id"],
                    None if prior is None else prior["session_ref"],
                    command.reopen_action_receipt_digest,
                    task_scope,
                    receipt_digest,
                    command.at,
                ),
            )
            updated = store.connection.execute(
                """
                UPDATE repository_issue_selection_leases
                   SET state='completed',owner_attempt_id=NULL,lease_expires_at=NULL,
                       version=version+1,updated_at=?
                 WHERE selection_key=? AND owner_attempt_id=? AND version=?
                """,
                (
                    command.at,
                    command.selection_key,
                    command.owner_attempt_id,
                    command.expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise StorageRefusal(
                    "issue_selection_conflict", "selection completion changed concurrently"
                )
            row = store.connection.execute(
                "SELECT * FROM repository_issue_selection_receipts WHERE selection_receipt_id=?",
                (selection_receipt_id,),
            ).fetchone()
            assert row is not None
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "issue selection completion conflicted") from exc
    return _receipt(row, idempotent=False)
