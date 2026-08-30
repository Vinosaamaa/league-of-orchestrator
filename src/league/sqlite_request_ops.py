"""SQLite prompt, request, claim, dispatch, result, and reconciliation operations."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Optional

from .storage_request import (
    MAX_TRIAGE_TURN_PROMPTS,
    MAX_TASK_RESULT_SOURCES,
    AnswerRequestCommand,
    DispatchRequestCommand,
    ReconcileDuplicateRequestCommand,
    RequestResultCommand,
    TurnDispatchPlan,
)
from .storage_types import StorageRefusal
from .orchestration import (
    LOCAL_CHAMPION,
    LOCAL_DIRECT,
    SQUAD_ROUTE,
    OrchestrationSignals,
    decide_orchestration_route,
)


REQUEST_STATES = {
    "open",
    "routed",
    "accepted",
    "in_progress",
    "awaiting_user",
    "blocked",
    "awaiting_requester",
    "deferred",
    "answered",
    "cancelled",
}
TERMINAL_REQUEST_STATES = {"answered", "cancelled"}
EXECUTION_MODES = {"direct", "hidden", "champion", "squad"}
PROMPT_ITEM_DISPOSITIONS = {
    "new_request",
    "follow_up",
    "context",
    "acknowledgement",
    "duplicate",
    "deferred",
}
CHAMPION_WORK_KINDS = {
    "benchmark",
    "bug-fix",
    "debugging",
    "durable-research",
    "operational",
    "release",
    "repository-reproduction",
    "repository-initialize",
    "repository-write",
    "configuration-write",
    "migration",
    "supervised-test",
    "long-running",
}
DIRECT_WORK_KINDS = {"question", "short-check", "read-only"}
MAX_PROMPT_BYTES = 262_144
MAX_PROMPT_ITEMS = 32
ROUTINE_PROGRESS_REASONS = {
    "child_started",
    "child_working",
    "milestone",
    "partial_completion",
    "recoverable_child_blocker",
    "aggregate_changed",
}
IMMEDIATE_PROGRESS_REASONS = {
    "route_accepted",
    "route_rejected",
    "owner_unavailable",
    "awaiting_user",
    "awaiting_authority",
    "parent_critical_blocker",
    "acceptance_risk",
    "safety_risk",
    "scope_change",
    "target_change",
    "authority_change",
    "cost_change",
    "deadline_change",
    "owner_change",
    "reroute",
    "request_resolved",
    "request_failed",
    "request_cancelled",
    "request_stalled",
}
PROGRESS_REASONS = ROUTINE_PROGRESS_REASONS | IMMEDIATE_PROGRESS_REASONS
PRIVATE_PROGRESS_PATTERN = re.compile(
    r"(?:^|\s)/(?:Users|home|private|tmp)/|\b(?:runtime|thread|worktree):|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def _bounded_public_text(value: str, label: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or "\n" in value
        or len(value.encode("utf-8")) > maximum
        or PRIVATE_PROGRESS_PATTERN.search(value)
    ):
        raise StorageRefusal(
            "public_summary_invalid",
            f"{label} must be bounded public-safe text without local runtime details",
        )
    return value


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("invalid_time", f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("invalid_time", f"{label} must include a UTC offset")
    return parsed


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _claim_hash(token: str) -> str:
    if not token:
        raise StorageRefusal("claim_required", "request claim proof is required")
    return _digest(f"league-request-claim\0{token}")


def _request_row(store: Any, request_id: str) -> sqlite3.Row:
    row = store.connection.execute(
        "SELECT * FROM requests WHERE request_id=?", (request_id,)
    ).fetchone()
    if row is None:
        raise StorageRefusal("request_unknown", "request does not exist")
    return row


def _active_claim(
    store: Any,
    request_id: str,
    *,
    token: str,
    runtime_instance_id: Optional[str] = None,
    at: Optional[str] = None,
) -> sqlite3.Row:
    row = store.connection.execute(
        "SELECT * FROM request_claims WHERE request_id=? AND released_at IS NULL",
        (request_id,),
    ).fetchone()
    if row is None or not hmac.compare_digest(str(row["claim_proof_hash"]), _claim_hash(token)):
        raise StorageRefusal("claim_mismatch", "request mutation requires the exact active claim")
    if runtime_instance_id is not None and row["runtime_instance_id"] != runtime_instance_id:
        raise StorageRefusal("claim_mismatch", "request claim belongs to a different runtime")
    if at is not None and _time(str(row["leased_until"]), "stored claim expiry") <= _time(at, "mutation time"):
        raise StorageRefusal("claim_expired", "request claim expired before mutation")
    return row


def _insert_request_event(
    store: Any,
    *,
    event_id: str,
    request_id: str,
    actor_id: str,
    request_version: int,
    event_type: str,
    state: str,
    update: str,
    at: str,
    detail: Optional[dict[str, Any]] = None,
    source_event_id: Optional[str] = None,
) -> None:
    store.connection.execute(
        """
        INSERT INTO events
          (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,
           detail_json,request_id,aggregate_kind,aggregate_id,source_event_id)
        VALUES(?,?,NULL,?,?,?,?,?,?,?,'request',?,?)
        """,
        (
            event_id,
            actor_id,
            request_version,
            event_type,
            state,
            update,
            at,
            _json(detail or {}),
            request_id,
            request_id,
            source_event_id,
        ),
    )


def intake_prompt(
    store: Any,
    prompt_id: str,
    intake_actor_id: str,
    runtime_instance_id: str,
    adapter_kind: str,
    session_ref: str,
    source_event_key: str,
    body: str,
    at: str,
    *,
    wake_scope_id: str | None = None,
    wake: bool = True,
) -> dict[str, Any]:
    _time(at, "prompt capture time")
    encoded = body.encode("utf-8")
    if not all((prompt_id, intake_actor_id, runtime_instance_id, adapter_kind, session_ref, source_event_key)):
        raise StorageRefusal("invalid_prompt", "prompt identity fields are required")
    if not encoded or len(encoded) > MAX_PROMPT_BYTES:
        raise StorageRefusal("invalid_prompt", "prompt body must be non-empty and within the bounded size")
    if not wake and wake_scope_id is not None:
        raise StorageRefusal("invalid_prompt_wake", "disabled prompt wake cannot name a scope")
    body_hash = hashlib.sha256(encoded).hexdigest()
    try:
        with store._transaction():
            existing = store.connection.execute(
                """
                SELECT p.prompt_id,p.intake_actor_id,p.runtime_instance_id,p.created_at,p.triage_state,
                       pp.body_hash,pp.byte_count
                  FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
                 WHERE p.adapter_kind=? AND p.session_ref=? AND p.source_event_key=?
                """,
                (adapter_kind, session_ref, source_event_key),
            ).fetchone()
            if existing is not None:
                exact = (
                    existing["intake_actor_id"] == intake_actor_id
                    and existing["runtime_instance_id"] == runtime_instance_id
                    and existing["body_hash"] == body_hash
                    and int(existing["byte_count"]) == len(encoded)
                )
                if not exact:
                    raise StorageRefusal(
                        "prompt_source_conflict",
                        "prompt source identity was already captured with different content or ownership",
                    )
                return {
                    "prompt_id": existing["prompt_id"],
                    "triage_state": existing["triage_state"],
                    "idempotent": True,
                }
            runtime = store.connection.execute(
                """
                SELECT r.actor_agent_id,r.status,r.verified,
                       i.state AS intake_state,s.shotcaller_agent_id,s.state AS squad_state
                  FROM runtime_instances r
                  JOIN agent_instances a ON a.agent_id=r.actor_agent_id
                  LEFT JOIN shotcaller_intake i ON i.agent_id=r.actor_agent_id
                  LEFT JOIN squads s ON s.squad_id=i.squad_id
                 WHERE r.runtime_instance_id=? AND a.retired_at IS NULL
                """,
                (runtime_instance_id,),
            ).fetchone()
            if (
                runtime is None
                or runtime["actor_agent_id"] != intake_actor_id
                or runtime["status"] not in {"active", "idle"}
                or not runtime["verified"]
            ):
                raise StorageRefusal(
                    "runtime_unverified",
                    "prompt intake runtime is not the actor's verified live endpoint",
                )
            if runtime["intake_state"] is not None and runtime["intake_state"] != "accepting":
                raise StorageRefusal(
                    "owner_draining", "Shotcaller intake is draining or closed"
                )
            if runtime["intake_state"] is not None and runtime["squad_state"] != "active":
                raise StorageRefusal(
                    "owner_superseded", "Shotcaller Squad is no longer active"
                )
            if (
                runtime["intake_state"] is not None
                and runtime["shotcaller_agent_id"] != intake_actor_id
            ):
                raise StorageRefusal(
                    "owner_superseded", "Shotcaller is no longer the stable Squad owner"
                )
            store.connection.execute(
                """
                INSERT INTO prompts
                  (prompt_id,intake_actor_id,runtime_instance_id,adapter_kind,session_ref,
                   source_event_key,triage_state,triage_digest,created_at,current_owner_agent_id,
                   current_owner_runtime_instance_id)
                VALUES(?,?,?,?,?,?,'untriaged',NULL,?,?,?)
                """,
                (
                    prompt_id,
                    intake_actor_id,
                    runtime_instance_id,
                    adapter_kind,
                    session_ref,
                    source_event_key,
                    at,
                    intake_actor_id,
                    runtime_instance_id,
                ),
            )
            store.connection.execute(
                "INSERT INTO prompt_payloads(prompt_id,body,body_hash,byte_count,pruned_at) VALUES(?,?,?,?,NULL)",
                (prompt_id, body, body_hash, len(encoded)),
            )
            if wake and wake_scope_id is None:
                scope_row = store.connection.execute(
                    "SELECT scope_id FROM watcher_scopes WHERE actor_agent_id=? ORDER BY scope_id LIMIT 1",
                    (intake_actor_id,),
                ).fetchone()
                if scope_row is not None:
                    wake_scope_id = str(scope_row["scope_id"])
                else:
                    actor = store.connection.execute(
                        "SELECT callsign FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                        (intake_actor_id,),
                    ).fetchone()
                    if actor is not None:
                        wake_scope_id = f"watcher:{actor['callsign']}"
            if wake_scope_id is not None:
                from .sqlite_watcher_ops import ensure_watcher_scope

                ensure_watcher_scope(
                    store, wake_scope_id, intake_actor_id, block_on_obligations=None
                )
                store.connection.execute(
                    """
                    UPDATE watcher_scopes
                       SET user_message_generation=user_message_generation+1,
                           wait_generation=wait_generation+1,stop_blocked=0,wait_active=0,
                           pending_stop_feedback_digest=NULL,
                           pending_stop_terminal_generation=NULL,
                           pending_stop_wait_generation=NULL
                     WHERE scope_id=?
                    """,
                    (wake_scope_id,),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "prompt intake conflicted with canonical state") from exc
    return {"prompt_id": prompt_id, "triage_state": "untriaged", "idempotent": False}


def quarantine_prompt(
    store: Any,
    prompt_id: str,
    adapter_kind: str,
    session_ref: str,
    source_event_key: str,
    body: str,
    at: str,
    *,
    wake_actor_id: str | None = None,
    wake_scope_id: str | None = None,
) -> dict[str, Any]:
    _time(at, "prompt quarantine time")
    encoded = body.encode("utf-8")
    if not all((prompt_id, adapter_kind, session_ref, source_event_key)):
        raise StorageRefusal("invalid_prompt", "quarantined prompt identity fields are required")
    if not encoded or len(encoded) > MAX_PROMPT_BYTES:
        raise StorageRefusal("invalid_prompt", "prompt body must be non-empty and within the bounded size")
    body_hash = hashlib.sha256(encoded).hexdigest()
    if (wake_actor_id is None) != (wake_scope_id is None):
        raise StorageRefusal(
            "invalid_prompt_wake", "prompt wake requires both exact actor and scope"
        )
    try:
        with store._transaction():
            existing = store.connection.execute(
                """
                SELECT * FROM prompt_quarantine
                 WHERE adapter_kind=? AND session_ref=? AND source_event_key=?
                """,
                (adapter_kind, session_ref, source_event_key),
            ).fetchone()
            if existing is not None:
                if existing["body_hash"] != body_hash or int(existing["byte_count"]) != len(encoded):
                    raise StorageRefusal(
                        "prompt_source_conflict",
                        "prompt source identity was already quarantined with different bytes",
                    )
                return {
                    "prompt_id": existing["prompt_id"],
                    "state": existing["state"],
                    "reason": existing["reason"],
                    "idempotent": True,
                }
            store.connection.execute(
                """
                INSERT INTO prompt_quarantine
                  (prompt_id,adapter_kind,session_ref,source_event_key,body,body_hash,
                   byte_count,state,reason,bound_actor_id,bound_runtime_instance_id,created_at,bound_at)
                VALUES(?,?,?,?,?,?,?,'quarantined','runtime_unverified',NULL,NULL,?,NULL)
                """,
                (prompt_id, adapter_kind, session_ref, source_event_key, body, body_hash, len(encoded), at),
            )
            if wake_actor_id is not None and wake_scope_id is not None:
                from .sqlite_watcher_ops import ensure_watcher_scope

                actor = store.connection.execute(
                    "SELECT role FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                    (wake_actor_id,),
                ).fetchone()
                if actor is None or actor["role"] != "shotcaller":
                    raise StorageRefusal(
                        "runtime_unverified", "quarantine wake actor is not a live Shotcaller"
                    )
                ensure_watcher_scope(
                    store, wake_scope_id, wake_actor_id, block_on_obligations=None
                )
                store.connection.execute(
                    """
                    UPDATE watcher_scopes
                       SET user_message_generation=user_message_generation+1,
                           wait_generation=wait_generation+1,stop_blocked=0,wait_active=0,
                           pending_stop_feedback_digest=NULL,
                           pending_stop_terminal_generation=NULL,
                           pending_stop_wait_generation=NULL
                     WHERE scope_id=? AND actor_agent_id=?
                    """,
                    (wake_scope_id, wake_actor_id),
                )
                store.connection.execute(
                    """
                    UPDATE prompt_quarantine
                       SET wake_actor_id=?,wake_scope_id=?,wake_committed=1
                     WHERE prompt_id=?
                    """,
                    (wake_actor_id, wake_scope_id, prompt_id),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "prompt quarantine conflicted with canonical state") from exc
    return {
        "prompt_id": prompt_id,
        "state": "quarantined",
        "reason": "runtime_unverified",
        "idempotent": False,
    }


def bind_quarantined_prompt(
    store: Any,
    prompt_id: str,
    intake_actor_id: str,
    runtime_instance_id: str,
    at: str,
    *,
    wake_scope_id: str | None = None,
    wake: bool = True,
) -> dict[str, Any]:
    _time(at, "prompt binding time")
    if not wake and wake_scope_id is not None:
        raise StorageRefusal("invalid_prompt_wake", "disabled prompt wake cannot name a scope")
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT * FROM prompt_quarantine WHERE prompt_id=?", (prompt_id,)
            ).fetchone()
            if row is None:
                raise StorageRefusal("prompt_unknown", "quarantined prompt does not exist")
            if row["state"] == "bound":
                exact = (
                    row["bound_actor_id"] == intake_actor_id
                    and row["bound_runtime_instance_id"] == runtime_instance_id
                )
                if not exact:
                    raise StorageRefusal("prompt_binding_conflict", "prompt was bound to a different runtime")
                return {"prompt_id": prompt_id, "triage_state": "untriaged", "idempotent": True}
            runtime = store.connection.execute(
                """
                SELECT actor_agent_id,status,verified,session_ref
                  FROM runtime_instances WHERE runtime_instance_id=?
                """,
                (runtime_instance_id,),
            ).fetchone()
            if (
                runtime is None
                or runtime["actor_agent_id"] != intake_actor_id
                or runtime["session_ref"] != row["session_ref"]
                or runtime["status"] not in {"active", "idle"}
                or not runtime["verified"]
            ):
                raise StorageRefusal("runtime_unverified", "binding requires the exact verified hook runtime")
            store.connection.execute(
                """
                INSERT INTO prompts
                  (prompt_id,intake_actor_id,runtime_instance_id,adapter_kind,session_ref,
                   source_event_key,triage_state,triage_digest,created_at,current_owner_agent_id,
                   current_owner_runtime_instance_id)
                VALUES(?,?,?,?,?,?,'untriaged',NULL,?,?,?)
                """,
                (
                    prompt_id, intake_actor_id, runtime_instance_id, row["adapter_kind"],
                    row["session_ref"], row["source_event_key"], row["created_at"],
                    intake_actor_id,
                    runtime_instance_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO prompt_payloads(prompt_id,body,body_hash,byte_count,pruned_at)
                VALUES(?,?,?,?,NULL)
                """,
                (prompt_id, row["body"], row["body_hash"], row["byte_count"]),
            )
            store.connection.execute(
                """
                UPDATE prompt_quarantine
                   SET state='bound',bound_actor_id=?,bound_runtime_instance_id=?,bound_at=?
                 WHERE prompt_id=? AND state='quarantined'
                """,
                (intake_actor_id, runtime_instance_id, at, prompt_id),
            )
            if row["wake_committed"]:
                wake_scope_id = None
            elif wake and wake_scope_id is None:
                scope_row = store.connection.execute(
                    "SELECT scope_id FROM watcher_scopes WHERE actor_agent_id=? ORDER BY scope_id LIMIT 1",
                    (intake_actor_id,),
                ).fetchone()
                if scope_row is not None:
                    wake_scope_id = str(scope_row["scope_id"])
                else:
                    actor = store.connection.execute(
                        "SELECT callsign FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                        (intake_actor_id,),
                    ).fetchone()
                    if actor is not None:
                        wake_scope_id = f"watcher:{actor['callsign']}"
            if wake_scope_id is not None:
                from .sqlite_watcher_ops import ensure_watcher_scope

                ensure_watcher_scope(store, wake_scope_id, intake_actor_id, block_on_obligations=None)
                store.connection.execute(
                    """
                    UPDATE watcher_scopes
                       SET user_message_generation=user_message_generation+1,
                           wait_generation=wait_generation+1,stop_blocked=0,wait_active=0,
                           pending_stop_feedback_digest=NULL,
                           pending_stop_terminal_generation=NULL,
                           pending_stop_wait_generation=NULL
                     WHERE scope_id=?
                    """,
                    (wake_scope_id,),
                )
                store.connection.execute(
                    """
                    UPDATE prompt_quarantine
                       SET wake_actor_id=?,wake_scope_id=?,wake_committed=1
                     WHERE prompt_id=?
                    """,
                    (intake_actor_id, wake_scope_id, prompt_id),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "prompt binding conflicted with canonical state") from exc
    return {"prompt_id": prompt_id, "triage_state": "untriaged", "idempotent": False}


def _normalize_triage_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not 1 <= len(items) <= MAX_PROMPT_ITEMS:
        raise StorageRefusal("invalid_triage", "triage requires one to 32 prompt items")
    normalized: list[dict[str, Any]] = []
    for expected_ordinal, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise StorageRefusal("invalid_triage", "every prompt item must be an object")
        item = {
            "prompt_item_id": str(raw.get("prompt_item_id", "")),
            "ordinal": raw.get("ordinal"),
            "summary": str(raw.get("summary", "")),
            "disposition": str(raw.get("disposition", "")),
            "request_id": raw.get("request_id"),
            "expected_request_version": raw.get("expected_request_version"),
            "next_attention_at": raw.get("next_attention_at"),
        }
        if (
            not item["prompt_item_id"]
            or item["ordinal"] != expected_ordinal
            or not item["summary"]
            or len(item["summary"]) > 512
            or item["disposition"] not in PROMPT_ITEM_DISPOSITIONS
        ):
            raise StorageRefusal("invalid_triage", "prompt item accounting is invalid or out of order")
        if item["disposition"] in {"new_request", "follow_up", "duplicate", "deferred"}:
            if not isinstance(item["request_id"], str) or not item["request_id"]:
                raise StorageRefusal("invalid_triage", "actionable prompt items require a request ID")
        elif item["request_id"] is not None:
            raise StorageRefusal("invalid_triage", "context and acknowledgement items cannot create requests")
        if item["expected_request_version"] is not None and (
            item["disposition"] not in {"follow_up", "duplicate", "deferred"}
            or type(item["expected_request_version"]) is not int
            or item["expected_request_version"] < 1
        ):
            raise StorageRefusal(
                "invalid_triage", "only an existing-request item accepts a positive expected version"
            )
        if item["disposition"] == "deferred":
            if not isinstance(item["next_attention_at"], str) or not item["next_attention_at"]:
                raise StorageRefusal("invalid_triage", "deferred prompt item requires next attention time")
            _time(item["next_attention_at"], "deferred item attention time")
        elif item["next_attention_at"] is not None:
            raise StorageRefusal("invalid_triage", "only deferred prompt items accept next attention time")
        normalized.append(item)
    return normalized


def _triage_counts() -> dict[str, int]:
    return {name: 0 for name in sorted(PROMPT_ITEM_DISPOSITIONS)}


def _persist_triage_item(
    store: Any,
    *,
    prompt_id: str,
    capture_actor_id: str,
    owner_agent_id: str,
    item: dict[str, Any],
    at: str,
) -> None:
    disposition = item["disposition"]
    request_id = item["request_id"]
    if disposition == "new_request":
        store.connection.execute(
            """
            INSERT INTO requests
              (request_id,summary,requester_agent_id,owner_agent_id,return_to_agent_id,
               execution_mode,state,latest_result_id,last_route_event_id,resolution_summary,
               next_attention_at,version,created_at,updated_at)
            VALUES(?,?,?, ?,NULL,NULL,?,NULL,NULL,NULL,?,1,?,?)
            """,
            (
                request_id,
                item["summary"],
                capture_actor_id,
                owner_agent_id,
                "open",
                None,
                at,
                at,
            ),
        )
    elif disposition in {"follow_up", "duplicate", "deferred"}:
        request = _request_row(store, str(request_id))
        expected_version = item["expected_request_version"]
        if expected_version is not None and int(request["version"]) != expected_version:
            raise StorageRefusal(
                "version_conflict",
                "semantic candidate request changed after intake",
                retryable=True,
            )
        if disposition == "deferred":
            if request["owner_agent_id"] != owner_agent_id:
                raise StorageRefusal(
                    "owner_mismatch", "only the current request owner may defer it"
                )
            changed = store.connection.execute(
                """
                UPDATE requests SET state='deferred',next_attention_at=?,version=version+1,
                       updated_at=? WHERE request_id=? AND owner_agent_id=? AND version=?
                """,
                (
                    item["next_attention_at"],
                    at,
                    request_id,
                    owner_agent_id,
                    request["version"],
                ),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("version_conflict", "deferred request changed")
    store.connection.execute(
        """
        INSERT INTO prompt_items(prompt_item_id,prompt_id,ordinal,summary,disposition)
        VALUES(?,?,?,?,?)
        """,
        (
            item["prompt_item_id"],
            prompt_id,
            item["ordinal"],
            item["summary"],
            disposition,
        ),
    )
    if request_id is not None:
        source_role = {
            "new_request": "origin",
            "deferred": "follow_up",
            "follow_up": "follow_up",
            "duplicate": "duplicate",
        }[disposition]
        store.connection.execute(
            "INSERT INTO request_sources(request_id,prompt_item_id,source_role) VALUES(?,?,?)",
            (request_id, item["prompt_item_id"], source_role),
        )


def _triage_prompt_in_transaction(
    store: Any,
    prompt_id: str,
    normalized: list[dict[str, Any]],
    triage_digest: str,
    at: str,
    *,
    expected_owner_agent_id: str | None = None,
) -> dict[str, Any]:
    counts = _triage_counts()
    prompt = store.connection.execute(
        "SELECT intake_actor_id,current_owner_agent_id,triage_state,triage_digest "
        "FROM prompts WHERE prompt_id=?",
        (prompt_id,),
    ).fetchone()
    if prompt is None:
        raise StorageRefusal("prompt_unknown", "prompt does not exist")
    if (
        expected_owner_agent_id is not None
        and prompt["current_owner_agent_id"] != expected_owner_agent_id
    ):
        raise StorageRefusal(
            "owner_mismatch", "triage batch contains a prompt owned by another actor"
        )
    if prompt["triage_state"] == "complete":
        if prompt["triage_digest"] != triage_digest:
            raise StorageRefusal(
                "triage_conflict", "prompt was already triaged with different items"
            )
        persisted = store.connection.execute(
            "SELECT disposition,COUNT(*) count FROM prompt_items WHERE prompt_id=? GROUP BY disposition",
            (prompt_id,),
        ).fetchall()
        for row in persisted:
            counts[row["disposition"]] = int(row["count"])
        return {
            "prompt_id": prompt_id,
            "triage_state": "complete",
            "item_count": len(normalized),
            "request_count": sum(
                1
                for item in normalized
                if item["disposition"] == "new_request"
            ),
            "dispositions": counts,
            "idempotent": True,
        }
    for item in normalized:
        counts[item["disposition"]] += 1
        _persist_triage_item(
            store,
            prompt_id=prompt_id,
            capture_actor_id=prompt["intake_actor_id"],
            owner_agent_id=prompt["current_owner_agent_id"],
            item=item,
            at=at,
        )
    store.connection.execute(
        "UPDATE prompts SET triage_state='complete',triage_digest=? WHERE prompt_id=?",
        (triage_digest, prompt_id),
    )
    return {
        "prompt_id": prompt_id,
        "triage_state": "complete",
        "item_count": len(normalized),
        "request_count": counts["new_request"],
        "dispositions": counts,
        "idempotent": False,
    }


def triage_prompt(
    store: Any,
    prompt_id: str,
    items: list[dict[str, Any]],
    at: str,
) -> dict[str, Any]:
    _time(at, "triage time")
    normalized = _normalize_triage_items(items)
    triage_digest = _digest(_json(normalized))
    try:
        with store._transaction():
            result = _triage_prompt_in_transaction(
                store, prompt_id, normalized, triage_digest, at
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "prompt triage conflicted with canonical state") from exc
    return result


def triage_prompt_batch(
    store: Any,
    owner_agent_id: str,
    expected_prompt_ids: tuple[str, ...],
    decisions: list[dict[str, Any]],
    at: str,
) -> dict[str, Any]:
    """Commit one exact turn's model-authored decisions in one transaction."""

    _time(at, "triage time")
    if not 0 <= len(expected_prompt_ids) <= MAX_TRIAGE_TURN_PROMPTS:
        raise StorageRefusal("invalid_triage_batch", "triage turn prompt count is invalid")
    if len(set(expected_prompt_ids)) != len(expected_prompt_ids):
        raise StorageRefusal("invalid_triage_batch", "triage turn prompt identities are duplicated")
    if not isinstance(decisions, list) or len(decisions) != len(expected_prompt_ids):
        raise StorageRefusal(
            "incomplete_triage_batch", "triage turn must decide every fetched prompt exactly once"
        )
    prepared: list[tuple[str, list[dict[str, Any]], str]] = []
    decision_prompt_ids: list[str] = []
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {"prompt_id", "items"}:
            raise StorageRefusal(
                "invalid_triage_batch", "each triage decision must contain only prompt_id and items"
            )
        prompt_id = decision["prompt_id"]
        items = decision["items"]
        if not isinstance(prompt_id, str) or not isinstance(items, list):
            raise StorageRefusal("invalid_triage_batch", "triage decision shape is invalid")
        normalized = _normalize_triage_items(items)
        prepared.append((prompt_id, normalized, _digest(_json(normalized))))
        decision_prompt_ids.append(prompt_id)
    if tuple(decision_prompt_ids) != expected_prompt_ids:
        raise StorageRefusal(
            "incomplete_triage_batch",
            "triage decisions must match the fetched prompt identities and order exactly",
        )
    try:
        with store._transaction():
            owner = store.connection.execute(
                "SELECT role,retired_at FROM agent_instances WHERE agent_id=?",
                (owner_agent_id,),
            ).fetchone()
            if owner is None or owner["role"] != "shotcaller" or owner["retired_at"] is not None:
                raise StorageRefusal(
                    "owner_invalid", "triage batch requires one live Shotcaller owner"
                )
            receipts = [
                _triage_prompt_in_transaction(
                    store,
                    prompt_id,
                    normalized,
                    digest,
                    at,
                    expected_owner_agent_id=owner_agent_id,
                )
                for prompt_id, normalized, digest in prepared
            ]
            linked_request_ids = tuple(
                dict.fromkeys(
                    str(item["request_id"])
                    for _, normalized, _ in prepared
                    for item in normalized
                    if item["request_id"] is not None
                )
            )
            readiness: list[dict[str, Any]] = []
            for request_id in linked_request_ids:
                request = _request_row(store, request_id)
                claim = store.connection.execute(
                    "SELECT released_at FROM request_claims WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                has_claim = claim is not None and claim["released_at"] is None
                readiness.append(
                    {
                        "request_id": request_id,
                        "state": request["state"],
                        "version": int(request["version"]),
                        "claim_required": not has_claim,
                        "dispatch_ready": request["state"] in {"open", "accepted"} and has_claim,
                    }
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "prompt triage batch conflicted with canonical state"
        ) from exc
    return {
        "owner_agent_id": owner_agent_id,
        "prompt_count": len(receipts),
        "triage": receipts,
        "dispatch_readiness": readiness,
        "idempotent": bool(receipts) and all(item["idempotent"] for item in receipts),
    }


def begin_request_turn(
    store: Any,
    owner_agent_id: str,
    expected_prompt_ids: tuple[str, ...],
    decisions: list[dict[str, Any]],
    plans: tuple[TurnDispatchPlan, ...],
    at: str,
    *,
    expected_candidate_digest: str | None = None,
    candidate_limit: int = 12,
    candidate_max_bytes: int = 24_576,
) -> dict[str, Any]:
    """Triage, claim, and record each new request's routing plan atomically."""

    planned_request_ids = tuple(plan.command.request_id for plan in plans)
    if len(set(planned_request_ids)) != len(planned_request_ids):
        raise StorageRefusal("invalid_turn_plan", "turn routing plans contain duplicate requests")
    new_request_ids = tuple(
        str(item.get("request_id"))
        for decision in decisions
        if isinstance(decision, dict)
        for item in decision.get("items", [])
        if isinstance(item, dict) and item.get("disposition") == "new_request"
    )
    if planned_request_ids != new_request_ids:
        raise StorageRefusal(
            "incomplete_turn_plan",
            "turn routing plans must match each new request identity and order exactly",
        )
    if any(plan.command.at != at for plan in plans):
        raise StorageRefusal("invalid_turn_plan", "turn routing plan timestamps must be exact")
    external_plans = tuple(
        plan for plan in plans if _dispatch_classification(plan.command)[0] != "direct"
    )
    if external_plans and not expected_candidate_digest:
        raise StorageRefusal(
            "candidate_inventory_required",
            "new request dispatch requires the exact same-owner candidate inventory digest",
        )
    try:
        with store._transaction():
            if external_plans:
                current_candidates = _candidate_request_inventory(
                    store,
                    owner_agent_id,
                    limit=candidate_limit,
                    max_bytes=candidate_max_bytes,
                )
                if current_candidates["truncated"]:
                    raise StorageRefusal(
                        "candidate_inventory_truncated",
                        "same-owner duplicate check is incomplete; external dispatch is refused",
                        retryable=True,
                    )
                if current_candidates["snapshot_digest"] != expected_candidate_digest:
                    raise StorageRefusal(
                        "version_conflict",
                        "same-owner candidate inventory changed before dispatch",
                        retryable=True,
                    )
            batch = triage_prompt_batch(
                store,
                owner_agent_id,
                expected_prompt_ids,
                decisions,
                at,
            )
            routed: list[dict[str, Any]] = []
            for plan in plans:
                claim = claim_request(
                    store,
                    plan.command.request_id,
                    plan.runtime_instance_id,
                    plan.claim_token,
                    plan.leased_until,
                    at,
                )
                dispatch = dispatch_request(store, plan.command)
                routed.append({"claim": claim, "dispatch": dispatch})
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "request turn begin conflicted with canonical state"
        ) from exc
    return {"batch": batch, "routing": routed}


def commit_request_turn(
    store: Any,
    owner_agent_id: str,
    actions: tuple[AnswerRequestCommand | RequestResultCommand, ...],
    at: str,
) -> dict[str, Any]:
    """Commit bounded direct answers/results and their delivery effects atomically."""

    _time(at, "turn commit time")
    if not 0 <= len(actions) <= 100:
        raise StorageRefusal("invalid_turn_commit", "turn commit action count is invalid")
    if any(action.at != at for action in actions):
        raise StorageRefusal("invalid_turn_commit", "turn commit timestamps must be exact")
    try:
        with store._transaction():
            receipts: list[dict[str, Any]] = []
            for action in actions:
                request = _request_row(store, action.request_id)
                if request["owner_agent_id"] != owner_agent_id:
                    raise StorageRefusal(
                        "owner_mismatch", "turn commit action belongs to another request owner"
                    )
                if isinstance(action, AnswerRequestCommand):
                    receipts.append(answer_request(store, action))
                else:
                    receipts.append(record_request_result(store, action))
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "request turn commit conflicted with canonical state"
        ) from exc
    return {"owner_agent_id": owner_agent_id, "actions": receipts}


def request_turn_boundary(store: Any, owner_agent_id: str) -> dict[str, Any]:
    """Return the full Stop-equivalent obligation boundary on the open connection."""

    from .sqlite_watcher_ops import _obligation_counts

    requests = unresolved_requests(store, owner_agent_id)
    obligations = _obligation_counts(store, owner_agent_id)
    return {
        **requests,
        "obligations": obligations,
        "safe_to_finish": sum(obligations.values()) == 0,
    }


def _existing_reconciliation_receipt(
    store: Any, command: ReconcileDuplicateRequestCommand
) -> dict[str, Any] | None:
    existing = store.connection.execute(
        "SELECT * FROM request_reconciliations WHERE duplicate_request_id=?",
        (command.duplicate_request_id,),
    ).fetchone()
    if existing is None:
        return None
    exact = (
        existing["canonical_request_id"] == command.canonical_request_id
        and existing["actor_agent_id"] == command.owner_agent_id
        and int(existing["duplicate_version_before"])
        == command.expected_duplicate_version
        and int(existing["canonical_version_at_link"])
        == command.expected_canonical_version
    )
    if not exact:
        raise StorageRefusal(
            "reconciliation_conflict",
            "duplicate request already has a different reconciliation",
        )
    duplicate = _request_row(store, command.duplicate_request_id)
    return {
        "schema": "league.request-reconciliation.v1",
        "duplicate_request_id": command.duplicate_request_id,
        "canonical_request_id": command.canonical_request_id,
        "duplicate_state": duplicate["state"],
        "duplicate_version": int(duplicate["version"]),
        "canonical_version": int(existing["canonical_version_at_link"]),
        "idempotent": True,
    }


def _validated_reconciliation_requests(
    store: Any, command: ReconcileDuplicateRequestCommand
) -> tuple[Any, Any]:
    duplicate = _request_row(store, command.duplicate_request_id)
    canonical = _request_row(store, command.canonical_request_id)
    if (
        duplicate["owner_agent_id"] != command.owner_agent_id
        or canonical["owner_agent_id"] != command.owner_agent_id
        or duplicate["owner_agent_id"] != canonical["owner_agent_id"]
        or duplicate["owner_squad_id"] != canonical["owner_squad_id"]
    ):
        raise StorageRefusal(
            "owner_mismatch",
            "duplicate reconciliation requires the same current owner and Squad",
        )
    if (
        int(duplicate["version"]) != command.expected_duplicate_version
        or int(canonical["version"]) != command.expected_canonical_version
    ):
        raise StorageRefusal(
            "version_conflict", "request changed before reconciliation", retryable=True
        )
    if (
        duplicate["state"] in TERMINAL_REQUEST_STATES
        or canonical["state"] in TERMINAL_REQUEST_STATES
    ):
        raise StorageRefusal(
            "reconciliation_conflict",
            "terminal requests refuse duplicate reconciliation",
        )
    chain = store.connection.execute(
        """
        SELECT 1 FROM request_reconciliations
         WHERE duplicate_request_id=? OR canonical_request_id=? LIMIT 1
        """,
        (command.canonical_request_id, command.duplicate_request_id),
    ).fetchone()
    if chain is not None:
        raise StorageRefusal(
            "reconciliation_cycle",
            "duplicate reconciliation chains and cycles are refused",
        )
    evidence_queries = (
        (
            "SELECT 1 FROM request_dispatches "
            "WHERE request_id=? AND execution_mode<>'direct' LIMIT 1"
        ),
        "SELECT 1 FROM tasks WHERE request_id=? LIMIT 1",
        "SELECT 1 FROM request_results WHERE request_id=? LIMIT 1",
    )
    if any(
        store.connection.execute(query, (command.duplicate_request_id,)).fetchone()
        is not None
        for query in evidence_queries
    ):
        raise StorageRefusal(
            "irreversible_execution_started",
            "duplicate request has execution or result evidence and requires separate resolution",
        )
    return duplicate, canonical


def _persist_request_reconciliation(
    store: Any, command: ReconcileDuplicateRequestCommand, duplicate: Any
) -> dict[str, Any]:
    next_version = int(duplicate["version"]) + 1
    changed = store.connection.execute(
        """
        UPDATE requests
           SET state='cancelled',resolution_summary=?,version=?,updated_at=?
         WHERE request_id=? AND owner_agent_id=? AND version=?
        """,
        (
            "Superseded by the canonical same-owner request.",
            next_version,
            command.at,
            command.duplicate_request_id,
            command.owner_agent_id,
            command.expected_duplicate_version,
        ),
    )
    if changed.rowcount != 1:
        raise StorageRefusal(
            "version_conflict", "duplicate request changed", retryable=True
        )
    store.connection.execute(
        "UPDATE request_claims SET released_at=? "
        "WHERE request_id=? AND released_at IS NULL",
        (command.at, command.duplicate_request_id),
    )
    event_id = "request-reconciliation:" + _digest(
        _json(
            {
                "duplicate": command.duplicate_request_id,
                "canonical": command.canonical_request_id,
                "duplicate_version": command.expected_duplicate_version,
                "canonical_version": command.expected_canonical_version,
            }
        )
    )
    _insert_request_event(
        store,
        event_id=event_id,
        request_id=command.duplicate_request_id,
        actor_id=command.owner_agent_id,
        request_version=next_version,
        event_type="request_superseded",
        state="cancelled",
        update="Duplicate request superseded by a canonical same-owner request.",
        at=command.at,
        detail={"canonical_request_id": command.canonical_request_id},
    )
    store.connection.execute(
        """
        INSERT INTO request_reconciliations
          (duplicate_request_id,canonical_request_id,actor_agent_id,
           duplicate_version_before,canonical_version_at_link,event_id,reconciled_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            command.duplicate_request_id,
            command.canonical_request_id,
            command.owner_agent_id,
            command.expected_duplicate_version,
            command.expected_canonical_version,
            event_id,
            command.at,
        ),
    )
    return {
        "schema": "league.request-reconciliation.v1",
        "duplicate_request_id": command.duplicate_request_id,
        "canonical_request_id": command.canonical_request_id,
        "duplicate_state": "cancelled",
        "duplicate_version": next_version,
        "canonical_version": command.expected_canonical_version,
        "idempotent": False,
    }


def reconcile_duplicate_request(
    store: Any, command: ReconcileDuplicateRequestCommand
) -> dict[str, Any]:
    """Supersede one same-owner duplicate without erasing either request's provenance."""

    _time(command.at, "request reconciliation time")
    if command.duplicate_request_id == command.canonical_request_id:
        raise StorageRefusal("invalid_reconciliation", "a request cannot supersede itself")
    if command.expected_duplicate_version < 1 or command.expected_canonical_version < 1:
        raise StorageRefusal(
            "invalid_reconciliation", "expected request versions must be positive"
        )
    try:
        with store._transaction():
            receipt = _existing_reconciliation_receipt(store, command)
            if receipt is not None:
                return receipt
            duplicate, _canonical = _validated_reconciliation_requests(store, command)
            return _persist_request_reconciliation(store, command, duplicate)
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "request reconciliation conflicted with canonical state"
        ) from exc


def claim_request(
    store: Any,
    request_id: str,
    runtime_instance_id: str,
    claim_token: str,
    leased_until: str,
    at: str,
) -> dict[str, Any]:
    now = _time(at, "claim time")
    if _time(leased_until, "claim expiry") <= now:
        raise StorageRefusal("invalid_claim", "request claim expiry must be in the future")
    proof = _claim_hash(claim_token)
    recovered = False
    accepted = False
    try:
        with store._transaction():
            request = _request_row(store, request_id)
            runtime = store.connection.execute(
                "SELECT actor_agent_id,status,verified FROM runtime_instances WHERE runtime_instance_id=?",
                (runtime_instance_id,),
            ).fetchone()
            if (
                runtime is None
                or runtime["status"] not in {"active", "idle"}
                or not runtime["verified"]
            ):
                raise StorageRefusal("runtime_unverified", "request claim runtime is not verified and active")
            claim_owner = (
                request["pending_owner_agent_id"]
                if request["state"] == "routed"
                else request["owner_agent_id"]
            )
            if runtime["actor_agent_id"] != claim_owner:
                raise StorageRefusal("owner_mismatch", "request claim runtime does not belong to the owner")
            existing = store.connection.execute(
                "SELECT * FROM request_claims WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing is not None and existing["released_at"] is None:
                same = (
                    existing["runtime_instance_id"] == runtime_instance_id
                    and hmac.compare_digest(str(existing["claim_proof_hash"]), proof)
                )
                if same:
                    store.connection.execute(
                        "UPDATE request_claims SET leased_until=? WHERE request_id=?",
                        (leased_until, request_id),
                    )
                    return {
                        "request_id": request_id,
                        "runtime_instance_id": runtime_instance_id,
                        "claim_version": int(existing["claim_version"]),
                        "state": request["state"],
                        "accepted": False,
                        "recovered": False,
                        "idempotent": True,
                    }
                if _time(str(existing["leased_until"]), "stored claim expiry") > now:
                    raise StorageRefusal("request_claimed", "request already has an unexpired claim")
                next_claim_version = int(existing["claim_version"]) + 1
                recovered = True
                store.connection.execute(
                    """
                    UPDATE request_claims
                       SET runtime_instance_id=?,claim_proof_hash=?,leased_until=?,claim_version=?,
                           claimed_at=?,released_at=NULL
                     WHERE request_id=?
                    """,
                    (runtime_instance_id, proof, leased_until, next_claim_version, at, request_id),
                )
                event_id = f"request:{request_id}:claim:{next_claim_version}"
                _insert_request_event(
                    store,
                    event_id=event_id,
                    request_id=request_id,
                    actor_id=claim_owner,
                    request_version=int(request["version"]),
                    event_type="request_claim_recovered",
                    state=request["state"],
                    update="expired request claim recovered",
                    at=at,
                )
            else:
                next_claim_version = 1 if existing is None else int(existing["claim_version"]) + 1
                store.connection.execute(
                    """
                    INSERT INTO request_claims
                      (request_id,runtime_instance_id,claim_proof_hash,leased_until,claim_version,claimed_at,released_at)
                    VALUES(?,?,?,?,?,?,NULL)
                    ON CONFLICT(request_id) DO UPDATE SET
                      runtime_instance_id=excluded.runtime_instance_id,
                      claim_proof_hash=excluded.claim_proof_hash,
                      leased_until=excluded.leased_until,
                      claim_version=excluded.claim_version,
                      claimed_at=excluded.claimed_at,
                      released_at=NULL
                    """,
                    (request_id, runtime_instance_id, proof, leased_until, next_claim_version, at),
                )
                _insert_request_event(
                    store,
                    event_id=f"request:{request_id}:claim:{next_claim_version}",
                    request_id=request_id,
                    actor_id=claim_owner,
                    request_version=int(request["version"]),
                    event_type="request_claimed",
                    state=request["state"],
                    update="request mutation claim acquired",
                    at=at,
                )
            if request["state"] == "routed":
                route_event = request["last_route_event_id"]
                receipt = store.connection.execute(
                    "SELECT 1 FROM recipient_receipts WHERE event_id=? AND recipient_agent_id=?",
                    (route_event, claim_owner),
                ).fetchone()
                if receipt is None:
                    raise StorageRefusal("route_unreceived", "routed request cannot be accepted before exact receipt")
                next_version = int(request["version"]) + 1
                store.connection.execute(
                    """
                    UPDATE requests SET owner_agent_id=pending_owner_agent_id,
                      owner_squad_id=pending_owner_squad_id,pending_owner_agent_id=NULL,
                      pending_owner_squad_id=NULL,state='accepted',version=?,updated_at=?
                     WHERE request_id=?
                    """,
                    (next_version, at, request_id),
                )
                _insert_request_event(
                    store,
                    event_id=f"request:{request_id}:{next_version}:accepted",
                    request_id=request_id,
                    actor_id=claim_owner,
                    request_version=next_version,
                    event_type="request_accepted",
                    state="accepted",
                    update="routed request accepted",
                    at=at,
                    source_event_id=route_event,
                )
                progress_event_id = f"request:{request_id}:{next_version}:route-accepted"
                progress_outbox_id = f"outbox:{request_id}:{next_version}:route-accepted"
                progress_value = {
                    "settled_count": 0,
                    "total_count": 0,
                    "current_phase": "Route accepted",
                    "blocker_count": 0,
                    "blocker_severity": "none",
                    "user_action_required": False,
                    "deadline_change": None,
                    "next_action": "The accepted owner continues the request",
                }
                _insert_request_event(
                    store,
                    event_id=progress_event_id,
                    request_id=request_id,
                    actor_id=claim_owner,
                    request_version=next_version,
                    event_type="request_progress",
                    state="accepted",
                    update="Route accepted by the selected Squad owner",
                    at=at,
                    detail={
                        "reason_code": "route_accepted",
                        "progress_generation": next_version,
                        **progress_value,
                    },
                    source_event_id=route_event,
                )
                store.connection.execute(
                    """
                    INSERT INTO delivery_outbox
                      (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                    VALUES(?,?,?,'pending',?,0)
                    """,
                    (progress_outbox_id, progress_event_id, request["requester_agent_id"], at),
                )
                store.connection.execute(
                    """
                    INSERT INTO request_progress_events
                      (progress_id,request_id,request_generation,progress_generation,
                       owner_agent_id,recipient_agent_id,urgency,reason_code,content_digest,
                       settled_count,total_count,current_phase,blocker_count,blocker_severity,
                       user_action_required,deadline_change,next_action,event_id,outbox_id,emitted_at)
                    VALUES(?,?,?,?,?,?,'immediate','route_accepted',?,?,?,?,?,'none',0,NULL,?,?,?,?)
                    """,
                    (
                        f"progress:{request_id}:{next_version}:route-accepted",
                        request_id,
                        next_version,
                        next_version,
                        claim_owner,
                        request["requester_agent_id"],
                        _digest(_json(progress_value)),
                        0,
                        0,
                        progress_value["current_phase"],
                        0,
                        progress_value["next_action"],
                        progress_event_id,
                        progress_outbox_id,
                        at,
                    ),
                )
                accepted = True
                request_state = "accepted"
            else:
                request_state = request["state"]
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "request claim conflicted with canonical state") from exc
    return {
        "request_id": request_id,
        "runtime_instance_id": runtime_instance_id,
        "claim_version": next_claim_version,
        "state": request_state,
        "accepted": accepted,
        "recovered": recovered,
        "idempotent": False,
    }


def release_request_claim(
    store: Any,
    request_id: str,
    runtime_instance_id: str,
    claim_token: str,
    at: str,
) -> dict[str, Any]:
    _time(at, "claim release time")
    try:
        with store._transaction():
            claim = _active_claim(
                store,
                request_id,
                token=claim_token,
                runtime_instance_id=runtime_instance_id,
            )
            store.connection.execute(
                "UPDATE request_claims SET released_at=? WHERE request_id=?",
                (at, request_id),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "request claim release failed") from exc
    return {"request_id": request_id, "claim_version": int(claim["claim_version"]), "released": True}


def classify_dispatch(
    *,
    work_kind: str,
    requested_mode: Optional[str],
    hidden_supported: bool,
    signals: OrchestrationSignals,
    explicit_squad_id: Optional[str],
    continuation_squad_id: Optional[str],
) -> tuple[str, str, str]:
    if requested_mode is not None and requested_mode not in EXECUTION_MODES:
        raise StorageRefusal("invalid_dispatch", "requested execution mode is invalid")
    if work_kind not in CHAMPION_WORK_KINDS | DIRECT_WORK_KINDS:
        raise StorageRefusal("invalid_dispatch", "work kind is not part of the bounded classifier contract")
    if requested_mode == "hidden" and not hidden_supported:
        raise StorageRefusal("hidden_unavailable", "hidden advisory support is unavailable")
    if work_kind in CHAMPION_WORK_KINDS and requested_mode in {"direct", "hidden"}:
        raise StorageRefusal(
            "champion_required",
            "repository, durable research, benchmark, release, operational, and debugging work requires a visible Champion",
        )
    force_local_champion = requested_mode == "champion"
    force_local_direct = requested_mode == "direct"
    decision = decide_orchestration_route(
        signals,
        explicit_squad_id=explicit_squad_id if requested_mode == "squad" else None,
        continuation_squad_id=(
            continuation_squad_id if requested_mode not in {"direct", "champion"} else None
        ),
    )
    if decision.route == SQUAD_ROUTE:
        raise StorageRefusal(
            "squad_route_required",
            "Squad ownership must use the acknowledgement-gated durable request-route operation",
        )
    if force_local_champion:
        decision = decision.__class__(LOCAL_CHAMPION, "explicit_champion", None, True)
    elif work_kind in CHAMPION_WORK_KINDS:
        decision = decision.__class__(LOCAL_CHAMPION, "worker_required", None, True)
    if force_local_direct and decision.route != LOCAL_DIRECT:
        raise StorageRefusal(
            "champion_required",
            "work outside every direct-tiny bound requires a visible Champion assignment receipt",
        )
    if requested_mode == "hidden":
        if not signals.hidden_scientist():
            raise StorageRefusal(
                "champion_required",
                "hidden scientists must stop at the bounded read-only boundary and promote to a visible Champion",
            )
        return "hidden", "hidden_scientist", "bounded read-only scientist support is recorded"
    if decision.route == LOCAL_CHAMPION:
        if requested_mode == "direct":
            raise StorageRefusal(
                "champion_required",
                "work outside every direct-tiny bound requires a visible Champion assignment receipt",
            )
        return "champion", decision.reason_code, "visible Champion ownership is required"
    return "direct", decision.reason_code, "every direct-tiny bound is satisfied"


def _validate_hidden_scientist(
    store: Any, command: DispatchRequestCommand, owner_agent_id: str
) -> str:
    values = (
        command.hidden_subtask,
        command.hidden_scope_budget,
        command.requested_model,
        command.requested_effort,
    )
    if not all(isinstance(value, str) and value for value in values):
        raise StorageRefusal(
            "hidden_scientist_incomplete",
            "hidden scientist dispatch requires subtask, scope budget, model, and effort",
        )
    subtask = _bounded_public_text(str(command.hidden_subtask), "hidden subtask")
    _bounded_public_text(str(command.hidden_scope_budget), "hidden scope budget", maximum=256)
    owner = store.connection.execute(
        "SELECT role,retired_at FROM agent_instances WHERE agent_id=?",
        (owner_agent_id,),
    ).fetchone()
    if owner is None or owner["role"] != "shotcaller" or owner["retired_at"] is not None:
        raise StorageRefusal("hidden_owner_invalid", "hidden scientist owner must be a live Shotcaller")
    signals = command.orchestration
    if not 1 <= signals.expected_minutes <= 5 or not 1 <= signals.expected_task_action_calls <= 2:
        raise StorageRefusal(
            "hidden_scientist_budget_invalid",
            "hidden scientist requires explicit bounded time and scope budgets",
        )
    return subtask


def _dispatch_classification(
    command: DispatchRequestCommand,
) -> tuple[str, str, str]:
    signal_value = command.orchestration.as_record()
    signal_value["hidden_advisory"] = command.requested_mode == "hidden"
    return classify_dispatch(
        work_kind=command.work_kind,
        requested_mode=command.requested_mode,
        hidden_supported=command.hidden_supported,
        signals=OrchestrationSignals(**signal_value),
        explicit_squad_id=command.explicit_route,
        continuation_squad_id=command.continuation_target,
    )


def dispatch_request(
    store: Any,
    command: DispatchRequestCommand,
) -> dict[str, Any]:
    request_id = command.request_id
    claim_token = command.claim_token
    dispatch_id = command.dispatch_id
    work_kind = command.work_kind
    requested_mode = command.requested_mode
    hidden_supported = command.hidden_supported
    requested_model = command.requested_model
    requested_effort = command.requested_effort
    explicit_route = command.explicit_route
    at = command.at
    _time(at, "dispatch time")
    signal_value = command.orchestration.as_record()
    signal_value["hidden_advisory"] = requested_mode == "hidden"
    hidden_value = {
        "hidden_subtask": command.hidden_subtask,
        "hidden_scope_budget": command.hidden_scope_budget,
    }
    mode, reason_code, reason = _dispatch_classification(command)
    try:
        with store._transaction():
            request = _request_row(store, request_id)
            _active_claim(store, request_id, token=claim_token, at=at)
            existing = store.connection.execute(
                "SELECT * FROM request_dispatches WHERE request_id=? AND request_version=?",
                (request_id, int(request["version"])),
            ).fetchone()
            if existing is not None:
                exact = (
                    existing["dispatch_id"] == dispatch_id
                    and existing["execution_mode"] == mode
                    and existing["work_kind"] == work_kind
                    and existing["requested_mode"] == requested_mode
                    and bool(existing["input_json"] == _json({
                        "work_kind": work_kind,
                        "requested_mode": requested_mode,
                        "hidden_supported": hidden_supported,
                        "signals": signal_value,
                        "continuation_role": command.continuation_role,
                        "continuation_target": command.continuation_target,
                        "hidden_scientist": hidden_value,
                    }))
                    and existing["requested_model"] == requested_model
                    and existing["requested_effort"] == requested_effort
                    and existing["explicit_route"] == explicit_route
                )
                if not exact:
                    raise StorageRefusal("dispatch_conflict", "request version already has a different dispatch")
                return {
                    "request_id": request_id,
                    "execution_mode": mode,
                    "reason": existing["reason"],
                    "request_version": int(request["version"]),
                    "idempotent": True,
                }
            if request["state"] not in {"open", "accepted", "blocked", "deferred"}:
                raise StorageRefusal("dispatch_conflict", "request state does not permit classification")
            hidden_subtask: Optional[str] = None
            if mode == "hidden":
                hidden_subtask = _validate_hidden_scientist(
                    store, command, str(request["owner_agent_id"])
                )
            elif any(hidden_value.values()):
                raise StorageRefusal(
                    "hidden_scientist_unexpected",
                    "hidden scientist identity is valid only for hidden execution",
                )
            next_version = int(request["version"]) + 1
            input_value = {
                "work_kind": work_kind,
                "requested_mode": requested_mode,
                "hidden_supported": hidden_supported,
                "signals": signal_value,
                "continuation_role": command.continuation_role,
                "continuation_target": command.continuation_target,
                "hidden_scientist": hidden_value,
            }
            store.connection.execute(
                """
                INSERT INTO request_dispatches
                  (dispatch_id,request_id,request_version,work_kind,execution_mode,reason,reason_code,
                   requested_mode,requested_model,requested_effort,explicit_route,input_json,decided_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    dispatch_id,
                    request_id,
                    next_version,
                    work_kind,
                    mode,
                    reason,
                    reason_code,
                    requested_mode,
                    requested_model,
                    requested_effort,
                    explicit_route,
                    _json(input_value),
                    at,
                ),
            )
            store.connection.execute(
                """
                UPDATE requests SET execution_mode=?,state='in_progress',version=?,updated_at=?
                 WHERE request_id=? AND version=?
                """,
                (mode, next_version, at, request_id, int(request["version"])),
            )
            dispatch_event_id = f"request:{request_id}:{next_version}:dispatched"
            _insert_request_event(
                store,
                event_id=dispatch_event_id,
                request_id=request_id,
                actor_id=request["owner_agent_id"],
                request_version=next_version,
                event_type="request_dispatched",
                state="in_progress",
                update=reason,
                at=at,
                detail={
                    "execution_mode": mode,
                    "reason_code": reason_code,
                    "requested_model": requested_model,
                    "requested_effort": requested_effort,
                    "explicit_route": explicit_route,
                },
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "request dispatch conflicted with canonical state") from exc
    return {
        "request_id": request_id,
        "execution_mode": mode,
        "reason": reason,
        "reason_code": reason_code,
        "request_version": next_version,
        "idempotent": False,
    }


def route_request(
    store: Any,
    request_id: str,
    claim_token: str,
    expected_version: int,
    recipient_agent_id: str,
    event_id: str,
    outbox_id: str,
    at: str,
    *,
    recipient_squad_id: Optional[str] = None,
    route_reason_code: str = "explicit_squad",
    route_policy_version: str = "league.orchestration.v1",
    route_confidence: str = "explicit",
    required_capabilities: tuple[str, ...] = (),
) -> dict[str, Any]:
    _time(at, "route time")
    if (
        not recipient_squad_id
        or route_reason_code not in {"explicit_squad", "continuation_squad", "unique_strong_squad"}
        or not route_policy_version
        or route_confidence not in {"explicit", "continuation", "strong"}
        or len(set(required_capabilities)) != len(required_capabilities)
        or any(not item for item in required_capabilities)
    ):
        raise StorageRefusal("invalid_route", "Squad route policy evidence is incomplete")
    try:
        with store._transaction():
            request = _request_row(store, request_id)
            if int(request["version"]) != expected_version:
                existing = store.connection.execute(
                    "SELECT outbox_id FROM delivery_outbox WHERE event_id=? AND recipient_agent_id=?",
                    (event_id, recipient_agent_id),
                ).fetchone()
                if existing is not None and request["last_route_event_id"] == event_id:
                    return {
                        "request_id": request_id,
                        "owner_agent_id": request["owner_agent_id"],
                        "owner_squad_id": request["owner_squad_id"],
                        "pending_owner_agent_id": request["pending_owner_agent_id"],
                        "pending_owner_squad_id": request["pending_owner_squad_id"],
                        "state": request["state"],
                        "version": int(request["version"]),
                        "event_id": event_id,
                        "outbox_id": existing["outbox_id"],
                        "idempotent": True,
                    }
                raise StorageRefusal("version_conflict", "request route expected-version failed")
            _active_claim(store, request_id, token=claim_token, at=at)
            recipient = store.connection.execute(
                """
                SELECT s.owner_fence,i.state intake_state,i.fence intake_fence,
                       a.role,a.retired_at
                  FROM squads s
                  JOIN agent_instances a ON a.agent_id=s.shotcaller_agent_id
                  JOIN shotcaller_intake i
                    ON i.squad_id=s.squad_id AND i.agent_id=s.shotcaller_agent_id
                 WHERE s.squad_id=? AND s.state='active' AND s.shotcaller_agent_id=?
                """,
                (recipient_squad_id, recipient_agent_id),
            ).fetchone()
            if (
                recipient is None
                or recipient["role"] != "shotcaller"
                or recipient["retired_at"] is not None
                or recipient["intake_state"] != "accepting"
                or int(recipient["intake_fence"]) != int(recipient["owner_fence"])
            ):
                raise StorageRefusal("owner_unavailable", "target Squad has no accepting current Shotcaller")
            required = set(required_capabilities)
            declared = {
                str(row["capability"])
                for row in store.connection.execute(
                    "SELECT capability FROM squad_capabilities WHERE squad_id=?",
                    (recipient_squad_id,),
                )
            }
            if not required <= declared:
                raise StorageRefusal(
                    "owner_capability_mismatch",
                    "target Squad has not declared every required capability",
                )
            runtimes = store.connection.execute(
                """
                SELECT capabilities_json FROM runtime_instances
                 WHERE actor_agent_id=? AND status IN ('active','idle') AND verified=1
                 ORDER BY last_seen_at DESC
                """,
                (recipient_agent_id,),
            )
            if not any(
                required <= set(json.loads(row["capabilities_json"])) for row in runtimes
            ):
                raise StorageRefusal(
                    "owner_capability_mismatch",
                    "target Squad's current live owner lacks required capabilities",
                )
            next_version = expected_version + 1
            return_to = request["return_to_agent_id"] or request["owner_agent_id"]
            _insert_request_event(
                store,
                event_id=event_id,
                request_id=request_id,
                actor_id=request["owner_agent_id"],
                request_version=next_version,
                event_type="request_routed",
                state="routed",
                update="request offered to the selected Squad owner",
                at=at,
                detail={
                    "recipient_squad_id": recipient_squad_id,
                    "route_reason_code": route_reason_code,
                    "route_policy_version": route_policy_version,
                    "route_confidence": route_confidence,
                    "required_capabilities": list(required_capabilities),
                },
            )
            store.connection.execute(
                """
                UPDATE requests SET pending_owner_agent_id=?,pending_owner_squad_id=?,
                  return_to_agent_id=?,route_reason_code=?,route_policy_version=?,route_confidence=?,
                  state='routed',last_route_event_id=?,version=?,updated_at=?
                 WHERE request_id=? AND version=?
                """,
                (
                    recipient_agent_id,
                    recipient_squad_id,
                    return_to,
                    route_reason_code,
                    route_policy_version,
                    route_confidence,
                    event_id,
                    next_version,
                    at,
                    request_id,
                    expected_version,
                ),
            )
            store.connection.execute(
                "UPDATE request_claims SET released_at=? WHERE request_id=?", (at, request_id)
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
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "request route conflicted with canonical state") from exc
    return {
        "request_id": request_id,
        "owner_agent_id": request["owner_agent_id"],
        "owner_squad_id": request["owner_squad_id"],
        "pending_owner_agent_id": recipient_agent_id,
        "pending_owner_squad_id": recipient_squad_id,
        "state": "routed",
        "version": next_version,
        "event_id": event_id,
        "outbox_id": outbox_id,
        "idempotent": False,
    }


def set_request_state(
    store: Any,
    request_id: str,
    claim_token: str,
    expected_version: int,
    state: str,
    summary: str,
    event_id: str,
    at: str,
    *,
    next_attention_at: Optional[str] = None,
) -> dict[str, Any]:
    _time(at, "request transition time")
    if state not in {"awaiting_user", "blocked", "deferred", "cancelled"} or not summary:
        raise StorageRefusal("invalid_request_transition", "request transition is invalid")
    if state == "deferred" and not next_attention_at:
        raise StorageRefusal("invalid_request_transition", "deferred request requires next attention time")
    if next_attention_at:
        _time(next_attention_at, "next attention time")
    try:
        with store._transaction():
            request = _request_row(store, request_id)
            _active_claim(store, request_id, token=claim_token, at=at)
            if int(request["version"]) != expected_version:
                raise StorageRefusal("version_conflict", "request transition expected-version failed")
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE requests SET state=?,resolution_summary=?,next_attention_at=?,version=?,updated_at=?
                 WHERE request_id=? AND version=?
                """,
                (state, summary, next_attention_at, next_version, at, request_id, expected_version),
            )
            if state == "cancelled":
                store.connection.execute(
                    "UPDATE request_claims SET released_at=? WHERE request_id=?", (at, request_id)
                )
            _insert_request_event(
                store,
                event_id=event_id,
                request_id=request_id,
                actor_id=request["owner_agent_id"],
                request_version=next_version,
                event_type=f"request_{state}",
                state=state,
                update=summary,
                at=at,
            )
            if state == "cancelled":
                store.connection.execute(
                    "UPDATE obligations SET state='cancelled',updated_at=? WHERE aggregate_id=? AND state='open'",
                    (at, request_id),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "request transition conflicted with canonical state") from exc
    return {"request_id": request_id, "state": state, "version": next_version, "event_id": event_id}


def record_request_result(store: Any, command: RequestResultCommand) -> dict[str, Any]:
    request_id = command.request_id
    claim_token = command.claim_token
    expected_version = command.expected_version
    result_id = command.result_id
    idempotency_key = command.idempotency_key
    outcome = command.outcome
    summary = command.summary
    at = command.at
    return_to_requester = command.return_to_requester
    event_id = command.event_id
    outbox_id = command.outbox_id
    _time(at, "result time")
    if not all((result_id, idempotency_key, outcome, summary)):
        raise StorageRefusal("invalid_result", "request result fields are required")
    if return_to_requester:
        _bounded_public_text(summary, "returned result", maximum=1024)
    if len(command.task_ids) > MAX_TASK_RESULT_SOURCES:
        raise StorageRefusal(
            "invalid_result",
            f"request result cites more than {MAX_TASK_RESULT_SOURCES} tasks",
        )
    sources = tuple(dict.fromkeys(command.task_ids))
    try:
        with store._transaction():
            request = _request_row(store, request_id)
            existing = store.connection.execute(
                "SELECT * FROM request_results WHERE request_id=? AND idempotency_key=?",
                (request_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["summary"] != summary or existing["outcome"] != outcome:
                    raise StorageRefusal("result_conflict", "idempotent result key has different content")
                return {
                    "request_id": request_id,
                    "result_id": existing["result_id"],
                    "state": request["state"],
                    "owner_agent_id": request["owner_agent_id"],
                    "version": int(request["version"]),
                    "event_id": existing["return_event_id"],
                    "outbox_id": existing["return_outbox_id"],
                    "idempotent": True,
                }
            _active_claim(store, request_id, token=claim_token, at=at)
            if int(request["version"]) != expected_version:
                raise StorageRefusal("version_conflict", "request result expected-version failed")
            cited_tasks: dict[str, sqlite3.Row] = {}
            if sources:
                placeholders = ",".join("?" for _ in sources)
                cited_tasks = {
                    str(row["task_id"]): row
                    for row in store.connection.execute(
                        f"SELECT task_id,state,result_summary FROM tasks "
                        f"WHERE request_id=? AND task_id IN ({placeholders})",
                        (request_id, *sources),
                    )
                }
            for task_id in sources:
                task = cited_tasks.get(task_id)
                if (
                    task is None
                    or task["state"] not in {"completed", "complete", "ready_to_land"}
                    or not task["result_summary"]
                ):
                    raise StorageRefusal("task_result_missing", "every cited task requires a settled result")
            store.connection.execute(
                """
                INSERT INTO request_results
                  (result_id,request_id,produced_by_agent_id,outcome,summary,payload_hash,
                   idempotency_key,return_event_id,return_outbox_id,created_at)
                VALUES(?,?,?,?,?,NULL,?,?,?,?)
                """,
                (
                    result_id,
                    request_id,
                    request["owner_agent_id"],
                    outcome,
                    summary,
                    idempotency_key,
                    event_id if return_to_requester else None,
                    outbox_id if return_to_requester else None,
                    at,
                ),
            )
            for task_id in sources:
                store.connection.execute(
                    "INSERT INTO request_result_sources(result_id,task_id,source_kind) VALUES(?,?,'champion_task')",
                    (result_id, task_id),
                )
            next_version = expected_version + 1
            target_owner = request["owner_agent_id"]
            next_state = request["state"]
            if return_to_requester:
                target_owner = request["requester_agent_id"]
                if not event_id or not outbox_id:
                    raise StorageRefusal("return_delivery_required", "owner return requires exact event and outbox IDs")
                next_state = "awaiting_requester"
            store.connection.execute(
                """
                UPDATE requests SET latest_result_id=?,owner_agent_id=?,state=?,version=?,updated_at=?
                 WHERE request_id=? AND version=?
                """,
                (result_id, target_owner, next_state, next_version, at, request_id, expected_version),
            )
            if return_to_requester:
                store.connection.execute(
                    "UPDATE request_claims SET released_at=? WHERE request_id=?", (at, request_id)
                )
                progress_value = {
                    "settled_count": len(sources),
                    "total_count": len(sources),
                    "current_phase": "Request result ready",
                    "blocker_count": int(outcome == "failed"),
                    "blocker_severity": "high" if outcome == "failed" else "none",
                    "user_action_required": False,
                    "deadline_change": None,
                    "next_action": "Review the returned final result",
                }
                _insert_request_event(
                    store,
                    event_id=str(event_id),
                    request_id=request_id,
                    actor_id=request["owner_agent_id"],
                    request_version=next_version,
                    event_type="request_progress",
                    state="awaiting_requester",
                    update=summary,
                    at=at,
                    detail={
                        "reason_code": (
                            "request_failed" if outcome == "failed" else "request_resolved"
                        ),
                        "progress_generation": next_version,
                        **progress_value,
                        "result_id": result_id,
                    },
                )
                store.connection.execute(
                    """
                    INSERT INTO delivery_outbox
                      (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                    VALUES(?,?,?,'pending',?,0)
                    """,
                    (outbox_id, event_id, target_owner, at),
                )
                store.connection.execute(
                    """
                    INSERT INTO request_progress_events
                      (progress_id,request_id,request_generation,progress_generation,
                       owner_agent_id,recipient_agent_id,urgency,reason_code,content_digest,
                       settled_count,total_count,current_phase,blocker_count,blocker_severity,
                       user_action_required,deadline_change,next_action,event_id,outbox_id,emitted_at)
                    VALUES(?,?,?,?,?,?,'immediate',?,?,?,?,?,?,?,?,NULL,?,?,?,?)
                    """,
                    (
                        f"progress:{request_id}:{next_version}:result",
                        request_id,
                        next_version,
                        next_version,
                        request["owner_agent_id"],
                        target_owner,
                        "request_failed" if outcome == "failed" else "request_resolved",
                        _digest(_json(progress_value)),
                        len(sources),
                        len(sources),
                        progress_value["current_phase"],
                        progress_value["blocker_count"],
                        progress_value["blocker_severity"],
                        0,
                        progress_value["next_action"],
                        event_id,
                        outbox_id,
                        at,
                    ),
                )
                store.connection.execute(
                    """
                    UPDATE request_progress_buffers SET state='superseded',updated_at=?
                     WHERE request_id=? AND state IN ('pending','due')
                    """,
                    (at, request_id),
                )
                store.connection.execute(
                    """
                    INSERT INTO obligations
                      (obligation_id,owner_agent_id,kind,aggregate_id,dedupe_key,state,
                       next_attention_at,details_json,created_at,updated_at)
                    VALUES(?,?, 'request_attention',?,?, 'open',?, '{}',?,?)
                    """,
                    (
                        f"obligation:request:{request_id}:return",
                        target_owner,
                        request_id,
                        f"request-return:{request_id}:{result_id}",
                        at,
                        at,
                        at,
                    ),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "request result conflicted with canonical state") from exc
    return {
        "request_id": request_id,
        "result_id": result_id,
        "state": next_state,
        "owner_agent_id": target_owner,
        "version": next_version,
        "event_id": event_id,
        "outbox_id": outbox_id,
        "idempotent": False,
    }


def answer_request(store: Any, command: AnswerRequestCommand) -> dict[str, Any]:
    request_id = command.request_id
    claim_token = command.claim_token
    expected_version = command.expected_version
    response_ref_id = command.response_ref_id
    adapter_kind = command.adapter_kind
    session_locator = command.session_locator
    response_locator = command.response_locator
    durability = command.durability
    content_hash = command.content_hash
    resolution_summary = command.resolution_summary
    event_id = command.event_id
    at = command.at
    _time(at, "answer time")
    if durability not in {"durable", "ephemeral"} or not all(
        (response_ref_id, adapter_kind, session_locator, response_locator, content_hash, resolution_summary)
    ):
        raise StorageRefusal("invalid_answer", "answer requires a bounded response reference and summary")
    try:
        with store._transaction():
            request = _request_row(store, request_id)
            existing = store.connection.execute(
                "SELECT response_ref_id FROM response_references WHERE request_id=? AND content_hash=?",
                (request_id, content_hash),
            ).fetchone()
            if request["state"] == "answered" and existing is not None:
                return {
                    "request_id": request_id,
                    "state": "answered",
                    "version": int(request["version"]),
                    "response_ref_id": existing["response_ref_id"],
                    "idempotent": True,
                }
            claim = _active_claim(store, request_id, token=claim_token, at=at)
            if int(request["version"]) != expected_version:
                raise StorageRefusal("version_conflict", "request answer expected-version failed")
            next_version = expected_version + 1
            store.connection.execute(
                """
                INSERT INTO response_references
                  (response_ref_id,request_id,runtime_instance_id,adapter_kind,session_locator,
                   response_locator,durability,content_hash,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    response_ref_id,
                    request_id,
                    claim["runtime_instance_id"],
                    adapter_kind,
                    session_locator,
                    response_locator,
                    durability,
                    content_hash,
                    at,
                ),
            )
            store.connection.execute(
                """
                UPDATE requests SET state='answered',resolution_summary=?,next_attention_at=NULL,
                  version=?,updated_at=? WHERE request_id=? AND version=?
                """,
                (resolution_summary, next_version, at, request_id, expected_version),
            )
            store.connection.execute(
                "UPDATE request_claims SET released_at=? WHERE request_id=?", (at, request_id)
            )
            _insert_request_event(
                store,
                event_id=event_id,
                request_id=request_id,
                actor_id=request["owner_agent_id"],
                request_version=next_version,
                event_type="request_answered",
                state="answered",
                update=resolution_summary,
                at=at,
                detail={"response_ref_id": response_ref_id, "durability": durability},
            )
            store.connection.execute(
                "UPDATE obligations SET state='satisfied',updated_at=? WHERE aggregate_id=? AND state='open'",
                (at, request_id),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "request answer conflicted with canonical state") from exc
    return {
        "request_id": request_id,
        "state": "answered",
        "version": next_version,
        "response_ref_id": response_ref_id,
        "idempotent": False,
    }


def unresolved_requests(
    store: Any,
    owner_agent_id: str,
    *,
    limit: int = 100,
    before_action: Optional[str] = None,
) -> dict[str, Any]:
    if not 1 <= limit <= 500:
        raise StorageRefusal("invalid_limit", "unresolved limit must be between 1 and 500")
    if before_action is not None and before_action not in {"reply", "wait", "handoff", "end"}:
        raise StorageRefusal("invalid_reconciliation", "before-action is invalid")
    request_total = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM requests WHERE owner_agent_id=? AND state NOT IN ('answered','cancelled')",
            (owner_agent_id,),
        ).fetchone()[0]
    )
    untriaged_prompt_count = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM prompts WHERE current_owner_agent_id=? AND triage_state='untriaged'",
            (owner_agent_id,),
        ).fetchone()[0]
    )
    prompt_rows = store.connection.execute(
        """
        SELECT p.prompt_id,p.adapter_kind,p.session_ref,p.source_event_key,p.created_at,
               pp.body_hash,pp.byte_count
          FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
         WHERE p.current_owner_agent_id=? AND p.triage_state='untriaged'
         ORDER BY p.created_at,p.prompt_id
         LIMIT ?
        """,
        (owner_agent_id, limit),
    ).fetchall()
    request_limit = max(0, limit - len(prompt_rows))
    rows = store.connection.execute(
        """
        SELECT request_id,summary,state,execution_mode,owner_agent_id,return_to_agent_id,
               next_attention_at,version,updated_at
          FROM requests
         WHERE owner_agent_id=? AND state NOT IN ('answered','cancelled')
         ORDER BY COALESCE(next_attention_at,updated_at),created_at,request_id
         LIMIT ?
        """,
        (owner_agent_id, request_limit),
    ).fetchall()
    obligations = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM obligations WHERE owner_agent_id=? AND state='open'",
            (owner_agent_id,),
        ).fetchone()[0]
    )
    return {
        "owner_agent_id": owner_agent_id,
        "before_action": before_action,
        "unresolved_count": request_total + untriaged_prompt_count,
        "untriaged_prompt_count": untriaged_prompt_count,
        "open_obligation_count": obligations,
        "truncated": request_total + untriaged_prompt_count > len(rows) + len(prompt_rows),
        "safe_to_finish": request_total == 0 and untriaged_prompt_count == 0 and obligations == 0,
        "untriaged_prompts": [dict(row) for row in prompt_rows],
        "requests": [dict(row) for row in rows],
    }


def _candidate_request_inventory(
    store: Any,
    owner_agent_id: str,
    *,
    limit: int,
    max_bytes: int,
    after: str | None = None,
    prompt_texts: tuple[str, ...] = (),
    page: bool = False,
) -> dict[str, Any]:
    total = int(
        store.connection.execute(
            """
            SELECT COUNT(*) FROM requests
             WHERE owner_agent_id=? AND state NOT IN ('answered','cancelled')
            """,
            (owner_agent_id,),
        ).fetchone()[0]
    )
    pool_limit = limit + 1 if page else min(500, limit * 4) + 1
    if page:
        rows = store.connection.execute(
            """
            SELECT request_id,summary,state,version,updated_at
              FROM requests
             WHERE owner_agent_id=? AND state NOT IN ('answered','cancelled')
               AND request_id>?
             ORDER BY request_id
             LIMIT ?
            """,
            (owner_agent_id, after or "", pool_limit),
        ).fetchall()
    else:
        rows = store.connection.execute(
            """
            SELECT request_id,summary,state,version,updated_at
              FROM requests
             WHERE owner_agent_id=? AND state NOT IN ('answered','cancelled')
             ORDER BY updated_at DESC,request_id
             LIMIT ?
            """,
            (owner_agent_id, pool_limit),
        ).fetchall()
    request_ids = [str(row["request_id"]) for row in rows]
    routing_by_request: dict[str, dict[str, str]] = {}
    if request_ids:
        placeholders = ",".join("?" for _ in request_ids)
        routing_rows = store.connection.execute(
            f"""
            SELECT t.request_id,t.project_id,p.repository,t.task_id
              FROM tasks t LEFT JOIN projects p ON p.project_id=t.project_id
             WHERE t.request_id IN ({placeholders})
             ORDER BY t.request_id,t.task_id
            """,
            tuple(request_ids),
        ).fetchall()
        for row in routing_rows:
            routing_by_request.setdefault(
                str(row["request_id"]),
                {
                    key: str(row[key])
                    for key in ("project_id", "repository")
                    if row[key] is not None
                },
            )
    prompt_terms = {
        term
        for text in prompt_texts
        for term in re.findall(r"[a-z0-9]+", text.lower())
        if len(term) > 2
    }
    prompt_terms = set(
        sorted(prompt_terms, key=lambda term: (-len(term), term))[:64]
    )
    prepared: list[dict[str, Any]] = []
    for row in rows:
        summary = " ".join(str(row["summary"]).split())[:240]
        candidate: dict[str, Any] = {
            "request_id": row["request_id"],
            "summary": summary,
            "state": row["state"],
            "version": int(row["version"]),
        }
        routing_key = routing_by_request.get(str(row["request_id"]), {})
        if routing_key:
            candidate["routing_key"] = routing_key
        searchable = " ".join(
            (summary, *(str(value) for value in routing_key.values()))
        ).lower()
        terms = {term for term in re.findall(r"[a-z0-9]+", searchable) if len(term) > 2}
        candidate["_overlap"] = len(prompt_terms & terms)
        candidate["_routing_overlap"] = int(
            any(
                (routing_terms := {
                    term
                    for term in re.findall(r"[a-z0-9]+", str(value).lower())
                    if len(term) > 2
                })
                and routing_terms <= prompt_terms
                for value in routing_key.values()
            )
        )
        candidate["_updated_at"] = str(row["updated_at"])
        prepared.append(candidate)
    snapshot_rows = [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in prepared
    ]
    snapshot_digest = _digest(_json(snapshot_rows))
    if not page:
        prepared.sort(key=lambda row: str(row["request_id"]))
        prepared.sort(key=lambda row: row["_updated_at"], reverse=True)
        prepared.sort(key=lambda row: row["_overlap"], reverse=True)
        prepared.sort(key=lambda row: row["_routing_overlap"], reverse=True)
    candidates: list[dict[str, Any]] = []
    returned_bytes = 0
    more = total > limit if not page else len(prepared) > limit
    for row in prepared[:limit]:
        candidate = {key: value for key, value in row.items() if not key.startswith("_")}
        encoded = _json(candidate).encode("utf-8")
        if returned_bytes + len(encoded) > max_bytes:
            more = True
            break
        candidates.append(candidate)
        returned_bytes += len(encoded)
    digest = _digest(
        _json(
            {
                "owner_agent_id": owner_agent_id,
                "after": after,
                "ranking": "request-id-page" if page else "routing-lexical-recency",
                "truncated": more,
                "requests": candidates,
            }
        )
    )
    return {
        "owner_agent_id": owner_agent_id,
        "total_count": total,
        "returned_count": len(candidates),
        "returned_bytes": returned_bytes,
        "truncated": more,
        "after": after if page else None,
        "ranking": "request-id-page" if page else "routing-lexical-recency",
        "next_cursor": candidates[-1]["request_id"] if page and more and candidates else None,
        "digest": digest,
        "snapshot_digest": snapshot_digest,
        "requests": candidates,
    }


def untriaged_intake(
    store: Any,
    owner_agent_id: str,
    *,
    limit: int = 20,
    max_bytes: int = 1_000_000,
    candidate_limit: int = 12,
    candidate_max_bytes: int = 24_576,
    candidate_after: str | None = None,
    candidate_page: bool = False,
) -> dict[str, Any]:
    """Return exact prompts plus bounded canonical semantic-dedup candidates."""

    if (
        not 1 <= limit <= 100
        or not MAX_PROMPT_BYTES <= max_bytes <= 4_000_000
        or not 1 <= candidate_limit <= 500
        or not 1_024 <= candidate_max_bytes <= 1_000_000
        or (candidate_after is not None and not candidate_page)
    ):
        raise StorageRefusal(
            "invalid_limit", "untriaged intake bounds are outside the supported range"
        )
    owner = store.connection.execute(
        "SELECT role,retired_at FROM agent_instances WHERE agent_id=?",
        (owner_agent_id,),
    ).fetchone()
    if owner is None or owner["role"] != "shotcaller" or owner["retired_at"] is not None:
        raise StorageRefusal(
            "owner_invalid", "untriaged intake requires one live Shotcaller owner"
        )
    total = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM prompts WHERE current_owner_agent_id=? AND triage_state='untriaged'",
            (owner_agent_id,),
        ).fetchone()[0]
    )
    rows = store.connection.execute(
        """
        SELECT p.prompt_id,p.runtime_instance_id,p.current_owner_runtime_instance_id,
               p.adapter_kind,p.session_ref,p.source_event_key,p.created_at,
               pp.body,pp.body_hash,pp.byte_count,pp.pruned_at
          FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
         WHERE p.current_owner_agent_id=? AND p.triage_state='untriaged'
         ORDER BY p.created_at,p.prompt_id
         LIMIT ?
        """,
        (owner_agent_id, limit),
    ).fetchall()
    prompts: list[dict[str, Any]] = []
    returned_bytes = 0
    for row in rows:
        body = row["body"]
        if body is None or row["pruned_at"] is not None:
            raise StorageRefusal(
                "prompt_payload_unavailable",
                "an untriaged prompt no longer has its exact retained body",
            )
        encoded = str(body).encode("utf-8")
        if (
            len(encoded) != int(row["byte_count"])
            or hashlib.sha256(encoded).hexdigest() != row["body_hash"]
        ):
            raise StorageRefusal(
                "prompt_payload_mismatch", "retained prompt bytes do not match their identity"
            )
        if returned_bytes + len(encoded) > max_bytes:
            break
        prompts.append(
            {
                "prompt_id": row["prompt_id"],
                "runtime_instance_id": row["runtime_instance_id"],
                "owner_runtime_instance_id": row["current_owner_runtime_instance_id"],
                "adapter_kind": row["adapter_kind"],
                "session_ref": row["session_ref"],
                "source_event_key": row["source_event_key"],
                "created_at": row["created_at"],
                "body": body,
                "body_hash": row["body_hash"],
                "byte_count": int(row["byte_count"]),
            }
        )
        returned_bytes += len(encoded)
    candidate_inventory = _candidate_request_inventory(
        store,
        owner_agent_id,
        limit=candidate_limit,
        max_bytes=candidate_max_bytes,
        after=candidate_after,
        prompt_texts=tuple(str(prompt["body"]) for prompt in prompts),
        page=candidate_page,
    )
    return {
        "owner_agent_id": owner_agent_id,
        "untriaged_prompt_count": total,
        "returned_count": len(prompts),
        "returned_bytes": returned_bytes,
        "truncated": total > len(prompts),
        "prompts": prompts,
        "candidate_inventory": candidate_inventory,
    }


def semantic_recovery_backlog(store: Any, *, limit: int = 20) -> dict[str, Any]:
    """Return only prompts that lack a verified live owner runtime."""

    if not 1 <= limit <= 100:
        raise StorageRefusal("invalid_limit", "semantic recovery backlog limit is invalid")
    rows = store.connection.execute(
        """
        SELECT prompt_id,created_at FROM prompt_quarantine
         WHERE state='quarantined'
        UNION ALL
        SELECT p.prompt_id,p.created_at FROM prompts p
         WHERE p.triage_state='untriaged'
           AND NOT EXISTS (
             SELECT 1 FROM runtime_instances r
              WHERE r.actor_agent_id=p.current_owner_agent_id
                AND r.status IN ('active','idle') AND r.verified=1
           )
         ORDER BY created_at,prompt_id
         LIMIT ?
        """,
        (limit + 1,),
    ).fetchall()
    return {
        "returned_count": min(len(rows), limit),
        "truncated": len(rows) > limit,
        "prompt_ids": [str(row["prompt_id"]) for row in rows[:limit]],
    }
