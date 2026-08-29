"""Fail-closed compatibility for hooks after SQLite becomes canonical."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .sqlite_store import SQLiteStorage
from .storage import StorageRefusal


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
    for name in (
        "enable", "disable", "allow-stop", "wait", "supervise", "deliver",
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


def main(argv: list[str] | None = None) -> int:
    args, _ = _parser().parse_known_args(argv)
    if args.command not in {
        "codex-stop-hook",
        "codex-user-prompt-hook",
        "cursor-stop-hook",
        "cursor-before-submit-hook",
        "status",
    }:
        raise StorageRefusal(
            "legacy_writer_fenced",
            "SQLite is canonical; this legacy writer command is fenced",
        )
    payload = _payload() if args.command.endswith("-hook") else {}
    with SQLiteStorage(_state_root(), request_wal=False) as store:
        actor = _actor(store, args, payload)
        if actor is None:
            _emit({})
            return 0
        actor_id, callsign = str(actor[0]), str(actor[1])
        scope = _scope(store, actor_id, callsign)
        if args.command in {"codex-user-prompt-hook", "cursor-before-submit-hook"}:
            store.note_user_message(
                scope, actor_id, datetime.now().astimezone().isoformat(timespec="seconds")
            )
            _emit({})
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
