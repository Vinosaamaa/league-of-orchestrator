"""Fail-closed compatibility for hooks after SQLite becomes canonical."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .sqlite_store import SQLiteStorage
from .sqlite_watcher_ops import _obligation_counts
from .storage import RuntimeRegistrationCommand, StorageRefusal


def _emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _payload() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-watcher")
    parser.add_argument("--shotcaller")
    parser.add_argument("--session-id")
    parser.add_argument("--records-root")
    parser.add_argument("--state-dir")
    parser.add_argument("--record-format")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("codex-stop-hook")
    commands.add_parser("codex-user-prompt-hook")
    commands.add_parser("cursor-stop-hook")
    commands.add_parser("cursor-before-submit-hook")
    commands.add_parser("status")
    supervise = commands.add_parser("supervise")
    supervise.add_argument("--poll-seconds", type=float, default=1.0)
    deliver = commands.add_parser("deliver")
    deliver.add_argument("--event-id", required=True)
    for name in (
        "enable", "disable", "allow-stop", "wait",
        "transition", "reconcile", "preflight", "launch", "resume", "teardown",
        "install-codex-hooks", "hidden-worker", "lead-relay", "route-model",
        "resource-inspect", "codex-stop-hook", "codex-user-prompt-hook",
    ):
        if name not in commands.choices:
            commands.add_parser(name, add_help=False)
    return parser


def _state_root() -> Path:
    configured = os.environ.get("LEAGUE_STATE_ROOT")
    return Path(configured) if configured else Path.home() / ".local/state/league"


def _actor(store: SQLiteStorage, args: argparse.Namespace, payload: dict[str, Any]) -> Any:
    session = args.session_id or payload.get("session_id") or payload.get("conversation_id")
    if session:
        row = store.connection.execute(
            "SELECT agent_id,callsign FROM agent_instances WHERE retired_at IS NULL AND (thread_id=? OR agent_id=?)",
            (session, session),
        ).fetchone()
        if row is not None:
            return row
    if args.shotcaller:
        return store.connection.execute(
            "SELECT agent_id,callsign FROM agent_instances WHERE retired_at IS NULL AND role='shotcaller' AND callsign=?",
            (args.shotcaller,),
        ).fetchone()
    return None


def _scope(store: SQLiteStorage, actor_id: str, callsign: str) -> str:
    row = store.connection.execute(
        "SELECT scope_id FROM watcher_scopes WHERE actor_agent_id=? ORDER BY scope_id LIMIT 1",
        (actor_id,),
    ).fetchone()
    return str(row[0]) if row is not None else f"watcher:{callsign}"


def _capture_prompt(
    store: SQLiteStorage,
    scope: str | None,
    actor_id: str | None,
    payload: dict[str, Any],
    *,
    adapter_kind: str,
) -> dict[str, Any]:
    if adapter_kind == "codex":
        event_name = "UserPromptSubmit"
        session_ref = payload.get("session_id")
        source_event_key = payload.get("turn_id")
    else:
        event_name = "beforeSubmitPrompt"
        session_ref = payload.get("conversation_id")
        source_event_key = payload.get("generation_id")
    body = payload.get("prompt")
    if (
        payload.get("hook_event_name") != event_name
        or not isinstance(session_ref, str)
        or not session_ref
        or not isinstance(source_event_key, str)
        or not source_event_key
        or not isinstance(body, str)
        or not body
    ):
        raise StorageRefusal(
            "prompt_hook_invalid",
            "prompt hook requires its exact event, session, turn/generation, and body",
        )
    identity = f"{adapter_kind}\0{session_ref}\0{source_event_key}"
    prompt_id = f"prompt:hook:{hashlib.sha256(identity.encode()).hexdigest()}"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    if actor_id is None:
        return store.quarantine_prompt(
            prompt_id, adapter_kind, session_ref, source_event_key, body, now
        )
    runtimes = store.connection.execute(
        """
        SELECT runtime_instance_id
          FROM runtime_instances
         WHERE actor_agent_id=? AND status IN ('active','idle') AND verified=1
           AND session_ref=?
         ORDER BY runtime_instance_id
        """,
        (actor_id, session_ref),
    ).fetchall()
    if not runtimes:
        actor = store.connection.execute(
            """
            SELECT role,thread_id,backend,address
              FROM agent_instances
             WHERE agent_id=? AND retired_at IS NULL
            """,
            (actor_id,),
        ).fetchone()
        if (
            actor is None
            or actor["role"] != "shotcaller"
            or actor["thread_id"] != session_ref
            or actor["backend"] not in {"herdr", "tmux"}
            or not actor["address"]
        ):
            return store.quarantine_prompt(
                prompt_id, adapter_kind, session_ref, source_event_key, body, now
            )
        runtime_digest = hashlib.sha256(
            f"{adapter_kind}\0{session_ref}\0{actor['backend']}\0{actor['address']}".encode()
        ).hexdigest()
        runtime_id = f"runtime:hook:{runtime_digest}"
        store.register_runtime(
            RuntimeRegistrationCommand(
                runtime_instance_id=runtime_id,
                actor_agent_id=actor_id,
                harness_kind=f"{adapter_kind}-thread",
                backend_kind=str(actor["backend"]),
                session_ref=session_ref,
                endpoint=str(actor["address"]),
                runtime_generation=f"hook:{runtime_digest}",
                status="active",
                verified=True,
                at=datetime.now().astimezone().isoformat(timespec="seconds"),
                capabilities=("prompt.capture", "stop", "supervise"),
            )
        )
        runtimes = [{"runtime_instance_id": runtime_id}]
    if len(runtimes) != 1:
        return store.quarantine_prompt(
            prompt_id, adapter_kind, session_ref, source_event_key, body, now
        )
    runtime_id = str(runtimes[0]["runtime_instance_id"])
    quarantined = store.connection.execute(
        "SELECT state FROM prompt_quarantine WHERE prompt_id=?", (prompt_id,)
    ).fetchone()
    if quarantined is not None:
        return store.bind_quarantined_prompt(
            prompt_id, actor_id, runtime_id, now, wake_scope_id=scope
        )
    try:
        return store.intake_prompt(
            prompt_id,
            actor_id,
            runtime_id,
            adapter_kind,
            session_ref,
            source_event_key,
            body,
            now,
            wake_scope_id=scope,
        )
    except StorageRefusal as exc:
        if exc.code != "runtime_unverified":
            raise
        return store.quarantine_prompt(
            prompt_id, adapter_kind, session_ref, source_event_key, body, now
        )


def _supervision_snapshot(
    store: SQLiteStorage, scope: str, actor_id: str
) -> dict[str, Any]:
    generation = store.connection.execute(
        "SELECT user_message_generation FROM watcher_scopes WHERE scope_id=?",
        (scope,),
    ).fetchone()
    champions = store.connection.execute(
        """
        SELECT a.agent_id,a.callsign,a.status,a.version,a.updated_at,a.update_text
          FROM agent_instances a LEFT JOIN tasks t ON t.task_id=a.task_id
         WHERE a.role='champion' AND a.shotcaller_agent_id=? AND a.retired_at IS NULL
           AND a.status IN ('active','started','working','progress','blocked','ready_to_land')
           AND (
             a.task_id IS NULL
             OR t.state IN ('active','pending','accepted','in_progress','blocked','ready_to_land')
           )
         ORDER BY a.agent_id
        """,
        (actor_id,),
    ).fetchall()
    return {
        "user_message_generation": 0 if generation is None else int(generation[0]),
        "obligations": _obligation_counts(store, actor_id),
        "champions": [dict(row) for row in champions],
    }


def _supervise(
    store: SQLiteStorage,
    scope: str,
    actor_id: str,
    callsign: str,
    poll_seconds: float,
) -> dict[str, Any]:
    if poll_seconds <= 0:
        raise StorageRefusal("invalid_supervision", "poll interval must be positive")
    lock_path = _state_root() / f".{hashlib.sha256(scope.encode()).hexdigest()}.supervise.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    acquired = False
    watcher_id: str | None = None
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise StorageRefusal(
                "supervision_active", "one SQLite supervisor is already active for this scope"
            ) from exc
        initial = _supervision_snapshot(store, scope, actor_id)
        if sum(initial["obligations"].values()) == 0:
            return {
                "event": "champions-idle",
                "active": 0,
                "shotcaller": callsign,
                "writer": "sqlite",
            }
        marker = hashlib.sha256(
            json.dumps(initial, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        runtimes = store.connection.execute(
            """
            SELECT r.runtime_instance_id
              FROM runtime_instances r
              JOIN agent_instances a ON a.agent_id=r.actor_agent_id
             WHERE r.actor_agent_id=? AND r.status IN ('active','idle') AND r.verified=1
               AND r.session_ref=a.thread_id AND a.retired_at IS NULL
             ORDER BY r.runtime_instance_id
            """,
            (actor_id,),
        ).fetchall()
        if len(runtimes) != 1:
            raise StorageRefusal(
                "runtime_unverified",
                "SQLite supervision requires one exact verified Shotcaller runtime",
            )
        current = store.connection.execute(
            "SELECT fence FROM watcher_registrations WHERE actor_agent_id=?",
            (actor_id,),
        ).fetchone()
        fence = 1 if current is None else int(current["fence"]) + 1
        watcher_id = f"watcher:sqlite:{actor_id}:{os.getpid()}"
        now = datetime.now().astimezone()
        store.register_watcher(
            scope,
            watcher_id,
            actor_id,
            str(runtimes[0]["runtime_instance_id"]),
            f"sqlite-supervise:{marker}",
            (now + timedelta(minutes=10)).isoformat(timespec="seconds"),
            fence,
            now.isoformat(timespec="seconds"),
            block_on_obligations=True,
        )
        store.rearm_wait(
            scope,
            actor_id,
            f"sqlite-supervision:{marker}",
            datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        baseline = _supervision_snapshot(store, scope, actor_id)
        while True:
            time.sleep(max(poll_seconds, 0.01))
            current = _supervision_snapshot(store, scope, actor_id)
            if current["user_message_generation"] != baseline["user_message_generation"]:
                return {
                    "event": "user-message",
                    "priority": "user",
                    "shotcaller": callsign,
                    "writer": "sqlite",
                }
            if current["champions"] != baseline["champions"]:
                previous = {row["agent_id"]: row for row in baseline["champions"]}
                changed = next(
                    (
                        row
                        for row in current["champions"]
                        if previous.get(row["agent_id"]) != row
                    ),
                    None,
                )
                if changed is None:
                    return {
                        "event": "champions-idle",
                        "active": len(current["champions"]),
                        "shotcaller": callsign,
                        "writer": "sqlite",
                    }
                return {
                    "event": "champion-update",
                    "callsign": changed["callsign"],
                    "status": changed["status"],
                    "at": changed["updated_at"],
                    "update": changed["update_text"],
                    "shotcaller": callsign,
                    "writer": "sqlite",
                }
            if current["obligations"] != baseline["obligations"]:
                return {
                    "event": "obligations-changed",
                    "before": baseline["obligations"],
                    "after": current["obligations"],
                    "shotcaller": callsign,
                    "writer": "sqlite",
                }
    finally:
        try:
            if acquired:
                with store._transaction():
                    store.connection.execute(
                        "UPDATE watcher_scopes SET wait_active=0 WHERE scope_id=? AND actor_agent_id=?",
                        (scope, actor_id),
                    )
                    if watcher_id is not None:
                        store.connection.execute(
                            "DELETE FROM watcher_registrations WHERE watcher_id=? AND actor_agent_id=?",
                            (watcher_id, actor_id),
                        )
        finally:
            if acquired:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def main(argv: list[str] | None = None) -> int:
    args, _ = _parser().parse_known_args(argv)
    if args.command not in {
        "codex-stop-hook",
        "codex-user-prompt-hook",
        "cursor-stop-hook",
        "cursor-before-submit-hook",
        "deliver",
        "supervise",
        "status",
    }:
        raise StorageRefusal(
            "legacy_writer_fenced",
            "SQLite is canonical; this legacy writer command is fenced",
        )
    payload = _payload() if args.command.endswith("-hook") else {}
    with SQLiteStorage(_state_root(), request_wal=False) as store:
        actor = _actor(store, args, payload)
        actor_id = None if actor is None else str(actor[0])
        callsign = None if actor is None else str(actor[1])
        scope = None if actor is None else _scope(store, actor_id, callsign)
        if args.command in {"codex-user-prompt-hook", "cursor-before-submit-hook"}:
            _capture_prompt(
                store,
                scope,
                actor_id,
                payload,
                adapter_kind=(
                    "codex" if args.command == "codex-user-prompt-hook" else "cursor"
                ),
            )
            _emit({})
            return 0
        if actor is None:
            _emit({})
            return 0
        assert actor_id is not None and callsign is not None and scope is not None
        if args.command == "supervise":
            _emit(_supervise(store, scope, actor_id, callsign, args.poll_seconds))
            return 0
        if args.command == "deliver":
            from .canonical_delivery import dispatch_event

            row = store.connection.execute(
                """
                SELECT outbox_id,event_id,recipient_agent_id,available_at
                  FROM delivery_outbox
                 WHERE event_id=? AND recipient_agent_id=?
                """,
                (args.event_id, actor_id),
            ).fetchone()
            if row is None:
                raise StorageRefusal(
                    "delivery_unknown", "the exact Shotcaller event outbox does not exist"
                )
            _emit(
                dispatch_event(
                    store,
                    outbox_id=str(row["outbox_id"]),
                    event_id=str(row["event_id"]),
                    recipient_agent_id=str(row["recipient_agent_id"]),
                    at=datetime.now().astimezone().isoformat(timespec="seconds"),
                )
            )
            return 0
        if args.command in {"codex-stop-hook", "cursor-stop-hook"}:
            terminal = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            result = store.stop_decision(
                scope,
                actor_id,
                terminal,
                datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            blocked = result["decision"] == "block"
            if args.command == "cursor-stop-hook":
                _emit({"followup_message": "League has unresolved obligations."} if blocked else {})
            else:
                _emit(
                    {"decision": "block", "reason": "League has unresolved obligations."}
                    if blocked
                    else {}
                )
            return 0
        _emit({"writer": "sqlite", "shotcaller": callsign})
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StorageRefusal as exc:
        print(f"ERROR: {exc.code}: {exc}", file=sys.stderr)
        raise SystemExit(2)
