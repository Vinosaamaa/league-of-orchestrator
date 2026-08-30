#!/usr/bin/env python3
"""Installed-shape SQLite watcher dispatch, Stop, and supervise regressions."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from storage_fixture import AT2, CHAMPION_ID, SHOTCALLER_ID, TASK_ID  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.sqlite_watcher_ops import _obligation_counts  # noqa: E402
from league.canonical_watcher import _supervision_snapshot  # noqa: E402


WATCHER = ROOT / "bin/agent-watcher"
LEAGUE = ROOT / "bin/league"


def _environment(root: Path, state: Path) -> dict[str, str]:
    pointer = root / "league-writer-pointer.json"
    pointer.write_text(
        json.dumps(
            {"writer": "sqlite", "generation": "synthetic-generation"},
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    installed = root / "installed-agent-watcher"
    installed.symlink_to(WATCHER)
    return {
        **os.environ,
        "LEAGUE_WRITER_POINTER": str(pointer),
        "LEAGUE_STATE_ROOT": str(state),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEST_INSTALLED_WATCHER": str(installed),
    }


def _pointer_environment(root: Path) -> dict[str, str]:
    env = _environment(root, root / "league")
    env.pop("LEAGUE_STATE_ROOT")
    return env


def _watcher(
    env: dict[str, str], *arguments: str, payload: dict[str, str] | None = None
) -> dict[str, object]:
    result = subprocess.run(
        [env["TEST_INSTALLED_WATCHER"], *arguments],
        input=None if payload is None else json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _hook_source_event_key(adapter_kind: str, payload: dict[str, str]) -> str:
    session_ref = payload[
        "session_id" if adapter_kind == "codex" else "conversation_id"
    ]
    raw_key = payload[
        "turn_id" if adapter_kind == "codex" else "generation_id"
    ]
    body_hash = hashlib.sha256(payload["prompt"].encode("utf-8")).hexdigest()
    digest = hashlib.sha256(
        f"{adapter_kind}\0{session_ref}\0{raw_key}\0{body_hash}".encode("utf-8")
    ).hexdigest()
    return f"hook:{digest}"


def _league(state: Path, *arguments: str) -> dict[str, object]:
    return _league_env(state, os.environ, *arguments)


def _league_env(
    state: Path, env: dict[str, str], *arguments: str
) -> dict[str, object]:
    result = subprocess.run(
        [str(LEAGUE), "--state-root", str(state), *arguments],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _register_garen_runtime(
    state: Path, suffix: str, *, session_ref: str | None = None
) -> str:
    runtime_id = f"runtime:installed:{suffix}"
    _league(
        state,
        "hook",
        "register-runtime",
        "--runtime-instance-id",
        runtime_id,
        "--actor-agent-id",
        SHOTCALLER_ID,
        "--harness-kind",
        "codex-thread",
        "--backend-kind",
        "herdr",
        "--session-ref",
        session_ref or f"session:{suffix}",
        "--endpoint",
        "garen",
        "--runtime-generation",
        f"generation:{suffix}",
        "--status",
        "active",
        "--verified",
        "--at",
        AT2,
    )
    return runtime_id


def _register_champion_runtime(state: Path, suffix: str, session_ref: str) -> str:
    runtime_id = f"runtime:champion:{suffix}"
    _league(
        state,
        "hook",
        "register-runtime",
        "--runtime-instance-id",
        runtime_id,
        "--actor-agent-id",
        CHAMPION_ID,
        "--harness-kind",
        "codex-thread",
        "--backend-kind",
        "herdr",
        "--session-ref",
        session_ref,
        "--endpoint",
        "synthetic-champion",
        "--runtime-generation",
        f"generation:{suffix}",
        "--status",
        "active",
        "--verified",
        "--at",
        AT2,
    )
    return runtime_id


def _fake_herdr(root: Path, env: dict[str, str]) -> Path:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    prompt_log = root / "prompts.log"
    fake = fake_bin / "herdr"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$PROMPT_LOG\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PROMPT_LOG": str(prompt_log),
            "HERDR_SESSION": "synthetic-session",
        }
    )
    return prompt_log


def test_explicit_and_session_stop_dispatch(root: Path) -> None:
    for name, arguments, payload in (
        ("explicit", ("--shotcaller", "Garen", "codex-stop-hook"), {}),
        (
            "session",
            ("codex-stop-hook",),
            {
                "session_id": SHOTCALLER_ID,
                "turn_id": "turn:real-session-dispatch",
                "hook_event_name": "Stop",
                "stop_hook_active": False,
                "last_assistant_message": "Attempting to finish.",
            },
        ),
    ):
        _, state, _ = seeded_state(root, name)
        env = _environment(root / name, state)
        status = _watcher(env, "--shotcaller", "Garen", "status")
        assert status == {
            "shotcaller": "Garen",
            "writer": "sqlite",
        }, status
        stop = _watcher(env, *arguments, payload=payload)
        assert stop["decision"] == "block"
        assert "unresolved obligations" in str(stop["reason"])


def test_supervise_wakes_and_stop_allows_after_settlement(root: Path) -> None:
    _, state, _ = seeded_state(root, "supervise")
    env = _environment(root / "supervise", state)
    _register_garen_runtime(state, "settlement", session_ref="session:current-garen")
    _fake_herdr(root / "supervise", env)
    first = _watcher(
        env,
        "codex-stop-hook",
        payload={
            "session_id": SHOTCALLER_ID,
            "turn_id": "turn:settlement-one",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        },
    )
    assert first["decision"] == "block"
    waiter = subprocess.Popen(
        [env["TEST_INSTALLED_WATCHER"], "--shotcaller", "Garen", "supervise", "--poll-seconds", "0.1"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    time.sleep(0.15)
    assert waiter.poll() is None, waiter.stderr.read()
    current = _league(state, "agent", "status", "--agent-id", CHAMPION_ID)
    version = current["result"]["agent"]["version"]
    settled = _league_env(
        state,
        env,
        "agent",
        "transition",
        "--agent-id",
        CHAMPION_ID,
        "--expected-version",
        str(version),
        "--status",
        "completed",
        "--update",
        "Synthetic installed-dispatch Champion settled.",
        "--at",
        AT2,
    )
    assert settled["result"]["delivery"]["state"] == "delivered", settled
    output, error = waiter.communicate(timeout=5)
    assert not error, error
    wake = json.loads(output)
    assert wake["event"] == "champions-idle"
    assert wake["writer"] == "sqlite"
    status = _watcher(env, "--shotcaller", "Garen", "status")
    assert status == {"shotcaller": "Garen", "writer": "sqlite"}
    allowed = _watcher(
        env,
        "codex-stop-hook",
        payload={
            "session_id": SHOTCALLER_ID,
            "turn_id": "turn:settlement-two",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        },
    )
    assert allowed == {}, allowed


def test_working_and_progress_tasks_remain_supervised(root: Path) -> None:
    _, state, _ = seeded_state(root, "working-task-supervision")
    with SQLiteStorage(state) as store:
        for task_state in ("working", "progress"):
            with store._transaction():
                store.connection.execute(
                    "UPDATE tasks SET state=? WHERE task_id=?", (task_state, TASK_ID)
                )
            counts = _obligation_counts(store, SHOTCALLER_ID)
            snapshot = _supervision_snapshot(
                store, "watcher:Garen", SHOTCALLER_ID
            )
            assert counts["active_champions"] >= 1
            assert any(
                row["agent_id"] == CHAMPION_ID for row in snapshot["champions"]
            )


def test_supervise_user_priority(root: Path) -> None:
    _, state, _ = seeded_state(root, "user-priority")
    env = _environment(root / "user-priority", state)
    _register_garen_runtime(
        state, "user-priority", session_ref="session:current-garen-priority"
    )
    waiter = subprocess.Popen(
        [env["TEST_INSTALLED_WATCHER"], "--shotcaller", "Garen", "supervise", "--poll-seconds", "0.1"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    time.sleep(0.15)
    assert waiter.poll() is None, waiter.stderr.read()
    assert _watcher(
        env,
        "codex-user-prompt-hook",
        payload={
            "session_id": SHOTCALLER_ID,
            "turn_id": "turn:user-priority",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Synthetic user-priority prompt.",
        },
    ) == {}
    output, error = waiter.communicate(timeout=5)
    assert not error, error
    assert json.loads(output) == {
        "event": "user-message",
        "priority": "user",
        "shotcaller": "Garen",
        "writer": "sqlite",
    }


def test_long_lived_supervisor_allows_concurrent_prompt_and_stop(root: Path) -> None:
    _, state, _ = seeded_state(root, "supervisor-hot-opens")
    env = _environment(root / "supervisor-hot-opens", state)
    _register_garen_runtime(
        state, "supervisor-hot-opens", session_ref=SHOTCALLER_ID
    )
    waiter = subprocess.Popen(
        [
            env["TEST_INSTALLED_WATCHER"],
            "--shotcaller",
            "Garen",
            "supervise",
            "--poll-seconds",
            "0.05",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    deadline = time.monotonic() + 3
    registered = False
    while time.monotonic() < deadline:
        with SQLiteStorage(
            state, busy_timeout_ms=100, request_wal=False
        ) as observer:
            registered = observer.connection.execute(
                "SELECT 1 FROM watcher_registrations WHERE actor_agent_id=?",
                (SHOTCALLER_ID,),
            ).fetchone() is not None
            assert observer.policy.journal_mode == "WAL"
        if registered:
            break
        time.sleep(0.02)
    assert registered and waiter.poll() is None

    prompt_payload = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:supervisor-hot-open",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Prompt captured while the foreground supervisor stays open.",
    }
    stop_payload = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:supervisor-hot-open",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    barrier = threading.Barrier(3)
    results: dict[str, tuple[subprocess.CompletedProcess[str], float]] = {}

    def invoke(name: str, command: str, payload: dict[str, object]) -> None:
        barrier.wait()
        started = time.monotonic()
        completed = subprocess.run(
            [env["TEST_INSTALLED_WATCHER"], command],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        results[name] = (completed, time.monotonic() - started)

    prompt_thread = threading.Thread(
        target=invoke,
        args=("prompt", "codex-user-prompt-hook", prompt_payload),
    )
    stop_thread = threading.Thread(
        target=invoke, args=("stop", "codex-stop-hook", stop_payload)
    )
    prompt_thread.start()
    stop_thread.start()
    barrier.wait()
    prompt_thread.join(timeout=5)
    stop_thread.join(timeout=5)
    assert not prompt_thread.is_alive() and not stop_thread.is_alive()

    prompt, prompt_elapsed = results["prompt"]
    stop, stop_elapsed = results["stop"]
    assert prompt.returncode == 0, prompt.stdout + prompt.stderr
    assert json.loads(prompt.stdout) == {}
    assert prompt_elapsed < 1.5
    assert stop.returncode == 0, stop.stdout + stop.stderr
    assert json.loads(stop.stdout)["decision"] == "block"
    assert stop_elapsed < 1.0
    output, error = waiter.communicate(timeout=5)
    assert not error, error
    assert json.loads(output)["priority"] == "user"

    with SQLiteStorage(state, request_wal=False) as store:
        rows = store.connection.execute(
            """
            SELECT p.source_event_key,pp.body_hash,pp.byte_count
              FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
             WHERE p.source_event_key=?
            """,
            (_hook_source_event_key("codex", prompt_payload),),
        ).fetchall()
        assert store.policy.journal_mode == "WAL"
    encoded = prompt_payload["prompt"].encode("utf-8")
    assert len(rows) == 1
    assert tuple(rows[0]) == (
        _hook_source_event_key("codex", prompt_payload),
        hashlib.sha256(encoded).hexdigest(),
        len(encoded),
    )


def test_codex_and_cursor_prompt_capture_exactly_once(root: Path) -> None:
    for name, command, event_field, payload in (
        (
            "codex-capture",
            "codex-user-prompt-hook",
            "turn_id",
            {
                "session_id": SHOTCALLER_ID,
                "turn_id": "turn:codex-capture",
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Complete local Codex prompt.\nSecond line.",
            },
        ),
        (
            "cursor-capture",
            "cursor-before-submit-hook",
            "generation_id",
            {
                "conversation_id": SHOTCALLER_ID,
                "generation_id": "generation:cursor-capture",
                "hook_event_name": "beforeSubmitPrompt",
                "prompt": "Complete local Cursor prompt.\nSecond line.",
            },
        ),
    ):
        _, state, _ = seeded_state(root, name)
        env = _environment(root / name, state)
        _register_garen_runtime(state, name, session_ref=SHOTCALLER_ID)
        assert _watcher(env, command, payload=payload) == {}
        assert _watcher(env, command, payload=payload) == {}
        _league(
            state,
            "storage",
            "export",
            "--purpose",
            "rollback",
            "--output-name",
            f"{name}.json",
        )
        exported = json.loads((state / f"{name}.json").read_text(encoding="utf-8"))
        adapter_kind = "codex" if command.startswith("codex-") else "cursor"
        prompts = [
            row
            for row in exported["tables"]["prompts"]
            if row["source_event_key"] == _hook_source_event_key(adapter_kind, payload)
        ]
        assert len(prompts) == 1
        rows = [
            row
            for row in exported["tables"]["prompt_payloads"]
            if row["prompt_id"] == prompts[0]["prompt_id"]
        ]
        encoded = payload["prompt"].encode("utf-8")
        assert len(rows) == 1
        assert rows[0]["body"] == payload["prompt"]
        assert rows[0]["body_hash"] == hashlib.sha256(encoded).hexdigest()
        assert rows[0]["byte_count"] == len(encoded)
        unresolved = _league(
            state,
            "request",
            "unresolved",
            "--owner-agent-id",
            SHOTCALLER_ID,
            "--before-action",
            "end",
        )["result"]
        assert unresolved["untriaged_prompt_count"] == 1
        assert unresolved["safe_to_finish"] is False
        pending_prompt = unresolved["untriaged_prompts"][0]
        assert pending_prompt["prompt_id"] == prompts[0]["prompt_id"]
        assert pending_prompt["body_hash"] == hashlib.sha256(encoded).hexdigest()
        request_id = f"request:{name}"
        triage = _league(
            state,
            "request",
            "triage",
            "--prompt-id",
            pending_prompt["prompt_id"],
            "--items-json",
            json.dumps(
                [
                    {
                        "prompt_item_id": f"item:{name}:1",
                        "ordinal": 1,
                        "summary": "Model-selected complete synthetic prompt item",
                        "disposition": "new_request",
                        "request_id": request_id,
                    }
                ],
                separators=(",", ":"),
            ),
            "--at",
            AT2,
        )["result"]
        assert triage["request_count"] == 1
        after_triage = _league(
            state,
            "request",
            "unresolved",
            "--owner-agent-id",
            SHOTCALLER_ID,
            "--before-action",
            "reply",
        )["result"]
        assert after_triage["untriaged_prompt_count"] == 0
        assert any(row["request_id"] == request_id for row in after_triage["requests"])
        stop = _watcher(
            env,
            "--shotcaller",
            "Garen",
            "codex-stop-hook",
            payload={
                "hook_event_name": "Stop",
                "session_id": SHOTCALLER_ID,
                "turn_id": f"turn:triaged-{name}",
                "stop_hook_active": False,
            },
        )
        assert stop["decision"] == "block"


def test_queued_prompts_reusing_turn_id_are_unique_and_conflicts_quarantine(
    root: Path,
) -> None:
    _, state, _ = seeded_state(root, "queued-prompt-identity")
    env = _environment(root / "queued-prompt-identity", state)
    garen_runtime = _register_garen_runtime(
        state, "queued-prompt-identity", session_ref=SHOTCALLER_ID
    )
    first = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:reused-by-queue",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "First queued ordinary user prompt.",
    }
    second = {
        **first,
        "prompt": "Second queued ordinary user prompt with distinct content.",
    }
    def generation() -> tuple[int, int]:
        with SQLiteStorage(state, request_wal=False) as store:
            return tuple(store.connection.execute(
                "SELECT user_message_generation,wait_generation FROM watcher_scopes WHERE actor_agent_id=?",
                (SHOTCALLER_ID,),
            ).fetchone())

    assert _watcher(env, "codex-user-prompt-hook", payload=first) == {}
    first_generation = generation()
    assert _watcher(env, "codex-user-prompt-hook", payload=first) == {}
    assert generation() == first_generation
    assert _watcher(env, "codex-user-prompt-hook", payload=second) == {}
    second_generation = generation()
    assert second_generation == (
        first_generation[0] + 1, first_generation[1] + 1
    )
    assert _watcher(env, "codex-user-prompt-hook", payload=second) == {}
    assert generation() == second_generation

    turn = subprocess.Popen(
        [
            str(LEAGUE),
            "--state-root",
            str(state),
            "request",
            "turn",
            "--owner-agent-id",
            SHOTCALLER_ID,
            "--at",
            AT2,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert turn.stdout is not None and turn.stdin is not None
    intake = json.loads(turn.stdout.readline())["result"]
    assert intake["returned_count"] == 2 and intake["truncated"] is False
    assert [row["body"] for row in intake["prompts"]] == [
        first["prompt"],
        second["prompt"],
    ]
    decisions = []
    for ordinal, _prompt in enumerate(intake["prompts"], start=1):
        decisions.append(
            {
                "items": [
                {
                    "summary": f"Semantic acknowledgement for queued prompt {ordinal}",
                    "disposition": "acknowledgement",
                }
                ],
            }
        )
    turn.stdin.write(
        json.dumps({"decisions": decisions, "plans": []}, separators=(",", ":"))
        + "\n"
    )
    turn.stdin.flush()
    committed = json.loads(turn.stdout.readline())["result"]
    assert committed["phase"] == "begun"
    assert committed["batch"]["prompt_count"] == 2
    assert not committed["batch"]["idempotent"]
    turn.stdin.write(json.dumps({"actions": []}, separators=(",", ":")) + "\n")
    turn.stdin.flush()
    completed = json.loads(turn.stdout.readline())["result"]
    assert turn.wait(timeout=10) == 0
    assert completed["phase"] == "committed"
    settled = _league(
        state,
        "request",
        "untriaged",
        "--owner-agent-id",
        SHOTCALLER_ID,
    )["result"]
    assert settled["untriaged_prompt_count"] == 0 and settled["prompts"] == []

    champion = {
        "session_id": CHAMPION_ID,
        "turn_id": first["turn_id"],
        "hook_event_name": "UserPromptSubmit",
        "prompt": first["prompt"],
    }
    assert _watcher(env, "codex-user-prompt-hook", payload=champion) == {}
    assert _watcher(env, "codex-user-prompt-hook", payload=champion) == {}
    expected_keys = {
        _hook_source_event_key("codex", first),
        _hook_source_event_key("codex", second),
        _hook_source_event_key("codex", champion),
    }
    with SQLiteStorage(state, request_wal=False) as store:
        captured = store.connection.execute(
            """
            SELECT p.source_event_key,p.intake_actor_id,pp.body
              FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
             WHERE pp.body IN (?,?,?)
             ORDER BY p.source_event_key
            """,
            (first["prompt"], second["prompt"], champion["prompt"]),
        ).fetchall()
        garen_generation = tuple(store.connection.execute(
            "SELECT user_message_generation,wait_generation FROM watcher_scopes WHERE actor_agent_id=?",
            (SHOTCALLER_ID,),
        ).fetchone())
    assert len(captured) == 3
    assert {row["source_event_key"] for row in captured} == expected_keys
    assert {row["intake_actor_id"] for row in captured} == {
        SHOTCALLER_ID, CHAMPION_ID
    }
    assert garen_generation == second_generation

    conflict = {
        **first,
        "turn_id": "turn:stale-owner-conflict",
        "prompt": "Queued prompt with stale conflicting ownership.",
    }
    source_key = _hook_source_event_key("codex", conflict)
    prompt_id = "prompt:" + source_key
    with SQLiteStorage(state, request_wal=False) as store:
        champion_runtime = str(store.connection.execute(
            "SELECT runtime_instance_id FROM runtime_instances WHERE actor_agent_id=?",
            (CHAMPION_ID,),
        ).fetchone()[0])
    encoded = conflict["prompt"].encode("utf-8")
    with SQLiteStorage(state, request_wal=False) as store:
        with store._transaction():
            store.connection.execute(
                """
                INSERT INTO prompts
                  (prompt_id,intake_actor_id,runtime_instance_id,adapter_kind,session_ref,
                   source_event_key,triage_state,triage_digest,created_at)
                VALUES(?,?,?,?,?,?,'untriaged',NULL,?)
                """,
                (
                    prompt_id, CHAMPION_ID, champion_runtime, "codex",
                    SHOTCALLER_ID, source_key, AT2,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO prompt_payloads
                  (prompt_id,body,body_hash,byte_count,pruned_at)
                VALUES(?,?,?,?,NULL)
                """,
                (
                    prompt_id,
                    conflict["prompt"],
                    hashlib.sha256(encoded).hexdigest(),
                    len(encoded),
                ),
            )
    assert _watcher(env, "codex-user-prompt-hook", payload=conflict) == {}
    with SQLiteStorage(state, request_wal=False) as store:
        quarantine = store.connection.execute(
            """
            SELECT state,reason,wake_actor_id,wake_scope_id,wake_committed
              FROM prompt_quarantine WHERE prompt_id=?
            """,
            (prompt_id,),
        ).fetchone()
        owner = store.connection.execute(
            "SELECT intake_actor_id,runtime_instance_id FROM prompts WHERE prompt_id=?",
            (prompt_id,),
        ).fetchone()
    assert tuple(quarantine) == (
        "quarantined", "runtime_unverified", None, None, 0
    )
    assert tuple(owner) == (CHAMPION_ID, champion_runtime)
    assert garen_runtime != champion_runtime


def test_missing_identity_quarantines_then_binds_and_triages(root: Path) -> None:
    _, state, _ = seeded_state(root, "missing-identity")
    env = _environment(root / "missing-identity", state)
    session = "session:missing-identity"
    payload = {
        "session_id": session,
        "turn_id": "turn:missing-identity",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Complete prompt retained before runtime identity exists.",
    }
    started = time.monotonic()
    assert _watcher(env, "codex-user-prompt-hook", payload=payload) == {}
    assert time.monotonic() - started < 1.0
    assert _watcher(env, "codex-user-prompt-hook", payload=payload) == {}
    exported_path = state / "missing-identity.json"
    _league(
        state,
        "storage",
        "export",
        "--purpose",
        "rollback",
        "--output-name",
        exported_path.name,
    )
    exported = json.loads(exported_path.read_text(encoding="utf-8"))
    quarantined = [
        row for row in exported["tables"]["prompt_quarantine"]
        if row["source_event_key"] == _hook_source_event_key("codex", payload)
    ]
    assert len(quarantined) == 1
    row = quarantined[0]
    encoded = payload["prompt"].encode("utf-8")
    assert row["state"] == "quarantined"
    assert row["reason"] == "runtime_unverified"
    assert row["body"] == payload["prompt"]
    assert row["body_hash"] == hashlib.sha256(encoded).hexdigest()
    assert row["byte_count"] == len(encoded)

    runtime_id = _register_garen_runtime(
        state, "later-binding", session_ref=session
    )
    bound = _league(
        state,
        "request",
        "bind-prompt",
        "--prompt-id",
        row["prompt_id"],
        "--intake-actor-id",
        SHOTCALLER_ID,
        "--runtime-instance-id",
        runtime_id,
        "--at",
        AT2,
    )["result"]
    assert bound["triage_state"] == "untriaged" and not bound["idempotent"]
    triaged = _league(
        state,
        "request",
        "triage",
        "--prompt-id",
        row["prompt_id"],
        "--items-json",
        json.dumps(
            [{
                "prompt_item_id": "item:missing-identity:1",
                "ordinal": 1,
                "summary": "Later-bound prompt acknowledged",
                "disposition": "acknowledgement",
                "request_id": None,
            }],
            separators=(",", ":"),
        ),
        "--at",
        AT2,
    )["result"]
    assert triaged["triage_state"] == "complete"


def test_unverified_champion_prompt_quarantines_without_shotcaller_wake(root: Path) -> None:
    _, state, _ = seeded_state(root, "champion-quarantine")
    env = _environment(root / "champion-quarantine", state)
    with SQLiteStorage(state, request_wal=False) as store:
        with store._transaction():
            store.connection.execute(
                "UPDATE agent_instances SET backend=NULL,address=NULL WHERE agent_id=?",
                (CHAMPION_ID,),
            )
    payload = {
        "session_id": CHAMPION_ID,
        "turn_id": "turn:champion-quarantine",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Exact Champion prompt must continue without a Shotcaller wake.",
    }
    assert _watcher(env, "codex-user-prompt-hook", payload=payload) == {}
    assert _watcher(env, "codex-user-prompt-hook", payload=payload) == {}
    with SQLiteStorage(state, request_wal=False) as store:
        rows = store.connection.execute(
            """
            SELECT state,reason,wake_actor_id,wake_scope_id,wake_committed
              FROM prompt_quarantine WHERE source_event_key=?
            """,
            (_hook_source_event_key("codex", payload),),
        ).fetchall()
        champion_scope = store.connection.execute(
            "SELECT user_message_generation,wait_generation FROM watcher_scopes WHERE actor_agent_id=?",
            (CHAMPION_ID,),
        ).fetchone()
    assert len(rows) == 1
    assert tuple(rows[0]) == ("quarantined", "runtime_unverified", None, None, 0)
    assert champion_scope is None

    runtime_id = _register_champion_runtime(
        state, "later-verified", CHAMPION_ID
    )
    assert _watcher(env, "codex-user-prompt-hook", payload=payload) == {}
    with SQLiteStorage(state, request_wal=False) as store:
        bound = store.connection.execute(
            """
            SELECT q.state,q.bound_actor_id,q.bound_runtime_instance_id,
                   q.wake_actor_id,q.wake_scope_id,q.wake_committed,
                   p.runtime_instance_id
              FROM prompt_quarantine q JOIN prompts p ON p.prompt_id=q.prompt_id
             WHERE q.source_event_key=?
            """,
            (_hook_source_event_key("codex", payload),),
        ).fetchone()
        champion_scope = store.connection.execute(
            "SELECT COUNT(*) FROM watcher_scopes WHERE actor_agent_id=?",
            (CHAMPION_ID,),
        ).fetchone()[0]
    assert tuple(bound) == (
        "bound", CHAMPION_ID, runtime_id, None, None, 0, runtime_id
    )
    assert champion_scope == 0


def test_verified_champion_prompt_captures_without_shotcaller_wake(root: Path) -> None:
    _, state, _ = seeded_state(root, "verified-champion-capture")
    env = _environment(root / "verified-champion-capture", state)
    runtime_digest = hashlib.sha256(
        f"codex\0{CHAMPION_ID}\0herdr\0w1:p2".encode()
    ).hexdigest()
    runtime_id = f"runtime:hook:{runtime_digest}"
    payload = {
        "session_id": CHAMPION_ID,
        "turn_id": "turn:verified-champion-capture",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Exact verified Champion prompt is retained without a wake.",
    }
    with SQLiteStorage(state, request_wal=False) as store:
        garen_before = store.connection.execute(
            "SELECT COUNT(*) FROM watcher_scopes WHERE actor_agent_id=?",
            (SHOTCALLER_ID,),
        ).fetchone()[0]
    assert _watcher(env, "codex-user-prompt-hook", payload=payload) == {}
    assert _watcher(env, "codex-user-prompt-hook", payload=payload) == {}
    with SQLiteStorage(state, request_wal=False) as store:
        prompts = store.connection.execute(
            """
            SELECT p.intake_actor_id,p.runtime_instance_id,p.triage_state,
                   pp.body,pp.body_hash,pp.byte_count
              FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
             WHERE p.source_event_key=?
            """,
            (_hook_source_event_key("codex", payload),),
        ).fetchall()
        quarantined = store.connection.execute(
            "SELECT COUNT(*) FROM prompt_quarantine WHERE source_event_key=?",
            (_hook_source_event_key("codex", payload),),
        ).fetchone()[0]
        runtime = store.connection.execute(
            """
            SELECT actor_agent_id,session_ref,status,verified,capabilities_json
              FROM runtime_instances WHERE runtime_instance_id=?
            """,
            (runtime_id,),
        ).fetchone()
        champion_scope = store.connection.execute(
            "SELECT COUNT(*) FROM watcher_scopes WHERE actor_agent_id=?",
            (CHAMPION_ID,),
        ).fetchone()[0]
        garen_after = store.connection.execute(
            "SELECT COUNT(*) FROM watcher_scopes WHERE actor_agent_id=?",
            (SHOTCALLER_ID,),
        ).fetchone()[0]
    encoded = payload["prompt"].encode("utf-8")
    assert len(prompts) == 1
    assert tuple(prompts[0]) == (
        CHAMPION_ID,
        runtime_id,
        "untriaged",
        payload["prompt"],
        hashlib.sha256(encoded).hexdigest(),
        len(encoded),
    )
    assert quarantined == 0
    assert tuple(runtime[:4]) == (CHAMPION_ID, CHAMPION_ID, "active", 1)
    assert json.loads(runtime["capabilities_json"]) == ["prompt.capture"]
    assert champion_scope == 0
    assert garen_after == garen_before


def test_verified_runtime_session_routes_stop_and_pointer_state(root: Path) -> None:
    _, state, _ = seeded_state(root, "runtime-session-routing")
    derived = root / "runtime-session-routing" / "league"
    state.rename(derived)
    env = _pointer_environment(root / "runtime-session-routing")
    session = "session:verified-current-garen"
    _register_garen_runtime(derived, "verified-current-garen", session_ref=session)
    assert _watcher(env, "--shotcaller", "Garen", "status") == {
        "shotcaller": "Garen",
        "writer": "sqlite",
    }
    payload = {
        "session_id": session,
        "turn_id": "turn:verified-current-garen",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    assert _watcher(env, "codex-stop-hook", payload=payload)["decision"] == "block"
    assert _watcher(env, "codex-stop-hook", payload=payload) == {}


def test_quarantined_prompt_rearms_one_shot_stop(root: Path) -> None:
    _, state, _ = seeded_state(root, "quarantine-stop-continuity")
    env = _environment(root / "quarantine-stop-continuity", state)
    with SQLiteStorage(state, request_wal=False) as store:
        with store._transaction():
            store.connection.execute(
                "UPDATE agent_instances SET backend=NULL,address=NULL WHERE agent_id=?",
                (SHOTCALLER_ID,),
            )
    first_generation = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:stop-generation-one",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    first = _watcher(
        env, "--shotcaller", "Garen", "codex-stop-hook", payload=first_generation
    )
    assert first["decision"] == "block"
    assert _watcher(
        env, "--shotcaller", "Garen", "codex-stop-hook", payload=first_generation
    ) == {}
    with SQLiteStorage(state, request_wal=False) as store:
        before = store.connection.execute(
            "SELECT user_message_generation,wait_generation FROM watcher_scopes WHERE actor_agent_id=?",
            (SHOTCALLER_ID,),
        ).fetchone()
        before = tuple(before)
    prompt = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:quarantined-rearm",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Quarantined prompt must atomically rearm Stop.",
    }
    assert _watcher(env, "codex-user-prompt-hook", payload=prompt) == {}
    assert _watcher(env, "codex-user-prompt-hook", payload=prompt) == {}
    with SQLiteStorage(state, request_wal=False) as store:
        after = store.connection.execute(
            "SELECT user_message_generation,wait_generation FROM watcher_scopes WHERE actor_agent_id=?",
            (SHOTCALLER_ID,),
        ).fetchone()
        quarantine = store.connection.execute(
            "SELECT state,reason,wake_actor_id,wake_scope_id,wake_committed FROM prompt_quarantine WHERE source_event_key=?",
            (_hook_source_event_key("codex", prompt),),
        ).fetchone()
    assert tuple(after) == (before[0] + 1, before[1] + 1)
    assert tuple(quarantine) == (
        "quarantined",
        "runtime_unverified",
        SHOTCALLER_ID,
        "watcher:Garen",
        1,
    )
    second_generation = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:stop-generation-two",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    again = _watcher(
        env, "--shotcaller", "Garen", "codex-stop-hook", payload=second_generation
    )
    assert again["decision"] == "block"


def test_real_codex_stop_payload_blocks_once_per_turn(root: Path) -> None:
    _, state, _ = seeded_state(root, "real-codex-stop-generation")
    env = _environment(root / "real-codex-stop-generation", state)
    first = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:owner-visible-one",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "First end attempt.",
    }
    blocked = _watcher(env, "codex-stop-hook", payload=first)
    assert blocked["decision"] == "block"
    assert "turn:owner-visible-one" in str(blocked["reason"])

    retry = {
        **first,
        "stop_hook_active": True,
        "last_assistant_message": "Continuation end attempt.",
    }
    assert _watcher(env, "codex-stop-hook", payload=retry) == {}

    # A new Codex turn is a fresh terminal generation even if prompt intake was
    # temporarily unavailable and therefore did not increment wait_generation.
    next_turn = {
        **first,
        "turn_id": "turn:owner-visible-two",
        "last_assistant_message": "Next real user turn end attempt.",
    }
    next_block = _watcher(env, "codex-stop-hook", payload=next_turn)
    assert next_block["decision"] == "block"
    assert "turn:owner-visible-two" in str(next_block["reason"])


def test_transition_contention_keeps_stop_safe_and_prompt_durable(root: Path) -> None:
    _, state, _ = seeded_state(root, "stop-contention")
    env = _environment(root / "stop-contention", state)
    _register_garen_runtime(state, "stop-contention", session_ref=SHOTCALLER_ID)
    holder = SQLiteStorage(state, busy_timeout_ms=1000, request_wal=False)
    entered = threading.Event()
    release = threading.Event()
    transition_errors: list[BaseException] = []
    results: dict[str, tuple[subprocess.CompletedProcess[str], float]] = {}

    def hold(point: str) -> None:
        if point == "after_event_insert":
            entered.set()
            assert release.wait(timeout=5)

    def transition() -> None:
        try:
            holder.transition(
                CHAMPION_ID,
                2,
                "blocked",
                "Synthetic transition holds the exact writer reservation.",
                "2026-01-01T00:02:00Z",
                fault=hold,
            )
        except BaseException as exc:
            transition_errors.append(exc)

    stop_payload = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:transition-contention",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    prompt_payload = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:transition-contention",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Ordinary prompt submitted while a transition commits.",
    }

    def invoke(name: str, command: str, payload: dict[str, object]) -> None:
        started = time.monotonic()
        completed = subprocess.run(
            [env["TEST_INSTALLED_WATCHER"], command],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        results[name] = (completed, time.monotonic() - started)

    transition_thread = threading.Thread(target=transition)
    transition_thread.start()
    assert entered.wait(timeout=5)
    stop_thread = threading.Thread(
        target=invoke, args=("stop", "codex-stop-hook", stop_payload)
    )
    stop_thread.start()
    prompt_thread = threading.Thread(
        target=invoke,
        args=("prompt", "codex-user-prompt-hook", prompt_payload),
    )
    prompt_thread.start()
    stop_thread.join(timeout=1.5)
    assert not stop_thread.is_alive()
    release.set()
    transition_thread.join(timeout=5)
    prompt_thread.join(timeout=5)
    assert not transition_thread.is_alive()
    assert not stop_thread.is_alive()
    assert not prompt_thread.is_alive()
    assert not transition_errors

    stop, stop_elapsed = results["stop"]
    prompt, prompt_elapsed = results["prompt"]
    assert stop.returncode == 0, stop.stdout + stop.stderr
    stop_result = json.loads(stop.stdout)
    assert stop_result["decision"] == "block"
    assert "retry" in stop_result["reason"].lower()
    assert stop_elapsed < 1.0
    assert prompt.returncode == 0, prompt.stdout + prompt.stderr
    assert json.loads(prompt.stdout) == {}
    assert prompt_elapsed < 1.5

    retry = _watcher(env, "codex-stop-hook", payload=stop_payload)
    assert retry["decision"] == "block"
    with SQLiteStorage(state, request_wal=False) as store:
        champion = store.agent_status(CHAMPION_ID)
        prompt_rows = store.connection.execute(
            """
            SELECT p.source_event_key,pp.body_hash,pp.byte_count
              FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
             WHERE p.source_event_key=?
            """,
            (_hook_source_event_key("codex", prompt_payload),),
        ).fetchall()
        obligations = _obligation_counts(store, SHOTCALLER_ID)
    holder.close()
    assert champion is not None and champion["version"] == 3
    assert champion["status"] == "blocked"
    assert len(prompt_rows) == 1
    encoded = prompt_payload["prompt"].encode("utf-8")
    assert tuple(prompt_rows[0]) == (
        _hook_source_event_key("codex", prompt_payload),
        hashlib.sha256(encoded).hexdigest(),
        len(encoded),
    )
    assert obligations["active_champions"] >= 1
    assert obligations["unresolved_requests"] >= 1
    assert obligations["pending_deliveries"] >= 1


def test_codex_stop_rejects_incomplete_real_payload(root: Path) -> None:
    _, state, _ = seeded_state(root, "invalid-real-codex-stop")
    env = _environment(root / "invalid-real-codex-stop", state)
    result = subprocess.run(
        [env["TEST_INSTALLED_WATCHER"], "codex-stop-hook"],
        input=json.dumps(
            {
                "session_id": SHOTCALLER_ID,
                "hook_event_name": "Stop",
                "stop_hook_active": False,
            }
        ),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 2
    assert "stop_hook_invalid" in result.stderr


def test_material_delivery_watcher_direct_dedup_and_unavailable(root: Path) -> None:
    _, watcher_state, _ = seeded_state(root, "watcher-delivery")
    watcher_env = _environment(root / "watcher-delivery", watcher_state)
    _register_garen_runtime(
        watcher_state, "watcher", session_ref="session:current-watcher"
    )
    waiter = subprocess.Popen(
        [watcher_env["TEST_INSTALLED_WATCHER"], "--shotcaller", "Garen", "supervise", "--poll-seconds", "0.1"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=watcher_env,
    )
    deadline = time.monotonic() + 3
    registered = False
    while time.monotonic() < deadline:
        with SQLiteStorage(watcher_state) as observer:
            registered = observer.connection.execute(
                "SELECT 1 FROM watcher_registrations WHERE actor_agent_id=?",
                (SHOTCALLER_ID,),
            ).fetchone() is not None
        if registered:
            break
        time.sleep(0.02)
    assert registered and waiter.poll() is None
    current = _league(watcher_state, "agent", "status", "--agent-id", CHAMPION_ID)
    version = current["result"]["agent"]["version"]
    transitioned = _league(
        watcher_state,
        "agent",
        "transition",
        "--agent-id",
        CHAMPION_ID,
        "--expected-version",
        str(version),
        "--status",
        "blocked",
        "--update",
        "Synthetic active-watcher delivery.",
        "--at",
        "2026-01-01T00:02:00Z",
    )
    assert transitioned["result"]["delivery"]["state"] == "delivered"
    output, error = waiter.communicate(timeout=5)
    assert not error, error
    assert json.loads(output)["event"] == "champion-update"

    _, direct_state, _ = seeded_state(root, "direct-delivery")
    direct_env = _environment(root / "direct-delivery", direct_state)
    _register_garen_runtime(direct_state, "direct")
    prompt_log = _fake_herdr(root / "direct-delivery", direct_env)
    current = _league_env(
        direct_state, direct_env, "agent", "status", "--agent-id", CHAMPION_ID
    )
    version = current["result"]["agent"]["version"]
    direct = _league_env(
        direct_state,
        direct_env,
        "agent",
        "transition",
        "--agent-id",
        CHAMPION_ID,
        "--expected-version",
        str(version),
        "--status",
        "ready_to_land",
        "--update",
        "Synthetic direct fallback delivery.",
        "--at",
        "2026-01-01T00:02:00Z",
    )
    event_id = direct["result"]["event_id"]
    assert direct["result"]["delivery"]["state"] == "delivered"
    assert len(prompt_log.read_text(encoding="utf-8").splitlines()) == 1
    retry = _watcher(
        direct_env,
        "--shotcaller",
        "Garen",
        "deliver",
        "--event-id",
        event_id,
    )
    assert retry["state"] == "delivered" and retry["idempotent"] is True
    assert len(prompt_log.read_text(encoding="utf-8").splitlines()) == 1

    _, unavailable_state, _ = seeded_state(root, "unavailable-delivery")
    unavailable_env = _environment(root / "unavailable-delivery", unavailable_state)
    _register_garen_runtime(unavailable_state, "unavailable")
    unavailable_bin = root / "unavailable-delivery" / "unavailable-bin"
    unavailable_bin.mkdir()
    unavailable_herdr = unavailable_bin / "herdr"
    unavailable_herdr.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    unavailable_herdr.chmod(0o755)
    unavailable_env["PATH"] = f"{unavailable_bin}:{unavailable_env['PATH']}"
    current = _league_env(
        unavailable_state,
        unavailable_env,
        "agent",
        "status",
        "--agent-id",
        CHAMPION_ID,
    )
    version = current["result"]["agent"]["version"]
    unavailable = _league_env(
        unavailable_state,
        unavailable_env,
        "agent",
        "transition",
        "--agent-id",
        CHAMPION_ID,
        "--expected-version",
        str(version),
        "--status",
        "blocked",
        "--update",
        "Synthetic unavailable fallback.",
        "--at",
        "2026-01-01T00:02:00Z",
    )
    assert unavailable["result"]["delivery"]["state"] == "pending"
    backlog = _league_env(
        unavailable_state,
        unavailable_env,
        "delivery",
        "backlog",
        "--at",
        "2026-01-01T00:03:00Z",
    )
    assert any(
        row["event_id"] == unavailable["result"]["event_id"]
        for row in backlog["result"]["rows"]
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-canonical-watcher-") as temporary:
        root = Path(temporary)
        test_explicit_and_session_stop_dispatch(root)
        test_supervise_wakes_and_stop_allows_after_settlement(root)
        test_working_and_progress_tasks_remain_supervised(root)
        test_supervise_user_priority(root)
        test_long_lived_supervisor_allows_concurrent_prompt_and_stop(root)
        test_codex_and_cursor_prompt_capture_exactly_once(root)
        test_queued_prompts_reusing_turn_id_are_unique_and_conflicts_quarantine(root)
        test_missing_identity_quarantines_then_binds_and_triages(root)
        test_unverified_champion_prompt_quarantines_without_shotcaller_wake(root)
        test_verified_champion_prompt_captures_without_shotcaller_wake(root)
        test_verified_runtime_session_routes_stop_and_pointer_state(root)
        test_quarantined_prompt_rearms_one_shot_stop(root)
        test_real_codex_stop_payload_blocks_once_per_turn(root)
        test_transition_contention_keeps_stop_safe_and_prompt_durable(root)
        test_codex_stop_rejects_incomplete_real_payload(root)
        test_material_delivery_watcher_direct_dedup_and_unavailable(root)
    print("PASS: installed SQLite Stop/supervise plus watcher/direct exact-once delivery and pending fallback")


if __name__ == "__main__":
    main()
