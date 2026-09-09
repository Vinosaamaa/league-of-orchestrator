"""Bounded native reply context; never read transcripts or infer completion."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .sqlite_request_ops import _time

MAX_CONTEXT_BYTES = 4096


def _bounded(text: str) -> dict[str, Any]:
    encoded = text.encode('utf-8')
    return {'text': encoded[:MAX_CONTEXT_BYTES].decode('utf-8', errors='ignore'),
            'truncated': len(encoded) > MAX_CONTEXT_BYTES}


def record_native_reply(store: Any, owner: str, payload: dict[str, Any], at: str) -> None:
    """Capture an optional Codex Stop field only for one exact live runtime.

    A reply is contextual data, not an answer receipt or permission grant.
    Empty Stop continuations do not overwrite a useful prior reply.
    """
    session, turn, text = (payload.get(key) for key in
                           ('session_id', 'turn_id', 'last_assistant_message'))
    if (payload.get('hook_event_name') != 'Stop'
            or not all(isinstance(value, str) and value for value in (session, turn, text))):
        return
    _time(at, 'native reply context time')
    with store._transaction():
        rows = store.connection.execute(
            "SELECT r.runtime_instance_id FROM runtime_instances r "
            "JOIN agent_instances a ON a.agent_id=r.actor_agent_id "
            "JOIN prompt_triage_settings s ON s.owner_agent_id=a.agent_id AND s.mode='background' "
            "WHERE r.actor_agent_id=? AND r.session_ref=? "
            "AND r.harness_kind IN ('codex','codex-thread') "
            "AND r.status IN ('active','idle') AND r.verified=1 "
            "AND a.role='shotcaller' AND a.retired_at IS NULL",
            (owner, session),
        ).fetchall()
        if len(rows) != 1:
            return
        runtime = rows[0]['runtime_instance_id']
        detail = {'session': session, 'runtime': runtime, 'turn': turn,
                  'assistant': _bounded(text),
                  'content_hash': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        identity = json.dumps([owner, runtime, session, turn, detail['content_hash']])
        event_id = 'event:triage-context:' + hashlib.sha256(identity.encode()).hexdigest()
        store.connection.execute(
            "INSERT OR IGNORE INTO events(event_id,agent_id,entity_version,event_type,"
            "status,update_text,occurred_at,detail_json,aggregate_kind,aggregate_id) "
            "VALUES(?,?,1,'triage_reply_context','recorded','Native reply context',?,?,"
            "'agent',?)", (event_id, owner, at, json.dumps(detail), owner),
        )


def preceding_context(store: Any, owner: str, prompt: dict[str, Any]) -> dict[str, Any]:
    """Only preceding, recent context from the captured prompt's exact runtime.

    Context is bounded independently of prompt capture. Missing/pruned prior
    input or reply data stays missing; there is no transcript backfill.
    """
    session, runtime, at = prompt['session_ref'], prompt['runtime_instance_id'], prompt['created_at']
    row = store.connection.execute(
        "SELECT detail_json FROM events WHERE agent_id=? AND event_type='triage_reply_context' "
        "AND json_extract(detail_json,'$.session')=? "
        "AND json_extract(detail_json,'$.runtime')=? "
        "AND julianday(occurred_at)<=julianday(?) "
        "AND julianday(occurred_at)>=julianday(?)-1 "
        "ORDER BY event_seq DESC LIMIT 1", (owner, session, runtime, at, at),
    ).fetchone()
    if row is None:
        return {}
    reply = json.loads(row['detail_json'])
    # Native Stop supplies the reply itself. Do not pretend this is a full
    # conversation or replay a different session's user messages.
    return {'previous_assistant': reply['assistant']}
