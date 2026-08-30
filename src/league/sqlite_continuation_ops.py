"""Canonical thread archive and exact-continuation operations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Optional

from .issue_first import (
    ISSUE_RECEIPT_SCHEMA,
    issue_scope_digest,
    normalize_issue_title,
    task_issue_semantic_binding_digest,
)
from .sqlite_project_ops import canonical_repository
from .storage_types import StorageRefusal
from .worktree import normalized_github_repository


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BENEFITS = {
    "same_task_recovery",
    "same_artifact_revision",
    "unresolved_decision_chain",
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("continuation_time_invalid", f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("continuation_time_invalid", f"{label} must include a UTC offset")
    return parsed


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _decode_thread_identity(provider_kind: str, thread_identity: str) -> str:
    prefix = provider_kind + ":"
    if not provider_kind or not thread_identity.startswith(prefix) or len(thread_identity) == len(prefix):
        raise StorageRefusal(
            "thread_identity_missing",
            "provider thread identity is not one exact namespaced opaque value",
        )
    return thread_identity[len(prefix) :]


def _archive_value(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["acceptance"] = json.loads(value.pop("acceptance_json"))
    value["cleanup_evidence"] = json.loads(value.pop("cleanup_evidence_json"))
    return value


def _operation_value(store: Any, row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["issue_receipt"] = (
        None
        if value["issue_receipt_json"] is None
        else json.loads(value["issue_receipt_json"])
    )
    value.pop("issue_receipt_json")
    archive = store.connection.execute(
        "SELECT * FROM thread_archives WHERE archive_id=?", (row["archive_id"],)
    ).fetchone()
    lineage = store.connection.execute(
        "SELECT * FROM thread_lineages WHERE lineage_id=?", (archive["lineage_id"],)
    ).fetchone()
    value["archive"] = _archive_value(archive)
    value["lineage"] = dict(lineage)
    value["lineage"]["resume_capabilities"] = json.loads(
        value["lineage"].pop("resume_capabilities_json")
    )
    return value


def _require_resumable_issue_binding(
    store: Any,
    *,
    task_id: str,
    owner_agent_id: str,
    runtime_instance_id: str,
    repository: str,
    issue: int,
    callsign: str,
) -> None:
    binding = store.connection.execute(
        """
        SELECT b.task_id binding_task_id,b.assignment_id,b.request_id binding_request_id,
               b.repository binding_repository,b.issue binding_issue,b.issue_url binding_issue_url,
               b.issue_state binding_issue_state,b.issue_title binding_issue_title,
               b.issue_body_digest binding_issue_body_digest,
               b.semantic_binding_digest,b.task_scope_digest binding_task_scope_digest,
               b.issue_selection_receipt_digest,b.reopen_action_receipt_digest binding_reopen_digest,
               b.verifier_kind,b.verified_at,b.receipt_digest binding_receipt_digest,
               a.task_id assignment_task_id,a.request_id assignment_request_id,
               a.coordinator_agent_id,a.champion_agent_id,a.runtime_instance_id,
               a.callsign assignment_callsign,
               t.summary task_summary,t.current_owner_agent_id,
               s.task_id selection_task_id,s.task_summary selection_task_summary,
               s.coordinator_agent_id selection_coordinator_agent_id,
               s.repository selection_repository,s.repository_key selection_repository_key,
               s.issue selection_issue,s.issue_url selection_issue_url,
               s.issue_state selection_issue_state,s.issue_title selection_issue_title,
               s.normalized_title selection_normalized_title,
               s.semantic_scope_digest selection_semantic_scope_digest,
               s.issue_body_digest selection_issue_body_digest,
               s.task_scope_digest selection_task_scope_digest,
               s.reopen_action_receipt_digest selection_reopen_digest,
               s.receipt_digest selection_receipt_digest
          FROM repository_issue_bindings b
          JOIN task_assignments a ON a.task_assignment_id=b.assignment_id
          JOIN tasks t ON t.task_id=b.task_id
          JOIN repository_issue_selection_receipts s
            ON s.receipt_digest=b.issue_selection_receipt_digest
         WHERE b.task_id=?
        """,
        (task_id,),
    ).fetchone()
    if binding is None:
        raise StorageRefusal(
            "assignment_issue_reconciliation_required",
            "resumable cleanup requires the migration-18 assignment issue binding",
        )
    repository_key = canonical_repository(repository)[1]
    expected_scope = issue_scope_digest(
        repository, issue, task_id, binding["task_summary"]
    )
    expected_semantic_binding = task_issue_semantic_binding_digest(
        repository,
        issue,
        task_id,
        binding["task_summary"],
        binding["selection_issue_title"],
        binding["selection_semantic_scope_digest"],
    )
    receipt = {
        "schema": ISSUE_RECEIPT_SCHEMA,
        "repository": binding["binding_repository"],
        "repository_key": binding["selection_repository_key"],
        "issue": int(binding["binding_issue"]),
        "issue_url": binding["binding_issue_url"],
        "issue_state": binding["binding_issue_state"],
        "issue_title": binding["binding_issue_title"],
        "normalized_title": binding["selection_normalized_title"],
        "issue_body_digest": binding["binding_issue_body_digest"],
        "semantic_scope_digest": binding["selection_semantic_scope_digest"],
        "task_scope_digest": binding["binding_task_scope_digest"],
        "issue_selection_receipt_digest": binding[
            "issue_selection_receipt_digest"
        ],
        "verifier_kind": binding["verifier_kind"],
        "verified_at": binding["verified_at"],
    }
    exact = (
        binding["binding_task_id"] == task_id
        and binding["assignment_task_id"] == task_id
        and binding["selection_task_id"] == task_id
        and binding["binding_request_id"] == binding["assignment_request_id"]
        and binding["selection_task_summary"] == binding["task_summary"]
        and binding["current_owner_agent_id"] == owner_agent_id
        and binding["champion_agent_id"] == owner_agent_id
        and binding["runtime_instance_id"] == runtime_instance_id
        and binding["assignment_callsign"] == callsign
        and binding["selection_coordinator_agent_id"]
        == binding["coordinator_agent_id"]
        and binding["binding_repository"] == repository
        and binding["selection_repository"] == repository
        and binding["selection_repository_key"] == repository_key
        and repository_key.partition("/")[0] == "github.com"
        and int(binding["binding_issue"]) == issue
        and int(binding["selection_issue"]) == issue
        and binding["binding_issue_state"] == "open"
        and binding["selection_issue_state"] == "open"
        and binding["binding_issue_url"] == binding["selection_issue_url"]
        and binding["binding_issue_title"] == binding["selection_issue_title"]
        and binding["selection_normalized_title"]
        == normalize_issue_title(binding["task_summary"])
        and binding["binding_issue_body_digest"]
        == binding["selection_issue_body_digest"]
        and binding["binding_task_scope_digest"] == expected_scope
        and binding["selection_task_scope_digest"] == expected_scope
        and binding["semantic_binding_digest"] == expected_semantic_binding
        and binding["issue_selection_receipt_digest"]
        == binding["selection_receipt_digest"]
        and binding["binding_reopen_digest"] == binding["selection_reopen_digest"]
        and binding["verifier_kind"] == "github-api"
        and binding["binding_receipt_digest"] == _digest(receipt)
    )
    if not exact:
        raise StorageRefusal(
            "assignment_issue_reverification_failed",
            "resumable cleanup issue identity does not match its immutable assignment binding",
        )


def record_thread_archive_for_cleanup(store: Any, plan: Mapping[str, Any]) -> None:
    """Insert the immutable provider-thread archive before cleanup can execute."""

    archive = plan.get("continuation_archive")
    if archive is None:
        return
    required = {
        "archive_id",
        "lineage_id",
        "provider_kind",
        "thread_identity",
        "runtime_instance_id",
        "repository",
        "issue",
        "branch",
        "worktree",
        "prior_callsign",
        "instruction_digest",
        "policy_digest",
        "context_health",
        "resume_capabilities",
        "acceptance",
        "cleanup_evidence",
    }
    if not isinstance(archive, Mapping) or set(archive) != required:
        raise StorageRefusal("thread_archive_invalid", "thread archive evidence is incomplete")
    identity_fields = (
        "archive_id",
        "lineage_id",
        "provider_kind",
        "thread_identity",
        "runtime_instance_id",
        "repository",
        "branch",
        "worktree",
        "prior_callsign",
    )
    if (
        any(not isinstance(archive[key], str) or not archive[key] for key in identity_fields)
        or re.fullmatch(r"[a-z][a-z0-9-]*", archive["provider_kind"]) is None
        or isinstance(archive["issue"], bool)
        or not isinstance(archive["issue"], int)
        or archive["issue"] < 1
        or not _DIGEST.fullmatch(str(archive["instruction_digest"]))
        or not _DIGEST.fullmatch(str(archive["policy_digest"]))
        or archive["context_health"] not in {"healthy", "degraded", "unhealthy", "conflicted"}
        or not isinstance(archive["acceptance"], Mapping)
        or archive["acceptance"].get("required_gates_complete") is not True
        or not isinstance(archive["cleanup_evidence"], Mapping)
        or archive["cleanup_evidence"] != plan.get("proof")
    ):
        raise StorageRefusal("thread_archive_invalid", "thread archive policy evidence is invalid")
    capabilities = archive["resume_capabilities"]
    if (
        not isinstance(capabilities, Mapping)
        or set(capabilities) != {"durable", "exact_resume", "safe_worktree_rebind"}
        or any(type(capabilities[key]) is not bool for key in capabilities)
    ):
        raise StorageRefusal("thread_archive_invalid", "thread resume declarations are invalid")
    session_ref = _decode_thread_identity(
        str(archive["provider_kind"]), str(archive["thread_identity"])
    )
    task = store.connection.execute(
        "SELECT * FROM tasks WHERE task_id=?", (plan["task_id"],)
    ).fetchone()
    owner = store.connection.execute(
        "SELECT * FROM agent_instances WHERE agent_id=?", (plan["owner_id"],)
    ).fetchone()
    runtime = store.connection.execute(
        "SELECT * FROM runtime_instances WHERE runtime_instance_id=?",
        (archive["runtime_instance_id"],),
    ).fetchone()
    exact = (
        task is not None
        and owner is not None
        and runtime is not None
        and task["current_owner_agent_id"] == plan["owner_id"]
        and owner["role"] == "champion"
        and owner["task_id"] == plan["task_id"]
        and owner["repository"] == archive["repository"]
        and owner["issue"] == archive["issue"]
        and owner["branch"] == archive["branch"]
        and owner["worktree"] == archive["worktree"]
        and owner["callsign"] == archive["prior_callsign"]
        and owner["thread_id"] == session_ref
        and runtime["actor_agent_id"] == plan["owner_id"]
        and runtime["session_ref"] == session_ref
        and runtime["status"] in {"active", "idle"}
        and bool(runtime["verified"])
    )
    if not exact:
        raise StorageRefusal(
            "thread_identity_ambiguous",
            "thread archive does not match the exact task, Champion, and runtime binding",
        )
    _require_resumable_issue_binding(
        store,
        task_id=plan["task_id"],
        owner_agent_id=plan["owner_id"],
        runtime_instance_id=archive["runtime_instance_id"],
        repository=archive["repository"],
        issue=archive["issue"],
        callsign=archive["prior_callsign"],
    )
    issue_actions = [
        action
        for action in plan["actions"]
        if action["action_kind"] == "issue_close"
    ]
    if len(issue_actions) != 1 or issue_actions[0]["expected_identity"] != {
        "repository": archive["repository"],
        "issue": archive["issue"],
        "state": "open",
    }:
        raise StorageRefusal(
            "issue_binding_mismatch",
            "thread archive requires one exact owning-issue close action",
        )
    lineage = store.connection.execute(
        "SELECT * FROM thread_lineages WHERE thread_identity=?",
        (archive["thread_identity"],),
    ).fetchone()
    if lineage is None:
        store.connection.execute(
            """
            INSERT INTO thread_lineages
              (lineage_id,provider_kind,thread_identity,resume_capabilities_json,policy_digest,
               state,version,created_at,updated_at)
            VALUES(?,?,?,?,?,'archived',1,?,?)
            """,
            (
                archive["lineage_id"],
                archive["provider_kind"],
                archive["thread_identity"],
                _json(capabilities),
                archive["policy_digest"],
                plan["at"],
                plan["at"],
            ),
        )
    else:
        if (
            lineage["lineage_id"] != archive["lineage_id"]
            or lineage["provider_kind"] != archive["provider_kind"]
            or json.loads(lineage["resume_capabilities_json"]) != capabilities
            or lineage["policy_digest"] != archive["policy_digest"]
        ):
            raise StorageRefusal(
                "thread_identity_reused",
                "provider thread identity conflicts with its immutable lineage",
            )
        incarnation = store.connection.execute(
            "SELECT lineage_id,archive_id FROM thread_incarnations WHERE runtime_instance_id=?",
            (archive["runtime_instance_id"],),
        ).fetchone()
        if incarnation is None or incarnation["lineage_id"] != archive["lineage_id"]:
            raise StorageRefusal(
                "thread_identity_reused",
                "provider thread appeared outside its recorded continuation lineage",
            )
    store.connection.execute(
        """
        INSERT INTO thread_archives
          (archive_id,lineage_id,task_id,owner_agent_id,runtime_instance_id,
           cleanup_operation_id,repository,issue,branch,worktree,prior_callsign,
           instruction_digest,context_health,acceptance_json,cleanup_evidence_json,
           state,version,archived_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending_cleanup',1,?,?)
        """,
        (
            archive["archive_id"],
            archive["lineage_id"],
            plan["task_id"],
            plan["owner_id"],
            archive["runtime_instance_id"],
            plan["operation_id"],
            archive["repository"],
            archive["issue"],
            archive["branch"],
            archive["worktree"],
            archive["prior_callsign"],
            archive["instruction_digest"],
            archive["context_health"],
            _json(archive["acceptance"]),
            _json(archive["cleanup_evidence"]),
            plan["at"],
            plan["at"],
        ),
    )
    if lineage is None:
        store.connection.execute(
            """
            INSERT INTO thread_incarnations
              (lineage_id,runtime_instance_id,archive_id,continuation_operation_id,bound_at)
            VALUES(?,?,?,NULL,?)
            """,
            (
                archive["lineage_id"],
                archive["runtime_instance_id"],
                archive["archive_id"],
                plan["at"],
            ),
        )
    else:
        store.connection.execute(
            "UPDATE thread_incarnations SET archive_id=? WHERE runtime_instance_id=? AND archive_id IS NULL",
            (archive["archive_id"], archive["runtime_instance_id"]),
        )


def finalize_thread_archive_for_cleanup(
    store: Any, operation_id: str, receipt_id: str, at: str
) -> None:
    archive = store.connection.execute(
        "SELECT * FROM thread_archives WHERE cleanup_operation_id=?", (operation_id,)
    ).fetchone()
    if archive is None:
        return
    issue_receipt = store.connection.execute(
        """
        SELECT r.after_json
          FROM cleanup_actions a JOIN cleanup_action_receipts r ON r.action_id=a.action_id
         WHERE a.operation_id=? AND a.action_kind='issue_close'
        """,
        (operation_id,),
    ).fetchall()
    if len(issue_receipt) != 1 or json.loads(issue_receipt[0]["after_json"]) != {
        "repository": archive["repository"],
        "issue": archive["issue"],
        "state": "closed",
    }:
        raise StorageRefusal(
            "issue_close_receipt_missing",
            "thread archive cannot become resumable without its exact issue-close receipt",
        )
    store.connection.execute(
        """
        UPDATE thread_archives
           SET cleanup_receipt_id=?,state='available',version=version+1,updated_at=?
         WHERE archive_id=? AND state='pending_cleanup'
        """,
        (receipt_id, at, archive["archive_id"]),
    )
    store.connection.execute(
        "UPDATE thread_lineages SET state='archived',version=version+1,updated_at=? WHERE lineage_id=?",
        (at, archive["lineage_id"]),
    )


def thread_archive(store: Any, archive_id: str) -> Optional[dict[str, Any]]:
    row = store.connection.execute(
        "SELECT * FROM thread_archives WHERE archive_id=?", (archive_id,)
    ).fetchone()
    return None if row is None else _archive_value(row)


def _validate_continuation_request(
    spec: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Optional[str]]:
    required = {
        "operation_id",
        "archive_id",
        "assignment_id",
        "new_task_id",
        "new_agent_id",
        "repository",
        "issue",
        "branch",
        "worktree",
        "binding",
        "instruction_digest",
        "reconciliation_digest",
        "concrete_benefit",
        "expected_archive_version",
        "at",
    }
    identity_fields = (
        "operation_id",
        "archive_id",
        "assignment_id",
        "new_task_id",
        "new_agent_id",
        "repository",
        "branch",
        "worktree",
    )
    if (
        set(spec) != required
        or not isinstance(spec["concrete_benefit"], str)
        or spec["concrete_benefit"] not in _BENEFITS
        or any(not isinstance(spec[key], str) or not spec[key] for key in identity_fields)
        or isinstance(spec["issue"], bool)
        or not isinstance(spec["issue"], int)
        or spec["issue"] < 1
        or isinstance(spec["expected_archive_version"], bool)
        or not isinstance(spec["expected_archive_version"], int)
        or spec["expected_archive_version"] < 1
    ):
        raise StorageRefusal("continuation_invalid", "exact continuation request is incomplete")
    _time(str(spec["at"]), "continuation preparation time")
    binding = spec["binding"]
    if (
        not isinstance(binding, Mapping)
        or set(binding) != {"verified", "repository", "issue", "branch", "worktree", "head"}
        or binding.get("verified") is not True
        or not re.fullmatch(r"[0-9a-f]{40}", str(binding.get("head", "")))
        or any(binding.get(key) != spec[key] for key in ("repository", "issue", "branch", "worktree"))
    ):
        raise StorageRefusal("workspace_binding_unsafe", "new continuation worktree binding is unverified")
    try:
        if normalized_github_repository(str(binding["repository"])) != normalized_github_repository(
            str(spec["repository"])
        ):
            raise StorageRefusal(
                "workspace_binding_unsafe", "new continuation repository identity conflicts"
            )
    except StorageRefusal as exc:
        raise StorageRefusal(
            "workspace_binding_unsafe", "new continuation repository identity is unsupported"
        ) from exc
    if not _DIGEST.fullmatch(str(spec["instruction_digest"])):
        raise StorageRefusal("continuation_invalid", "current instruction digest is invalid")
    reconciliation = spec["reconciliation_digest"]
    if reconciliation is not None and not _DIGEST.fullmatch(str(reconciliation)):
        raise StorageRefusal("continuation_invalid", "instruction reconciliation digest is invalid")
    return binding, reconciliation


def _verify_closed_lineage_runtimes(
    store: Any, lineage: Mapping[str, Any]
) -> str:
    session_ref = _decode_thread_identity(
        str(lineage["provider_kind"]), str(lineage["thread_identity"])
    )
    runtimes = store.connection.execute(
        "SELECT runtime_instance_id,status FROM runtime_instances WHERE session_ref=? ORDER BY runtime_instance_id",
        (session_ref,),
    ).fetchall()
    linked = {
        row["runtime_instance_id"]
        for row in store.connection.execute(
            "SELECT runtime_instance_id FROM thread_incarnations WHERE lineage_id=?",
            (lineage["lineage_id"],),
        )
    }
    if (
        not runtimes
        or {row["runtime_instance_id"] for row in runtimes} != linked
        or any(row["status"] != "closed" for row in runtimes)
    ):
        raise StorageRefusal(
            "thread_identity_reused",
            "provider thread identity is ambiguous, reused, or still live",
        )
    return session_ref


def _continuation_archive_claim(
    store: Any,
    spec: Mapping[str, Any],
    reconciliation: Optional[str],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    archive = store.connection.execute(
        "SELECT * FROM thread_archives WHERE archive_id=?", (spec["archive_id"],)
    ).fetchone()
    if (
        archive is None
        or archive["state"] != "available"
        or int(archive["version"]) != int(spec["expected_archive_version"])
    ):
        raise StorageRefusal(
            "continuation_conflict", "thread archive is not exclusively claimable"
        )
    _require_resumable_issue_binding(
        store,
        task_id=archive["task_id"],
        owner_agent_id=archive["owner_agent_id"],
        runtime_instance_id=archive["runtime_instance_id"],
        repository=archive["repository"],
        issue=int(archive["issue"]),
        callsign=archive["prior_callsign"],
    )
    lineage = store.connection.execute(
        "SELECT * FROM thread_lineages WHERE lineage_id=?", (archive["lineage_id"],)
    ).fetchone()
    capabilities = json.loads(lineage["resume_capabilities_json"])
    if not all(
        capabilities.get(key) is True
        for key in ("durable", "exact_resume", "safe_worktree_rebind")
    ):
        raise StorageRefusal(
            "resume_unsupported",
            "provider does not declare exact durable resume and safe rebinding",
        )
    if archive["context_health"] != "healthy":
        raise StorageRefusal(
            "resume_context_unhealthy", "archived provider context is not healthy"
        )
    if (
        normalized_github_repository(archive["repository"])
        != normalized_github_repository(spec["repository"])
        or int(archive["issue"]) != int(spec["issue"])
    ):
        raise StorageRefusal(
            "issue_binding_mismatch", "continuation does not target the archived owning issue"
        )
    if archive["instruction_digest"] != spec["instruction_digest"] and reconciliation is None:
        raise StorageRefusal(
            "instruction_drift_unreconciled",
            "changed governing instructions require an explicit reconciliation digest",
        )
    close_rows = store.connection.execute(
        """
        SELECT r.after_json
          FROM cleanup_actions a JOIN cleanup_action_receipts r ON r.action_id=a.action_id
         WHERE a.operation_id=? AND a.action_kind='issue_close'
        """,
        (archive["cleanup_operation_id"],),
    ).fetchall()
    if len(close_rows) != 1 or json.loads(close_rows[0]["after_json"]).get("state") != "closed":
        raise StorageRefusal(
            "issue_close_receipt_missing", "archived task has no exact owning-issue close receipt"
        )
    _verify_closed_lineage_runtimes(store, lineage)
    return archive, lineage


def _verify_fresh_continuation_successor(store: Any, spec: Mapping[str, Any]) -> None:
    if store.connection.execute(
        """
        SELECT 1 FROM agent_instances
         WHERE retired_at IS NULL
           AND (worktree=? OR (repository=? AND branch=?))
        """,
        (spec["worktree"], spec["repository"], spec["branch"]),
    ).fetchone() is not None:
        raise StorageRefusal(
            "workspace_binding_unsafe", "new continuation worktree is already bound"
        )
    if any(
        store.connection.execute(query, (value,)).fetchone() is not None
        for query, value in (
            ("SELECT 1 FROM task_assignments WHERE task_assignment_id=?", spec["assignment_id"]),
            ("SELECT 1 FROM tasks WHERE task_id=?", spec["new_task_id"]),
            ("SELECT 1 FROM agent_instances WHERE agent_id=?", spec["new_agent_id"]),
        )
    ):
        raise StorageRefusal(
            "continuation_conflict",
            "successor assignment, task, or agent identity is not fresh",
        )
    active = store.connection.execute(
        """
        SELECT operation_id FROM continuation_operations
         WHERE archive_id=? AND state IN ('prepared','reopening_issue','issue_reopened','launching')
        """,
        (spec["archive_id"],),
    ).fetchone()
    if active is not None:
        raise StorageRefusal(
            "continuation_conflict", "thread archive already has an exclusive continuation claim"
        )


def prepare_continuation(store: Any, spec: Mapping[str, Any]) -> dict[str, Any]:
    binding, reconciliation = _validate_continuation_request(spec)
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM continuation_operations WHERE operation_id=?",
                (spec["operation_id"],),
            ).fetchone()
            if existing is not None:
                comparable = (
                    "archive_id",
                    "assignment_id",
                    "new_task_id",
                    "new_agent_id",
                    "repository",
                    "issue",
                    "branch",
                    "worktree",
                    "instruction_digest",
                    "reconciliation_digest",
                    "concrete_benefit",
                )
                if any(existing[key] != spec[key] for key in comparable) or existing[
                    "binding_digest"
                ] != _digest(binding):
                    raise StorageRefusal(
                        "continuation_conflict",
                        "continuation retry differs from its immutable request",
                    )
                result = _operation_value(store, existing)
                result["idempotent"] = True
                return result
            archive, lineage = _continuation_archive_claim(
                store, spec, reconciliation
            )
            _verify_fresh_continuation_successor(store, spec)
            store.connection.execute(
                """
                INSERT INTO continuation_operations
                  (operation_id,archive_id,assignment_id,new_task_id,new_agent_id,repository,
                   issue,branch,worktree,binding_digest,instruction_digest,reconciliation_digest,
                   concrete_benefit,state,version,fence,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',1,0,?,?)
                """,
                (
                    spec["operation_id"],
                    spec["archive_id"],
                    spec["assignment_id"],
                    spec["new_task_id"],
                    spec["new_agent_id"],
                    spec["repository"],
                    spec["issue"],
                    spec["branch"],
                    spec["worktree"],
                    _digest(binding),
                    spec["instruction_digest"],
                    reconciliation,
                    spec["concrete_benefit"],
                    spec["at"],
                    spec["at"],
                ),
            )
            store.connection.execute(
                "UPDATE thread_archives SET state='claimed',version=version+1,updated_at=? WHERE archive_id=? AND version=?",
                (spec["at"], spec["archive_id"], spec["expected_archive_version"]),
            )
            store.connection.execute(
                "UPDATE thread_lineages SET state='claimed',version=version+1,updated_at=? WHERE lineage_id=?",
                (spec["at"], archive["lineage_id"]),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,task_id,entity_version,event_type,status,update_text,occurred_at,
                   detail_json,aggregate_kind,aggregate_id)
                VALUES(?,?,?,'continuation_decided','prepared','exact archived thread claimed',?,?,
                       'continuation',?)
                """,
                (
                    f"event:{spec['operation_id']}:prepared",
                    archive["task_id"],
                    int(archive["version"]) + 1,
                    spec["at"],
                    _json(
                        {
                            "archive_id": spec["archive_id"],
                            "outcome": "resume",
                            "reason_code": spec["concrete_benefit"],
                        }
                    ),
                    spec["operation_id"],
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "exact continuation preparation conflicted with canonical state"
        ) from exc
    row = store.connection.execute(
        "SELECT * FROM continuation_operations WHERE operation_id=?", (spec["operation_id"],)
    ).fetchone()
    result = _operation_value(store, row)
    result["idempotent"] = False
    return result


def continuation_status(store: Any, operation_id: str) -> Optional[dict[str, Any]]:
    row = store.connection.execute(
        "SELECT * FROM continuation_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    return None if row is None else _operation_value(store, row)


def continuation_for_assignment(store: Any, assignment_id: str) -> Optional[dict[str, Any]]:
    row = store.connection.execute(
        "SELECT * FROM continuation_operations WHERE assignment_id=?", (assignment_id,)
    ).fetchone()
    return None if row is None else _operation_value(store, row)


def claim_issue_reopen(
    store: Any,
    operation_id: str,
    expected_version: int,
    expected_fence: int,
    executor_id: str,
    leased_until: str,
    at: str,
) -> dict[str, Any]:
    now = _time(at, "issue reopen claim time")
    expiry = _time(leased_until, "issue reopen lease expiry")
    if not executor_id or expiry <= now:
        raise StorageRefusal("continuation_lease_invalid", "issue reopen lease is invalid")
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT * FROM continuation_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise StorageRefusal("continuation_unknown", "continuation operation does not exist")
            if row["state"] in {"issue_reopened", "launching", "resumed"}:
                return {
                    "operation_id": operation_id,
                    "state": row["state"],
                    "version": int(row["version"]),
                    "fence": int(row["fence"]),
                    "idempotent": True,
                }
            if int(row["version"]) != expected_version or int(row["fence"]) != expected_fence:
                raise StorageRefusal("continuation_conflict", "continuation version or fence changed")
            if row["state"] == "reopening_issue" and row["leased_until"] is not None:
                if _time(row["leased_until"], "stored issue reopen lease") > now:
                    raise StorageRefusal(
                        "continuation_busy", "issue reopen has an unexpired executor lease", retryable=True
                    )
            if row["state"] not in {"prepared", "reopening_issue"}:
                raise StorageRefusal("continuation_conflict", "continuation is not issue-reopen eligible")
            version = expected_version + 1
            fence = expected_fence + 1
            store.connection.execute(
                """
                UPDATE continuation_operations
                   SET state='reopening_issue',version=?,fence=?,executor_id=?,leased_until=?,updated_at=?
                 WHERE operation_id=? AND version=? AND fence=?
                """,
                (version, fence, executor_id, leased_until, at, operation_id, expected_version, expected_fence),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "issue reopen claim conflicted") from exc
    return {
        "operation_id": operation_id,
        "state": "reopening_issue",
        "version": version,
        "fence": fence,
        "idempotent": False,
    }


def record_issue_reopen(
    store: Any,
    operation_id: str,
    expected_version: int,
    fence: int,
    outcome: str,
    receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    if outcome not in {"applied", "already_applied"}:
        raise StorageRefusal("continuation_invalid", "issue reopen outcome is invalid")
    payload = {"outcome": outcome, "receipt": dict(receipt)}
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT * FROM continuation_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise StorageRefusal("continuation_unknown", "continuation operation does not exist")
            if row["state"] == "issue_reopened":
                if json.loads(row["issue_receipt_json"]) != payload:
                    raise StorageRefusal("continuation_receipt_conflict", "issue reopen receipt changed")
                return {
                    "operation_id": operation_id,
                    "state": "issue_reopened",
                    "version": int(row["version"]),
                    "fence": int(row["fence"]),
                    "idempotent": True,
                }
            if (
                row["state"] != "reopening_issue"
                or int(row["version"]) != expected_version
                or int(row["fence"]) != fence
            ):
                raise StorageRefusal("continuation_conflict", "issue reopen receipt has a stale fence")
            version = expected_version + 1
            store.connection.execute(
                """
                UPDATE continuation_operations
                   SET state='issue_reopened',version=?,executor_id=NULL,leased_until=NULL,
                       issue_receipt_json=?,updated_at=?
                 WHERE operation_id=? AND state='reopening_issue' AND version=? AND fence=?
                """,
                (version, _json(payload), at, operation_id, expected_version, fence),
            )
            archive = store.connection.execute(
                "SELECT task_id FROM thread_archives WHERE archive_id=?", (row["archive_id"],)
            ).fetchone()
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,task_id,entity_version,event_type,status,update_text,occurred_at,
                   detail_json,aggregate_kind,aggregate_id)
                VALUES(?,?,?,'issue_reopened','issue_reopened','exact owning issue reopened',?,?,
                       'continuation',?)
                """,
                (
                    f"event:{operation_id}:issue-reopened",
                    archive["task_id"],
                    version,
                    at,
                    _json({"outcome": outcome}),
                    operation_id,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "issue reopen receipt conflicted") from exc
    return {
        "operation_id": operation_id,
        "state": "issue_reopened",
        "version": version,
        "fence": fence,
        "idempotent": False,
    }


def mark_continuation_launching(
    store: Any, operation_id: str, expected_version: int, at: str
) -> dict[str, Any]:
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT state,version FROM continuation_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise StorageRefusal("continuation_unknown", "continuation operation does not exist")
            if row["state"] in {"launching", "resumed"}:
                return {
                    "operation_id": operation_id,
                    "state": row["state"],
                    "version": int(row["version"]),
                    "idempotent": True,
                }
            if row["state"] != "issue_reopened" or int(row["version"]) != expected_version:
                raise StorageRefusal("continuation_conflict", "continuation is not launchable")
            version = expected_version + 1
            store.connection.execute(
                "UPDATE continuation_operations SET state='launching',version=?,updated_at=? WHERE operation_id=? AND version=?",
                (version, at, operation_id, expected_version),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "continuation launch fence conflicted") from exc
    return {"operation_id": operation_id, "state": "launching", "version": version, "idempotent": False}


def authorize_resumed_runtime(
    store: Any, assignment_id: str, receipt: Mapping[str, Any]
) -> Optional[dict[str, Any]]:
    operation = store.connection.execute(
        "SELECT * FROM continuation_operations WHERE assignment_id=?", (assignment_id,)
    ).fetchone()
    if operation is None:
        return None
    if operation["state"] != "launching":
        raise StorageRefusal("continuation_conflict", "exact continuation is not at its launch fence")
    archive = store.connection.execute(
        "SELECT * FROM thread_archives WHERE archive_id=?", (operation["archive_id"],)
    ).fetchone()
    lineage = store.connection.execute(
        "SELECT * FROM thread_lineages WHERE lineage_id=?", (archive["lineage_id"],)
    ).fetchone()
    session_ref = _decode_thread_identity(lineage["provider_kind"], lineage["thread_identity"])
    if (
        receipt.get("thread_id") != session_ref
        or receipt.get("task_id") != operation["new_task_id"]
        or receipt.get("champion_agent_id") != operation["new_agent_id"]
        or receipt.get("repository") != operation["repository"]
        or receipt.get("issue") != operation["issue"]
        or receipt.get("branch") != operation["branch"]
        or receipt.get("worktree") != operation["worktree"]
    ):
        raise StorageRefusal(
            "thread_identity_ambiguous", "resumed runtime receipt is not the exact claimed thread and binding"
        )
    _verify_closed_lineage_runtimes(store, lineage)
    return {
        "operation": dict(operation),
        "archive": dict(archive),
        "lineage": dict(lineage),
    }


def complete_resumed_runtime(
    store: Any,
    authorization: Mapping[str, Any],
    runtime_instance_id: str,
    callsign: str,
    at: str,
) -> None:
    operation = authorization["operation"]
    archive = authorization["archive"]
    lineage = authorization["lineage"]
    version = int(operation["version"]) + 1
    store.connection.execute(
        """
        UPDATE continuation_operations
           SET state='resumed',version=?,runtime_instance_id=?,callsign=?,updated_at=?
         WHERE operation_id=? AND state='launching' AND version=?
        """,
        (version, runtime_instance_id, callsign, at, operation["operation_id"], operation["version"]),
    )
    store.connection.execute(
        "UPDATE thread_archives SET state='resumed',version=version+1,updated_at=? WHERE archive_id=? AND state='claimed'",
        (at, archive["archive_id"]),
    )
    store.connection.execute(
        "UPDATE thread_lineages SET state='active',version=version+1,updated_at=? WHERE lineage_id=?",
        (at, lineage["lineage_id"]),
    )
    store.connection.execute(
        """
        INSERT INTO thread_incarnations
          (lineage_id,runtime_instance_id,archive_id,continuation_operation_id,bound_at)
        VALUES(?,?,NULL,?,?)
        """,
        (lineage["lineage_id"], runtime_instance_id, operation["operation_id"], at),
    )
    store.connection.execute(
        """
        INSERT INTO events
          (event_id,task_id,entity_version,event_type,status,update_text,occurred_at,
           detail_json,aggregate_kind,aggregate_id)
        VALUES(?,?,?,'thread_resumed','resumed','exact provider thread resumed',?,?,
               'continuation',?)
        """,
        (
            f"event:{operation['operation_id']}:resumed",
            operation["new_task_id"],
            version,
            at,
            _json({"archive_id": archive["archive_id"], "lineage_id": lineage["lineage_id"]}),
            operation["operation_id"],
        ),
    )


__all__ = [
    "authorize_resumed_runtime",
    "claim_issue_reopen",
    "complete_resumed_runtime",
    "continuation_for_assignment",
    "continuation_status",
    "finalize_thread_archive_for_cleanup",
    "mark_continuation_launching",
    "prepare_continuation",
    "record_issue_reopen",
    "record_thread_archive_for_cleanup",
    "thread_archive",
]
