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
import league.visible_launch as visible_launch  # noqa: E402
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
SHOTCALLER_PANE_ID = "w1:p1"


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


def test_generated_task_labels_are_deterministic_two_word_names() -> None:
    examples = {
        "Make Cursor and Pi operational across the full League lifecycle": "Cursor Pi",
        "Research and implement cross-provider agent usage metering": "Usage Meter",
        "Add YOLO mode for one bounded launch": "YOLO Mode",
        "Implement issue-coupled Champion cleanup and exact-thread reopen": "Thread Reopen",
        "Preserve Champion title after launch context delivery": "Title Repair",
    }
    for summary, expected in examples.items():
        assert visible_launch.derive_task_label(summary) == expected
        assert len(expected.split()) == 2


def test_task_label_defaults_and_explicit_labels_stay_two_words(root: Path) -> None:
    help_result = subprocess.run(
        (str(ROOT / "bin/league"), "assign", "run", "--help"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "[--task-label TASK_LABEL]" in help_result.stdout

    options = _options(root)
    too_many_words = VisibleLaunchOptions(
        workspace_id=options.workspace_id,
        task_label="Too Many Words",
        model=options.model,
        effort=options.effort,
        league_command=options.league_command,
        state_root=options.state_root,
    )
    try:
        _adapter(too_many_words, FakeHerdrRunner(root))
    except StorageRefusal as exc:
        assert exc.code == "launch_scope_invalid"
    else:
        raise AssertionError("three-word Champion task label was accepted")


class FakeHerdrRunner:
    def __init__(
        self,
        worktree: Path,
        *,
        wrong_thread: bool = False,
        delayed_context_title_reads: int | None = None,
    ) -> None:
        self.worktree = str(worktree.resolve())
        self.wrong_thread = wrong_thread
        self.delayed_context_title_reads = delayed_context_title_reads
        self.started = False
        self.session_reported = False
        self.closed = False
        self.title = ""
        self.tokens: dict[str, str] = {}
        self.metadata_source = "herdr:codex"
        self.source_sequences: dict[str, int] = {}
        self.state_change_seq = 99
        self.pending_title_reads: int | None = None
        self.contexts: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def _agent(self) -> dict[str, object]:
        thread = "not-a-thread" if self.wrong_thread else THREAD_ID
        agent = {
            "agent": "codex",
            "agent_status": "idle",
            "interactive_ready": True,
            "cwd": self.worktree,
            "foreground_cwd": self.worktree,
            "name": "lux",
            "pane_id": "w1:p99",
            "metadata_source": self.metadata_source,
            "state_change_seq": self.state_change_seq,
            "tab_id": "w1:t99",
            "terminal_id": "term_test_99",
            "terminal_title": self.title,
            "terminal_title_stripped": self.title,
            "tokens": dict(self.tokens),
            "workspace_id": "w1",
        }
        agent["agent_session"] = {
            "agent": "codex",
            "kind": "id",
            "source": "herdr:codex",
            "value": thread,
        }
        return agent

    def _advance_delayed_title(self) -> None:
        if self.pending_title_reads is None:
            return
        self.pending_title_reads -= 1
        if self.pending_title_reads > 0:
            return
        self.pending_title_reads = None
        self.metadata_source = "herdr:codex"
        self.state_change_seq += 1
        self.title = "League assignment context | codex"
        self.tokens.update(
            {
                "callsign": "League",
                "sidebar_name": "League assignment context",
                "task_label": "assignment context",
                "thread_title": "League assignment context",
            }
        )

    def active_copy(self) -> "FakeHerdrRunner":
        copied = FakeHerdrRunner(Path(self.worktree))
        copied.started = True
        copied.title = self.title
        copied.tokens = dict(self.tokens)
        copied.metadata_source = self.metadata_source
        copied.source_sequences = dict(self.source_sequences)
        copied.state_change_seq = self.state_change_seq
        return copied

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
            self._advance_delayed_title()
        elif command[:3] == ("herdr", "pane", "report-metadata"):
            source = command[command.index("--source") + 1]
            if "--applies-to-source" in command:
                applies_to = command[command.index("--applies-to-source") + 1]
                if applies_to != "herdr:codex":
                    return subprocess.CompletedProcess(
                        command, 1, "", "metadata source mismatch"
                    )
            sequence = int(command[command.index("--seq") + 1])
            if sequence <= self.source_sequences.get(source, 0):
                return subprocess.CompletedProcess(
                    command, 1, "", "metadata sequence conflict"
                )
            self.source_sequences[source] = sequence
            self.metadata_source = source
            self.state_change_seq += 1
            self.title = command[command.index("--title") + 1]
            self.tokens = {}
            for index, value in enumerate(command):
                if value == "--token":
                    key, token_value = command[index + 1].split("=", 1)
                    self.tokens[key] = token_value
            return subprocess.CompletedProcess(command, 0, "", "")
        elif command[:3] == ("herdr", "agent", "prompt"):
            prompt = command[4]
            if prompt == "/exit":
                self.closed = True
            else:
                self.contexts.append(prompt)
                if (
                    prompt.startswith("League assignment:")
                    and self.delayed_context_title_reads is not None
                ):
                    self.pending_title_reads = self.delayed_context_title_reads
                    if "--wait" in command:
                        while self.pending_title_reads is not None:
                            self._advance_delayed_title()
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
    runner = FakeHerdrRunner(worktree, delayed_context_title_reads=2)
    service = VisibleChampionLaunchService(store, _adapter(options, runner), options, clock)
    spec = _spec(worktree, "success")
    result = service.launch(spec)
    assert result["state"] == "active" and result["version"] == 4
    assert result["context_delivery"]["bytes"] <= 4096
    display_receipt = result["context_delivery"]["display_receipt"]
    assert display_receipt == {
        "source": next(iter(runner.source_sequences)),
        "applies_to_source": "herdr:codex",
        "state_change_seq": runner.state_change_seq,
        "sidebar_name": "Lux",
        "task_label": "Tiny Gate",
        "thread_title": "Lux · Tiny Gate",
        "terminal_title": "Lux · Tiny Gate",
    }
    assert runner.metadata_source == display_receipt["source"]
    assert len(runner.contexts) == 1
    context = runner.contexts[0]
    assert "Use only the stable League SQLite commands" in context
    assert "status.json" not in context and "updates.jsonl" not in context
    tab_index = next(i for i, call in enumerate(runner.calls) if call[:3] == ("herdr", "tab", "create"))
    start_index = next(i for i, call in enumerate(runner.calls) if call[:3] == ("herdr", "agent", "start"))
    context_index = max(i for i, call in enumerate(runner.calls) if call[:3] == ("herdr", "agent", "prompt"))
    metadata_indexes = [
        i
        for i, call in enumerate(runner.calls)
        if call[:3] == ("herdr", "pane", "report-metadata")
    ]
    assert tab_index < start_index < context_index
    assert metadata_indexes[0] < context_index
    assert runner.title == "Lux · Tiny Gate"
    assert runner.tokens["sidebar_name"] == "Lux"
    assert runner.tokens["thread_title"] == "Lux · Tiny Gate"
    start = runner.calls[start_index]
    assert start[start.index("--pane") + 1] == "w1:p99"
    assert start[start.index("--pane") + 1] != SHOTCALLER_PANE_ID
    assert not any(call[:3] == ("herdr", "pane", "split") for call in runner.calls)
    assert not any(call[:3] == ("herdr", "workspace", "create") for call in runner.calls)
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
    retry_runner = runner.active_copy()
    contexts_before_retry = len(runner.contexts)
    retry = VisibleChampionLaunchService(
        store, _adapter(options, retry_runner), options, clock
    ).launch(spec)
    assert retry["idempotent"] is True
    retry_calls = retry_runner.calls
    assert retry_calls
    assert all(call[:3] == ("herdr", "agent", "get") for call in retry_calls)
    assert retry_runner.contexts == []
    assert len(runner.contexts) == contexts_before_retry == 1

    runner.metadata_source = "herdr:codex"
    runner.state_change_seq += 1
    runner.title = "League assignment context | codex"
    runner.tokens.update(
        {
            "callsign": "League",
            "sidebar_name": "League assignment context",
            "task_label": "assignment context",
            "thread_title": "League assignment context",
        }
    )
    calls_before_restore_retry = len(runner.calls)
    restore_retry = service.launch(spec)
    restore_retry_calls = runner.calls[calls_before_restore_retry:]
    assert restore_retry["state"] == "active" and restore_retry["idempotent"] is True
    assert len(
        [
            call
            for call in restore_retry_calls
            if call[:3] == ("herdr", "pane", "report-metadata")
        ]
    ) == 1
    assert not any(
        call[:3] == ("herdr", "agent", "prompt") for call in restore_retry_calls
    )
    assert runner.title == "Lux · Tiny Gate"
    assert runner.tokens["launch_title_source"] == display_receipt["source"]
    assert runner.metadata_source == display_receipt["source"]
    assert len(runner.contexts) == contexts_before_retry == 1
    durable_display_receipt = store.assignment_launch_context(
        spec.assignment_id
    )["context_delivery"]["display_receipt"]
    assert durable_display_receipt == restore_retry["context_delivery"]["display_receipt"]
    assert durable_display_receipt["source"] == runner.metadata_source
    assert durable_display_receipt["state_change_seq"] == runner.state_change_seq
    revalidation_events = store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='assignment_title_revalidated' AND aggregate_id=?",
        (spec.assignment_id,),
    ).fetchone()[0]
    assert revalidation_events == 1
    calls_before_exact_revalidation = len(runner.calls)
    exact_revalidation = service.launch(spec)
    assert exact_revalidation["context_delivery"]["display_receipt"] == durable_display_receipt
    assert store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='assignment_title_revalidated' AND aggregate_id=?",
        (spec.assignment_id,),
    ).fetchone()[0] == revalidation_events
    assert not any(
        call[:3]
        in {
            ("herdr", "pane", "report-metadata"),
            ("herdr", "agent", "prompt"),
        }
        for call in runner.calls[calls_before_exact_revalidation:]
    )
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


class UnownedContextTitleRunner(FakeHerdrRunner):
    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        completed = super().run(arguments, timeout_seconds=timeout_seconds)
        if (
            command[:3] == ("herdr", "agent", "prompt")
            and command[4].startswith("League assignment:")
        ):
            self.title = "User selected title"
            self.tokens["launch_title_owner"] = "unowned"
        return completed


def test_post_context_title_restoration_refuses_unowned_metadata(root: Path) -> None:
    store, clock, worktree = _context(root, "unowned-title")
    options = _options(root)
    runner = UnownedContextTitleRunner(worktree)
    result = VisibleChampionLaunchService(
        store, _adapter(options, runner), options, clock
    ).launch(_spec(worktree, "unowned-title"))
    assert result["state"] == "blocked" and result["version"] == 5
    assert store.connection.execute(
        "SELECT failure_class FROM task_assignments WHERE task_assignment_id='assignment:unowned-title'"
    ).fetchone()[0] == "launch_title_restore_refused"
    assert len(
        [
            call
            for call in runner.calls
            if call[:3] == ("herdr", "pane", "report-metadata")
        ]
    ) == 1
    assert runner.closed is True
    assert store.connection.execute(
        "SELECT state FROM callsign_queue WHERE callsign='Lux'"
    ).fetchone()[0] == "available"
    store.close()


def test_active_retry_refuses_newer_user_metadata(root: Path) -> None:
    store, clock, worktree = _context(root, "retry-unowned-title")
    options = _options(root)
    runner = FakeHerdrRunner(worktree, delayed_context_title_reads=2)
    service = VisibleChampionLaunchService(
        store, _adapter(options, runner), options, clock
    )
    spec = _spec(worktree, "retry-unowned-title")
    first = service.launch(spec)
    assert first["state"] == "active"

    runner.metadata_source = "user-selected"
    runner.state_change_seq += 1
    runner.title = "User selected title"
    stale_launch_tokens = dict(runner.tokens)
    calls_before_retry = len(runner.calls)
    contexts_before_retry = len(runner.contexts)
    result = service.launch(spec)

    assert result["state"] == "cleanup_pending" and result["version"] == 5
    retry_calls = runner.calls[calls_before_retry:]
    assert retry_calls
    assert not any(
        call[:3] == ("herdr", "pane", "report-metadata") for call in retry_calls
    )
    assert runner.metadata_source == "user-selected"
    assert runner.title == "User selected title"
    assert runner.tokens == stale_launch_tokens
    assert len(runner.contexts) == contexts_before_retry == 1
    launch = store.assignment_launch_context(spec.assignment_id)
    assert launch["runtime_instance_id"] == f"runtime:{LUX_ID}"
    assert launch["failure_class"] == "launch_title_restore_refused"
    assert store.connection.execute(
        "SELECT cleanup_state FROM cleanup_obligations WHERE task_id=?",
        (spec.task_id,),
    ).fetchone()[0] == "pending"
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


class FailedMetadataRunner(FakeHerdrRunner):
    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        if command[:3] == ("herdr", "pane", "report-metadata"):
            del timeout_seconds
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "metadata refused")
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


def test_metadata_silent_success_is_exact_and_failure_stays_closed(root: Path) -> None:
    store, clock, worktree = _context(root, "metadata-failure")
    options = _options(root)
    runner = FailedMetadataRunner(worktree)
    result = VisibleChampionLaunchService(
        store, _adapter(options, runner), options, clock
    ).launch(_spec(worktree, "metadata-failure"))
    assert result["state"] == "blocked"
    assert result["failure_class"] == "launch_adapter_failed"
    assert result["cleanup_proven"] is True
    assert runner.closed is True
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
        test_generated_task_labels_are_deterministic_two_word_names()
        test_task_label_defaults_and_explicit_labels_stay_two_words(root)
        test_real_adapter_one_command_success_and_retry(root)
        test_post_context_title_restoration_refuses_unowned_metadata(root)
        test_active_retry_refuses_newer_user_metadata(root)
        test_real_adapter_persists_exact_initial_codex_session(root)
        test_pre_session_launch_failure_closes_exact_pending_pane(root)
        test_metadata_silent_success_is_exact_and_failure_stays_closed(root)
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
