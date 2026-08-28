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
from .importer import build_import_plan
from .adapters import builtin_contract_registry
from .cleanup import CleanupPlanner
from .routing import ModelRouter, load_routing_config
from .sqlite_store import DEFAULT_BUSY_TIMEOUT_MS, MAX_EXPORT_RECORDS, SQLiteStorage
from .storage import Storage, StorageRefusal


COMMAND_SCHEMA = "league.command.v1"
MAX_JSON_INPUT_BYTES = 1_000_000
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


def _add_runtime_commands(groups: argparse._SubParsersAction) -> None:
    runtime = groups.add_parser("runtime", help="Inspect registered harness/backend capabilities.")
    commands = runtime.add_subparsers(dest="action", required=True)
    commands.add_parser("matrix", help="Report supported, unsupported, and unverified adapter operations.")


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="league",
        description="Operate League's canonical store through stable domain commands; SQL is not exposed.",
    )
    parser.add_argument("--version", action="version", version=f"league {__version__}")
    parser.add_argument(
        "--state-root",
        type=Path,
        help="Explicit absolute League state root (the database filename remains internal).",
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
        _add_routing_commands,
        _add_resource_commands,
        _add_cleanup_commands,
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
        raise StorageRefusal("state_root_required", "this command requires --state-root")
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
    "project.resolve": _project_resolve,
    "task.transfer-owner": _task_transfer,
    "runtime.matrix": _runtime_matrix,
    "routing.choose": _routing_choose,
    "routing.escalate": _routing_escalate,
    "routing.outcome": _routing_outcome,
    "resource.register": _resource_register,
    "cleanup.plan": _cleanup_plan,
    "cleanup.status": _cleanup_status,
}


def _run(args: argparse.Namespace) -> CommandResult:
    command = _command_name(args)
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
    if command == "storage.migrate":
        if args.state_root is None:
            raise StorageRefusal("state_root_required", "storage migrate requires --state-root")
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
