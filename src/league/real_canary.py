"""One-shot real disposable Herdr/Codex cleanup acceptance gate.

This command is deliberately narrower than a production cleanup daemon.  It
may create and remove resources only beneath an explicit temporary root, plus
one uniquely named sibling Herdr pane in the caller's current workspace.  It
never reads or writes the canonical League home.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .acceptance import NAMESPACE_PATTERN, _atomic_write, _sha256, _stable_bytes, _write_json
from .orchestration import OrchestrationSignals
from .issue_first import (
    GitHubIssueSelectionService,
    GitHubIssueVerifier,
    IssueSelectionSpec,
)
from .precutover import (
    CHAMPION_ID,
    LIFECYCLE_TASK_ID,
    SHOTCALLER_ID,
    _Clock,
    _DeliveryDouble,
    _Ids,
    _seed_synthetic_store,
)
from .request_services import AssignmentService, AssignmentSpec, DeliveryService
from .sqlite_store import SQLiteStorage
from .supervision_policy import (
    CONSECUTIVE_OBSERVATIONS,
    READINESS_WAIT_MILLISECONDS,
)
from .storage import (
    AnswerRequestCommand,
    DispatchRequestCommand,
    RequestResultCommand,
    RuntimeRegistrationCommand,
)
from .storage_types import StorageRefusal


RECEIPT_SCHEMA = "league.real-cleanup-canary-receipt.v1"
ADAPTER_SCHEMA = "league.cleanup-canary-adapters.v1"
TRANSITION_AT = "2026-01-01T01:01:00Z"
INTERRUPT_AT = "2026-01-01T01:02:00Z"
INTERRUPT_LEASE = "2026-01-01T01:03:00Z"
RESUME_AT = "2026-01-01T01:04:00Z"
RESUME_LEASE = "2026-01-01T01:14:00Z"
READINESS_MAX_OBSERVATIONS = CONSECUTIVE_OBSERVATIONS
REPORT_ARTIFACT_ID = "artifact:issue-39-overnight-report"
REPORT_REPOSITORY = "https://github.com/Vinosaamaa/league-of-orchestrator"
REPORT_ISSUE = 39
REPORT_PULL_REQUEST = 41
REPORT_PULL_REQUEST_URL = f"{REPORT_REPOSITORY}/pull/{REPORT_PULL_REQUEST}"
REPORT_TESTED_HEAD = "509e00b7476a8a449f690a04250fe9d49bfbaca3"
REPORT_MERGE_COMMIT = "3c517535b6cf4423bd6704b06d30f2e3cc299784"
REPORT_MERGED_AT = "2026-08-29T02:49:40Z"
REPORT_BRANCH = "agent/braum/39-overnight-report"
REPORT_PATH = "docs/reports/2026-08-28-overnight-delivery-report.md"
CANARY_ISSUE_REPOSITORY = "https://github.com/Vinosaamaa/league-of-orchestrator.git"
CODEX_SESSION_TITLE = re.compile(
    r"^(?P<session>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) \| codex$"
)


class _FixtureSettlementIds:
    def __init__(self) -> None:
        self.sequence = 0

    def new(self, kind: str) -> str:
        self.sequence += 1
        return f"{kind}:fixture-settlement:{self.sequence}"


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    allowed: frozenset[int] = frozenset({0}),
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(arguments),
            cwd=cwd,
            env=None if env is None else dict(env),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StorageRefusal(
            "real_canary_command_failed", "a bounded real-canary command could not complete"
        ) from exc
    if result.returncode not in allowed:
        raise StorageRefusal(
            "real_canary_command_failed", "a bounded real-canary command refused or failed"
        )
    return result


class _OwnerApiRunner:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def run(
        self, arguments: Sequence[str], *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            arguments,
            cwd=self.cwd,
            timeout=timeout_seconds,
            allowed=frozenset(range(256)),
        )


def _owner_verified_issue_spec(
    store: SQLiteStorage, cwd: Path, spec: AssignmentSpec, at: str
) -> AssignmentSpec:
    runner = _OwnerApiRunner(cwd)
    observed = runner.run(
        (
            "gh",
            "api",
            "--method",
            "GET",
            "repos/Vinosaamaa/league-of-orchestrator/issues/23",
        ),
        timeout_seconds=30,
    )
    if observed.returncode != 0:
        raise StorageRefusal(
            "real_canary_issue_unverified",
            "the owner API did not return canary issue 23",
        )
    try:
        issue = json.loads(observed.stdout)
    except json.JSONDecodeError as exc:
        raise StorageRefusal(
            "real_canary_issue_unverified",
            "the owner API returned malformed issue evidence",
        ) from exc
    if (
        not isinstance(issue, dict)
        or not isinstance(issue.get("title"), str)
        or not isinstance(issue.get("body"), str)
    ):
        raise StorageRefusal(
            "real_canary_issue_unverified",
            "the owner API returned incomplete issue evidence",
        )
    exact_spec = replace(spec, task_summary=issue["title"])
    selected = GitHubIssueSelectionService(store, runner).select(
        IssueSelectionSpec(
            task_id=exact_spec.task_id,
            task_summary=exact_spec.task_summary,
            coordinator_agent_id=exact_spec.coordinator_agent_id,
            repository=exact_spec.repository,
            issue_title=issue["title"],
            issue_body=issue["body"],
        ),
        f"attempt:{exact_spec.task_id}",
        at,
        allow_create=False,
        expected_issue=exact_spec.issue,
    )
    if int(selected["issue"]) != exact_spec.issue:
        raise StorageRefusal(
            "real_canary_issue_unverified",
            "duplicate preflight selected a different issue",
        )
    receipt = GitHubIssueVerifier(
        runner,
        selection_receipt_digest=selected["receipt_digest"],
    ).verify(exact_spec, at)
    return replace(exact_spec, issue_receipt=receipt)


def _json_result(result: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    payload = result.stdout if result.stdout.strip() else result.stderr
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("real_canary_output_invalid", f"{label} returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise StorageRefusal("real_canary_output_invalid", f"{label} returned a non-object")
    return value


def _herdr(arguments: Sequence[str], cwd: Path, *, timeout: int = 120) -> dict[str, Any]:
    return _json_result(_run(("herdr", *arguments), cwd=cwd, timeout=timeout), "Herdr")


def _agent_rows(cwd: Path) -> list[dict[str, Any]]:
    value = _herdr(("agent", "list"), cwd)
    rows = value.get("result", {}).get("agents", [])
    if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
        raise StorageRefusal("real_canary_output_invalid", "Herdr agent inventory is malformed")
    return rows


def _exact_agent(cwd: Path, name: str) -> dict[str, Any] | None:
    matches = [item for item in _agent_rows(cwd) if item.get("name") == name]
    if len(matches) > 1:
        raise StorageRefusal("real_canary_identity_ambiguous", "Herdr canary name is ambiguous")
    return matches[0] if matches else None


def _codex_session_id(agent: Mapping[str, Any]) -> str | None:
    session = agent.get("agent_session")
    if isinstance(session, Mapping) and isinstance(session.get("value"), str):
        return str(session["value"])
    title = agent.get("terminal_title_stripped")
    matched = CODEX_SESSION_TITLE.fullmatch(title) if isinstance(title, str) else None
    return matched.group("session") if matched is not None else None


def _disposable_git_scope(
    home: Path, *, deterministic_dates: bool = False
) -> tuple[Path, Path, dict[str, str]]:
    repository = (home / "git/repository").resolve(strict=False)
    worktree = (home / "git/worktree").resolve(strict=False)
    repository.parent.mkdir(parents=True, mode=0o700)
    process_home = home / "process-home"
    process_home.mkdir(mode=0o700, exist_ok=True)
    environment = {
        **os.environ,
        "HOME": str(process_home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    if deterministic_dates:
        environment.update(
            {
                "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
            }
        )
    return repository, worktree, environment


def _create_git_canary(home: Path) -> dict[str, str]:
    repository, worktree, environment = _disposable_git_scope(
        home, deterministic_dates=True
    )
    _run(("git", "init", "-b", "main", str(repository)), cwd=home, env=environment)
    _atomic_write(repository / "README.md", b"League disposable cleanup canary\n", mode=0o600)
    _run(("git", "-C", str(repository), "add", "README.md"), cwd=home, env=environment)
    _run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=League Canary",
            "-c",
            "user.email=12794431+Vinosaamaa@users.noreply.github.com",
            "commit",
            "--no-gpg-sign",
            "-m",
            "Seed disposable cleanup canary",
        ),
        cwd=home,
        env=environment,
    )
    head = _run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        cwd=home,
        env=environment,
    ).stdout.strip()
    branch = "canary/issue-23-cleanup"
    _run(
        (
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            head,
        ),
        cwd=home,
        env=environment,
    )
    return {
        "repository": str(repository),
        "worktree": str(worktree),
        "branch": branch,
        "head": head,
        "base_ref": "main",
        "merge_commit": head,
    }


def _create_repository_artifact_canary(
    home: Path, source_root: Path, namespace: str
) -> dict[str, str]:
    repository, worktree, environment = _disposable_git_scope(home)
    remote = _run(
        ("git", "-C", str(source_root), "remote", "get-url", "origin"),
        cwd=home,
        env=environment,
    ).stdout.strip()
    if remote.removesuffix(".git") != REPORT_REPOSITORY:
        raise StorageRefusal(
            "real_canary_repository_mismatch",
            "source root is not the repository bound to the artifact receipt",
        )
    _run(("git", "init", str(repository)), cwd=home, env=environment)
    _write_json(
        home / "failure-scope.json",
        {
            "schema": "league.real-canary-failure-scope.v1",
            "namespace": namespace,
            "git": {
                "repository": str(repository),
                "worktree": str(worktree),
                "branch": REPORT_BRANCH,
                "head": REPORT_TESTED_HEAD,
            }
        },
    )
    for commit in (REPORT_TESTED_HEAD, REPORT_MERGE_COMMIT):
        _run(
            (
                "git",
                "-C",
                str(repository),
                "fetch",
                "--no-tags",
                "--depth=1",
                str(source_root),
                commit,
            ),
            cwd=home,
            env=environment,
        )
    tested_tree = _run(
        ("git", "-C", str(repository), "rev-parse", f"{REPORT_TESTED_HEAD}^{{tree}}"),
        cwd=home,
        env=environment,
    ).stdout.strip()
    merge_tree = _run(
        ("git", "-C", str(repository), "rev-parse", f"{REPORT_MERGE_COMMIT}^{{tree}}"),
        cwd=home,
        env=environment,
    ).stdout.strip()
    if tested_tree != merge_tree:
        raise StorageRefusal(
            "real_canary_merge_mismatch",
            "repository artifact tested and squash-merge trees differ",
        )
    _run(
        (
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-b",
            REPORT_BRANCH,
            str(worktree),
            REPORT_TESTED_HEAD,
        ),
        cwd=home,
        env=environment,
    )
    report = worktree / REPORT_PATH
    if not report.is_file() or report.is_symlink():
        raise StorageRefusal(
            "real_canary_artifact_missing", "repository-owned report is missing"
        )
    tested_bytes = report.read_bytes()
    merged_bytes = _run(
        (
            "git",
            "-C",
            str(repository),
            "show",
            f"{REPORT_MERGE_COMMIT}:{REPORT_PATH}",
        ),
        cwd=home,
        env=environment,
    ).stdout.encode("utf-8")
    if tested_bytes != merged_bytes:
        raise StorageRefusal(
            "real_canary_artifact_mismatch",
            "repository-owned report bytes differ after merge",
        )
    return {
        "repository": str(repository),
        "worktree": str(worktree),
        "branch": REPORT_BRANCH,
        "head": REPORT_TESTED_HEAD,
        "base_ref": REPORT_MERGE_COMMIT,
        "merge_commit": REPORT_MERGE_COMMIT,
        "tested_tree": tested_tree,
        "merge_tree": merge_tree,
        "artifact_sha256": _sha256(tested_bytes),
    }


def _create_herdr_canary(home: Path, worktree: Path, namespace: str) -> dict[str, str]:
    name = f"l23{hashlib.sha256(namespace.encode('utf-8')).hexdigest()[:12]}"
    repository = worktree.parent / "repository"
    if not repository.is_dir() or repository.is_symlink():
        raise StorageRefusal("real_canary_scope_refused", "disposable repository identity changed")
    trust_override = (
        f"projects.{json.dumps(str(repository))}.trust_level=\"trusted\""
    )
    if _exact_agent(home, name) is not None:
        raise StorageRefusal("real_canary_name_conflict", "Herdr canary name is already active")
    split = _herdr(
        (
            "pane",
            "split",
            "--current",
            "--direction",
            "right",
            "--ratio",
            "0.30",
            "--cwd",
            str(worktree),
            "--no-focus",
        ),
        home,
    )
    split_result = split.get("result", {})
    pane = split_result.get("pane", {}) if isinstance(split_result, dict) else {}
    pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
    if not isinstance(pane_id, str) or not pane_id:
        raise StorageRefusal("real_canary_output_invalid", "Herdr split receipt lacks a pane id")
    failure_scope_path = home / "failure-scope.json"
    failure_scope = json.loads(failure_scope_path.read_text(encoding="utf-8"))
    failure_scope["herdr"] = {
        "agent_name": name,
        "workspace_id": pane_id.split(":", 1)[0],
        "pane_id": pane_id,
    }
    _write_json(failure_scope_path, failure_scope)
    _herdr(
        (
            "agent",
            "start",
            name,
            "--kind",
            "codex",
            "--pane",
            pane_id,
            "--timeout",
            "120000",
            "--",
            "--model",
            "gpt-5.6-sol",
            "--config",
            'model_reasoning_effort="high"',
            "--config",
            trust_override,
        ),
        home,
        timeout=150,
    )
    read_arguments = (
        "herdr",
        "pane",
        "read",
        pane_id,
        "--source",
        "recent-unwrapped",
        "--lines",
        "160",
        "--format",
        "text",
    )
    prompt_arguments = (
        "herdr",
        "agent",
        "prompt",
        name,
        "Reply exactly LEAGUE23_CANARY_READY. Do not edit files or run commands.",
        "--wait",
        "--timeout",
        str(READINESS_WAIT_MILLISECONDS),
    )
    observed: subprocess.CompletedProcess[str] | None = None
    for attempt in range(READINESS_MAX_OBSERVATIONS):
        prompted = _run(
            prompt_arguments,
            cwd=home,
            allowed=frozenset({0, 1}),
            timeout=45,
        )
        prompt_receipt = _json_result(prompted, "Herdr prompt")
        error_code = prompt_receipt.get("error", {}).get("code")
        if prompted.returncode != 0 and error_code not in {
            "agent_prompt_stalled",
            "timeout",
        }:
            raise StorageRefusal(
                "real_canary_command_failed", "Herdr readiness prompt was not accepted"
            )
        if prompted.returncode == 0:
            _run(
                (
                    "herdr", "pane", "wait-output", pane_id,
                    "--match", "LEAGUE23_CANARY_READY",
                    "--source", "recent-unwrapped", "--lines", "160",
                    "--timeout", "15000",
                ),
                cwd=home,
                allowed=frozenset({0, 1}),
                timeout=20,
            )
        observed = _run(read_arguments, cwd=home)
        if "LEAGUE23_CANARY_READY" in observed.stdout:
            break
        if attempt == 0 and error_code == "agent_prompt_stalled":
            time.sleep(1)
            continue
        break
    if observed is None:
        raise StorageRefusal("real_canary_readiness_unproven", "Codex readiness was not observed")
    clean = _run(
        ("git", "-C", str(worktree), "status", "--porcelain"), cwd=home
    ).stdout == ""
    head = _run(
        ("git", "-C", str(worktree), "rev-parse", "HEAD"), cwd=home
    ).stdout.strip()
    branch = _run(
        ("git", "-C", str(worktree), "branch", "--show-current"), cwd=home
    ).stdout.strip()
    readiness = {
        "token_observed": "LEAGUE23_CANARY_READY" in observed.stdout,
        "route_observed": "gpt-5.6-sol high" in observed.stdout,
        "worktree_clean": clean,
        "head_exact": head == REPORT_TESTED_HEAD,
        "branch_exact": branch == REPORT_BRANCH,
    }
    _write_json(home / "readiness-receipt.json", readiness)
    if not all(readiness.values()):
        raise StorageRefusal(
            "real_canary_readiness_unproven",
            "Codex readiness, requested route, or clean state was not observed",
        )
    agent = _exact_agent(home, name)
    if agent is None or agent.get("agent") != "codex" or agent.get("pane_id") != pane_id:
        raise StorageRefusal("real_canary_identity_mismatch", "Herdr Codex canary identity changed")
    values = {
        "agent_name": name,
        "workspace_id": agent.get("workspace_id"),
        "pane_id": pane_id,
        "terminal_id": agent.get("terminal_id"),
        "session_id": _codex_session_id(agent),
    }
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise StorageRefusal("real_canary_identity_mismatch", "Herdr Codex canary receipt is incomplete")
    values["runtime_instance_id"] = f"runtime:{name}"
    values["runtime_generation"] = "herdr:" + _sha256(
        f"{values['terminal_id']}\0{values['session_id']}".encode("utf-8")
    )[:24]
    return values


class _RealLaunchReceipt:
    def __init__(self, herdr: Mapping[str, str]) -> None:
        self.herdr = dict(herdr)

    def launch(self, specification: AssignmentSpec) -> dict[str, Any]:
        return {
            "verified": True,
            "assignment_id": specification.assignment_id,
            "task_id": specification.task_id,
            "champion_agent_id": specification.champion_agent_id,
            "callsign": specification.callsign,
            "runtime_instance_id": self.herdr["runtime_instance_id"],
            "thread_id": self.herdr["session_id"],
            "endpoint": self.herdr["pane_id"],
            "runtime_generation": self.herdr["runtime_generation"],
            "harness_kind": "codex",
            "backend_kind": "herdr",
            "routing_name": str(specification.callsign).lower(),
            "display_agent": "codex",
            "repository": specification.repository,
            "issue": specification.issue,
            "branch": specification.branch,
            "worktree": specification.worktree,
            "capabilities": list(specification.required_capabilities),
        }


def _cli_environment(home: Path) -> dict[str, str]:
    process_home = home / "process-home"
    pycache = home / "pycache"
    pycache.mkdir(mode=0o700, exist_ok=True)
    if not pycache.is_dir() or pycache.is_symlink():
        raise StorageRefusal("real_canary_scope_refused", "canary Python cache identity changed")
    return {**os.environ, "HOME": str(process_home), "PYTHONPYCACHEPREFIX": str(pycache)}


def _league(
    source_root: Path,
    home: Path,
    arguments: Sequence[str],
    *,
    allowed: frozenset[int] = frozenset({0}),
) -> tuple[int, dict[str, Any]]:
    result = _run(
        (str(source_root / "bin/league"), *arguments),
        cwd=source_root,
        env=_cli_environment(home),
        allowed=allowed,
    )
    return result.returncode, _json_result(result, "League CLI")


def _setup_sqlite(
    home: Path,
    source_root: Path,
    git: Mapping[str, str],
    herdr: Mapping[str, str],
    issue_spec_resolver: Callable[
        [SQLiteStorage, AssignmentSpec, str], AssignmentSpec
    ]
    | None = None,
) -> dict[str, Any]:
    clock = _Clock()
    ids = _Ids()
    delivery = _DeliveryDouble()
    store = _seed_synthetic_store(home / "league", source_root)
    try:
        store.register_runtime(
            RuntimeRegistrationCommand(
                runtime_instance_id="runtime:real-canary-shotcaller",
                actor_agent_id=SHOTCALLER_ID,
                harness_kind="codex",
                backend_kind="herdr",
                session_ref="session:real-canary-shotcaller",
                endpoint="isolated:shotcaller",
                runtime_generation="generation:real-canary-shotcaller",
                status="active",
                verified=True,
                at=clock.now(),
            )
        )
        roster = store.roster_snapshot(
            as_of=clock.now(),
            recent_since="2025-01-01T00:00:00Z",
            stale_before="2025-01-01T00:00:00Z",
            visibility="local",
        )
        imported_champions: dict[str, dict[str, Any]] = {}
        fixture_ids = _FixtureSettlementIds()
        for project in roster["projects"]:
            for group in project["groups"].values():
                for item in group:
                    if item.get("kind") == "agent" and item.get("role") == "champion":
                        imported_champions[str(item["agent_id"])] = item
                    for agent in item.get("agents", []):
                        if agent.get("role") == "champion":
                            imported_champions[str(agent["agent_id"])] = agent
        for imported_id in sorted(imported_champions):
            if imported_id == CHAMPION_ID:
                continue
            imported = store.agent_status(imported_id)
            if imported is None or imported["status"] in {
                "completed", "complete", "cancelled", "canceled", "failed"
            }:
                continue
            settled = store.transition(
                imported_id,
                int(imported["version"]),
                "completed",
                "Synthetic pre-existing fixture settled before isolated canary.",
                clock.now(),
            )
            DeliveryService(
                store,
                delivery,
                clock,
                fixture_ids,
                dispatcher_id=f"dispatcher:fixture-settlement:{imported_id}",
            ).dispatch_source(
                settled["outbox_id"], settled["event_id"], settled["recipient_agent_id"]
            )
        store.intake_prompt(
            "prompt:real-cleanup-canary",
            SHOTCALLER_ID,
            "runtime:real-canary-shotcaller",
            "codex",
            "session:real-canary-shotcaller",
            "source:real-cleanup-canary",
            "Run the real disposable cleanup canary.",
            clock.now(),
        )
        store.triage_prompt(
            "prompt:real-cleanup-canary",
            [
                {
                    "prompt_item_id": "prompt-item:real-cleanup-canary",
                    "ordinal": 1,
                    "summary": "Run the real disposable cleanup canary",
                    "disposition": "new_request",
                    "request_id": "request:real-cleanup-canary",
                }
            ],
            clock.now(),
        )
        store.claim_request(
            "request:real-cleanup-canary",
            "runtime:real-canary-shotcaller",
            "claim:real-cleanup-canary",
            clock.after(600),
            clock.now(),
        )
        dispatch = store.dispatch_request(
            DispatchRequestCommand(
                request_id="request:real-cleanup-canary",
                claim_token="claim:real-cleanup-canary",
                dispatch_id="dispatch:real-cleanup-canary",
                work_kind="repository-write",
                requested_mode="champion",
                hidden_supported=False,
                requested_model="real-codex-canary",
                requested_effort="low",
                explicit_route="DisposableChampion",
                at=clock.now(),
                orchestration=OrchestrationSignals(False, False, False, 0, 0),
            )
        )
        assignment_spec = AssignmentSpec(
                assignment_id="assignment:real-cleanup-canary",
                request_id="request:real-cleanup-canary",
                claim_token="claim:real-cleanup-canary",
                task_id=LIFECYCLE_TASK_ID,
                task_summary="Real disposable cleanup canary",
                coordinator_agent_id=SHOTCALLER_ID,
                champion_agent_id=CHAMPION_ID,
                callsign="Lux",
                repository=CANARY_ISSUE_REPOSITORY,
                issue=23,
                branch=git["branch"],
                worktree=git["worktree"],
                issue_receipt=None,
            )
        bound_spec = (
            _owner_verified_issue_spec(
                store, Path(git["worktree"]), assignment_spec, clock.now()
            )
            if issue_spec_resolver is None
            else issue_spec_resolver(store, assignment_spec, clock.now())
        )
        assignment = AssignmentService(store, _RealLaunchReceipt(herdr), clock, ids).assign(
            bound_spec
        )
        if assignment.get("state") != "active":
            raise StorageRefusal("real_canary_assignment_failed", "real Codex assignment did not activate")
        available_at = store.connection.execute(
            "SELECT available_at FROM delivery_outbox WHERE outbox_id=?",
            (assignment["outbox_id"],),
        ).fetchone()[0]
        DeliveryService(
            store,
            delivery,
            _Clock(str(available_at)),
            ids,
            dispatcher_id="dispatcher:real-cleanup-canary",
        ).dispatch_source(assignment["outbox_id"], assignment["event_id"], CHAMPION_ID)
        return {"dispatch": dispatch, "assignment": assignment}
    finally:
        store.close()


def _settle_transition_and_request(
    state: Path,
    transition: Mapping[str, Any],
    dispatch: Mapping[str, Any],
) -> dict[str, Any]:
    ids = _Ids()
    delivery = _DeliveryDouble()
    with SQLiteStorage(state, request_wal=False) as store:
        available_at = store.connection.execute(
            "SELECT available_at FROM delivery_outbox WHERE outbox_id=?",
            (transition["outbox_id"],),
        ).fetchone()[0]
        clock = _Clock(str(available_at))
        DeliveryService(
            store, delivery, clock, ids, dispatcher_id="dispatcher:real-transition"
        ).dispatch_source(
            transition["outbox_id"], transition["event_id"], SHOTCALLER_ID
        )
        result = store.record_request_result(
            RequestResultCommand(
                request_id="request:real-cleanup-canary",
                claim_token="claim:real-cleanup-canary",
                expected_version=dispatch["request_version"],
                result_id="result:real-cleanup-canary",
                idempotency_key="result-key:real-cleanup-canary",
                outcome="success",
                summary="Real disposable Champion reached a terminal transition",
                task_ids=(LIFECYCLE_TASK_ID,),
                at=clock.now(),
                return_to_requester=False,
                event_id=None,
                outbox_id=None,
            )
        )
        store.answer_request(
            AnswerRequestCommand(
                request_id="request:real-cleanup-canary",
                claim_token="claim:real-cleanup-canary",
                expected_version=result["version"],
                response_ref_id="response:real-cleanup-canary",
                adapter_kind="codex",
                session_locator="session:real-cleanup-canary",
                response_locator="response:real-cleanup-canary",
                durability="durable",
                content_hash=_sha256(b"real-cleanup-canary-response"),
                resolution_summary="Real cleanup canary request settled",
                event_id="event:real-cleanup-canary-answered",
                at=clock.now(),
            )
        )
        obligation = store.connection.execute(
            "SELECT * FROM cleanup_obligations WHERE task_id=?", (LIFECYCLE_TASK_ID,)
        ).fetchone()
        event_count = store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_id=?", (transition["event_id"],)
        ).fetchone()[0]
        outbox_count = store.connection.execute(
            "SELECT COUNT(*) FROM delivery_outbox WHERE outbox_id=?", (transition["outbox_id"],)
        ).fetchone()[0]
        wait_stop = store.stop_decision(
            "scope:real-cleanup-canary",
            SHOTCALLER_ID,
            "terminal:real-cleanup-canary:wait",
            clock.now(),
        )
        store.rearm_wait(
            "scope:real-cleanup-canary",
            SHOTCALLER_ID,
            "event:real-cleanup-canary:end-attempt",
            clock.now(),
        )
        end_stop = store.stop_decision(
            "scope:real-cleanup-canary",
            SHOTCALLER_ID,
            "terminal:real-cleanup-canary:end",
            clock.now(),
        )
        expected_obligations = {
            "active_champions": 0,
            "pending_assignments": 0,
            "unresolved_requests": 0,
            "pending_deliveries": 0,
            "cleanup_obligations": 1,
        }
        if (
            obligation is None
            or obligation["cleanup_state"] != "pending"
            or event_count != 1
            or outbox_count != 1
            or wait_stop.get("decision") != "block"
            or end_stop.get("decision") != "block"
            or wait_stop.get("obligations") != expected_obligations
            or end_stop.get("obligations") != expected_obligations
        ):
            raise StorageRefusal(
                "real_canary_stop_gate_failed",
                "cleanup was not the sole remaining Stop obligation: "
                f"wait={wait_stop.get('obligations')!r}, end={end_stop.get('obligations')!r}",
            )
        return {
            "cleanup_state": obligation["cleanup_state"],
            "cleanup_obligation_id": obligation["cleanup_obligation_id"],
            "event_count": event_count,
            "outbox_count": outbox_count,
            "hook_decision": wait_stop["decision"],
            "obligations": wait_stop["obligations"],
            "wait_safe": wait_stop["decision"] != "block",
            "end_safe": end_stop["decision"] != "block",
        }


def _cleanup_files(
    home: Path,
    git: Mapping[str, str],
    herdr: Mapping[str, str],
    assignment_id: str,
    callsign: str,
) -> tuple[Path, Path]:
    identity = {
        "task_id": LIFECYCLE_TASK_ID,
        "owner_id": CHAMPION_ID,
        "runtime_generation": herdr["runtime_generation"],
        "session_id": herdr["session_id"],
        "pane_id": herdr["pane_id"],
        "git_head": git["head"],
    }
    proof = {
        "identity": {"exact": True},
        "endpoint": {"terminal_or_idle": True},
        "git": {"exact_registration": True, "clean": True, "no_unpublished": True},
        "decision": {"explicit": True},
        "publication": {
            "exact_head": False,
            "ci_green": False,
            "integrated": False,
            "applicability": "not_applicable_to_local_disposable_canary",
        },
    }
    manifest = {
        "task_id": LIFECYCLE_TASK_ID,
        "owner": {"id": CHAMPION_ID, "role": "champion", "persistent": False},
        "task_class": "local_git",
        "disposition": "completed",
        "pending_decisions_clear": True,
        "expected_cleanup_version": 1,
        "identity": identity,
        "legacy_identity": dict(identity),
        "proof": proof,
        "resources": [],
        "final_actions": [
            {
                "action_kind": "session_exit",
                "adapter_kind": "harness",
                "expected_identity": {
                    "agent_name": herdr["agent_name"],
                    "pane_id": herdr["pane_id"],
                    "session_id": herdr["session_id"],
                },
                "intended_state": {"completed": True, "action": "session_exit"},
            },
            {
                "action_kind": "endpoint_close",
                "adapter_kind": "backend",
                "expected_identity": {
                    "pane_id": herdr["pane_id"],
                    "terminal_id": herdr["terminal_id"],
                    "runtime_instance_id": herdr["runtime_instance_id"],
                    "runtime_generation": herdr["runtime_generation"],
                },
                "intended_state": {"completed": True, "action": "endpoint_close"},
            },
            {
                "action_kind": "worktree_remove",
                "adapter_kind": "git",
                "expected_identity": {
                    key: git[key] for key in ("repository", "worktree", "branch", "head")
                },
                "intended_state": {"completed": True, "action": "worktree_remove"},
            },
            {
                "action_kind": "branch_delete",
                "adapter_kind": "git",
                "expected_identity": {
                    key: git[key]
                    for key in (
                        "repository",
                        "branch",
                        "head",
                        "base_ref",
                        "merge_commit",
                    )
                },
                "intended_state": {"completed": True, "action": "branch_delete"},
            },
            {
                "action_kind": "callsign_release",
                "adapter_kind": "callsign",
                "expected_identity": {
                    "assignment_id": assignment_id,
                    "callsign": callsign,
                    "expected_version": 2,
                },
                "intended_state": {"completed": True, "action": "callsign_release"},
            },
        ],
    }
    adapter = {
        "schema": ADAPTER_SCHEMA,
        "scope": "disposable-canary",
        "temporary_root": str(home),
        "archive_path": str(home / "archive/identity-evidence.json"),
        "herdr": dict(herdr),
        "git": {
            key: git[key]
            for key in (
                "repository",
                "worktree",
                "branch",
                "head",
                "base_ref",
                "merge_commit",
            )
        },
        "callsign": {
            "assignment_id": assignment_id,
            "callsign": callsign,
            "expected_version": 2,
        },
    }
    manifest_path = home / "cleanup-manifest.json"
    adapter_path = home / "cleanup-adapters.json"
    _write_json(manifest_path, manifest)
    _write_json(adapter_path, adapter)
    return manifest_path, adapter_path


def _artifact_files(home: Path, git: Mapping[str, str]) -> tuple[Path, Path]:
    declaration = {
        "artifact_id": REPORT_ARTIFACT_ID,
        "task_id": LIFECYCLE_TASK_ID,
        "name": "2026-08-28 overnight League delivery report",
        "classification": "repository_owned",
        "repository": REPORT_REPOSITORY,
        "issue": REPORT_ISSUE,
        "worktree": git["worktree"],
        "branch": git["branch"],
        "repository_path": REPORT_PATH,
    }
    publication = {
        "pull_request_number": REPORT_PULL_REQUEST,
        "pull_request_url": REPORT_PULL_REQUEST_URL,
        "tested_head": REPORT_TESTED_HEAD,
        "merge_receipt": {
            "commit": REPORT_MERGE_COMMIT,
            "url": f"{REPORT_REPOSITORY}/commit/{REPORT_MERGE_COMMIT}",
            "merged_at": REPORT_MERGED_AT,
        },
    }
    declaration_path = home / "repository-artifact-declaration.json"
    publication_path = home / "repository-artifact-publication.json"
    _write_json(declaration_path, declaration)
    _write_json(publication_path, publication)
    return declaration_path, publication_path


def _final_verification(
    state: Path,
    home: Path,
    git: Mapping[str, str],
    herdr: Mapping[str, str],
    operation_id: str,
) -> dict[str, Any]:
    with SQLiteStorage(state, request_wal=False) as store:
        operation = store.cleanup_operation(operation_id)
        if operation is None:
            raise StorageRefusal("real_canary_cleanup_missing", "cleanup operation disappeared")
        actions = operation["actions"]
        rows = list(
            store.connection.execute(
                "SELECT action_id,outcome,receipt_hash FROM cleanup_action_receipts WHERE operation_id=? ORDER BY action_id",
                (operation_id,),
            )
        )
        teardown = store.connection.execute(
            "SELECT * FROM teardown_receipts WHERE operation_id=?", (operation_id,)
        ).fetchone()
        obligation = store.connection.execute(
            "SELECT cleanup_state FROM cleanup_obligations WHERE task_id=?", (LIFECYCLE_TASK_ID,)
        ).fetchone()
        callsign = store.connection.execute(
            "SELECT state FROM callsign_assignments WHERE callsign_assignment_id=?",
            ("callsign-assignment:assignment:real-cleanup-canary",),
        ).fetchone()
        artifact = store.connection.execute(
            "SELECT state,tested_head,merge_commit FROM repository_artifacts WHERE artifact_id=?",
            (REPORT_ARTIFACT_ID,),
        ).fetchone()
        after = store.stop_decision(
            "scope:real-cleanup-canary",
            SHOTCALLER_ID,
            "terminal:real-cleanup-canary:after",
            RESUME_AT,
        )
        wait = store.unresolved_requests(SHOTCALLER_ID, before_action="wait")
        end = store.unresolved_requests(SHOTCALLER_ID, before_action="end")
        expected_order = [
            "archive_identity_evidence",
            "session_exit",
            "endpoint_close",
            "worktree_remove",
            "branch_delete",
            "callsign_release",
        ]
        if (
            operation["state"] != "completed"
            or [item["action_kind"] for item in actions] != expected_order
            or len(rows) != len(actions)
            or teardown is None
            or obligation is None
            or obligation["cleanup_state"] != "cleanup_completed"
            or callsign is None
            or callsign["state"] != "released"
            or artifact is None
            or tuple(artifact)
            != ("published", REPORT_TESTED_HEAD, REPORT_MERGE_COMMIT)
            or after.get("decision") != "allow"
            or any(after.get("obligations", {}).values())
            or wait.get("safe_to_finish") is not True
            or end.get("safe_to_finish") is not True
        ):
            raise StorageRefusal("real_canary_final_verification_failed", "real cleanup did not settle exactly")
        branch = _run(
            (
                "git",
                "-C",
                git["repository"],
                "for-each-ref",
                "--format=%(objectname)",
                f"refs/heads/{git['branch']}",
            ),
            cwd=home,
        )
        if (
            Path(git["worktree"]).exists()
            or bool(branch.stdout.strip())
            or not Path(git["repository"]).is_dir()
            or _exact_agent(home, herdr["agent_name"]) is not None
        ):
            raise StorageRefusal("real_canary_scope_verification_failed", "a canary resource was not exact-cleaned")
        return {
            "cleanup_state": obligation["cleanup_state"],
            "action_order": expected_order,
            "action_receipt_count": len(rows),
            "action_receipt_hashes": [row["receipt_hash"] for row in rows],
            "teardown_receipt_hash": teardown["receipt_hash"],
            "callsign_state": callsign["state"],
            "worktree_removed": True,
            "branch_removed": True,
            "repository_preserved": True,
            "exact_endpoint_closed": True,
            "hook_decision": after["decision"],
            "wait_safe": wait["safe_to_finish"],
            "end_safe": end["safe_to_finish"],
        }


def _real_canary_paths(
    temporary_root: Path, namespace: str, source_root: Path | None
) -> tuple[Path, Path, Path]:
    root = Path(temporary_root)
    if (
        not root.is_absolute()
        or not root.is_dir()
        or root.is_symlink()
        or root.resolve() == Path("/")
    ):
        raise StorageRefusal("invalid_temporary_root", "real canary requires an explicit directory")
    if not NAMESPACE_PATTERN.fullmatch(namespace):
        raise StorageRefusal("invalid_namespace", "real canary namespace is invalid")
    source = (source_root or Path(__file__).resolve().parents[2]).resolve()
    if (
        not (source / "bin/league").is_file()
        or not (source / "tests/storage_fixture.py").is_file()
    ):
        raise StorageRefusal("invalid_source_root", "real canary source root is incomplete")
    home = root.resolve() / f"league-real-canary-{namespace}"
    try:
        home.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise StorageRefusal("namespace_collision", "real canary namespace already exists") from exc
    return root.resolve(), source, home


def _prepare_real_canary(home: Path, source: Path, namespace: str) -> dict[str, Any]:
    git = _create_repository_artifact_canary(home, source, namespace)
    scope = {
        "schema": "league.real-canary-failure-scope.v1",
        "namespace": namespace,
        "git": git,
    }
    _write_json(home / "failure-scope.json", scope)
    herdr = _create_herdr_canary(home, Path(git["worktree"]), namespace)
    _write_json(home / "failure-scope.json", {**scope, "herdr": herdr})
    _write_json(home / "setup-receipt.json", {"git": git, "herdr": herdr})
    setup = _setup_sqlite(home, source, git, herdr)
    declaration, publication = _artifact_files(home, git)
    return {
        "git": git,
        "herdr": herdr,
        "setup": setup,
        "state": home / "league/state",
        "artifact_declaration": declaration,
        "artifact_publication": publication,
    }


def _failure_scope(home: Path) -> tuple[str, dict[str, Any]] | None:
    scope_path = home / "failure-scope.json"
    if not scope_path.is_file() or scope_path.is_symlink():
        return None
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    if not isinstance(scope, dict) or set(scope) not in (
        {"schema", "namespace", "git"},
        {"schema", "namespace", "git", "herdr"},
    ) or not isinstance(scope.get("git"), Mapping) or (
        "herdr" in scope and not isinstance(scope.get("herdr"), Mapping)
    ):
        raise StorageRefusal(
            "real_canary_failure_cleanup_refused",
            "failed canary cleanup scope is malformed",
        )
    namespace = scope.get("namespace")
    if (
        scope.get("schema") != "league.real-canary-failure-scope.v1"
        or not isinstance(namespace, str)
        or not NAMESPACE_PATTERN.fullmatch(namespace)
        or home.name != f"league-real-canary-{namespace}"
    ):
        raise StorageRefusal(
            "real_canary_failure_cleanup_refused",
            "failed canary cleanup scope identity changed",
        )
    return str(namespace), scope


def _cleanup_failed_herdr(
    home: Path, namespace: str, herdr: Mapping[str, Any]
) -> None:
    agent_name = herdr.get("agent_name")
    pane_id = herdr.get("pane_id")
    workspace_id = herdr.get("workspace_id")
    if not all(
        isinstance(value, str) and value
        for value in (agent_name, pane_id, workspace_id)
    ):
        raise StorageRefusal(
            "real_canary_failure_cleanup_refused",
            "failed canary Herdr identity is incomplete",
        )
    expected_agent = f"l23{hashlib.sha256(namespace.encode('utf-8')).hexdigest()[:12]}"
    if agent_name != expected_agent:
        raise StorageRefusal(
            "real_canary_failure_cleanup_refused", "failed canary agent name changed"
        )
    agent = _exact_agent(home, str(agent_name))
    if agent is not None and agent.get("pane_id") != pane_id:
        raise StorageRefusal(
            "real_canary_failure_cleanup_refused",
            "failed canary agent endpoint identity changed",
        )
    if agent is not None and agent.get("agent_status") != "done":
        remaining = agent
        for attempt in range(2):
            _run(
                (
                    "herdr", "agent", "prompt", str(agent_name), "/exit",
                    "--wait", "--timeout", "30000",
                ),
                cwd=home,
                allowed=frozenset({0, 1}),
                timeout=45,
            )
            remaining = _exact_agent(home, str(agent_name))
            if remaining is None or remaining.get("agent_status") == "done":
                break
            if attempt == 0:
                time.sleep(1)
        if remaining is not None and remaining.get("agent_status") != "done":
            raise StorageRefusal(
                "real_canary_failure_cleanup_refused",
                "failed canary agent did not terminate",
            )
    pane_inventory = _herdr(("pane", "list", "--workspace", str(workspace_id)), home)
    panes = pane_inventory.get("result", {}).get("panes", [])
    if not isinstance(panes, list) or any(
        not isinstance(item, Mapping) for item in panes
    ):
        raise StorageRefusal(
            "real_canary_failure_cleanup_refused",
            "failed canary pane inventory is malformed",
        )
    matches = [item for item in panes if item.get("pane_id") == pane_id]
    if len(matches) > 1:
        raise StorageRefusal(
            "real_canary_failure_cleanup_refused",
            "failed canary pane identity is ambiguous",
        )
    if matches:
        _herdr(("pane", "close", str(pane_id)), home)


def _cleanup_failed_git(home: Path, git: Mapping[str, Any]) -> None:
    repository = Path(str(git.get("repository", "")))
    worktree = Path(str(git.get("worktree", "")))
    branch = git.get("branch")
    head = git.get("head")
    if not (
        repository.resolve(strict=False) == (home / "git/repository").resolve(strict=False)
        and worktree.resolve(strict=False) == (home / "git/worktree").resolve(strict=False)
        and repository.is_dir()
        and isinstance(branch, str)
        and isinstance(head, str)
        and re.fullmatch(r"[0-9a-f]{40}", head)
        and branch in {REPORT_BRANCH, "canary/issue-23-cleanup"}
    ):
        raise StorageRefusal(
            "real_canary_failure_cleanup_refused",
            "failed canary Git identity is incomplete",
        )
    if worktree.exists():
        status = _run(("git", "-C", str(worktree), "status", "--porcelain"), cwd=home)
        observed_head = _run(("git", "-C", str(worktree), "rev-parse", "HEAD"), cwd=home)
        observed_branch = _run(
            ("git", "-C", str(worktree), "branch", "--show-current"), cwd=home
        )
        if (
            status.stdout
            or observed_head.stdout.strip() != head
            or observed_branch.stdout.strip() != branch
        ):
            raise StorageRefusal(
                "real_canary_failure_cleanup_refused",
                "failed canary worktree is not clean and exact",
            )
        _run(
            ("git", "-C", str(repository), "worktree", "remove", str(worktree)),
            cwd=home,
        )
    ref = _run(
        (
            "git", "-C", str(repository), "for-each-ref", "--format=%(objectname)",
            f"refs/heads/{branch}",
        ),
        cwd=home,
    ).stdout.strip()
    if ref and ref != head:
        raise StorageRefusal(
            "real_canary_failure_cleanup_refused", "failed canary branch head changed"
        )
    if ref:
        _run(
            (
                "git", "-C", str(repository), "update-ref", "-d",
                f"refs/heads/{branch}", head,
            ),
            cwd=home,
        )


def _cleanup_failed_canary(home: Path) -> None:
    """Remove only exact active resources recorded by this disposable run."""

    validated = _failure_scope(home)
    if validated is None:
        return
    namespace, scope = validated
    if isinstance(scope.get("herdr"), Mapping):
        _cleanup_failed_herdr(home, namespace, scope["herdr"])
    if isinstance(scope.get("git"), Mapping):
        _cleanup_failed_git(home, scope["git"])


def _complete_real_canary_task(
    home: Path, source: Path, prepared: Mapping[str, Any]
) -> dict[str, Any]:
    state = Path(prepared["state"])
    herdr = prepared["herdr"]
    _, declared_envelope = _league(
        source,
        home,
        (
            "--state-root", str(state), "--no-wal", "artifact", "declare",
            "--input", str(prepared["artifact_declaration"]),
            "--at", "2026-01-01T01:00:30Z",
        ),
    )
    declared = declared_envelope.get("result", {})
    if declared.get("state") != "pending":
        raise StorageRefusal(
            "real_canary_artifact_declaration_failed",
            "repository-owned report declaration did not become pending",
        )
    _, transition_envelope = _league(
        source,
        home,
        (
            "--state-root", str(state), "--no-wal", "task", "transition",
            "--task-id", LIFECYCLE_TASK_ID,
            "--runtime-instance-id", herdr["runtime_instance_id"],
            "--expected-version", "3", "--state", "completed",
            "--update", "Real disposable Champion completed",
            "--next-action", "Automatically execute exact cleanup",
            "--transition-id", "transition:real-cleanup-canary",
            "--transition-key", "transition-key:real-cleanup-canary",
            "--event-id", "event:real-cleanup-canary-completed",
            "--outbox-id", "outbox:real-cleanup-canary-completed",
            "--recipient-agent-id", SHOTCALLER_ID, "--at", TRANSITION_AT,
        ),
    )
    if transition_envelope.get("ok") is not True:
        raise StorageRefusal("real_canary_transition_failed", "terminal CLI transition failed")
    before = _settle_transition_and_request(
        state, transition_envelope["result"], prepared["setup"]["dispatch"]
    )
    assignment = prepared["setup"]["assignment"]
    manifest_path, adapter_path = _cleanup_files(
        home,
        prepared["git"],
        herdr,
        f"callsign-assignment:{assignment['assignment_id']}",
        "Lux",
    )
    return {
        "declared": declared,
        "before": before,
        "manifest_path": manifest_path,
        "adapter_path": adapter_path,
    }


def _publish_real_canary_artifact(
    home: Path,
    source: Path,
    prepared: Mapping[str, Any],
    task: Mapping[str, Any],
    operation_id: str,
) -> dict[str, Any]:
    state = Path(prepared["state"])
    pending_code, pending_cleanup = _league(
        source,
        home,
        (
            "--state-root", str(state), "--no-wal", "cleanup", "reconcile",
            "--manifest", str(task["manifest_path"]), "--operation-id", operation_id,
            "--adapter-config", str(task["adapter_path"]),
            "--executor-id", "executor:real-cleanup-canary:publication-pending",
            "--leased-until", "2026-01-01T01:02:00Z",
            "--at", "2026-01-01T01:01:30Z",
        ),
        allowed=frozenset({2}),
    )
    if (
        pending_code != 2
        or pending_cleanup.get("error", {}).get("code")
        != "repository_publication_unresolved"
    ):
        raise StorageRefusal(
            "real_canary_artifact_gate_failed",
            "cleanup did not refuse the pending repository publication",
        )
    _, published_envelope = _league(
        source,
        home,
        (
            "--state-root", str(state), "--no-wal", "artifact", "publish",
            "--artifact-id", REPORT_ARTIFACT_ID, "--expected-version", "1",
            "--receipt", str(prepared["artifact_publication"]),
            "--at", "2026-01-01T01:01:45Z",
        ),
    )
    published = published_envelope.get("result", {})
    if (
        published.get("state") != "published"
        or published.get("tested_head") != REPORT_TESTED_HEAD
        or published.get("merge_commit") != REPORT_MERGE_COMMIT
    ):
        raise StorageRefusal(
            "real_canary_artifact_publication_failed",
            "exact repository publication receipt was not stored",
        )
    return {"pending_cleanup": pending_cleanup, "published": published}


def _execute_real_canary_cleanup(
    home: Path,
    source: Path,
    prepared: Mapping[str, Any],
    task: Mapping[str, Any],
    operation_id: str,
) -> dict[str, Any]:
    state = Path(prepared["state"])
    first_code, first = _league(
        source,
        home,
        (
            "--state-root", str(state), "--no-wal", "cleanup", "reconcile",
            "--manifest", str(task["manifest_path"]), "--operation-id", operation_id,
            "--adapter-config", str(task["adapter_path"]),
            "--executor-id", "executor:real-cleanup-canary:first",
            "--leased-until", INTERRUPT_LEASE, "--at", INTERRUPT_AT,
            "--simulate-interruption-after-archive",
        ),
        allowed=frozenset({3}),
    )
    archive_path = home / "archive/identity-evidence.json"
    with SQLiteStorage(state, request_wal=False) as store:
        interrupted = store.cleanup_operation(operation_id)
        interrupted_receipts = store.connection.execute(
            "SELECT COUNT(*) FROM cleanup_action_receipts WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
    if (
        first_code != 3
        or first.get("error", {}).get("code") != "cleanup_interrupted"
        or interrupted is None
        or interrupted["state"] != "executing"
        or interrupted["fence"] != 1
        or interrupted_receipts != 0
        or not archive_path.is_file()
    ):
        raise StorageRefusal("real_canary_interruption_failed", "cleanup interruption was not durable")
    _, resumed = _league(
        source,
        home,
        (
            "--state-root", str(state), "--no-wal", "cleanup", "reconcile",
            "--manifest", str(task["manifest_path"]), "--operation-id", operation_id,
            "--adapter-config", str(task["adapter_path"]),
            "--executor-id", "executor:real-cleanup-canary:resume",
            "--leased-until", RESUME_LEASE, "--at", RESUME_AT,
        ),
    )
    execution = resumed.get("result", {})
    if (
        resumed.get("ok") is not True
        or execution.get("automatic_after_proof") is not True
        or execution.get("execution", {}).get("state") != "cleanup_completed"
    ):
        raise StorageRefusal("real_canary_resume_failed", "automatic cleanup resume did not complete")
    return {
        "first_code": first_code,
        "first": first,
        "interrupted": interrupted,
        "interrupted_receipts": interrupted_receipts,
        "final": _final_verification(
            state, home, prepared["git"], prepared["herdr"], operation_id
        ),
    }


def _build_real_canary_receipt(
    root: Path,
    home: Path,
    namespace: str,
    prepared: Mapping[str, Any],
    task: Mapping[str, Any],
    publication: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    operation_id: str,
) -> dict[str, Any]:
    git = prepared["git"]
    herdr = prepared["herdr"]
    before = task["before"]
    final = cleanup["final"]
    first = cleanup["first"]
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "namespace": namespace,
        "mode": "real-disposable",
        "temporary_root": str(root),
        "completed_at": RESUME_AT,
        "runtime": {
            "harness": "codex",
            "backend": "herdr",
            "real_runtime": True,
            "agent_name": herdr["agent_name"],
            "session_id": herdr["session_id"],
            "pane_id": herdr["pane_id"],
            "terminal_id": herdr["terminal_id"],
        },
        "supervision": {
            "normal_wake": "event_driven",
            "readiness_wait_milliseconds": READINESS_WAIT_MILLISECONDS,
            "maximum_observations": READINESS_MAX_OBSERVATIONS,
            "periodic_unchanged_messages": 0,
            "separate_15_second_policy": False,
        },
        "repository_artifact": {
            "artifact_id": REPORT_ARTIFACT_ID,
            "classification": "repository_owned",
            "issue": REPORT_ISSUE,
            "repository_path": REPORT_PATH,
            "pull_request_number": REPORT_PULL_REQUEST,
            "pull_request_url": REPORT_PULL_REQUEST_URL,
            "tested_head": REPORT_TESTED_HEAD,
            "merge_commit": REPORT_MERGE_COMMIT,
            "tested_tree": git["tested_tree"],
            "merge_tree": git["merge_tree"],
            "tree_parity": git["tested_tree"] == git["merge_tree"],
            "artifact_sha256": git["artifact_sha256"],
            "declaration_state": task["declared"]["state"],
            "prepublication_cleanup_refusal": publication["pending_cleanup"]["error"]["code"],
            "publication_state": publication["published"]["state"],
            "publication_gate_cleared": True,
            "hosted_mutation_performed": False,
        },
        "transition": before,
        "stop_before": {
            "decision": before["hook_decision"],
            "wait_safe": before["wait_safe"],
            "end_safe": before["end_safe"],
            "cleanup_only": True,
        },
        "interruption": {
            "operation_id": operation_id,
            "first_exit": cleanup["first_code"],
            "error_code": first["error"]["code"],
            "durable_fence": cleanup["interrupted"]["fence"],
            "action_receipts_before_restart": cleanup["interrupted_receipts"],
            "archive_external_effect_present": True,
            "store_reopened": True,
        },
        "cleanup": {
            "operation_id": operation_id,
            "automatic_after_proof": True,
            "same_operation_resumed": True,
            **final,
        },
        "stop_after": {
            "decision": final["hook_decision"],
            "wait_safe": final["wait_safe"],
            "end_safe": final["end_safe"],
            "allowed_only_after_cleanup_completed": True,
        },
        "scope": {
            "archive_first": final["action_order"][0] == "archive_identity_evidence",
            "callsign_last": final["action_order"][-1] == "callsign_release",
            "exact_endpoint_only": True,
            "clean_worktree_only": True,
            "eligible_branch_only": True,
            "canonical_league_state_touched": False,
            "global_install_performed": False,
            "hosted_mutation_performed": False,
        },
    }
    receipt["receipt_sha256"] = _sha256(_stable_bytes(receipt))
    _write_json(home / "real-cleanup-canary-receipt.json", receipt)
    return receipt


def run_real_cleanup_canary(
    temporary_root: Path,
    namespace: str,
    *,
    source_root: Path | None = None,
) -> dict[str, Any]:
    root, source, home = _real_canary_paths(temporary_root, namespace, source_root)
    try:
        prepared = _prepare_real_canary(home, source, namespace)
        task = _complete_real_canary_task(home, source, prepared)
        operation_id = "operation:real-cleanup-canary"
        publication = _publish_real_canary_artifact(
            home, source, prepared, task, operation_id
        )
        cleanup = _execute_real_canary_cleanup(
            home, source, prepared, task, operation_id
        )
        return _build_real_canary_receipt(
            root, home, namespace, prepared, task, publication, cleanup, operation_id
        )
    except BaseException:
        try:
            _cleanup_failed_canary(home)
        except BaseException as cleanup_exc:
            raise StorageRefusal(
                "real_canary_failure_cleanup_failed",
                "failed canary resources could not be proven safe for exact cleanup",
            ) from cleanup_exc
        raise
