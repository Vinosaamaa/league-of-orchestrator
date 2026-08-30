#!/usr/bin/env python3
"""In-place Shotcaller bootstrap and three-way placement-policy coverage."""

from __future__ import annotations

import hashlib
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
        self.terminal_id = "terminal:1"
        self.publish_mismatch = publish_mismatch
        self.delayed_auto_title_reads = delayed_auto_title_reads
        self.name: str | None = None
        self.tokens: dict[str, str] | None = {}
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
            "terminal_id": self.terminal_id,
            "workspace_id": "w1",
            "tokens": dict(self.tokens) if isinstance(self.tokens, dict) else self.tokens,
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
            if "--title" in command:
                self.metadata_source = source
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


class TransientMalformedCurrentHerdr(RecordingHerdr):
    """The exact in-place pane briefly returns non-JSON before a valid read."""

    def __init__(self, worktree: Path, malformed_reads: int = 1) -> None:
        super().__init__(worktree)
        self.malformed_current_reads = malformed_reads

    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        if (
            command == ("herdr", "pane", "current", "--current")
            and self.malformed_current_reads
        ):
            self.malformed_current_reads -= 1
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return super().run(arguments, timeout_seconds=timeout_seconds)


class InjectedBootstrapFault(RuntimeError):
    pass


class InjectedBootstrapCrash(BaseException):
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


def _service(
    store: SQLiteStorage,
    clock: FakeClock,
    worktree: Path,
    runner: RecordingHerdr,
) -> ShotcallerBootstrapService:
    return ShotcallerBootstrapService(
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


def _create_clean_bootstrap_residue(
    store: SQLiteStorage,
    service: ShotcallerBootstrapService,
    runner: RecordingHerdr,
) -> tuple[ShotcallerBootstrapSpec, dict[str, object]]:
    original = _spec()
    try:
        service.bootstrap(original)
    except StorageRefusal as exc:
        assert exc.code == "shotcaller_metadata_unverified"
    else:
        raise AssertionError("failed bootstrap did not create the retired residue")
    assignment = store.callsign_assignment_status(original.assignment_id)
    assert assignment is not None
    assert assignment["state"] == "rolled_back"
    assert assignment["version"] == 2
    runner.publish_mismatch = False
    return original, assignment


def _make_legacy_bootstrap_residue(
    store: SQLiteStorage,
    service: ShotcallerBootstrapService,
    runner: RecordingHerdr,
) -> tuple[ShotcallerBootstrapSpec, dict[str, object]]:
    original, assignment = _create_clean_bootstrap_residue(store, service, runner)
    store.connection.execute(
        "UPDATE agent_instances SET metadata_json='{}' WHERE agent_id=?",
        (original.agent_id,),
    )
    runner.metadata_source = "herdr:codex"
    runner.title = "Interview Prep"
    runner.tokens = {"user_theme": "focused"}
    runner.state_change_seq += 1
    return original, assignment


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
                "candidate_inventory_digest": intake["result"]["candidate_inventory"][
                    "digest"
                ],
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


def test_in_place_bootstrap_retries_transient_malformed_identity_read_without_layout(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "transient-malformed-current")
    worktree = root / "transient-malformed-current" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = TransientMalformedCurrentHerdr(worktree)
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
        assert runner.malformed_current_reads == 0
        assert sum(
            call == ("herdr", "pane", "current", "--current")
            for call in runner.calls
        ) >= 2
        forbidden = {
            ("herdr", "tab", "create"),
            ("herdr", "pane", "split"),
            ("herdr", "workspace", "create"),
            ("herdr", "agent", "start"),
        }
        assert not any(call[:3] in forbidden for call in runner.calls)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads"
        ).fetchone()[0] == 0


def test_in_place_bootstrap_refuses_persistently_malformed_identity_without_mutation(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "persistent-malformed-current")
    worktree = root / "persistent-malformed-current" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = TransientMalformedCurrentHerdr(worktree, malformed_reads=3)
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
            raise AssertionError("persistent malformed Herdr identity was accepted")
        assert store.callsign_status("shotcaller") == before
        assert runner.calls == [
            ("herdr", "pane", "current", "--current"),
            ("herdr", "pane", "current", "--current"),
            ("herdr", "pane", "current", "--current"),
        ]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads"
        ).fetchone()[0] == 0


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


def test_clean_rolled_back_bootstrap_residue_rebinds_same_thread_in_place(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "shotcaller-clean-residue-retry")
    worktree = root / "shotcaller-clean-residue-retry" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree, publish_mismatch=True)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = _service(store, clock, worktree, runner)
        original, original_assignment = _create_clean_bootstrap_residue(
            store, service, runner
        )
        retry = ShotcallerBootstrapSpec(
            assignment_id="callsign-assignment:bootstrap:ashe:retry",
            agent_id=original.agent_id,
            runtime_instance_id=original.runtime_instance_id,
            thread_id=original.thread_id,
            capabilities=original.capabilities,
        )
        calls_before_retry = len(runner.calls)

        created = service.bootstrap(retry)

        assert created["state"] == "active"
        assert created["callsign"] == original_assignment["callsign"]
        assert store.callsign_assignment_status(original.assignment_id) == original_assignment
        assert store.callsign_assignment_status(retry.assignment_id)["state"] == "active"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squad_registration_offers WHERE shotcaller_agent_id=?",
            (AGENT_ID,),
        ).fetchone()[0] == 0
        created_event = store.connection.execute(
            "SELECT entity_version,detail_json FROM events WHERE event_id=?",
            (f"shotcaller:{retry.assignment_id}:created",),
        ).fetchone()
        assert created_event is not None
        assert created_event["entity_version"] == 4
        assert json.loads(created_event["detail_json"])["placement"] == "existing-current-pane"

    retry_calls = runner.calls[calls_before_retry:]
    assert runner.name == "ashe"
    assert not any(
        call[:3]
        in {
            ("herdr", "tab", "create"),
            ("herdr", "pane", "split"),
            ("herdr", "workspace", "create"),
            ("herdr", "agent", "start"),
            ("herdr", "agent", "prompt"),
        }
        for call in retry_calls
    )


def test_legacy_rolled_back_bootstrap_residue_captures_clean_live_baseline(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "shotcaller-legacy-residue-retry")
    worktree = root / "shotcaller-legacy-residue-retry" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree, publish_mismatch=True)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = _service(store, clock, worktree, runner)
        original, original_assignment = _make_legacy_bootstrap_residue(
            store, service, runner
        )
        retry = ShotcallerBootstrapSpec(
            assignment_id="callsign-assignment:bootstrap:ashe:legacy-retry",
            agent_id=original.agent_id,
            runtime_instance_id=original.runtime_instance_id,
            thread_id=original.thread_id,
            capabilities=original.capabilities,
        )
        expected_baseline = {
            "schema": "league.shotcaller-bootstrap-baseline.v2",
            "terminal_id": "terminal:1",
            "endpoint_generation": "herdr:"
            + hashlib.sha256(
                f"terminal:1\0{original.thread_id}".encode("utf-8")
            ).hexdigest()[:24],
            "state_change_seq": runner.state_change_seq,
            "routing_name": None,
            "sidebar_name": "",
            "thread_title": "",
            "title": "Interview Prep",
            "presentation_source": "herdr:codex",
        }
        calls_before_retry = len(runner.calls)

        created = service.bootstrap(retry)

        assert created["state"] == "active"
        assert created["callsign"] == original_assignment["callsign"]
        assert store.callsign_assignment_status(original.assignment_id) == original_assignment
        assert store.shotcaller_bootstrap_baseline(retry.assignment_id) == expected_baseline
        metadata = json.loads(
            store.connection.execute(
                "SELECT metadata_json FROM agent_instances WHERE agent_id=?",
                (AGENT_ID,),
            ).fetchone()[0]
        )
        assert metadata == {
            "scope_kind": "shotcaller",
            "scope_id": AGENT_ID,
            "shotcaller_bootstrap_baseline": expected_baseline,
        }
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0

    retry_calls = runner.calls[calls_before_retry:]
    assert runner.name == "ashe"
    assert runner.title == "Ashe"
    assert runner.tokens == {
        "user_theme": "focused",
        "sidebar_name": "Ashe",
        "thread_title": "Ashe",
    }
    assert not any(
        call[:3]
        in {
            ("herdr", "tab", "create"),
            ("herdr", "pane", "split"),
            ("herdr", "workspace", "create"),
            ("herdr", "agent", "start"),
            ("herdr", "agent", "prompt"),
        }
        for call in retry_calls
    )


def test_legacy_residue_refuses_dirty_or_ambiguous_state_before_publication(
    root: Path,
) -> None:
    cases = (
        "sidebar-residue",
        "thread-residue",
        "title-residue",
        "league-source",
        "unproven-tokens",
        "foreign-subject",
        "partial-agent-metadata",
        "ambiguous-history",
    )
    for case in cases:
        state, _ = migrated_state(root, f"shotcaller-legacy-refusal-{case}")
        worktree = root / f"shotcaller-legacy-refusal-{case}" / "worktree"
        worktree.mkdir()
        clock = FakeClock()
        runner = RecordingHerdr(worktree, publish_mismatch=True)
        with SQLiteStorage(state) as store:
            _seed_available_ashe(store, clock)
            service = _service(store, clock, worktree, runner)
            original, original_assignment = _make_legacy_bootstrap_residue(
                store, service, runner
            )
            if case == "sidebar-residue":
                runner.tokens["sidebar_name"] = "Ashe"
            elif case == "thread-residue":
                runner.tokens["thread_title"] = "Ashe"
            elif case == "title-residue":
                runner.title = "Ashe"
            elif case == "league-source":
                runner.metadata_source = "league-shotcaller-legacy"
            elif case == "unproven-tokens":
                runner.tokens = None
            elif case == "foreign-subject":
                store.connection.execute(
                    "UPDATE callsign_assignments SET subject_id='agent:foreign' "
                    "WHERE callsign_assignment_id=?",
                    (original.assignment_id,),
                )
            elif case == "partial-agent-metadata":
                store.connection.execute(
                    "UPDATE agent_instances SET metadata_json=? WHERE agent_id=?",
                    (json.dumps({"scope_kind": "shotcaller"}), AGENT_ID),
                )
            elif case == "ambiguous-history":
                store.connection.execute(
                    """
                    INSERT INTO callsign_assignments
                      (callsign_assignment_id,callsign,subject_id,agent_id,
                       runtime_instance_id,role,scope_kind,scope_id,state,
                       reservation_position,queue_version,requirements_json,
                       acceptance_digest,release_receipt_digest,failure_receipt_digest,
                       version,reserved_at,activated_at,released_at)
                    VALUES('callsign-assignment:legacy:ambiguous','Ashe',?, ?,NULL,
                           'shotcaller','shotcaller',?,'rolled_back',0,1,'[]',NULL,NULL,
                           'failure:legacy:ambiguous',2,?,NULL,?)
                    """,
                    (
                        "agent:legacy:ambiguous",
                        AGENT_ID,
                        AGENT_ID,
                        clock.now(),
                        clock.now(),
                    ),
                )
            retry = ShotcallerBootstrapSpec(
                assignment_id=f"callsign-assignment:bootstrap:ashe:legacy:{case}",
                agent_id=original.agent_id,
                runtime_instance_id=original.runtime_instance_id,
                thread_id=original.thread_id,
                capabilities=original.capabilities,
            )
            agent_before = tuple(
                store.connection.execute(
                    "SELECT version,retired_at,metadata_json FROM agent_instances "
                    "WHERE agent_id=?",
                    (AGENT_ID,),
                ).fetchone()
            )
            queue_before = tuple(
                store.connection.execute(
                    "SELECT state,reservation_assignment_id,version FROM callsign_queue "
                    "WHERE callsign='Ashe'"
                ).fetchone()
            )
            calls_before_retry = len(runner.calls)

            try:
                service.bootstrap(retry)
            except StorageRefusal as exc:
                expected = (
                    "shotcaller_identity_unverified"
                    if case == "unproven-tokens"
                    else "agent_conflict"
                )
                assert exc.code == expected
            else:
                raise AssertionError(f"unsafe legacy residue {case} was rebound")

            assert store.callsign_assignment_status(original.assignment_id) == original_assignment
            assert store.callsign_assignment_status(retry.assignment_id) is None
            assert tuple(
                store.connection.execute(
                    "SELECT version,retired_at,metadata_json FROM agent_instances "
                    "WHERE agent_id=?",
                    (AGENT_ID,),
                ).fetchone()
            ) == agent_before
            assert tuple(
                store.connection.execute(
                    "SELECT state,reservation_assignment_id,version FROM callsign_queue "
                    "WHERE callsign='Ashe'"
                ).fetchone()
            ) == queue_before
        retry_calls = runner.calls[calls_before_retry:]
        assert not any(
            call[:3]
            in {
                ("herdr", "agent", "rename"),
                ("herdr", "pane", "report-metadata"),
                ("herdr", "agent", "prompt"),
                ("herdr", "tab", "create"),
                ("herdr", "pane", "split"),
                ("herdr", "workspace", "create"),
                ("herdr", "agent", "start"),
            }
            for call in retry_calls
        )


def test_legacy_residue_refuses_newer_presentation_write_before_publication(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "shotcaller-legacy-presentation-race")
    worktree = root / "shotcaller-legacy-presentation-race" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree, publish_mismatch=True)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = _service(store, clock, worktree, runner)
        original, original_assignment = _make_legacy_bootstrap_residue(
            store, service, runner
        )
        retry = ShotcallerBootstrapSpec(
            assignment_id="callsign-assignment:bootstrap:ashe:legacy-race",
            agent_id=original.agent_id,
            runtime_instance_id=original.runtime_instance_id,
            thread_id=original.thread_id,
            capabilities=original.capabilities,
        )
        allocate = store.allocate_callsign

        def interleaved_allocate(*args, **kwargs):
            runner.metadata_source = "user-selected"
            runner.title = "User selected title"
            runner.tokens = {"user_theme": "newer"}
            runner.state_change_seq += 1
            return allocate(*args, **kwargs)

        store.allocate_callsign = interleaved_allocate  # type: ignore[method-assign]
        calls_before_retry = len(runner.calls)

        try:
            service.bootstrap(retry)
        except StorageRefusal as exc:
            assert exc.code == "shotcaller_metadata_unverified"
        else:
            raise AssertionError("legacy recovery overwrote a newer presentation write")

        assert store.callsign_assignment_status(original.assignment_id) == original_assignment
        attempted = store.callsign_assignment_status(retry.assignment_id)
        assert attempted is not None and attempted["state"] == "rolled_back"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM runtime_instances WHERE actor_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0

    retry_calls = runner.calls[calls_before_retry:]
    assert runner.name is None
    assert runner.metadata_source == "user-selected"
    assert runner.title == "User selected title"
    assert runner.tokens == {"user_theme": "newer"}
    assert not any(
        call[:3]
        in {
            ("herdr", "agent", "rename"),
            ("herdr", "pane", "report-metadata"),
            ("herdr", "agent", "prompt"),
            ("herdr", "tab", "create"),
            ("herdr", "pane", "split"),
            ("herdr", "workspace", "create"),
            ("herdr", "agent", "start"),
        }
        for call in retry_calls
    )


def test_legacy_residue_refuses_thread_or_generation_race_before_publication(
    root: Path,
) -> None:
    for case in ("thread", "generation"):
        state, _ = migrated_state(root, f"shotcaller-legacy-{case}-race")
        worktree = root / f"shotcaller-legacy-{case}-race" / "worktree"
        worktree.mkdir()
        clock = FakeClock()
        runner = RecordingHerdr(worktree, publish_mismatch=True)
        with SQLiteStorage(state) as store:
            _seed_available_ashe(store, clock)
            service = _service(store, clock, worktree, runner)
            original, original_assignment = _make_legacy_bootstrap_residue(
                store, service, runner
            )
            retry = ShotcallerBootstrapSpec(
                assignment_id=f"callsign-assignment:bootstrap:ashe:legacy-{case}-race",
                agent_id=original.agent_id,
                runtime_instance_id=original.runtime_instance_id,
                thread_id=original.thread_id,
                capabilities=original.capabilities,
            )
            allocate = store.allocate_callsign

            def interleaved_allocate(*args, **kwargs):
                if case == "thread":
                    runner.thread_id = "77777777-7777-4777-8777-777777777777"
                else:
                    runner.terminal_id = "terminal:foreign"
                runner.state_change_seq += 1
                return allocate(*args, **kwargs)

            store.allocate_callsign = interleaved_allocate  # type: ignore[method-assign]
            calls_before_retry = len(runner.calls)

            try:
                service.bootstrap(retry)
            except StorageRefusal as exc:
                assert exc.code == "shotcaller_metadata_unverified"
            else:
                raise AssertionError(f"legacy recovery accepted a {case} race")

            assert store.callsign_assignment_status(original.assignment_id) == original_assignment
            attempted = store.callsign_assignment_status(retry.assignment_id)
            assert attempted is not None and attempted["state"] == "rolled_back"
            assert store.connection.execute(
                "SELECT COUNT(*) FROM runtime_instances WHERE actor_agent_id=?", (AGENT_ID,)
            ).fetchone()[0] == 0
            assert store.connection.execute(
                "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
            ).fetchone()[0] == 0

        retry_calls = runner.calls[calls_before_retry:]
        assert runner.name is None
        assert not any(
            call[:3]
            in {
                ("herdr", "agent", "rename"),
                ("herdr", "pane", "report-metadata"),
                ("herdr", "agent", "prompt"),
                ("herdr", "tab", "create"),
                ("herdr", "pane", "split"),
                ("herdr", "workspace", "create"),
                ("herdr", "agent", "start"),
            }
            for call in retry_calls
        )


def test_legacy_residue_crash_after_baseline_retries_exactly_once(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "shotcaller-legacy-crash-retry")
    worktree = root / "shotcaller-legacy-crash-retry" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree, publish_mismatch=True)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = _service(store, clock, worktree, runner)
        original, original_assignment = _make_legacy_bootstrap_residue(
            store, service, runner
        )
        retry = ShotcallerBootstrapSpec(
            assignment_id="callsign-assignment:bootstrap:ashe:legacy-crash",
            agent_id=original.agent_id,
            runtime_instance_id=original.runtime_instance_id,
            thread_id=original.thread_id,
            capabilities=original.capabilities,
        )
        calls_before_crash = len(runner.calls)

        def crash(point: str) -> None:
            if point == "after_shotcaller_recovery_reserved":
                raise InjectedBootstrapCrash(point)

        try:
            service.bootstrap(retry, fault=crash)
        except InjectedBootstrapCrash:
            pass
        else:
            raise AssertionError("legacy recovery did not stop at the crash boundary")

        reserved = store.callsign_assignment_status(retry.assignment_id)
        assert reserved is not None and reserved["state"] == "reserved"
        baseline = store.shotcaller_bootstrap_baseline(retry.assignment_id)
        assert baseline is not None
        assert baseline["schema"] == "league.shotcaller-bootstrap-baseline.v2"
        assert baseline["title"] == "Interview Prep"
        assert baseline["presentation_source"] == "herdr:codex"
        assert store.callsign_assignment_status(original.assignment_id) == original_assignment
        crash_calls = runner.calls[calls_before_crash:]
        assert not any(
            call[:3]
            in {
                ("herdr", "agent", "rename"),
                ("herdr", "pane", "report-metadata"),
                ("herdr", "agent", "prompt"),
            }
            for call in crash_calls
        )

        created = service.bootstrap(retry)
        calls_before_exact_retry = len(runner.calls)
        retried = service.bootstrap(retry)

        assert created["state"] == "active"
        assert retried == {**created, "idempotent": True}
        assert store.shotcaller_bootstrap_status(retry.assignment_id) == retried
        assert store.callsign_assignment_status(original.assignment_id) == original_assignment
        assert store.connection.execute(
            "SELECT COUNT(*) FROM callsign_assignments WHERE agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 2
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_id=?",
            (f"callsign:{retry.assignment_id}:reserved",),
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0

    exact_retry_calls = runner.calls[calls_before_exact_retry:]
    assert not any(
        call[:3]
        in {
            ("herdr", "agent", "rename"),
            ("herdr", "pane", "report-metadata"),
            ("herdr", "agent", "prompt"),
            ("herdr", "tab", "create"),
            ("herdr", "pane", "split"),
            ("herdr", "workspace", "create"),
            ("herdr", "agent", "start"),
        }
        for call in exact_retry_calls
    )


def test_legacy_residue_finalization_failure_restores_captured_presentation(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "shotcaller-legacy-finalization-rollback")
    worktree = root / "shotcaller-legacy-finalization-rollback" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree, publish_mismatch=True)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = _service(store, clock, worktree, runner)
        original, original_assignment = _make_legacy_bootstrap_residue(
            store, service, runner
        )
        retry = ShotcallerBootstrapSpec(
            assignment_id="callsign-assignment:bootstrap:ashe:legacy-rollback",
            agent_id=original.agent_id,
            runtime_instance_id=original.runtime_instance_id,
            thread_id=original.thread_id,
            capabilities=original.capabilities,
        )

        def fail_finalization(point: str) -> None:
            if point == "after_shotcaller_activation":
                raise InjectedBootstrapFault(point)

        try:
            service.bootstrap(retry, fault=fail_finalization)
        except InjectedBootstrapFault:
            pass
        else:
            raise AssertionError("legacy recovery finalization fault escaped")

        assert store.callsign_assignment_status(original.assignment_id) == original_assignment
        attempted = store.callsign_assignment_status(retry.assignment_id)
        assert attempted is not None and attempted["state"] == "rolled_back"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM runtime_instances WHERE actor_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squad_registration_offers WHERE shotcaller_agent_id=?",
            (AGENT_ID,),
        ).fetchone()[0] == 0

    assert runner.name is None
    assert runner.title == "Interview Prep"
    assert runner.tokens == {"user_theme": "focused"}
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


def test_recovered_bootstrap_exact_retry_is_receipt_identical_and_read_only(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "shotcaller-recovered-exact-retry")
    worktree = root / "shotcaller-recovered-exact-retry" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree, publish_mismatch=True)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = _service(store, clock, worktree, runner)
        original, _ = _create_clean_bootstrap_residue(store, service, runner)
        spec = ShotcallerBootstrapSpec(
            assignment_id="callsign-assignment:bootstrap:ashe:recovered",
            agent_id=original.agent_id,
            runtime_instance_id=original.runtime_instance_id,
            thread_id=original.thread_id,
            capabilities=original.capabilities,
        )
        created = service.bootstrap(spec)
        calls_before_retry = len(runner.calls)

        retried = service.bootstrap(spec)

        assert retried == {**created, "idempotent": True}
        assert store.shotcaller_bootstrap_status(spec.assignment_id) == retried
    retry_calls = runner.calls[calls_before_retry:]
    assert not any(
        call[:3]
        in {
            ("herdr", "agent", "rename"),
            ("herdr", "pane", "report-metadata"),
            ("herdr", "agent", "prompt"),
            ("herdr", "tab", "create"),
            ("herdr", "pane", "split"),
            ("herdr", "workspace", "create"),
            ("herdr", "agent", "start"),
        }
        for call in retry_calls
    )


def test_recovered_bootstrap_finalization_fault_rolls_back_without_losing_history(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "shotcaller-recovered-rollback")
    worktree = root / "shotcaller-recovered-rollback" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree, publish_mismatch=True)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = _service(store, clock, worktree, runner)
        original, original_assignment = _create_clean_bootstrap_residue(
            store, service, runner
        )
        retry = ShotcallerBootstrapSpec(
            assignment_id="callsign-assignment:bootstrap:ashe:rollback-retry",
            agent_id=original.agent_id,
            runtime_instance_id=original.runtime_instance_id,
            thread_id=original.thread_id,
            capabilities=original.capabilities,
        )

        def fail_finalization(point: str) -> None:
            if point == "after_shotcaller_activation":
                raise InjectedBootstrapFault(point)

        try:
            service.bootstrap(retry, fault=fail_finalization)
        except InjectedBootstrapFault:
            pass
        else:
            raise AssertionError("recovered bootstrap finalization fault escaped")

        assert store.callsign_assignment_status(original.assignment_id) == original_assignment
        recovered = store.callsign_assignment_status(retry.assignment_id)
        assert recovered is not None and recovered["state"] == "rolled_back"
        assert store.connection.execute(
            "SELECT state FROM callsign_queue WHERE callsign=?",
            (original_assignment["callsign"],),
        ).fetchone()[0] == "available"
        agent = store.connection.execute(
            "SELECT kind,retired_at,version FROM agent_instances WHERE agent_id=?",
            (AGENT_ID,),
        ).fetchone()
        assert agent["kind"] == "unbound"
        assert agent["retired_at"] is not None
        assert agent["version"] == 4
        assert store.connection.execute(
            "SELECT COUNT(*) FROM runtime_instances WHERE actor_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE shotcaller_agent_id=?", (AGENT_ID,)
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squad_registration_offers WHERE shotcaller_agent_id=?",
            (AGENT_ID,),
        ).fetchone()[0] == 0
    assert runner.name is None
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


def test_retired_shotcaller_recovery_refuses_unsafe_residue_shapes_without_mutation(
    root: Path,
) -> None:
    cases = (
        "different-thread",
        "runtime-bearing",
        "squad-bearing",
        "offer-bearing",
        "active-assignment",
        "active-callsign",
        "ambiguous-history",
        "malformed-rollback-history",
        "foreign-subject",
        "non-bootstrap-retired-agent",
    )
    for case in cases:
        state, _ = migrated_state(root, f"shotcaller-residue-refusal-{case}")
        worktree = root / f"shotcaller-residue-refusal-{case}" / "worktree"
        worktree.mkdir()
        clock = FakeClock()
        runner = RecordingHerdr(worktree, publish_mismatch=True)
        with SQLiteStorage(state) as store:
            _seed_available_ashe(store, clock)
            service = _service(store, clock, worktree, runner)
            original, _ = _create_clean_bootstrap_residue(store, service, runner)
            thread_id = original.thread_id
            if case == "different-thread":
                thread_id = "77777777-7777-4777-8777-777777777777"
                runner.thread_id = thread_id
            elif case == "runtime-bearing":
                store.connection.execute(
                    """
                    INSERT INTO runtime_instances
                      (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,
                       session_ref,endpoint,runtime_generation,status,verified,last_seen_at,
                       capabilities_json)
                    VALUES('runtime:residue',?,'codex-thread','herdr',?,
                           'pane:residue','generation:residue','closed',1,?,'[]')
                    """,
                    (AGENT_ID, original.thread_id, clock.now()),
                )
            elif case == "squad-bearing":
                store.connection.execute(
                    "INSERT INTO squads(squad_id,shotcaller_agent_id,state,version,updated_at) "
                    "VALUES('squad:residue',?,'retired',1,?)",
                    (AGENT_ID, clock.now()),
                )
            elif case == "offer-bearing":
                requester = "agent:offer-requester"
                store.connection.execute(
                    """
                    INSERT INTO agent_instances
                      (agent_id,callsign,role,shotcaller_agent_id,task_id,kind,address,
                       thread_id,backend,routing_name,display_agent,repository,issue,branch,
                       worktree,status,version,updated_at,update_text,blocker,next_action,
                       metadata_json,retired_at)
                    VALUES(?,'Ashe','hidden-worker',NULL,NULL,'unbound',NULL,NULL,NULL,NULL,
                           NULL,NULL,NULL,NULL,NULL,'active',1,?,'offer fixture',NULL,
                           'none','{}',?)
                    """,
                    (requester, clock.now(), clock.now()),
                )
                store.connection.execute(
                    """
                    INSERT INTO runtime_instances
                      (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,
                       session_ref,endpoint,runtime_generation,status,verified,last_seen_at,
                       capabilities_json)
                    VALUES('runtime:offer',?,'codex-thread','herdr','thread:offer',
                           'pane:offer','generation:offer','closed',1,?,'[]')
                    """,
                    (requester, clock.now()),
                )
                store.connection.execute(
                    "INSERT INTO events(event_id,agent_id,task_id,entity_version,event_type,"
                    "status,update_text,occurred_at,detail_json) "
                    "VALUES('event:offer',?,NULL,1,'squad_registration_offered','pending',"
                    "'offer fixture',?,'{}')",
                    (requester, clock.now()),
                )
                store.connection.execute(
                    "INSERT INTO delivery_outbox(outbox_id,event_id,recipient_agent_id,state,"
                    "available_at) VALUES('outbox:offer','event:offer',?,'pending',?)",
                    (requester, clock.now()),
                )
                store.connection.execute(
                    """
                    INSERT INTO squad_registration_offers
                      (registration_id,squad_id,requester_agent_id,shotcaller_agent_id,
                       runtime_instance_id,project_ids_json,capabilities_json,state,expires_at,
                       offer_event_id,offer_outbox_id,response_event_id,response_outbox_id,
                       registered_at,responded_at)
                    VALUES('registration:residue','squad:offered',?,?,'runtime:offer','[]','[]',
                           'rejected',?,'event:offer','outbox:offer',NULL,NULL,?,?)
                    """,
                    (requester, AGENT_ID, clock.now(), clock.now(), clock.now()),
                )
            elif case == "active-assignment":
                store.connection.execute(
                    "UPDATE callsign_assignments SET state='active',activated_at=? "
                    "WHERE callsign_assignment_id=?",
                    (clock.now(), original.assignment_id),
                )
            elif case == "active-callsign":
                store.connection.execute(
                    "UPDATE callsign_queue SET state='active',queue_position=NULL "
                    "WHERE callsign='Ashe'"
                )
            elif case == "ambiguous-history":
                store.connection.execute(
                    """
                    INSERT INTO callsign_assignments
                      (callsign_assignment_id,callsign,subject_id,agent_id,runtime_instance_id,
                       role,scope_kind,scope_id,state,reservation_position,queue_version,
                       requirements_json,acceptance_digest,release_receipt_digest,
                       failure_receipt_digest,version,reserved_at,activated_at,released_at)
                    VALUES('callsign-assignment:ambiguous','Ashe','agent:ambiguous',?,NULL,
                           'shotcaller','shotcaller',?,'rolled_back',0,1,'[]',NULL,NULL,
                           'failure:ambiguous',2,?,NULL,?)
                    """,
                    (AGENT_ID, AGENT_ID, clock.now(), clock.now()),
                )
            elif case == "malformed-rollback-history":
                store.connection.execute(
                    "UPDATE events SET detail_json='{}' WHERE event_id=?",
                    (f"callsign:{original.assignment_id}:rolled-back",),
                )
            elif case == "foreign-subject":
                store.connection.execute(
                    "UPDATE callsign_assignments SET subject_id='agent:foreign' "
                    "WHERE callsign_assignment_id=?",
                    (original.assignment_id,),
                )
            elif case == "non-bootstrap-retired-agent":
                store.connection.execute(
                    "UPDATE agent_instances SET metadata_json=? WHERE agent_id=?",
                    (json.dumps({"scope_kind": "shotcaller", "scope_id": AGENT_ID}), AGENT_ID),
                )
            retry = ShotcallerBootstrapSpec(
                assignment_id=f"callsign-assignment:bootstrap:ashe:refuse:{case}",
                agent_id=original.agent_id,
                runtime_instance_id=original.runtime_instance_id,
                thread_id=thread_id,
                capabilities=original.capabilities,
            )
            agent_before = tuple(
                store.connection.execute(
                    "SELECT version,retired_at,metadata_json FROM agent_instances WHERE agent_id=?",
                    (AGENT_ID,),
                ).fetchone()
            )
            queue_before = tuple(
                store.connection.execute(
                    "SELECT state,reservation_assignment_id,version FROM callsign_queue "
                    "WHERE callsign='Ashe'"
                ).fetchone()
            )
            history_before = tuple(
                store.connection.execute(
                    "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                    (original.assignment_id,),
                ).fetchone()
            )
            calls_before_retry = len(runner.calls)

            try:
                service.bootstrap(retry)
            except StorageRefusal as exc:
                assert exc.code == "agent_conflict"
            else:
                raise AssertionError(f"unsafe residue {case} was rebound")

            assert store.callsign_assignment_status(retry.assignment_id) is None
            assert tuple(
                store.connection.execute(
                    "SELECT version,retired_at,metadata_json FROM agent_instances WHERE agent_id=?",
                    (AGENT_ID,),
                ).fetchone()
            ) == agent_before
            assert tuple(
                store.connection.execute(
                    "SELECT state,reservation_assignment_id,version FROM callsign_queue "
                    "WHERE callsign='Ashe'"
                ).fetchone()
            ) == queue_before
            assert tuple(
                store.connection.execute(
                    "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                    (original.assignment_id,),
                ).fetchone()
            ) == history_before
        retry_calls = runner.calls[calls_before_retry:]
        assert not any(
            call[:3]
            in {
                ("herdr", "agent", "rename"),
                ("herdr", "pane", "report-metadata"),
                ("herdr", "agent", "prompt"),
                ("herdr", "tab", "create"),
                ("herdr", "pane", "split"),
                ("herdr", "workspace", "create"),
                ("herdr", "agent", "start"),
            }
            for call in retry_calls
        )


def test_retired_shotcaller_recovery_refuses_interleaved_agent_write(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "shotcaller-residue-race")
    worktree = root / "shotcaller-residue-race" / "worktree"
    worktree.mkdir()
    clock = FakeClock()
    runner = RecordingHerdr(worktree, publish_mismatch=True)
    with SQLiteStorage(state) as store:
        _seed_available_ashe(store, clock)
        service = _service(store, clock, worktree, runner)
        original, _ = _create_clean_bootstrap_residue(store, service, runner)
        retry = ShotcallerBootstrapSpec(
            assignment_id="callsign-assignment:bootstrap:ashe:race",
            agent_id=original.agent_id,
            runtime_instance_id=original.runtime_instance_id,
            thread_id=original.thread_id,
            capabilities=original.capabilities,
        )
        queue_before = tuple(
            store.connection.execute(
                "SELECT state,reservation_assignment_id,version FROM callsign_queue "
                "WHERE callsign='Ashe'"
            ).fetchone()
        )
        allocate = store.allocate_callsign

        def interleaved_allocate(*args, **kwargs):
            store.connection.execute(
                "UPDATE agent_instances SET version=version+1,update_text='newer owner write' "
                "WHERE agent_id=?",
                (AGENT_ID,),
            )
            return allocate(*args, **kwargs)

        store.allocate_callsign = interleaved_allocate  # type: ignore[method-assign]
        calls_before_retry = len(runner.calls)

        try:
            service.bootstrap(retry)
        except StorageRefusal as exc:
            assert exc.code == "agent_conflict"
        else:
            raise AssertionError("interleaved retired-agent write was overwritten")

        assert store.callsign_assignment_status(retry.assignment_id) is None
        agent = store.connection.execute(
            "SELECT version,retired_at,update_text FROM agent_instances WHERE agent_id=?",
            (AGENT_ID,),
        ).fetchone()
        assert tuple(agent) == (3, clock.now(), "newer owner write")
        assert tuple(
            store.connection.execute(
                "SELECT state,reservation_assignment_id,version FROM callsign_queue "
                "WHERE callsign='Ashe'"
            ).fetchone()
        ) == queue_before
    retry_calls = runner.calls[calls_before_retry:]
    assert not any(
        call[:3]
        in {
            ("herdr", "agent", "rename"),
            ("herdr", "pane", "report-metadata"),
            ("herdr", "agent", "prompt"),
            ("herdr", "tab", "create"),
            ("herdr", "pane", "split"),
            ("herdr", "workspace", "create"),
            ("herdr", "agent", "start"),
        }
        for call in retry_calls
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
    runner.tokens = {"user_badge": "favorite"}
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
    assert runner.tokens == {"user_badge": "favorite"}
    metadata_calls = [
        call
        for call in runner.calls
        if call[:3] == ("herdr", "pane", "report-metadata")
    ]
    assert len(metadata_calls) == 2
    rollback_metadata = metadata_calls[-1]
    assert "--title" not in rollback_metadata
    rollback_tokens = {
        rollback_metadata[index + 1]
        for index, value in enumerate(rollback_metadata)
        if value == "--token"
    }
    assert rollback_tokens == {"sidebar_name=", "thread_title="}
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
        test_in_place_bootstrap_retries_transient_malformed_identity_read_without_layout(root)
        test_in_place_bootstrap_refuses_persistently_malformed_identity_without_mutation(root)
        test_bootstrap_reasserts_owned_title_after_auto_title_settles(root)
        test_bootstrap_identity_mismatch_makes_no_canonical_mutation(root)
        test_preexisting_reserved_bootstrap_requires_durable_baseline_when_alias_empty(root)
        test_preexisting_reserved_bootstrap_rejects_residual_metadata_without_baseline(root)
        test_completed_bootstrap_retry_restores_owned_title_without_prompt(root)
        test_completed_bootstrap_retry_refuses_newer_user_title_with_stale_tokens(root)
        test_bootstrap_metadata_and_atomic_finalization_failures_restore_exact_state(root)
        test_clean_rolled_back_bootstrap_residue_rebinds_same_thread_in_place(root)
        test_legacy_rolled_back_bootstrap_residue_captures_clean_live_baseline(root)
        test_legacy_residue_refuses_dirty_or_ambiguous_state_before_publication(root)
        test_legacy_residue_refuses_newer_presentation_write_before_publication(root)
        test_legacy_residue_refuses_thread_or_generation_race_before_publication(root)
        test_legacy_residue_crash_after_baseline_retries_exactly_once(root)
        test_legacy_residue_finalization_failure_restores_captured_presentation(root)
        test_recovered_bootstrap_exact_retry_is_receipt_identical_and_read_only(root)
        test_recovered_bootstrap_finalization_fault_rolls_back_without_losing_history(root)
        test_retired_shotcaller_recovery_refuses_unsafe_residue_shapes_without_mutation(root)
        test_retired_shotcaller_recovery_refuses_interleaved_agent_write(root)
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
