"""SQLite prompt, request, claim, dispatch, result, and reconciliation operations."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

from .storage_request import (
    MAX_TASK_RESULT_SOURCES,
    AnswerRequestCommand,
    DispatchRequestCommand,
    RequestResultCommand,
)
from .storage_types import StorageRefusal


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
EXECUTION_MODES = {"direct", "hidden", "champion"}
PROMPT_ITEM_DISPOSITIONS = {
    "new_request",
    "follow_up",
    "context",
    "acknowledgement",
    "duplicate",
    "deferred",
}
CHAMPION_WORK_KINDS = {
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
) -> dict[str, Any]:
    _time(at, "prompt capture time")
    encoded = body.encode("utf-8")
    if not all((prompt_id, intake_actor_id, runtime_instance_id, adapter_kind, session_ref, source_event_key)):
        raise StorageRefusal("invalid_prompt", "prompt identity fields are required")
    if not encoded or len(encoded) > MAX_PROMPT_BYTES:
        raise StorageRefusal("invalid_prompt", "prompt body must be non-empty and within the bounded size")
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
                       i.state AS intake_state,s.shotcaller_agent_id
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
                   source_event_key,triage_state,triage_digest,created_at)
                VALUES(?,?,?,?,?,?,'untriaged',NULL,?)
                """,
                (
                    prompt_id,
                    intake_actor_id,
                    runtime_instance_id,
                    adapter_kind,
                    session_ref,
                    source_event_key,
                    at,
                ),
            )
            store.connection.execute(
                "INSERT INTO prompt_payloads(prompt_id,body,body_hash,byte_count,pruned_at) VALUES(?,?,?,?,NULL)",
                (prompt_id, body, body_hash, len(encoded)),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "prompt intake conflicted with canonical state") from exc
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
    intake_actor_id: str,
    item: dict[str, Any],
    at: str,
) -> None:
    disposition = item["disposition"]
    request_id = item["request_id"]
    if disposition in {"new_request", "deferred"}:
        request_state = "deferred" if disposition == "deferred" else "open"
        next_attention_at = item["next_attention_at"] if disposition == "deferred" else None
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
                intake_actor_id,
                intake_actor_id,
                request_state,
                next_attention_at,
                at,
                at,
            ),
        )
    elif disposition in {"follow_up", "duplicate"}:
        _request_row(store, str(request_id))
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
            "deferred": "origin",
            "follow_up": "follow_up",
            "duplicate": "duplicate",
        }[disposition]
        store.connection.execute(
            "INSERT INTO request_sources(request_id,prompt_item_id,source_role) VALUES(?,?,?)",
            (request_id, item["prompt_item_id"], source_role),
        )


def triage_prompt(
    store: Any,
    prompt_id: str,
    items: list[dict[str, Any]],
    at: str,
) -> dict[str, Any]:
    _time(at, "triage time")
    normalized = _normalize_triage_items(items)
    triage_digest = _digest(_json(normalized))
    counts = _triage_counts()
    try:
        with store._transaction():
            prompt = store.connection.execute(
                "SELECT intake_actor_id,triage_state,triage_digest FROM prompts WHERE prompt_id=?",
                (prompt_id,),
            ).fetchone()
            if prompt is None:
                raise StorageRefusal("prompt_unknown", "prompt does not exist")
            if prompt["triage_state"] == "complete":
                if prompt["triage_digest"] != triage_digest:
                    raise StorageRefusal("triage_conflict", "prompt was already triaged with different items")
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
                        if item["disposition"] in {"new_request", "deferred"}
                    ),
                    "dispositions": counts,
                    "idempotent": True,
                }
            for item in normalized:
                counts[item["disposition"]] += 1
                _persist_triage_item(
                    store,
                    prompt_id=prompt_id,
                    intake_actor_id=prompt["intake_actor_id"],
                    item=item,
                    at=at,
                )
            store.connection.execute(
                "UPDATE prompts SET triage_state='complete',triage_digest=? WHERE prompt_id=?",
                (triage_digest, prompt_id),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "prompt triage conflicted with canonical state") from exc
    return {
        "prompt_id": prompt_id,
        "triage_state": "complete",
        "item_count": len(normalized),
        "request_count": counts["new_request"] + counts["deferred"],
        "dispositions": counts,
        "idempotent": False,
    }


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
            if runtime["actor_agent_id"] != request["owner_agent_id"]:
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
                    actor_id=request["owner_agent_id"],
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
                    actor_id=request["owner_agent_id"],
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
                    (route_event, request["owner_agent_id"]),
                ).fetchone()
                if receipt is None:
                    raise StorageRefusal("route_unreceived", "routed request cannot be accepted before exact receipt")
                next_version = int(request["version"]) + 1
                store.connection.execute(
                    "UPDATE requests SET state='accepted',version=?,updated_at=? WHERE request_id=?",
                    (next_version, at, request_id),
                )
                _insert_request_event(
                    store,
                    event_id=f"request:{request_id}:{next_version}:accepted",
                    request_id=request_id,
                    actor_id=request["owner_agent_id"],
                    request_version=next_version,
                    event_type="request_accepted",
                    state="accepted",
                    update="routed request accepted",
                    at=at,
                    source_event_id=route_event,
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
    *, work_kind: str, requested_mode: Optional[str], hidden_supported: bool
) -> tuple[str, str]:
    if requested_mode is not None and requested_mode not in EXECUTION_MODES:
        raise StorageRefusal("invalid_dispatch", "requested execution mode is invalid")
    if work_kind in CHAMPION_WORK_KINDS:
        if requested_mode in {"direct", "hidden"}:
            raise StorageRefusal(
                "champion_required",
                "repository writes, migration, supervised tests, and long work require a visible Champion",
            )
        return "champion", f"{work_kind} requires visible Champion ownership"
    if work_kind not in DIRECT_WORK_KINDS:
        raise StorageRefusal("invalid_dispatch", "work kind is not part of the bounded classifier contract")
    if requested_mode == "champion":
        return "champion", "explicit Champion routing was preserved"
    if requested_mode == "hidden":
        if not hidden_supported or work_kind != "read-only":
            raise StorageRefusal("hidden_unavailable", "hidden execution is unavailable for this work kind")
        return "hidden", "bounded read-only analysis uses an available hidden worker"
    return "direct", "bounded non-writing work is safe for direct execution"


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
    mode, reason = classify_dispatch(
        work_kind=work_kind,
        requested_mode=requested_mode,
        hidden_supported=hidden_supported,
    )
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
            next_version = int(request["version"]) + 1
            input_value = {
                "work_kind": work_kind,
                "requested_mode": requested_mode,
                "hidden_supported": hidden_supported,
            }
            store.connection.execute(
                """
                INSERT INTO request_dispatches
                  (dispatch_id,request_id,request_version,work_kind,execution_mode,reason,
                   requested_mode,requested_model,requested_effort,explicit_route,input_json,decided_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    dispatch_id,
                    request_id,
                    next_version,
                    work_kind,
                    mode,
                    reason,
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
            _insert_request_event(
                store,
                event_id=f"request:{request_id}:{next_version}:dispatched",
                request_id=request_id,
                actor_id=request["owner_agent_id"],
                request_version=next_version,
                event_type="request_dispatched",
                state="in_progress",
                update=reason,
                at=at,
                detail={
                    "execution_mode": mode,
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
) -> dict[str, Any]:
    _time(at, "route time")
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
                        "state": request["state"],
                        "version": int(request["version"]),
                        "event_id": event_id,
                        "outbox_id": existing["outbox_id"],
                        "idempotent": True,
                    }
                raise StorageRefusal("version_conflict", "request route expected-version failed")
            _active_claim(store, request_id, token=claim_token, at=at)
            recipient = store.connection.execute(
                "SELECT 1 FROM agent_instances WHERE agent_id=? AND role='shotcaller' AND retired_at IS NULL",
                (recipient_agent_id,),
            ).fetchone()
            if recipient is None:
                raise StorageRefusal("owner_unknown", "route recipient is not an active Shotcaller")
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
                update="request routed to another Shotcaller",
                at=at,
                detail={"recipient_agent_id": recipient_agent_id, "return_to_agent_id": return_to},
            )
            store.connection.execute(
                """
                UPDATE requests SET owner_agent_id=?,return_to_agent_id=?,state='routed',
                  last_route_event_id=?,version=?,updated_at=?
                 WHERE request_id=? AND version=?
                """,
                (recipient_agent_id, return_to, event_id, next_version, at, request_id, expected_version),
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
        "owner_agent_id": recipient_agent_id,
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
                target_owner = request["return_to_agent_id"] or request["requester_agent_id"]
                if target_owner == request["owner_agent_id"]:
                    raise StorageRefusal("return_owner_conflict", "request is already owned by its requester")
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
                _insert_request_event(
                    store,
                    event_id=str(event_id),
                    request_id=request_id,
                    actor_id=request["owner_agent_id"],
                    request_version=next_version,
                    event_type="owner_result_recorded",
                    state="awaiting_requester",
                    update=summary,
                    at=at,
                    detail={"result_id": result_id, "return_to_agent_id": target_owner},
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
    total = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM requests WHERE owner_agent_id=? AND state NOT IN ('answered','cancelled')",
            (owner_agent_id,),
        ).fetchone()[0]
    )
    rows = store.connection.execute(
        """
        SELECT request_id,summary,state,execution_mode,owner_agent_id,return_to_agent_id,
               next_attention_at,version,updated_at
          FROM requests
         WHERE owner_agent_id=? AND state NOT IN ('answered','cancelled')
         ORDER BY COALESCE(next_attention_at,updated_at),created_at,request_id
         LIMIT ?
        """,
        (owner_agent_id, limit),
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
        "unresolved_count": total,
        "open_obligation_count": obligations,
        "truncated": total > len(rows),
        "safe_to_finish": total == 0 and obligations == 0,
        "requests": [dict(row) for row in rows],
    }
