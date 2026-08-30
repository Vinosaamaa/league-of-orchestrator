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
        self, worktree: Path, *, thread_id: str = THREAD_ID, publish_mismatch: bool = False
    ) -> None:
        self.worktree = str(worktree.resolve())
        self.thread_id = thread_id
        self.publish_mismatch = publish_mismatch
        self.name: str | None = None
        self.tokens: dict[str, str] = {}
        self.title = ""
        self.state_change_seq = 7
        self.calls: list[tuple[str, ...]] = []

    def _agent(self) -> dict[str, object]:
        value: dict[str, object] = {
            "agent": "codex",
            "agent_status": "working",
            "agent_session": {"value": self.thread_id},
            "cwd": self.worktree,
            "foreground_cwd": self.worktree,
            "pane_id": "w1:p1",
            "state_change_seq": self.state_change_seq,
            "tab_id": "w1:t1",
            "terminal_id": "terminal:1",
            "workspace_id": "w1",
            "tokens": dict(self.tokens),
            "terminal_title_stripped": self.title,
        }
        if self.name is not None:
            value["name"] = self.name
        return value

    def run(
        self, arguments, *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        command = tuple(arguments)
        self.calls.append(command)
        if command == ("herdr", "pane", "current", "--current"):
            result = {"pane": self._agent()}
        elif command == ("herdr", "agent", "list"):
            result = {"agents": [self._agent()]}
        elif command[:3] == ("herdr", "agent", "rename"):
            self.name = None if command[-1] == "--clear" else command[-1]
            self.state_change_seq += 1
            result = {"agent": self._agent()}
        elif command[:3] == ("herdr", "pane", "report-metadata"):
            self.state_change_seq += 1
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
            return subprocess.CompletedProcess(command, 0, "", "")
        else:
            raise AssertionError(f"unexpected Herdr command: {command}")
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"id": "test", "result": result}) + "\n", ""
        )


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
        assert [call[:3] for call in runner.calls[:6]] == [
            ("herdr", "pane", "current"),
            ("herdr", "agent", "list"),
            ("herdr", "agent", "rename"),
            ("herdr", "pane", "report-metadata"),
            ("herdr", "pane", "current"),
            ("herdr", "agent", "list"),
        ]
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
        retry = service.bootstrap(_spec())
        assert retry == {**created, "idempotent": True}
        assert not any(
            call[:3] in {("herdr", "agent", "rename"), ("herdr", "pane", "report-metadata")}
            for call in runner.calls[6:]
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


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-shotcaller-bootstrap-") as temporary:
        root = Path(temporary)
        test_in_place_bootstrap_creates_shotcaller_without_layout_or_squad_registration(root)
        test_bootstrap_identity_mismatch_makes_no_canonical_mutation(root)
        test_bootstrap_metadata_and_atomic_finalization_failures_restore_exact_state(root)
    print(
        "PASS: Shotcaller bootstrap stays in-place, Squad registration stays separate, "
        "and Champion launch owns a new tab root"
    )


if __name__ == "__main__":
    main()
