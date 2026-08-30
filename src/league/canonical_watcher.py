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

from .sqlite_store import DEFAULT_BUSY_TIMEOUT_MS, SQLiteStorage
from .sqlite_watcher_ops import _obligation_counts, stop_feedback_reason
from .storage import RuntimeRegistrationCommand, StorageRefusal
from .persistent_supervisor import (
    PersistentSupervisor,
    notify_user_message,
    pause_supervisor,
    resume_supervisor,
    send_supervisor_message,
    SupervisorUnavailable,
    stop_supervisor,
    supervisor_status,
)


STOP_BUSY_TIMEOUT_MS = 250
PROMPT_BUSY_TIMEOUT_MS = 1000


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


BROKERED_HOOK_COMMANDS = frozenset(
    {"codex-stop-hook", "codex-user-prompt-hook", "cursor-before-submit-hook"}
)


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
    service_run = commands.add_parser("service-run")
    service_run.add_argument("--lease-seconds", type=float, default=60)
    service_run.add_argument("--renew-seconds", type=float, default=20)
    service_resume = commands.add_parser("service-resume")
    service_resume.add_argument("--lease-seconds", type=float, default=60)
    service_resume.add_argument("--renew-seconds", type=float, default=20)
    commands.add_parser("service-status")
    commands.add_parser("service-stop")
    commands.add_parser("service-pause")
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
        runtime_rows = store.connection.execute(
            """
            SELECT DISTINCT a.agent_id,a.callsign,a.role
              FROM runtime_instances r
              JOIN agent_instances a ON a.agent_id=r.actor_agent_id
             WHERE r.session_ref=? AND r.status IN ('active','idle') AND r.verified=1
               AND a.retired_at IS NULL
             ORDER BY a.agent_id
            """,
            (session,),
        ).fetchall()
        if len(runtime_rows) > 1:
            raise StorageRefusal(
                "runtime_identity_ambiguous",
                "hook session matches more than one live verified runtime",
            )
        if runtime_rows:
            return runtime_rows[0]
        legacy_rows = store.connection.execute(
            """
            SELECT agent_id,callsign,role
              FROM agent_instances
             WHERE retired_at IS NULL AND (thread_id=? OR agent_id=?)
             ORDER BY agent_id
            """,
            (session, session),
        ).fetchall()
        if len(legacy_rows) > 1:
            raise StorageRefusal(
                "runtime_identity_ambiguous",
                "hook session matches more than one active agent identity",
            )
        if legacy_rows:
            return legacy_rows[0]
    if args.shotcaller:
        return store.connection.execute(
            "SELECT agent_id,callsign,role FROM agent_instances WHERE retired_at IS NULL AND role='shotcaller' AND callsign=?",
            (args.shotcaller,),
        ).fetchone()
    return None


def _scope(store: SQLiteStorage, actor_id: str, callsign: str) -> str:
    row = store.connection.execute(
        "SELECT scope_id FROM watcher_scopes WHERE actor_agent_id=? ORDER BY scope_id LIMIT 1",
        (actor_id,),
    ).fetchone()
    return str(row[0]) if row is not None else f"watcher:{callsign}"


def _codex_stop_generation(
    args: argparse.Namespace, payload: dict[str, Any]
) -> tuple[str, str | None]:
    """Bind the one-shot Stop guard to Codex's stable turn identity."""
    if args.shotcaller and not payload:
        explicit = f"explicit\0{args.shotcaller}"
        return hashlib.sha256(explicit.encode()).hexdigest(), None
    session_ref = payload.get("session_id")
    turn_id = payload.get("turn_id")
    if (
        payload.get("hook_event_name") != "Stop"
        or not isinstance(session_ref, str)
        or not session_ref
        or not isinstance(turn_id, str)
        or not turn_id
        or not isinstance(payload.get("stop_hook_active"), bool)
    ):
        raise StorageRefusal(
            "stop_hook_invalid",
            "Codex Stop hook requires its exact event, session, turn, and active flag",
        )
    return _codex_turn_generation(session_ref, turn_id), turn_id


def _codex_turn_generation(session_ref: str, turn_id: str) -> str:
    identity = f"codex\0{session_ref}\0{turn_id}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _busy_stop_result(
    args: argparse.Namespace, payload: dict[str, Any]
) -> dict[str, str]:
    if args.command == "cursor-stop-hook":
        return {
            "followup_message": (
                "League canonical state is busy; unresolved obligations remain "
                "authoritative and Stop is safely retryable."
            )
        }
    _codex_stop_generation(args, payload)
    return {
        "decision": "block",
        "reason": (
            "League canonical state is busy; unresolved obligations remain "
            "authoritative and Stop is safely retryable."
        ),
    }


def _champion_stop_output(command: str, result: dict[str, Any]) -> dict[str, Any]:
    if result["decision"] != "block":
        return {}
    reason = (
        f"League requires {result['callsign']} to record a fresh durable transition "
        f"for active task {result['task_id']} before this Champion turn ends: "
        f"{result['task_summary']}"
    )
    return (
        {"decision": "block", "reason": reason}
        if command == "codex-stop-hook"
        else {"followup_message": reason}
    )


def _codex_stop_reason(
    callsign: str, wait_generation: int, summaries: tuple[str, ...] = ()
) -> str:
    """Render only the resolved callsign; Codex turn identity stays internal."""

    return stop_feedback_reason(callsign, wait_generation, summaries)


def _hook_busy_timeout(command: str) -> int:
    if command in {"codex-stop-hook", "cursor-stop-hook"}:
        return STOP_BUSY_TIMEOUT_MS
    if command in {"codex-user-prompt-hook", "cursor-before-submit-hook"}:
        return PROMPT_BUSY_TIMEOUT_MS
    return DEFAULT_BUSY_TIMEOUT_MS


def _prompt_identity(
    adapter_kind: str, session_ref: str, raw_source_event_key: str, body: str
) -> tuple[str, str]:
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    identity = (
        f"{adapter_kind}\0{session_ref}\0{raw_source_event_key}\0{body_hash}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"prompt:hook:{digest}", f"hook:{digest}"


def _capture_prompt(
    store: SQLiteStorage,
    scope: str | None,
    actor_id: str | None,
    actor_role: str | None,
    payload: dict[str, Any],
    *,
    adapter_kind: str,
) -> dict[str, Any]:
    if adapter_kind == "codex":
        event_name = "UserPromptSubmit"
        session_ref = payload.get("session_id")
        raw_source_event_key = payload.get("turn_id")
    else:
        event_name = "beforeSubmitPrompt"
        session_ref = payload.get("conversation_id")
        raw_source_event_key = payload.get("generation_id")
    body = payload.get("prompt")
    if (
        payload.get("hook_event_name") != event_name
        or not isinstance(session_ref, str)
        or not session_ref
        or not isinstance(raw_source_event_key, str)
        or not raw_source_event_key
        or not isinstance(body, str)
        or not body
    ):
        raise StorageRefusal(
            "prompt_hook_invalid",
            "prompt hook requires its exact event, session, turn/generation, and body",
        )
    prompt_id, source_event_key = _prompt_identity(
        adapter_kind, session_ref, raw_source_event_key, body
    )
    if (
        adapter_kind == "codex"
        and actor_role == "shotcaller"
        and actor_id is not None
        and scope is not None
        and store.consume_stop_feedback(
            scope,
            actor_id,
            _codex_turn_generation(session_ref, raw_source_event_key),
            body,
        )
    ):
        return {
            "suppressed": "exact_stop_feedback",
            "prompt_id": None,
            "idempotent": False,
        }
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
    if actor_role != "shotcaller":
        if not runtimes:
            actor = store.connection.execute(
                """
                SELECT thread_id,backend,address
                  FROM agent_instances
                 WHERE agent_id=? AND retired_at IS NULL
                """,
                (actor_id,),
            ).fetchone()
            if (
                actor is not None
                and actor["thread_id"] == session_ref
                and actor["backend"] in {"herdr", "tmux"}
                and actor["address"]
            ):
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
                        at=now,
                        capabilities=("prompt.capture",),
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
        try:
            if quarantined is not None:
                return store.bind_quarantined_prompt(
                    prompt_id, actor_id, runtime_id, now, wake=False
                )
            return store.intake_prompt(
                prompt_id,
                actor_id,
                runtime_id,
                adapter_kind,
                session_ref,
                source_event_key,
                body,
                now,
                wake=False,
            )
        except StorageRefusal as exc:
            if exc.code not in {
                "runtime_unverified",
                "prompt_source_conflict",
                "prompt_binding_conflict",
            }:
                raise
            return store.quarantine_prompt(
                prompt_id, adapter_kind, session_ref, source_event_key, body, now
            )
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
                prompt_id, adapter_kind, session_ref, source_event_key, body, now,
                wake_actor_id=actor_id, wake_scope_id=scope,
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
            prompt_id, adapter_kind, session_ref, source_event_key, body, now,
            wake_actor_id=actor_id, wake_scope_id=scope,
        )
    runtime_id = str(runtimes[0]["runtime_instance_id"])
    quarantined = store.connection.execute(
        "SELECT state FROM prompt_quarantine WHERE prompt_id=?", (prompt_id,)
    ).fetchone()
    try:
        if quarantined is not None:
            return store.bind_quarantined_prompt(
                prompt_id, actor_id, runtime_id, now, wake_scope_id=scope
            )
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
        if exc.code in {"prompt_source_conflict", "prompt_binding_conflict"}:
            return store.quarantine_prompt(
                prompt_id, adapter_kind, session_ref, source_event_key, body, now
            )
        if exc.code != "runtime_unverified":
            raise
        return store.quarantine_prompt(
            prompt_id, adapter_kind, session_ref, source_event_key, body, now,
            wake_actor_id=actor_id, wake_scope_id=scope,
        )


def handle_brokered_hook(
    store: SQLiteStorage, hook: dict[str, Any]
) -> dict[str, Any]:
    """Execute one validated hook inside the persistent canonical-state owner."""

    command = hook.get("command")
    payload = hook.get("payload")
    shotcaller = hook.get("shotcaller")
    session_id = hook.get("session_id")
    if (
        command not in BROKERED_HOOK_COMMANDS
        or not isinstance(payload, dict)
        or (shotcaller is not None and not isinstance(shotcaller, str))
        or (session_id is not None and not isinstance(session_id, str))
    ):
        raise StorageRefusal("prompt_hook_invalid", "brokered hook request is invalid")
    args = argparse.Namespace(shotcaller=shotcaller, session_id=session_id)
    actor = _actor(store, args, payload)
    actor_id = None if actor is None else str(actor[0])
    callsign = None if actor is None else str(actor[1])
    actor_role = None if actor is None else str(actor[2])
    scope = None if actor is None else _scope(store, actor_id, str(callsign))
    if command == "codex-stop-hook":
        if actor is None:
            return {"hook_output": {}, "capture": None}
        assert actor_id is not None and callsign is not None and scope is not None
        terminal, _ = _codex_stop_generation(args, payload)
        if actor_role == "champion":
            result = store.champion_stop_decision(
                actor_id,
                terminal,
                datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            return {
                "hook_output": _champion_stop_output(command, result),
                "capture": None,
            }
        result = store.stop_decision(
            scope,
            actor_id,
            terminal,
            datetime.now().astimezone().isoformat(timespec="seconds"),
            block_on_fresh_terminal=True,
        )
        output = (
            {
                "decision": "block",
                "reason": _codex_stop_reason(
                    callsign,
                    result["wait_generation"],
                    tuple(result.get("unresolved_summaries", ())),
                ),
            }
            if result["decision"] == "block"
            else {}
        )
        return {"hook_output": output, "capture": None}
    captured = _capture_prompt(
        store,
        scope,
        actor_id,
        actor_role,
        payload,
        adapter_kind="codex" if command == "codex-user-prompt-hook" else "cursor",
    )
    return {
        "hook_output": {},
        "capture": {
            "prompt_id": captured.get("prompt_id"),
            "idempotent": bool(captured.get("idempotent", False)),
            "owned_by_shotcaller": actor_role == "shotcaller",
            "suppressed": captured.get("suppressed"),
            "state": captured.get("triage_state") or captured.get("state"),
        },
    }


def _broker_hook(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    locator = f"unix:{PersistentSupervisor(_state_root()).socket_path}"
    return send_supervisor_message(
        locator,
        {
            "kind": "hook",
            "hook": {
                "command": args.command,
                "shotcaller": args.shotcaller,
                "session_id": args.session_id,
                "payload": payload,
            },
        },
        timeout_seconds=0.1,
    )


def _direct_hook_fallback_safe(
    args: argparse.Namespace, payload: dict[str, Any]
) -> bool:
    """Permit direct storage only when no live or starting service can own the hook."""

    state_root = _state_root()
    socket_path = PersistentSupervisor(state_root).socket_path
    if socket_path.exists():
        return False
    with SQLiteStorage(
        state_root,
        busy_timeout_ms=_hook_busy_timeout(args.command),
    ) as store:
        actor = _actor(store, args, payload)
        if actor is None:
            return True
        registration = store.watcher_registration(str(actor[0]))
    if registration is None:
        return True
    if str(registration["wake_locator"]).startswith("sqlite-supervise:"):
        # The legacy bounded waiter is woken by the direct capture's canonical
        # generation update; it is not a second writer or persistent broker.
        return True
    try:
        leased_until = datetime.fromisoformat(str(registration["leased_until"]))
    except (TypeError, ValueError) as exc:
        raise StorageRefusal(
            "supervisor_ownership_uncertain",
            "persistent supervisor lease ownership could not be verified",
            retryable=True,
        ) from exc
    return leased_until <= datetime.now().astimezone()


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
             OR t.state IN ('active','pending','accepted','working','progress','in_progress','blocked','ready_to_land')
           )
         ORDER BY a.agent_id
        """,
        (actor_id,),
    ).fetchall()
    watcher_delivery = store.connection.execute(
        """
        SELECT r.event_id,r.received_at,e.status,e.occurred_at,e.update_text,
               COALESCE(
                 (SELECT a.callsign FROM agent_instances a WHERE a.agent_id=e.agent_id),
                 (SELECT a.callsign
                    FROM task_assignments x
                    JOIN agent_instances a ON a.agent_id=x.champion_agent_id
                   WHERE x.task_id=e.task_id
                   ORDER BY x.updated_at DESC,x.task_assignment_id DESC LIMIT 1)
               ) callsign
          FROM recipient_receipts r JOIN events e ON e.event_id=r.event_id
         WHERE r.recipient_agent_id=? AND r.effect_kind='watcher_event'
         ORDER BY r.received_at DESC,r.event_id DESC LIMIT 1
        """,
        (actor_id,),
    ).fetchone()
    return {
        "user_message_generation": 0 if generation is None else int(generation[0]),
        "obligations": _obligation_counts(store, actor_id),
        "champions": [dict(row) for row in champions],
        "watcher_delivery": None if watcher_delivery is None else dict(watcher_delivery),
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
               AND a.retired_at IS NULL AND a.role='shotcaller'
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
            if current["watcher_delivery"] != baseline["watcher_delivery"]:
                delivered = current["watcher_delivery"]
                if delivered is not None:
                    return {
                        "event": "champion-update",
                        "event_id": delivered["event_id"],
                        "callsign": delivered["callsign"],
                        "status": delivered["status"],
                        "at": delivered["occurred_at"],
                        "update": delivered["update_text"],
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
                if changed is None and current["obligations"]["pending_deliveries"] == 0:
                    return {
                        "event": "champions-idle",
                        "active": len(current["champions"]),
                        "shotcaller": callsign,
                        "writer": "sqlite",
                    }
                if changed is not None and current["obligations"]["pending_deliveries"] == 0:
                    return {
                        "event": "champion-update",
                        "callsign": changed["callsign"],
                        "status": changed["status"],
                        "at": changed["updated_at"],
                        "update": changed["update_text"],
                        "shotcaller": callsign,
                        "writer": "sqlite",
                    }
            before_obligations = {
                key: value
                for key, value in baseline["obligations"].items()
                if key != "pending_deliveries"
            }
            after_obligations = {
                key: value
                for key, value in current["obligations"].items()
                if key != "pending_deliveries"
            }
            if (
                after_obligations != before_obligations
                and current["obligations"]["pending_deliveries"] == 0
            ):
                return {
                    "event": "obligations-changed",
                    "before": before_obligations,
                    "after": after_obligations,
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
        "service-run",
        "service-resume",
        "service-status",
        "service-stop",
        "service-pause",
        "status",
    }:
        raise StorageRefusal(
            "legacy_writer_fenced",
            "SQLite is canonical; this legacy writer command is fenced",
        )
    if args.command == "service-run":
        return PersistentSupervisor(
            _state_root(),
            callsign=args.shotcaller,
            lease_seconds=args.lease_seconds,
            renew_seconds=args.renew_seconds,
        ).run()
    if args.command == "service-resume":
        _emit(resume_supervisor(_state_root(), args.shotcaller))
        return 0
    if args.command == "service-status":
        _emit(supervisor_status(_state_root(), args.shotcaller))
        return 0
    if args.command == "service-stop":
        _emit(stop_supervisor(_state_root(), args.shotcaller))
        return 0
    if args.command == "service-pause":
        _emit(pause_supervisor(_state_root(), args.shotcaller))
        return 0
    payload = _payload() if args.command.endswith("-hook") else {}
    if args.command in BROKERED_HOOK_COMMANDS:
        try:
            response = _broker_hook(args, payload)
        except StorageRefusal:
            raise
        except SupervisorUnavailable as exc:
            if not _direct_hook_fallback_safe(args, payload):
                raise StorageRefusal(
                    "supervisor_ownership_uncertain",
                    "persistent supervisor owns or may be starting this hook boundary",
                    retryable=True,
                ) from exc
        else:
            _emit(response["hook_output"])
            return 0
    try:
        store_context = SQLiteStorage(
            _state_root(),
            busy_timeout_ms=_hook_busy_timeout(args.command),
        )
    except StorageRefusal as exc:
        if exc.code == "busy" and args.command in {
            "codex-stop-hook",
            "cursor-stop-hook",
        }:
            _emit(_busy_stop_result(args, payload))
            return 0
        raise
    with store_context as store:
        actor = _actor(store, args, payload)
        actor_id = None if actor is None else str(actor[0])
        callsign = None if actor is None else str(actor[1])
        actor_role = None if actor is None else str(actor[2])
        scope = None if actor is None else _scope(store, actor_id, callsign)
        if args.command in {"codex-user-prompt-hook", "cursor-before-submit-hook"}:
            captured = _capture_prompt(
                store,
                scope,
                actor_id,
                actor_role,
                payload,
                adapter_kind=(
                    "codex" if args.command == "codex-user-prompt-hook" else "cursor"
                ),
            )
            if (
                actor_role == "shotcaller"
                and actor_id is not None
                and captured.get("prompt_id")
                and not captured.get("idempotent", False)
            ):
                notify_user_message(store, actor_id, str(captured["prompt_id"]))
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
            if args.command == "codex-stop-hook":
                terminal, turn_id = _codex_stop_generation(args, payload)
            else:
                terminal = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                turn_id = None
            try:
                if actor_role == "champion":
                    result = store.champion_stop_decision(
                        actor_id,
                        terminal,
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                    )
                else:
                    result = store.stop_decision(
                        scope,
                        actor_id,
                        terminal,
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                        block_on_fresh_terminal=args.command == "codex-stop-hook",
                    )
            except StorageRefusal as exc:
                if exc.code != "busy":
                    raise
                _emit(_busy_stop_result(args, payload))
                return 0
            if actor_role == "champion":
                _emit(_champion_stop_output(args.command, result))
                return 0
            blocked = result["decision"] == "block"
            if args.command == "cursor-stop-hook":
                _emit({"followup_message": "League has unresolved obligations."} if blocked else {})
            else:
                reason = _codex_stop_reason(
                    callsign,
                    result["wait_generation"],
                    tuple(result.get("unresolved_summaries", ())),
                )
                _emit(
                    {"decision": "block", "reason": reason}
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
