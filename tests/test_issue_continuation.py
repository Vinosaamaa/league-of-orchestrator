#!/usr/bin/env python3
"""Focused issue-close, exact-thread reopen, and successor cleanup coverage."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.cleanup import CleanupAdapterRegistry, CleanupExecutor, CleanupPlanner  # noqa: E402
from league.continuation import (  # noqa: E402
    ContinuationIssueReopener,
    GitHubIssueAdapter,
    verified_binding,
)
from league.request_services import AssignmentService, AssignmentSpec  # noqa: E402
from league.sqlite_continuation_ops import authorize_resumed_runtime  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from lifecycle_fakes import FakeClock, issue_bound_spec  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    AHRI_ID,
    GAREN_RUNTIME,
    LUX_ID,
    capture_p100,
    create_context,
    dispatch_request,
)
from runtime_doubles import StateCleanupAdapter  # noqa: E402
from storage_fixture import SHOTCALLER_ID  # noqa: E402


THREAD_ID = "33333333-3333-4333-8333-333333333333"
REPOSITORY = "https://github.com/example/league.git"
DIGEST = hashlib.sha256(b"issue-83-policy").hexdigest()
AT_PLAN = "2026-01-01T00:10:00Z"
AT_EXECUTE = "2026-01-01T00:11:00Z"
AT_RETRY = "2026-01-01T00:20:00Z"
LEASE = "2026-01-01T00:12:00Z"
LEASE_RETRY = "2026-01-01T00:30:00Z"


class Ids:
    def __init__(self, suffix: str) -> None:
        self.suffix = suffix

    def new(self, kind: str) -> str:
        return f"{kind}:{self.suffix}"


class ExactThreadLaunchAdapter:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id

    def launch(self, spec: AssignmentSpec) -> dict:
        return {
            "verified": True,
            "assignment_id": spec.assignment_id,
            "task_id": spec.task_id,
            "champion_agent_id": spec.champion_agent_id,
            "callsign": spec.callsign,
            "runtime_instance_id": f"runtime:{spec.champion_agent_id}",
            "thread_id": self.thread_id,
            "endpoint": f"synthetic:{str(spec.callsign).lower()}",
            "runtime_generation": f"generation:{spec.champion_agent_id}",
            "harness_kind": "codex-thread",
            "backend_kind": "herdr",
            "routing_name": str(spec.callsign).lower(),
            "display_agent": "codex",
            "repository": spec.repository,
            "issue": spec.issue,
            "branch": spec.branch,
            "worktree": spec.worktree,
            "capabilities": list(spec.required_capabilities),
        }


class RuntimeCloseAdapter(StateCleanupAdapter):
    def __init__(self, store, states, effects, runtime: dict) -> None:
        super().__init__("backend", states, effects)
        self.store = store
        self.runtime = runtime

    def apply(self, action):
        self.store.close_runtime_for_cleanup(
            self.runtime["runtime_instance_id"],
            self.runtime["endpoint"],
            self.runtime["runtime_generation"],
            AT_EXECUTE,
        )
        return super().apply(action)


class CallsignReleaseAdapter(StateCleanupAdapter):
    def __init__(self, store, states, effects, assignment_id: str) -> None:
        super().__init__("callsign", states, effects)
        self.store = store
        self.assignment_id = assignment_id

    def apply(self, action):
        self.store.release_callsign(
            f"callsign-assignment:{self.assignment_id}",
            2,
            hashlib.sha256(self.assignment_id.encode("utf-8")).hexdigest(),
            AT_EXECUTE,
        )
        return super().apply(action)


class RetryableStateCleanupAdapter(StateCleanupAdapter):
    def __init__(self, kind, states, effects, fail_action_kind: str | None) -> None:
        super().__init__(kind, states, effects)
        self.fail_action_kind = fail_action_kind

    def apply(self, action):
        if action["action_kind"] == self.fail_action_kind:
            self.fail_action_kind = None
            raise StorageRefusal(
                "cleanup_action_failed", "synthetic cleanup failure", retryable=True
            )
        return super().apply(action)


class FakeIssueAdapter:
    kind = "issue"

    def __init__(self, state: dict[str, str], *, fail_once: str | None = None) -> None:
        self.state = state
        self.fail_once = fail_once
        self.calls: list[str] = []

    def inspect(self, action):
        expected = action["expected_identity"]
        return {
            "repository": expected["repository"],
            "issue": expected["issue"],
            "state": self.state["value"],
        }

    def apply(self, action):
        target = action["intended_state"]["state"]
        self.calls.append(target)
        if self.fail_once == "before":
            self.fail_once = None
            raise StorageRefusal("issue_action_failed", "synthetic issue failure", retryable=True)
        self.state["value"] = target
        if self.fail_once == "after":
            self.fail_once = None
            raise StorageRefusal(
                "issue_action_failed", "synthetic unknown issue outcome", retryable=True
            )
        return {"provider": "synthetic", "state": target}

    @staticmethod
    def intended(action, observation):
        return dict(observation) == dict(action["intended_state"])


class FakeGitHubRunner:
    def __init__(self, repository: str = "Vinosaamaa/league-of-orchestrator") -> None:
        self.repository = repository
        self.state = "open"
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments, *, timeout_seconds: int = 30):
        del timeout_seconds
        command = tuple(arguments)
        self.calls.append(command)
        issue = command[3] if len(command) > 3 else ""
        repository = command[command.index("--repo") + 1] if "--repo" in command else ""
        if issue != "83" or repository != self.repository:
            return subprocess.CompletedProcess(command, 1, "", "exact identity mismatch")
        if command[1:3] == ("issue", "view"):
            stdout = f"issue:\n  number: 83\n  state: {self.state}\n"
        elif command[1:3] == ("issue", "close"):
            self.state = "closed"
            stdout = "closed issue 83\n"
        elif command[1:3] == ("issue", "reopen"):
            self.state = "open"
            stdout = "reopened issue 83\n"
        else:
            raise AssertionError(f"unexpected GitHub command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout, "")


def test_exact_github_issue_adapter() -> None:
    runner = FakeGitHubRunner()
    adapter = GitHubIssueAdapter(runner, executable="gh-axi")
    expected = {
        "repository": "https://github.com/Vinosaamaa/league-of-orchestrator.git",
        "issue": 83,
        "state": "open",
    }
    close = {"expected_identity": expected, "intended_state": {**expected, "state": "closed"}}
    assert adapter.inspect(close) == expected
    close_receipt = adapter.apply(close)
    assert close_receipt["provider"] == "github" and runner.state == "closed"
    reopen = {
        "expected_identity": {**expected, "state": "closed"},
        "intended_state": expected,
    }
    adapter.apply(reopen)
    assert runner.state == "open"
    assert all(
        call[call.index("--repo") + 1] == "Vinosaamaa/league-of-orchestrator"
        for call in runner.calls
    )
    wrong_issue = {
        "expected_identity": {**expected, "issue": 84},
        "intended_state": {**expected, "issue": 84, "state": "closed"},
    }
    try:
        adapter.inspect(wrong_issue)
    except StorageRefusal as exc:
        assert exc.code == "issue_action_failed"
    else:
        raise AssertionError("exact-identity fake accepted a different issue")


def test_exact_new_worktree_binding(root: Path) -> None:
    worktree = root / "binding-worktree"
    worktree.mkdir()

    def git(*arguments: str) -> None:
        subprocess.run(
            ("git", "-C", str(worktree), *arguments),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    git("init", "-b", "agent/synthetic/exact-binding")
    git("config", "user.name", "League Test")
    git("config", "user.email", "league-test@example.invalid")
    (worktree / "README.md").write_text("synthetic\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "synthetic binding")
    accepted = (
        "https://github.com/example/league.git",
        "git@github.com:example/league.git",
        "ssh://git@github.com/example/league.git",
    )
    git("remote", "add", "origin", accepted[0])
    for remote in accepted:
        git("remote", "set-url", "origin", remote)
        binding = verified_binding(
            repository=REPOSITORY,
            issue=83,
            branch="agent/synthetic/exact-binding",
            worktree=str(worktree),
        )
        assert binding["verified"] is True and len(binding["head"]) == 40
    try:
        verified_binding(
            repository=REPOSITORY,
            issue=83,
            branch="main",
            worktree=str(worktree),
        )
    except StorageRefusal as exc:
        assert exc.code == "workspace_binding_unsafe"
    else:
        raise AssertionError("stale/default branch binding was accepted")
    git("remote", "set-url", "origin", "git://github.com/example/league.git")
    try:
        verified_binding(
            repository=REPOSITORY,
            issue=83,
            branch="agent/synthetic/exact-binding",
            worktree=str(worktree),
        )
    except StorageRefusal as exc:
        assert exc.code == "workspace_binding_unsafe"
    else:
        raise AssertionError("unsupported GitHub remote form was durably claimable")


def _complete_task(store, task_id: str, runtime_id: str, suffix: str) -> None:
    version = store.connection.execute(
        "SELECT version FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()[0]
    store.transition_task(
        task_id,
        runtime_id,
        version,
        "completed",
        "Synthetic issue implementation and acceptance completed.",
        "Execute exact cleanup and close the owning issue.",
        None,
        f"transition:{suffix}",
        f"transition-key:{suffix}",
        f"event:{suffix}",
        f"outbox:{suffix}",
        SHOTCALLER_ID,
        AT_PLAN,
    )


def _manifest(
    *,
    store,
    task_id: str,
    agent_id: str,
    assignment_id: str,
    runtime_id: str,
    archive_id: str,
    lineage_id: str,
    issue_state: str,
    exact_resume: bool = True,
    provider_kind: str = "codex",
) -> dict:
    agent = store.agent_status(agent_id)
    binding = store.connection.execute(
        "SELECT branch,worktree FROM agent_instances WHERE agent_id=?", (agent_id,)
    ).fetchone()
    runtime = store.connection.execute(
        "SELECT * FROM runtime_instances WHERE runtime_instance_id=?", (runtime_id,)
    ).fetchone()
    cleanup = store.connection.execute(
        "SELECT version FROM cleanup_obligations WHERE task_id=?", (task_id,)
    ).fetchone()
    expected_cleanup_version = 0 if cleanup is None else int(cleanup["version"])
    proof = {
        "identity": {"exact": True},
        "endpoint": {"terminal_or_idle": True},
        "git": {"exact_registration": True, "clean": True, "no_unpublished": True},
        "publication": {"exact_head": True, "ci_green": True, "integrated": True},
    }
    issue_identity = {"repository": REPOSITORY, "issue": 83, "state": issue_state}
    final_actions = [
        {
            "action_kind": action,
            "adapter_kind": {
                "session_exit": "harness",
                "endpoint_close": "backend",
                "worktree_remove": "git",
                "branch_delete": "git",
                "callsign_release": "callsign",
                "issue_close": "issue",
            }[action],
            "expected_identity": (
                issue_identity
                if action == "issue_close"
                else {"action": action, "generation": "exact"}
            ),
            "intended_state": (
                {**issue_identity, "state": "closed"}
                if action == "issue_close"
                else {"completed": True, "action": action}
            ),
        }
        for action in (
            "session_exit",
            "endpoint_close",
            "worktree_remove",
            "branch_delete",
            "callsign_release",
            "issue_close",
        )
    ]
    return {
        "task_id": task_id,
        "owner": {"id": agent_id, "role": "champion", "persistent": False},
        "task_class": "pr_ci",
        "disposition": "completed",
        "pending_decisions_clear": True,
        "expected_cleanup_version": expected_cleanup_version,
        "identity": {"task_id": task_id, "owner_id": agent_id},
        "legacy_identity": {"task_id": task_id, "owner_id": agent_id},
        "proof": proof,
        "resources": [],
        "continuation_archive": {
            "archive_id": archive_id,
            "lineage_id": lineage_id,
            "provider_kind": provider_kind,
            "thread_identity": f"{provider_kind}:{runtime['session_ref']}",
            "runtime_instance_id": runtime_id,
            "repository": REPOSITORY,
            "issue": 83,
            "branch": binding["branch"],
            "worktree": binding["worktree"],
            "prior_callsign": agent["callsign"],
            "instruction_digest": DIGEST,
            "policy_digest": DIGEST,
            "context_health": "healthy",
            "resume_capabilities": {
                "durable": True,
                "exact_resume": exact_resume,
                "safe_worktree_rebind": True,
            },
            "acceptance": {"required_gates_complete": True},
            "cleanup_evidence": proof,
        },
        "final_actions": final_actions,
    }


def _execute_cleanup(
    store,
    manifest: dict,
    operation_id: str,
    assignment_id: str,
    issue: FakeIssueAdapter,
    *,
    expected_fence: int = 0,
    at: str = AT_EXECUTE,
    fail_action_kind: str | None = None,
) -> dict:
    existing = store.cleanup_operation(operation_id)
    if existing is None:
        planned = CleanupPlanner(store).plan(manifest, operation_id=operation_id, at=AT_PLAN)
    else:
        planned = {"fence": expected_fence}
    operation = store.cleanup_operation(operation_id)
    states = {
        action["action_id"]: dict(
            action["intended_state"]
            if action["state"] == "completed"
            else action["expected_identity"]
        )
        for action in operation["actions"]
    }
    effects: list[str] = []
    registry = CleanupAdapterRegistry()
    for kind in ("archive", "harness", "git"):
        registry.register(
            RetryableStateCleanupAdapter(kind, states, effects, fail_action_kind)
            if kind == "git" and fail_action_kind is not None
            else StateCleanupAdapter(kind, states, effects)
        )
    runtime = store.connection.execute(
        "SELECT * FROM runtime_instances WHERE runtime_instance_id=?",
        (manifest["continuation_archive"]["runtime_instance_id"],),
    ).fetchone()
    registry.register(RuntimeCloseAdapter(store, states, effects, dict(runtime)))
    registry.register(CallsignReleaseAdapter(store, states, effects, assignment_id))
    registry.register(issue)
    return CleanupExecutor(store, registry).execute(
        operation_id,
        expected_fence=int(planned["fence"]),
        executor_id=f"executor:{operation_id}",
        leased_until=LEASE_RETRY if at == AT_RETRY else LEASE,
        at=at,
    )


def _prepare_fixture(
    root: Path,
    name: str,
    *,
    exact_resume: bool = True,
    context_health: str = "healthy",
    provider_kind: str = "codex",
    thread_id: str = THREAD_ID,
):
    _, store, clock = create_context(root, name)
    capture_p100(store, clock)
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    dispatch_request(store, clock, "R3", "claim-r3", "dispatch-r3", "repository-write", "champion")
    worktree = root / name / "original-worktree"
    worktree.mkdir()
    spec = AssignmentSpec(
        assignment_id=f"assignment:{name}:original",
        request_id="R3",
        claim_token="claim-r3",
        task_id=f"task:{name}:original",
        task_summary="Synthetic issue continuation origin",
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID,
        repository=REPOSITORY,
        issue=83,
        branch=f"agent/synthetic/{name}-original",
        worktree=str(worktree),
        issue_receipt=None,
    )
    spec = issue_bound_spec(
        store, spec, clock.now(), repository=spec.repository
    )
    AssignmentService(
        store, ExactThreadLaunchAdapter(thread_id), clock, Ids(name + ":original")
    ).assign(spec)
    _complete_task(store, spec.task_id, f"runtime:{LUX_ID}", name + ":complete")
    manifest = _manifest(
        store=store,
        task_id=spec.task_id,
        agent_id=LUX_ID,
        assignment_id=spec.assignment_id,
        runtime_id=f"runtime:{LUX_ID}",
        archive_id=f"archive:{name}:original",
        lineage_id=f"lineage:{name}",
        issue_state="open",
        exact_resume=exact_resume,
        provider_kind=provider_kind,
    )
    manifest["continuation_archive"]["context_health"] = context_health
    return store, clock, spec, manifest


def _continuation_spec(root: Path, name: str, archive_id: str) -> dict:
    worktree = root / name / "successor-worktree"
    worktree.mkdir(exist_ok=True)
    return {
        "operation_id": f"continuation:{name}",
        "archive_id": archive_id,
        "assignment_id": f"assignment:{name}:successor",
        "new_task_id": f"task:{name}:successor",
        "new_agent_id": AHRI_ID,
        "repository": REPOSITORY,
        "issue": 83,
        "branch": f"agent/synthetic/{name}-successor",
        "worktree": str(worktree),
        "binding": {
            "verified": True,
            "repository": REPOSITORY,
            "issue": 83,
            "branch": f"agent/synthetic/{name}-successor",
            "worktree": str(worktree),
            "head": "a" * 40,
        },
        "instruction_digest": DIGEST,
        "reconciliation_digest": None,
        "concrete_benefit": "same_artifact_revision",
        "expected_archive_version": 2,
        "at": AT_RETRY,
    }


def test_cleanup_close_retries_and_already_closed_is_idempotent(root: Path) -> None:
    store, _, original, manifest = _prepare_fixture(root, "cleanup-failure")
    issue_state = {"value": "open"}
    issue = FakeIssueAdapter(issue_state)
    try:
        _execute_cleanup(
            store,
            manifest,
            "cleanup:cleanup-failure",
            original.assignment_id,
            issue,
            fail_action_kind="worktree_remove",
        )
    except StorageRefusal as exc:
        assert exc.code == "cleanup_action_failed" and exc.retryable is True
    else:
        raise AssertionError("pre-issue cleanup failure was not preserved")
    assert issue.calls == [] and issue_state["value"] == "open"
    operation = store.cleanup_operation("cleanup:cleanup-failure")
    assert operation["state"] == "executing" and operation["fence"] == 1
    completed = _execute_cleanup(
        store,
        manifest,
        "cleanup:cleanup-failure",
        original.assignment_id,
        issue,
        expected_fence=1,
        at=AT_RETRY,
    )
    assert completed["state"] == "cleanup_completed" and issue_state["value"] == "closed"
    store.close()

    store, _, original, manifest = _prepare_fixture(root, "close-retry")
    issue_state = {"value": "open"}
    issue = FakeIssueAdapter(issue_state, fail_once="before")
    try:
        _execute_cleanup(
            store,
            manifest,
            "cleanup:close-retry",
            original.assignment_id,
            issue,
        )
    except StorageRefusal as exc:
        assert exc.code == "issue_action_failed" and exc.retryable is True
    else:
        raise AssertionError("synthetic issue-close failure was not preserved")
    operation = store.cleanup_operation("cleanup:close-retry")
    assert operation["state"] == "executing" and operation["fence"] == 1
    completed = _execute_cleanup(
        store,
        manifest,
        "cleanup:close-retry",
        original.assignment_id,
        issue,
        expected_fence=1,
        at=AT_RETRY,
    )
    assert completed["state"] == "cleanup_completed" and issue_state["value"] == "closed"
    archive = store.thread_archive("archive:close-retry:original")
    assert archive["state"] == "available" and archive["cleanup_receipt_id"] is not None
    store.close()

    store, _, original, manifest = _prepare_fixture(root, "already-closed")
    issue_state = {"value": "closed"}
    issue = FakeIssueAdapter(issue_state)
    completed = _execute_cleanup(
        store,
        manifest,
        "cleanup:already-closed",
        original.assignment_id,
        issue,
    )
    assert completed["state"] == "cleanup_completed" and issue.calls == []
    store.close()


def test_reopen_exact_thread_new_callsign_and_final_cleanup(root: Path) -> None:
    store, clock, original, manifest = _prepare_fixture(root, "resume")
    issue_state = {"value": "open"}
    _execute_cleanup(
        store,
        manifest,
        "cleanup:resume:original",
        original.assignment_id,
        FakeIssueAdapter(issue_state),
    )
    original_callsign = manifest["continuation_archive"]["prior_callsign"]
    status = store.callsign_status("champion")
    store.reconcile_callsign_pool(
        "champion",
        status["queue_version"],
        status["seed"],
        status["shuffle_version"],
        [
            {
                "callsign": entry["callsign"],
                "enabled": entry["callsign"] != original_callsign,
                "capabilities": [],
            }
            for entry in status["entries"]
        ],
        AT_RETRY,
    )
    continuation = _continuation_spec(root, "resume", "archive:resume:original")
    prepared = store.prepare_continuation(continuation)
    assert prepared["state"] == "prepared"
    reopened = ContinuationIssueReopener(store, FakeIssueAdapter(issue_state)).execute(
        continuation["operation_id"],
        expected_version=1,
        expected_fence=0,
        executor_id="executor:reopen",
        leased_until=LEASE_RETRY,
        at=AT_RETRY,
    )
    assert reopened["state"] == "issue_reopened" and issue_state["value"] == "open"
    store.mark_continuation_launching(
        continuation["operation_id"], reopened["version"], AT_RETRY
    )
    store.claim_request("R2", GAREN_RUNTIME, "claim-r2", clock.after(120), clock.now())
    dispatch_request(store, clock, "R2", "claim-r2", "dispatch-r2", "repository-write", "champion")
    successor = AssignmentSpec(
        assignment_id=continuation["assignment_id"],
        request_id="R2",
        claim_token="claim-r2",
        task_id=continuation["new_task_id"],
        task_summary="Synthetic exact-thread continuation successor",
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=AHRI_ID,
        repository=REPOSITORY,
        issue=83,
        branch=continuation["branch"],
        worktree=continuation["worktree"],
        issue_receipt=None,
    )
    successor = issue_bound_spec(
        store, successor, clock.now(), repository=successor.repository
    )
    wrong_receipt = ExactThreadLaunchAdapter(
        "44444444-4444-4444-8444-444444444444"
    ).launch(AssignmentSpec(**{**vars(successor), "callsign": "Ahri"}))
    try:
        authorize_resumed_runtime(store, successor.assignment_id, wrong_receipt)
    except StorageRefusal as exc:
        assert exc.code == "thread_identity_ambiguous"
    else:
        raise AssertionError("exact-thread fake accepted the wrong archived identity")
    active = AssignmentService(
        store, ExactThreadLaunchAdapter(THREAD_ID), clock, Ids("resume:successor")
    ).assign(successor)
    assert active["state"] == "active"
    operation = store.continuation_status(continuation["operation_id"])
    assert operation["state"] == "resumed" and operation["callsign"] != original_callsign
    runtimes = store.connection.execute(
        "SELECT status,verified FROM runtime_instances WHERE session_ref=? ORDER BY runtime_instance_id",
        (THREAD_ID,),
    ).fetchall()
    assert sorted(tuple(row) for row in runtimes) == [("active", 1), ("closed", 0)]

    _complete_task(store, successor.task_id, f"runtime:{AHRI_ID}", "resume:successor-complete")
    successor_manifest = _manifest(
        store=store,
        task_id=successor.task_id,
        agent_id=AHRI_ID,
        assignment_id=successor.assignment_id,
        runtime_id=f"runtime:{AHRI_ID}",
        archive_id="archive:resume:successor",
        lineage_id="lineage:resume",
        issue_state="open",
    )
    final = _execute_cleanup(
        store,
        successor_manifest,
        "cleanup:resume:successor",
        successor.assignment_id,
        FakeIssueAdapter(issue_state),
    )
    assert final["state"] == "cleanup_completed" and issue_state["value"] == "closed"
    assert store.thread_archive("archive:resume:successor")["state"] == "available"
    store.close()


def test_resume_capability_and_acceptance_refusals(root: Path) -> None:
    store, _, original, manifest = _prepare_fixture(root, "refusals", exact_resume=False)
    incomplete = {
        **manifest,
        "continuation_archive": {
            **manifest["continuation_archive"],
            "acceptance": {"required_gates_complete": False},
        },
    }
    try:
        CleanupPlanner(store).plan(
            incomplete, operation_id="cleanup:incomplete-gates", at=AT_PLAN
        )
    except StorageRefusal as exc:
        assert exc.code == "thread_archive_invalid"
    else:
        raise AssertionError("incomplete acceptance gates were archived")
    issue_state = {"value": "open"}
    _execute_cleanup(
        store,
        manifest,
        "cleanup:refusals",
        original.assignment_id,
        FakeIssueAdapter(issue_state),
    )
    spec = _continuation_spec(root, "refusals", "archive:refusals:original")
    try:
        store.prepare_continuation(spec)
    except StorageRefusal as exc:
        assert exc.code == "resume_unsupported"
    else:
        raise AssertionError("unsupported exact resume was accepted")
    store.close()


def test_provider_driver_refuses_opaque_thread(root: Path) -> None:
    opaque_thread = "opaque/provider-thread?id=17#fragment"
    store, _, original, manifest = _prepare_fixture(
        root,
        "opaque-provider",
        provider_kind="cursor",
        thread_id=opaque_thread,
    )
    issue_state = {"value": "open"}
    _execute_cleanup(
        store,
        manifest,
        "cleanup:opaque-provider",
        original.assignment_id,
        FakeIssueAdapter(issue_state),
    )
    opaque = _continuation_spec(
        root, "opaque-provider", "archive:opaque-provider:original"
    )
    store.prepare_continuation(opaque)
    issue = FakeIssueAdapter(issue_state)
    try:
        ContinuationIssueReopener(store, issue).execute(
            opaque["operation_id"],
            expected_version=1,
            expected_fence=0,
            executor_id="executor:unsupported-provider",
            leased_until=LEASE,
            at=AT_EXECUTE,
        )
    except StorageRefusal as exc:
        assert exc.code == "resume_unsupported"
    else:
        raise AssertionError("unsupported provider driver reopened an issue")
    assert issue.calls == [] and issue_state["value"] == "closed"
    store.close()


def test_unhealthy_context_refusal(root: Path) -> None:
    store, _, original, manifest = _prepare_fixture(
        root, "unhealthy", context_health="unhealthy"
    )
    issue_state = {"value": "open"}
    _execute_cleanup(
        store,
        manifest,
        "cleanup:unhealthy",
        original.assignment_id,
        FakeIssueAdapter(issue_state),
    )
    try:
        store.prepare_continuation(
            _continuation_spec(root, "unhealthy", "archive:unhealthy:original")
        )
    except StorageRefusal as exc:
        assert exc.code == "resume_context_unhealthy"
    else:
        raise AssertionError("unhealthy archived context was accepted")
    store.close()


def test_instruction_drift_requires_reconciliation(root: Path) -> None:
    store, _, original, manifest = _prepare_fixture(root, "instruction-drift")
    issue_state = {"value": "open"}
    _execute_cleanup(
        store,
        manifest,
        "cleanup:instruction-drift",
        original.assignment_id,
        FakeIssueAdapter(issue_state),
    )
    drift = _continuation_spec(
        root, "instruction-drift", "archive:instruction-drift:original"
    )
    drift["repository"] = "git@github.com:example/league.git"
    drift["binding"] = {
        **drift["binding"],
        "repository": drift["repository"],
    }
    drift["instruction_digest"] = "b" * 64
    try:
        store.prepare_continuation(drift)
    except StorageRefusal as exc:
        assert exc.code == "instruction_drift_unreconciled"
    else:
        raise AssertionError("unreconciled instruction drift was accepted")
    drift["reconciliation_digest"] = "c" * 64
    assert store.prepare_continuation(drift)["state"] == "prepared"
    store.close()


def test_continuation_claim_and_partial_reopen_recovery(root: Path) -> None:
    store, _, original, manifest = _prepare_fixture(root, "guards")
    issue_state = {"value": "open"}
    _execute_cleanup(
        store,
        manifest,
        "cleanup:guards",
        original.assignment_id,
        FakeIssueAdapter(issue_state),
    )
    unsupported = _continuation_spec(
        root, "guards", "archive:guards:original"
    )
    unsupported["repository"] = "https://example.invalid/example/league.git"
    unsupported["binding"] = {
        **unsupported["binding"],
        "repository": unsupported["repository"],
    }
    try:
        store.prepare_continuation(unsupported)
    except StorageRefusal as exc:
        assert exc.code == "workspace_binding_unsafe"
    else:
        raise AssertionError("unsupported repository form reached a durable claim")
    assert store.continuation_status(unsupported["operation_id"]) is None
    unsafe = _continuation_spec(root, "guards", "archive:guards:original")
    unsafe["binding"] = {**unsafe["binding"], "verified": False}
    try:
        store.prepare_continuation(unsafe)
    except StorageRefusal as exc:
        assert exc.code == "workspace_binding_unsafe"
    else:
        raise AssertionError("stale worktree binding was accepted")
    spec = _continuation_spec(root, "guards", "archive:guards:original")
    store.prepare_continuation(spec)
    duplicate = {**spec, "operation_id": "continuation:guards:duplicate"}
    try:
        store.prepare_continuation(duplicate)
    except StorageRefusal as exc:
        assert exc.code == "continuation_conflict"
    else:
        raise AssertionError("concurrent continuation claim was accepted")
    issue = FakeIssueAdapter(issue_state, fail_once="after")
    try:
        ContinuationIssueReopener(store, issue).execute(
            spec["operation_id"],
            expected_version=1,
            expected_fence=0,
            executor_id="executor:first",
            leased_until=LEASE,
            at=AT_EXECUTE,
        )
    except StorageRefusal as exc:
        assert exc.code == "issue_action_failed" and issue_state["value"] == "open"
    else:
        raise AssertionError("partial issue reopen failure was not surfaced")
    recovered = ContinuationIssueReopener(store, issue).execute(
        spec["operation_id"],
        expected_version=2,
        expected_fence=1,
        executor_id="executor:retry",
        leased_until=LEASE_RETRY,
        at=AT_RETRY,
    )
    assert recovered["state"] == "issue_reopened" and recovered["idempotent"] is False
    store.close()


def test_reused_thread_identity_refusal(root: Path) -> None:
    store, _, original, manifest = _prepare_fixture(root, "reused")
    issue_state = {"value": "open"}
    _execute_cleanup(
        store,
        manifest,
        "cleanup:reused",
        original.assignment_id,
        FakeIssueAdapter(issue_state),
    )
    from league.storage import RuntimeRegistrationCommand

    store.register_runtime(
        RuntimeRegistrationCommand(
            runtime_instance_id="runtime:unlinked-reuse",
            actor_agent_id=SHOTCALLER_ID,
            harness_kind="codex-thread",
            backend_kind="herdr",
            session_ref=THREAD_ID,
            endpoint="synthetic:closed-reuse",
            runtime_generation="generation:closed-reuse",
            status="closed",
            verified=False,
            at=AT_RETRY,
        )
    )
    spec = _continuation_spec(root, "reused", "archive:reused:original")
    try:
        store.prepare_continuation(spec)
    except StorageRefusal as exc:
        assert exc.code == "thread_identity_reused"
    else:
        raise AssertionError("ambiguous or reused provider thread was accepted")
    store.close()


def main() -> None:
    test_exact_github_issue_adapter()
    with tempfile.TemporaryDirectory(prefix="league-issue-continuation-") as directory:
        root = Path(directory)
        test_exact_new_worktree_binding(root)
        test_cleanup_close_retries_and_already_closed_is_idempotent(root)
        test_reopen_exact_thread_new_callsign_and_final_cleanup(root)
        test_resume_capability_and_acceptance_refusals(root)
        test_provider_driver_refuses_opaque_thread(root)
        test_unhealthy_context_refusal(root)
        test_instruction_drift_requires_reconciliation(root)
        test_continuation_claim_and_partial_reopen_recovery(root)
        test_reused_thread_identity_refusal(root)
    print(
        "PASS: pre-close cleanup/issue retry, exact-thread reopen, new callsign/runtime, "
        "resume refusals, opaque provider IDs, fencing, partial recovery, and final cleanup"
    )


if __name__ == "__main__":
    main()
