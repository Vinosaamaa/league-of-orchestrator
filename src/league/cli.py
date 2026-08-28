"""Stable ``league`` command facade over the composite storage interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional

from . import MAX_ACCEPTANCE_SENTINEL_PATHS, __version__
from .adapters import builtin_contract_registry
from .cleanup import CleanupPlanner
from .importer import build_import_plan
from .routing import ModelRouter, load_routing_config
from .skill_contracts import (
    audit_installations,
    capability_matrix as skill_capability_matrix,
    load_json_object as load_skill_json_object,
    validate_contract as validate_skill_contract,
)
from .sqlite_store import DEFAULT_BUSY_TIMEOUT_MS, MAX_EXPORT_RECORDS, SQLiteStorage
from .storage import (
    DispatchRequestCommand,
    OutboxDispatchIdentity,
    PrepareAssignmentCommand,
    RuntimeRegistrationCommand,
    Storage,
    StorageRefusal,
)
from .storage_request import (
    MAX_TRIAGE_JSON_BYTES,
    AnswerRequestCommand,
    RequestResultCommand,
)


COMMAND_SCHEMA = "league.command.v1"
MAX_JSON_INPUT_BYTES = 1_000_000
REQUEST_STATE_COMMANDS = {
    "awaiting-user": "awaiting_user",
    "block": "blocked",
    "defer": "deferred",
    "cancel": "cancelled",
}
CommandResult = tuple[Any, Optional[bytes]]
CommandHandler = Callable[[Storage, argparse.Namespace], CommandResult]


class _BoundedSentinelPath(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        value: Path,
        option_string: Optional[str] = None,
    ) -> None:
        paths = list(getattr(namespace, self.dest, None) or [])
        if len(paths) >= MAX_ACCEPTANCE_SENTINEL_PATHS:
            raise argparse.ArgumentError(
                self,
                f"at most {MAX_ACCEPTANCE_SENTINEL_PATHS} sentinel paths are allowed",
            )
        paths.append(value)
        setattr(namespace, self.dest, paths)


def _add_acceptance_commands(groups: argparse._SubParsersAction) -> None:
    acceptance = groups.add_parser(
        "acceptance", help="Run isolated acceptance beneath an explicit temporary root."
    )
    commands = acceptance.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run", help="Create and verify one namespaced disposable League home.")
    run.add_argument("--temporary-root", type=Path, required=True)
    run.add_argument("--namespace", required=True)
    run.add_argument(
        "--sentinel-path",
        type=Path,
        action=_BoundedSentinelPath,
        required=True,
    )
    run.add_argument("--config-sentinel", type=Path, required=True)
    run.add_argument("--process-sentinel", type=Path, required=True)


def _add_storage_commands(groups: argparse._SubParsersAction) -> None:
    storage = groups.add_parser("storage", help="Migrate, inspect, import, back up, and export storage.")
    commands = storage.add_subparsers(dest="action", required=True)
    migrate = commands.add_parser("migrate", help="Apply reviewed migrations transactionally.")
    migrate.add_argument(
        "--backup-name",
        help="Safe state-root-relative backup name; required before upgrading an existing database.",
    )
    commands.add_parser("integrity", help="Run integrity_check and foreign_key_check.")
    backup = commands.add_parser("backup", help="Create and verify a SQLite Online Backup snapshot.")
    backup.add_argument("--name", required=True, help="Safe state-root-relative backup name.")
    export = commands.add_parser("export", help="Emit a bounded deterministic non-canonical export.")
    export.add_argument("--format", choices=("json", "jsonl"), default="json")
    export.add_argument("--purpose", choices=("inspection", "rollback"), default="inspection")
    export.add_argument("--max-records", type=int, default=MAX_EXPORT_RECORDS)
    export.add_argument(
        "--output-name",
        help="Safe state-root-relative output; required for restricted rollback exports.",
    )
    import_command = commands.add_parser(
        "import", help="Validate every declared legacy artifact; dry-run is the default."
    )
    import_command.add_argument("--source-root", type=Path, required=True)
    import_command.add_argument("--manifest", type=Path, required=True)
    import_command.add_argument("--apply", action="store_true")
    import_command.add_argument(
        "--expected-digest",
        help="Exact report_digest from the immediately preceding equivalent dry-run; required with --apply.",
    )


def _add_agent_commands(groups: argparse._SubParsersAction) -> None:
    agent = groups.add_parser("agent", help="Read or atomically transition one agent incarnation.")
    commands = agent.add_subparsers(dest="action", required=True)
    status = commands.add_parser("status", help="Read current agent state.")
    status.add_argument("--agent-id", required=True)
    transition = commands.add_parser("transition", help="Append event and update state atomically.")
    transition.add_argument("--agent-id", required=True)
    transition.add_argument("--expected-version", type=int, required=True)
    transition.add_argument("--status", required=True)
    transition.add_argument("--update", required=True)
    transition.add_argument("--at", required=True)


def _add_callsign_commands(groups: argparse._SubParsersAction) -> None:
    callsign = groups.add_parser("callsign", help="Reserve or release one exact callsign lease.")
    commands = callsign.add_subparsers(dest="action", required=True)
    reserve = commands.add_parser("reserve", help="Reserve callsign, incarnation, and event atomically.")
    reserve.add_argument("--callsign", required=True)
    reserve.add_argument("--agent-id", required=True)
    reserve.add_argument("--task-id", required=True)
    reserve.add_argument("--role", choices=("shotcaller", "champion", "hidden-worker"), required=True)
    reserve.add_argument("--status", required=True)
    reserve.add_argument("--update", required=True)
    reserve.add_argument("--at", required=True)
    release = commands.add_parser("release", help="Release an exact live callsign lease atomically.")
    release.add_argument("--callsign", required=True)
    release.add_argument("--agent-id", required=True)
    release.add_argument("--expected-version", type=int, required=True)
    release.add_argument("--at", required=True)


def _add_delivery_commands(groups: argparse._SubParsersAction) -> None:
    delivery = groups.add_parser("delivery", help="Claim, acknowledge, or fail exact event delivery.")
    commands = delivery.add_subparsers(dest="action", required=True)
    claim = commands.add_parser("claim", help="Claim a delivery with an explicit bounded lease.")
    claim.add_argument("--event-id", required=True)
    claim.add_argument("--recipient-agent-id", required=True)
    claim.add_argument("--claim-token", required=True)
    claim.add_argument("--claim-expires-at", required=True)
    claim.add_argument("--at", required=True, help="RFC3339 claim observation time.")
    for name in ("ack", "fail"):
        command = commands.add_parser(name, help=f"{name.title()} one exact claimed delivery.")
        command.add_argument("--event-id", required=True)
        command.add_argument("--recipient-agent-id", required=True)
        command.add_argument("--claim-token", required=True)
        command.add_argument("--at", required=True)
        if name == "fail":
            command.add_argument("--reason", required=True)
    claim_outbox = commands.add_parser(
        "claim-outbox", help="Claim one exact source-event-bound outbox row."
    )
    for name in ("outbox-id", "event-id", "recipient-agent-id", "dispatcher-id", "attempt-id"):
        claim_outbox.add_argument(f"--{name}", required=True)
    claim_outbox.add_argument("--lease-expires-at", required=True)
    claim_outbox.add_argument("--at", required=True)
    ack_outbox = commands.add_parser(
        "ack-outbox", help="Apply one exact recipient effect and delivery receipt atomically."
    )
    for name in (
        "outbox-id",
        "event-id",
        "recipient-agent-id",
        "dispatcher-id",
        "attempt-id",
        "adapter-kind",
        "effect-kind",
        "effect-id",
    ):
        ack_outbox.add_argument(f"--{name}", required=True)
    ack_outbox.add_argument("--fence", type=int, required=True)
    ack_outbox.add_argument("--at", required=True)
    fail_outbox = commands.add_parser(
        "fail-outbox", help="Record a bounded failed attempt and return the outbox to pending."
    )
    for name in (
        "outbox-id",
        "event-id",
        "recipient-agent-id",
        "dispatcher-id",
        "attempt-id",
        "adapter-kind",
        "reason",
    ):
        fail_outbox.add_argument(f"--{name}", required=True)
    fail_outbox.add_argument("--fence", type=int, required=True)
    fail_outbox.add_argument("--retry-at", required=True)
    fail_outbox.add_argument("--at", required=True)
    backlog = commands.add_parser("backlog", help="List a fair bounded page of due outbox rows.")
    backlog.add_argument("--at", required=True)
    backlog.add_argument("--limit", type=int, default=100)
    backlog.add_argument("--per-recipient", type=int, default=2)


def _add_project_commands(groups: argparse._SubParsersAction) -> None:
    project = groups.add_parser("project", help="Resolve exact project identity.")
    commands = project.add_subparsers(dest="action", required=True)
    resolve = commands.add_parser("resolve", help="Resolve one exact repository URL.")
    resolve.add_argument("--repository", required=True)


def _add_task_commands(groups: argparse._SubParsersAction) -> None:
    task = groups.add_parser("task", help="Transfer task ownership with expected-version concurrency.")
    commands = task.add_subparsers(dest="action", required=True)
    transfer = commands.add_parser(
        "transfer-owner", help="Update task owner, event, and assignment receipt atomically."
    )
    transfer.add_argument("--task-id", required=True)
    transfer.add_argument("--expected-version", type=int, required=True)
    transfer.add_argument("--owner-kind", choices=("agent", "squad"), required=True)
    transfer.add_argument("--owner-id", required=True)
    transfer.add_argument("--at", required=True)
    transition = commands.add_parser(
        "transition", help="Record one exact task transition, event, and coordinator outbox row."
    )
    for name in (
        "task-id",
        "runtime-instance-id",
        "state",
        "update",
        "next-action",
        "transition-id",
        "transition-key",
        "event-id",
        "outbox-id",
        "recipient-agent-id",
        "at",
    ):
        transition.add_argument(f"--{name}", required=True)
    transition.add_argument("--expected-version", type=int, required=True)
    transition.add_argument("--blocker")


def _add_runtime_commands(groups: argparse._SubParsersAction) -> None:
    runtime = groups.add_parser("runtime", help="Inspect registered harness/backend capabilities.")
    commands = runtime.add_subparsers(dest="action", required=True)
    commands.add_parser("matrix", help="Report supported, unsupported, and unverified adapter operations.")


def _add_skill_commands(groups: argparse._SubParsersAction) -> None:
    skill = groups.add_parser(
        "skill", help="Validate skill provenance, installation parity, and runtime capabilities."
    )
    commands = skill.add_subparsers(dest="action", required=True)
    validate = commands.add_parser("validate", help="Validate one repository-local skill contract.")
    validate.add_argument("--config", type=Path, required=True)
    audit = commands.add_parser(
        "audit", help="Audit explicit custom roots without returning local paths or skill bodies."
    )
    audit.add_argument("--config", type=Path, required=True)
    audit.add_argument(
        "--root",
        action="append",
        required=True,
        metavar="LABEL=ABSOLUTE_PATH",
        help="Bind one declared public root label to one exact local custom root.",
    )
    matrix = commands.add_parser(
        "matrix", help="Resolve skill availability against one explicit runtime capability profile."
    )
    matrix.add_argument("--config", type=Path, required=True)
    matrix.add_argument("--profile", type=Path, required=True)


def _add_routing_commands(groups: argparse._SubParsersAction) -> None:
    routing = groups.add_parser("routing", help="Choose and measure evidence-based model/effort routes.")
    commands = routing.add_subparsers(dest="action", required=True)
    choose = commands.add_parser("choose", help="Record one semantic route without assigning work.")
    choose.add_argument("--config", type=Path, required=True)
    choose.add_argument("--decision-id", required=True)
    choose.add_argument("--subject-kind", required=True)
    choose.add_argument("--subject-id", required=True)
    choose.add_argument("--role", required=True)
    choose.add_argument(
        "--profile",
        choices=("coordination", "bounded", "ambiguous", "high-impact", "weak-verification"),
        required=True,
    )
    choose.add_argument("--model")
    choose.add_argument("--effort")
    choose.add_argument("--at", required=True)
    escalate = commands.add_parser("escalate", help="Record at most one evidence-triggered stronger retry.")
    escalate.add_argument("--config", type=Path, required=True)
    escalate.add_argument("--decision-id", required=True)
    escalate.add_argument("--prior-decision-id", required=True)
    escalate.add_argument(
        "--failure-class",
        choices=(
            "schema_failure",
            "tool_failure",
            "missing_evidence",
            "ambiguity",
            "conflicting_results",
            "failed_acceptance",
            "high_impact_boundary",
        ),
        required=True,
    )
    escalate.add_argument("--at", required=True)
    outcome = commands.add_parser("outcome", help="Record role-level success, correction, latency, and cost evidence.")
    outcome.add_argument("--config", type=Path, required=True)
    outcome.add_argument("--outcome-id", required=True)
    outcome.add_argument("--decision-id", required=True)
    outcome.add_argument("--success", choices=("true", "false"), required=True)
    outcome.add_argument("--corrections", type=int, required=True)
    outcome.add_argument("--latency-ms", type=int, required=True)
    outcome.add_argument("--cost-microunits", type=int, required=True)
    outcome.add_argument("--at", required=True)


def _add_resource_commands(groups: argparse._SubParsersAction) -> None:
    resource = groups.add_parser("resource", help="Register exact typed task resources.")
    commands = resource.add_subparsers(dest="action", required=True)
    register = commands.add_parser("register", help="Register one task resource from a strict JSON object.")
    register.add_argument("--spec", type=Path, required=True)
    register.add_argument("--at", required=True)


def _add_cleanup_commands(groups: argparse._SubParsersAction) -> None:
    cleanup = groups.add_parser("cleanup", help="Plan and inspect proof-gated recoverable cleanup.")
    commands = cleanup.add_subparsers(dest="action", required=True)
    plan = commands.add_parser("plan", help="Validate all policy proof and claim one cleanup revision.")
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--operation-id", required=True)
    plan.add_argument("--at", required=True)
    status = commands.add_parser("status", help="Read one cleanup operation and ordered action state.")
    status.add_argument("--operation-id", required=True)


def _add_request_commands(groups: argparse._SubParsersAction) -> None:
    request = groups.add_parser(
        "request", help="Capture, triage, claim, route, resolve, answer, and reconcile requests."
    )
    commands = request.add_subparsers(dest="action", required=True)
    intake = commands.add_parser("intake", help="Capture one complete prompt exactly once.")
    for name in (
        "prompt-id",
        "intake-actor-id",
        "runtime-instance-id",
        "adapter-kind",
        "session-ref",
        "source-event-key",
        "body",
        "at",
    ):
        intake.add_argument(f"--{name}", required=True)
    triage = commands.add_parser("triage", help="Commit complete ordered prompt-item accounting.")
    triage.add_argument("--prompt-id", required=True)
    triage.add_argument("--items-json", required=True, help="JSON array of bounded prompt items.")
    triage.add_argument("--at", required=True)
    claim = commands.add_parser("claim", help="Acquire or recover one request mutation claim.")
    for name in ("request-id", "runtime-instance-id", "claim-token", "leased-until", "at"):
        claim.add_argument(f"--{name}", required=True)
    release = commands.add_parser("release", help="Release the exact current request claim.")
    for name in ("request-id", "runtime-instance-id", "claim-token", "at"):
        release.add_argument(f"--{name}", required=True)
    dispatch = commands.add_parser(
        "dispatch", help="Record direct, hidden, or Champion execution before substantive action."
    )
    for name in ("request-id", "claim-token", "dispatch-id", "work-kind", "at"):
        dispatch.add_argument(f"--{name}", required=True)
    dispatch.add_argument("--requested-mode", choices=("direct", "hidden", "champion"))
    dispatch.add_argument("--hidden-supported", action="store_true")
    dispatch.add_argument("--requested-model")
    dispatch.add_argument("--requested-effort")
    dispatch.add_argument("--explicit-route")
    route = commands.add_parser("route", help="Route ownership with event and outbox atomically.")
    for name in (
        "request-id",
        "claim-token",
        "recipient-agent-id",
        "event-id",
        "outbox-id",
        "at",
    ):
        route.add_argument(f"--{name}", required=True)
    route.add_argument("--expected-version", type=int, required=True)
    accept = commands.add_parser(
        "accept", help="Claim and accept one exactly received routed request."
    )
    for name in ("request-id", "runtime-instance-id", "claim-token", "leased-until", "at"):
        accept.add_argument(f"--{name}", required=True)
    for state in REQUEST_STATE_COMMANDS:
        command = commands.add_parser(state, help=f"Record an explicit {state} request transition.")
        for name in ("request-id", "claim-token", "summary", "event-id", "at"):
            command.add_argument(f"--{name}", required=True)
        command.add_argument("--expected-version", type=int, required=True)
        if state == "defer":
            command.add_argument("--next-attention-at", required=True)
    result = commands.add_parser(
        "result", help="Record an owner result and optionally return ownership atomically."
    )
    for name in (
        "request-id",
        "claim-token",
        "result-id",
        "idempotency-key",
        "outcome",
        "summary",
        "at",
    ):
        result.add_argument(f"--{name}", required=True)
    result.add_argument("--expected-version", type=int, required=True)
    result.add_argument("--task-id", action="append", default=[])
    result.add_argument("--return-to-requester", action="store_true")
    result.add_argument("--event-id")
    result.add_argument("--outbox-id")
    answer = commands.add_parser("answer", help="Record response evidence and answer one request.")
    for name in (
        "request-id",
        "claim-token",
        "response-ref-id",
        "adapter-kind",
        "session-locator",
        "response-locator",
        "content-hash",
        "resolution-summary",
        "event-id",
        "at",
    ):
        answer.add_argument(f"--{name}", required=True)
    answer.add_argument("--durability", choices=("durable", "ephemeral"), required=True)
    answer.add_argument("--expected-version", type=int, required=True)
    unresolved = commands.add_parser(
        "unresolved", help="Query unresolved work before reply, wait, handoff, or end."
    )
    unresolved.add_argument("--owner-agent-id", required=True)
    unresolved.add_argument("--before-action", choices=("reply", "wait", "handoff", "end"))
    unresolved.add_argument("--limit", type=int, default=100)


def _add_assignment_commands(groups: argparse._SubParsersAction) -> None:
    assignment = groups.add_parser(
        "assign", help="Drive recoverable pending, launching, active, blocked, or cleanup-pending assignment state."
    )
    commands = assignment.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser("prepare", help="Reserve one visible Champion assignment before launch.")
    for name in (
        "assignment-id",
        "request-id",
        "claim-token",
        "task-id",
        "task-summary",
        "coordinator-agent-id",
        "champion-agent-id",
        "callsign",
        "repository",
        "branch",
        "worktree",
        "at",
    ):
        prepare.add_argument(f"--{name}", required=True)
    prepare.add_argument("--issue", type=int, required=True)
    launching = commands.add_parser("launching", help="Commit launch intent before adapter work.")
    launching.add_argument("--assignment-id", required=True)
    launching.add_argument("--expected-version", type=int, required=True)
    launching.add_argument("--at", required=True)
    activate = commands.add_parser("activate", help="Activate only from an exact verified Champion receipt.")
    activate.add_argument("--assignment-id", required=True)
    activate.add_argument("--expected-version", type=int, required=True)
    activate.add_argument("--receipt-json", required=True)
    activate.add_argument("--event-id", required=True)
    activate.add_argument("--outbox-id", required=True)
    activate.add_argument("--at", required=True)
    block = commands.add_parser("block", help="Record a blocked or cleanup-pending failed launch.")
    block.add_argument("--assignment-id", required=True)
    block.add_argument("--expected-version", type=int, required=True)
    block.add_argument("--failure-class", required=True)
    block.add_argument("--cleanup-required", action="store_true")
    block.add_argument("--cleanup-proven", action="store_true")
    block.add_argument("--at", required=True)


def _add_hook_commands(groups: argparse._SubParsersAction) -> None:
    hook = groups.add_parser("hook", help="Record runtime/watcher leases and make one bounded Stop decision.")
    commands = hook.add_subparsers(dest="action", required=True)
    runtime = commands.add_parser("register-runtime", help="Register one exact verified runtime generation.")
    for name in (
        "runtime-instance-id",
        "actor-agent-id",
        "harness-kind",
        "backend-kind",
        "session-ref",
        "endpoint",
        "runtime-generation",
        "at",
    ):
        runtime.add_argument(f"--{name}", required=True)
    runtime.add_argument("--status", choices=("active", "idle", "closed", "failed"), required=True)
    runtime.add_argument("--verified", action="store_true")
    watcher = commands.add_parser("register-watcher", help="Register one distinct wake lease.")
    for name in (
        "scope-id",
        "watcher-id",
        "actor-agent-id",
        "runtime-instance-id",
        "wake-locator",
        "leased-until",
        "at",
    ):
        watcher.add_argument(f"--{name}", required=True)
    watcher.add_argument("--fence", type=int, required=True)
    watcher.add_argument("--no-block", action="store_true")
    user = commands.add_parser("user-message", help="Give an ordinary user message priority and rearm waiting.")
    user.add_argument("--scope-id", required=True)
    user.add_argument("--actor-agent-id", required=True)
    user.add_argument("--at", required=True)
    rearm = commands.add_parser("rearm", help="Bind the next possible block to a fresh event wait generation.")
    for name in ("scope-id", "actor-agent-id", "event-id", "at"):
        rearm.add_argument(f"--{name}", required=True)
    allow = commands.add_parser("allow-stop-once", help="Permit one explicit final Stop decision.")
    allow.add_argument("--scope-id", required=True)
    allow.add_argument("--actor-agent-id", required=True)
    stop = commands.add_parser("stop", help="Combine request, assignment, delivery, and cleanup obligations once.")
    for name in ("scope-id", "actor-agent-id", "terminal-generation", "at"):
        stop.add_argument(f"--{name}", required=True)


def _add_help_commands(groups: argparse._SubParsersAction) -> None:
    help_group = groups.add_parser("help", help="Emit machine-readable command and schema inventory.")
    commands = help_group.add_subparsers(dest="action", required=True)
    commands.add_parser("inventory", help="Emit the versioned command inventory.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="league",
        description="Operate League's canonical store through stable domain commands; SQL is not exposed.",
    )
    parser.add_argument("--version", action="version", version=f"league {__version__}")
    parser.add_argument(
        "--state-root",
        type=Path,
        help=(
            "Explicit absolute League state root; required for state-backed commands. "
            "Help, skill validation, and separately rooted acceptance create no state here."
        ),
    )
    parser.add_argument(
        "--busy-timeout-ms",
        type=int,
        default=DEFAULT_BUSY_TIMEOUT_MS,
        help="Bounded SQLite contention timeout in milliseconds (default: %(default)s).",
    )
    parser.add_argument(
        "--no-wal",
        action="store_true",
        help="Request rollback-journal mode even when the loaded runtime passes the WAL gate.",
    )
    groups = parser.add_subparsers(dest="group", required=True)
    for builder in (
        _add_storage_commands,
        _add_agent_commands,
        _add_callsign_commands,
        _add_delivery_commands,
        _add_project_commands,
        _add_task_commands,
        _add_runtime_commands,
        _add_skill_commands,
        _add_routing_commands,
        _add_resource_commands,
        _add_cleanup_commands,
        _add_request_commands,
        _add_assignment_commands,
        _add_hook_commands,
        _add_help_commands,
        _add_acceptance_commands,
    ):
        builder(groups)
    return parser


def _command_name(args: argparse.Namespace) -> str:
    return f"{args.group}.{args.action}"


def _envelope_bytes(
    command: str, *, result: Any = None, error: Optional[StorageRefusal] = None
) -> bytes:
    if error is None:
        value = {"schema": COMMAND_SCHEMA, "ok": True, "command": command, "result": result}
    else:
        value = {
            "schema": COMMAND_SCHEMA,
            "ok": False,
            "command": command,
            "error": {"code": error.code, "message": str(error), "retryable": error.retryable},
        }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _open(args: argparse.Namespace) -> SQLiteStorage:
    if args.state_root is None:
        raise StorageRefusal("state_root_required", "operation requires an explicit absolute state root")
    return SQLiteStorage(
        args.state_root,
        busy_timeout_ms=args.busy_timeout_ms,
        request_wal=not args.no_wal,
    )


def _storage_integrity(store: Storage, _: argparse.Namespace) -> CommandResult:
    result = store.integrity()
    if not result["ok"]:
        raise StorageRefusal("integrity_failed", "database integrity or foreign-key check failed")
    return result, None


def _storage_backup(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.backup(args.name), None


def _storage_export(store: Storage, args: argparse.Namespace) -> CommandResult:
    payload = store.export_bytes(
        format_name=args.format, purpose=args.purpose, max_records=args.max_records
    )
    if args.purpose == "rollback":
        if not args.output_name:
            raise StorageRefusal("output_required", "rollback export requires a restricted output name")
        store.write_restricted(args.output_name, payload)
        return {
            "schema": "league.export-receipt.v1",
            "purpose": "rollback",
            "format": args.format,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }, None
    if args.output_name:
        raise StorageRefusal("invalid_export", "inspection export writes to standard output only")
    return None, payload


def _storage_import(store: Storage, args: argparse.Namespace) -> CommandResult:
    plan = build_import_plan(
        args.source_root, args.manifest, target_counts=store.import_target_counts()
    )
    if not args.apply:
        return plan["report"], None
    if not args.expected_digest:
        raise StorageRefusal("import_digest_required", "--apply requires --expected-digest")
    return store.apply_import(plan, args.expected_digest), None


def _agent_status(store: Storage, args: argparse.Namespace) -> CommandResult:
    value = store.agent_status(args.agent_id)
    return {"found": value is not None, "agent": value}, None


def _agent_transition(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.transition(
        args.agent_id, args.expected_version, args.status, args.update, args.at
    ), None


def _callsign_reserve(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.reserve_callsign(
        args.callsign, args.agent_id, args.task_id, args.role, args.status, args.update, args.at
    ), None


def _callsign_release(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.release_callsign(
        args.callsign, args.agent_id, args.expected_version, args.at
    ), None


def _delivery_claim(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.claim_delivery(
        args.event_id,
        args.recipient_agent_id,
        args.claim_token,
        args.claim_expires_at,
        args.at,
    ), None


def _delivery_ack(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.acknowledge_delivery(
        args.event_id, args.recipient_agent_id, args.claim_token, args.at
    ), None


def _delivery_fail(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.fail_delivery(
        args.event_id, args.recipient_agent_id, args.claim_token, args.reason, args.at
    ), None


def _project_resolve(store: Storage, args: argparse.Namespace) -> CommandResult:
    value = store.resolve_project(args.repository)
    return {"found": value is not None, "project": value}, None


def _task_transfer(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.transfer_task_owner(
        args.task_id, args.expected_version, args.owner_kind, args.owner_id, args.at
    ), None


def _task_transition(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.transition_task(
        args.task_id,
        args.runtime_instance_id,
        args.expected_version,
        args.state,
        args.update,
        args.next_action,
        args.blocker,
        args.transition_id,
        args.transition_key,
        args.event_id,
        args.outbox_id,
        args.recipient_agent_id,
        args.at,
    ), None


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_JSON_INPUT_BYTES + 1)
        if len(payload) > MAX_JSON_INPUT_BYTES:
            raise StorageRefusal(
                "input_too_large",
                f"JSON input exceeds the {MAX_JSON_INPUT_BYTES}-byte limit",
            )
        value = json.loads(payload.decode("utf-8"))
    except StorageRefusal:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("input_invalid", "JSON input could not be read") from exc
    if not isinstance(value, dict):
        raise StorageRefusal("input_invalid", "JSON input must be an object")
    return value


def _runtime_matrix(_: Storage, __: argparse.Namespace) -> CommandResult:
    return builtin_contract_registry().capability_matrix(), None


def _skill_contract(args: argparse.Namespace) -> dict[str, Any]:
    return load_skill_json_object(args.config, label="skill contract")


def _skill_validate(args: argparse.Namespace) -> CommandResult:
    return validate_skill_contract(_skill_contract(args)), None


def _skill_audit(args: argparse.Namespace) -> CommandResult:
    bindings: dict[str, Path] = {}
    for raw in args.root:
        if not isinstance(raw, str) or "=" not in raw:
            raise StorageRefusal(
                "skill_root_invalid", "skill root must use LABEL=ABSOLUTE_PATH"
            )
        label, raw_path = raw.split("=", 1)
        if not label or not raw_path or label in bindings:
            raise StorageRefusal("skill_root_invalid", "skill root binding is invalid or duplicated")
        bindings[label] = Path(raw_path)
    return audit_installations(_skill_contract(args), bindings), None


def _skill_matrix(args: argparse.Namespace) -> CommandResult:
    profile = load_skill_json_object(args.profile, label="skill runtime profile")
    return skill_capability_matrix(
        _skill_contract(args), profile, builtin_contract_registry()
    ), None


def _router(store: Storage, args: argparse.Namespace) -> ModelRouter:
    return ModelRouter(load_routing_config(args.config), store)


def _routing_choose(store: Storage, args: argparse.Namespace) -> CommandResult:
    return _router(store, args).choose(
        decision_id=args.decision_id,
        subject_kind=args.subject_kind,
        subject_id=args.subject_id,
        role=args.role,
        profile=args.profile,
        chosen_at=args.at,
        explicit_model=args.model,
        explicit_effort=args.effort,
    ), None


def _routing_escalate(store: Storage, args: argparse.Namespace) -> CommandResult:
    return _router(store, args).escalate(
        decision_id=args.decision_id,
        prior_decision_id=args.prior_decision_id,
        failure_class=args.failure_class,
        chosen_at=args.at,
    ), None


def _routing_outcome(store: Storage, args: argparse.Namespace) -> CommandResult:
    return _router(store, args).record_outcome(
        outcome_id=args.outcome_id,
        decision_id=args.decision_id,
        success=args.success == "true",
        corrections=args.corrections,
        latency_ms=args.latency_ms,
        cost_microunits=args.cost_microunits,
        recorded_at=args.at,
    ), None


def _resource_register(store: Storage, args: argparse.Namespace) -> CommandResult:
    return CleanupPlanner(store).register_resource(_read_json_object(args.spec), args.at), None


def _cleanup_plan(store: Storage, args: argparse.Namespace) -> CommandResult:
    return CleanupPlanner(store).plan(
        _read_json_object(args.manifest), operation_id=args.operation_id, at=args.at
    ), None


def _cleanup_status(store: Storage, args: argparse.Namespace) -> CommandResult:
    value = store.cleanup_operation(args.operation_id)
    return {"found": value is not None, "operation": value}, None


def _request_intake(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.intake_prompt(
        args.prompt_id,
        args.intake_actor_id,
        args.runtime_instance_id,
        args.adapter_kind,
        args.session_ref,
        args.source_event_key,
        args.body,
        args.at,
    ), None


def _decode_json(value: str, label: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise StorageRefusal("invalid_json", f"{label} must be valid JSON") from exc


def _request_triage(store: Storage, args: argparse.Namespace) -> CommandResult:
    if len(args.items_json.encode("utf-8")) > MAX_TRIAGE_JSON_BYTES:
        raise StorageRefusal(
            "invalid_json", "triage items exceed the bounded encoded size"
        )
    items = _decode_json(args.items_json, "triage items")
    if not isinstance(items, list):
        raise StorageRefusal("invalid_json", "triage items must be a JSON array")
    return store.triage_prompt(args.prompt_id, items, args.at), None


def _request_claim(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.claim_request(
        args.request_id,
        args.runtime_instance_id,
        args.claim_token,
        args.leased_until,
        args.at,
    ), None


def _request_release(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.release_request_claim(
        args.request_id, args.runtime_instance_id, args.claim_token, args.at
    ), None


def _request_dispatch(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.dispatch_request(
        DispatchRequestCommand(
            request_id=args.request_id,
            claim_token=args.claim_token,
            dispatch_id=args.dispatch_id,
            work_kind=args.work_kind,
            requested_mode=args.requested_mode,
            hidden_supported=args.hidden_supported,
            requested_model=args.requested_model,
            requested_effort=args.requested_effort,
            explicit_route=args.explicit_route,
            at=args.at,
        )
    ), None


def _request_route(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.route_request(
        args.request_id,
        args.claim_token,
        args.expected_version,
        args.recipient_agent_id,
        args.event_id,
        args.outbox_id,
        args.at,
    ), None


def _request_state(store: Storage, args: argparse.Namespace) -> CommandResult:
    state = REQUEST_STATE_COMMANDS[args.action]
    return store.set_request_state(
        args.request_id,
        args.claim_token,
        args.expected_version,
        state,
        args.summary,
        args.event_id,
        args.at,
        next_attention_at=getattr(args, "next_attention_at", None),
    ), None


def _request_result(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.record_request_result(
        RequestResultCommand(
            request_id=args.request_id,
            claim_token=args.claim_token,
            expected_version=args.expected_version,
            result_id=args.result_id,
            idempotency_key=args.idempotency_key,
            outcome=args.outcome,
            summary=args.summary,
            task_ids=tuple(args.task_id),
            at=args.at,
            return_to_requester=args.return_to_requester,
            event_id=args.event_id,
            outbox_id=args.outbox_id,
        )
    ), None


def _request_answer(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.answer_request(
        AnswerRequestCommand(
            request_id=args.request_id,
            claim_token=args.claim_token,
            expected_version=args.expected_version,
            response_ref_id=args.response_ref_id,
            adapter_kind=args.adapter_kind,
            session_locator=args.session_locator,
            response_locator=args.response_locator,
            durability=args.durability,
            content_hash=args.content_hash,
            resolution_summary=args.resolution_summary,
            event_id=args.event_id,
            at=args.at,
        )
    ), None


def _request_unresolved(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.unresolved_requests(
        args.owner_agent_id, limit=args.limit, before_action=args.before_action
    ), None


def _assign_prepare(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.prepare_assignment(
        PrepareAssignmentCommand(
            assignment_id=args.assignment_id,
            request_id=args.request_id,
            claim_token=args.claim_token,
            task_id=args.task_id,
            task_summary=args.task_summary,
            coordinator_agent_id=args.coordinator_agent_id,
            champion_agent_id=args.champion_agent_id,
            callsign=args.callsign,
            repository=args.repository,
            issue=args.issue,
            branch=args.branch,
            worktree=args.worktree,
            at=args.at,
        )
    ), None


def _assign_launching(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.mark_assignment_launching(
        args.assignment_id, args.expected_version, args.at
    ), None


def _assign_activate(store: Storage, args: argparse.Namespace) -> CommandResult:
    receipt = _decode_json(args.receipt_json, "assignment receipt")
    if not isinstance(receipt, dict):
        raise StorageRefusal("invalid_json", "assignment receipt must be a JSON object")
    return store.activate_assignment(
        args.assignment_id,
        args.expected_version,
        receipt,
        args.event_id,
        args.outbox_id,
        args.at,
    ), None


def _assign_block(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.block_assignment(
        args.assignment_id,
        args.expected_version,
        args.failure_class,
        args.cleanup_required,
        args.cleanup_proven,
        args.at,
    ), None


def _delivery_claim_outbox(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.claim_outbox(
        OutboxDispatchIdentity(
            outbox_id=args.outbox_id,
            event_id=args.event_id,
            recipient_agent_id=args.recipient_agent_id,
            dispatcher_id=args.dispatcher_id,
            attempt_id=args.attempt_id,
        ),
        args.lease_expires_at,
        args.at,
    ), None


def _delivery_ack_outbox(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.acknowledge_outbox(
        OutboxDispatchIdentity(
            outbox_id=args.outbox_id,
            event_id=args.event_id,
            recipient_agent_id=args.recipient_agent_id,
            dispatcher_id=args.dispatcher_id,
            attempt_id=args.attempt_id,
        ),
        args.fence,
        args.adapter_kind,
        args.effect_kind,
        args.effect_id,
        args.at,
    ), None


def _delivery_fail_outbox(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.fail_outbox(
        OutboxDispatchIdentity(
            outbox_id=args.outbox_id,
            event_id=args.event_id,
            recipient_agent_id=args.recipient_agent_id,
            dispatcher_id=args.dispatcher_id,
            attempt_id=args.attempt_id,
        ),
        args.fence,
        args.adapter_kind,
        args.reason,
        args.retry_at,
        args.at,
    ), None


def _delivery_backlog(store: Storage, args: argparse.Namespace) -> CommandResult:
    return {
        "rows": store.pending_backlog(
            args.at, limit=args.limit, per_recipient=args.per_recipient
        )
    }, None


def _hook_register_runtime(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.register_runtime(
        RuntimeRegistrationCommand(
            runtime_instance_id=args.runtime_instance_id,
            actor_agent_id=args.actor_agent_id,
            harness_kind=args.harness_kind,
            backend_kind=args.backend_kind,
            session_ref=args.session_ref,
            endpoint=args.endpoint,
            runtime_generation=args.runtime_generation,
            status=args.status,
            verified=args.verified,
            at=args.at,
        )
    ), None


def _hook_register_watcher(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.register_watcher(
        args.scope_id,
        args.watcher_id,
        args.actor_agent_id,
        args.runtime_instance_id,
        args.wake_locator,
        args.leased_until,
        args.fence,
        args.at,
        block_on_obligations=not args.no_block,
    ), None


def _hook_user_message(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.note_user_message(args.scope_id, args.actor_agent_id, args.at), None


def _hook_rearm(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.rearm_wait(args.scope_id, args.actor_agent_id, args.event_id, args.at), None


def _hook_allow_stop_once(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.set_allow_stop_once(args.scope_id, args.actor_agent_id), None


def _hook_stop(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.stop_decision(
        args.scope_id, args.actor_agent_id, args.terminal_generation, args.at
    ), None


HANDLERS: dict[str, CommandHandler] = {
    "storage.integrity": _storage_integrity,
    "storage.backup": _storage_backup,
    "storage.export": _storage_export,
    "storage.import": _storage_import,
    "agent.status": _agent_status,
    "agent.transition": _agent_transition,
    "callsign.reserve": _callsign_reserve,
    "callsign.release": _callsign_release,
    "delivery.claim": _delivery_claim,
    "delivery.ack": _delivery_ack,
    "delivery.fail": _delivery_fail,
    "delivery.claim-outbox": _delivery_claim_outbox,
    "delivery.ack-outbox": _delivery_ack_outbox,
    "delivery.fail-outbox": _delivery_fail_outbox,
    "delivery.backlog": _delivery_backlog,
    "project.resolve": _project_resolve,
    "task.transfer-owner": _task_transfer,
    "task.transition": _task_transition,
    "runtime.matrix": _runtime_matrix,
    "routing.choose": _routing_choose,
    "routing.escalate": _routing_escalate,
    "routing.outcome": _routing_outcome,
    "resource.register": _resource_register,
    "cleanup.plan": _cleanup_plan,
    "cleanup.status": _cleanup_status,
    "request.intake": _request_intake,
    "request.triage": _request_triage,
    "request.claim": _request_claim,
    "request.accept": _request_claim,
    "request.release": _request_release,
    "request.dispatch": _request_dispatch,
    "request.route": _request_route,
    "request.awaiting-user": _request_state,
    "request.block": _request_state,
    "request.defer": _request_state,
    "request.cancel": _request_state,
    "request.result": _request_result,
    "request.answer": _request_answer,
    "request.unresolved": _request_unresolved,
    "assign.prepare": _assign_prepare,
    "assign.launching": _assign_launching,
    "assign.activate": _assign_activate,
    "assign.block": _assign_block,
    "hook.register-runtime": _hook_register_runtime,
    "hook.register-watcher": _hook_register_watcher,
    "hook.user-message": _hook_user_message,
    "hook.rearm": _hook_rearm,
    "hook.allow-stop-once": _hook_allow_stop_once,
    "hook.stop": _hook_stop,
}


SCHEMA_INVENTORY = (
    "league-command-output.schema.json",
    "league-import-report.schema.json",
    "league-export.schema.json",
    "league-acceptance-receipt.schema.json",
    "league-help.schema.json",
    "league-request-triage.schema.json",
    "league-assignment-receipt.schema.json",
    "league-stop-decision.schema.json",
    "league-skill-contracts.schema.json",
    "league-skill-runtime-profile.schema.json",
    "league-skill-validation.schema.json",
    "league-skill-audit.schema.json",
    "league-skill-matrix.schema.json",
)

CONFIG_ONLY_COMMANDS = {
    "skill.validate": _skill_validate,
    "skill.audit": _skill_audit,
    "skill.matrix": _skill_matrix,
}


def _help_inventory() -> dict[str, Any]:
    return {
        "schema": "league.help.v1",
        "command_schema": COMMAND_SCHEMA,
        "commands": sorted(
            (
                *HANDLERS,
                *CONFIG_ONLY_COMMANDS,
                "storage.migrate",
                "acceptance.run",
                "help.inventory",
            )
        ),
        "schemas": list(SCHEMA_INVENTORY),
        "execution_modes": ["direct", "hidden", "champion"],
        "request_states": [
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
        ],
        "assignment_states": ["pending", "launching", "active", "blocked", "cleanup_pending"],
        "lease_kinds": ["request_claim", "outbox_dispatch", "watcher_registration"],
    }


def _run(args: argparse.Namespace) -> CommandResult:
    command = _command_name(args)
    if command == "help.inventory":
        return _help_inventory(), None
    if command == "acceptance.run":
        from .acceptance import run_acceptance

        if args.state_root is not None:
            raise StorageRefusal(
                "invalid_acceptance_root",
                "acceptance uses --temporary-root and refuses --state-root",
            )
        return run_acceptance(
            args.temporary_root,
            args.namespace,
            sentinel_paths=tuple(args.sentinel_path),
            config_sentinel=args.config_sentinel,
            process_sentinel=args.process_sentinel,
        ), None
    if command in CONFIG_ONLY_COMMANDS:
        if args.state_root is not None:
            raise StorageRefusal(
                "invalid_skill_state_root",
                "skill validation uses explicit config/root inputs and refuses --state-root",
            )
        return CONFIG_ONLY_COMMANDS[command](args)
    if command == "storage.migrate":
        if args.state_root is None:
            raise StorageRefusal(
                "state_root_required", "migration requires an explicit absolute state root"
            )
        with SQLiteStorage.for_migration(
            args.state_root,
            busy_timeout_ms=args.busy_timeout_ms,
            request_wal=not args.no_wal,
        ) as store:
            return store.migrate(backup_name=args.backup_name), None
    handler = HANDLERS.get(command)
    if handler is None:
        raise StorageRefusal("unsupported_command", "command is unsupported")
    with _open(args) as store:
        return handler(store, args)


def main(argv: Optional[list[str]] = None, *, output: Optional[BinaryIO] = None) -> int:
    args = _parser().parse_args(argv)
    command = _command_name(args)
    sink = output or sys.stdout.buffer
    try:
        result, raw = _run(args)
    except StorageRefusal as exc:
        sink.write(_envelope_bytes(command, error=exc))
        sink.flush()
        return 3 if exc.retryable else 2
    except (OSError, sqlite3.DatabaseError, UnicodeError, ValueError, TypeError):
        refusal = StorageRefusal(
            "operation_failed", "command could not complete due to a bounded operational failure"
        )
        sink.write(_envelope_bytes(command, error=refusal))
        sink.flush()
        return 2
    except Exception:
        refusal = StorageRefusal(
            "internal_error", "command could not complete due to an unexpected internal failure"
        )
        sink.write(_envelope_bytes(command, error=refusal))
        sink.flush()
        return 2
    sink.write(raw if raw is not None else _envelope_bytes(command, result=result))
    sink.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
