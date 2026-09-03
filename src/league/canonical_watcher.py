"""Fail-closed compatibility for hooks after SQLite becomes canonical."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .agent_adapters import SharedLifecyclePolicy, builtin_agent_adapter_registry
from .multiplexer_adapters import builtin_multiplexer_adapter_registry
from .sqlite_store import DEFAULT_BUSY_TIMEOUT_MS, SQLiteStorage
from .sqlite_watcher_ops import (
    obligation_counts,
    resolve_supervisor_scope,
    stop_feedback_reason,
)
from .sqlite_runtime_replacement_ops import runtime_replacement_mutation_fenced
from .storage import RuntimeRegistrationCommand, StorageRefusal
from .persistent_supervisor import (
    PersistentSupervisor,
    attach_shotcaller,
    detach_shotcaller,
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


def _hook_routes(operation: str) -> dict[str, str]:
    return {
        str(adapter.hook_profile[operation]["command"]): adapter.contract.kind
        for adapter in builtin_agent_adapter_registry().adapters()
    }


PROMPT_HOOK_ADAPTERS = _hook_routes("prompt_intake")
STOP_HOOK_ADAPTERS = _hook_routes("stop_supervision")
PRE_TOOL_HOOK_ADAPTERS = _hook_routes("pre_tool_authorization")
BROKERED_HOOK_COMMANDS = frozenset(
    PROMPT_HOOK_ADAPTERS | STOP_HOOK_ADAPTERS | PRE_TOOL_HOOK_ADAPTERS
)


def _registered_multiplexer(kind: Any) -> bool:
    if not isinstance(kind, str) or not kind:
        return False
    try:
        builtin_multiplexer_adapter_registry().adapter(kind)
    except StorageRefusal:
        return False
    return True


def _hook_profile(command: str, operation: str) -> tuple[Any, dict[str, Any]]:
    routes = {
        "prompt_intake": PROMPT_HOOK_ADAPTERS,
        "stop_supervision": STOP_HOOK_ADAPTERS,
        "pre_tool_authorization": PRE_TOOL_HOOK_ADAPTERS,
    }
    adapter = builtin_agent_adapter_registry().adapter(routes[operation][command])
    return adapter, dict(adapter.hook_profile[operation])


def _hook_operation(command: str) -> str:
    if command in PROMPT_HOOK_ADAPTERS:
        return "prompt_intake"
    if command in PRE_TOOL_HOOK_ADAPTERS:
        return "pre_tool_authorization"
    if command in STOP_HOOK_ADAPTERS:
        return "stop_supervision"
    raise StorageRefusal("hook_command_invalid", "provider hook command is not registered")


def _canonical_hook_payload(
    command: str,
    payload: dict[str, Any],
    *,
    explicit_shotcaller: str | None = None,
) -> dict[str, Any]:
    if (
        command in STOP_HOOK_ADAPTERS
        and explicit_shotcaller
        and not payload
    ):
        return {}
    adapter, _ = _hook_profile(command, _hook_operation(command))
    return dict(adapter.translate_hook_input(_hook_operation(command), payload))


def _native_hook_output(
    command: str, output: dict[str, Any], *, bound: bool
) -> dict[str, Any]:
    adapter, _ = _hook_profile(command, _hook_operation(command))
    canonical = dict(output)
    if adapter.contract.kind == "pi":
        canonical = {"binding": "bound" if bound else "unbound", **canonical}
    return dict(adapter.translate_hook_output(_hook_operation(command), canonical))


def _stop_output_mode(command: str) -> str:
    _, profile = _hook_profile(command, "stop_supervision")
    return str(profile.get("output_mode", "followup"))


def _needs_invocation_identity(command: str) -> bool:
    _, profile = _hook_profile(command, "prompt_intake")
    return profile.get("invocation_identity") is True


def _service_paths() -> dict[str, Path]:
    repository_root = Path(__file__).resolve().parents[2]
    plist = Path(
        os.environ.get(
            "LEAGUE_SUPERVISOR_PLIST",
            Path.home()
            / "Library/LaunchAgents/io.league-of-orchestrator.supervisor.plist",
        )
    )
    return {
        "agent_watcher": Path(
            os.environ.get(
                "LEAGUE_AGENT_WATCHER", repository_root / "bin/agent-watcher"
            )
        ),
        "template": Path(
            os.environ.get(
                "LEAGUE_SUPERVISOR_TEMPLATE",
                repository_root / "config/league-supervisor.launchd.plist.in",
            )
        ),
        "plist": plist,
        "backup": Path(
            os.environ.get(
                "LEAGUE_SUPERVISOR_BACKUP", f"{plist}.league-backup"
            )
        ),
        "manifest": Path(
            os.environ.get(
                "LEAGUE_SUPERVISOR_MANIFEST", f"{plist}.league-install.json"
            )
        ),
    }


def _add_service_file_options(parser: argparse.ArgumentParser) -> None:
    paths = _service_paths()
    parser.add_argument("--agent-watcher", type=Path, default=paths["agent_watcher"])
    parser.add_argument("--template", type=Path, default=paths["template"])
    parser.add_argument("--plist", type=Path, default=paths["plist"])
    parser.add_argument("--backup", type=Path, default=paths["backup"])
    parser.add_argument("--manifest", type=Path, default=paths["manifest"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-watcher")
    parser.add_argument("--shotcaller")
    parser.add_argument("--session-id")
    parser.add_argument("--records-root")
    parser.add_argument("--state-dir")
    parser.add_argument("--record-format")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in sorted(BROKERED_HOOK_COMMANDS):
        commands.add_parser(command)
    commands.add_parser("status")
    service_run = commands.add_parser("service-run")
    service_run.add_argument("--lease-seconds", type=float, default=60)
    service_run.add_argument("--renew-seconds", type=float, default=20)
    service_resume = commands.add_parser("service-resume")
    service_resume.add_argument("--lease-seconds", type=float, default=60)
    service_resume.add_argument("--renew-seconds", type=float, default=20)
    commands.add_parser("service-status")
    commands.add_parser("service-stop")
    commands.add_parser("attach-shotcaller")
    commands.add_parser("detach-shotcaller")
    commands.add_parser(
        "service-pause", help="Deprecated alias for detach-shotcaller."
    )
    service_install = commands.add_parser(
        "service-install",
        help="Hash-bind, install, start, and verify the user LaunchAgent.",
    )
    _add_service_file_options(service_install)
    service_install.add_argument("--expected-agent-watcher-sha256", required=True)
    service_install.add_argument("--expected-template-sha256", required=True)
    service_start = commands.add_parser(
        "service-start", help="Start or restart one exact installed LaunchAgent."
    )
    _add_service_file_options(service_start)
    service_rollback = commands.add_parser(
        "service-rollback",
        help="Stop the owned LaunchAgent and restore its exact prior plist.",
    )
    _add_service_file_options(service_rollback)
    service_rollback.add_argument("--expected-installed-plist-sha256", required=True)
    service_rollback.add_argument("--expected-backup-sha256")
    for name in ("supervise", "wait"):
        foreground = commands.add_parser(name)
        foreground.add_argument("--poll-seconds", type=float, default=1.0)
    allow_stop = commands.add_parser(
        "allow-stop", help="Authorize exactly one subsequent Shotcaller Stop."
    )
    allow_stop.add_argument("--once", action="store_true", required=True)
    deliver = commands.add_parser("deliver")
    deliver.add_argument("--event-id", required=True)
    for name in (
        "enable", "disable",
        "transition", "reconcile", "preflight", "launch", "resume", "teardown",
        "install-codex-hooks", "hidden-worker", "lead-relay", "route-model",
        "resource-inspect",
    ):
        if name not in commands.choices:
            commands.add_parser(name, add_help=False)
    return parser


def _state_root() -> Path:
    configured = os.environ.get("LEAGUE_STATE_ROOT")
    return Path(configured) if configured else Path.home() / ".local/state/league"


def _service_installer(args: argparse.Namespace) -> Any:
    from .supervisor_service import SupervisorServiceInstaller

    return SupervisorServiceInstaller(
        state_root=_state_root().resolve(),
        agent_watcher=args.agent_watcher.resolve(),
        template_path=args.template.resolve(),
        plist_path=args.plist.expanduser().resolve(),
        backup_path=args.backup.expanduser().resolve(),
        manifest_path=args.manifest.expanduser().resolve(),
    )


def _adapter_kind_for_command(command: str) -> str | None:
    for routes in (PROMPT_HOOK_ADAPTERS, STOP_HOOK_ADAPTERS, PRE_TOOL_HOOK_ADAPTERS):
        if command in routes:
            return routes[command]
    return None


def _actor(
    store: SQLiteStorage,
    args: argparse.Namespace,
    payload: dict[str, Any],
    *,
    adapter_kind: str | None = None,
) -> Any:
    session = (
        args.session_id
        or payload.get("session_path")
        or payload.get("session_id")
        or payload.get("conversation_id")
    )
    if session:
        runtime_rows = store.connection.execute(
            """
            SELECT DISTINCT a.agent_id,a.callsign,a.role,r.harness_kind
              FROM runtime_instances r
              JOIN agent_instances a ON a.agent_id=r.actor_agent_id
             WHERE r.session_ref=? AND r.status IN ('active','idle') AND r.verified=1
               AND a.retired_at IS NULL
             ORDER BY a.agent_id
            """,
            (session,),
        ).fetchall()
        if adapter_kind is not None:
            runtime_rows = [
                row
                for row in runtime_rows
                if str(row["harness_kind"]).removesuffix("-thread") == adapter_kind
            ]
        if len(runtime_rows) > 1:
            raise StorageRefusal(
                "runtime_identity_ambiguous",
                "hook session matches more than one live verified runtime",
            )
        if runtime_rows:
            return runtime_rows[0]
        legacy_rows = store.connection.execute(
            """
            SELECT agent_id,callsign,role,kind AS harness_kind
              FROM agent_instances
             WHERE retired_at IS NULL AND (thread_id=? OR agent_id=?)
             ORDER BY agent_id
            """,
            (session, session),
        ).fetchall()
        if adapter_kind is not None:
            legacy_rows = [
                row
                for row in legacy_rows
                if str(row["harness_kind"]).removesuffix("-thread") == adapter_kind
            ]
        if len(legacy_rows) > 1:
            raise StorageRefusal(
                "runtime_identity_ambiguous",
                "hook session matches more than one active agent identity",
            )
        if legacy_rows:
            return legacy_rows[0]
        return None
    if args.shotcaller:
        return store.connection.execute(
            "SELECT agent_id,callsign,role FROM agent_instances WHERE retired_at IS NULL AND role='shotcaller' AND callsign=?",
            (args.shotcaller,),
        ).fetchone()
    return None


def _scope(store: SQLiteStorage, actor_id: str, callsign: str) -> str:
    return str(resolve_supervisor_scope(store, actor_id, callsign)["scope_id"])


def _codex_turn_generation(session_ref: str, turn_id: str) -> str:
    identity = f"codex\0{session_ref}\0{turn_id}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _busy_stop_result(
    args: argparse.Namespace, payload: dict[str, Any]
) -> dict[str, str]:
    if _stop_output_mode(args.command) != "decision":
        return {
            "followup_message": (
                "League canonical state is busy; unresolved obligations remain "
                "authoritative and Stop is safely retryable."
            )
        }
    _stop_generation(args.command, args, payload)
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
        if _stop_output_mode(command) == "decision"
        else {"followup_message": reason}
    )


def _supervisor_unavailable_stop_output(command: str) -> dict[str, str]:
    reason = (
        "supervisor_unavailable: League's OS-managed persistent watcher is not live; "
        "no handoff was claimed. Run `agent-watcher service-start` through the "
        "supported owner release path, verify `service-status`, then retry Stop."
    )
    return (
        {"decision": "block", "reason": reason}
        if _stop_output_mode(command) == "decision"
        else {"followup_message": reason}
    )


def _codex_stop_reason(
    callsign: str, wait_generation: int, summaries: tuple[str, ...] = ()
) -> str:
    """Render only the resolved callsign; Codex turn identity stays internal."""

    return stop_feedback_reason(callsign, wait_generation, summaries)


def _hook_busy_timeout(command: str) -> int:
    if command in STOP_HOOK_ADAPTERS:
        return STOP_BUSY_TIMEOUT_MS
    if command in PROMPT_HOOK_ADAPTERS or command in PRE_TOOL_HOOK_ADAPTERS:
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


def _codex_prompt_invocation_id() -> str:
    """Mint one opaque identity for one provider hook invocation.

    Codex deliberately reuses ``turn_id`` for queued steers in the same active
    turn.  The invocation identity is therefore created once in the hook
    process and carried unchanged through broker retry or direct fallback.
    """

    return f"codex-user-prompt:{secrets.token_hex(16)}"


def _valid_codex_prompt_invocation_id(value: Any) -> bool:
    prefix = "codex-user-prompt:"
    return (
        isinstance(value, str)
        and len(value) == len(prefix) + 32
        and value.startswith(prefix)
        and all(character in "0123456789abcdef" for character in value[len(prefix):])
    )


def _capture_prompt(
    store: SQLiteStorage,
    scope: str | None,
    actor_id: str | None,
    actor_role: str | None,
    payload: dict[str, Any],
    *,
    adapter_kind: str,
    capture_event_id: str | None = None,
) -> dict[str, Any]:
    adapter = builtin_agent_adapter_registry().adapter(adapter_kind)
    profile = dict(adapter.hook_profile["prompt_intake"])
    event_name = str(profile["native_event"])
    session_ref = payload.get(str(profile["session_field"]))
    provider_turn_id = payload.get(str(profile["source_field"]))
    raw_source_event_key = provider_turn_id
    if profile.get("invocation_identity") is True:
        raw_source_event_key = (
            f"{provider_turn_id}\0{capture_event_id}"
            if isinstance(provider_turn_id, str)
            and provider_turn_id
            and isinstance(capture_event_id, str)
            and capture_event_id
            else None
        )
    body = payload.get("prompt")
    if (
        payload.get("hook_event_name")
        != profile["hook_event"]
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
    translated_payload = {**payload, "source_event_key": raw_source_event_key}
    event = adapter.translate_event(
        event_name, translated_payload
    )
    decision = SharedLifecyclePolicy().decide(event)
    if decision.outcome != "accept":
        raise StorageRefusal(decision.reason_code, "shared agent lifecycle policy refused prompt intake")
    session_ref = event.session_ref
    raw_source_event_key = event.source_event_key
    prompt_id, source_event_key = _prompt_identity(
        adapter_kind, session_ref, raw_source_event_key, body
    )
    if (
        profile.get("stop_feedback_suppression") is True
        and actor_role == "shotcaller"
        and actor_id is not None
        and scope is not None
        and store.consume_stop_feedback(
            scope,
            actor_id,
            _codex_turn_generation(session_ref, provider_turn_id),
            body,
        )
    ):
        return {
            "suppressed": "exact_stop_feedback",
            "prompt_id": None,
            "idempotent": False,
        }
    # Preserve provider invocation order for multiple steers accepted inside
    # one Codex turn; second precision collapses ordinary queued submissions.
    now = datetime.now().astimezone().isoformat(timespec="microseconds")
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
                and _registered_multiplexer(actor["backend"])
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
            or not _registered_multiplexer(actor["backend"])
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


def _stop_generation(
    command: str, args: argparse.Namespace, payload: dict[str, Any]
) -> tuple[str, str | None]:
    adapter, profile = _hook_profile(command, "stop_supervision")
    adapter_kind = adapter.contract.kind
    if args.shotcaller and not payload and _stop_output_mode(command) == "decision":
        explicit = f"explicit\0{args.shotcaller}"
        return hashlib.sha256(explicit.encode()).hexdigest(), None
    session = payload.get(str(profile["session_field"]))
    source = payload.get(str(profile["source_field"]))
    active_field = profile.get("active_field")
    if (
        payload.get("hook_event_name") != profile["hook_event"]
        or not isinstance(session, str)
        or not session
        or not isinstance(source, str)
        or not source
        or (
            isinstance(active_field, str)
            and not isinstance(payload.get(active_field), bool)
        )
    ):
        raise StorageRefusal(
            "stop_hook_invalid",
            "Stop hook requires its exact provider event, session, and generation",
        )
    event = adapter.translate_event(
        str(profile["native_event"]),
        {**payload, "source_event_key": source},
    )
    decision = SharedLifecyclePolicy().decide(event)
    if decision.outcome != "accept":
        raise StorageRefusal(decision.reason_code, "shared lifecycle policy refused Stop")
    identity = f"{adapter_kind}\0{event.session_ref}\0{event.source_event_key}"
    return hashlib.sha256(identity.encode()).hexdigest(), source


def _pre_tool_output(
    command: str,
    payload: dict[str, Any],
    *,
    exact_binding: bool,
    actor_role: str | None,
    delegated_by_shotcaller: bool,
    mutation_fenced: bool = False,
) -> dict[str, Any]:
    adapter, profile = _hook_profile(command, "pre_tool_authorization")
    session = payload.get(str(profile["session_field"]))
    source = payload.get(str(profile["source_field"]))
    if (
        payload.get("hook_event_name") != profile["hook_event"]
        or not isinstance(session, str)
        or not session
        or not isinstance(source, str)
        or not source
    ):
        raise StorageRefusal(
            "pre_tool_hook_invalid",
            "pre-tool hook requires exact provider event, session, and generation",
        )
    event = adapter.translate_event(
        str(profile["native_event"]),
        {**payload, "source_event_key": source},
    )
    decision = SharedLifecyclePolicy().decide(
        event,
        authorized=True,
        exact_binding=exact_binding,
        actor_role=actor_role,
        delegated_by_shotcaller=delegated_by_shotcaller,
        mutation_fenced=mutation_fenced,
    )
    return {
        "decision": decision.outcome,
        "reason_code": decision.reason_code,
    }


def _delegation_verified(
    store: SQLiteStorage, actor_id: str | None, actor_role: str | None
) -> bool:
    if actor_id is None or actor_role not in {"champion", "hidden-worker"}:
        return False
    row = store.connection.execute(
        """
        SELECT owner.role,owner.retired_at
          FROM agent_instances child
          JOIN agent_instances owner ON owner.agent_id=child.shotcaller_agent_id
         WHERE child.agent_id=? AND child.role=? AND child.retired_at IS NULL
        """,
        (actor_id, actor_role),
    ).fetchone()
    return bool(row is not None and row["role"] == "shotcaller" and row["retired_at"] is None)


def _priority_eligible_capture(
    actor_id: str | None,
    actor_role: str | None,
    captured: dict[str, Any],
) -> bool:
    """Trust only the atomic storage receipt for user-priority delivery."""

    return bool(
        actor_role == "shotcaller"
        and actor_id is not None
        and isinstance(captured.get("prompt_id"), str)
        and captured.get("wake_committed") is True
    )


def _notify_direct_user_priority(
    store: SQLiteStorage,
    actor_id: str | None,
    actor_role: str | None,
    captured: dict[str, Any],
) -> bool:
    if not _priority_eligible_capture(actor_id, actor_role, captured):
        return False
    assert actor_id is not None
    return notify_user_message(store, actor_id, str(captured["prompt_id"]))


@dataclass(frozen=True)
class BrokeredHookContext:
    command: Any
    payload: Any
    capture_event_id: Any
    args: argparse.Namespace
    actor: Any
    actor_id: str | None
    callsign: str | None
    actor_role: str | None
    scope: str | None


def brokered_hook_context(
    store: SQLiteStorage, hook: dict[str, Any]
) -> BrokeredHookContext:
    command = hook.get("command")
    payload = hook.get("payload")
    shotcaller = hook.get("shotcaller")
    session_id = hook.get("session_id")
    capture_event_id = hook.get("capture_event_id")
    if (
        command not in BROKERED_HOOK_COMMANDS
        or not isinstance(payload, dict)
        or (shotcaller is not None and not isinstance(shotcaller, str))
        or (session_id is not None and not isinstance(session_id, str))
        or (
            command in PROMPT_HOOK_ADAPTERS
            and _needs_invocation_identity(command)
            and not _valid_codex_prompt_invocation_id(capture_event_id)
        )
    ):
        raise StorageRefusal("prompt_hook_invalid", "brokered hook request is invalid")
    payload = _canonical_hook_payload(
        str(command), payload, explicit_shotcaller=shotcaller
    )
    args = argparse.Namespace(shotcaller=shotcaller, session_id=session_id)
    actor = _actor(
        store, args, payload, adapter_kind=_adapter_kind_for_command(str(command))
    )
    actor_id = None if actor is None else str(actor[0])
    callsign = None if actor is None else str(actor[1])
    actor_role = None if actor is None else str(actor[2])
    scope = None if actor is None else _scope(store, actor_id, str(callsign))
    return BrokeredHookContext(
        command=command,
        payload=payload,
        capture_event_id=capture_event_id,
        args=args,
        actor=actor,
        actor_id=actor_id,
        callsign=callsign,
        actor_role=actor_role,
        scope=scope,
    )


def handle_brokered_hook(
    store: SQLiteStorage,
    hook: dict[str, Any],
    *,
    context: BrokeredHookContext | None = None,
) -> dict[str, Any]:
    """Execute one validated hook inside the persistent canonical-state owner."""

    resolved = context or brokered_hook_context(store, hook)
    command = resolved.command
    payload = resolved.payload
    capture_event_id = resolved.capture_event_id
    args = resolved.args
    actor = resolved.actor
    actor_id = resolved.actor_id
    callsign = resolved.callsign
    actor_role = resolved.actor_role
    scope = resolved.scope
    if actor is None:
        return {
            "hook_output": _native_hook_output(command, {}, bound=False),
            "capture": None,
            "actor_agent_id": None,
        }
    if command in PRE_TOOL_HOOK_ADAPTERS:
        return {
            "hook_output": _native_hook_output(
                command,
                _pre_tool_output(
                    command,
                    payload,
                    exact_binding=actor is not None,
                    actor_role=actor_role,
                    delegated_by_shotcaller=_delegation_verified(
                        store, actor_id, actor_role
                    ),
                    mutation_fenced=bool(
                        actor_id
                        and runtime_replacement_mutation_fenced(store, actor_id)
                    ),
                ),
                bound=True,
            ),
            "capture": None,
            "actor_agent_id": actor_id,
        }
    if command in STOP_HOOK_ADAPTERS:
        if actor is None:
            return {
                "hook_output": _native_hook_output(command, {}, bound=False),
                "capture": None,
                "actor_agent_id": None,
            }
        assert actor_id is not None and callsign is not None and scope is not None
        terminal, _ = _stop_generation(command, args, payload)
        if actor_role == "champion":
            result = store.champion_stop_decision(
                actor_id,
                terminal,
                datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            return {
                "hook_output": _native_hook_output(
                    command, _champion_stop_output(command, result), bound=True
                ),
                "capture": None,
                "actor_agent_id": actor_id,
            }
        result = store.stop_decision(
            scope,
            actor_id,
            terminal,
            datetime.now().astimezone().isoformat(timespec="seconds"),
            block_on_fresh_terminal=False,
        )
        if result["decision"] != "block":
            output = {}
        elif _stop_output_mode(command) != "decision":
            output = {
                "followup_message": _codex_stop_reason(
                    callsign,
                    result["wait_generation"],
                    tuple(result.get("unresolved_summaries", ())),
                )
            }
        else:
            output = {
                "decision": "block",
                "reason": _codex_stop_reason(
                    callsign,
                    result["wait_generation"],
                    tuple(result.get("unresolved_summaries", ())),
                ),
            }
        return {
            "hook_output": _native_hook_output(command, output, bound=True),
            "capture": None,
            "actor_agent_id": actor_id,
            "supervision_handoff": result.get("supervision_handoff") is True,
        }
    captured = _capture_prompt(
        store,
        scope,
        actor_id,
        actor_role,
        payload,
        adapter_kind=PROMPT_HOOK_ADAPTERS[command],
        capture_event_id=capture_event_id,
    )
    priority_eligible = _priority_eligible_capture(actor_id, actor_role, captured)
    return {
        "hook_output": _native_hook_output(command, {}, bound=True),
        "actor_agent_id": actor_id,
        "capture": {
            "prompt_id": captured.get("prompt_id"),
            "idempotent": bool(captured.get("idempotent", False)),
            "owned_by_shotcaller": actor_role == "shotcaller",
            "wake_committed": captured.get("wake_committed") is True,
            "priority_eligible": priority_eligible,
            "suppressed": captured.get("suppressed"),
            "state": captured.get("triage_state") or captured.get("state"),
        },
    }


def _broker_hook(
    args: argparse.Namespace,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = 0.1,
    capture_event_id: str | None = None,
) -> dict[str, Any]:
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
                "capture_event_id": capture_event_id,
            },
        },
        timeout_seconds=timeout_seconds,
    )


def _persistent_service_lock_held(state_root: Path) -> bool:
    supervisor = PersistentSupervisor(state_root)
    supervisor.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = supervisor.lock_path.open("a+")
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        lock.close()


@contextmanager
def _direct_hook_fallback_store(
    args: argparse.Namespace, payload: dict[str, Any]
) -> Iterator[SQLiteStorage]:
    """Open one direct-hook store behind the shared service-start fence."""

    state_root = _state_root()
    supervisor = PersistentSupervisor(state_root)
    supervisor.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = supervisor.lock_path.open("a+")
    acquired = False
    try:
        try:
            # Direct hooks may run concurrently, but a persistent supervisor's
            # exclusive service lock fences every direct fallback boundary.
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise StorageRefusal(
                "supervisor_ownership_uncertain",
                "persistent supervisor owns or may be starting this hook boundary",
                retryable=True,
            ) from exc
        if supervisor.socket_path.exists():
            raise StorageRefusal(
                "supervisor_ownership_uncertain",
                "persistent supervisor socket exists without a verified broker response",
                retryable=True,
            )
        with SQLiteStorage(
            state_root,
            busy_timeout_ms=_hook_busy_timeout(args.command),
        ) as store:
            actor = _actor(
                store,
                args,
                payload,
                adapter_kind=_adapter_kind_for_command(args.command),
            )
            registration = (
                None if actor is None else store.watcher_registration(str(actor[0]))
            )
            if registration is not None and not str(
                registration["wake_locator"]
            ).startswith("sqlite-supervise:"):
                try:
                    leased_until = datetime.fromisoformat(
                        str(registration["leased_until"])
                    )
                except (TypeError, ValueError) as exc:
                    raise StorageRefusal(
                        "supervisor_ownership_uncertain",
                        "persistent supervisor lease ownership could not be verified",
                        retryable=True,
                    ) from exc
                if leased_until > datetime.now().astimezone():
                    raise StorageRefusal(
                        "supervisor_ownership_uncertain",
                        "persistent supervisor still owns the hook boundary",
                        retryable=True,
                    )
            yield store
    finally:
        if acquired:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


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
        "obligations": obligation_counts(store, actor_id),
        "champions": [dict(row) for row in champions],
        "watcher_delivery": None if watcher_delivery is None else dict(watcher_delivery),
    }


def _supervise(
    store: SQLiteStorage,
    scope: str,
    actor_id: str,
    callsign: str,
    poll_seconds: float,
    *,
    own_watcher_registration: bool = True,
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
        if own_watcher_registration:
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
        # `initial` is the event baseline. Publishing wait_active in
        # rearm_wait only after this snapshot makes it the readiness signal and
        # prevents an immediate user/event wake from falling into a gap.
        baseline = initial
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
    if args.command not in BROKERED_HOOK_COMMANDS | {
        "deliver",
        "supervise",
        "service-run",
        "service-resume",
        "service-status",
        "service-stop",
        "service-pause",
        "service-install",
        "service-start",
        "service-rollback",
        "attach-shotcaller",
        "detach-shotcaller",
        "status",
        "allow-stop",
        "wait",
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
    if args.command == "service-install":
        _emit(
            _service_installer(args).install(
                expected_agent_watcher_sha256=args.expected_agent_watcher_sha256,
                expected_template_sha256=args.expected_template_sha256,
            )
        )
        return 0
    if args.command == "service-start":
        _emit(_service_installer(args).start())
        return 0
    if args.command == "service-rollback":
        _emit(
            _service_installer(args).rollback(
                expected_installed_plist_sha256=(
                    args.expected_installed_plist_sha256
                ),
                expected_backup_sha256=args.expected_backup_sha256,
            )
        )
        return 0
    if args.command == "service-resume":
        _emit(resume_supervisor(_state_root(), args.shotcaller))
        return 0
    if args.command == "attach-shotcaller":
        _emit(attach_shotcaller(_state_root(), args.shotcaller))
        return 0
    if args.command == "detach-shotcaller":
        _emit(detach_shotcaller(_state_root(), args.shotcaller))
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
        payload = _canonical_hook_payload(
            args.command, payload, explicit_shotcaller=args.shotcaller
        )
    capture_event_id = (
        _codex_prompt_invocation_id()
        if args.command in PROMPT_HOOK_ADAPTERS
        and _needs_invocation_identity(args.command)
        else None
    )
    fallback_store = None
    if args.command in BROKERED_HOOK_COMMANDS:
        try:
            response = _broker_hook(
                args, payload, capture_event_id=capture_event_id
            )
        except StorageRefusal:
            raise
        except SupervisorUnavailable:
            persistent_required = False
            try:
                with SQLiteStorage(
                    _state_root(),
                    busy_timeout_ms=_hook_busy_timeout(args.command),
                    request_wal=False,
                ) as observer:
                    actor = _actor(
                        observer,
                        args,
                        payload,
                        adapter_kind=_adapter_kind_for_command(args.command),
                    )
                    if actor is None:
                        _emit(_native_hook_output(args.command, {}, bound=False))
                        return 0
                    persistent_required = bool(
                        args.command in STOP_HOOK_ADAPTERS
                        and str(actor[2]) == "shotcaller"
                        and observer.persistent_supervision_required(str(actor[0]))
                    )
            except StorageRefusal as exc:
                if exc.code in {"invalid_root", "store_missing"}:
                    _emit(_native_hook_output(args.command, {}, bound=False))
                    return 0
                if exc.code == "busy" and args.command in STOP_HOOK_ADAPTERS:
                    _emit(
                        _native_hook_output(
                            args.command, _busy_stop_result(args, payload), bound=True
                        )
                    )
                    return 0
                raise
            if _persistent_service_lock_held(_state_root()):
                try:
                    response = _broker_hook(
                        args,
                        payload,
                        timeout_seconds=0.4,
                        capture_event_id=capture_event_id,
                    )
                except SupervisorUnavailable:
                    pass
                else:
                    _emit(response["hook_output"])
                    return 0
            if args.command in STOP_HOOK_ADAPTERS and persistent_required:
                _emit(
                    _native_hook_output(
                        args.command,
                        _supervisor_unavailable_stop_output(args.command),
                        bound=True,
                    )
                )
                return 0
            fallback_store = _direct_hook_fallback_store(args, payload)
        else:
            _emit(response["hook_output"])
            return 0
    stack = ExitStack()
    try:
        store = stack.enter_context(
            fallback_store
            if fallback_store is not None
            else SQLiteStorage(
                _state_root(),
                busy_timeout_ms=_hook_busy_timeout(args.command),
            )
        )
    except StorageRefusal as exc:
        stack.close()
        if exc.code == "busy" and args.command in STOP_HOOK_ADAPTERS:
            _emit(
                _native_hook_output(
                    args.command, _busy_stop_result(args, payload), bound=True
                )
            )
            return 0
        raise
    with stack:
        actor = _actor(
            store,
            args,
            payload,
            adapter_kind=_adapter_kind_for_command(args.command),
        )
        actor_id = None if actor is None else str(actor[0])
        callsign = None if actor is None else str(actor[1])
        actor_role = None if actor is None else str(actor[2])
        scope = None if actor is None else _scope(store, actor_id, callsign)
        if args.command in BROKERED_HOOK_COMMANDS and actor is None:
            _emit(_native_hook_output(args.command, {}, bound=False))
            return 0
        if args.command in PROMPT_HOOK_ADAPTERS:
            captured = _capture_prompt(
                store,
                scope,
                actor_id,
                actor_role,
                payload,
                adapter_kind=PROMPT_HOOK_ADAPTERS[args.command],
                capture_event_id=capture_event_id,
            )
            _notify_direct_user_priority(store, actor_id, actor_role, captured)
            _emit(_native_hook_output(args.command, {}, bound=True))
            return 0
        if args.command in PRE_TOOL_HOOK_ADAPTERS:
            _emit(
                _native_hook_output(
                    args.command,
                    _pre_tool_output(
                        args.command,
                        payload,
                        exact_binding=actor is not None,
                        actor_role=actor_role,
                        delegated_by_shotcaller=_delegation_verified(
                            store, actor_id, actor_role
                        ),
                        mutation_fenced=bool(
                            actor_id
                            and runtime_replacement_mutation_fenced(store, actor_id)
                        ),
                    ),
                    bound=True,
                )
            )
            return 0
        if actor is None:
            _emit({})
            return 0
        assert actor_id is not None and callsign is not None and scope is not None
        if args.command == "allow-stop":
            if actor_role != "shotcaller":
                raise StorageRefusal(
                    "allow_stop_invalid",
                    "one-shot Stop authorization requires an active Shotcaller",
                )
            _emit(store.set_allow_stop_once(scope, actor_id))
            return 0
        if args.command in {"supervise", "wait"}:
            _emit(
                _supervise(
                    store,
                    scope,
                    actor_id,
                    callsign,
                    args.poll_seconds,
                    own_watcher_registration=args.command == "supervise",
                )
            )
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
        if args.command in STOP_HOOK_ADAPTERS:
            terminal, turn_id = _stop_generation(args.command, args, payload)
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
                        block_on_fresh_terminal=False,
                    )
            except StorageRefusal as exc:
                if exc.code != "busy":
                    raise
                _emit(
                    _native_hook_output(
                        args.command, _busy_stop_result(args, payload), bound=True
                    )
                )
                return 0
            if actor_role == "champion":
                _emit(
                    _native_hook_output(
                        args.command,
                        _champion_stop_output(args.command, result),
                        bound=True,
                    )
                )
                return 0
            blocked = result["decision"] == "block"
            if _stop_output_mode(args.command) != "decision":
                _emit(
                    _native_hook_output(
                        args.command,
                        {
                            "followup_message": _codex_stop_reason(
                                callsign,
                                result["wait_generation"],
                                tuple(result.get("unresolved_summaries", ())),
                            )
                        }
                        if blocked else {},
                        bound=True,
                    )
                )
            else:
                reason = _codex_stop_reason(
                    callsign,
                    result["wait_generation"],
                    tuple(result.get("unresolved_summaries", ())),
                )
                _emit(
                    _native_hook_output(
                        args.command,
                        {"decision": "block", "reason": reason}
                        if blocked
                        else {},
                        bound=True,
                    )
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
