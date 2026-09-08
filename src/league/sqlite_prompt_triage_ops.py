"""Canonical prompt-triage policy, separate from execution and supervision."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .storage import StorageRefusal
from .sqlite_request_ops import _time


def next_input(store: Any) -> dict[str, Any] | None:
    """Oldest enabled prompt; one canonical watcher owns the classifier loop."""
    from .sqlite_request_ops import untriaged_intake
    row = store.connection.execute(
        "SELECT p.prompt_id,p.current_owner_agent_id,s.version FROM prompts p "
        "JOIN prompt_triage_settings s ON s.owner_agent_id=p.current_owner_agent_id "
        "WHERE s.mode='background' AND p.triage_mode='background' "
        "AND p.triage_state='untriaged' AND NOT EXISTS ("
        "SELECT 1 FROM events e WHERE e.aggregate_kind='prompt' "
        "AND e.aggregate_id=p.prompt_id AND e.entity_version=s.version "
        "AND e.event_type='prompt_triage_failed') "
        "ORDER BY p.created_at,p.prompt_id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    owner_id = str(row["current_owner_agent_id"])
    intake = untriaged_intake(store, owner_id, background=True, limit=1,
                             background_prompt_id=row["prompt_id"])
    if not intake["prompts"]:
        return None
    return {**intake, "policy_version": int(row["version"])}


def compact_input(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt": frame["prompts"][0]["body"],
        "existing": [
            {"r": index, "s": row["summary"], "state": row["state"]}
            for index, row in enumerate(frame["candidate_inventory"]["requests"], 1)
        ],
    }


def commit(store: Any, frame: dict[str, Any], output: dict[str, Any], at: str,
           *, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    """Commit semantic splits plus exact internal delivery, never task execution."""
    from .cli import _mechanize_turn_decisions
    from .sqlite_request_ops import triage_prompt_batch
    owner_id = frame["owner_agent_id"]
    prompt = frame["prompts"][0]
    if not isinstance(output, dict) or set(output) != {"items"} or not isinstance(output["items"], list):
        raise StorageRefusal("invalid_triage", "classifier must return only semantic items")
    candidates = frame["candidate_inventory"]["requests"]
    semantic = []
    for item in output["items"]:
        if not isinstance(item, dict) or set(item) != {"k", "s", "r", "d"}:
            raise StorageRefusal("invalid_triage", "classifier item fields are invalid")
        kind = item["k"]
        translated = {"disposition": kind, "summary": item["s"]}
        if kind in {"follow_up", "duplicate", "deferred"}:
            ref = item["r"]
            if type(ref) is not int or not 1 <= ref <= len(candidates):
                raise StorageRefusal("candidate_request_unknown", "classifier reference is not in its input")
            candidate = candidates[ref - 1]
            translated.update(related_request_id=candidate["request_id"], related_request_version=candidate["version"])
        elif item["r"] is not None:
            raise StorageRefusal("invalid_triage", "non-linked items cannot name existing requests")
        if kind == "deferred":
            translated["defer_seconds"] = item["d"]
        elif item["d"] is not None:
            raise StorageRefusal("invalid_triage", "only deferred items have a delay")
        semantic.append(translated)
    mechanized = _mechanize_turn_decisions(frame, [{"items": semantic}], at)
    digest = hashlib.sha256(json.dumps(mechanized.decisions, sort_keys=True).encode()).hexdigest()
    event_id = "event:prompt-triage:" + digest
    outbox_id = "outbox:prompt-triage:" + digest
    with store._transaction():
        current = policy(store, owner_id)
        if current["mode"] != "background" or current["version"] != frame["policy_version"]:
            raise StorageRefusal("triage_policy_changed", "classification was cancelled by a policy change")
        batch = triage_prompt_batch(store, owner_id, (prompt["prompt_id"],), mechanized.decisions, at, background=True)
        existing = store.connection.execute("SELECT event_id FROM events WHERE event_id=?", (event_id,)).fetchone()
        if existing is not None:
            return {"event_id": event_id, "idempotent": True, "batch": batch}
        checklist = []
        for item in mechanized.decisions[0]["items"]:
            if item["request_id"] is None:
                continue
            request = store.connection.execute("SELECT version,state FROM requests WHERE request_id=?", (item["request_id"],)).fetchone()
            checklist.append({"id": item["request_id"], "s": item["summary"], "k": item["disposition"],
                              "v": int(request["version"]), "state": request["state"]})
        content = json.dumps({"prompt_id": prompt["prompt_id"], "requests": checklist}, separators=(",", ":"), ensure_ascii=False)
        store.connection.execute(
            "INSERT INTO events(event_id,agent_id,entity_version,event_type,status,update_text,occurred_at,detail_json,aggregate_kind,aggregate_id) "
            "VALUES(?,?,?,'prompt_triaged','ready',?,?,?,'prompt',?)",
            (event_id, owner_id, frame["policy_version"], content, at,
             json.dumps(metrics or {}, separators=(',', ':')), prompt["prompt_id"]),
        )
        # Acknowledgements/context need accounting, not an extra model wake.
        if checklist:
            store.connection.execute(
                "INSERT INTO delivery_outbox(outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count) "
                "VALUES(?,?,?,'pending',?,0)", (outbox_id, event_id, owner_id, at),
            )
    return {"event_id": event_id, "outbox_id": outbox_id if checklist else None,
            "recipient_agent_id": owner_id, "batch": batch, "idempotent": False}


def record_failure(store: Any, frame: dict[str, Any], code: str, at: str) -> None:
    """One durable failure per prompt/policy; do not burn tokens on retry loops."""
    prompt_id = frame["prompts"][0]["prompt_id"]
    version = frame["policy_version"]
    digest = hashlib.sha256(f"{prompt_id}:{version}".encode()).hexdigest()
    with store._transaction():
        store.connection.execute(
            "INSERT OR IGNORE INTO events(event_id,agent_id,entity_version,event_type,status,update_text,occurred_at,detail_json,aggregate_kind,aggregate_id) "
            "VALUES(?,?,?,'prompt_triage_failed','blocked',?,?,'{}','prompt',?)",
            ("event:triage-failed:" + digest, frame["owner_agent_id"], version, code, at, prompt_id),
        )


def policy(store: Any, owner_agent_id: str) -> dict[str, Any]:
    owner = store.connection.execute(
        "SELECT role,retired_at FROM agent_instances WHERE agent_id=?",
        (owner_agent_id,),
    ).fetchone()
    if owner is None or owner["role"] != "shotcaller" or owner["retired_at"] is not None:
        raise StorageRefusal("owner_invalid", "prompt triage requires an active Shotcaller")
    row = store.connection.execute(
        "SELECT mode,version FROM prompt_triage_settings WHERE owner_agent_id=?",
        (owner_agent_id,),
    ).fetchone()
    return {
        "owner_agent_id": owner_agent_id,
        "mode": "inline" if row is None else row["mode"],
        "version": 0 if row is None else int(row["version"]),
    }


def status(store: Any, owner_agent_id: str) -> dict[str, Any]:
    current = policy(store, owner_agent_id)
    pending = store.connection.execute(
        "SELECT COUNT(*) FROM prompts WHERE current_owner_agent_id=? AND triage_state='untriaged' "
        "AND triage_mode='background'", (owner_agent_id,),
    ).fetchone()[0]
    failure = store.connection.execute(
        "SELECT e.aggregate_id prompt_id,e.update_text code,e.occurred_at FROM events e "
        "JOIN prompts p ON p.prompt_id=e.aggregate_id WHERE e.agent_id=? "
        "AND e.event_type='prompt_triage_failed' AND e.entity_version=? "
        "AND p.triage_state='untriaged' ORDER BY e.event_seq DESC LIMIT 1",
        (owner_agent_id, current['version']),
    ).fetchone()
    return {**current, 'pending_classification': int(pending),
            'paused': current['mode'] == 'off',
            'latest_failure': None if failure is None else dict(failure)}


def configure(
    store: Any, owner_agent_id: str, enabled: bool, expected_version: int, at: str
) -> dict[str, Any]:
    """Toggle only prompt classification. Existing work and outboxes are untouched."""
    _time(at, "prompt triage configuration time")
    if type(enabled) is not bool or type(expected_version) is not int:
        raise StorageRefusal("invalid_triage_policy", "enablement and version must be explicit")
    mode = "background" if enabled else "off"
    with store._transaction():
        current = policy(store, owner_agent_id)
        if current["version"] != expected_version:
            raise StorageRefusal("version_conflict", "prompt triage policy changed")
        if current["mode"] == mode:
            return {**current, "idempotent": True}
        version = expected_version + 1
        store.connection.execute(
            "INSERT INTO prompt_triage_settings(owner_agent_id,mode,version,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(owner_agent_id) DO UPDATE SET "
            "mode=excluded.mode,version=excluded.version,updated_at=excluded.updated_at",
            (owner_agent_id, mode, version, at),
        )
        # Captured pending work is preserved. Turning off pauses this queue;
        # prompts arriving while off are explicitly skipped at capture instead.
        store.connection.execute(
            "UPDATE prompts SET triage_mode='background' "
            "WHERE current_owner_agent_id=? AND triage_state='untriaged' "
            "AND triage_mode='inline'",
            (owner_agent_id,),
        )
    return {"owner_agent_id": owner_agent_id, "mode": mode, "version": version,
            "idempotent": False}
