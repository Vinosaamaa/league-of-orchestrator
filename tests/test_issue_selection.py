#!/usr/bin/env python3
"""Focused duplicate-preflight issue selection, reopen, and concurrency coverage."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.issue_first import (  # noqa: E402
    GitHubIssueSelectionService,
    IssueSelectionSpec,
    build_issue_receipt,
    validate_issue_receipt,
)
from league.request_services import AssignmentService, AssignmentSpec  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from league.storage_mode import SettleModeActionCommand  # noqa: E402
from lifecycle_fakes import FakeIds, FakeLaunchAdapter  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    LUX_ID,
    capture_p100,
    create_context,
    dispatch_request,
)
from storage_fixture import SHOTCALLER_ID  # noqa: E402


AT = "2026-01-01T00:10:00Z"
REPOSITORY = "https://github.com/example/job-journey.git"
TITLE = "Prevent duplicate implementation issues"
BODY = """## Objective
Prevent duplicate implementation issues before Champion assignment.

## Verification
Cover open reuse, closed reopen, distinct creation, and concurrent selection.

## Hard boundaries
Do not create an issue when normalized title and semantic scope already match.
"""


def _issue(number: int, *, state: str, title: str = TITLE, body: str = BODY) -> dict:
    return {
        "number": number,
        "state": state,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/example/job-journey/issues/{number}",
        "repository_url": "https://api.github.com/repos/example/job-journey",
    }


class FakeGitHubRunner:
    def __init__(self, issues: list[dict], *, block_first_list: bool = False) -> None:
        self.issues = [dict(issue) for issue in issues]
        self.created = 0
        self.reopened: list[int] = []
        self.calls: list[tuple[str, ...]] = []
        self.block_first_list = block_first_list
        self.list_started = threading.Event()
        self.release_list = threading.Event()
        self._lock = threading.Lock()

    def run(self, arguments, *, timeout_seconds: int = 30):
        del timeout_seconds
        command = tuple(arguments)
        self.calls.append(command)
        endpoint = next(value for value in command if value.startswith("repos/"))
        if "issues?state=all" in endpoint:
            if self.block_first_list:
                self.block_first_list = False
                self.list_started.set()
                assert self.release_list.wait(timeout=5)
            with self._lock:
                payload = [[dict(issue) for issue in self.issues]]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if "--input" not in command:
            raise AssertionError(f"unexpected GitHub command: {command}")
        input_path = Path(command[command.index("--input") + 1])
        value = json.loads(input_path.read_text(encoding="utf-8"))
        if command[command.index("--method") + 1] == "POST":
            with self._lock:
                number = max((int(issue["number"]) for issue in self.issues), default=215) + 1
                created = _issue(number, state="open", title=value["title"], body=value["body"])
                self.issues.append(created)
                self.created += 1
            return subprocess.CompletedProcess(command, 0, json.dumps(created), "")
        number = int(endpoint.rsplit("/", 1)[1])
        assert value == {"state": "open"}
        with self._lock:
            selected = next(issue for issue in self.issues if int(issue["number"]) == number)
            selected["state"] = "open"
            self.reopened.append(number)
            payload = dict(selected)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")


def _spec(task_id: str, *, task_summary: str = TITLE) -> IssueSelectionSpec:
    return IssueSelectionSpec(
        task_id=task_id,
        task_summary=task_summary,
        coordinator_agent_id=SHOTCALLER_ID,
        repository=REPOSITORY,
        issue_title=TITLE,
        issue_body=BODY,
    )


def _authorize_reopen(
    store: SQLiteStorage, issue: int, runner: FakeGitHubRunner
) -> str:
    grant = {
        "schema": "league.autonomous-grant.v1",
        "grant_id": f"grant:issue-reopen:{issue}",
        "goal_id": f"goal:issue-reopen:{issue}",
        "issuer": {"kind": "summoner", "id": "summoner:synthetic-owner"},
        "shotcaller_agent_id": SHOTCALLER_ID,
        "exact_goal": f"Reopen synthetic issue {issue} after duplicate preflight.",
        "scope": {
            "project_ids": [],
            "repositories": [REPOSITORY],
            "environments": [],
            "deployment_targets": [],
        },
        "allowed_actions": ["issue_reopen"],
        "exclusions": [],
        "sensitive_inclusions": [],
        "resource_boundary": {"issue": issue},
        "starts_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-02T00:00:00Z",
        "limits": {
            "max_attempts": 1,
            "max_concurrency": 1,
            "max_cost_microunits": 1,
            "max_changed_files": 1,
            "max_duration_seconds": 60,
            "max_repair_attempts": 1,
        },
        "revision": 1,
    }
    store.authorize_mode(grant, 0, AT)
    action = {
        "schema": "league.autonomous-action.v1",
        "action_use_id": f"action:issue-reopen:{issue}",
        "idempotency_key": f"idempotency:issue-reopen:{issue}",
        "goal_id": f"goal:issue-reopen:{issue}",
        "grant_id": f"grant:issue-reopen:{issue}",
        "actor_agent_id": SHOTCALLER_ID,
        "action_kind": "issue_reopen",
        "scope": {
            "project_id": None,
            "repository": REPOSITORY,
            "environment": None,
            "deployment_target": None,
        },
        "risk_categories": [],
        "sensitive_categories": [],
        "resources": {"issue": issue},
        "usage": {
            "attempts": 1,
            "cost_microunits": 0,
            "changed_files": 0,
            "duration_seconds": 1,
        },
    }
    used = store.use_mode_action(action, 1, AT)
    selected = next(item for item in runner.issues if int(item["number"]) == issue)
    selected["state"] = "open"
    runner.reopened.append(issue)
    result_digest = "e" * 64
    settled = store.settle_mode_action(
        SettleModeActionCommand(
            action_use_id=action["action_use_id"],
            goal_id=action["goal_id"],
            expected_goal_version=2,
            use_receipt_digest=used["use_receipt_digest"],
            outcome="succeeded",
            result_receipt_digest=result_digest,
            failure_class=None,
            at="2026-01-01T00:11:00Z",
        )
    )
    assert settled["state"] == "succeeded"
    return result_digest


def test_open_match_reuse_and_distinct_scope_creation(root: Path) -> None:
    state, store, clock = create_context(root, "open-distinct")
    runner = FakeGitHubRunner(
        [
            _issue(216, state="open", title="  PREVENT duplicate implementation issues!!  "),
            _issue(217, state="closed"),
        ]
    )
    selector = GitHubIssueSelectionService(store, runner)
    reused = selector.select(_spec("task:reuse"), "attempt:reuse", AT)
    assert reused["decision"] == "reuse_open"
    assert reused["issue"] == 216 and runner.created == 0
    assert reused["duplicate_matches"] == 2
    calls_after_selection = len(runner.calls)
    exact_retry = selector.select(
        _spec("task:reuse"), "attempt:reuse-retry", "2026-01-01T00:11:00Z"
    )
    assert exact_retry["idempotent"] is True
    assert exact_retry["receipt_digest"] == reused["receipt_digest"]
    assert len(runner.calls) == calls_after_selection

    capture_p100(store, clock)
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    dispatch_request(
        store, clock, "R3", "claim-r3", "dispatch-r3", "repository-write", "champion"
    )
    assignment_spec = AssignmentSpec(
        assignment_id="assignment:reuse",
        request_id="R3",
        claim_token="claim-r3",
        task_id="task:reuse",
        task_summary=TITLE,
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID,
        repository=REPOSITORY,
        issue=216,
        branch="agent/synthetic/216-reuse",
        worktree="/synthetic/worktrees/216-reuse",
        issue_receipt=None,
    )
    issue_receipt = build_issue_receipt(
        assignment_spec,
        runner.issues[0],
        AT,
        selection_receipt_digest=reused["receipt_digest"],
    )
    active = AssignmentService(store, FakeLaunchAdapter(), clock, FakeIds()).assign(
        replace(assignment_spec, issue_receipt=issue_receipt)
    )
    assert active["state"] == "active"

    distinct_body = BODY.replace(
        "Prevent duplicate implementation issues before Champion assignment.",
        "Implement a distinct release benchmark for the deployment pipeline.",
    )
    distinct = selector.select(
        replace(_spec("task:distinct"), issue_body=distinct_body),
        "attempt:distinct",
        "2026-01-01T00:12:00Z",
    )
    assert distinct["decision"] == "create_distinct"
    assert distinct["issue"] == 218 and runner.created == 1
    assert len(distinct["receipt_digest"]) == 64
    inspection = json.loads(
        store.export_bytes(format_name="json", purpose="inspection", max_records=10_000)
    )
    rollback = json.loads(
        store.export_bytes(format_name="json", purpose="rollback", max_records=10_000)
    )
    assert inspection["tables"]["repository_issue_selection_receipts"][0][
        "task_summary"
    ] == "[redacted]"
    assert {
        row["receipt_digest"]
        for row in rollback["tables"]["repository_issue_selection_receipts"]
    } == {reused["receipt_digest"], distinct["receipt_digest"]}
    store.close()


def test_issue_creation_validates_final_public_bytes_before_search(root: Path) -> None:
    _, store, _ = create_context(root, "public-boundary")
    runner = FakeGitHubRunner([])
    unsafe_body = BODY.replace(
        "Prevent duplicate implementation issues before Champion assignment.",
        "Read /Users/synthetic/private-state before Champion assignment.",
    )
    try:
        GitHubIssueSelectionService(store, runner).select(
            replace(_spec("task:unsafe"), issue_body=unsafe_body),
            "attempt:unsafe",
            AT,
        )
    except StorageRefusal as exc:
        assert exc.code == "outbound_payload_rejected"
    else:
        raise AssertionError("unsafe issue bytes reached the GitHub search boundary")
    assert runner.calls == []
    assert store.connection.execute(
        "SELECT COUNT(*) FROM repository_issue_selection_leases"
    ).fetchone()[0] == 0
    store.close()


def test_selection_refuses_title_different_from_task_before_github_or_lease(root: Path) -> None:
    _, store, _ = create_context(root, "title-mismatch")
    runner = FakeGitHubRunner([])
    try:
        GitHubIssueSelectionService(store, runner).select(
            _spec("task:title-mismatch", task_summary="Different task title"),
            "attempt:title-mismatch",
            AT,
        )
    except StorageRefusal as exc:
        assert exc.code == "issue_title_mismatch"
    else:
        raise AssertionError("issue selection accepted a title different from the task")
    assert runner.calls == []
    assert store.connection.execute(
        "SELECT COUNT(*) FROM repository_issue_selection_leases"
    ).fetchone()[0] == 0
    store.close()


def test_closed_match_reopen_preserves_prior_champion_linkage(root: Path) -> None:
    state, store, clock = create_context(root, "closed-reopen")
    capture_p100(store, clock)
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    dispatch_request(
        store, clock, "R3", "claim-r3", "dispatch-r3", "repository-write", "champion"
    )
    runner = FakeGitHubRunner([_issue(216, state="open")])
    selector = GitHubIssueSelectionService(store, runner)
    prior_selection = selector.select(_spec("task:prior"), "attempt:prior", AT)
    assignment_spec = AssignmentSpec(
        assignment_id="assignment:prior",
        request_id="R3",
        claim_token="claim-r3",
        task_id="task:prior",
        task_summary=TITLE,
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID,
        callsign="Lux",
        repository=REPOSITORY,
        issue=216,
        branch="agent/synthetic/216",
        worktree="/synthetic/worktrees/216",
        issue_receipt=None,
    )
    issue_receipt = build_issue_receipt(
        assignment_spec,
        _issue(216, state="open"),
        AT,
        selection_receipt_digest=prior_selection["receipt_digest"],
    )
    tampered_url = dict(issue_receipt)
    tampered_url["issue_url"] = "https://github.com/example/job-journey/issues/999"
    unsigned = dict(tampered_url)
    unsigned.pop("receipt_digest")
    tampered_url["receipt_digest"] = __import__("hashlib").sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    try:
        validate_issue_receipt(tampered_url)
    except StorageRefusal as exc:
        assert exc.code == "issue_receipt_invalid"
    else:
        raise AssertionError("issue receipt accepted a changed canonical URL")
    tampered_title = dict(issue_receipt)
    tampered_title["issue_title"] = issue_receipt["issue_title"].upper()
    unsigned = dict(tampered_title)
    unsigned.pop("receipt_digest")
    tampered_title["receipt_digest"] = __import__("hashlib").sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    try:
        AssignmentService(store, FakeLaunchAdapter(), clock, FakeIds()).assign(
            replace(assignment_spec, issue_receipt=tampered_title)
        )
    except StorageRefusal as exc:
        assert exc.code == "issue_selection_unproven"
    else:
        raise AssertionError("assignment accepted a title differing from its selection receipt")
    active = AssignmentService(store, FakeLaunchAdapter(), clock, FakeIds()).assign(
        replace(assignment_spec, issue_receipt=issue_receipt)
    )
    assert active["state"] == "active"

    runner.issues[0]["state"] = "closed"
    try:
        selector.select(
            _spec("task:recurrence"),
            "attempt:recurrence-without-authority",
            "2026-01-01T00:12:00Z",
        )
    except StorageRefusal as exc:
        assert exc.code == "issue_reopen_required"
    else:
        raise AssertionError("closed equivalent reopened without Shotcaller authority")
    assert runner.reopened == [] and runner.created == 0
    reopen_digest = _authorize_reopen(store, 216, runner)
    reopened = selector.select(
        _spec("task:recurrence"),
        "attempt:recurrence",
        "2026-01-01T00:13:00Z",
        reopen_action_receipt_digest=reopen_digest,
    )
    assert reopened["decision"] == "reopen_closed"
    assert runner.reopened == [216]
    assert reopened["prior_linkage"] == {
        "task_id": "task:prior",
        "assignment_id": "assignment:prior",
        "champion_agent_id": LUX_ID,
        "runtime_instance_id": active["runtime_instance_id"],
        "session_ref": f"thread:{LUX_ID}",
    }
    store.close()


def test_concurrent_distinct_selection_creates_exactly_one_issue(root: Path) -> None:
    state, store, _ = create_context(root, "concurrent")
    store.close()
    runner = FakeGitHubRunner([], block_first_list=True)
    first: list[dict] = []

    def create_first() -> None:
        with SQLiteStorage(state) as candidate:
            first.append(
                GitHubIssueSelectionService(candidate, runner).select(
                    _spec("task:concurrent-a"), "attempt:concurrent-a", AT
                )
            )

    thread = threading.Thread(target=create_first)
    thread.start()
    assert runner.list_started.wait(timeout=5)
    with SQLiteStorage(state) as contender:
        try:
            GitHubIssueSelectionService(contender, runner).select(
                _spec("task:concurrent-b"), "attempt:concurrent-b", AT
            )
        except StorageRefusal as exc:
            assert exc.code == "issue_selection_busy"
        else:
            raise AssertionError("concurrent issue creation bypassed the durable preflight lease")
    runner.release_list.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first[0]["decision"] == "create_distinct" and runner.created == 1

    with SQLiteStorage(state) as retry_store:
        retried = GitHubIssueSelectionService(retry_store, runner).select(
            _spec("task:concurrent-b"),
            "attempt:concurrent-b-retry",
            "2026-01-01T00:12:00Z",
        )
    assert retried["decision"] == "reuse_open"
    assert retried["issue"] == first[0]["issue"] and runner.created == 1


def test_read_only_exact_issue_selection_never_creates(root: Path) -> None:
    _, store, _ = create_context(root, "read-only-selection")
    runner = FakeGitHubRunner([_issue(216, state="open")])
    selected = GitHubIssueSelectionService(store, runner).select(
        _spec("task:read-only"),
        "attempt:read-only",
        AT,
        allow_create=False,
        expected_issue=216,
    )
    assert selected["decision"] == "reuse_open"
    assert selected["issue"] == 216 and runner.created == 0
    store.close()

    _, missing_store, _ = create_context(root, "read-only-missing")
    missing_runner = FakeGitHubRunner([])
    try:
        GitHubIssueSelectionService(missing_store, missing_runner).select(
            _spec("task:read-only-missing"),
            "attempt:read-only-missing",
            AT,
            allow_create=False,
            expected_issue=216,
        )
    except StorageRefusal as exc:
        assert exc.code == "issue_selection_no_match"
    else:
        raise AssertionError("read-only issue selection created a missing issue")
    assert missing_runner.created == 0
    assert missing_store.connection.execute(
        "SELECT state FROM repository_issue_selection_leases"
    ).fetchone()[0] == "available"
    missing_store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-issue-selection-") as temporary:
        root = Path(temporary)
        test_open_match_reuse_and_distinct_scope_creation(root)
        test_issue_creation_validates_final_public_bytes_before_search(root)
        test_selection_refuses_title_different_from_task_before_github_or_lease(root)
        test_closed_match_reopen_preserves_prior_champion_linkage(root)
        test_concurrent_distinct_selection_creates_exactly_one_issue(root)
        test_read_only_exact_issue_selection_never_creates(root)
    print("PASS: duplicate preflight reuses, reopens with linkage, creates only distinct scope, and serializes concurrency")


if __name__ == "__main__":
    main()
