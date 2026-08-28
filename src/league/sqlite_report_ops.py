"""Indexed evidence recording and deterministic bounded League reports."""

from __future__ import annotations

import base64
import hashlib
import heapq
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .privacy import validate_final_rendered_payload
from .storage_types import StorageRefusal


REPORT_SCHEMA = "league.report.v1"
MAX_REPORT_FACTS = 100_000
MAX_REPORT_PAGE = 1_000
MAX_REPORT_GAPS = 200
MAX_REPAIR_GROUPS = 1_000
TYPICAL_DAY_LATENCY_BUDGET_MS = 500
LARGE_HISTORY_LATENCY_BUDGET_MS = 3_000
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
SAFE_ACTION = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UUID_TEXT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
EVIDENCE_KINDS = frozenset(
    {
        "issue",
        "commit",
        "pull_request",
        "check",
        "merge",
        "install",
        "deployment",
        "smoke",
        "rollback",
        "teardown",
        "resource",
        "authority",
        "handoff",
        "continuation",
        "repair",
    }
)
EVIDENCE_STATES = frozenset({"pending", "succeeded", "failed", "cancelled", "blocked"})
VERIFICATIONS = frozenset({"verified", "unverified", "unknown"})
REPAIR_PHASES = frozenset({"failure", "attempt", "fix", "final"})
TERMINAL_REQUEST_STATES = frozenset({"answered", "cancelled"})
TERMINAL_TASK_STATES = frozenset(
    {"completed", "complete", "cancelled", "canceled", "failed", "rejected"}
)
SETTLED_CLEANUP_STATES = frozenset({"not_due", "completed", "cleanup_completed"})
SETTLED_ASSIGNMENT_STATES = frozenset({"active"})
SETTLED_OUTBOX_STATES = frozenset({"delivered", "cancelled"})
PUBLIC_ID_PREFIXES = frozenset(
    {
        "project", "request", "task", "event", "report", "evidence", "repair",
        "actor", "squad", "assignment", "resource", "cleanup", "operation", "callsign",
    }
)


def _stable_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("invalid_report_time", f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("invalid_report_time", f"{label} must include an offset")
    return parsed.astimezone(timezone.utc)


def canonical_timestamp(value: str, label: str) -> str:
    return _timestamp(value, label).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise StorageRefusal("invalid_report_timezone", "report timezone is unknown") from exc
    return value


def _text(value: Any, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise StorageRefusal("invalid_evidence", f"{field} must be text")
    item = value.strip()
    if not item or len(item) > maximum or "\x00" in item:
        raise StorageRefusal("invalid_evidence", f"{field} is empty or exceeds its bound")
    return item


def _optional_id(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    item = _text(value, field, 192)
    if not SAFE_ID.fullmatch(item):
        raise StorageRefusal("invalid_evidence", f"{field} is not an opaque League identifier")
    return item


def _public_id(kind: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    item = str(value)
    prefix = item.split(":", 1)[0] if ":" in item else ""
    if prefix in PUBLIC_ID_PREFIXES and SAFE_ID.fullmatch(item):
        return item
    if SAFE_ID.fullmatch(item) and UUID_TEXT.search(item) is None:
        safe_kind = kind if kind in PUBLIC_ID_PREFIXES else "evidence"
        return f"{safe_kind}:{item}"
    digest = hashlib.sha256(item.encode("utf-8")).hexdigest()[:24]
    safe_kind = kind if kind in PUBLIC_ID_PREFIXES else "evidence"
    return f"{safe_kind}:{digest}"


def _public_details(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {item_key: _public_details(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_public_details(item, key) for item in value]
    if value is None or not isinstance(value, str):
        return value
    kind_by_key = {
        "prompt_id": "evidence",
        "request_id": "request",
        "task_id": "task",
        "coordinator_actor_id": "actor",
        "predecessor_agent_id": "actor",
        "successor_agent_id": "actor",
        "callsign_assignment_id": "assignment",
        "snapshot_id": "evidence",
        "operation_id": "operation",
        "resource_id": "resource",
        "subject_id": "evidence",
    }
    if key in kind_by_key:
        return _public_id(kind_by_key[key], value)
    return value


def record_activity_evidence(store: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "evidence_id",
        "evidence_kind",
        "action",
        "owner_agent_id",
        "squad_id",
        "project_id",
        "request_id",
        "task_id",
        "state",
        "verification",
        "summary",
        "public_url",
        "object_hash",
        "local_evidence_ref",
        "local_evidence",
        "local_evidence_hash",
        "stable_repair_id",
        "repair_phase",
        "root_cause_tag",
        "owning_issue_url",
        "required_for_completion",
        "occurred_at",
    }
    if set(evidence) != expected:
        raise StorageRefusal("invalid_evidence", "activity evidence fields are incomplete or unsupported")
    evidence_id = _optional_id(evidence["evidence_id"], "evidence_id")
    assert evidence_id is not None
    kind = evidence["evidence_kind"]
    state = evidence["state"]
    verification = evidence["verification"]
    action = _text(evidence["action"], "action", 64)
    if (
        kind not in EVIDENCE_KINDS
        or state not in EVIDENCE_STATES
        or verification not in VERIFICATIONS
        or not SAFE_ACTION.fullmatch(action)
    ):
        raise StorageRefusal("invalid_evidence", "activity evidence kind, state, or action is invalid")
    summary = _text(evidence["summary"], "summary")
    public_url = evidence["public_url"]
    if public_url is not None:
        public_url = _text(public_url, "public_url", 2048)
    owning_issue_url = evidence["owning_issue_url"]
    if owning_issue_url is not None:
        owning_issue_url = _text(owning_issue_url, "owning_issue_url", 2048)
    approved_urls = tuple(
        item for item in (public_url, owning_issue_url) if item is not None
    )
    validate_final_rendered_payload(
        "\n".join((summary, *approved_urls)),
        destination_visibility="public",
        approved_urls=approved_urls,
        field="activity_evidence.summary",
    )
    object_hash = evidence["object_hash"]
    if object_hash is not None and not SHA256.fullmatch(str(object_hash)):
        raise StorageRefusal("invalid_evidence", "object_hash must be SHA-256")
    local_ref = _optional_id(evidence["local_evidence_ref"], "local_evidence_ref")
    local_evidence = evidence["local_evidence"]
    local_hash = evidence["local_evidence_hash"]
    if not ((local_ref is None) == (local_hash is None) == (local_evidence is None)):
        raise StorageRefusal("invalid_evidence", "local evidence, reference, and hash must be paired")
    local_json = None
    if local_evidence is not None:
        try:
            local_payload = _stable_bytes(local_evidence)
        except (TypeError, ValueError) as exc:
            raise StorageRefusal("invalid_evidence", "local evidence must be bounded JSON") from exc
        if len(local_payload) > 1_000_000:
            raise StorageRefusal("invalid_evidence", "local evidence exceeds its byte bound")
        local_json = local_payload.decode("utf-8")
    if local_hash is not None and not SHA256.fullmatch(str(local_hash)):
        raise StorageRefusal("invalid_evidence", "local_evidence_hash must be SHA-256")
    if local_hash is not None and hashlib.sha256(local_payload).hexdigest() != local_hash:
        raise StorageRefusal("invalid_evidence", "local evidence hash does not match canonical bytes")
    repair_id = _optional_id(evidence["stable_repair_id"], "stable_repair_id")
    repair_phase = evidence["repair_phase"]
    if repair_phase is not None and repair_phase not in REPAIR_PHASES:
        raise StorageRefusal("invalid_evidence", "repair phase is unsupported")
    if kind == "repair" and repair_id is None:
        raise StorageRefusal("invalid_evidence", "repair evidence requires a stable identifier")
    required = evidence["required_for_completion"]
    if not isinstance(required, bool):
        raise StorageRefusal("invalid_evidence", "required_for_completion must be boolean")
    occurred_at = canonical_timestamp(evidence["occurred_at"], "occurred_at")
    row = {
        "evidence_id": evidence_id,
        "evidence_kind": kind,
        "action": action,
        "owner_agent_id": _optional_id(evidence["owner_agent_id"], "owner_agent_id"),
        "squad_id": _optional_id(evidence["squad_id"], "squad_id"),
        "project_id": _optional_id(evidence["project_id"], "project_id"),
        "request_id": _optional_id(evidence["request_id"], "request_id"),
        "task_id": _optional_id(evidence["task_id"], "task_id"),
        "state": state,
        "verification": verification,
        "summary": summary,
        "summary_classification": "outbound_safe",
        "public_url": public_url,
        "object_hash": object_hash,
        "local_evidence_ref": local_ref,
        "local_evidence_json": local_json,
        "local_evidence_hash": local_hash,
        "local_evidence_classification": "local_only",
        "stable_repair_id": repair_id,
        "repair_phase": repair_phase,
        "root_cause_tag": _optional_id(evidence["root_cause_tag"], "root_cause_tag"),
        "owning_issue_url": owning_issue_url,
        "required_for_completion": int(required),
        "occurred_at": occurred_at,
    }
    columns = tuple(row)
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM activity_evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if existing is not None:
                observed = {column: existing[column] for column in columns}
                if observed != row:
                    raise StorageRefusal("evidence_conflict", "activity evidence identity already has different facts")
                return {**_public_evidence(row), "idempotent": True}
            store.connection.execute(
                f"INSERT INTO activity_evidence({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "activity evidence write conflicted") from exc
    return {**_public_evidence(row), "idempotent": False}


def _public_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key
        not in {
            "local_evidence_ref",
            "local_evidence_json",
            "local_evidence_classification",
            "summary_classification",
        }
    } | {"local_evidence_present": row["local_evidence_ref"] is not None}


def _owner(store: Any, owner_id: Optional[str]) -> Optional[dict[str, Any]]:
    if owner_id is None:
        return None
    cache = getattr(store, "_report_owner_cache", None)
    if cache is None:
        cache = {}
        setattr(store, "_report_owner_cache", cache)
    if owner_id in cache:
        return cache[owner_id]
    row = store.connection.execute(
        "SELECT agent_id,callsign,role FROM agent_instances WHERE agent_id=?", (owner_id,)
    ).fetchone()
    if row is None:
        result = {"actor_id": _public_id("actor", owner_id), "callsign": None, "role": None}
    else:
        result = {
            "actor_id": _public_id("actor", row["agent_id"]),
            "callsign": row["callsign"],
            "role": row["role"],
        }
    cache[owner_id] = result
    return result


def _fact(
    store: Any,
    *,
    fact_id: str,
    occurred_at: str,
    category: str,
    action: str,
    owner_id: Optional[str],
    subject_kind: str,
    subject_id: str,
    state: Optional[str],
    verification: str = "verified",
    summary: str = "<local-only>",
    details: Optional[dict[str, Any]] = None,
    evidence: Optional[list[dict[str, Any]]] = None,
    gaps: Optional[list[str]] = None,
    project_id: Optional[str] = None,
    request_id: Optional[str] = None,
    task_id: Optional[str] = None,
    squad_id: Optional[str] = None,
    repair_id: Optional[str] = None,
    repair_phase: Optional[str] = None,
) -> dict[str, Any]:
    public_subject = _public_id(
        {
            "requests": "request",
            "tasks": "task",
            "events": "event",
            "task-assignments": "assignment",
            "task-resources": "resource",
            "cleanup-obligations": "cleanup",
        }.get(subject_kind, "evidence"),
        subject_id,
    )
    return {
        "fact_id": _public_id("evidence", fact_id),
        "occurred_at": occurred_at,
        "category": category,
        "action": action,
        "owner": _owner(store, owner_id),
        "subject": {"kind": subject_kind, "id": public_subject},
        "state": state,
        "verification": verification,
        "summary": summary,
        "details": _public_details(details or {}),
        "evidence": evidence
        or [{"kind": "canonical", "ref": f"league://{subject_kind}/{public_subject}"}],
        "gaps": sorted(gaps or []),
        "scope": {
            "project_id": project_id,
            "request_id": request_id,
            "task_id": task_id,
            "squad_id": squad_id,
        },
        "repair": {"stable_id": _public_id("repair", repair_id), "phase": repair_phase}
        if repair_id is not None
        else None,
    }


def _time_clause(column: str, inclusive: bool) -> str:
    return f"{column} {'>=' if inclusive else '>'} ? AND {column} <= ?"


def _query(store: Any, statement: str, parameters: tuple[Any, ...]) -> Iterator[Any]:
    yield from store.connection.execute(statement, parameters)


def _source_facts(
    store: Any, from_at: str, to_at: str, inclusive: bool, local: bool
) -> list[Iterator[dict[str, Any]]]:
    sources: list[Iterator[dict[str, Any]]] = []
    time_parameters = (from_at, to_at)

    def prompts() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT i.prompt_item_id,i.prompt_id,i.disposition,i.summary,p.created_at,
                   p.intake_actor_id,
                   (SELECT rs.request_id FROM request_sources rs
                     WHERE rs.prompt_item_id=i.prompt_item_id ORDER BY rs.request_id LIMIT 1) request_id
              FROM prompt_items i JOIN prompts p ON p.prompt_id=i.prompt_id
             WHERE {_time_clause('p.created_at', inclusive)}
             ORDER BY p.created_at,i.prompt_item_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"prompt-item:{row['prompt_item_id']}",
                occurred_at=row["created_at"],
                category="prompt",
                action=row["disposition"],
                owner_id=row["intake_actor_id"],
                subject_kind="prompt-items",
                subject_id=row["prompt_item_id"],
                state=row["disposition"],
                summary=row["summary"] if local else "<local-only>",
                details={"prompt_id": row["prompt_id"], "request_id": row["request_id"]},
                request_id=row["request_id"],
            )

    def requests() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT request_id,owner_agent_id,state,execution_mode,summary,updated_at
              FROM requests WHERE {_time_clause('updated_at', inclusive)}
             ORDER BY updated_at,request_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"request:{row['request_id']}",
                occurred_at=row["updated_at"],
                category="request",
                action="state",
                owner_id=row["owner_agent_id"],
                subject_kind="requests",
                subject_id=row["request_id"],
                state=row["state"],
                summary=row["summary"] if local else "<local-only>",
                details={"execution_mode": row["execution_mode"] or "<unknown>"},
                gaps=[] if row["execution_mode"] is not None else ["execution_mode"],
                request_id=row["request_id"],
            )

    def dispatches() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT d.*,r.owner_agent_id FROM request_dispatches d
              JOIN requests r ON r.request_id=d.request_id
             WHERE {_time_clause('d.decided_at', inclusive)}
             ORDER BY d.decided_at,d.dispatch_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"dispatch:{row['dispatch_id']}",
                occurred_at=row["decided_at"],
                category="direct_work" if row["execution_mode"] == "direct" else "dispatch",
                action=row["execution_mode"],
                owner_id=row["owner_agent_id"],
                subject_kind="dispatches",
                subject_id=row["dispatch_id"],
                state="recorded",
                details={
                    "request_id": row["request_id"],
                    "execution_mode": row["execution_mode"],
                    "requested_model": row["requested_model"] or "<unknown>",
                    "requested_effort": row["requested_effort"] or "<unknown>",
                    "explicit_route": "recorded" if row["explicit_route"] else None,
                },
                request_id=row["request_id"],
            )

    def tasks() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT task_id,project_id,request_id,coordinator_agent_id,champion_agent_id,
                   state,summary,updated_at FROM tasks
             WHERE {_time_clause('updated_at', inclusive)}
             ORDER BY updated_at,task_id
        """
        for row in _query(store, statement, time_parameters):
            owner_id = row["champion_agent_id"] or row["coordinator_agent_id"]
            yield _fact(
                store,
                fact_id=f"task:{row['task_id']}",
                occurred_at=row["updated_at"],
                category="task",
                action="state",
                owner_id=owner_id,
                subject_kind="tasks",
                subject_id=row["task_id"],
                state=row["state"],
                summary=row["summary"] if local else "<local-only>",
                project_id=row["project_id"],
                request_id=row["request_id"],
                task_id=row["task_id"],
            )

    def assignments() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT a.task_assignment_id,a.task_id,a.request_id,a.coordinator_agent_id,
                   a.champion_agent_id,a.callsign,a.state,a.created_at,r.harness_kind,r.backend_kind,
                   m.model,m.effort,t.project_id
              FROM task_assignments a
              JOIN tasks t ON t.task_id=a.task_id
              LEFT JOIN runtime_instances r ON r.runtime_instance_id=a.runtime_instance_id
              LEFT JOIN model_routing_decisions m ON m.subject_kind='task'
                    AND m.subject_id=a.task_id
                    AND m.chosen_at<=?
                    AND m.chosen_at=(SELECT MAX(m2.chosen_at) FROM model_routing_decisions m2
                                      WHERE m2.subject_kind='task' AND m2.subject_id=a.task_id
                                        AND m2.chosen_at<=?)
             WHERE {_time_clause('a.created_at', inclusive)}
             ORDER BY a.created_at,a.task_assignment_id
        """
        for row in _query(store, statement, (to_at, to_at, *time_parameters)):
            gaps = []
            if row["harness_kind"] is None or row["backend_kind"] is None:
                gaps.append("runtime_harness")
            if row["model"] is None or row["effort"] is None:
                gaps.append("model_effort")
            yield _fact(
                store,
                fact_id=f"assignment:{row['task_assignment_id']}",
                occurred_at=row["created_at"],
                category="champion_assignment",
                action="assigned",
                owner_id=row["champion_agent_id"],
                subject_kind="task-assignments",
                subject_id=row["task_assignment_id"],
                state=row["state"],
                verification="verified" if not gaps else "unverified",
                details={
                    "task_id": row["task_id"],
                    "request_id": row["request_id"],
                    "callsign": row["callsign"],
                    "coordinator_actor_id": row["coordinator_agent_id"],
                    "harness": row["harness_kind"] or "<unknown>",
                    "backend": row["backend_kind"] or "<unknown>",
                    "model": row["model"] or "<unknown>",
                    "effort": row["effort"] or "<unknown>",
                },
                gaps=gaps,
                project_id=row["project_id"],
                request_id=row["request_id"],
                task_id=row["task_id"],
            )

    def callsign_history() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT h.*,t.project_id,t.request_id FROM (
              SELECT callsign_assignment_id,callsign,agent_id,role,scope_kind,scope_id,
                     queue_version,acceptance_digest,release_receipt_digest,
                     failure_receipt_digest,'reserved' action,'reserved' fact_state,
                     reserved_at occurred_at
                FROM callsign_assignments
               WHERE {_time_clause('reserved_at', inclusive)}
              UNION ALL
              SELECT callsign_assignment_id,callsign,agent_id,role,scope_kind,scope_id,
                     queue_version,acceptance_digest,release_receipt_digest,
                     failure_receipt_digest,'activated','active',activated_at
                FROM callsign_assignments
               WHERE activated_at IS NOT NULL AND {_time_clause('activated_at', inclusive)}
              UNION ALL
              SELECT callsign_assignment_id,callsign,agent_id,role,scope_kind,scope_id,
                     queue_version,acceptance_digest,release_receipt_digest,
                     failure_receipt_digest,state,state,released_at
                FROM callsign_assignments
               WHERE released_at IS NOT NULL AND {_time_clause('released_at', inclusive)}
            ) h
            LEFT JOIN tasks t ON h.scope_kind='task' AND t.task_id=h.scope_id
            ORDER BY h.occurred_at,h.callsign_assignment_id,h.action
        """
        parameters = (*time_parameters, *time_parameters, *time_parameters)
        for row in _query(store, statement, parameters):
            yield _fact(
                store,
                fact_id=f"callsign-assignment:{row['callsign_assignment_id']}:{row['action']}",
                occurred_at=row["occurred_at"],
                category="callsign_assignment",
                action=row["action"],
                owner_id=row["agent_id"],
                subject_kind="callsign-assignments",
                subject_id=row["callsign_assignment_id"],
                state=row["fact_state"],
                details={
                    "callsign": row["callsign"],
                    "role": row["role"],
                    "scope_kind": row["scope_kind"],
                    "scope_id": _public_id(
                        {"squad": "squad", "task": "task", "worker": "actor"}[row["scope_kind"]],
                        row["scope_id"],
                    ),
                    "queue_version": int(row["queue_version"]),
                    "acceptance_digest": row["acceptance_digest"],
                    "release_receipt_digest": row["release_receipt_digest"],
                    "failure_receipt_digest": row["failure_receipt_digest"],
                },
                project_id=row["project_id"],
                request_id=row["request_id"],
                task_id=row["scope_id"] if row["scope_kind"] == "task" else None,
                squad_id=row["scope_id"] if row["scope_kind"] == "squad" else None,
            )

    def rollovers() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT operation_id,squad_id,predecessor_agent_id,successor_agent_id,
                   callsign_assignment_id,state,authority_kind,authority_digest,plan_digest,
                   handoff_digest,snapshot_id,acknowledgement_digest,switch_receipt_digest,
                   cleanup_receipt_digest,updated_at
              FROM rollover_operations
             WHERE {_time_clause('updated_at', inclusive)}
             ORDER BY updated_at,operation_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"rollover:{row['operation_id']}",
                occurred_at=row["updated_at"],
                category="handoff",
                action=f"rollover_{row['state']}",
                owner_id=(
                    row["successor_agent_id"]
                    if row["state"] in {"switched", "completed"}
                    else row["predecessor_agent_id"]
                ),
                subject_kind="rollover-operations",
                subject_id=row["operation_id"],
                state=row["state"],
                details={
                    "operation_id": row["operation_id"],
                    "predecessor_agent_id": row["predecessor_agent_id"],
                    "successor_agent_id": row["successor_agent_id"],
                    "callsign_assignment_id": row["callsign_assignment_id"],
                    "authority_kind": row["authority_kind"],
                    "authority_digest": row["authority_digest"],
                    "plan_digest": row["plan_digest"],
                    "handoff_digest": row["handoff_digest"],
                    "snapshot_id": row["snapshot_id"],
                    "acknowledgement_digest": row["acknowledgement_digest"],
                    "switch_receipt_digest": row["switch_receipt_digest"],
                    "cleanup_receipt_digest": row["cleanup_receipt_digest"],
                },
                squad_id=row["squad_id"],
            )

    def owner_changes() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT e.event_id,e.squad_id,e.status,e.occurred_at,e.event_seq,
                   e.aggregate_id,o.operation_id,o.successor_agent_id
              FROM events e
              LEFT JOIN rollover_operations o
                ON e.aggregate_kind='rollover' AND o.operation_id=e.aggregate_id
             WHERE e.event_type='owner_changed'
               AND {_time_clause('e.occurred_at', inclusive)}
             ORDER BY e.occurred_at,e.event_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"owner-change:{row['event_id']}",
                occurred_at=row["occurred_at"],
                category="handoff",
                action="owner_changed",
                owner_id=row["successor_agent_id"],
                subject_kind="events",
                subject_id=row["event_id"],
                state=row["status"],
                details={
                    "operation_id": row["operation_id"] or row["aggregate_id"],
                    "event_watermark": int(row["event_seq"]),
                },
                squad_id=row["squad_id"],
            )

    def transitions() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT x.*,t.project_id,t.request_id,t.champion_agent_id,t.coordinator_agent_id
              FROM task_transitions x JOIN tasks t ON t.task_id=x.task_id
             WHERE {_time_clause('x.created_at', inclusive)}
             ORDER BY x.created_at,x.transition_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"transition:{row['transition_id']}",
                occurred_at=row["created_at"],
                category="task_transition",
                action=row["to_state"],
                owner_id=row["champion_agent_id"] or row["coordinator_agent_id"],
                subject_kind="task-transitions",
                subject_id=row["transition_id"],
                state=row["to_state"],
                summary=row["update_text"] if local else "<local-only>",
                details={"from_state": row["from_state"], "task_id": row["task_id"]},
                project_id=row["project_id"],
                request_id=row["request_id"],
                task_id=row["task_id"],
            )

    def routing() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT * FROM model_routing_decisions
             WHERE {_time_clause('chosen_at', inclusive)}
             ORDER BY chosen_at,decision_id
        """
        for row in _query(store, statement, time_parameters):
            owner_id = row["subject_id"] if row["subject_kind"] == "agent" else None
            yield _fact(
                store,
                fact_id=f"routing:{row['decision_id']}",
                occurred_at=row["chosen_at"],
                category="model_routing",
                action=row["state"],
                owner_id=owner_id,
                subject_kind="model-routing",
                subject_id=row["decision_id"],
                state=row["state"],
                details={
                    "subject_kind": row["subject_kind"],
                    "subject_id": row["subject_id"],
                    "tier": row["tier"],
                    "model": row["model"],
                    "effort": row["effort"],
                    "explicit_model": bool(row["explicit_model"]),
                    "explicit_effort": bool(row["explicit_effort"]),
                },
                task_id=row["subject_id"] if row["subject_kind"] == "task" else None,
            )

    def runtimes() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT b.binding_id,b.task_id,b.harness_kind,b.backend_kind,b.state,b.created_at,
                   t.project_id,t.request_id,t.champion_agent_id,t.coordinator_agent_id
              FROM runtime_bindings b JOIN tasks t ON t.task_id=b.task_id
             WHERE {_time_clause('b.created_at', inclusive)}
             ORDER BY b.created_at,b.binding_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"runtime:{row['binding_id']}",
                occurred_at=row["created_at"],
                category="runtime",
                action="bound",
                owner_id=row["champion_agent_id"] or row["coordinator_agent_id"],
                subject_kind="runtime-bindings",
                subject_id=row["binding_id"],
                state=row["state"],
                details={"harness": row["harness_kind"], "backend": row["backend_kind"]},
                project_id=row["project_id"],
                request_id=row["request_id"],
                task_id=row["task_id"],
            )

    def resources() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT r.resource_id,r.task_id,r.owner_id,r.resource_type,r.lifetime,r.state,
                   r.registered_at,t.project_id,t.request_id
              FROM task_resources r JOIN tasks t ON t.task_id=r.task_id
             WHERE {_time_clause('r.registered_at', inclusive)}
             ORDER BY r.registered_at,r.resource_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"resource:{row['resource_id']}",
                occurred_at=row["registered_at"],
                category="resource",
                action="registered",
                owner_id=row["owner_id"],
                subject_kind="task-resources",
                subject_id=row["resource_id"],
                state=row["state"],
                details={"resource_type": row["resource_type"], "lifetime": row["lifetime"]},
                project_id=row["project_id"],
                request_id=row["request_id"],
                task_id=row["task_id"],
            )

    def cleanup() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT c.*,t.project_id,t.request_id,t.champion_agent_id,t.coordinator_agent_id
              FROM cleanup_obligations c JOIN tasks t ON t.task_id=c.task_id
             WHERE {_time_clause('c.updated_at', inclusive)}
             ORDER BY c.updated_at,c.cleanup_obligation_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"cleanup:{row['cleanup_obligation_id']}",
                occurred_at=row["updated_at"],
                category="cleanup",
                action="obligation",
                owner_id=row["owner_id"] or row["champion_agent_id"] or row["coordinator_agent_id"],
                subject_kind="cleanup-obligations",
                subject_id=row["cleanup_obligation_id"],
                state=row["cleanup_state"],
                details={"task_class": row["task_class"] or "<unknown>", "task_id": row["task_id"]},
                gaps=[] if row["task_class"] is not None else ["cleanup_task_class"],
                project_id=row["project_id"],
                request_id=row["request_id"],
                task_id=row["task_id"],
            )

    def cleanup_receipts() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT r.action_id,r.outcome,r.receipt_hash,r.recorded_at,a.action_kind,
                   a.adapter_kind,a.resource_id,c.task_id,t.project_id,t.request_id,
                   c.owner_id,t.champion_agent_id,t.coordinator_agent_id
              FROM cleanup_action_receipts r
              JOIN cleanup_actions a ON a.action_id=r.action_id
              JOIN cleanup_operations o ON o.operation_id=r.operation_id
              JOIN cleanup_obligations c ON c.cleanup_obligation_id=o.cleanup_obligation_id
              JOIN tasks t ON t.task_id=c.task_id
             WHERE {_time_clause('r.recorded_at', inclusive)}
             ORDER BY r.recorded_at,r.action_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"cleanup-receipt:{row['action_id']}",
                occurred_at=row["recorded_at"],
                category="resource" if row["resource_id"] is not None else "cleanup_action",
                action=row["action_kind"],
                owner_id=row["owner_id"] or row["champion_agent_id"] or row["coordinator_agent_id"],
                subject_kind="cleanup-action-receipts",
                subject_id=row["action_id"],
                state=row["outcome"],
                details={
                    "adapter": row["adapter_kind"],
                    "resource_id": _public_id("resource", row["resource_id"]),
                    "receipt_hash": row["receipt_hash"],
                },
                project_id=row["project_id"],
                request_id=row["request_id"],
                task_id=row["task_id"],
            )

    def teardowns() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT r.*,t.project_id,t.request_id,t.champion_agent_id,t.coordinator_agent_id
              FROM teardown_receipts r JOIN tasks t ON t.task_id=r.task_id
             WHERE {_time_clause('r.completed_at', inclusive)}
             ORDER BY r.completed_at,r.receipt_id
        """
        for row in _query(store, statement, time_parameters):
            yield _fact(
                store,
                fact_id=f"teardown:{row['receipt_id']}",
                occurred_at=row["completed_at"],
                category="teardown",
                action="completed",
                owner_id=row["champion_agent_id"] or row["coordinator_agent_id"],
                subject_kind="teardown-receipts",
                subject_id=row["receipt_id"],
                state="succeeded",
                details={"receipt_hash": row["receipt_hash"], "policy_version": row["policy_version"]},
                project_id=row["project_id"],
                request_id=row["request_id"],
                task_id=row["task_id"],
            )

    def evidence_rows() -> Iterator[dict[str, Any]]:
        statement = f"""
            SELECT * FROM activity_evidence
             WHERE {_time_clause('occurred_at', inclusive)}
             ORDER BY occurred_at,evidence_id
        """
        for row in _query(store, statement, time_parameters):
            links = [
                {
                    "kind": "canonical",
                    "ref": f"league://activity-evidence/{_public_id('evidence', row['evidence_id'])}",
                }
            ]
            if row["public_url"] is not None:
                links.append({"kind": "public_url", "url": row["public_url"]})
            gaps = [] if row["verification"] == "verified" else ["evidence_verification"]
            yield _fact(
                store,
                fact_id=f"evidence:{row['evidence_id']}",
                occurred_at=row["occurred_at"],
                category=row["evidence_kind"],
                action=row["action"],
                owner_id=row["owner_agent_id"],
                subject_kind="activity-evidence",
                subject_id=row["evidence_id"],
                state=row["state"],
                verification=row["verification"],
                summary=row["summary"],
                details={
                    "object_hash": row["object_hash"],
                    "local_evidence_hash": row["local_evidence_hash"],
                    "local_evidence": (
                        json.loads(row["local_evidence_json"])
                        if local and row["local_evidence_json"] is not None
                        else ("<local-only>" if row["local_evidence_json"] is not None else None)
                    ),
                    "root_cause_tag": row["root_cause_tag"],
                    "owning_issue_url": row["owning_issue_url"],
                    "required_for_completion": bool(row["required_for_completion"]),
                },
                evidence=links,
                gaps=gaps,
                project_id=row["project_id"],
                request_id=row["request_id"],
                task_id=row["task_id"],
                squad_id=row["squad_id"],
                repair_id=row["stable_repair_id"],
                repair_phase=row["repair_phase"],
            )

    for source in (
        prompts,
        requests,
        dispatches,
        tasks,
        assignments,
        callsign_history,
        transitions,
        rollovers,
        owner_changes,
        routing,
        runtimes,
        resources,
        cleanup,
        cleanup_receipts,
        teardowns,
        evidence_rows,
    ):
        sources.append(source())
    return sources


def _source_watermark(store: Any, to_at: str) -> str:
    sources = (
        ("prompts", "created_at"),
        ("requests", "updated_at"),
        ("request_dispatches", "decided_at"),
        ("tasks", "updated_at"),
        ("task_assignments", "updated_at"),
        ("callsign_assignments", "reserved_at"),
        ("callsign_assignments", "activated_at"),
        ("callsign_assignments", "released_at"),
        ("task_transitions", "created_at"),
        ("rollover_operations", "updated_at"),
        ("events", "occurred_at"),
        ("model_routing_decisions", "chosen_at"),
        ("runtime_bindings", "updated_at"),
        ("task_resources", "updated_at"),
        ("cleanup_obligations", "updated_at"),
        ("cleanup_action_receipts", "recorded_at"),
        ("teardown_receipts", "completed_at"),
        ("activity_evidence", "occurred_at"),
    )
    observed = []
    for table, column in sources:
        row = store.connection.execute(
            f"SELECT COUNT(*),COALESCE(MAX({column}),''),COALESCE(MAX(rowid),0) FROM {table} WHERE {column}<=?",
            (to_at,),
        ).fetchone()
        observed.append((table, int(row[0]), row[1], int(row[2])))
    return hashlib.sha256(_stable_bytes(observed)).hexdigest()


def _scope_context(store: Any, kind: str, identifier: Optional[str]) -> dict[str, Any]:
    if kind not in {"owner", "squad", "project", "all"}:
        raise StorageRefusal("invalid_report_scope", "report scope is unsupported")
    if (kind == "all") != (identifier is None):
        raise StorageRefusal("invalid_report_scope", "report scope identity is incomplete")
    result = {"kind": kind, "id": identifier, "actors": set(), "tasks": set(), "requests": set()}
    if kind == "all":
        return result
    if kind == "owner":
        rows = store.connection.execute(
            "SELECT agent_id FROM agent_instances WHERE agent_id=? OR callsign=? ORDER BY agent_id LIMIT 2",
            (identifier, identifier),
        ).fetchall()
        if not rows:
            raise StorageRefusal("report_scope_unknown", "report owner is unknown")
        if len(rows) > 1:
            raise StorageRefusal("report_scope_ambiguous", "report owner identity is ambiguous")
        result["id"] = rows[0]["agent_id"]
        result["actors"].add(rows[0]["agent_id"])
    elif kind == "squad":
        squad = store.connection.execute(
            "SELECT shotcaller_agent_id FROM squads WHERE squad_id=?", (identifier,)
        ).fetchone()
        if squad is None:
            raise StorageRefusal("report_scope_unknown", "report Squad is unknown")
        result["actors"].add(squad["shotcaller_agent_id"])
        actor_rows = store.connection.execute(
            """
            SELECT actor_id FROM (
              SELECT champion_agent_id actor_id FROM squad_champions WHERE squad_id=?
              UNION
              SELECT agent_id FROM shotcaller_intake WHERE squad_id=?
              UNION
              SELECT agent_id FROM agent_instances WHERE shotcaller_agent_id=?
            ) ORDER BY actor_id LIMIT ?
            """,
            (identifier, identifier, squad["shotcaller_agent_id"], MAX_REPORT_FACTS + 1),
        ).fetchall()
        if len(actor_rows) > MAX_REPORT_FACTS:
            raise StorageRefusal("report_scope_too_large", "report Squad scope exceeds its bound")
        result["actors"].update(row["actor_id"] for row in actor_rows)
    else:
        project = store.connection.execute(
            "SELECT project_id FROM projects WHERE project_id=?", (identifier,)
        ).fetchone()
        if project is None:
            raise StorageRefusal("report_scope_unknown", "report project is unknown")
        task_rows = store.connection.execute(
            "SELECT task_id,request_id FROM tasks WHERE project_id=? ORDER BY task_id LIMIT ?",
            (identifier, MAX_REPORT_FACTS + 1),
        ).fetchall()
        if len(task_rows) > MAX_REPORT_FACTS:
            raise StorageRefusal("report_scope_too_large", "report project scope exceeds its bound")
        result["tasks"].update(row["task_id"] for row in task_rows)
        result["requests"].update(
            row["request_id"] for row in task_rows if row["request_id"] is not None
        )
        actor_rows = store.connection.execute(
            """
            SELECT a.agent_id FROM agent_instances a
              JOIN tasks t ON t.task_id=a.task_id
             WHERE t.project_id=? ORDER BY a.agent_id LIMIT ?
            """,
            (identifier, MAX_REPORT_FACTS + 1),
        ).fetchall()
        if len(actor_rows) > MAX_REPORT_FACTS:
            raise StorageRefusal("report_scope_too_large", "report project actors exceed the bound")
        result["actors"].update(row["agent_id"] for row in actor_rows)
    return result


def _matches_scope(fact: dict[str, Any], scope: dict[str, Any]) -> bool:
    if scope["kind"] == "all":
        return True
    owner = fact["owner"]
    owner_id = owner["actor_id"] if owner else None
    if scope["kind"] == "owner":
        return owner_id in {_public_id("actor", item) for item in scope["actors"]}
    values = fact["scope"]
    if scope["kind"] == "squad":
        return (
            values["squad_id"] == scope["id"]
            or owner_id in {_public_id("actor", item) for item in scope["actors"]}
        )
    return (
        values["project_id"] == scope["id"]
        or values["task_id"] in scope["tasks"]
        or values["request_id"] in scope["requests"]
    )


def _cursor(value: Optional[str], spec_hash: str) -> Optional[tuple[str, str]]:
    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(value + padding))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("invalid_report_cursor", "report cursor is malformed") from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"spec", "at", "fact"}
        or decoded["spec"] != spec_hash
        or not isinstance(decoded["at"], str)
        or not isinstance(decoded["fact"], str)
    ):
        raise StorageRefusal("invalid_report_cursor", "report cursor belongs to another specification")
    return decoded["at"], decoded["fact"]


def _encode_cursor(spec_hash: str, fact: dict[str, Any]) -> str:
    payload = _stable_bytes(
        {"spec": spec_hash, "at": fact["occurred_at"], "fact": fact["fact_id"]}
    )
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _safe_hash_fact(fact: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(fact))
    if value["summary"] != "<local-only>" and value["category"] not in EVIDENCE_KINDS:
        value["summary"] = "<local-only>"
    if "local_evidence" in value["details"]:
        value["details"]["local_evidence"] = (
            "<local-only>" if value["details"]["local_evidence"] is not None else None
        )
    value["scope"] = {
        key: _public_id(
            {"project_id": "project", "request_id": "request", "task_id": "task", "squad_id": "squad"}[key],
            item,
        )
        for key, item in value["scope"].items()
    }
    return value


def _display_fact(fact: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(fact))
    value["scope"] = {
        key: _public_id(
            {"project_id": "project", "request_id": "request", "task_id": "task", "squad_id": "squad"}[key],
            item,
        )
        for key, item in value["scope"].items()
    }
    return value


def _completion(store: Any, scope: dict[str, Any], to_at: str) -> dict[str, Any]:
    def fact_scope(
        owner: Optional[str],
        project: Optional[str],
        request: Optional[str],
        task: Optional[str],
        squad: Optional[str] = None,
    ) -> bool:
        if scope["kind"] == "all":
            return True
        if scope["kind"] == "owner":
            return owner in scope["actors"]
        if scope["kind"] == "squad":
            return squad == scope["id"] or owner in scope["actors"]
        return project == scope["id"] or task in scope["tasks"] or request in scope["requests"]

    counts: Counter[str] = Counter()
    gaps: list[dict[str, Any]] = []
    scanned_rows = 0

    def bounded_rows(statement: str, parameters: tuple[Any, ...]) -> Iterator[Any]:
        nonlocal scanned_rows
        for row in store.connection.execute(statement, parameters):
            scanned_rows += 1
            if scanned_rows > MAX_REPORT_FACTS:
                raise StorageRefusal(
                    "report_completion_too_large",
                    "report completion scan exceeds its bounded row budget",
                )
            yield row

    for row in bounded_rows(
        """
        SELECT r.request_id,r.owner_agent_id,r.state,r.updated_at,
               EXISTS(SELECT 1 FROM response_references x WHERE x.request_id=r.request_id) has_response
          FROM requests r WHERE r.updated_at<=?
        """,
        (to_at,),
    ):
        if fact_scope(row["owner_agent_id"], None, row["request_id"], None):
            if row["state"] not in TERMINAL_REQUEST_STATES:
                counts["unresolved_requests"] += 1
            elif row["state"] == "answered":
                if not row["has_response"]:
                    counts["evidence_gaps"] += 1
                    if len(gaps) < MAX_REPORT_GAPS:
                        gaps.append({"kind": "response_evidence", "subject": _public_id("request", row["request_id"]), "status": "unknown"})

    for row in bounded_rows(
        """
        SELECT t.task_id,t.project_id,t.request_id,t.coordinator_agent_id,
               t.champion_agent_id,t.state,t.updated_at,
               EXISTS(
                 SELECT 1 FROM task_transitions x
                  WHERE x.task_id=t.task_id AND x.to_state=t.state AND x.created_at<=?
               ) has_terminal_transition
          FROM tasks t WHERE t.updated_at<=?
        """,
        (to_at, to_at),
    ):
        owner = row["champion_agent_id"] or row["coordinator_agent_id"]
        if not fact_scope(owner, row["project_id"], row["request_id"], row["task_id"]):
            continue
        if row["state"] == "ready_to_land":
            counts["pending_landing"] += 1
        elif row["state"] not in TERMINAL_TASK_STATES:
            counts["unresolved_tasks"] += 1
        elif row["state"] in {"completed", "complete", "failed", "cancelled", "canceled", "rejected"}:
            if not row["has_terminal_transition"]:
                counts["evidence_gaps"] += 1
                if len(gaps) < MAX_REPORT_GAPS:
                    gaps.append({"kind": "task_transition", "subject": _public_id("task", row["task_id"]), "status": "unverified"})

    for row in bounded_rows(
        """
        SELECT a.task_assignment_id,a.task_id,a.coordinator_agent_id,a.champion_agent_id,a.state,
               a.runtime_instance_id,t.project_id,t.request_id,
               EXISTS(
                 SELECT 1 FROM model_routing_decisions m
                  WHERE m.subject_kind='task' AND m.subject_id=a.task_id AND m.chosen_at<=?
               ) has_model,
               EXISTS(
                 SELECT 1 FROM runtime_instances r
                  WHERE r.runtime_instance_id=a.runtime_instance_id
                    AND r.verified=1 AND r.last_seen_at<=?
               ) has_runtime
          FROM task_assignments a JOIN tasks t ON t.task_id=a.task_id
         WHERE a.updated_at<=?
        """,
        (to_at, to_at, to_at),
    ):
        owner = row["champion_agent_id"] or row["coordinator_agent_id"]
        if not fact_scope(owner, row["project_id"], row["request_id"], row["task_id"]):
            continue
        if row["state"] not in SETTLED_ASSIGNMENT_STATES:
            counts["pending_assignments"] += 1
        if not row["has_runtime"] or not row["has_model"]:
            counts["evidence_gaps"] += 1
            if len(gaps) < MAX_REPORT_GAPS:
                gaps.append({"kind": "assignment_runtime_model", "subject": _public_id("assignment", row["task_assignment_id"]), "status": "unverified"})

    for row in bounded_rows(
        """
        SELECT o.outbox_id,o.state,o.recipient_agent_id,e.request_id,e.task_id,t.project_id
          FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
          LEFT JOIN tasks t ON t.task_id=e.task_id
         WHERE o.available_at<=?
        """,
        (to_at,),
    ):
        if row["state"] not in SETTLED_OUTBOX_STATES and fact_scope(
            row["recipient_agent_id"], row["project_id"], row["request_id"], row["task_id"]
        ):
            counts["pending_delivery"] += 1

    for row in bounded_rows(
        "SELECT owner_agent_id,state,aggregate_id FROM obligations WHERE created_at<=?", (to_at,)
    ):
        if row["state"] == "open" and fact_scope(row["owner_agent_id"], None, None, row["aggregate_id"]):
            counts["pending_verification"] += 1

    for row in bounded_rows(
        """
        SELECT operation_id,squad_id,predecessor_agent_id,successor_agent_id,state
          FROM rollover_operations
         WHERE updated_at<=? AND state IN ('prepared','acknowledged','switched')
        """,
        (to_at,),
    ):
        predecessor_in_scope = fact_scope(
            row["predecessor_agent_id"], None, None, None, row["squad_id"]
        )
        successor_in_scope = fact_scope(
            row["successor_agent_id"], None, None, None, row["squad_id"]
        )
        if predecessor_in_scope or successor_in_scope:
            counts["pending_handoff"] += 1

    for row in bounded_rows(
        """
        SELECT c.cleanup_state,c.owner_id,c.task_id,t.project_id,t.request_id,
               t.champion_agent_id,t.coordinator_agent_id
          FROM cleanup_obligations c JOIN tasks t ON t.task_id=c.task_id
         WHERE c.updated_at<=?
        """,
        (to_at,),
    ):
        owner = row["owner_id"] or row["champion_agent_id"] or row["coordinator_agent_id"]
        if row["cleanup_state"] not in SETTLED_CLEANUP_STATES and fact_scope(
            owner, row["project_id"], row["request_id"], row["task_id"]
        ):
            counts["pending_cleanup"] += 1

    for row in bounded_rows(
        """
        SELECT r.state,r.owner_id,r.task_id,t.project_id,t.request_id
          FROM task_resources r JOIN tasks t ON t.task_id=r.task_id
         WHERE r.registered_at<=?
        """,
        (to_at,),
    ):
        if row["state"] == "active" and fact_scope(
            row["owner_id"], row["project_id"], row["request_id"], row["task_id"]
        ):
            counts["pending_resources"] += 1

    latest_required: dict[tuple[Any, ...], Any] = {}
    for row in bounded_rows(
        """
        SELECT evidence_id,evidence_kind,action,owner_agent_id,squad_id,project_id,
               request_id,task_id,state,verification,stable_repair_id,root_cause_tag,
               required_for_completion,occurred_at
          FROM activity_evidence WHERE occurred_at<=?
         ORDER BY occurred_at,evidence_id
        """,
        (to_at,),
    ):
        key = (
            row["evidence_kind"], row["action"], row["owner_agent_id"], row["squad_id"],
            row["project_id"], row["request_id"], row["task_id"], row["stable_repair_id"],
            row["root_cause_tag"], bool(row["required_for_completion"]),
        )
        latest_required[key] = row
    for row in latest_required.values():
        if not fact_scope(
            row["owner_agent_id"], row["project_id"], row["request_id"],
            row["task_id"], row["squad_id"],
        ):
            continue
        if row["verification"] != "verified":
            counts["evidence_gaps"] += 1
            if len(gaps) < MAX_REPORT_GAPS:
                gaps.append({"kind": "evidence_verification", "subject": _public_id("evidence", row["evidence_id"]), "status": row["verification"]})
        if not row["required_for_completion"]:
            continue
        if row["state"] != "succeeded":
            gate = {
                "install": "pending_installation",
                "deployment": "pending_deployment",
                "smoke": "pending_smoke",
                "rollback": "pending_rollback",
                "teardown": "pending_teardown",
            }.get(row["evidence_kind"], "pending_authority_or_release")
            counts[gate] += 1

    gate_order = (
        "unresolved_requests",
        "unresolved_tasks",
        "pending_assignments",
        "pending_landing",
        "pending_delivery",
        "pending_installation",
        "pending_deployment",
        "pending_smoke",
        "pending_rollback",
        "pending_teardown",
        "pending_authority_or_release",
        "pending_handoff",
        "pending_verification",
        "pending_resources",
        "pending_cleanup",
        "evidence_gaps",
    )
    gates = [
        {
            "kind": kind,
            "count": counts[kind],
            "status": "unknown" if kind == "evidence_gaps" and counts[kind] else ("pending" if counts[kind] else "settled"),
        }
        for kind in gate_order
    ]
    pending = sum(counts[kind] for kind in gate_order if kind != "evidence_gaps")
    unknown = counts["evidence_gaps"]
    return {
        "everything_finished": pending == 0 and unknown == 0,
        "status": "unfinished" if pending else ("unknown" if unknown else "finished"),
        "gates": gates,
        "gap_total": unknown,
        "gaps": gaps,
        "gaps_truncated": unknown > len(gaps),
    }


def _repair_summary(groups: dict[str, dict[str, Any]], truncated: bool) -> dict[str, Any]:
    values = []
    for identifier in sorted(groups):
        group = groups[identifier]
        group["phases"] = dict(sorted(group["phases"].items()))
        group["underlying_evidence"] = sorted(group["underlying_evidence"])
        values.append(group)
    return {"groups": values, "truncated": truncated, "total_groups": len(groups)}


def generate_report(
    store: Any,
    *,
    from_at: str,
    to_at: str,
    timezone_name: str,
    from_inclusive: bool,
    scope_kind: str,
    scope_id: Optional[str],
    limit: int,
    cursor: Optional[str],
    local_diagnostic: bool,
    report_id: Optional[str] = None,
    event_watermark: Optional[int] = None,
    source_watermark: Optional[str] = None,
    persist: bool = True,
    expected_content_hash: Optional[str] = None,
) -> dict[str, Any]:
    store._report_owner_cache = {}
    from_value = canonical_timestamp(from_at, "from")
    to_value = canonical_timestamp(to_at, "to")
    if _timestamp(from_value, "from") > _timestamp(to_value, "to"):
        raise StorageRefusal("invalid_report_range", "report from time follows its to time")
    zone = validate_timezone(timezone_name)
    if not 1 <= limit <= MAX_REPORT_PAGE:
        raise StorageRefusal("invalid_report_limit", f"report limit must be between 1 and {MAX_REPORT_PAGE}")
    scope = _scope_context(store, scope_kind, scope_id)
    if event_watermark is None:
        row = store.connection.execute(
            "SELECT COALESCE(MAX(event_seq),0) FROM events WHERE occurred_at<=?", (to_value,)
        ).fetchone()
        event_watermark = int(row[0])
    if source_watermark is None:
        source_watermark = _source_watermark(store, to_value)
    elif not SHA256.fullmatch(source_watermark):
        raise StorageRefusal("invalid_report_watermark", "report source watermark is invalid")
    spec = {
        "schema": REPORT_SCHEMA,
        "from": from_value,
        "to": to_value,
        "timezone": zone,
        "from_inclusive": bool(from_inclusive),
        "scope": {"kind": scope_kind, "id": scope["id"]},
        "event_watermark": event_watermark,
        "source_watermark": source_watermark,
    }
    spec_hash = hashlib.sha256(_stable_bytes(spec)).hexdigest()
    after = _cursor(cursor, spec_hash)
    facts = heapq.merge(
        *_source_facts(store, from_value, to_value, from_inclusive, local_diagnostic),
        key=lambda item: (item["occurred_at"], item["fact_id"]),
    )
    page: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    states: Counter[str] = Counter()
    owners: dict[str, dict[str, Any]] = {}
    repair_groups: dict[str, dict[str, Any]] = {}
    repair_truncated = False
    content = hashlib.sha256()
    content.update(spec_hash.encode("ascii"))
    fact_count = 0
    for fact in facts:
        if not _matches_scope(fact, scope):
            continue
        fact_count += 1
        if fact_count > MAX_REPORT_FACTS:
            raise StorageRefusal("report_too_large", "report exceeds the bounded fact scan")
        totals[fact["category"]] += 1
        states[fact["state"] or "unknown"] += 1
        content.update(_stable_bytes(_safe_hash_fact(fact)))
        repair = fact["repair"]
        if repair is not None:
            identifier = repair["stable_id"]
            if identifier not in repair_groups and len(repair_groups) >= MAX_REPAIR_GROUPS:
                repair_truncated = True
            else:
                group = repair_groups.setdefault(
                    identifier,
                    {
                        "stable_id": identifier,
                        "repetitions": 0,
                        "phases": Counter(),
                        "final_state": None,
                        "owning_issue_url": fact["details"].get("owning_issue_url"),
                        "root_cause_tag": fact["details"].get("root_cause_tag"),
                        "underlying_evidence": [],
                    },
                )
                group["repetitions"] += 1
                phase = repair["phase"] or "unknown"
                group["phases"][phase] += 1
                if phase == "final":
                    group["final_state"] = fact["state"]
                group["underlying_evidence"].append(fact["fact_id"])
        key = (fact["occurred_at"], fact["fact_id"])
        if after is not None and key <= after:
            continue
        if len(page) < limit:
            page.append(_display_fact(fact))
    completion = _completion(store, scope, to_value)
    content.update(_stable_bytes(completion))
    content_hash = content.hexdigest()
    identity_hash = hashlib.sha256(
        f"{spec_hash}:{content_hash}".encode("ascii")
    ).hexdigest()
    report_identity = report_id or f"report:{identity_hash[:24]}"
    if page:
        for fact in page:
            owner = fact["owner"]
            owner_key = owner["actor_id"] if owner else "unowned"
            group = owners.setdefault(
                owner_key,
                {"owner": owner, "facts": [], "count": 0},
            )
            group["facts"].append(fact)
            group["count"] += 1
    next_cursor = None
    consumed = 0
    if after is not None:
        consumed = sum(
            1
            for source in _source_facts(store, from_value, to_value, from_inclusive, False)
            for fact in source
            if _matches_scope(fact, scope)
            and (fact["occurred_at"], fact["fact_id"]) <= after
        )
    if consumed + len(page) < fact_count and page:
        next_cursor = _encode_cursor(spec_hash, page[-1])
    reproduction = {
        "requested": expected_content_hash is not None,
        "matches_stored_hash": expected_content_hash is None or expected_content_hash == content_hash,
        "expected_content_hash": expected_content_hash,
        "observed_content_hash": content_hash,
    }
    if expected_content_hash is not None and expected_content_hash != content_hash:
        completion = dict(completion)
        completion["everything_finished"] = False
        completion["status"] = "unknown"
        completion["gap_total"] += 1
        completion["gates"] = [
            {
                **gate,
                "count": gate["count"] + 1,
                "status": "unknown",
            }
            if gate["kind"] == "evidence_gaps"
            else gate
            for gate in completion["gates"]
        ]
        if len(completion["gaps"]) < MAX_REPORT_GAPS:
            completion["gaps"] = [
                *completion["gaps"],
                {"kind": "report_reproduction", "subject": report_identity, "status": "unverified"},
            ]
    approved_public_urls = {
        link["url"]
        for fact in page
        for link in fact["evidence"]
        if link.get("kind") == "public_url"
    } | {
        fact["details"]["owning_issue_url"]
        for fact in page
        if fact["details"].get("owning_issue_url") is not None
    } | {
        value
        for group in repair_groups.values()
        for value in (group.get("owning_issue_url"),)
        if value is not None
    }
    if len(approved_public_urls) > MAX_REPORT_PAGE:
        raise StorageRefusal(
            "report_public_urls_too_large",
            "report approved public URL set exceeds its bound",
        )
    report = {
        "schema": REPORT_SCHEMA,
        "mode": "local_diagnostic" if local_diagnostic else "outbound_safe",
        "report": {
            "report_id": report_identity,
            "created_at": to_value,
            "spec_hash": spec_hash,
            "content_hash": content_hash,
            "event_watermark": event_watermark,
            "source_watermark": source_watermark,
            "from": from_value,
            "to": to_value,
            "timezone": zone,
            "from_inclusive": bool(from_inclusive),
            "scope": {
                "kind": scope_kind,
                "id": _public_id(
                    {"owner": "actor", "squad": "squad", "project": "project", "all": "evidence"}[scope_kind],
                    scope["id"],
                ),
            },
            "reproduction": reproduction,
        },
        "completion": completion,
        "totals": {"facts": fact_count, "by_category": dict(sorted(totals.items())), "by_state": dict(sorted(states.items()))},
        "pagination": {
            "limit": limit,
            "returned": len(page),
            "total": fact_count,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "truncated": next_cursor is not None,
        },
        "chronological": page,
        "owner_grouped": [owners[key] for key in sorted(owners)],
        "recurring_repairs": _repair_summary(repair_groups, repair_truncated),
        "approved_public_urls": sorted(approved_public_urls),
    }
    if persist:
        try:
            with store._transaction():
                existing = store.connection.execute(
                    "SELECT * FROM report_specs WHERE report_id=?", (report_identity,)
                ).fetchone()
                desired = (
                    report_identity,
                    REPORT_SCHEMA,
                    from_value,
                    to_value,
                    zone,
                    int(from_inclusive),
                    scope_kind,
                    scope["id"],
                    event_watermark,
                    source_watermark,
                    to_value,
                    spec_hash,
                    content_hash,
                    fact_count,
                )
                if existing is None:
                    store.connection.execute(
                        """
                        INSERT INTO report_specs
                          (report_id,report_schema,from_at,to_at,timezone,from_inclusive,
                           scope_kind,scope_id,event_watermark,source_watermark,created_at,spec_hash,content_hash,fact_count)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        desired,
                    )
                else:
                    observed = tuple(existing[column] for column in (
                        "report_id","report_schema","from_at","to_at","timezone","from_inclusive",
                        "scope_kind","scope_id","event_watermark","source_watermark","created_at","spec_hash","content_hash","fact_count",
                    ))
                    if observed != desired:
                        raise StorageRefusal("report_spec_conflict", "immutable report identity already differs")
        except StorageRefusal:
            raise
        except sqlite3.DatabaseError as exc:
            raise store._translate_database_error(exc, "report specification write conflicted") from exc
    return report


def report_spec(store: Any, report_id: str) -> Optional[dict[str, Any]]:
    if not SAFE_ID.fullmatch(report_id):
        raise StorageRefusal("invalid_report_id", "report identity is invalid")
    row = store.connection.execute(
        "SELECT * FROM report_specs WHERE report_id=?", (report_id,)
    ).fetchone()
    return dict(row) if row is not None else None


__all__ = [
    "LARGE_HISTORY_LATENCY_BUDGET_MS",
    "MAX_REPORT_FACTS",
    "MAX_REPORT_PAGE",
    "REPORT_SCHEMA",
    "TYPICAL_DAY_LATENCY_BUDGET_MS",
    "canonical_timestamp",
    "generate_report",
    "record_activity_evidence",
    "report_spec",
    "validate_timezone",
]
