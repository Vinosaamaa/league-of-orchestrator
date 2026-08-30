#!/usr/bin/env python3
"""Focused one-command visible Champion launch and failure recovery coverage."""

from __future__ import annotations

import json
import hashlib
import subprocess
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.request_services import AssignmentSpec, LaunchAdapterError  # noqa: E402
from league.storage import (  # noqa: E402
    LegacyDisplayReconciliationCommand,
    StorageRefusal,
)
from league.issue_first import (  # noqa: E402
    GitHubIssueVerifier,
    issue_scope_digest,
    normalize_issue_title,
    semantic_scope_digest,
)
from league.sqlite_project_ops import canonical_repository  # noqa: E402
import league.visible_launch as visible_launch  # noqa: E402
from league.storage_issue import (  # noqa: E402
    BeginIssueSelectionCommand,
    CompleteIssueSelectionCommand,
)
from league.visible_launch import (  # noqa: E402
    HerdrCodexLaunchAdapter,
    VisibleChampionLaunchService,
    VisibleLaunchOptions,
    _codex_trust_root,
)
from league.legacy_display_reconciliation import (  # noqa: E402
    HerdrLegacyDisplayAdapter,
    LegacyDisplayReconciliationService,
    LegacyDisplayReconciliationSpec,
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
ISSUE_BODY = """## Objective
Perform one tiny synthetic Champion task.

## Verification
Verify the synthetic launch lifecycle.

## Hard boundaries
Use only synthetic adapters and temporary state.
"""


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
        issue_receipt=None,
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


def test_legacy_display_command_exposes_exact_owner_cas_inputs() -> None:
    result = subprocess.run(
        (str(ROOT / "bin/league"), "assign", "reconcile-legacy-display", "--help"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    for option in (
        "--assignment-id",
        "--champion-agent-id",
        "--runtime-instance-id",
        "--callsign",
        "--pane-id",
        "--terminal-id",
        "--thread-id",
        "--worktree",
        "--routing-name",
        "--expected-presentation-json",
        "--target-task-label",
        "--owner-authorized",
    ):
        assert option in result.stdout


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
        _adapter(too_many_words, FakeHerdrRunner(root), None)
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
        routing_name: str = "lux",
    ) -> None:
        self.worktree = str(worktree.resolve())
        self.wrong_thread = wrong_thread
        self.delayed_context_title_reads = delayed_context_title_reads
        self.routing_name = routing_name
        self.started = False
        self.session_reported = False
        self.closed = False
        self.title = ""
        self.tokens: dict[str, str] = {}
        self.metadata_source = "herdr:codex"
        self.source_sequences: dict[str, int] = {}
        self.overlay_baselines: dict[
            str, tuple[str | None, str, dict[str, str], str, dict[str, str]]
        ] = {}
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
            "name": self.routing_name,
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
        copied = FakeHerdrRunner(Path(self.worktree), routing_name=self.routing_name)
        copied.started = True
        copied.title = self.title
        copied.tokens = dict(self.tokens)
        copied.metadata_source = self.metadata_source
        copied.source_sequences = dict(self.source_sequences)
        copied.overlay_baselines = {
            source: (
                baseline_source,
                baseline_title,
                dict(baseline_tokens),
                overlay_title,
                dict(overlay_tokens),
            )
            for source, (
                baseline_source,
                baseline_title,
                baseline_tokens,
                overlay_title,
                overlay_tokens,
            ) in self.overlay_baselines.items()
        }
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
            self.state_change_seq += 1
            if "--clear-title" in command and source in self.overlay_baselines:
                (
                    baseline_source,
                    baseline_title,
                    baseline_tokens,
                    overlay_title,
                    overlay_tokens,
                ) = self.overlay_baselines.pop(source)
                current_source = self.metadata_source
                current_title = self.title
                current_tokens = dict(self.tokens)
                self.metadata_source = baseline_source
                self.title = baseline_title
                self.tokens = dict(baseline_tokens)
                if current_source != source or current_title != overlay_title:
                    self.metadata_source = current_source
                    self.title = current_title
                for key, value in current_tokens.items():
                    if overlay_tokens.get(key) != value:
                        self.tokens[key] = value
                return subprocess.CompletedProcess(command, 0, "", "")
            prior_source = self.metadata_source
            prior_title = self.title
            prior_tokens = dict(self.tokens)
            self.metadata_source = source
            self.title = command[command.index("--title") + 1]
            for index, value in enumerate(command):
                if value == "--token":
                    key, token_value = command[index + 1].split("=", 1)
                    self.tokens[key] = token_value
            if source.startswith("league-legacy-"):
                baseline = self.overlay_baselines.get(source)
                self.overlay_baselines[source] = (
                    baseline[0] if baseline else prior_source,
                    baseline[1] if baseline else prior_title,
                    dict(baseline[2]) if baseline else prior_tokens,
                    self.title,
                    dict(self.tokens),
                )
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


class FakeIssueVerifier:
    def __init__(
        self,
        *,
        store=None,
        repository: str | None = None,
        state: str = "open",
        persist_selection: bool = True,
        receipt_scope_digest: str | None = None,
    ) -> None:
        self.store = store
        self.repository = repository
        self.state = state
        self.persist_selection = persist_selection
        self.receipt_scope_digest = receipt_scope_digest
        self.calls = 0

    def verify(self, spec: AssignmentSpec, at: str) -> dict[str, object]:
        self.calls += 1
        repository = self.repository or spec.repository
        repository_key = canonical_repository(repository)[1]
        issue_url = f"https://{repository_key}/issues/{spec.issue}"
        issue_title = spec.task_summary
        normalized_title = normalize_issue_title(issue_title)
        scope_digest = semantic_scope_digest(ISSUE_BODY)
        selection_identity = "\0".join(
            (repository_key, normalized_title, scope_digest)
        ).encode("utf-8")
        selection_key = f"issue-scope:{hashlib.sha256(selection_identity).hexdigest()}"
        selection_digest = "d" * 64
        if self.persist_selection and self.store is not None:
            acquired = self.store.begin_issue_selection(
                BeginIssueSelectionCommand(
                    selection_key=selection_key,
                    task_id=spec.task_id,
                    task_summary=spec.task_summary,
                    coordinator_agent_id=spec.coordinator_agent_id,
                    repository=repository,
                    repository_key=repository_key,
                    normalized_title=normalized_title,
                    semantic_scope_digest=scope_digest,
                    owner_attempt_id=f"attempt:{spec.task_id}",
                    lease_expires_at="2099-01-01T00:00:00Z",
                    at=at,
                )
            )
            if acquired["state"] == "completed":
                selected = acquired["receipt"]
            else:
                selected = self.store.complete_issue_selection(
                    CompleteIssueSelectionCommand(
                        selection_key=selection_key,
                        expected_version=acquired["version"],
                        owner_attempt_id=f"attempt:{spec.task_id}",
                        task_id=spec.task_id,
                        task_summary=spec.task_summary,
                        coordinator_agent_id=spec.coordinator_agent_id,
                        repository=repository,
                        repository_key=repository_key,
                        normalized_title=normalized_title,
                        semantic_scope_digest=scope_digest,
                        decision="reuse_open",
                        issue=spec.issue,
                        issue_url=issue_url,
                        issue_title=issue_title,
                        issue_body_digest=hashlib.sha256(ISSUE_BODY.encode()).hexdigest(),
                        duplicate_matches=1,
                        reopen_action_receipt_digest=None,
                        at=at,
                    )
                )
            selection_digest = selected["receipt_digest"]
        receipt: dict[str, object] = {
            "schema": "league.repository-issue.v1",
            "repository": repository,
            "repository_key": repository_key,
            "issue": spec.issue,
            "issue_url": issue_url,
            "issue_state": self.state,
            "issue_title": issue_title,
            "normalized_title": normalized_title,
            "issue_body_digest": hashlib.sha256(ISSUE_BODY.encode()).hexdigest(),
            "semantic_scope_digest": self.receipt_scope_digest or scope_digest,
            "task_scope_digest": issue_scope_digest(
                repository, spec.issue, spec.task_id, spec.task_summary
            ),
            "issue_selection_receipt_digest": selection_digest,
            "verifier_kind": "synthetic-fixture",
            "verified_at": at,
        }
        receipt["receipt_digest"] = hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return receipt


def _adapter(options: VisibleLaunchOptions, runner: FakeHerdrRunner, store):
    adapter = HerdrCodexLaunchAdapter(
        options,
        runner,
        environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
    )
    adapter.issue_verifier = FakeIssueVerifier(store=store)
    return adapter


def test_real_adapter_one_command_success_and_retry(root: Path) -> None:
    store, clock, worktree = _context(root, "success")
    options = _options(root)
    runner = FakeHerdrRunner(worktree, delayed_context_title_reads=2)
    service = VisibleChampionLaunchService(store, _adapter(options, runner, store), options, clock)
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
        store, _adapter(options, retry_runner, store), options, clock
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


def test_legacy_active_champion_display_is_reconciled_once_with_exact_receipt(
    root: Path,
) -> None:
    store, clock, worktree = _context(root, "legacy-display")
    store.connection.execute(
        "INSERT INTO callsigns(callsign,pool_role,enabled,pool_position,last_released_at) VALUES('Shaco','champion',1,99,NULL)"
    )
    store.connection.execute(
        "INSERT INTO callsign_queue(callsign,pool_role,queue_position,state,reservation_assignment_id,version,updated_at) VALUES('Shaco','champion',-1,'available',NULL,1,?)",
        (clock.now(),),
    )
    options = _options(root)
    launch_runner = FakeHerdrRunner(worktree, routing_name="shaco")
    launch = VisibleChampionLaunchService(
        store, _adapter(options, launch_runner), options, clock
    ).launch(_spec(worktree, "legacy-display"))
    assignment_id = launch["assignment_id"]
    receipt = store.assignment_launch_context(assignment_id)["acceptance_receipt"]
    event = store.connection.execute(
        "SELECT event_id,detail_json FROM events WHERE aggregate_id=? AND event_type='assignment_context_delivered'",
        (assignment_id,),
    ).fetchone()
    legacy_detail = json.loads(event["detail_json"])
    legacy_detail.pop("display_receipt")
    store.connection.execute(
        "UPDATE events SET detail_json=? WHERE event_id=?",
        (json.dumps(legacy_detail, sort_keys=True, separators=(",", ":")), event["event_id"]),
    )

    runner = launch_runner.active_copy()
    runner.metadata_source = "herdr:codex"
    runner.state_change_seq += 1
    runner.title = "Prepare handshake only"
    runner.tokens = {
        "sidebar_name": "Prepare handshake only",
        "thread_title": "Prepare handshake only",
        "user_accent": "keep-me",
    }
    calls_before = len(runner.calls)
    spec = LegacyDisplayReconciliationSpec(
        assignment_id=assignment_id,
        expected_version=launch["version"],
        champion_agent_id=LUX_ID,
        runtime_instance_id=launch["runtime_instance_id"],
        callsign="Shaco",
        pane_id=receipt["endpoint"],
        terminal_id="term_test_99",
        thread_id=receipt["thread_id"],
        worktree=str(worktree.resolve()),
        routing_name="shaco",
        expected_presentation_source="herdr:codex",
        expected_title="Prepare handshake only",
        expected_state_change_seq=runner.state_change_seq,
        target_task_label="Broker Repair",
        owner_authorized=True,
    )
    service = LegacyDisplayReconciliationService(
        store,
        HerdrLegacyDisplayAdapter(
            runner,
            environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
        ),
        clock,
    )

    finalize = store.finalize_legacy_display_reconciliation

    def interrupt_after_effect(*args, **kwargs):
        del args, kwargs
        raise StorageRefusal(
            "synthetic_reconciliation_interrupt",
            "synthetic interruption after Herdr metadata effect",
        )

    store.finalize_legacy_display_reconciliation = interrupt_after_effect  # type: ignore[method-assign]
    try:
        service.reconcile(spec)
    except StorageRefusal as exc:
        assert exc.code == "synthetic_reconciliation_interrupt"
    else:
        raise AssertionError("synthetic post-effect interruption did not stop finalization")
    assert store.assignment_launch_context(assignment_id)[
        "legacy_display_reconciliation"
    ]["receipt"] is None
    store.finalize_legacy_display_reconciliation = finalize  # type: ignore[method-assign]

    result = service.reconcile(spec)
    assert result["state"] == "reconciled"
    assert result["receipt"]["terminal_title"] == "Shaco · Broker Repair"
    assert runner.tokens["sidebar_name"] == "Shaco"
    assert runner.tokens["task_label"] == "Broker Repair"
    assert runner.tokens["thread_title"] == "Shaco · Broker Repair"
    assert runner.tokens["user_accent"] == "keep-me"
    assert len(
        [
            call
            for call in runner.calls[calls_before:]
            if call[:3] == ("herdr", "pane", "report-metadata")
        ]
    ) == 1

    retry = service.reconcile(spec)
    assert retry["receipt"] == result["receipt"]
    assert len(
        [call for call in runner.calls if call[:3] == ("herdr", "pane", "report-metadata")]
    ) == 1
    try:
        service.reconcile(
            LegacyDisplayReconciliationSpec(
                **{**vars(spec), "target_task_label": "Different Repair"}
            )
        )
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_conflict"
    else:
        raise AssertionError("legacy reconciliation retry changed its target")
    assert len(
        [call for call in runner.calls if call[:3] == ("herdr", "pane", "report-metadata")]
    ) == 1
    durable = store.assignment_launch_context(assignment_id)
    assert durable["legacy_display_reconciliation"]["intent"]["owner_authorized"] is True
    assert durable["legacy_display_reconciliation"]["receipt"] == result["receipt"]
    store.connection.execute(
        "UPDATE events SET detail_json=? WHERE aggregate_id=? AND event_type='assignment_legacy_display_reconciled'",
        (json.dumps({"receipt": {"source": "malformed"}}), assignment_id),
    )
    try:
        store.assignment_launch_context(assignment_id)
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_ambiguous"
    else:
        raise AssertionError("malformed legacy final history escaped controlled refusal")
    store.close()


class UserTitleRaceLegacyRunner(FakeHerdrRunner):
    def __init__(self, worktree: Path) -> None:
        super().__init__(worktree)
        self.legacy_get_reads = 0

    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        if command[:3] == ("herdr", "agent", "get"):
            self.legacy_get_reads += 1
            if self.legacy_get_reads == 2:
                self.metadata_source = "user-selected"
                self.state_change_seq += 1
                self.title = "User selected title"
                self.tokens["user_note"] = "preserve"
        return super().run(arguments, timeout_seconds=timeout_seconds)


class UserTitleAtCompareAndSetRunner(FakeHerdrRunner):
    """A user title lands in the final read-to-write window."""

    def __init__(self, worktree: Path) -> None:
        super().__init__(worktree)
        self.raced = False

    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        completed = super().run(arguments, timeout_seconds=timeout_seconds)
        if (
            command[:3] == ("herdr", "pane", "report-metadata")
            and "--title" in command
            and completed.returncode == 0
            and not self.raced
        ):
            self.raced = True
            self.metadata_source = "user-selected"
            self.state_change_seq += 1
            self.source_sequences["user-selected"] = self.state_change_seq
            self.title = "User selected title"
            self.tokens["user_note"] = "preserve"
        return completed


def _legacy_reconciliation_spec(
    launch: dict[str, object],
    receipt: dict[str, object],
    worktree: Path,
    runner: FakeHerdrRunner,
) -> LegacyDisplayReconciliationSpec:
    return LegacyDisplayReconciliationSpec(
        assignment_id=str(launch["assignment_id"]),
        expected_version=int(launch["version"]),
        champion_agent_id=LUX_ID,
        runtime_instance_id=str(launch["runtime_instance_id"]),
        callsign="Lux",
        pane_id=str(receipt["endpoint"]),
        terminal_id="term_test_99",
        thread_id=str(receipt["thread_id"]),
        worktree=str(worktree.resolve()),
        routing_name="lux",
        expected_presentation_source=runner.metadata_source,
        expected_title=runner.title,
        expected_state_change_seq=runner.state_change_seq,
        target_task_label="Broker Repair",
        owner_authorized=True,
    )


def test_legacy_display_reconciliation_refuses_user_title_race_before_write(
    root: Path,
) -> None:
    store, clock, worktree = _context(root, "legacy-user-race")
    options = _options(root)
    launched_runner = FakeHerdrRunner(worktree)
    launch = VisibleChampionLaunchService(
        store, _adapter(options, launched_runner), options, clock
    ).launch(_spec(worktree, "legacy-user-race"))
    assignment_id = str(launch["assignment_id"])
    receipt = store.assignment_launch_context(assignment_id)["acceptance_receipt"]
    event = store.connection.execute(
        "SELECT event_id,detail_json FROM events WHERE aggregate_id=? AND event_type='assignment_context_delivered'",
        (assignment_id,),
    ).fetchone()
    detail = json.loads(event["detail_json"])
    detail.pop("display_receipt")
    store.connection.execute(
        "UPDATE events SET detail_json=? WHERE event_id=?",
        (json.dumps(detail, sort_keys=True, separators=(",", ":")), event["event_id"]),
    )
    runner = UserTitleRaceLegacyRunner(worktree)
    runner.started = True
    runner.title = "Prepare handshake only"
    runner.tokens = {
        "sidebar_name": "Prepare handshake only",
        "thread_title": "Prepare handshake only",
    }
    runner.state_change_seq = launched_runner.state_change_seq + 1
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, runner)
    service = LegacyDisplayReconciliationService(
        store,
        HerdrLegacyDisplayAdapter(
            runner,
            environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
        ),
        clock,
    )

    try:
        service.reconcile(spec)
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_race"
    else:
        raise AssertionError("newer user title was overwritten by legacy reconciliation")
    assert runner.title == "User selected title"
    assert runner.metadata_source == "user-selected"
    assert runner.tokens["user_note"] == "preserve"
    assert not any(
        call[:3] == ("herdr", "pane", "report-metadata") for call in runner.calls
    )
    durable = store.assignment_launch_context(assignment_id)
    assert durable["legacy_display_reconciliation"]["receipt"] is None
    store.close()


def test_legacy_display_compare_and_set_does_not_overwrite_last_window_user_title(
    root: Path,
) -> None:
    store, clock, worktree = _context(root, "legacy-cas-race")
    options = _options(root)
    launched_runner = FakeHerdrRunner(worktree)
    launch = VisibleChampionLaunchService(
        store, _adapter(options, launched_runner), options, clock
    ).launch(_spec(worktree, "legacy-cas-race"))
    assignment_id = str(launch["assignment_id"])
    receipt = store.assignment_launch_context(assignment_id)["acceptance_receipt"]
    event = store.connection.execute(
        "SELECT event_id,detail_json FROM events WHERE aggregate_id=? AND event_type='assignment_context_delivered'",
        (assignment_id,),
    ).fetchone()
    detail = json.loads(event["detail_json"])
    detail.pop("display_receipt")
    store.connection.execute(
        "UPDATE events SET detail_json=? WHERE event_id=?",
        (json.dumps(detail, sort_keys=True, separators=(",", ":")), event["event_id"]),
    )
    runner = UserTitleAtCompareAndSetRunner(worktree)
    runner.started = True
    runner.title = "Prepare handshake only"
    runner.tokens = {
        "sidebar_name": "Prepare handshake only",
        "thread_title": "Prepare handshake only",
    }
    runner.state_change_seq = launched_runner.state_change_seq + 1
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, runner)
    service = LegacyDisplayReconciliationService(
        store,
        HerdrLegacyDisplayAdapter(
            runner,
            environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
        ),
        clock,
    )

    try:
        service.reconcile(spec)
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_race"
    else:
        raise AssertionError("last-window user title race was overwritten")
    assert runner.title == "User selected title"
    assert runner.metadata_source == "user-selected"
    assert runner.tokens["user_note"] == "preserve"
    assert runner.tokens["sidebar_name"] == "Prepare handshake only"
    assert "legacy_display_owner" not in runner.tokens
    assert "legacy_display_assignment" not in runner.tokens
    reports = [
        call for call in runner.calls if call[:3] == ("herdr", "pane", "report-metadata")
    ]
    assert len(reports) == 2
    assert "--clear-title" in reports[-1]
    assert store.assignment_launch_context(assignment_id)[
        "legacy_display_reconciliation"
    ]["receipt"] is None
    store.close()


def test_legacy_display_reconciliation_refuses_modern_receipt_before_live_write(
    root: Path,
) -> None:
    store, clock, worktree = _context(root, "modern-display")
    options = _options(root)
    runner = FakeHerdrRunner(worktree)
    launch = VisibleChampionLaunchService(
        store, _adapter(options, runner), options, clock
    ).launch(_spec(worktree, "modern-display"))
    receipt = store.assignment_launch_context(str(launch["assignment_id"]))[
        "acceptance_receipt"
    ]
    active = runner.active_copy()
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, active)

    try:
        LegacyDisplayReconciliationService(
            store,
            HerdrLegacyDisplayAdapter(
                active,
                environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
            ),
            clock,
        ).reconcile(spec)
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_modern"
    else:
        raise AssertionError("modern display receipt entered legacy reconciliation")
    assert active.calls == []
    assert store.assignment_launch_context(str(launch["assignment_id"]))[
        "legacy_display_reconciliation"
    ] is None
    store.close()


def test_legacy_display_reconciliation_refuses_ambiguous_runtime_and_wrong_route(
    root: Path,
) -> None:
    store, clock, worktree = _context(root, "legacy-ambiguous")
    options = _options(root)
    launched_runner = FakeHerdrRunner(worktree)
    launch = VisibleChampionLaunchService(
        store, _adapter(options, launched_runner), options, clock
    ).launch(_spec(worktree, "legacy-ambiguous"))
    assignment_id = str(launch["assignment_id"])
    receipt = store.assignment_launch_context(assignment_id)["acceptance_receipt"]
    event = store.connection.execute(
        "SELECT event_id,detail_json FROM events WHERE aggregate_id=? AND event_type='assignment_context_delivered'",
        (assignment_id,),
    ).fetchone()
    detail = json.loads(event["detail_json"])
    detail.pop("display_receipt")
    store.connection.execute(
        "UPDATE events SET detail_json=? WHERE event_id=?",
        (json.dumps(detail, sort_keys=True, separators=(",", ":")), event["event_id"]),
    )
    active = launched_runner.active_copy()
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, active)
    unauthorized = LegacyDisplayReconciliationSpec(
        **{**vars(spec), "owner_authorized": False}
    )
    unauthorized_service = LegacyDisplayReconciliationService(
        store,
        HerdrLegacyDisplayAdapter(
            active,
            environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
        ),
        clock,
    )
    try:
        unauthorized_service.reconcile(unauthorized)
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_invalid"
    else:
        raise AssertionError("legacy reconciliation accepted no owner authorization")
    store.connection.execute(
        """
        INSERT INTO runtime_instances
          (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,endpoint,
           runtime_generation,status,verified,last_seen_at,capabilities_json)
        VALUES('runtime:synthetic-duplicate',?,'codex-thread','herdr',
               '44444444-4444-4444-8444-444444444444','w1:p100',
               'herdr:synthetic-duplicate','active',1,?,'{}')
        """,
        (LUX_ID, clock.now()),
    )
    service = LegacyDisplayReconciliationService(
        store,
        HerdrLegacyDisplayAdapter(
            active,
            environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
        ),
        clock,
    )
    for changed in (
        spec,
        LegacyDisplayReconciliationSpec(
            **{**vars(spec), "callsign": "Sona", "routing_name": "sona"}
        ),
    ):
        try:
            service.reconcile(changed)
        except StorageRefusal as exc:
            assert exc.code == "legacy_display_conflict"
        else:
            raise AssertionError("ambiguous runtime or wrong route was accepted")
    assert active.calls == []
    assert store.assignment_launch_context(assignment_id)[
        "legacy_display_reconciliation"
    ] is None
    store.close()


def _prepared_legacy_display(
    root: Path, name: str
) -> tuple[object, object, Path, dict[str, object], dict[str, object], FakeHerdrRunner]:
    store, clock, worktree = _context(root, name)
    options = _options(root)
    launched_runner = FakeHerdrRunner(worktree)
    launch = VisibleChampionLaunchService(
        store, _adapter(options, launched_runner), options, clock
    ).launch(_spec(worktree, name))
    assignment_id = str(launch["assignment_id"])
    receipt = store.assignment_launch_context(assignment_id)["acceptance_receipt"]
    event = store.connection.execute(
        "SELECT event_id,detail_json FROM events WHERE aggregate_id=? AND event_type='assignment_context_delivered'",
        (assignment_id,),
    ).fetchone()
    detail = json.loads(event["detail_json"])
    detail.pop("display_receipt")
    store.connection.execute(
        "UPDATE events SET detail_json=? WHERE event_id=?",
        (json.dumps(detail, sort_keys=True, separators=(",", ":")), event["event_id"]),
    )
    runner = launched_runner.active_copy()
    runner.title = "Prepare handshake only"
    runner.tokens = {
        "sidebar_name": "Prepare handshake only",
        "thread_title": "Prepare handshake only",
    }
    runner.state_change_seq += 1
    return store, clock, worktree, launch, receipt, runner


def test_legacy_display_reconciliation_requires_live_owned_presentation_source(
    root: Path,
) -> None:
    store, clock, worktree, launch, receipt, runner = _prepared_legacy_display(
        root, "legacy-provider-guards"
    )
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, runner)
    service = LegacyDisplayReconciliationService(
        store,
        HerdrLegacyDisplayAdapter(
            runner,
            environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
        ),
        clock,
    )

    original_agent = runner._agent
    for mutate in (
        lambda agent: agent.update(agent_status="done"),
        lambda agent: agent.pop("metadata_source"),
    ):
        runner._agent = lambda mutate=mutate: (  # type: ignore[method-assign]
            lambda agent: (mutate(agent), agent)[1]
        )(original_agent())
        try:
            service.reconcile(spec)
        except StorageRefusal as exc:
            assert exc.code == "legacy_display_identity_unverified"
        else:
            raise AssertionError("non-live or source-less legacy display was mutated")
        assert not any(
            call[:3] == ("herdr", "pane", "report-metadata") for call in runner.calls
        )
    runner._agent = original_agent  # type: ignore[method-assign]
    store.close()


def test_legacy_display_reconciliation_refuses_boolean_sequence(root: Path) -> None:
    store, _clock, worktree, launch, receipt, runner = _prepared_legacy_display(
        root, "legacy-boolean-sequence"
    )
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, runner)
    command = LegacyDisplayReconciliationCommand(
        **{
            **vars(spec),
            "expected_state_change_seq": True,
            "at": "2026-08-30T12:00:00Z",
        }
    )
    try:
        store.begin_legacy_display_reconciliation(command)
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_invalid"
    else:
        raise AssertionError("boolean legacy display sequence was accepted as integer one")
    assert store.assignment_launch_context(str(launch["assignment_id"]))[
        "legacy_display_reconciliation"
    ] is None
    store.close()


def test_legacy_display_reconciliation_refuses_orphaned_final_receipt(
    root: Path,
) -> None:
    store, clock, worktree, launch, receipt, runner = _prepared_legacy_display(
        root, "legacy-orphan-result"
    )
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, runner)
    service = LegacyDisplayReconciliationService(
        store,
        HerdrLegacyDisplayAdapter(
            runner,
            environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
        ),
        clock,
    )
    service.reconcile(spec)
    store.connection.execute(
        "DELETE FROM events WHERE aggregate_id=? AND event_type='assignment_legacy_display_reconciliation_intent'",
        (launch["assignment_id"],),
    )

    try:
        store.assignment_launch_context(str(launch["assignment_id"]))
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_ambiguous"
    else:
        raise AssertionError("orphaned legacy display result was reported as no history")
    reports_before = len(
        [call for call in runner.calls if call[:3] == ("herdr", "pane", "report-metadata")]
    )
    try:
        service.reconcile(spec)
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_ambiguous"
    else:
        raise AssertionError("a new legacy display intent was created around an orphan result")
    assert store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE aggregate_id=? AND event_type='assignment_legacy_display_reconciliation_intent'",
        (launch["assignment_id"],),
    ).fetchone()[0] == 0
    assert len(
        [call for call in runner.calls if call[:3] == ("herdr", "pane", "report-metadata")]
    ) == reports_before
    store.close()


def test_legacy_display_readback_refuses_expected_plus_two_receipt(root: Path) -> None:
    store, clock, worktree, launch, receipt, runner = _prepared_legacy_display(
        root, "legacy-plus-two-receipt"
    )
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, runner)
    LegacyDisplayReconciliationService(
        store,
        HerdrLegacyDisplayAdapter(
            runner,
            environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
        ),
        clock,
    ).reconcile(spec)
    final = store.connection.execute(
        "SELECT event_id,detail_json FROM events WHERE aggregate_id=? AND event_type='assignment_legacy_display_reconciled'",
        (launch["assignment_id"],),
    ).fetchone()
    detail = json.loads(final["detail_json"])
    detail["receipt"]["state_change_seq"] = spec.expected_state_change_seq + 2
    store.connection.execute(
        "UPDATE events SET detail_json=? WHERE event_id=?",
        (json.dumps(detail, sort_keys=True, separators=(",", ":")), final["event_id"]),
    )
    try:
        store.assignment_launch_context(str(launch["assignment_id"]))
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_ambiguous"
    else:
        raise AssertionError("expected-plus-two legacy display receipt was accepted")
    store.close()


def test_legacy_display_interrupted_effect_refuses_newer_sequence(root: Path) -> None:
    store, clock, worktree, launch, receipt, runner = _prepared_legacy_display(
        root, "legacy-interrupted-race"
    )
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, runner)
    service = LegacyDisplayReconciliationService(
        store,
        HerdrLegacyDisplayAdapter(
            runner,
            environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
        ),
        clock,
    )
    finalize = store.finalize_legacy_display_reconciliation

    def interrupt_after_effect(*args, **kwargs):
        del args, kwargs
        raise StorageRefusal(
            "synthetic_reconciliation_interrupt",
            "synthetic interruption after Herdr metadata effect",
        )

    store.finalize_legacy_display_reconciliation = interrupt_after_effect  # type: ignore[method-assign]
    try:
        service.reconcile(spec)
    except StorageRefusal as exc:
        assert exc.code == "synthetic_reconciliation_interrupt"
    else:
        raise AssertionError("synthetic post-effect interruption did not stop finalization")
    store.finalize_legacy_display_reconciliation = finalize  # type: ignore[method-assign]
    runner.state_change_seq += 1
    runner.tokens["launch_title_owner"] = "new-owner"

    try:
        service.reconcile(spec)
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_race"
    else:
        raise AssertionError("interrupted retry absorbed a newer endpoint sequence")
    assert runner.metadata_source == spec.expected_presentation_source
    assert "legacy_display_owner" not in runner.tokens
    assert runner.tokens["launch_title_owner"] == "new-owner"
    assert store.assignment_launch_context(str(launch["assignment_id"]))[
        "legacy_display_reconciliation"
    ]["receipt"] is None
    store.close()


def test_legacy_display_refuses_malformed_modern_receipt(root: Path) -> None:
    store, clock, worktree, launch, receipt, runner = _prepared_legacy_display(
        root, "legacy-malformed-modern"
    )
    event = store.connection.execute(
        "SELECT event_id,detail_json FROM events WHERE aggregate_id=? AND event_type='assignment_context_delivered'",
        (launch["assignment_id"],),
    ).fetchone()
    detail = json.loads(event["detail_json"])
    detail["display_receipt"] = {
        "source": "league-launch-malformed",
        "state_change_seq": "not-an-integer",
    }
    store.connection.execute(
        "UPDATE events SET detail_json=? WHERE event_id=?",
        (json.dumps(detail, sort_keys=True, separators=(",", ":")), event["event_id"]),
    )
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, runner)
    try:
        LegacyDisplayReconciliationService(
            store,
            HerdrLegacyDisplayAdapter(
                runner,
                environment={"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"},
            ),
            clock,
        ).reconcile(spec)
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_ambiguous"
    else:
        raise AssertionError("malformed modern receipt was reclassified as legacy")
    assert runner.calls == []
    store.close()


def test_legacy_display_refuses_missing_acceptance_worktree(root: Path) -> None:
    store, clock, worktree, launch, receipt, runner = _prepared_legacy_display(
        root, "legacy-missing-worktree"
    )
    spec = _legacy_reconciliation_spec(launch, receipt, worktree, runner)
    stored_receipt = store.assignment_launch_context(str(launch["assignment_id"]))[
        "acceptance_receipt"
    ]
    stored_receipt["worktree"] = ""
    store.connection.execute(
        "UPDATE agent_instances SET worktree=? WHERE agent_id=?",
        (str(Path.cwd()), LUX_ID),
    )
    store.connection.execute(
        "UPDATE task_assignments SET acceptance_receipt_json=? WHERE task_assignment_id=?",
        (
            json.dumps(stored_receipt, sort_keys=True, separators=(",", ":")),
            launch["assignment_id"],
        ),
    )
    command = LegacyDisplayReconciliationCommand(
        **{**vars(spec), "worktree": str(Path.cwd()), "at": clock.now()}
    )
    try:
        store.begin_legacy_display_reconciliation(command)
    except StorageRefusal as exc:
        assert exc.code == "legacy_display_conflict"
    else:
        raise AssertionError("missing acceptance worktree matched the process directory")
    assert store.assignment_launch_context(str(launch["assignment_id"]))[
        "legacy_display_reconciliation"
    ] is None
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
        store, _adapter(options, runner, store), options, clock
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
        store, _adapter(options, runner, store), options, clock
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


def test_issue_first_refuses_missing_and_mismatched_scope_before_launch(root: Path) -> None:
    store, clock, worktree = _context(root, "issue-first-refusal")
    options = _options(root)
    runner = FakeHerdrRunner(worktree)
    adapter = _adapter(options, runner, store)
    del adapter.issue_verifier
    try:
        VisibleChampionLaunchService(store, adapter, options, clock).launch(
            _spec(worktree, "issue-first-refusal")
        )
    except StorageRefusal as exc:
        assert exc.code == "issue_verification_required"
    else:
        raise AssertionError("visible launch accepted missing issue verification")
    assert runner.calls == []
    assert store.connection.execute(
        "SELECT COUNT(*) FROM task_assignments WHERE task_assignment_id='assignment:issue-first-refusal'"
    ).fetchone()[0] == 0

    unproven_reopen = FakeIssueVerifier(store=store, persist_selection=False)
    try:
        VisibleChampionLaunchService(
            store, adapter, options, clock, issue_verifier=unproven_reopen
        ).launch(_spec(worktree, "issue-first-refusal"))
    except StorageRefusal as exc:
        assert exc.code == "issue_selection_unproven"
    else:
        raise AssertionError("visible launch accepted an unproven issue-selection receipt")
    assert runner.calls == []

    changed_scope = FakeIssueVerifier(store=store, receipt_scope_digest="a" * 64)
    try:
        VisibleChampionLaunchService(
            store, adapter, options, clock, issue_verifier=changed_scope
        ).launch(_spec(worktree, "issue-first-changed"))
    except StorageRefusal as exc:
        assert exc.code == "issue_selection_unproven"
    else:
        raise AssertionError("visible launch accepted a changed semantic issue scope")
    assert runner.calls == []

    mismatch = FakeIssueVerifier(
        store=store, repository="https://example.invalid/different.git"
    )
    try:
        VisibleChampionLaunchService(
            store, adapter, options, clock, issue_verifier=mismatch
        ).launch(_spec(worktree, "issue-first-mismatch"))
    except StorageRefusal as exc:
        assert exc.code == "issue_scope_mismatch"
    else:
        raise AssertionError("visible launch accepted a mismatched issue scope")
    assert runner.calls == []
    assert store.connection.execute(
        "SELECT COUNT(*) FROM task_assignments WHERE task_assignment_id LIKE 'assignment:issue-first-%'"
    ).fetchone()[0] == 0
    store.close()


class FakeGitHubRunner:
    def __init__(self, payload: dict[str, object] | None, *, returncode: int = 0) -> None:
        self.payload = payload
        self.returncode = returncode

    def run(self, arguments, *, timeout_seconds: int = 30):
        del timeout_seconds
        return subprocess.CompletedProcess(
            tuple(arguments),
            self.returncode,
            "" if self.payload is None else json.dumps(self.payload),
            "synthetic failure" if self.returncode else "",
        )


def test_github_issue_verifier_refuses_missing_wrong_repository_and_closed_issue(root: Path) -> None:
    del root
    spec = AssignmentSpec(
        assignment_id="assignment:github",
        request_id="request:github",
        claim_token="claim:github",
        task_id="task:github",
        task_summary="Implement the exact GitHub issue",
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID,
        repository="https://github.com/example/league.git",
        issue=81,
        branch="agent/example/81",
        worktree="/synthetic/worktree",
        issue_receipt=None,
    )
    try:
        GitHubIssueVerifier(
            FakeGitHubRunner(None, returncode=1), selection_receipt_digest="f" * 64
        ).verify(spec, "2026-01-01T00:00:00Z")
    except StorageRefusal as exc:
        assert exc.code == "issue_verification_failed"
    else:
        raise AssertionError("missing repository issue was accepted")
    payload: dict[str, object] = {
        "number": 81,
        "state": "open",
        "title": "Implement the exact GitHub issue",
        "body": "## Objective\nImplement.\n## Verification\nTest.\n## Hard boundaries\nStay scoped.",
        "html_url": "https://github.com/example/league/issues/81",
        "repository_url": "https://api.github.com/repos/example/wrong",
    }
    try:
        GitHubIssueVerifier(
            FakeGitHubRunner(payload), selection_receipt_digest="f" * 64
        ).verify(spec, "2026-01-01T00:00:00Z")
    except StorageRefusal as exc:
        assert exc.code == "issue_identity_refused"
    else:
        raise AssertionError("wrong-repository issue was accepted")
    payload["repository_url"] = "https://api.github.com/repos/example/league"
    payload["state"] = "closed"
    try:
        GitHubIssueVerifier(
            FakeGitHubRunner(payload), selection_receipt_digest="f" * 64
        ).verify(spec, "2026-01-01T00:00:00Z")
    except StorageRefusal as exc:
        assert exc.code == "issue_closed"
    else:
        raise AssertionError("closed issue without reopen authority was accepted")


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
        store, _adapter(options, runner, store), options, clock
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
    service = VisibleChampionLaunchService(store, _adapter(options, runner, store), options, clock)
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
        store, _adapter(options, runner, store), options, clock
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
    def __init__(self, store, base: FakeLaunchAdapter, *, cleanup: bool) -> None:
        self.base = base
        self._created = {"pane_id": "synthetic:pane"}
        self.cleanup_result = cleanup
        self.issue_verifier = FakeIssueVerifier(store=store)

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
        store, FailingAdapter(store, FakeLaunchAdapter(), cleanup=False), options, clock  # type: ignore[arg-type]
    ).launch(_spec(worktree, "partial"))
    assert result["state"] == "cleanup_pending"
    assert store.connection.execute(
        "SELECT required_policy FROM cleanup_obligations WHERE task_id='task:partial'"
    ).fetchone()[0] == "failed_launch"
    store.close()


class ContextFailureAdapter:
    def __init__(self, store, *, cleanup: bool) -> None:
        self.base = FakeLaunchAdapter()
        self._created = {"pane_id": "herdr:lux"}
        self.cleanup_result = cleanup
        self.issue_verifier = FakeIssueVerifier(store=store)

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
        store, ContextFailureAdapter(store, cleanup=False), options, clock  # type: ignore[arg-type]
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
        store, ContextFailureAdapter(store, cleanup=True), options, clock  # type: ignore[arg-type]
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
        store, _adapter(options, runner, store), options, clock
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
        store, _adapter(options, runner, store), options, clock
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
        test_legacy_display_command_exposes_exact_owner_cas_inputs()
        test_task_label_defaults_and_explicit_labels_stay_two_words(root)
        test_real_adapter_one_command_success_and_retry(root)
        test_legacy_active_champion_display_is_reconciled_once_with_exact_receipt(root)
        test_legacy_display_reconciliation_refuses_user_title_race_before_write(root)
        test_legacy_display_compare_and_set_does_not_overwrite_last_window_user_title(root)
        test_legacy_display_reconciliation_refuses_modern_receipt_before_live_write(root)
        test_legacy_display_reconciliation_refuses_ambiguous_runtime_and_wrong_route(root)
        test_legacy_display_reconciliation_requires_live_owned_presentation_source(root)
        test_legacy_display_reconciliation_refuses_boolean_sequence(root)
        test_legacy_display_reconciliation_refuses_orphaned_final_receipt(root)
        test_legacy_display_readback_refuses_expected_plus_two_receipt(root)
        test_legacy_display_interrupted_effect_refuses_newer_sequence(root)
        test_legacy_display_refuses_malformed_modern_receipt(root)
        test_legacy_display_refuses_missing_acceptance_worktree(root)
        test_post_context_title_restoration_refuses_unowned_metadata(root)
        test_active_retry_refuses_newer_user_metadata(root)
        test_issue_first_refuses_missing_and_mismatched_scope_before_launch(root)
        test_github_issue_verifier_refuses_missing_wrong_repository_and_closed_issue(root)
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
