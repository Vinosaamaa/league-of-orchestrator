"""Stable ``league`` command facade over the composite storage interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import MAX_ACCEPTANCE_SENTINEL_PATHS, __version__
from .adapters import builtin_contract_registry
from .artifacts import ArtifactLifecycle
from .cleanup import CleanupExecutor, CleanupFaultEvent, CleanupPlanner
from .continuation import (
    ContinuationIssueReopener,
    GitHubIssueAdapter,
    continuation_resume_thread,
    verified_binding,
)
from .importer import build_import_plan
from .orchestration import OrchestrationSignals
from .routing import ModelRouter, load_routing_config
from .reporting import REPORT_FORMATS, render_report
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
    MAX_TRIAGE_TURN_BYTES,
    MAX_TRIAGE_TURN_PROMPTS,
    AnswerRequestCommand,
    ReconcileDuplicateRequestCommand,
    RequestProgressCommand,
    RequestResultCommand,
    TurnDispatchPlan,
)
from .storage_assignment import FinishHiddenAssignmentCommand
from .storage_mode import PROTECTED_GATE_ACTIONS
from .protected_gate import ProtectedGateExecutor
from .request_services import AssignmentSpec
from .shotcaller_bootstrap import (
    HerdrShotcallerBootstrapAdapter,
    ShotcallerBootstrapOptions,
    ShotcallerBootstrapService,
    ShotcallerBootstrapSpec,
)
from .rollover_descendant import (
    HerdrDescendantRuntimeAdapter,
    RolloverDescendantService,
)
from .rollover_snapshot import (
    HerdrRolloverSnapshotAdapter,
    RolloverSnapshotRefreshService,
)
from .visible_launch import (
    HerdrCodexLaunchAdapter,
    SubprocessRunner,
    VisibleChampionLaunchService,
    VisibleLaunchOptions,
    derived_assignment_id,
    derived_champion_agent_id,
    derive_task_label,
)
from .legacy_display_reconciliation import (
    HerdrLegacyDisplayAdapter,
    LegacyDisplayReconciliationService,
    LegacyDisplayReconciliationSpec,
)
from .issue_first import (
    MAX_ISSUE_BODY_BYTES,
    GitHubIssueSelectionService,
    GitHubIssueVerifier,
    IssueSelectionSpec,
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


class _ProvidedClock:
    def __init__(self, at: str) -> None:
        self.at = at

    def now(self) -> str:
        return self.at


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


def _turn_prompt_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("turn prompt limit must be an integer") from exc
    if not 1 <= parsed <= MAX_TRIAGE_TURN_PROMPTS:
        raise argparse.ArgumentTypeError(
            f"turn prompt limit must be between 1 and {MAX_TRIAGE_TURN_PROMPTS}"
        )
    return parsed


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
    preflight = commands.add_parser(
        "preflight",
        help="Run the complete no-apply pre-cutover gate from an explicit plan.",
    )
    preflight.add_argument("--temporary-root", type=Path, required=True)
    preflight.add_argument("--namespace", required=True)
    preflight.add_argument("--plan", type=Path, required=True)
    preflight.add_argument(
        "--sentinel-path",
        type=Path,
        action=_BoundedSentinelPath,
        required=True,
    )
    preflight.add_argument("--config-sentinel", type=Path, required=True)
    preflight.add_argument("--process-sentinel", type=Path, required=True)
    cutover = commands.add_parser(
        "cutover", help="Apply one exact authorized issue-23 live cutover atomically."
    )
    cutover.add_argument("--temporary-root", type=Path, required=True)
    cutover.add_argument("--namespace", required=True)
    cutover.add_argument("--plan", type=Path, required=True)
    cutover.add_argument("--authority-receipt", type=Path, required=True)
    cutover.add_argument("--authority-digest", required=True)
    cutover.add_argument("--source-root", type=Path, required=True)
    archive_verify = commands.add_parser(
        "archive-verify", help="Verify one immutable legacy-system archive."
    )
    archive_verify.add_argument("--archive", type=Path, required=True)
    cleanup_canary = commands.add_parser(
        "cleanup-canary",
        help=(
            "Run one real disposable Herdr/Codex Champion through terminal transition, "
            "interrupted cleanup, restart, and Stop clearance."
        ),
    )
    cleanup_canary.add_argument("--temporary-root", type=Path, required=True)
    cleanup_canary.add_argument("--namespace", required=True)
    cleanup_canary.add_argument("--source-root", type=Path, required=True)


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
    callsign = groups.add_parser(
        "callsign", help="Reconcile and advance the durable shuffled callsign queue."
    )
    commands = callsign.add_subparsers(dest="action", required=True)
    reconcile = commands.add_parser(
        "reconcile", help="Reconcile one explicit catalog without reordering existing entries."
    )
    reconcile.add_argument("--role", choices=("shotcaller", "champion", "hidden-worker"), required=True)
    reconcile.add_argument("--expected-queue-version", type=int, required=True)
    reconcile.add_argument("--seed", required=True)
    reconcile.add_argument("--shuffle-version", type=int, required=True)
    reconcile.add_argument("--catalog", type=Path, required=True)
    reconcile.add_argument("--at", required=True)
    allocate = commands.add_parser(
        "allocate", help="Reserve the first compatible eligible callsign from the queue front."
    )
    for name in ("assignment-id", "agent-id", "scope-id", "at"):
        allocate.add_argument(f"--{name}", required=True)
    allocate.add_argument("--role", choices=("shotcaller", "champion", "hidden-worker"), required=True)
    allocate.add_argument("--scope-kind", choices=("squad", "task", "worker"), required=True)
    allocate.add_argument("--requires", action="append", default=[])
    activate = commands.add_parser(
        "activate", help="Activate a reservation from one exact verified runtime receipt."
    )
    activate.add_argument("--assignment-id", required=True)
    activate.add_argument("--expected-version", type=int, required=True)
    activate.add_argument("--receipt", type=Path, required=True)
    activate.add_argument("--at", required=True)
    rollback = commands.add_parser(
        "rollback", help="Idempotently restore a failed reservation to its original position."
    )
    rollback.add_argument("--assignment-id", required=True)
    rollback.add_argument("--expected-version", type=int, required=True)
    rollback.add_argument("--failure-receipt-digest", required=True)
    rollback.add_argument("--at", required=True)
    release = commands.add_parser(
        "release", help="Release a cleaned active callsign to the queue tail."
    )
    release.add_argument("--assignment-id", required=True)
    release.add_argument("--expected-version", type=int, required=True)
    release.add_argument("--release-receipt-digest", required=True)
    release.add_argument("--at", required=True)
    _add_mode_gate_options(release)
    status = commands.add_parser("status", help="Read one role queue without private runtime data.")
    status.add_argument("--role", choices=("shotcaller", "champion", "hidden-worker"), required=True)


def _add_mode_gate_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode-action",
        type=Path,
        help="Consume and settle one exact already-authorized autonomous action.",
    )
    parser.add_argument("--expected-mode-goal-version", type=int)


def _add_shotcaller_commands(groups: argparse._SubParsersAction) -> None:
    shotcaller = groups.add_parser(
        "shotcaller", help="Create one canonical Shotcaller in the exact calling Codex pane."
    )
    commands = shotcaller.add_subparsers(dest="action", required=True)
    create = commands.add_parser(
        "create",
        help="Allocate and bind the calling unnamed Codex thread without creating Herdr layout.",
    )
    for name in (
        "callsign-assignment-id",
        "agent-id",
        "runtime-instance-id",
        "thread-id",
        "at",
    ):
        create.add_argument(f"--{name}", required=True)
    create.add_argument("--capability", action="append", default=[])
    _add_mode_gate_options(create)


def _add_rollover_commands(groups: argparse._SubParsersAction) -> None:
    rollover = groups.add_parser(
        "rollover", help="Guard one disposable Shotcaller replacement for a stable Squad."
    )
    commands = rollover.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser(
        "prepare", help="Freeze the durable plan and bounded active-Champion snapshot."
    )
    for name in (
        "operation-id", "squad-id", "predecessor-agent-id", "successor-agent-id",
        "callsign-assignment-id", "at",
    ):
        prepare.add_argument(f"--{name}", required=True)
    prepare.add_argument("--expected-owner-version", type=int, required=True)
    prepare.add_argument("--expected-owner-fence", type=int, required=True)
    prepare.add_argument("--authority-kind", choices=("explicit", "automatic"))
    prepare.add_argument("--authority-digest")
    prepare.add_argument("--requires", action="append", default=[])
    prepare.add_argument("--plan", type=Path, required=True)
    _add_mode_gate_options(prepare)
    bindings = commands.add_parser(
        "bindings", help="Read one immutable bounded snapshot page by opaque cursor."
    )
    bindings.add_argument("--operation-id", required=True)
    bindings.add_argument("--cursor")
    bindings.add_argument(
        "--limit",
        type=int,
        help="Page size; defaults to the immutable snapshot page bound.",
    )
    bindings.add_argument("--at", required=True)
    refresh_bindings = commands.add_parser(
        "refresh-bindings",
        help="Replace one expired switched-rollover snapshot after exact live verification.",
    )
    for name in (
        "operation-id",
        "refresh-id",
        "squad-id",
        "predecessor-agent-id",
        "successor-agent-id",
        "expected-snapshot-digest",
        "expires-at",
        "at",
    ):
        refresh_bindings.add_argument(f"--{name}", required=True)
    refresh_bindings.add_argument(
        "--expected-rollover-version", type=int, required=True
    )
    refresh_bindings.add_argument(
        "--expected-snapshot-version", type=int, required=True
    )
    _add_mode_gate_options(refresh_bindings)
    acknowledge = commands.add_parser(
        "acknowledge", help="Acknowledge exact successor identity, capability, and snapshot coverage."
    )
    for name in (
        "operation-id", "successor-agent-id", "runtime-instance-id", "handoff-digest",
        "snapshot-digest", "at",
    ):
        acknowledge.add_argument(f"--{name}", required=True)
    acknowledge.add_argument("--snapshot-version", type=int, required=True)
    acknowledge.add_argument("--snapshot-count", type=int, required=True)
    acknowledge.add_argument("--pages", type=Path, required=True)
    commit = commands.add_parser(
        "commit", help="Atomically CAS the Squad owner and emit one owner-changed outbox event."
    )
    for name in ("operation-id", "owner-event-id", "owner-outbox-id", "at"):
        commit.add_argument(f"--{name}", required=True)
    commit.add_argument("--expected-owner-version", type=int, required=True)
    commit.add_argument("--expected-owner-fence", type=int, required=True)
    _add_mode_gate_options(commit)
    reconcile_descendant = commands.add_parser(
        "reconcile-descendant",
        help="Bind one exact frozen imported Champion to the committed successor.",
    )
    for name in (
        "operation-id",
        "reconciliation-id",
        "champion-agent-id",
        "task-id",
        "runtime-instance-id",
        "snapshot-digest",
        "snapshot-row-digest",
        "at",
    ):
        reconcile_descendant.add_argument(f"--{name}", required=True)
    reconcile_descendant.add_argument("--expected-rollover-version", type=int, required=True)
    reconcile_descendant.add_argument("--expected-agent-version", type=int, required=True)
    reconcile_descendant.add_argument("--expected-task-version", type=int, required=True)
    reconcile_descendant.add_argument("--expected-assignment-version", type=int, required=True)
    reconcile_descendant.add_argument(
        "--expected-callsign-assignment-version", type=int, required=True
    )
    reconcile_descendant.add_argument(
        "--pending-outbox-id",
        action="append",
        default=[],
        help="Declare one exact pending descendant outbox still targeting the predecessor.",
    )
    _add_mode_gate_options(reconcile_descendant)
    reconcile_intake = commands.add_parser(
        "reconcile-intake",
        help="Rebind one exact predecessor-owned unresolved intake plan to the committed successor.",
    )
    for name in ("operation-id", "reconciliation-id", "snapshot-digest", "at"):
        reconcile_intake.add_argument(f"--{name}", required=True)
    reconcile_intake.add_argument("--expected-rollover-version", type=int, required=True)
    reconcile_intake.add_argument("--plan", type=Path, required=True)
    _add_mode_gate_options(reconcile_intake)
    intake_plan = commands.add_parser(
        "intake-plan",
        help="Read the next exact bounded predecessor-intake reconciliation page.",
    )
    intake_plan.add_argument("--operation-id", required=True)
    intake_plan.add_argument("--snapshot-digest", required=True)
    intake_plan.add_argument("--expected-rollover-version", type=int, required=True)
    abort = commands.add_parser("abort", help="Abort only before the owner switch.")
    drain = commands.add_parser("drain", help="Complete old-owner cleanup after the switch.")
    for command in (abort, drain):
        command.add_argument("--operation-id", required=True)
        command.add_argument("--expected-version", type=int, required=True)
        command.add_argument("--cleanup-receipt", type=Path, required=True)
        command.add_argument("--at", required=True)
    _add_mode_gate_options(drain)
    status = commands.add_parser("status", help="Read durable rollover state and public digests.")
    status.add_argument("--operation-id", required=True)


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
    project = groups.add_parser("project", help="Maintain and inspect the advisory project catalog.")
    commands = project.add_subparsers(dest="action", required=True)
    put = commands.add_parser("put", help="Create or update one exact catalog entry with CAS.")
    for name in ("project-id", "summary", "repository", "root", "at"):
        put.add_argument(f"--{name}", required=True)
    put.add_argument("--expected-version", type=int, required=True)
    put.add_argument("--code")
    put.add_argument("--alias", action="append", default=[])
    put.add_argument("--state", choices=("active", "retired"), default="active")
    put.add_argument(
        "--repository-visibility", choices=("public", "private", "unknown"), required=True
    )
    put.add_argument(
        "--export-policy", choices=("deny", "metadata_only", "public_repository"), required=True
    )
    resolve = commands.add_parser("resolve", help="Resolve one exact canonical identity.")
    selectors = resolve.add_mutually_exclusive_group(required=True)
    for name in ("repository", "project-id", "root", "code", "alias"):
        selectors.add_argument(f"--{name}")
    resolve.add_argument("--visibility", choices=("local", "outbound"), default="local")
    listing = commands.add_parser("list", help="List a deterministic bounded catalog page.")
    listing.add_argument("--visibility", choices=("local", "outbound"), default="local")
    listing.add_argument("--limit", type=int, default=200)
    suggestions = commands.add_parser(
        "suggest-squads", help="Replace advisory Squads without moving any work."
    )
    suggestions.add_argument("--project-id", required=True)
    suggestions.add_argument("--expected-version", type=int, required=True)
    suggestions.add_argument("--squad-id", action="append", default=[])
    suggestions.add_argument("--at", required=True)
    advice = commands.add_parser(
        "advise", help="Report explicit routing separately from advisory Squads."
    )
    advice.add_argument("--project-id", required=True)
    advice.add_argument("--explicit-squad-id")
    advice.add_argument("--visibility", choices=("local", "outbound"), default="local")


def _add_roster_commands(groups: argparse._SubParsersAction) -> None:
    roster = groups.add_parser("roster", help="Read one bounded project-grouped Roster snapshot.")
    commands = roster.add_subparsers(dest="action", required=True)
    snapshot = commands.add_parser("snapshot", help="Read needs-action, recent, underway, and unresolved work.")
    snapshot.add_argument("--as-of", required=True)
    snapshot.add_argument("--recent-since", required=True)
    snapshot.add_argument("--stale-before", required=True)
    snapshot.add_argument("--limit", type=int, default=500)
    snapshot.add_argument("--visibility", choices=("local", "outbound"), default="outbound")


def _add_evidence_commands(groups: argparse._SubParsersAction) -> None:
    evidence = groups.add_parser(
        "evidence", help="Record bounded outbound-safe evidence while retaining local hashes."
    )
    commands = evidence.add_subparsers(dest="action", required=True)
    record = commands.add_parser("record", help="Record one versioned activity-evidence object.")
    record.add_argument("--input", type=Path, required=True)


def _add_artifact_commands(groups: argparse._SubParsersAction) -> None:
    artifact = groups.add_parser(
        "artifact", help="Declare and prove merged repository-owned artifacts."
    )
    commands = artifact.add_subparsers(dest="action", required=True)
    declare = commands.add_parser("declare", help="Declare one expected repository artifact.")
    declare.add_argument("--input", type=Path, required=True)
    declare.add_argument("--at", required=True)
    publish = commands.add_parser("publish", help="Record one exact merged publication receipt.")
    publish.add_argument("--artifact-id", required=True)
    publish.add_argument("--expected-version", type=int, required=True)
    publish.add_argument("--receipt", type=Path, required=True)
    publish.add_argument("--at", required=True)
    status = commands.add_parser("status", help="Read repository artifacts for one task.")
    status.add_argument("--task-id", required=True)


def _add_report_options(parser: argparse.ArgumentParser, *, show: bool) -> None:
    if show:
        parser.add_argument("report_id")
    else:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--today", action="store_true")
        mode.add_argument("--since-report")
        parser.add_argument("--from", dest="from_at")
        parser.add_argument("--to", dest="to_at")
        parser.add_argument("--timezone")
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument("--owner")
        scope.add_argument("--squad")
        scope.add_argument("--project")
        scope.add_argument("--all", action="store_true")
    parser.add_argument("--format", choices=tuple(sorted(REPORT_FORMATS)), default="json")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--cursor")
    parser.add_argument("--local-diagnostic", action="store_true")


def _add_report_commands(groups: argparse._SubParsersAction) -> None:
    report = groups.add_parser(
        "report", help="Generate or reproduce a deterministic bounded evidence report."
    )
    report.set_defaults(action="generate")
    _add_report_options(report, show=False)
    commands = report.add_subparsers(dest="report_action")
    show = commands.add_parser("show", help="Reproduce one immutable stored report specification.")
    show.set_defaults(action="show")
    _add_report_options(show, show=True)


def _add_squad_commands(groups: argparse._SubParsersAction) -> None:
    squad = groups.add_parser(
        "squad", help="Register, accept, or inspect a stable Squad without overwriting an active owner."
    )
    commands = squad.add_subparsers(dest="action", required=True)
    register = commands.add_parser(
        "register", help="Offer one pending Squad registration; routing remains inactive."
    )
    for name in (
        "registration-id",
        "squad-id",
        "requester-agent-id",
        "shotcaller-agent-id",
        "runtime-instance-id",
        "expires-at",
        "event-id",
        "outbox-id",
        "at",
    ):
        register.add_argument(f"--{name}", required=True)
    register.add_argument("--project-id", action="append", default=[])
    register.add_argument("--capability", action="append", default=[])
    _add_mode_gate_options(register)
    accept = commands.add_parser(
        "accept", help="Accept or reject from the exact offered live Shotcaller runtime."
    )
    for name in (
        "registration-id",
        "shotcaller-agent-id",
        "runtime-instance-id",
        "event-id",
        "outbox-id",
        "at",
    ):
        accept.add_argument(f"--{name}", required=True)
    accept.add_argument("--decision", choices=("accept", "reject"), required=True)
    _add_mode_gate_options(accept)
    status = commands.add_parser("status", help="Inspect one registration or active stable Squad.")
    selector = status.add_mutually_exclusive_group(required=True)
    selector.add_argument("--registration-id")
    selector.add_argument("--squad-id")
    status.add_argument("--at", required=True)


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
    transition.add_argument(
        "--attention-required",
        action="store_true",
        help="Wake the Shotcaller even when Calm mode would silence this transition.",
    )


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
    choose.add_argument("--signals", type=Path, required=True)
    choose.add_argument("--provider")
    choose.add_argument("--model")
    choose.add_argument("--effort")
    choose.add_argument("--required-capability", action="append", default=[])
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
    outcome.add_argument("--cost-microunits", type=int)
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
    execute = commands.add_parser(
        "execute",
        help="Execute one canonical SQLite cleanup plan through supported production adapters.",
    )
    execute.add_argument("--operation-id", required=True)
    execute.add_argument("--expected-fence", type=int, required=True)
    execute.add_argument("--executor-id", required=True)
    execute.add_argument("--leased-until", required=True)
    execute.add_argument("--at", required=True)
    _add_mode_gate_options(execute)
    reconcile = commands.add_parser(
        "reconcile",
        help="Plan and automatically execute one exact disposable-canary cleanup.",
    )
    reconcile.add_argument("--manifest", type=Path, required=True)
    reconcile.add_argument("--operation-id", required=True)
    reconcile.add_argument("--adapter-config", type=Path, required=True)
    reconcile.add_argument("--executor-id", required=True)
    reconcile.add_argument("--leased-until", required=True)
    reconcile.add_argument("--at", required=True)
    reconcile.add_argument(
        "--simulate-interruption-after-archive",
        action="store_true",
        help="Disposable-canary-only crash injection after the archive external effect.",
    )
    _add_mode_gate_options(reconcile)


def _add_continuation_commands(groups: argparse._SubParsersAction) -> None:
    continuation = groups.add_parser(
        "continuation",
        help="Claim one archived provider thread and reopen its exact owning issue.",
    )
    commands = continuation.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser(
        "prepare",
        help="Verify a new worktree and exclusively claim one exact archived thread.",
    )
    for name in (
        "operation-id",
        "archive-id",
        "assignment-id",
        "new-task-id",
        "new-agent-id",
        "repository",
        "branch",
        "worktree",
        "instruction-digest",
        "concrete-benefit",
        "at",
    ):
        prepare.add_argument(f"--{name}", required=True)
    prepare.add_argument("--issue", type=int, required=True)
    prepare.add_argument("--expected-archive-version", type=int, required=True)
    prepare.add_argument("--reconciliation-digest")
    status = commands.add_parser("status", help="Read one exact continuation operation.")
    status.add_argument("--operation-id", required=True)
    reopen = commands.add_parser(
        "reopen",
        help="Reopen the exact archived owning issue under a recoverable fence.",
    )
    for name in ("operation-id", "executor-id", "leased-until", "at"):
        reopen.add_argument(f"--{name}", required=True)
    reopen.add_argument("--expected-version", type=int, required=True)
    reopen.add_argument("--expected-fence", type=int, required=True)


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
    turn = commands.add_parser(
        "turn",
        help=(
            "Emit one bounded exact intake, read one model-authored decision batch from stdin, "
            "and commit it atomically on the same SQLite connection."
        ),
    )
    turn.add_argument("--owner-agent-id", required=True)
    turn.add_argument("--at", help=argparse.SUPPRESS)
    turn.add_argument("--limit", type=_turn_prompt_limit, default=20)
    turn.add_argument("--max-bytes", type=int, default=1_000_000)
    turn.add_argument("--candidate-limit", type=int, default=12)
    turn.add_argument("--candidate-max-bytes", type=int, default=24_576)
    reconcile_duplicate = commands.add_parser(
        "reconcile-duplicate",
        help="Supersede one same-owner duplicate request under exact request versions.",
    )
    for name in (
        "duplicate-request-id",
        "canonical-request-id",
        "owner-agent-id",
        "at",
    ):
        reconcile_duplicate.add_argument(f"--{name}", required=True)
    reconcile_duplicate.add_argument("--expected-duplicate-version", type=int, required=True)
    reconcile_duplicate.add_argument("--expected-canonical-version", type=int, required=True)
    bind_prompt = commands.add_parser(
        "bind-prompt", help="Bind one quarantined prompt to an exact verified runtime."
    )
    for name in ("prompt-id", "intake-actor-id", "runtime-instance-id", "at"):
        bind_prompt.add_argument(f"--{name}", required=True)
    claim = commands.add_parser("claim", help="Acquire or recover one request mutation claim.")
    for name in ("request-id", "runtime-instance-id", "claim-token", "leased-until", "at"):
        claim.add_argument(f"--{name}", required=True)
    release = commands.add_parser("release", help="Release the exact current request claim.")
    for name in ("request-id", "runtime-instance-id", "claim-token", "at"):
        release.add_argument(f"--{name}", required=True)
    dispatch = commands.add_parser(
        "dispatch",
        help=(
            "Record Shotcaller direct, bounded hidden scientist, or local Champion execution; "
            "Squad ownership uses request route."
        ),
        description=(
            "Choose exactly one: Shotcaller direct, recorded hidden scientist, local visible "
            "Champion, or acknowledgement-gated Squad route. Squad is ownership, not execution."
        ),
    )
    for name in ("request-id", "claim-token", "dispatch-id", "work-kind", "at"):
        dispatch.add_argument(f"--{name}", required=True)
    dispatch.add_argument(
        "--requested-mode",
        choices=("direct", "hidden", "champion", "squad"),
        help=(
            "direct=Shotcaller direct; hidden=recorded scientist; champion=local visible "
            "Champion; squad=durable Squad ownership route"
        ),
    )
    dispatch.add_argument("--hidden-supported", action="store_true")
    dispatch.add_argument("--requested-model")
    dispatch.add_argument("--requested-effort")
    dispatch.add_argument("--explicit-route")
    dispatch.add_argument("--pre-bounded", action="store_true")
    dispatch.add_argument("--read-only", action="store_true")
    dispatch.add_argument("--answer-or-routing-only", action="store_true")
    dispatch.add_argument("--expected-minutes", type=int, default=0)
    dispatch.add_argument("--expected-task-action-calls", type=int, default=0)
    for name in (
        "creates-artifact",
        "mutates-state",
        "reproduces-issue",
        "runs-tests",
        "runs-benchmark",
        "uses-browser-or-computer",
        "project-implementation",
    ):
        dispatch.add_argument(f"--{name}", action="store_true")
    dispatch.add_argument("--continuation-role", choices=("champion", "shotcaller"))
    dispatch.add_argument("--continuation-target")
    dispatch.add_argument("--project-suggested-shotcaller")
    dispatch.add_argument("--hidden-subtask")
    dispatch.add_argument("--hidden-scope-budget")
    decide_route = commands.add_parser(
        "decide-route", help="Resolve local direct/Champion work or one deterministic Squad route."
    )
    decide_route.add_argument("--signals", type=Path, required=True)
    decide_route.add_argument("--project-id", action="append", default=[])
    decide_route.add_argument("--explicit-squad-id")
    decide_route.add_argument("--continuation-squad-id")
    decide_route.add_argument("--required-capability", action="append", default=[])
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
    route.add_argument("--recipient-squad-id", required=True)
    route.add_argument(
        "--route-reason-code",
        choices=("explicit_squad", "continuation_squad", "unique_strong_squad"),
        required=True,
    )
    route.add_argument("--route-policy-version", required=True)
    route.add_argument("--route-confidence", choices=("explicit", "continuation", "strong"), required=True)
    route.add_argument("--required-capability", action="append", default=[])
    accept = commands.add_parser(
        "accept", help="Claim and accept one exactly received routed request."
    )
    for name in ("request-id", "runtime-instance-id", "claim-token", "leased-until", "at"):
        accept.add_argument(f"--{name}", required=True)
    progress = commands.add_parser(
        "progress", help="Emit immediate requester progress or coalesce a changed routine aggregate."
    )
    for name in (
        "progress-id",
        "request-id",
        "claim-token",
        "reason-code",
        "current-phase",
        "blocker-severity",
        "next-action",
        "event-id",
        "outbox-id",
        "at",
    ):
        progress.add_argument(f"--{name}", required=True)
    for name in ("expected-version", "progress-generation", "settled-count", "total-count", "blocker-count"):
        progress.add_argument(f"--{name}", type=int, required=True)
    progress.add_argument("--user-action-required", action="store_true")
    progress.add_argument("--deadline-change")
    progress.add_argument("--minimum-interval-seconds", type=int, default=900)
    progress.add_argument("--grace-seconds", type=int, default=300)
    progress.add_argument("--promised-checkpoint-at")
    reconcile_progress = commands.add_parser(
        "reconcile-progress", help="Create truthful due work and one post-grace stalled notification."
    )
    reconcile_progress.add_argument("--owner-agent-id", required=True)
    reconcile_progress.add_argument("--at", required=True)
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
    untriaged = commands.add_parser(
        "untriaged",
        help="Read exact retained untriaged prompt bodies for one live Shotcaller turn.",
    )
    untriaged.add_argument("--owner-agent-id", required=True)
    untriaged.add_argument("--limit", type=int, default=20)
    untriaged.add_argument("--max-bytes", type=int, default=1_000_000)
    untriaged.add_argument("--candidate-limit", type=int, default=12)
    untriaged.add_argument("--candidate-max-bytes", type=int, default=24_576)
    untriaged.add_argument("--candidate-page", action="store_true")
    untriaged.add_argument("--candidate-after")


def _add_assignment_commands(groups: argparse._SubParsersAction) -> None:
    assignment = groups.add_parser(
        "assign", help="Drive recoverable pending, launching, active, blocked, or cleanup-pending assignment state."
    )
    commands = assignment.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser(
        "prepare",
        help="Reserve one hidden-scientist assignment; visible Champions must use assign run.",
    )
    for name in (
        "assignment-id",
        "request-id",
        "claim-token",
        "task-id",
        "task-summary",
        "coordinator-agent-id",
        "at",
    ):
        prepare.add_argument(f"--{name}", required=True)
    prepare.add_argument(
        "--assignee-agent-id",
        "--champion-agent-id",
        dest="champion_agent_id",
        required=True,
        help="Exact Champion or hidden-worker assignee; the role flag controls validation.",
    )
    prepare.add_argument("--role", choices=("champion", "hidden-worker"), default="champion")
    prepare.add_argument("--repository", default="")
    prepare.add_argument("--issue", type=int, default=0)
    prepare.add_argument("--branch", default="")
    prepare.add_argument("--worktree", default="")
    prepare.add_argument("--dispatch-id")
    prepare.add_argument("--promoted-from-assignment-id")
    prepare.add_argument("--requires", action="append", default=[])
    launch = commands.add_parser(
        "run",
        help="Reserve, start, verify, activate, and brief one visible Herdr/Codex Champion.",
    )
    for name in (
        "request-id",
        "claim-token",
        "task-id",
        "task-summary",
        "coordinator-agent-id",
        "repository",
        "branch",
        "worktree",
        "model",
        "effort",
    ):
        launch.add_argument(f"--{name}", required=True)
    launch.add_argument("--task-label")
    launch.add_argument("--issue", type=int, required=True)
    launch.add_argument("--assignment-id")
    launch.add_argument("--champion-agent-id")
    launch.add_argument("--workspace-id")
    launch.add_argument("--league-command")
    launch.add_argument("--requires", action="append", default=[])
    launch.add_argument("--startup-timeout-ms", type=int, default=120_000)
    launch.add_argument("--issue-selection-receipt-digest", required=True)
    launching = commands.add_parser("launching", help="Commit launch intent before adapter work.")
    launching.add_argument("--assignment-id", required=True)
    launching.add_argument("--expected-version", type=int, required=True)
    launching.add_argument("--at", required=True)
    activate = commands.add_parser(
        "activate", help="Activate only from an exact verified role-specific assignment receipt."
    )
    activate.add_argument("--assignment-id", required=True)
    activate.add_argument("--expected-version", type=int, required=True)
    activate.add_argument("--receipt-json", required=True)
    activate.add_argument("--event-id", required=True)
    activate.add_argument("--outbox-id", required=True)
    activate.add_argument("--at", required=True)
    reconcile_runtime = commands.add_parser(
        "reconcile-runtime",
        help="Fence one exact stale active assignment runtime without emitting progress.",
    )
    reconcile_runtime.add_argument("--assignment-id", required=True)
    reconcile_runtime.add_argument("--at", required=True)
    _add_mode_gate_options(reconcile_runtime)
    reconcile_display = commands.add_parser(
        "reconcile-legacy-display",
        help="Owner-authorized CAS-safe repair of one exact pre-fix active Champion display.",
    )
    for name in (
        "assignment-id",
        "champion-agent-id",
        "runtime-instance-id",
        "callsign",
        "pane-id",
        "terminal-id",
        "thread-id",
        "worktree",
        "routing-name",
        "target-task-label",
        "at",
    ):
        reconcile_display.add_argument(f"--{name}", required=True)
    reconcile_display.add_argument("--expected-version", type=int, required=True)
    reconcile_display.add_argument("--expected-presentation-json", required=True)
    reconcile_display.add_argument("--owner-authorized", action="store_true")
    _add_mode_gate_options(reconcile_display)
    block = commands.add_parser("block", help="Record a blocked or cleanup-pending failed launch.")
    block.add_argument("--assignment-id", required=True)
    block.add_argument("--expected-version", type=int, required=True)
    block.add_argument("--failure-class", required=True)
    block.add_argument("--cleanup-required", action="store_true")
    block.add_argument("--cleanup-proven", action="store_true")
    finish_hidden = commands.add_parser(
        "finish-hidden",
        help="Deliver one cleanup-gated hidden scientist terminal result; no routine progress is allowed.",
    )
    for name in (
        "assignment-id",
        "runtime-instance-id",
        "result-summary",
        "cleanup-receipt",
        "unpublished-state-receipt",
        "transition-id",
        "transition-key",
        "event-id",
        "outbox-id",
        "at",
    ):
        finish_hidden.add_argument(f"--{name}", required=True)
    finish_hidden.add_argument("--expected-version", type=int, required=True)
    finish_hidden.add_argument(
        "--status",
        choices=("completed", "blocked", "failed", "promotion_required"),
        required=True,
    )
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
    runtime.add_argument(
        "--capability",
        action="append",
        help="Replace declared runtime capabilities on this observation.",
    )
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
    policy = commands.add_parser(
        "set-supervision-policy",
        help="Configure durable all-material or Calm wake delivery without changing hooks.",
    )
    for name in ("scope-id", "actor-agent-id", "at"):
        policy.add_argument(f"--{name}", required=True)
    policy.add_argument("--mode", choices=("all_material", "calm"), required=True)
    policy.add_argument("--unreachable-grace-seconds", type=int, default=60)
    reconcile_silent = commands.add_parser(
        "reconcile-silent",
        help="Return and acknowledge one bounded page of Calm-suppressed transitions.",
    )
    reconcile_silent.add_argument("--actor-agent-id", required=True)
    reconcile_silent.add_argument("--after-event-seq", type=int)
    reconcile_silent.add_argument("--limit", type=int, default=20)
    reconcile_silent.add_argument("--at", required=True)


def _add_mode_commands(groups: argparse._SubParsersAction) -> None:
    mode = groups.add_parser(
        "mode",
        help="Authorize and account for one durable scoped autonomous-delivery goal.",
    )
    commands = mode.add_subparsers(dest="action", required=True)
    authorize = commands.add_parser(
        "authorize", help="Create one immutable Summoner grant revision and bind its exact goal."
    )
    authorize.add_argument("--grant", type=Path, required=True)
    authorize.add_argument("--expected-goal-version", type=int, required=True)
    authorize.add_argument("--at", required=True)
    status = commands.add_parser(
        "status", help="Show manual or autonomous mode, exact authority, usage, and goal state."
    )
    status.add_argument("--goal-id", required=True)
    status.add_argument("--at", required=True)
    use = commands.add_parser(
        "use", help="Authorize and record one exact external action atomically."
    )
    use.add_argument("--action", dest="action_spec", type=Path, required=True)
    use.add_argument("--expected-goal-version", type=int, required=True)
    use.add_argument("--at", required=True)
    settle = commands.add_parser(
        "settle", help="Settle one exact in-progress action or create its repair obligation."
    )
    for name in (
        "action-use-id",
        "goal-id",
        "use-receipt-digest",
        "result-receipt-digest",
        "at",
    ):
        settle.add_argument(f"--{name}", required=True)
    settle.add_argument("--expected-goal-version", type=int, required=True)
    settle.add_argument("--outcome", choices=("succeeded", "failed"), required=True)
    settle.add_argument("--failure-class")
    transition = commands.add_parser(
        "transition", help="Perform one checked non-external delivery-goal transition."
    )
    transition.add_argument("--goal-id", required=True)
    transition.add_argument("--expected-goal-version", type=int, required=True)
    transition.add_argument(
        "--state",
        choices=(
            "awaiting_authority",
            "implementing",
            "ready_to_land",
            "landing",
            "deploying",
            "verifying",
            "repair_pending",
            "delivered",
            "cleanup_pending",
            "cleaned",
        ),
        required=True,
    )
    transition.add_argument("--at", required=True)
    revoke = commands.add_parser(
        "revoke", help="Revoke one grant immediately while retaining in-progress evidence."
    )
    for name in ("grant-id", "revoked-by", "reason", "at"):
        revoke.add_argument(f"--{name}", required=True)
    revoke.add_argument("--expected-goal-version", type=int, required=True)


def _add_issue_commands(groups: argparse._SubParsersAction) -> None:
    issue = groups.add_parser(
        "issue", help="Select, reopen, or create one issue after durable duplicate preflight."
    )
    commands = issue.add_subparsers(dest="action", required=True)
    select = commands.add_parser(
        "select",
        help="Search open and closed issues, then reuse, reopen, or create only distinct scope.",
    )
    for name in (
        "task-id",
        "task-summary",
        "coordinator-agent-id",
        "repository",
        "issue-title",
        "at",
    ):
        select.add_argument(f"--{name}", required=True)
    select.add_argument("--issue-body", type=Path, required=True)
    select.add_argument("--reopen-action-receipt-digest")


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
        help=(
            "During migration, establish rollback-journal mode even when the loaded "
            "runtime passes the WAL gate; normal commands only validate the established mode."
        ),
    )
    groups = parser.add_subparsers(dest="group", required=True)
    for builder in (
        _add_storage_commands,
        _add_agent_commands,
        _add_callsign_commands,
        _add_shotcaller_commands,
        _add_rollover_commands,
        _add_delivery_commands,
        _add_project_commands,
        _add_roster_commands,
        _add_evidence_commands,
        _add_artifact_commands,
        _add_report_commands,
        _add_squad_commands,
        _add_task_commands,
        _add_runtime_commands,
        _add_skill_commands,
        _add_routing_commands,
        _add_resource_commands,
        _add_cleanup_commands,
        _add_continuation_commands,
        _add_request_commands,
        _add_assignment_commands,
        _add_hook_commands,
        _add_mode_commands,
        _add_issue_commands,
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
    transition = store.transition(
        args.agent_id, args.expected_version, args.status, args.update, args.at
    )
    if transition.get("outbox_id"):
        from .canonical_delivery import dispatch_event

        transition["delivery"] = dispatch_event(
            store,
            outbox_id=transition["outbox_id"],
            event_id=transition["event_id"],
            recipient_agent_id=transition["recipient_agent_id"],
            at=args.at,
        )
    return transition, None


def _callsign_reconcile(store: Storage, args: argparse.Namespace) -> CommandResult:
    catalog = _read_json_object(args.catalog)
    if set(catalog) != {"entries"} or not isinstance(catalog["entries"], list):
        raise StorageRefusal("invalid_pool", "callsign catalog must contain one entries array")
    return store.reconcile_callsign_pool(
        args.role,
        args.expected_queue_version,
        args.seed,
        args.shuffle_version,
        catalog["entries"],
        args.at,
    ), None


def _callsign_allocate(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.allocate_callsign(
        args.assignment_id,
        args.agent_id,
        args.role,
        args.scope_kind,
        args.scope_id,
        args.requires,
        args.at,
    ), None


def _callsign_activate(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.activate_callsign(
        args.assignment_id,
        args.expected_version,
        _read_json_object(args.receipt),
        args.at,
    ), None


def _callsign_rollback(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.rollback_callsign(
        args.assignment_id,
        args.expected_version,
        args.failure_receipt_digest,
        args.at,
    ), None


def _callsign_release(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.release_callsign(
        args.assignment_id,
        args.expected_version,
        args.release_receipt_digest,
        args.at,
    ), None


def _callsign_status(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.callsign_status(args.role), None


def _shotcaller_create(store: Storage, args: argparse.Namespace) -> CommandResult:
    spec = ShotcallerBootstrapSpec(
        assignment_id=args.callsign_assignment_id,
        agent_id=args.agent_id,
        runtime_instance_id=args.runtime_instance_id,
        thread_id=args.thread_id,
        capabilities=tuple(args.capability),
    )
    options = ShotcallerBootstrapOptions(
        workspace_id=os.environ.get("HERDR_WORKSPACE_ID", ""),
        tab_id=os.environ.get("HERDR_TAB_ID", ""),
        pane_id=os.environ.get("HERDR_PANE_ID", ""),
        worktree=str(Path.cwd().resolve()),
    )
    class FixedClock:
        def now(self) -> str:
            return args.at

    return ShotcallerBootstrapService(
        store, HerdrShotcallerBootstrapAdapter(options), FixedClock()
    ).bootstrap(spec), None


def _rollover_prepare(store: Storage, args: argparse.Namespace) -> CommandResult:
    if args.authority_kind is None or args.authority_digest is None:
        raise StorageRefusal(
            "rollover_authority_required",
            "rollover preparation requires explicit authority or one exact mode action",
        )
    return store.prepare_rollover(
        args.operation_id,
        args.squad_id,
        args.predecessor_agent_id,
        args.successor_agent_id,
        args.callsign_assignment_id,
        args.expected_owner_version,
        args.expected_owner_fence,
        args.authority_kind,
        args.authority_digest,
        args.requires,
        _read_json_object(args.plan),
        args.at,
    ), None


def _rollover_bindings(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.rollover_bindings(
        args.operation_id, args.at, cursor=args.cursor, limit=args.limit
    ), None


def _rollover_refresh_bindings(
    store: Storage, args: argparse.Namespace
) -> CommandResult:
    return RolloverSnapshotRefreshService(
        store, HerdrRolloverSnapshotAdapter()
    ).refresh(
        operation_id=args.operation_id,
        refresh_id=args.refresh_id,
        squad_id=args.squad_id,
        predecessor_agent_id=args.predecessor_agent_id,
        successor_agent_id=args.successor_agent_id,
        expected_rollover_version=args.expected_rollover_version,
        expected_snapshot_version=args.expected_snapshot_version,
        expected_snapshot_digest=args.expected_snapshot_digest,
        expires_at=args.expires_at,
        at=args.at,
    ), None


def _rollover_acknowledge(store: Storage, args: argparse.Namespace) -> CommandResult:
    pages = _read_json_object(args.pages)
    if set(pages) != {"pages"} or not isinstance(pages["pages"], list):
        raise StorageRefusal("invalid_handoff", "snapshot acknowledgement requires one pages array")
    return store.acknowledge_rollover(
        args.operation_id,
        args.successor_agent_id,
        args.runtime_instance_id,
        args.handoff_digest,
        args.snapshot_version,
        args.snapshot_count,
        args.snapshot_digest,
        pages["pages"],
        args.at,
    ), None


def _rollover_commit(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.commit_rollover(
        args.operation_id,
        args.expected_owner_version,
        args.expected_owner_fence,
        args.owner_event_id,
        args.owner_outbox_id,
        args.at,
    ), None


def _rollover_reconcile_descendant(
    store: Storage, args: argparse.Namespace
) -> CommandResult:
    return RolloverDescendantService(
        store, HerdrDescendantRuntimeAdapter()
    ).reconcile(
        operation_id=args.operation_id,
        reconciliation_id=args.reconciliation_id,
        champion_agent_id=args.champion_agent_id,
        task_id=args.task_id,
        runtime_instance_id=args.runtime_instance_id,
        snapshot_digest=args.snapshot_digest,
        snapshot_row_digest=args.snapshot_row_digest,
        expected_rollover_version=args.expected_rollover_version,
        expected_agent_version=args.expected_agent_version,
        expected_task_version=args.expected_task_version,
        expected_assignment_version=args.expected_assignment_version,
        expected_callsign_assignment_version=args.expected_callsign_assignment_version,
        pending_outbox_ids=tuple(args.pending_outbox_id),
        at=args.at,
    ), None


def _rollover_reconcile_intake(
    store: Storage, args: argparse.Namespace
) -> CommandResult:
    return store.reconcile_rollover_intake(
        args.operation_id,
        args.reconciliation_id,
        args.snapshot_digest,
        args.expected_rollover_version,
        _read_json_object(args.plan),
        args.at,
    ), None


def _rollover_intake_plan(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.rollover_intake_plan(
        args.operation_id,
        args.snapshot_digest,
        args.expected_rollover_version,
    ), None


def _rollover_abort(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.abort_rollover(
        args.operation_id,
        args.expected_version,
        _read_json_object(args.cleanup_receipt),
        args.at,
    ), None


def _rollover_drain(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.complete_rollover_drain(
        args.operation_id,
        args.expected_version,
        _read_json_object(args.cleanup_receipt),
        args.at,
    ), None


def _rollover_status(store: Storage, args: argparse.Namespace) -> CommandResult:
    value = store.rollover_status(args.operation_id)
    return {"found": value is not None, "rollover": value}, None


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
    value = store.resolve_project(
        args.repository,
        project_id=args.project_id,
        root=args.root,
        code=args.code,
        alias=args.alias,
        visibility=args.visibility,
    )
    return {"found": value is not None, "project": value}, None


def _project_put(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.put_project(
        args.project_id,
        expected_version=args.expected_version,
        summary=args.summary,
        repository=args.repository,
        root=args.root,
        code=args.code,
        aliases=args.alias,
        state=args.state,
        repository_visibility=args.repository_visibility,
        export_policy=args.export_policy,
        at=args.at,
    ), None


def _project_list(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.list_projects(visibility=args.visibility, limit=args.limit), None


def _project_suggest_squads(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.set_project_suggestions(
        args.project_id, args.expected_version, args.squad_id, args.at
    ), None


def _project_advise(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.project_advice(
        args.project_id,
        explicit_squad_id=args.explicit_squad_id,
        visibility=args.visibility,
    ), None


def _roster_snapshot(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.roster_snapshot(
        as_of=args.as_of,
        recent_since=args.recent_since,
        stale_before=args.stale_before,
        limit=args.limit,
        visibility=args.visibility,
    ), None


def _evidence_record(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.record_activity_evidence(_read_json_object(args.input)), None


def _scope_args(args: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    for kind in ("owner", "squad", "project"):
        value = getattr(args, kind, None)
        if value is not None:
            return kind, value
    if getattr(args, "all", False):
        return "all", None
    return None, None


def _report_zone(zone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise StorageRefusal("invalid_report_timezone", "report timezone is unknown") from exc


def _now_rfc3339(zone_name: str) -> str:
    return datetime.now(_report_zone(zone_name)).astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _report_generate(store: Storage, args: argparse.Namespace) -> CommandResult:
    scope_kind, scope_id = _scope_args(args)
    timezone_name = args.timezone
    from_at = args.from_at
    to_at = args.to_at
    from_inclusive = True
    event_watermark = None
    if (args.today or args.since_report) and args.from_at:
        raise StorageRefusal("report_range_ambiguous", "report range modes cannot also set from")
    if args.since_report:
        prior = store.report_spec(args.since_report)
        if prior is None:
            raise StorageRefusal("report_not_found", "since-report specification is unknown")
        from_at = prior["to_at"]
        from_inclusive = False
        timezone_name = timezone_name or prior["timezone"]
        if scope_kind is None:
            scope_kind, scope_id = prior["scope_kind"], prior["scope_id"]
        to_at = to_at or _now_rfc3339(timezone_name)
    elif args.today:
        if timezone_name is None:
            raise StorageRefusal("invalid_report_timezone", "today requires an exact timezone")
        observed = datetime.now(_report_zone(timezone_name))
        from_at = observed.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        to_at = to_at or observed.isoformat(timespec="seconds")
    if not from_at or not to_at or not timezone_name:
        raise StorageRefusal(
            "report_range_required", "report requires exact from, to, and timezone values"
        )
    if scope_kind is None:
        raise StorageRefusal("report_scope_required", "report requires one exact scope")
    report = store.generate_report(
        from_at=from_at,
        to_at=to_at,
        timezone_name=timezone_name,
        from_inclusive=from_inclusive,
        scope_kind=scope_kind,
        scope_id=scope_id,
        limit=args.limit,
        cursor=args.cursor,
        local_diagnostic=args.local_diagnostic,
        event_watermark=event_watermark,
    )
    return None, render_report(report, args.format)


def _report_show(store: Storage, args: argparse.Namespace) -> CommandResult:
    spec = store.report_spec(args.report_id)
    if spec is None:
        raise StorageRefusal("report_not_found", "report specification is unknown")
    report = store.generate_report(
        from_at=spec["from_at"],
        to_at=spec["to_at"],
        timezone_name=spec["timezone"],
        from_inclusive=bool(spec["from_inclusive"]),
        scope_kind=spec["scope_kind"],
        scope_id=spec["scope_id"],
        limit=args.limit,
        cursor=args.cursor,
        local_diagnostic=args.local_diagnostic,
        report_id=spec["report_id"],
        event_watermark=int(spec["event_watermark"]),
        source_watermark=spec["source_watermark"],
        persist=False,
        expected_content_hash=spec["content_hash"],
    )
    return None, render_report(report, args.format)


def _task_transfer(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.transfer_task_owner(
        args.task_id, args.expected_version, args.owner_kind, args.owner_id, args.at
    ), None


def _task_transition(store: Storage, args: argparse.Namespace) -> CommandResult:
    transition = store.transition_task(
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
        args.attention_required,
    )
    if transition.get("outbox_id"):
        from .canonical_delivery import dispatch_event

        transition["delivery"] = dispatch_event(
            store,
            outbox_id=transition["outbox_id"],
            event_id=transition["event_id"],
            recipient_agent_id=args.recipient_agent_id,
            at=args.at,
        )
    return transition, None


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


def _read_bounded_text(path: Path, maximum: int, label: str) -> str:
    try:
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
        if not payload or len(payload) > maximum or b"\x00" in payload:
            raise StorageRefusal("input_invalid", f"{label} is empty or exceeds its bound")
        return payload.decode("utf-8")
    except StorageRefusal:
        raise
    except (OSError, UnicodeError) as exc:
        raise StorageRefusal("input_invalid", f"{label} could not be read") from exc


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
        chosen_at=args.at,
        signals=_read_json_object(args.signals),
        required_capabilities=tuple(args.required_capability),
        explicit_provider=args.provider,
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


def _artifact_declare(store: Storage, args: argparse.Namespace) -> CommandResult:
    return ArtifactLifecycle(store).declare(_read_json_object(args.input), args.at), None


def _artifact_publish(store: Storage, args: argparse.Namespace) -> CommandResult:
    return ArtifactLifecycle(store).publish(
        args.artifact_id,
        args.expected_version,
        _read_json_object(args.receipt),
        args.at,
    ), None


def _artifact_status(store: Storage, args: argparse.Namespace) -> CommandResult:
    return ArtifactLifecycle(store).status(args.task_id), None


def _cleanup_plan(store: Storage, args: argparse.Namespace) -> CommandResult:
    return CleanupPlanner(store).plan(
        _read_json_object(args.manifest), operation_id=args.operation_id, at=args.at
    ), None


def _cleanup_status(store: Storage, args: argparse.Namespace) -> CommandResult:
    value = store.cleanup_operation(args.operation_id)
    return {"found": value is not None, "operation": value}, None


def _cleanup_execute(store: Storage, args: argparse.Namespace) -> CommandResult:
    from .production_cleanup import ProductionCleanup

    return ProductionCleanup(store).execute(
        args.operation_id,
        expected_fence=args.expected_fence,
        executor_id=args.executor_id,
        leased_until=args.leased_until,
        at=args.at,
    ), None


def _cleanup_reconcile(store: Storage, args: argparse.Namespace) -> CommandResult:
    from .real_cleanup import canary_cleanup_registry

    manifest = _read_json_object(args.manifest)
    existing = store.cleanup_operation(args.operation_id)
    if existing is not None:
        manifest = {
            **manifest,
            "expected_cleanup_version": int(existing["cleanup_revision"]),
        }
    planned = CleanupPlanner(store).plan(
        manifest, operation_id=args.operation_id, at=args.at
    )
    adapters = canary_cleanup_registry(
        store,
        _read_json_object(args.adapter_config),
        at=args.at,
    )

    def fault(event: CleanupFaultEvent) -> None:
        if (
            args.simulate_interruption_after_archive
            and event.phase == "after_external_action"
            and event.action_kind == "archive_identity_evidence"
        ):
            raise StorageRefusal(
                "cleanup_interrupted",
                "disposable canary cleanup was interrupted after archive",
                retryable=True,
            )

    executed = CleanupExecutor(store, adapters).execute(
        args.operation_id,
        expected_fence=int(planned["fence"]),
        executor_id=args.executor_id,
        leased_until=args.leased_until,
        at=args.at,
        fault=fault,
    )
    return {
        "automatic_after_proof": True,
        "plan": planned,
        "execution": executed,
    }, None


def _continuation_prepare(store: Storage, args: argparse.Namespace) -> CommandResult:
    worktree = str(Path(args.worktree).resolve())
    binding = verified_binding(
        repository=args.repository,
        issue=args.issue,
        branch=args.branch,
        worktree=worktree,
    )
    return store.prepare_continuation(
        {
            "operation_id": args.operation_id,
            "archive_id": args.archive_id,
            "assignment_id": args.assignment_id,
            "new_task_id": args.new_task_id,
            "new_agent_id": args.new_agent_id,
            "repository": args.repository,
            "issue": args.issue,
            "branch": args.branch,
            "worktree": worktree,
            "binding": binding,
            "instruction_digest": args.instruction_digest,
            "reconciliation_digest": args.reconciliation_digest,
            "concrete_benefit": args.concrete_benefit,
            "expected_archive_version": args.expected_archive_version,
            "at": args.at,
        }
    ), None


def _continuation_status(store: Storage, args: argparse.Namespace) -> CommandResult:
    value = store.continuation_status(args.operation_id)
    if value is None:
        raise StorageRefusal("continuation_unknown", "continuation operation does not exist")
    return value, None


def _continuation_reopen(store: Storage, args: argparse.Namespace) -> CommandResult:
    return ContinuationIssueReopener(store, GitHubIssueAdapter()).execute(
        args.operation_id,
        expected_version=args.expected_version,
        expected_fence=args.expected_fence,
        executor_id=args.executor_id,
        leased_until=args.leased_until,
        at=args.at,
    ), None


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


def _read_turn_payload(source: BinaryIO) -> dict[str, Any]:
    encoded = source.readline(MAX_TRIAGE_TURN_BYTES + 2)
    if not encoded:
        raise StorageRefusal(
            "triage_batch_missing", "triage turn ended before the model-authored batch arrived"
        )
    if len(encoded) > MAX_TRIAGE_TURN_BYTES + 1 or (
        len(encoded) == MAX_TRIAGE_TURN_BYTES + 1 and not encoded.endswith(b"\n")
    ):
        raise StorageRefusal(
            "invalid_json", "triage turn decision batch exceeds its encoded bound"
        )
    try:
        value = json.loads(encoded)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise StorageRefusal(
            "invalid_json", "triage turn decisions must be one valid JSON line"
        ) from exc
    if not isinstance(value, dict):
        raise StorageRefusal("invalid_turn_payload", "request turn input must be an object")
    return value


def _turn_begin_payload(
    source: BinaryIO,
    intake: dict[str, Any],
    expected_prompt_ids: tuple[str, ...],
) -> dict[str, Any]:
    payload = (
        _read_turn_payload(source)
        if expected_prompt_ids
        else {
            "candidate_inventory_digest": intake["candidate_inventory"]["digest"],
            "decisions": [],
            "plans": [],
        }
    )
    if set(payload) != {
        "candidate_inventory_digest",
        "decisions",
        "plans",
    } or not isinstance(payload["decisions"], list):
        raise StorageRefusal(
            "invalid_turn_payload",
            "turn begin input must contain the candidate digest, decisions, and plans",
        )
    if (
        not isinstance(payload["candidate_inventory_digest"], str)
        or payload["candidate_inventory_digest"]
        != intake["candidate_inventory"]["digest"]
    ):
        raise StorageRefusal(
            "version_conflict",
            "turn begin candidate digest differs from exact intake",
            retryable=True,
        )
    return payload


def _turn_time(value: Optional[str] = None) -> str:
    if value is not None:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise StorageRefusal("invalid_time", "turn time must include a UTC offset")
        return value
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _turn_mechanical_id(kind: str, *parts: str) -> str:
    payload = "\0".join(("league.request-turn.v1", kind, *parts)).encode("utf-8")
    return f"{kind}:{hashlib.sha256(payload).hexdigest()}"


def _mechanize_turn_decisions(
    intake: dict[str, Any], value: Any, at: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prompts = intake["prompts"]
    candidate_inventory = intake.get("candidate_inventory", {})
    candidates = {
        row["request_id"]: row
        for row in candidate_inventory.get("requests", [])
        if isinstance(row, dict) and isinstance(row.get("request_id"), str)
    }
    if not isinstance(value, list) or len(value) != len(prompts):
        raise StorageRefusal(
            "incomplete_triage_batch", "turn must decide every fetched prompt exactly once"
        )
    decisions: list[dict[str, Any]] = []
    new_requests: list[dict[str, Any]] = []
    for prompt_index, (prompt, raw_decision) in enumerate(zip(prompts, value), start=1):
        if not isinstance(raw_decision, dict) or set(raw_decision) != {"items"}:
            raise StorageRefusal(
                "invalid_triage_batch", "each semantic decision must contain only items"
            )
        raw_items = raw_decision["items"]
        if not isinstance(raw_items, list):
            raise StorageRefusal("invalid_triage_batch", "semantic items must be an array")
        items: list[dict[str, Any]] = []
        for ordinal, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                raise StorageRefusal("invalid_triage", "every semantic item must be an object")
            disposition = raw.get("disposition")
            allowed = {"summary", "disposition"}
            if disposition in {"follow_up", "duplicate", "deferred"}:
                allowed.update(("related_request_id", "related_request_version"))
            if disposition == "deferred":
                allowed.add("defer_seconds")
            if set(raw) != allowed:
                raise StorageRefusal(
                    "invalid_triage", "semantic item contains missing or mechanical fields"
                )
            summary = raw.get("summary")
            if not isinstance(summary, str) or not summary:
                raise StorageRefusal("invalid_triage", "semantic item summary is required")
            item_id = _turn_mechanical_id(
                "prompt-item", str(prompt["prompt_id"]), str(ordinal)
            )
            request_id: Optional[str] = None
            expected_request_version: Optional[int] = None
            next_attention_at: Optional[str] = None
            if disposition == "new_request":
                request_id = _turn_mechanical_id("request", item_id)
            elif disposition in {"follow_up", "duplicate", "deferred"}:
                related = raw.get("related_request_id")
                if not isinstance(related, str) or not related:
                    raise StorageRefusal(
                        "invalid_triage", "related request identity is required"
                    )
                request_id = related
                related_version = raw.get("related_request_version")
                candidate = candidates.get(related)
                if candidate is None:
                    raise StorageRefusal(
                        "candidate_request_unknown",
                        "semantic decision references a request outside the supplied candidate inventory",
                        retryable=True,
                    )
                if (
                    type(related_version) is not int
                    or related_version != candidate.get("version")
                ):
                    raise StorageRefusal(
                        "version_conflict",
                        "semantic decision candidate version differs from intake",
                        retryable=True,
                    )
                expected_request_version = related_version
            if disposition == "deferred":
                seconds = raw.get("defer_seconds")
                if type(seconds) is not int or not 1 <= seconds <= 31_536_000:
                    raise StorageRefusal(
                        "invalid_triage", "defer duration must be one second to one year"
                    )
                parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
                next_attention_at = (parsed + timedelta(seconds=seconds)).isoformat(
                    timespec="seconds"
                ).replace("+00:00", "Z")
            item = {
                "prompt_item_id": item_id,
                "ordinal": ordinal,
                "summary": summary,
                "disposition": disposition,
                "request_id": request_id,
                "expected_request_version": expected_request_version,
                "next_attention_at": next_attention_at,
            }
            items.append(item)
            if disposition == "new_request":
                new_requests.append(
                    {
                        "request_id": request_id,
                        "prompt_index": prompt_index,
                        "prompt_id": prompt["prompt_id"],
                        "adapter_kind": prompt["adapter_kind"],
                        "session_ref": prompt["session_ref"],
                        "runtime_instance_id": prompt["owner_runtime_instance_id"],
                    }
                )
        decisions.append({"prompt_id": prompt["prompt_id"], "items": items})
    return decisions, new_requests


def _turn_dispatch_plans(
    value: Any, at: str, new_requests: list[dict[str, Any]]
) -> tuple[TurnDispatchPlan, ...]:
    if not isinstance(value, list) or len(value) > 20:
        raise StorageRefusal("invalid_turn_plan", "turn routing plans must be a bounded array")
    if len(value) != len(new_requests):
        raise StorageRefusal(
            "incomplete_turn_plan", "turn plans must match each new semantic request"
        )
    required = {"work_kind", "requested_mode", "signals"}
    optional = {
        "hidden_supported",
        "requested_model",
        "requested_effort",
        "explicit_route",
        "continuation_role",
        "continuation_target",
        "hidden_subtask",
        "hidden_scope_budget",
    }
    plans: list[TurnDispatchPlan] = []
    parsed_at = datetime.fromisoformat(at.replace("Z", "+00:00"))
    leased_until = (parsed_at + timedelta(minutes=15)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    for raw, request in zip(value, new_requests):
        if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - required - optional:
            raise StorageRefusal("invalid_turn_plan", "turn routing plan shape is invalid")
        requested_mode = raw["requested_mode"]
        if requested_mode == "squad":
            raise StorageRefusal(
                "batched_route_unsupported",
                "cross-Squad ownership still requires the acknowledgement-gated request route command",
            )
        if not isinstance(raw["work_kind"], str) or not raw["work_kind"]:
            raise StorageRefusal("invalid_turn_plan", "turn work kind is incomplete")
        if requested_mode not in {"direct", "hidden", "champion"}:
            raise StorageRefusal("invalid_turn_plan", "turn routing mode is invalid")
        if "hidden_supported" in raw and not isinstance(raw["hidden_supported"], bool):
            raise StorageRefusal("invalid_turn_plan", "hidden support must be a boolean")
        for name in optional - {"hidden_supported"}:
            if name in raw and raw[name] is not None and (
                not isinstance(raw[name], str) or not raw[name]
            ):
                raise StorageRefusal("invalid_turn_plan", "optional turn routing identity is invalid")
        signals = raw["signals"]
        if not isinstance(signals, dict):
            raise StorageRefusal("invalid_turn_plan", "turn routing signals must be an object")
        plans.append(
            TurnDispatchPlan(
                runtime_instance_id=request["runtime_instance_id"],
                claim_token=_turn_mechanical_id("claim", request["request_id"]),
                leased_until=leased_until,
                command=DispatchRequestCommand(
                    request_id=request["request_id"],
                    claim_token=_turn_mechanical_id("claim", request["request_id"]),
                    dispatch_id=_turn_mechanical_id("dispatch", request["request_id"]),
                    work_kind=raw["work_kind"],
                    requested_mode=requested_mode,
                    hidden_supported=raw.get("hidden_supported", False),
                    requested_model=raw.get("requested_model"),
                    requested_effort=raw.get("requested_effort"),
                    explicit_route=raw.get("explicit_route"),
                    at=at,
                    orchestration=OrchestrationSignals.from_value(signals),
                    continuation_role=raw.get("continuation_role"),
                    continuation_target=raw.get("continuation_target"),
                    hidden_subtask=raw.get("hidden_subtask"),
                    hidden_scope_budget=raw.get("hidden_scope_budget"),
                ),
            )
        )
    return tuple(plans)


def _turn_commit_actions(
    value: Any, at: str, new_requests: list[dict[str, Any]], begun: dict[str, Any]
) -> tuple[Any, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise StorageRefusal("invalid_turn_commit", "turn commit actions must be a bounded array")
    actions: list[Any] = []
    for raw in value:
        if not isinstance(raw, dict) or raw.get("kind") not in {"answer", "result"}:
            raise StorageRefusal("invalid_turn_commit", "turn commit action kind is invalid")
        if raw["kind"] == "answer":
            fields = {"kind", "request_index", "content", "resolution_summary"}
            if set(raw) != fields:
                raise StorageRefusal("invalid_turn_commit", "turn answer action shape is invalid")
            if (
                type(raw["request_index"]) is not int
                or not isinstance(raw["content"], str)
                or not raw["content"]
                or not isinstance(raw["resolution_summary"], str)
                or not raw["resolution_summary"]
            ):
                raise StorageRefusal("invalid_turn_commit", "turn answer action values are invalid")
            index = raw["request_index"] - 1
            if not 0 <= index < len(new_requests):
                raise StorageRefusal("invalid_turn_commit", "turn request index is invalid")
            request = new_requests[index]
            routing = begun["routing"][index]
            request_id = request["request_id"]
            claim_token = _turn_mechanical_id("claim", request_id)
            actions.append(
                AnswerRequestCommand(
                    request_id=request_id,
                    claim_token=claim_token,
                    expected_version=routing["dispatch"]["request_version"],
                    response_ref_id=_turn_mechanical_id("response", request_id),
                    adapter_kind=request["adapter_kind"],
                    session_locator=request["session_ref"],
                    response_locator=f"turn:{request['prompt_id']}:{request['prompt_index']}",
                    durability="durable",
                    content_hash=hashlib.sha256(raw["content"].encode("utf-8")).hexdigest(),
                    resolution_summary=raw["resolution_summary"],
                    event_id=_turn_mechanical_id("event-answer", request_id),
                    at=at,
                )
            )
        else:
            fields = {"kind", "request_index", "outcome", "summary", "task_ids", "return_to_requester"}
            if (
                set(raw) != fields
                or type(raw["request_index"]) is not int
                or not isinstance(raw["task_ids"], list)
                or any(not isinstance(item, str) or not item for item in raw["task_ids"])
                or not isinstance(raw["return_to_requester"], bool)
                or any(
                    not isinstance(raw[name], str) or not raw[name]
                    for name in fields - {"kind", "request_index", "task_ids", "return_to_requester"}
                )
            ):
                raise StorageRefusal("invalid_turn_commit", "turn result action shape is invalid")
            index = raw["request_index"] - 1
            if not 0 <= index < len(new_requests):
                raise StorageRefusal("invalid_turn_commit", "turn request index is invalid")
            request_id = new_requests[index]["request_id"]
            routing = begun["routing"][index]
            actions.append(
                RequestResultCommand(
                    request_id=request_id,
                    claim_token=_turn_mechanical_id("claim", request_id),
                    expected_version=routing["dispatch"]["request_version"],
                    result_id=_turn_mechanical_id("result", request_id),
                    idempotency_key=_turn_mechanical_id("result-key", request_id),
                    outcome=raw["outcome"],
                    summary=raw["summary"],
                    task_ids=tuple(raw["task_ids"]),
                    at=at,
                    return_to_requester=raw["return_to_requester"],
                    event_id=_turn_mechanical_id("event-result", request_id) if raw["return_to_requester"] else None,
                    outbox_id=_turn_mechanical_id("outbox-result", request_id) if raw["return_to_requester"] else None,
                )
            )
    return tuple(actions)


def _request_bind_prompt(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.bind_quarantined_prompt(
        args.prompt_id, args.intake_actor_id, args.runtime_instance_id, args.at
    ), None


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
            orchestration=OrchestrationSignals(
                pre_bounded=args.pre_bounded,
                read_only=args.read_only,
                answer_or_routing_only=args.answer_or_routing_only,
                expected_minutes=args.expected_minutes,
                expected_task_action_calls=args.expected_task_action_calls,
                creates_artifact=args.creates_artifact,
                mutates_state=args.mutates_state,
                reproduces_issue=args.reproduces_issue,
                runs_tests=args.runs_tests,
                runs_benchmark=args.runs_benchmark,
                uses_browser_or_computer=args.uses_browser_or_computer,
                project_implementation=args.project_implementation,
                project_suggested_shotcaller=args.project_suggested_shotcaller,
            ),
            continuation_role=args.continuation_role,
            continuation_target=args.continuation_target,
            hidden_subtask=args.hidden_subtask,
            hidden_scope_budget=args.hidden_scope_budget,
        )
    ), None


def _request_decide_route(store: Storage, args: argparse.Namespace) -> CommandResult:
    value = _read_json_object(args.signals)
    signals = OrchestrationSignals.from_value(value)
    return store.orchestration_decision(
        signals,
        project_ids=args.project_id,
        explicit_squad_id=args.explicit_squad_id,
        continuation_squad_id=args.continuation_squad_id,
        required_capabilities=args.required_capability,
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
        recipient_squad_id=args.recipient_squad_id,
        route_reason_code=args.route_reason_code,
        route_policy_version=args.route_policy_version,
        route_confidence=args.route_confidence,
        required_capabilities=tuple(args.required_capability),
    ), None


def _request_progress(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.emit_request_progress(
        RequestProgressCommand(
            progress_id=args.progress_id,
            request_id=args.request_id,
            claim_token=args.claim_token,
            expected_version=args.expected_version,
            progress_generation=args.progress_generation,
            reason_code=args.reason_code,
            settled_count=args.settled_count,
            total_count=args.total_count,
            current_phase=args.current_phase,
            blocker_count=args.blocker_count,
            blocker_severity=args.blocker_severity,
            user_action_required=args.user_action_required,
            deadline_change=args.deadline_change,
            next_action=args.next_action,
            event_id=args.event_id,
            outbox_id=args.outbox_id,
            at=args.at,
            minimum_interval_seconds=args.minimum_interval_seconds,
            grace_seconds=args.grace_seconds,
            promised_checkpoint_at=args.promised_checkpoint_at,
        )
    ), None


def _request_reconcile_progress(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.reconcile_request_progress(args.owner_agent_id, args.at), None


def _squad_register(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.register_squad(
        registration_id=args.registration_id,
        squad_id=args.squad_id,
        requester_agent_id=args.requester_agent_id,
        shotcaller_agent_id=args.shotcaller_agent_id,
        runtime_instance_id=args.runtime_instance_id,
        project_ids=tuple(args.project_id),
        capabilities=tuple(args.capability),
        expires_at=args.expires_at,
        event_id=args.event_id,
        outbox_id=args.outbox_id,
        at=args.at,
    ), None


def _squad_accept(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.accept_squad(
        registration_id=args.registration_id,
        shotcaller_agent_id=args.shotcaller_agent_id,
        runtime_instance_id=args.runtime_instance_id,
        decision=args.decision,
        event_id=args.event_id,
        outbox_id=args.outbox_id,
        at=args.at,
    ), None


def _squad_status(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.squad_status(
        registration_id=args.registration_id,
        squad_id=args.squad_id,
        at=args.at,
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


def _request_untriaged(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.untriaged_intake(
        args.owner_agent_id,
        limit=args.limit,
        max_bytes=args.max_bytes,
        candidate_limit=args.candidate_limit,
        candidate_max_bytes=args.candidate_max_bytes,
        candidate_after=args.candidate_after,
        candidate_page=args.candidate_page,
    ), None


def _request_reconcile_duplicate(
    store: Storage, args: argparse.Namespace
) -> CommandResult:
    return store.reconcile_duplicate_request(
        ReconcileDuplicateRequestCommand(
            duplicate_request_id=args.duplicate_request_id,
            canonical_request_id=args.canonical_request_id,
            owner_agent_id=args.owner_agent_id,
            expected_duplicate_version=args.expected_duplicate_version,
            expected_canonical_version=args.expected_canonical_version,
            at=args.at,
        )
    ), None


def _assign_prepare(store: Storage, args: argparse.Namespace) -> CommandResult:
    if args.role == "champion":
        raise StorageRefusal(
            "issue_verification_required",
            "visible repository work must use assign run for owner-API issue verification",
        )
    return store.prepare_assignment(
        PrepareAssignmentCommand(
            assignment_id=args.assignment_id,
            request_id=args.request_id,
            claim_token=args.claim_token,
            task_id=args.task_id,
            task_summary=args.task_summary,
            coordinator_agent_id=args.coordinator_agent_id,
            champion_agent_id=args.champion_agent_id,
            repository=args.repository,
            issue=args.issue,
            branch=args.branch,
            worktree=args.worktree,
            at=args.at,
            issue_receipt=None,
            required_capabilities=tuple(args.requires),
            assignment_role=args.role,
            dispatch_id=args.dispatch_id,
            promoted_from_assignment_id=args.promoted_from_assignment_id,
        )
    ), None


def _assign_launch(store: Storage, args: argparse.Namespace) -> CommandResult:
    if args.state_root is None:
        raise StorageRefusal("state_root_required", "visible launch requires canonical state")
    assignment_id = args.assignment_id or derived_assignment_id(
        args.request_id, args.task_id
    )
    champion_agent_id = args.champion_agent_id or derived_champion_agent_id(
        assignment_id
    )
    resume_thread_id = continuation_resume_thread(
        store,
        assignment_id=assignment_id,
        task_id=args.task_id,
        champion_agent_id=champion_agent_id,
        repository=args.repository,
        issue=args.issue,
        branch=args.branch,
        worktree=args.worktree,
        at=_turn_time(),
    )
    workspace_id = args.workspace_id or os.environ.get("HERDR_WORKSPACE_ID", "")
    league_command = str(
        Path(args.league_command).resolve()
        if args.league_command
        else Path(sys.argv[0]).resolve()
    )
    options = VisibleLaunchOptions(
        workspace_id=workspace_id,
        task_label=args.task_label or derive_task_label(args.task_summary),
        model=args.model,
        effort=args.effort,
        league_command=league_command,
        state_root=str(args.state_root.resolve()),
        startup_timeout_ms=args.startup_timeout_ms,
    )
    spec = AssignmentSpec(
        assignment_id=assignment_id,
        request_id=args.request_id,
        claim_token=args.claim_token,
        task_id=args.task_id,
        task_summary=args.task_summary,
        coordinator_agent_id=args.coordinator_agent_id,
        champion_agent_id=champion_agent_id,
        repository=args.repository,
        issue=args.issue,
        branch=args.branch,
        worktree=str(Path(args.worktree).resolve()),
        issue_receipt=None,
        required_capabilities=tuple(args.requires),
    )
    runner = SubprocessRunner()
    adapter = HerdrCodexLaunchAdapter(
        options, runner, resume_thread_id=resume_thread_id
    )
    verifier = GitHubIssueVerifier(
        runner,
        selection_receipt_digest=args.issue_selection_receipt_digest,
    )
    return VisibleChampionLaunchService(
        store, adapter, options, issue_verifier=verifier
    ).launch(spec), None


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


def _assign_finish_hidden(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.finish_hidden_assignment(
        FinishHiddenAssignmentCommand(
            assignment_id=args.assignment_id,
            runtime_instance_id=args.runtime_instance_id,
            expected_version=args.expected_version,
            status=args.status,
            result_summary=args.result_summary,
            cleanup_receipt=args.cleanup_receipt,
            unpublished_state_receipt=args.unpublished_state_receipt,
            transition_id=args.transition_id,
            transition_key=args.transition_key,
            event_id=args.event_id,
            outbox_id=args.outbox_id,
            at=args.at,
        )
    ), None


def _assign_reconcile_runtime(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.reconcile_assignment_runtime(args.assignment_id, args.at), None


def _assign_reconcile_legacy_display(
    store: Storage, args: argparse.Namespace
) -> CommandResult:
    expected_source = None
    expected_title = None
    expected_sequence = None
    expected = _decode_json(
        args.expected_presentation_json, "legacy display expected presentation"
    )
    if not isinstance(expected, dict) or set(expected) != {
        "source",
        "title",
        "state_change_seq",
    }:
        raise StorageRefusal(
            "legacy_display_invalid",
            "expected presentation must contain only source, title, and state_change_seq",
        )
    expected_source = expected["source"]
    expected_title = expected["title"]
    expected_sequence = expected["state_change_seq"]
    spec = LegacyDisplayReconciliationSpec(
        assignment_id=args.assignment_id,
        expected_version=args.expected_version,
        champion_agent_id=args.champion_agent_id,
        runtime_instance_id=args.runtime_instance_id,
        callsign=args.callsign,
        pane_id=args.pane_id,
        terminal_id=args.terminal_id,
        thread_id=args.thread_id,
        worktree=str(Path(args.worktree).resolve()),
        routing_name=args.routing_name,
        expected_presentation_source=expected_source,
        expected_title=expected_title,
        expected_state_change_seq=expected_sequence,
        target_task_label=args.target_task_label,
        owner_authorized=args.owner_authorized or args.mode_action is not None,
    )
    return LegacyDisplayReconciliationService(
        store, HerdrLegacyDisplayAdapter(), _ProvidedClock(args.at)
    ).reconcile(spec), None


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
            capabilities=None if args.capability is None else tuple(args.capability),
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


def _mode_authorize(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.authorize_mode(
        _read_json_object(args.grant), args.expected_goal_version, args.at
    ), None


def _mode_status(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.mode_status(args.goal_id, args.at), None


def _mode_use(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.use_mode_action(
        _read_json_object(args.action_spec), args.expected_goal_version, args.at
    ), None


def _mode_settle(store: Storage, args: argparse.Namespace) -> CommandResult:
    from .storage_mode import SettleModeActionCommand

    return store.settle_mode_action(
        SettleModeActionCommand(
            action_use_id=args.action_use_id,
            goal_id=args.goal_id,
            expected_goal_version=args.expected_goal_version,
            use_receipt_digest=args.use_receipt_digest,
            outcome=args.outcome,
            result_receipt_digest=args.result_receipt_digest,
            failure_class=args.failure_class,
            at=args.at,
        )
    ), None


def _mode_transition(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.transition_mode_goal(
        args.goal_id, args.expected_goal_version, args.state, args.at
    ), None


def _mode_revoke(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.revoke_mode_grant(
        args.grant_id,
        args.revoked_by,
        args.reason,
        args.expected_goal_version,
        args.at,
    ), None


def _hook_set_supervision_policy(
    store: Storage, args: argparse.Namespace
) -> CommandResult:
    return store.configure_supervision_policy(
        args.scope_id,
        args.actor_agent_id,
        args.mode,
        args.unreachable_grace_seconds,
        args.at,
    ), None


def _issue_select(store: Storage, args: argparse.Namespace) -> CommandResult:
    runner = SubprocessRunner()
    service = GitHubIssueSelectionService(store, runner)
    return service.select(
        IssueSelectionSpec(
            task_id=args.task_id,
            task_summary=args.task_summary,
            coordinator_agent_id=args.coordinator_agent_id,
            repository=args.repository,
            issue_title=args.issue_title,
            issue_body=_read_bounded_text(
                args.issue_body, MAX_ISSUE_BODY_BYTES, "repository issue body"
            ),
        ),
        f"issue-attempt:{uuid.uuid4()}",
        args.at,
        reopen_action_receipt_digest=args.reopen_action_receipt_digest,
    ), None


def _hook_reconcile_silent(store: Storage, args: argparse.Namespace) -> CommandResult:
    return store.silent_supervision_updates(
        args.actor_agent_id,
        after_event_seq=args.after_event_seq,
        limit=args.limit,
        advance_cursor=True,
        at=args.at,
    ), None


HANDLERS: dict[str, CommandHandler] = {
    "storage.integrity": _storage_integrity,
    "storage.backup": _storage_backup,
    "storage.export": _storage_export,
    "storage.import": _storage_import,
    "agent.status": _agent_status,
    "agent.transition": _agent_transition,
    "callsign.reconcile": _callsign_reconcile,
    "callsign.allocate": _callsign_allocate,
    "callsign.activate": _callsign_activate,
    "callsign.rollback": _callsign_rollback,
    "callsign.release": _callsign_release,
    "callsign.status": _callsign_status,
    "shotcaller.create": _shotcaller_create,
    "rollover.prepare": _rollover_prepare,
    "rollover.bindings": _rollover_bindings,
    "rollover.refresh-bindings": _rollover_refresh_bindings,
    "rollover.acknowledge": _rollover_acknowledge,
    "rollover.commit": _rollover_commit,
    "rollover.reconcile-descendant": _rollover_reconcile_descendant,
    "rollover.reconcile-intake": _rollover_reconcile_intake,
    "rollover.intake-plan": _rollover_intake_plan,
    "rollover.abort": _rollover_abort,
    "rollover.drain": _rollover_drain,
    "rollover.status": _rollover_status,
    "delivery.claim": _delivery_claim,
    "delivery.ack": _delivery_ack,
    "delivery.fail": _delivery_fail,
    "delivery.claim-outbox": _delivery_claim_outbox,
    "delivery.ack-outbox": _delivery_ack_outbox,
    "delivery.fail-outbox": _delivery_fail_outbox,
    "delivery.backlog": _delivery_backlog,
    "project.put": _project_put,
    "project.resolve": _project_resolve,
    "project.list": _project_list,
    "project.suggest-squads": _project_suggest_squads,
    "project.advise": _project_advise,
    "roster.snapshot": _roster_snapshot,
    "evidence.record": _evidence_record,
    "report.generate": _report_generate,
    "report.show": _report_show,
    "squad.register": _squad_register,
    "squad.accept": _squad_accept,
    "squad.status": _squad_status,
    "task.transfer-owner": _task_transfer,
    "task.transition": _task_transition,
    "runtime.matrix": _runtime_matrix,
    "routing.choose": _routing_choose,
    "routing.escalate": _routing_escalate,
    "routing.outcome": _routing_outcome,
    "artifact.declare": _artifact_declare,
    "artifact.publish": _artifact_publish,
    "artifact.status": _artifact_status,
    "resource.register": _resource_register,
    "cleanup.plan": _cleanup_plan,
    "cleanup.execute": _cleanup_execute,
    "cleanup.reconcile": _cleanup_reconcile,
    "cleanup.status": _cleanup_status,
    "continuation.prepare": _continuation_prepare,
    "continuation.reopen": _continuation_reopen,
    "continuation.status": _continuation_status,
    "request.intake": _request_intake,
    "request.triage": _request_triage,
    "request.bind-prompt": _request_bind_prompt,
    "request.claim": _request_claim,
    "request.accept": _request_claim,
    "request.release": _request_release,
    "request.dispatch": _request_dispatch,
    "request.decide-route": _request_decide_route,
    "request.route": _request_route,
    "request.progress": _request_progress,
    "request.reconcile-progress": _request_reconcile_progress,
    "request.awaiting-user": _request_state,
    "request.block": _request_state,
    "request.defer": _request_state,
    "request.cancel": _request_state,
    "request.result": _request_result,
    "request.answer": _request_answer,
    "request.unresolved": _request_unresolved,
    "request.untriaged": _request_untriaged,
    "request.reconcile-duplicate": _request_reconcile_duplicate,
    "assign.prepare": _assign_prepare,
    "assign.run": _assign_launch,
    "assign.launching": _assign_launching,
    "assign.activate": _assign_activate,
    "assign.reconcile-runtime": _assign_reconcile_runtime,
    "assign.reconcile-legacy-display": _assign_reconcile_legacy_display,
    "assign.block": _assign_block,
    "assign.finish-hidden": _assign_finish_hidden,
    "hook.register-runtime": _hook_register_runtime,
    "hook.register-watcher": _hook_register_watcher,
    "hook.user-message": _hook_user_message,
    "hook.rearm": _hook_rearm,
    "hook.allow-stop-once": _hook_allow_stop_once,
    "hook.stop": _hook_stop,
    "mode.authorize": _mode_authorize,
    "mode.status": _mode_status,
    "mode.use": _mode_use,
    "mode.settle": _mode_settle,
    "mode.transition": _mode_transition,
    "mode.revoke": _mode_revoke,
    "issue.select": _issue_select,
    "hook.set-supervision-policy": _hook_set_supervision_policy,
    "hook.reconcile-silent": _hook_reconcile_silent,
}


SCHEMA_INVENTORY = (
    "league-command-output.schema.json",
    "league-import-report.schema.json",
    "league-export.schema.json",
    "league-acceptance-receipt.schema.json",
    "league-legacy-reconciliation.schema.json",
    "league-pre-cutover-plan.schema.json",
    "league-pre-cutover-receipt.schema.json",
    "league-cleanup-canary-adapters.schema.json",
    "league-real-cleanup-artifact-profile.schema.json",
    "league-real-cleanup-canary-receipt.schema.json",
    "league-help.schema.json",
    "league-request-triage.schema.json",
    "league-assignment-receipt.schema.json",
    "league-stop-decision.schema.json",
    "league-skill-contracts.schema.json",
    "league-skill-runtime-profile.schema.json",
    "league-skill-validation.schema.json",
    "league-skill-audit.schema.json",
    "league-skill-matrix.schema.json",
    "league-project-catalog.schema.json",
    "league-roster-snapshot.schema.json",
    "league-callsign-catalog.schema.json",
    "league-runtime-acceptance.schema.json",
    "league-shotcaller-handoff-plan.schema.json",
    "league-rollover-pages.schema.json",
    "league-rollover-abort-receipt.schema.json",
    "league-rollover-drain-receipt.schema.json",
    "league-activity-evidence.schema.json",
    "league-report.schema.json",
    "league-outbound-receipt.schema.json",
    "league-autonomous-grant.schema.json",
    "league-autonomous-action.schema.json",
    "league-mode-status.schema.json",
    "league-mode-action-receipt.schema.json",
    "league-protected-gate-receipt.schema.json",
    "league-repository-issue.schema.json",
    "league-issue-selection-receipt.schema.json",
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
                "acceptance.preflight",
                "acceptance.cutover",
                "acceptance.cleanup-canary",
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


_PROTECTED_GATE_SCOPE_OMISSIONS = {
    "action",
    "at",
    "authority_digest",
    "authority_kind",
    "busy_timeout_ms",
    "expected_mode_goal_version",
    "group",
    "mode_action",
    "no_wal",
    "owner_authorized",
    "state_root",
}


def _protected_gate_scope(command: str, args: argparse.Namespace) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    for name, value in sorted(vars(args).items()):
        if name in _PROTECTED_GATE_SCOPE_OMISSIONS:
            continue
        if isinstance(value, Path):
            try:
                with value.open("rb") as stream:
                    payload = stream.read(MAX_JSON_INPUT_BYTES + 1)
            except OSError as exc:
                raise StorageRefusal(
                    "input_invalid", "protected gate input could not be read"
                ) from exc
            if len(payload) > MAX_JSON_INPUT_BYTES:
                raise StorageRefusal(
                    "input_too_large",
                    f"protected gate input exceeds the {MAX_JSON_INPUT_BYTES}-byte limit",
                )
            arguments[name] = {
                "content_sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        elif isinstance(value, tuple):
            arguments[name] = list(value)
        else:
            arguments[name] = value
    return {"command": command, "arguments": arguments}


def _run_protected_gate(
    handler: Callable[[Storage, argparse.Namespace], CommandResult],
    store: Storage,
    args: argparse.Namespace,
    command: str,
    begun: dict[str, Any],
) -> Any:
    if command == "rollover.prepare":
        args.authority_kind = "automatic"
        args.authority_digest = begun["use_receipt_digest"]
    result, raw = handler(store, args)
    if raw is not None:
        raise StorageRefusal(
            "protected_gate_output_refused",
            "protected gates require one structured canonical result",
        )
    return result


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
    if command == "acceptance.preflight":
        from .precutover import run_pre_cutover

        if args.state_root is not None:
            raise StorageRefusal(
                "invalid_acceptance_root",
                "pre-cutover acceptance uses --temporary-root and refuses --state-root",
            )
        return run_pre_cutover(
            args.temporary_root,
            args.namespace,
            plan_path=args.plan,
            sentinel_paths=tuple(args.sentinel_path),
            config_sentinel=args.config_sentinel,
            process_sentinel=args.process_sentinel,
        ), None
    if command == "acceptance.cutover":
        from .livecutover import run_live_cutover

        if args.state_root is not None:
            raise StorageRefusal(
                "invalid_acceptance_root",
                "live cutover uses --temporary-root and refuses --state-root",
            )
        return run_live_cutover(
            args.temporary_root,
            args.namespace,
            plan_path=args.plan,
            authority_receipt=args.authority_receipt,
            authority_digest=args.authority_digest,
            source_root=args.source_root,
        ), None
    if command == "acceptance.archive-verify":
        from .livecutover import verify_legacy_archive

        if args.state_root is not None:
            raise StorageRefusal(
                "invalid_acceptance_root",
                "legacy archive verification refuses --state-root",
            )
        return verify_legacy_archive(args.archive), None
    if command == "acceptance.cleanup-canary":
        from .real_canary import run_real_cleanup_canary

        if args.state_root is not None:
            raise StorageRefusal(
                "invalid_acceptance_root",
                "real cleanup canary uses --temporary-root and refuses --state-root",
            )
        return run_real_cleanup_canary(
            args.temporary_root,
            args.namespace,
            source_root=args.source_root,
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
        mode_action_path = getattr(args, "mode_action", None)
        expected_mode_goal_version = getattr(
            args, "expected_mode_goal_version", None
        )
        if (mode_action_path is None) != (expected_mode_goal_version is None):
            raise StorageRefusal(
                "protected_gate_authority_incomplete",
                "mode action and expected mode goal version must be supplied together",
            )
        if mode_action_path is not None:
            if command not in PROTECTED_GATE_ACTIONS:
                raise StorageRefusal(
                    "protected_gate_unknown",
                    "command is not an autonomous protected gate",
                )
            if command == "rollover.prepare" and (
                args.authority_kind is not None or args.authority_digest is not None
            ):
                raise StorageRefusal(
                    "protected_gate_authority_conflict",
                    "rollover preparation cannot combine manual and mode authority",
                )
            if (
                command == "assign.reconcile-legacy-display"
                and args.owner_authorized
            ):
                raise StorageRefusal(
                    "protected_gate_authority_conflict",
                    "legacy display reconciliation cannot combine manual and mode authority",
                )
            action = _read_json_object(mode_action_path)
            gate_scope = _protected_gate_scope(command, args)
            return (
                ProtectedGateExecutor(store).execute(
                    gate_name=command,
                    gate_scope=gate_scope,
                    action=action,
                    expected_goal_version=expected_mode_goal_version,
                    at=args.at,
                    operation=lambda begun: _run_protected_gate(
                        handler, store, args, command, begun
                    ),
                ),
                None,
            )
        return handler(store, args)


def main(
    argv: Optional[list[str]] = None,
    *,
    input_stream: Optional[BinaryIO] = None,
    output: Optional[BinaryIO] = None,
) -> int:
    args = _parser().parse_args(argv)
    command = _command_name(args)
    sink = output or sys.stdout.buffer
    try:
        if command == "request.turn":
            with _open(args) as store:
                source = input_stream or sys.stdin.buffer
                begin_at = _turn_time(args.at)
                intake = store.untriaged_intake(
                    args.owner_agent_id,
                    limit=args.limit,
                    max_bytes=args.max_bytes,
                    candidate_limit=args.candidate_limit,
                    candidate_max_bytes=args.candidate_max_bytes,
                )
                sink.write(
                    _envelope_bytes(command, result={"phase": "intake", **intake})
                )
                sink.flush()
                expected_prompt_ids = tuple(
                    prompt["prompt_id"] for prompt in intake["prompts"]
                )
                begin_payload = _turn_begin_payload(
                    source, intake, expected_prompt_ids
                )
                decisions, new_requests = _mechanize_turn_decisions(
                    intake, begin_payload["decisions"], begin_at
                )
                begun = store.begin_request_turn(
                    args.owner_agent_id,
                    expected_prompt_ids,
                    decisions,
                    _turn_dispatch_plans(begin_payload["plans"], begin_at, new_requests),
                    begin_at,
                    expected_candidate_digest=intake["candidate_inventory"]["snapshot_digest"],
                    candidate_limit=args.candidate_limit,
                    candidate_max_bytes=args.candidate_max_bytes,
                )
                for request, route in zip(new_requests, begun["routing"]):
                    route["mechanical"] = {
                        "request_id": request["request_id"],
                        "claim_token": _turn_mechanical_id("claim", request["request_id"]),
                    }
                sink.write(
                    _envelope_bytes(
                        command,
                        result={
                            "phase": "begun",
                            **begun,
                            "unresolved": store.request_turn_boundary(args.owner_agent_id),
                        },
                    )
                )
                sink.flush()
                commit_payload = _read_turn_payload(source)
                if set(commit_payload) != {"actions"}:
                    raise StorageRefusal(
                        "invalid_turn_payload",
                        "turn commit input must contain only an actions array",
                    )
                commit_at = _turn_time(args.at)
                committed = store.commit_request_turn(
                    args.owner_agent_id,
                    _turn_commit_actions(
                        commit_payload["actions"], commit_at, new_requests, begun
                    ),
                    commit_at,
                )
                result, raw = (
                    {
                        "phase": "committed",
                        **committed,
                        "unresolved": store.request_turn_boundary(args.owner_agent_id),
                    },
                    None,
                )
        else:
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
