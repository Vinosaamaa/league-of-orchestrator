#!/usr/bin/env python3
"""Focused one-command visible Champion launch and failure recovery coverage."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.request_services import AssignmentSpec, LaunchAdapterError  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from league.visible_launch import (  # noqa: E402
    HerdrCodexLaunchAdapter,
    VisibleChampionLaunchService,
    VisibleLaunchOptions,
    _codex_trust_root,
)
from lifecycle_fakes import FakeLaunchAdapter  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    LUX_ID,
    capture_p100,
    create_context,
    dispatch_request,
)
from storage_fixture import REPOSITORY, SHOTCALLER_ID  # noqa: E402


THREAD_ID = "33333333-3333-4333-8333-333333333333"


def _context(root: Path, name: str):
    _, store, clock = create_context(root, name)
    capture_p100(store, clock)
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    dispatch_request(
        store,
        clock,
        "R3",
        "claim-r3",
        "dispatch-r3",
        "repository-write",
        "champion",
    )
    worktree = root / name / "worktree"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    return store, clock, worktree


def _spec(worktree: Path, suffix: str) -> AssignmentSpec:
    return AssignmentSpec(
        assignment_id=f"assignment:{suffix}",
        request_id="R3",
        claim_token="claim-r3",
        task_id=f"task:{suffix}",
        task_summary="Perform one tiny synthetic Champion task",
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID,
        repository=REPOSITORY,
        issue=23,
        branch=f"agent/synthetic/{suffix}",
        worktree=str(worktree),
    )


def _options(root: Path) -> VisibleLaunchOptions:
    (root / "league").mkdir(parents=True, exist_ok=True)
    return VisibleLaunchOptions(
        workspace_id="w1",
        task_label="Tiny Gate",
        model="gpt-5.6-sol",
        effort="high",
        league_command=str(ROOT / "bin/league"),
        state_root=str(root / "league"),
    )


class FakeHerdrRunner:
    def __init__(self, worktree: Path, *, wrong_thread: bool = False) -> None:
        self.worktree = str(worktree.resolve())
        self.wrong_thread = wrong_thread
        self.started = False
        self.renamed = False
        self.session_reported = False
        self.closed = False
        self.contexts: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def _agent(self) -> dict[str, object]:
        thread = "not-a-thread" if self.wrong_thread else THREAD_ID
        tokens = (
            {
                "sidebar_name": "Lux",
                "task_label": "Tiny Gate",
                "thread_title": "Lux · Tiny Gate",
            }
            if self.renamed
            else {}
        )
        agent = {
            "agent": "codex",
            "agent_status": "idle",
            "interactive_ready": True,
            "cwd": self.worktree,
            "foreground_cwd": self.worktree,
            "name": "lux",
            "pane_id": "w1:p99",
            "state_change_seq": 99,
            "tab_id": "w1:t99",
            "terminal_id": "term_test_99",
            "tokens": tokens,
            "workspace_id": "w1",
        }
        agent["agent_session"] = {"value": thread}
        return agent

    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        command = tuple(arguments)
        self.calls.append(command)
        result: dict[str, object]
        if command[:3] == ("herdr", "agent", "list"):
            agents = [self._agent()] if self.started and not self.closed else []
            result = {"agents": agents}
        elif command[:3] == ("herdr", "tab", "create"):
            result = {
                "tab": {"tab_id": "w1:t99"},
                "root_pane": {
                    "pane_id": "w1:p99",
                    "terminal_id": "term_test_99",
                },
            }
        elif command[:3] == ("herdr", "agent", "start"):
            self.started = True
            result = {"agent": self._agent()}
        elif command[:3] == ("herdr", "agent", "get"):
            result = {"agent": self._agent()}
        elif command[:3] == ("herdr", "pane", "report-metadata"):
            self.renamed = True
            result = {"accepted": True}
        elif command[:3] == ("herdr", "agent", "prompt"):
            prompt = command[4]
            if prompt == "/exit":
                self.closed = True
            else:
                self.contexts.append(prompt)
            result = {"accepted": True}
        elif command[:3] == ("herdr", "pane", "close"):
            self.closed = True
            result = {"closed": True}
        else:
            raise AssertionError(f"unexpected Herdr command: {command}")
        stdout = json.dumps({"id": "test", "result": result}) + "\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")


def _adapter(options: VisibleLaunchOptions, runner: FakeHerdrRunner):
    return HerdrCodexLaunchAdapter(
        options,
        runner,
        environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
    )


def test_real_adapter_one_command_success_and_retry(root: Path) -> None:
    store, clock, worktree = _context(root, "success")
    options = _options(root)
    runner = FakeHerdrRunner(worktree)
    service = VisibleChampionLaunchService(store, _adapter(options, runner), options, clock)
    spec = _spec(worktree, "success")
    result = service.launch(spec)
    assert result["state"] == "active" and result["version"] == 4
    assert result["context_delivery"]["bytes"] <= 4096
    assert len(runner.contexts) == 1
    context = runner.contexts[0]
    assert "Use only the stable League SQLite commands" in context
    assert "status.json" not in context and "updates.jsonl" not in context
    tab_index = next(i for i, call in enumerate(runner.calls) if call[:3] == ("herdr", "tab", "create"))
    start_index = next(i for i, call in enumerate(runner.calls) if call[:3] == ("herdr", "agent", "start"))
    context_index = max(i for i, call in enumerate(runner.calls) if call[:3] == ("herdr", "agent", "prompt"))
    assert tab_index < start_index < context_index
    start = runner.calls[start_index]
    assert start[start.index("--add-dir") + 1] == str(root / "league")
    assert "--sandbox" not in start
    assert not any(value.startswith("sandbox_mode=") for value in start)
    assert not any(
        value.startswith("sandbox_workspace_write.writable_roots=") for value in start
    )
    assert not any("trust_level=" in value for value in start)
    row = store.connection.execute(
        "SELECT state,version,runtime_instance_id FROM task_assignments WHERE task_assignment_id=?",
        (spec.assignment_id,),
    ).fetchone()
    assert tuple(row) == ("active", 4, f"runtime:{LUX_ID}")
    retry_runner = FakeHerdrRunner(worktree)
    retry = VisibleChampionLaunchService(
        store, _adapter(options, retry_runner), options, clock
    ).launch(spec)
    assert retry["idempotent"] is True
    assert retry_runner.calls == [] and len(runner.contexts) == 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='assignment_context_delivered' AND aggregate_id=?",
        (spec.assignment_id,),
    ).fetchone()[0] == 1
    activation_delivery = store.connection.execute(
        """
        SELECT o.state,o.last_outcome,r.effect_kind,r.effect_id
          FROM events e JOIN delivery_outbox o ON o.event_id=e.event_id
          JOIN recipient_receipts r
            ON r.event_id=o.event_id AND r.recipient_agent_id=o.recipient_agent_id
         WHERE e.aggregate_kind='assignment' AND e.aggregate_id=?
           AND e.event_type='assignment_active'
        """,
        (spec.assignment_id,),
    ).fetchone()
    assert tuple(activation_delivery[:3]) == (
        "delivered",
        "assignment_context_delivered",
        "assignment_context",
    )
    assert activation_delivery["effect_id"] == result["context_delivery"]["effect_sha256"]
    store.close()


class DeferredSessionRunner(FakeHerdrRunner):
    def _agent(self) -> dict[str, object]:
        agent = super()._agent()
        if not self.session_reported:
            agent.pop("agent_session", None)
        return agent

    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        if command[:3] == ("herdr", "agent", "prompt") and "identity handshake" in command[4]:
            self.session_reported = True
        return super().run(arguments, timeout_seconds=timeout_seconds)


def test_real_adapter_persists_exact_initial_codex_session(root: Path) -> None:
    store, clock, worktree = _context(root, "deferred-session")
    options = _options(root)
    runner = DeferredSessionRunner(worktree)
    result = VisibleChampionLaunchService(
        store, _adapter(options, runner), options, clock
    ).launch(_spec(worktree, "deferred-session"))
    assert result["state"] == "active"
    launch = store.assignment_launch_context("assignment:deferred-session")
    assert launch["acceptance_receipt"]["thread_id"] == THREAD_ID
    assert runner.session_reported is True
    assert any(
        "identity handshake" in call[4]
        for call in runner.calls
        if call[:3] == ("herdr", "agent", "prompt")
    )
    assert any(
        call[:3] == ("herdr", "pane", "report-metadata") for call in runner.calls
    )
    assert not any(
        call[:3] == ("herdr", "agent", "prompt")
        and len(call) > 4
        and call[4].startswith("/rename ")
        for call in runner.calls
    )
    store.close()


class PendingStartFailureRunner(FakeHerdrRunner):
    def _agent(self) -> dict[str, object]:
        agent = super()._agent()
        agent.pop("agent_session", None)
        agent["launch_pending"] = True
        agent["agent_status"] = "blocked"
        return agent

    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        if command[:3] == ("herdr", "agent", "start"):
            del timeout_seconds
            self.calls.append(command)
            self.started = True
            return subprocess.CompletedProcess(
                command,
                1,
                json.dumps(
                    {
                        "error": {
                            "code": "agent_launch_pending",
                            "message": "synthetic pending start",
                        }
                    }
                ),
                "",
            )
        return super().run(arguments, timeout_seconds=timeout_seconds)


def test_pre_session_launch_failure_closes_exact_pending_pane(root: Path) -> None:
    store, clock, worktree = _context(root, "pending-start-cleanup")
    options = _options(root)
    runner = PendingStartFailureRunner(worktree)
    service = VisibleChampionLaunchService(store, _adapter(options, runner), options, clock)
    result = service.launch(_spec(worktree, "pending-start-cleanup"))
    assert result["state"] == "blocked"
    assert runner.closed is True
    assert not any(
        call[:5] == ("herdr", "agent", "prompt", "lux", "/exit")
        for call in runner.calls
    )
    assert any(call[:3] == ("herdr", "pane", "close") for call in runner.calls)
    store.close()


def test_linked_worktree_trust_binds_owning_repository(root: Path) -> None:
    repository = root / "linked-trust/repository"
    worktree = root / "linked-trust/worktree"
    git_dir = repository / ".git/worktrees/canary"
    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    marker = worktree / ".git"
    marker.write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    (git_dir / "gitdir").write_text(f"{marker}\n", encoding="utf-8")
    assert _codex_trust_root(worktree) == repository.resolve()


class FailingAdapter:
    def __init__(self, base: FakeLaunchAdapter, *, cleanup: bool) -> None:
        self.base = base
        self._created = {"pane_id": "synthetic:pane"}
        self.cleanup_result = cleanup

    def launch(self, spec: AssignmentSpec):
        raise LaunchAdapterError(
            "synthetic_partial_launch", cleanup_required=True, cleanup_proven=False
        )

    def cleanup(self, receipt):
        del receipt
        return self.cleanup_result


def test_unproven_partial_launch_stays_cleanup_pending(root: Path) -> None:
    store, clock, worktree = _context(root, "partial")
    options = _options(root)
    result = VisibleChampionLaunchService(
        store, FailingAdapter(FakeLaunchAdapter(), cleanup=False), options, clock  # type: ignore[arg-type]
    ).launch(_spec(worktree, "partial"))
    assert result["state"] == "cleanup_pending"
    assert store.connection.execute(
        "SELECT required_policy FROM cleanup_obligations WHERE task_id='task:partial'"
    ).fetchone()[0] == "failed_launch"
    store.close()


class ContextFailureAdapter:
    def __init__(self, *, cleanup: bool) -> None:
        self.base = FakeLaunchAdapter()
        self._created = {"pane_id": "herdr:lux"}
        self.cleanup_result = cleanup

    def launch(self, spec: AssignmentSpec):
        return self.base.launch(spec)

    def deliver_context(self, receipt, context):
        del receipt, context
        raise LaunchAdapterError(
            "synthetic_context_failure", cleanup_required=True, cleanup_proven=False
        )

    def cleanup(self, receipt):
        del receipt
        return self.cleanup_result


def test_context_failure_records_pending_when_cleanup_is_unproven(root: Path) -> None:
    store, clock, worktree = _context(root, "context-pending")
    options = _options(root)
    result = VisibleChampionLaunchService(
        store, ContextFailureAdapter(cleanup=False), options, clock  # type: ignore[arg-type]
    ).launch(_spec(worktree, "context-pending"))
    assert result["state"] == "cleanup_pending" and result["version"] == 4
    assignment = store.connection.execute(
        "SELECT state,failure_class FROM task_assignments WHERE task_assignment_id='assignment:context-pending'"
    ).fetchone()
    assert tuple(assignment) == ("cleanup_pending", "synthetic_context_failure")
    assert store.connection.execute(
        "SELECT state FROM callsign_queue WHERE callsign='Lux'"
    ).fetchone()[0] == "active"
    store.close()


def test_context_failure_exact_cleanup_blocks_and_releases(root: Path) -> None:
    store, clock, worktree = _context(root, "context-clean")
    options = _options(root)
    result = VisibleChampionLaunchService(
        store, ContextFailureAdapter(cleanup=True), options, clock  # type: ignore[arg-type]
    ).launch(_spec(worktree, "context-clean"))
    assert result["state"] == "blocked" and result["version"] == 5
    assert store.connection.execute(
        "SELECT status FROM runtime_instances WHERE runtime_instance_id=?",
        (f"runtime:{LUX_ID}",),
    ).fetchone()[0] == "closed"
    assert store.connection.execute(
        "SELECT state FROM callsign_queue WHERE callsign='Lux'"
    ).fetchone()[0] == "available"
    assert store.connection.execute(
        "SELECT cleanup_state FROM cleanup_obligations WHERE task_id='task:context-clean'"
    ).fetchone()[0] == "cleanup_completed"
    assert store.connection.execute(
        "SELECT retired_at FROM agent_instances WHERE agent_id=?", (LUX_ID,)
    ).fetchone()[0] is not None
    store.close()


def test_generated_thread_mismatch_closes_owned_tab_and_blocks(root: Path) -> None:
    store, clock, worktree = _context(root, "wrong-thread")
    options = _options(root)
    runner = FakeHerdrRunner(worktree, wrong_thread=True)
    result = VisibleChampionLaunchService(
        store, _adapter(options, runner), options, clock
    ).launch(_spec(worktree, "wrong-thread"))
    assert result["state"] == "blocked"
    assert result["failure_class"] == "launch_identity_unverified"
    assert result["cleanup_required"] is True
    assert result["cleanup_proven"] is True
    assert runner.closed is True
    assert store.connection.execute(
        "SELECT state FROM callsign_queue WHERE callsign='Lux'"
    ).fetchone()[0] == "available"
    store.close()


def test_post_launch_activation_refusal_closes_owned_tab_and_blocks(root: Path) -> None:
    store, clock, worktree = _context(root, "activation-refusal")
    options = _options(root)
    runner = FakeHerdrRunner(worktree)

    def refuse_activation(*args, **kwargs):
        del args, kwargs
        raise StorageRefusal("busy", "synthetic activation contention", retryable=True)

    store.activate_assignment = refuse_activation  # type: ignore[method-assign]
    result = VisibleChampionLaunchService(
        store, _adapter(options, runner), options, clock
    ).launch(_spec(worktree, "activation-refusal"))
    assert result["state"] == "blocked"
    assert result["failure_class"] == "launch_busy"
    assert store.connection.execute(
        "SELECT failure_class FROM task_assignments WHERE task_assignment_id='assignment:activation-refusal'"
    ).fetchone()[0] == "launch_busy"
    assert runner.closed is True
    assert store.connection.execute(
        "SELECT state FROM callsign_queue WHERE callsign='Lux'"
    ).fetchone()[0] == "available"
    store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-visible-launch-") as temporary:
        root = Path(temporary)
        test_real_adapter_one_command_success_and_retry(root)
        test_real_adapter_persists_exact_initial_codex_session(root)
        test_pre_session_launch_failure_closes_exact_pending_pane(root)
        test_linked_worktree_trust_binds_owning_repository(root)
        test_unproven_partial_launch_stays_cleanup_pending(root)
        test_context_failure_records_pending_when_cleanup_is_unproven(root)
        test_context_failure_exact_cleanup_blocks_and_releases(root)
        test_generated_thread_mismatch_closes_owned_tab_and_blocks(root)
        test_post_launch_activation_refusal_closes_owned_tab_and_blocks(root)
    print(
        "PASS: visible Champion launch verifies generated identity, delivers bounded context, "
        "deduplicates retries, and preserves exact failure cleanup"
    )


if __name__ == "__main__":
    main()
