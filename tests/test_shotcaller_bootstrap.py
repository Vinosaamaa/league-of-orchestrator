#!/usr/bin/env python3
"""In-place Shotcaller bootstrap and three-way placement-policy coverage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.shotcaller_bootstrap import (  # noqa: E402
    HerdrShotcallerBootstrapAdapter,
    ShotcallerBootstrapOptions,
    ShotcallerBootstrapService,
    ShotcallerBootstrapSpec,
)
from league.sqlite_handoff_schema import SHOTCALLER_SEED, SHUFFLE_VERSION  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from lifecycle_fakes import FakeClock  # noqa: E402
from storage_test_support import migrated_state  # noqa: E402


THREAD_ID = "88888888-8888-4888-8888-888888888888"
AGENT_ID = "agent:shotcaller:ashe"
WATCHER = ROOT / "bin/agent-watcher"
LEAGUE = ROOT / "bin/league"


class RecordingHerdr:
    def __init__(
        self,
        worktree: Path,
        *,
        thread_id: str = THREAD_ID,
        publish_mismatch: bool = False,
        delayed_auto_title_reads: int | None = None,
    ) -> None:
        self.worktree = str(worktree.resolve())
        self.thread_id = thread_id
        self.publish_mismatch = publish_mismatch
        self.delayed_auto_title_reads = delayed_auto_title_reads
        self.name: str | None = None
        self.tokens: dict[str, str] = {}
        self.title = ""
        self.metadata_source = "herdr:codex"
        self.source_sequences: dict[str, int] = {}
        self.pending_auto_title_reads: int | None = None
        self.auto_title_scheduled = False
        self.terminal_title_override: str | None = None
        self.terminal_title_stripped_override: str | None = None
        self.state_change_seq = 7
        self.calls: list[tuple[str, ...]] = []

    def _agent(self) -> dict[str, object]:
        value: dict[str, object] = {
            "agent": "codex",
            "agent_status": "working",
            "agent_session": {"source": "herdr:codex", "value": self.thread_id},
            "cwd": self.worktree,
            "foreground_cwd": self.worktree,
            "metadata_source": self.metadata_source,
            "pane_id": "w1:p1",
            "state_change_seq": self.state_change_seq,
            "tab_id": "w1:t1",
            "terminal_id": "terminal:1",
            "workspace_id": "w1",
            "tokens": dict(self.tokens),
            "terminal_title": (
                self.title
                if self.terminal_title_override is None
                else self.terminal_title_override
            ),
            "terminal_title_stripped": (
                self.title
                if self.terminal_title_stripped_override is None
                else self.terminal_title_stripped_override
            ),
        }
        if self.name is not None:
            value["name"] = self.name
        return value

    def _advance_auto_title(self) -> None:
        if self.pending_auto_title_reads is None:
            return
        self.pending_auto_title_reads -= 1
        if self.pending_auto_title_reads > 0:
            return
        self.pending_auto_title_reads = None
        self.metadata_source = "herdr:codex"
        self.state_change_seq += 1
        self.title = "Create the Shotcaller for this pane | codex"

    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        command = tuple(arguments)
        self.calls.append(command)
        if command == ("herdr", "pane", "current", "--current"):
            result = {"pane": self._agent()}
            self._advance_auto_title()
        elif command == ("herdr", "agent", "list"):
            result = {"agents": [self._agent()]}
            self._advance_auto_title()
        elif command[:3] == ("herdr", "agent", "rename"):
            self.name = None if command[-1] == "--clear" else command[-1]
            self.state_change_seq += 1
            result = {"agent": self._agent()}
        elif command[:3] == ("herdr", "pane", "report-metadata"):
            source = command[command.index("--source") + 1]
            sequence = int(command[command.index("--seq") + 1])
            if sequence <= self.source_sequences.get(source, 0):
                return subprocess.CompletedProcess(
                    command, 1, "", "metadata sequence conflict"
                )
            if "--applies-to-source" in command:
                applies_to = command[command.index("--applies-to-source") + 1]
                if applies_to != "herdr:codex":
                    return subprocess.CompletedProcess(
                        command, 1, "", "metadata source mismatch"
                    )
            self.source_sequences[source] = sequence
            self.state_change_seq += 1
            self.metadata_source = source
            if "--title" in command:
                title = command[command.index("--title") + 1]
                if not self.publish_mismatch or not title:
                    self.title = title
            positions = [index for index, value in enumerate(command) if value == "--token"]
            for position in positions:
                key, value = command[position + 1].split("=", 1)
                if self.publish_mismatch and value:
                    continue
                if value:
                    self.tokens[key] = value
                else:
                    self.tokens.pop(key, None)
            if (
                self.delayed_auto_title_reads is not None
                and not self.auto_title_scheduled
                and source.startswith("league-shotcaller-")
                and source != "league-shotcaller-rollback"
            ):
                self.auto_title_scheduled = True
                self.pending_auto_title_reads = self.delayed_auto_title_reads
            return subprocess.CompletedProcess(command, 0, "", "")
        else:
            raise AssertionError(f"unexpected Herdr command: {command}")
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"id": "test", "result": result}) + "\n", ""
        )


class PersistentRecordingHerdr(RecordingHerdr):
    """A fake Herdr endpoint whose metadata survives test process boundaries."""

    def __init__(self, worktree: Path, state_path: Path) -> None:
        self.state_path = state_path
        super().__init__(worktree)
        if state_path.exists():
            self._load()
        else:
            self._save()


    def _load(self) -> None:
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.thread_id = value["thread_id"]
        self.name = value["name"]
        self.tokens = dict(value["tokens"])
        self.title = value["title"]
        self.metadata_source = value["metadata_source"]
        self.source_sequences = {
            str(key): int(sequence)
            for key, sequence in value["source_sequences"].items()
        }
        self.state_change_seq = int(value["state_change_seq"])

    def _save(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "thread_id": self.thread_id,
                    "name": self.name,
                    "tokens": self.tokens,
                    "title": self.title,
                    "metadata_source": self.metadata_source,
                    "source_sequences": self.source_sequences,
                    "state_change_seq": self.state_change_seq,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        self._load()
        try:
            return super().run(arguments, timeout_seconds=timeout_seconds)
        finally:
            self._save()


class RepeatingAutoTitleHerdr(RecordingHerdr):
    """Codex rewrites the owner-prompt title after both bootstrap reports."""

    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        if (
            command[:3] == ("herdr", "pane", "report-metadata")
            and "--source" in command
            and command[command.index("--source") + 1]
            != "league-shotcaller-rollback"
        ):
            self.auto_title_scheduled = False
        return super().run(arguments, timeout_seconds=timeout_seconds)


class UserTitleAfterPublishHerdr(RecordingHerdr):
    """A newer user presentation write lands after bootstrap publication."""

    def _advance_auto_title(self) -> None:
        if self.pending_auto_title_reads is None:
            return
        self.pending_auto_title_reads -= 1
        if self.pending_auto_title_reads > 0:
            return
        self.pending_auto_title_reads = None
        self.metadata_source = "user-selected"
        self.state_change_seq += 1
        self.title = "User selected title"


class InjectedBootstrapFault(RuntimeError):
    pass


def _seed_available_ashe(store: SQLiteStorage, clock: FakeClock) -> None:
    status = store.callsign_status("shotcaller")
    catalog = [
        {
            "callsign": entry["callsign"],
            "enabled": entry["enabled"],
            "capabilities": [],
        }
        for entry in status["entries"]
    ]
    catalog.append(
        {
            "callsign": "Ashe",
            "enabled": True,
            "capabilities": ["request.triage", "rollover.accept"],
        }
    )
    store.reconcile_callsign_pool(
        "shotcaller",
        status["queue_version"],
        SHOTCALLER_SEED,
        SHUFFLE_VERSION,
        catalog,
        clock.now(),
    )


def _spec() -> ShotcallerBootstrapSpec:
    return ShotcallerBootstrapSpec(
        assignment_id="callsign-assignment:bootstrap:ashe",
        agent_id=AGENT_ID,
        runtime_instance_id="runtime:shotcaller:ashe",
        thread_id=THREAD_ID,
        capabilities=("request.triage", "rollover.accept"),
    )


def _options(worktree: Path) -> ShotcallerBootstrapOptions:
    return ShotcallerBootstrapOptions(
        workspace_id="w1",
        tab_id="w1:t1",
        pane_id="w1:p1",
        worktree=str(worktree.resolve()),
    )


def test_in_place_bootstrap_creates_shotcaller_without_layout_or_squad_registration(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "in-place-shotcaller")
    worktree = root / "in-place-shotcaller" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = ShotcallerBootstrapService(
            store,
            HerdrShotcallerBootstrapAdapter(
                _options(worktree),
                runner,
                environment={
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            ),
            clock,
        )
        created = service.bootstrap(_spec())
        assert created["state"] == "active"
        assert created["idempotent"] is False
        assert created["callsign"] == "Ashe"
        assert created["runtime_instance_id"] == "runtime:shotcaller:ashe"
        assert [call[:3] for call in runner.calls[:2]] == [
            ("herdr", "pane", "current"),
            ("herdr", "agent", "list"),
        ]
        assert any(
            call[:3] == ("herdr", "agent", "rename") for call in runner.calls
        )
        assert any(
            call[:3] == ("herdr", "pane", "report-metadata")
            for call in runner.calls
        )
        forbidden = {
            ("herdr", "tab", "create"),
            ("herdr", "pane", "split"),
            ("herdr", "workspace", "create"),
            ("herdr", "agent", "start"),
        }
        assert not any(call[:3] in forbidden for call in runner.calls)
        agent = store.connection.execute(
            "SELECT role,callsign,kind,address,thread_id,backend,routing_name,status "
            "FROM agent_instances WHERE agent_id=?",
            (AGENT_ID,),
        ).fetchone()
        assert tuple(agent) == (
            "shotcaller",
            "Ashe",
            "codex-thread",
            "w1:p1",
            THREAD_ID,
            "herdr",
            "ashe",
            "working",
        )
        runtime = store.connection.execute(
            "SELECT actor_agent_id,session_ref,endpoint,status,verified "
            "FROM runtime_instances WHERE runtime_instance_id='runtime:shotcaller:ashe'"
        ).fetchone()
        assert tuple(runtime) == (AGENT_ID, THREAD_ID, "w1:p1", "active", 1)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squad_registration_offers WHERE shotcaller_agent_id=?",
            (AGENT_ID,),
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
        events = [
            row[0]
            for row in store.connection.execute(
                "SELECT event_type FROM events WHERE agent_id=? ORDER BY event_seq", (AGENT_ID,)
            )
        ]
        assert events == [
            "callsign_reserved",
            "callsign_activated",
            "shotcaller_created",
        ]
        calls_before_retry = len(runner.calls)
        retry = service.bootstrap(_spec())
        assert retry == {**created, "idempotent": True}
        assert not any(
            call[:3] in {("herdr", "agent", "rename"), ("herdr", "pane", "report-metadata")}
            for call in runner.calls[calls_before_retry:]
        )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 3
        calls_before_mismatch = len(runner.calls)
        runner.thread_id = "77777777-7777-4777-8777-777777777777"
        try:
            service.bootstrap(_spec())
        except StorageRefusal as exc:
            assert exc.code == "shotcaller_identity_unverified"
        else:
            raise AssertionError("durable bootstrap retry accepted a different live thread")
        assert runner.name == "ashe"
        assert not any(
            call[:3] == ("herdr", "agent", "rename")
            for call in runner.calls[calls_before_mismatch:]
        )
        runner.thread_id = THREAD_ID

    pointer = root / "in-place-shotcaller" / "writer-pointer.json"
    pointer.write_text(
        '{"writer":"sqlite","generation":"synthetic-bootstrap"}\n', encoding="utf-8"
    )
    hook_environment = {
        **os.environ,
        "LEAGUE_STATE_ROOT": str(state),
        "LEAGUE_WRITER_POINTER": str(pointer),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    prompt_body = "Synthetic prompt submitted immediately after in-place bootstrap."
    hook = subprocess.run(
        [str(WATCHER), "codex-user-prompt-hook"],
        input=json.dumps(
            {
                "session_id": THREAD_ID,
                "turn_id": "turn:bootstrap:first-prompt",
                "hook_event_name": "UserPromptSubmit",
                "prompt": prompt_body,
            }
        ),
        text=True,
        capture_output=True,
        env=hook_environment,
        check=False,
    )
    assert hook.returncode == 0, hook.stdout + hook.stderr
    assert json.loads(hook.stdout) == {}

    turn = subprocess.Popen(
        [
            str(LEAGUE),
            "--state-root",
            str(state),
            "request",
            "turn",
            "--owner-agent-id",
            AGENT_ID,
            "--at",
            clock.now(),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=hook_environment,
    )
    turn_pid = turn.pid
    assert turn.stdin is not None and turn.stdout is not None
    intake = json.loads(turn.stdout.readline())
    assert intake["result"]["phase"] == "intake"
    assert [row["body"] for row in intake["result"]["prompts"]] == [prompt_body]
    turn.stdin.write(
        json.dumps(
            {
                "decisions": [
                    {
                        "items": [
                            {
                                "summary": "Answer the synthetic bootstrap prompt",
                                "disposition": "new_request",
                            }
                        ]
                    }
                ],
                "plans": [
                    {
                        "work_kind": "short-check",
                        "requested_mode": "direct",
                        "signals": {
                            "pre_bounded": True,
                            "read_only": True,
                            "answer_or_routing_only": True,
                            "expected_minutes": 2,
                            "expected_task_action_calls": 1,
                        },
                    }
                ],
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    turn.stdin.flush()
    begun = json.loads(turn.stdout.readline())
    assert begun["result"]["phase"] == "begun"
    assert turn.poll() is None and turn.pid == turn_pid

    # The open request-turn process has yielded to the model here. A genuine
    # native steer is accepted by Codex independently; League's hook only makes
    # the exact message durable and must not wait for this process to exit.
    steered_body = "Synthetic native steer while the request-turn tool is yielded."
    started = time.monotonic()
    steered = subprocess.run(
        [str(WATCHER), "codex-user-prompt-hook"],
        input=json.dumps(
            {
                "session_id": THREAD_ID,
                "turn_id": "turn:bootstrap:first-prompt",
                "hook_event_name": "UserPromptSubmit",
                "prompt": steered_body,
            }
        ),
        text=True,
        capture_output=True,
        env=hook_environment,
        check=False,
    )
    capture_elapsed = time.monotonic() - started
    assert steered.returncode == 0, steered.stdout + steered.stderr
    assert json.loads(steered.stdout) == {} and capture_elapsed < 1.5
    assert turn.poll() is None and turn.pid == turn_pid
    turn.stdin.write(
        json.dumps(
            {
                "actions": [
                    {
                        "kind": "answer",
                        "request_index": 1,
                        "content": "Synthetic bootstrap answer.",
                        "resolution_summary": "Answered the bootstrap prompt.",
                    }
                ]
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    turn.stdin.flush()
    committed = json.loads(turn.stdout.readline())
    assert committed["result"]["phase"] == "committed"
    assert turn.wait(timeout=10) == 0 and turn.pid == turn_pid
    with SQLiteStorage(state) as store:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM prompts WHERE intake_actor_id=?", (AGENT_ID,)
        ).fetchone()[0] == 2
        assert store.connection.execute(
            "SELECT COUNT(*) FROM prompts WHERE current_owner_agent_id=? AND triage_state='untriaged'",
            (AGENT_ID,),
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM prompt_quarantine"
        ).fetchone()[0] == 0
        scope = store.connection.execute(
            "SELECT actor_agent_id,user_message_generation,wait_generation "
            "FROM watcher_scopes WHERE scope_id='watcher:Ashe'"
        ).fetchone()
        assert tuple(scope) == (AGENT_ID, 2, 3)


def test_bootstrap_reasserts_owned_title_after_auto_title_settles(root: Path) -> None:
    state, _ = migrated_state(root, "shotcaller-auto-title-settling")
    worktree = root / "shotcaller-auto-title-settling" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree, delayed_auto_title_reads=2)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = ShotcallerBootstrapService(
            store,
            HerdrShotcallerBootstrapAdapter(
                _options(worktree),
                runner,
                environment={
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            ),
            clock,
        )

        created = service.bootstrap(_spec())

        assert created["state"] == "active"
        assert runner.name == "ashe"
        assert runner.metadata_source.startswith("league-shotcaller-")
        assert runner.title == "Ashe"
        assert runner.tokens["sidebar_name"] == "Ashe"
        assert runner.tokens["thread_title"] == "Ashe"
        metadata_calls = [
            call
            for call in runner.calls
            if call[:3] == ("herdr", "pane", "report-metadata")
        ]
        assert len(metadata_calls) == 2
        assert not any(
            call[:3]
            in {
                ("herdr", "tab", "create"),
                ("herdr", "pane", "split"),
                ("herdr", "workspace", "create"),
                ("herdr", "agent", "start"),
                ("herdr", "agent", "prompt"),
            }
            for call in runner.calls
        )


def test_bootstrap_identity_mismatch_makes_no_canonical_mutation(root: Path) -> None:
    state, _ = migrated_state(root, "shotcaller-identity-mismatch")
    worktree = root / "shotcaller-identity-mismatch" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(
        worktree, thread_id="99999999-9999-4999-8999-999999999999"
    )
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        before = store.callsign_status("shotcaller")
        service = ShotcallerBootstrapService(
            store,
            HerdrShotcallerBootstrapAdapter(
                _options(worktree),
                runner,
                environment={
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            ),
            clock,
        )
        try:
            service.bootstrap(_spec())
        except StorageRefusal as exc:
            assert exc.code == "shotcaller_identity_unverified"
        else:
            raise AssertionError("mismatched calling Codex thread was bootstrapped")
        assert store.callsign_status("shotcaller") == before
        assert store.connection.execute(
            "SELECT COUNT(*) FROM agent_instances WHERE agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0


def _assert_preexisting_reserved_bootstrap_requires_baseline(
    root: Path, label: str, *, tokens: dict[str, str], title: str
) -> None:
    state, _ = migrated_state(root, label)
    worktree = root / label / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree)
    runner.tokens = dict(tokens)
    runner.title = title
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        reserved = store.allocate_callsign(
            _spec().assignment_id,
            _spec().agent_id,
            "shotcaller",
            "shotcaller",
            _spec().agent_id,
            _spec().capabilities,
            clock.now(),
        )
        assert reserved["state"] == "reserved"
        service = ShotcallerBootstrapService(
            store,
            HerdrShotcallerBootstrapAdapter(
                _options(worktree),
                runner,
                environment={
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            ),
            clock,
        )
        try:
            service.bootstrap(_spec())
        except StorageRefusal as exc:
            assert exc.code == "shotcaller_creation_cleanup_unproven"
        else:
            raise AssertionError(
                "pre-existing reservation manufactured a missing restoration baseline"
            )
        assert store.callsign_assignment_status(_spec().assignment_id)["state"] == "reserved"
        assert store.shotcaller_bootstrap_baseline(_spec().assignment_id) is None
        assert runner.name is None
        assert runner.tokens == tokens
        assert runner.title == title
        assert not any(
            call[:3] in {
                ("herdr", "agent", "rename"),
                ("herdr", "pane", "report-metadata"),
            }
            for call in runner.calls
        )


def test_preexisting_reserved_bootstrap_requires_durable_baseline_when_alias_empty(
    root: Path,
) -> None:
    _assert_preexisting_reserved_bootstrap_requires_baseline(
        root,
        "shotcaller-reserved-missing-baseline-clean",
        tokens={},
        title="",
    )


def test_preexisting_reserved_bootstrap_rejects_residual_metadata_without_baseline(
    root: Path,
) -> None:
    _assert_preexisting_reserved_bootstrap_requires_baseline(
        root,
        "shotcaller-reserved-missing-baseline-residual",
        tokens={"sidebar_name": "Partial", "thread_title": "Partial"},
        title="Partial",
    )


def test_completed_bootstrap_retry_restores_owned_title_without_prompt(root: Path) -> None:
    state, _ = migrated_state(root, "shotcaller-completed-retry")
    worktree = root / "shotcaller-completed-retry" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = ShotcallerBootstrapService(
            store,
            HerdrShotcallerBootstrapAdapter(
                _options(worktree),
                runner,
                environment={
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            ),
            clock,
        )
        created = service.bootstrap(_spec())
        changed_specs = (
            ShotcallerBootstrapSpec(
                assignment_id=_spec().assignment_id,
                agent_id="agent:shotcaller:changed",
                runtime_instance_id=_spec().runtime_instance_id,
                thread_id=_spec().thread_id,
                capabilities=_spec().capabilities,
            ),
            ShotcallerBootstrapSpec(
                assignment_id=_spec().assignment_id,
                agent_id=_spec().agent_id,
                runtime_instance_id="runtime:shotcaller:changed",
                thread_id=_spec().thread_id,
                capabilities=_spec().capabilities,
            ),
            ShotcallerBootstrapSpec(
                assignment_id=_spec().assignment_id,
                agent_id=_spec().agent_id,
                runtime_instance_id=_spec().runtime_instance_id,
                thread_id=_spec().thread_id,
                capabilities=("request.triage", "rollover.accept", "artifact.publish"),
            ),
        )
        for changed in changed_specs:
            try:
                service.bootstrap(changed)
            except StorageRefusal as exc:
                assert exc.code in {"receipt_conflict", "receipt_mismatch"}
            else:
                raise AssertionError("completed bootstrap retry accepted changed identity")
        runner.metadata_source = "herdr:codex"
        runner.state_change_seq += 1
        runner.title = "Create the Shotcaller for this pane | codex"
        calls_before_retry = len(runner.calls)

        retry = service.bootstrap(_spec())

        assert retry == {**created, "idempotent": True}
        retry_calls = runner.calls[calls_before_retry:]
        assert len(
            [
                call
                for call in retry_calls
                if call[:3] == ("herdr", "pane", "report-metadata")
            ]
        ) == 1
        assert not any(
            call[:3]
            in {
                ("herdr", "agent", "rename"),
                ("herdr", "agent", "prompt"),
                ("herdr", "tab", "create"),
                ("herdr", "pane", "split"),
                ("herdr", "agent", "start"),
            }
            for call in retry_calls
        )
        assert runner.metadata_source.startswith("league-shotcaller-")
        assert runner.title == "Ashe"
        assert store.shotcaller_bootstrap_status(_spec().assignment_id) == {
            **created,
            "idempotent": True,
        }


def test_completed_bootstrap_retry_refuses_newer_user_title_with_stale_tokens(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "shotcaller-completed-user-title")
    worktree = root / "shotcaller-completed-user-title" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = ShotcallerBootstrapService(
            store,
            HerdrShotcallerBootstrapAdapter(
                _options(worktree),
                runner,
                environment={
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            ),
            clock,
        )
        created = service.bootstrap(_spec())
        stale_tokens = dict(runner.tokens)
        runner.metadata_source = "user-selected"
        runner.state_change_seq += 1
        runner.title = "User selected title"
        calls_before_retry = len(runner.calls)

        try:
            service.bootstrap(_spec())
        except StorageRefusal as exc:
            assert exc.code == "shotcaller_metadata_unverified"
        else:
            raise AssertionError("completed bootstrap retry overwrote a newer user title")

        retry_calls = runner.calls[calls_before_retry:]
        assert not any(
            call[:3]
            in {
                ("herdr", "pane", "report-metadata"),
                ("herdr", "agent", "rename"),
                ("herdr", "agent", "prompt"),
            }
            for call in retry_calls
        )
        assert runner.metadata_source == "user-selected"
        assert runner.title == "User selected title"
        assert runner.tokens == stale_tokens
        assert store.shotcaller_bootstrap_status(_spec().assignment_id) == {
            **created,
            "idempotent": True,
        }


def test_bootstrap_metadata_and_atomic_finalization_failures_restore_exact_state(
    root: Path,
) -> None:
    cases = ("metadata", "after_shotcaller_activation", "after_shotcaller_created_event")
    for case in cases:
        state, _ = migrated_state(root, f"shotcaller-rollback-{case}")
        worktree = root / f"shotcaller-rollback-{case}" / "worktree"
        worktree.mkdir()
        clock = FakeClock()
        runner = RecordingHerdr(worktree, publish_mismatch=case == "metadata")
        with SQLiteStorage(state) as store:
            _seed_available_ashe(store, clock)
            service = ShotcallerBootstrapService(
                store,
                HerdrShotcallerBootstrapAdapter(
                    _options(worktree),
                    runner,
                    environment={
                        "HERDR_ENV": "1",
                        "HERDR_WORKSPACE_ID": "w1",
                        "HERDR_TAB_ID": "w1:t1",
                        "HERDR_PANE_ID": "w1:p1",
                    },
                ),
                clock,
            )

            def fault(point: str) -> None:
                if point == case:
                    raise InjectedBootstrapFault(point)

            try:
                service.bootstrap(_spec(), fault=fault)
            except (InjectedBootstrapFault, StorageRefusal) as exc:
                if case == "metadata":
                    assert getattr(exc, "code", None) == "shotcaller_metadata_unverified"
            else:
                raise AssertionError(f"bootstrap failure {case} unexpectedly committed")
            assignment = store.callsign_assignment_status(_spec().assignment_id)
            assert assignment is not None and assignment["state"] == "rolled_back"
            assert store.connection.execute(
                "SELECT COUNT(*) FROM runtime_instances WHERE actor_agent_id=?", (AGENT_ID,)
            ).fetchone()[0] == 0
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE agent_id=? AND event_type='shotcaller_created'",
                (AGENT_ID,),
            ).fetchone()[0] == 0
            assert store.connection.execute(
                "SELECT retired_at FROM agent_instances WHERE agent_id=?", (AGENT_ID,)
            ).fetchone()[0] is not None
            assert store.connection.execute(
                "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
            ).fetchone()[0] == 0
        assert runner.name is None and runner.tokens == {} and runner.title == ""
        assert any(call[:3] == ("herdr", "agent", "rename") and call[-1] == "--clear" for call in runner.calls)
        assert not any(
            call[:3]
            in {
                ("herdr", "tab", "create"),
                ("herdr", "pane", "split"),
                ("herdr", "workspace", "create"),
                ("herdr", "agent", "start"),
            }
            for call in runner.calls
        )


def test_bootstrap_rolls_back_when_owned_auto_title_never_settles(root: Path) -> None:
    state, _ = migrated_state(root, "shotcaller-auto-title-rollback")
    worktree = root / "shotcaller-auto-title-rollback" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RepeatingAutoTitleHerdr(worktree, delayed_auto_title_reads=2)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = ShotcallerBootstrapService(
            store,
            HerdrShotcallerBootstrapAdapter(
                _options(worktree),
                runner,
                environment={
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            ),
            clock,
        )

        try:
            service.bootstrap(_spec())
        except StorageRefusal as exc:
            assert exc.code == "shotcaller_metadata_unverified"
        else:
            raise AssertionError("bootstrap accepted an auto-title that never settled")

        assignment = store.callsign_assignment_status(_spec().assignment_id)
        assert assignment is not None and assignment["state"] == "rolled_back"
        assert store.connection.execute(
            "SELECT state FROM callsign_queue WHERE callsign='Ashe'"
        ).fetchone()[0] == "available"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM runtime_instances WHERE actor_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
    assert runner.name is None and runner.tokens == {} and runner.title == ""
    assert not any(
        call[:3]
        in {
            ("herdr", "tab", "create"),
            ("herdr", "pane", "split"),
            ("herdr", "workspace", "create"),
            ("herdr", "agent", "start"),
            ("herdr", "agent", "prompt"),
        }
        for call in runner.calls
    )


def test_bootstrap_rolls_back_without_overwriting_newer_user_title(root: Path) -> None:
    state, _ = migrated_state(root, "shotcaller-user-title-rollback")
    worktree = root / "shotcaller-user-title-rollback" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = UserTitleAfterPublishHerdr(worktree, delayed_auto_title_reads=2)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = ShotcallerBootstrapService(
            store,
            HerdrShotcallerBootstrapAdapter(
                _options(worktree),
                runner,
                environment={
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            ),
            clock,
        )

        try:
            service.bootstrap(_spec())
        except StorageRefusal as exc:
            assert exc.code == "shotcaller_metadata_unverified"
        else:
            raise AssertionError("bootstrap accepted a newer user-owned title")

        assignment = store.callsign_assignment_status(_spec().assignment_id)
        assert assignment is not None and assignment["state"] == "rolled_back"
        assert store.connection.execute(
            "SELECT state FROM callsign_queue WHERE callsign='Ashe'"
        ).fetchone()[0] == "available"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM runtime_instances WHERE actor_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
    assert runner.name is None
    assert runner.metadata_source == "user-selected"
    assert runner.title == "User selected title"
    assert len(
        [
            call
            for call in runner.calls
            if call[:3] == ("herdr", "pane", "report-metadata")
        ]
    ) == 1
    assert not any(
        call[:3]
        in {
            ("herdr", "tab", "create"),
            ("herdr", "pane", "split"),
            ("herdr", "workspace", "create"),
            ("herdr", "agent", "start"),
            ("herdr", "agent", "prompt"),
        }
        for call in runner.calls
    )


def _bootstrap_process_child(
    state: Path, worktree: Path, herdr_state: Path, phase: str
) -> int:
    clock = FakeClock()
    runner = PersistentRecordingHerdr(worktree, herdr_state)
    with SQLiteStorage(state) as store:
        service = ShotcallerBootstrapService(
            store,
            HerdrShotcallerBootstrapAdapter(
                _options(worktree),
                runner,
                environment={
                    "HERDR_ENV": "1",
                    "HERDR_WORKSPACE_ID": "w1",
                    "HERDR_TAB_ID": "w1:t1",
                    "HERDR_PANE_ID": "w1:p1",
                },
            ),
            clock,
        )
        if phase == "publish-crash":
            def crash(point: str) -> None:
                if point == "after_shotcaller_publish":
                    os._exit(86)

            service.bootstrap(_spec(), fault=crash)
            return 90
        if phase == "retry-fault":
            def fail_finalization(point: str) -> None:
                if point == "after_shotcaller_activation":
                    raise InjectedBootstrapFault(point)

            try:
                service.bootstrap(_spec(), fault=fail_finalization)
            except InjectedBootstrapFault:
                return 0
            return 91
    return 92


def test_publish_crash_then_retry_fault_restores_original_unbound_metadata(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "shotcaller-publish-crash")
    worktree = root / "shotcaller-publish-crash" / "worktree"
    worktree.mkdir()
    herdr_state = root / "shotcaller-publish-crash" / "herdr-state.json"
    runner = PersistentRecordingHerdr(worktree, herdr_state)
    runner.tokens = {
        "sidebar_name": "Original sidebar",
        "thread_title": "Original thread",
    }
    runner.title = "Original title"
    runner._save()
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, FakeClock())

    child = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--bootstrap-process-child",
        str(state),
        str(worktree),
        str(herdr_state),
    ]
    crashed = subprocess.run([*child, "publish-crash"], check=False, timeout=10)
    assert crashed.returncode == 86
    published = PersistentRecordingHerdr(worktree, herdr_state)
    assert published.name == "ashe"
    assert published.tokens == {"sidebar_name": "Ashe", "thread_title": "Ashe"}
    assert published.title == "Ashe"
    with SQLiteStorage(state) as store:
        assignment = store.callsign_assignment_status(_spec().assignment_id)
        assert assignment is not None and assignment["state"] == "reserved"
        assert store.shotcaller_bootstrap_status(_spec().assignment_id) is None
        baseline = store.shotcaller_bootstrap_baseline(_spec().assignment_id)
        assert baseline is not None
        assert baseline["routing_name"] is None
        assert baseline["sidebar_name"] == "Original sidebar"
        assert baseline["thread_title"] == "Original thread"
        assert baseline["title"] == "Original title"

    retried = subprocess.run([*child, "retry-fault"], check=False, timeout=10)
    assert retried.returncode == 0
    restored = PersistentRecordingHerdr(worktree, herdr_state)
    assert restored.name is None
    assert restored.tokens == {
        "sidebar_name": "Original sidebar",
        "thread_title": "Original thread",
    }
    assert restored.title == "Original title"
    with SQLiteStorage(state) as store:
        assignment = store.callsign_assignment_status(_spec().assignment_id)
        assert assignment is not None and assignment["state"] == "rolled_back"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM runtime_instances WHERE actor_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE agent_id=? AND event_type='shotcaller_created'",
            (AGENT_ID,),
        ).fetchone()[0] == 0


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-shotcaller-bootstrap-") as temporary:
        root = Path(temporary)
        test_in_place_bootstrap_creates_shotcaller_without_layout_or_squad_registration(root)
        test_bootstrap_reasserts_owned_title_after_auto_title_settles(root)
        test_bootstrap_identity_mismatch_makes_no_canonical_mutation(root)
        test_preexisting_reserved_bootstrap_requires_durable_baseline_when_alias_empty(root)
        test_preexisting_reserved_bootstrap_rejects_residual_metadata_without_baseline(root)
        test_completed_bootstrap_retry_restores_owned_title_without_prompt(root)
        test_completed_bootstrap_retry_refuses_newer_user_title_with_stale_tokens(root)
        test_bootstrap_metadata_and_atomic_finalization_failures_restore_exact_state(root)
        test_bootstrap_rolls_back_when_owned_auto_title_never_settles(root)
        test_bootstrap_rolls_back_without_overwriting_newer_user_title(root)
        test_publish_crash_then_retry_fault_restores_original_unbound_metadata(root)
    print(
        "PASS: Shotcaller bootstrap stays in-place, Squad registration stays separate, "
        "and Champion launch owns a new tab root"
    )


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--bootstrap-process-child":
        raise SystemExit(
            _bootstrap_process_child(
                Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
            )
        )
    main()
