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
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from storage_fixture import AT2, CHAMPION_ID, SHOTCALLER_ID, TASK_ID  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402
from lifecycle_fakes import FakeIds, FakeLaunchAdapter, issue_bound_spec  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME_TWO,
    LUX_ID,
    capture_p100,
    create_context,
    dispatch_request,
)
from league.request_services import AssignmentService, AssignmentSpec  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.sqlite_watcher_ops import obligation_counts  # noqa: E402
from league.storage import RuntimeRegistrationCommand  # noqa: E402
from league.canonical_watcher import (  # noqa: E402
    _capture_prompt,
    _codex_stop_reason,
    _notify_direct_user_priority,
    _prompt_identity,
    _supervision_snapshot,
    handle_brokered_hook,
)


WATCHER = ROOT / "bin/agent-watcher"
LEAGUE = ROOT / "bin/league"
MAX_HOOK_LAUNCH_SECONDS = 2.0


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
    env: dict[str, str], *arguments: str, payload: dict[str, object] | None = None
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


def _stop_payload(
    adapter_kind: str, session_ref: str, generation: str
) -> dict[str, object]:
    if adapter_kind == "codex":
        return {
            "hook_event_name": "Stop",
            "session_id": session_ref,
            "turn_id": generation,
            "stop_hook_active": True,
        }
    if adapter_kind == "cursor":
        return {
            "hook_event_name": "stop",
            "conversation_id": session_ref,
            "generation_id": generation,
        }
    return {
        "hook_event_name": "PiStop",
        "session_path": session_ref,
        "input_id": generation,
    }


def _stop_mutation_snapshot(state: Path) -> str:
    """Digest every canonical row so an unbound hook cannot hide a mutation."""

    with SQLiteStorage(state) as store:
        tables = [
            str(row["name"])
            for row in store.connection.execute(
                """
                SELECT name FROM sqlite_master
                 WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name
                """
            ).fetchall()
        ]
        digest = hashlib.sha256()
        for table in tables:
            digest.update(table.encode("utf-8"))
            for row in store.connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall():
                digest.update(repr(tuple(row)).encode("utf-8"))
        return digest.hexdigest()


def test_unbound_provider_stops_allow_without_mutation_when_broker_is_absent(
    root: Path,
) -> None:
    for adapter_kind, command in (
        ("codex", "codex-stop-hook"),
        ("cursor", "cursor-stop-hook"),
        ("pi", "pi-stop-hook"),
    ):
        _, state, _ = seeded_state(root, f"unbound-stop-{adapter_kind}")
        env = _environment(root / f"unbound-stop-{adapter_kind}", state)
        before = _stop_mutation_snapshot(state)
        payload = _stop_payload(
            adapter_kind,
            f"unbound:{adapter_kind}:session",
            f"unbound:{adapter_kind}:generation",
        )
        assert _watcher(env, command, payload=payload) == {}
        assert _watcher(env, command, payload=payload) == {}
        assert _stop_mutation_snapshot(state) == before


def test_bound_shotcallers_fail_closed_and_champion_gate_survives_absent_broker(
    root: Path,
) -> None:
    for adapter_kind, command in (
        ("codex", "codex-stop-hook"),
        ("cursor", "cursor-stop-hook"),
        ("pi", "pi-stop-hook"),
    ):
        _, state, _ = seeded_state(root, f"bound-stop-{adapter_kind}")
        env = _environment(root / f"bound-stop-{adapter_kind}", state)
        session_ref = f"bound:{adapter_kind}:shotcaller"
        _register_garen_runtime(
            state,
            adapter_kind,
            session_ref=session_ref,
            harness_kind=f"{adapter_kind}-thread",
        )
        with SQLiteStorage(state) as store, store._transaction():
            from league.sqlite_watcher_ops import ensure_watcher_scope

            ensure_watcher_scope(
                store, "watcher:Garen", SHOTCALLER_ID, block_on_obligations=None
            )
            row = store.connection.execute(
                "SELECT metadata_json FROM watcher_scopes WHERE scope_id='watcher:Garen'"
            ).fetchone()
            metadata = json.loads(row["metadata_json"])
            metadata.setdefault("supervision", {})["service_owner"] = "persistent"
            store.connection.execute(
                "UPDATE watcher_scopes SET metadata_json=? WHERE scope_id='watcher:Garen'",
                (json.dumps(metadata, sort_keys=True, separators=(",", ":")),),
            )
        payload = _stop_payload(adapter_kind, session_ref, "bound:generation")
        first = _watcher(env, command, payload=payload)
        repeated = _watcher(env, command, payload=payload)
        assert first == repeated
        assert "supervisor_unavailable" in str(first)
        with SQLiteStorage(state) as store:
            scope = store.connection.execute(
                "SELECT last_terminal_generation FROM watcher_scopes WHERE scope_id='watcher:Garen'"
            ).fetchone()
            assert scope["last_terminal_generation"] is None
            rearmed = store.rearm_wait(
                "watcher:Garen", SHOTCALLER_ID, f"event:rearm:{adapter_kind}", AT2
            )
        next_payload = _stop_payload(
            adapter_kind, session_ref, "bound:generation:rearmed"
        )
        next_first = _watcher(env, command, payload=next_payload)
        next_repeated = _watcher(env, command, payload=next_payload)
        assert next_first == next_repeated
        assert "supervisor_unavailable" in str(next_first)
        with SQLiteStorage(state) as store:
            scope = store.connection.execute(
                "SELECT wait_generation,last_terminal_generation FROM watcher_scopes "
                "WHERE scope_id='watcher:Garen'"
            ).fetchone()
            assert scope["wait_generation"] == rearmed["wait_generation"]
            assert scope["last_terminal_generation"] is None

    for adapter_kind, command in (
        ("codex", "codex-stop-hook"),
        ("cursor", "cursor-stop-hook"),
        ("pi", "pi-stop-hook"),
    ):
        _, state, _ = seeded_state(root, f"bound-stop-champion-{adapter_kind}")
        env = _environment(root / f"bound-stop-champion-{adapter_kind}", state)
        champion_session = f"bound:{adapter_kind}:champion"
        _register_champion_runtime(
            state,
            f"absent-broker-{adapter_kind}",
            champion_session,
            harness_kind=f"{adapter_kind}-thread",
        )
        payload = _stop_payload(
            adapter_kind, champion_session, "champion:generation"
        )
        assert _watcher(env, command, payload=payload)
        assert _watcher(env, command, payload=payload) == {}
        with SQLiteStorage(state) as store:
            owner_scope = store.resolve_supervisor_scope(SHOTCALLER_ID)
            store.rearm_wait(
                str(owner_scope["scope_id"]),
                SHOTCALLER_ID,
                f"event:champion-rearm:{adapter_kind}",
                AT2,
            )
        rearmed = _stop_payload(
            adapter_kind, champion_session, "champion:generation:rearmed"
        )
        assert _watcher(env, command, payload=rearmed)
        assert _watcher(env, command, payload=rearmed) == {}


def test_stop_reason_uses_resolved_callsign_not_provider_turn_identity() -> None:
    provider_turn = "11111111-2222-4333-8444-555555555555"
    reason = _codex_stop_reason("Ashe", 4)
    assert reason == "League has unresolved obligations for Ashe at wait generation 4."
    assert provider_turn not in reason


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


def _wait_for_watcher_registration(
    state: Path, waiter: subprocess.Popen[str], *, timeout: float = 3
) -> None:
    deadline = time.monotonic() + timeout
    with SQLiteStorage(state, busy_timeout_ms=100, request_wal=False) as observer:
        while time.monotonic() < deadline:
            if waiter.poll() is not None:
                output, error = waiter.communicate()
                raise AssertionError(
                    f"watcher exited before canonical registration: {output}{error}"
                )
            registered = observer.connection.execute(
                "SELECT 1 FROM watcher_registrations WHERE actor_agent_id=?",
                (SHOTCALLER_ID,),
            ).fetchone() is not None
            if registered:
                assert waiter.poll() is None
                return
            time.sleep(0.02)
    waiter.terminate()
    try:
        output, error = waiter.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        waiter.kill()
        output, error = waiter.communicate(timeout=1)
    raise AssertionError(
        f"watcher registration did not become ready within {timeout}s: {output}{error}"
    )


def _register_garen_runtime(
    state: Path,
    suffix: str,
    *,
    session_ref: str | None = None,
    harness_kind: str = "codex-thread",
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
        harness_kind,
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


def _register_champion_runtime(
    state: Path,
    suffix: str,
    session_ref: str,
    *,
    harness_kind: str = "codex-thread",
) -> str:
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
        harness_kind,
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


def test_codex_stop_reason_uses_resolved_callsign_not_turn_uuid() -> None:
    turn_id = "01a0503e-1b77-7c21-be2b-1cf6d52cf047"
    reason = _codex_stop_reason("Ashe", 4)
    assert reason == "League has unresolved obligations for Ashe at wait generation 4."
    assert turn_id not in reason


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
    _wait_for_watcher_registration(state, waiter)
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
    assert wake["event"] == "champion-update"
    assert wake["event_id"] == settled["result"]["event_id"]
    assert wake["status"] == "completed"
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
            counts = obligation_counts(store, SHOTCALLER_ID)
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
    _wait_for_watcher_registration(state, waiter)

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
    assert prompt_elapsed < MAX_HOOK_LAUNCH_SECONDS
    assert stop.returncode == 0, stop.stdout + stop.stderr
    assert json.loads(stop.stdout)["decision"] == "block"
    assert stop_elapsed < MAX_HOOK_LAUNCH_SECONDS
    output, error = waiter.communicate(timeout=5)
    assert not error, error
    assert json.loads(output)["priority"] == "user"

    with SQLiteStorage(state, request_wal=False) as store:
        rows = store.connection.execute(
            """
            SELECT p.source_event_key,pp.body_hash,pp.byte_count
              FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
             WHERE p.adapter_kind='codex' AND p.session_ref=? AND pp.body=?
            """,
            (SHOTCALLER_ID, prompt_payload["prompt"]),
        ).fetchall()
        assert store.policy.journal_mode == "WAL"
    encoded = prompt_payload["prompt"].encode("utf-8")
    assert len(rows) == 1
    assert rows[0]["source_event_key"].startswith("hook:")
    assert tuple(rows[0])[1:] == (
        hashlib.sha256(encoded).hexdigest(), len(encoded)
    )


def test_provider_prompt_capture_identity_contracts(root: Path) -> None:
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
        (
            "pi-capture",
            "pi-input-hook",
            "input_id",
            {
                "session_id": "33333333-3333-4333-8333-333333333333",
                "session_path": SHOTCALLER_ID,
                "input_id": "input:pi-capture",
                "hook_event_name": "PiInput",
                "prompt": "Complete local Pi prompt.\nSecond line.",
            },
        ),
    ):
        _, state, _ = seeded_state(root, name)
        env = _environment(root / name, state)
        adapter_kind = command.split("-", 1)[0]
        _register_garen_runtime(
            state,
            name,
            session_ref=SHOTCALLER_ID,
            harness_kind=f"{adapter_kind}-thread",
        )
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
        payload_rows = {
            row["prompt_id"]: row
            for row in exported["tables"]["prompt_payloads"]
            if row["body"] == payload["prompt"]
        }
        prompts = [
            row
            for row in exported["tables"]["prompts"]
            if row["adapter_kind"] == adapter_kind
            and row["session_ref"] == SHOTCALLER_ID
            and row["prompt_id"] in payload_rows
        ]
        expected_count = 2 if adapter_kind == "codex" else 1
        assert len(prompts) == expected_count
        rows = [payload_rows[row["prompt_id"]] for row in prompts]
        encoded = payload["prompt"].encode("utf-8")
        assert len(rows) == expected_count
        assert all(row["body"] == payload["prompt"] for row in rows)
        assert all(
            row["body_hash"] == hashlib.sha256(encoded).hexdigest() for row in rows
        )
        assert all(row["byte_count"] == len(encoded) for row in rows)


        if adapter_kind == "codex":
            assert len({row["source_event_key"] for row in prompts}) == 2
        unresolved = _league(
            state,
            "request",
            "unresolved",
            "--owner-agent-id",
            SHOTCALLER_ID,
            "--before-action",
            "end",
        )["result"]
        assert unresolved["untriaged_prompt_count"] == expected_count
        assert unresolved["safe_to_finish"] is False
        request_id = f"request:{name}"
        for ordinal, pending_prompt in enumerate(
            unresolved["untriaged_prompts"], start=1
        ):
            assert pending_prompt["body_hash"] == hashlib.sha256(encoded).hexdigest()
            is_first = ordinal == 1
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
                            "prompt_item_id": f"item:{name}:{ordinal}",
                            "ordinal": 1,
                            "summary": "Model-selected complete synthetic prompt item",
                            "disposition": "new_request" if is_first else "acknowledgement",
                            "request_id": request_id if is_first else None,
                        }
                    ],
                    separators=(",", ":"),
                ),
                "--at",
                AT2,
            )["result"]
            assert triage["request_count"] == (1 if is_first else 0)
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


def test_provider_pre_tool_policy_and_pi_stop_are_shared_and_fail_closed(
    root: Path,
) -> None:
    cases = (
        (
            "codex", "codex-pre-tool-hook", "codex-stop-hook",
            "33333333-3333-4333-8333-333333333333",
            {
                "session_id": "33333333-3333-4333-8333-333333333333",
                "turn_id": "turn:provider-hooks", "hook_event_name": "PreToolUse",
            },
            {
                "session_id": "33333333-3333-4333-8333-333333333333",
                "turn_id": "turn:provider-hooks", "hook_event_name": "Stop",
                "stop_hook_active": True,
            },
        ),
        (
            "cursor", "cursor-pre-tool-hook", "cursor-stop-hook",
            "44444444-4444-4444-8444-444444444444",
            {
                "conversation_id": "44444444-4444-4444-8444-444444444444",
                "generation_id": "generation:provider-hooks",
                "hook_event_name": "beforeShellExecution",
            },
            {
                "conversation_id": "44444444-4444-4444-8444-444444444444",
                "generation_id": "generation:provider-hooks", "hook_event_name": "stop",
            },
        ),
        (
            "pi", "pi-pre-tool-hook", "pi-stop-hook",
            str(root / "provider-hooks-pi" / "session.jsonl"),
            {
                "session_path": str(root / "provider-hooks-pi" / "session.jsonl"),
                "input_id": "input:provider-hooks", "hook_event_name": "PiToolCall",
            },
            {
                "session_path": str(root / "provider-hooks-pi" / "session.jsonl"),
                "input_id": "input:provider-hooks", "hook_event_name": "PiStop",
            },
        ),
    )
    for kind, pretool_command, stop_command, session_ref, pretool, stop in cases:
        label = f"provider-hooks-{kind}"
        _, state, _ = seeded_state(root, label)
        env = _environment(root / label, state)
        _register_garen_runtime(
            state, label, session_ref=session_ref, harness_kind=f"{kind}-thread"
        )
        accepted = _watcher(
            env, pretool_command, payload={**pretool, "authorized": True}
        )
        refused = _watcher(
            env, pretool_command, payload={**pretool, "authorized": False}
        )
        assert accepted == {"decision": "accept", "reason_code": "policy_accepted"}
        assert refused == {"decision": "refuse", "reason_code": "tool_not_authorized"}
        stopped = _watcher(env, stop_command, payload=stop)
        if kind == "codex":
            assert stopped["decision"] == "block"
        else:
            assert "unresolved obligations" in str(stopped["followup_message"])


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
    assert _watcher(env, "codex-user-prompt-hook", payload=second) == {}
    second_generation = generation()
    assert second_generation == (
        first_generation[0] + 1, first_generation[1] + 1
    )

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
        json.dumps(
            {
                "candidate_inventory_digest": intake["candidate_inventory"]["digest"],
                "decisions": decisions,
                "plans": [],
            },
            separators=(",", ":"),
        )
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
    assert len({row["source_event_key"] for row in captured}) == 3
    assert all(row["source_event_key"].startswith("hook:") for row in captured)
    assert {row["intake_actor_id"] for row in captured} == {
        SHOTCALLER_ID, CHAMPION_ID
    }
    assert garen_generation == second_generation

    conflict = {
        **first,
        "turn_id": "turn:stale-owner-conflict",
        "prompt": "Queued prompt with stale conflicting ownership.",
    }
    capture_event_id = "codex-user-prompt:" + "a" * 32
    prompt_id, source_key = _prompt_identity(
        "codex",
        SHOTCALLER_ID,
        f"{conflict['turn_id']}\0{capture_event_id}",
        conflict["prompt"],
    )
    encoded = conflict["prompt"].encode("utf-8")
    with SQLiteStorage(state, request_wal=False) as store:
        champion_runtime = str(store.connection.execute(
            "SELECT runtime_instance_id FROM runtime_instances WHERE actor_agent_id=?",
            (CHAMPION_ID,),
        ).fetchone()[0])
        before_conflict = tuple(store.connection.execute(
            "SELECT user_message_generation,wait_generation FROM watcher_scopes "
            "WHERE actor_agent_id=?",
            (SHOTCALLER_ID,),
        ).fetchone())
        with store._transaction():
            store.connection.execute(
                """
                INSERT INTO prompts
                  (prompt_id,intake_actor_id,runtime_instance_id,adapter_kind,session_ref,
                   source_event_key,triage_state,triage_digest,created_at,
                   current_owner_agent_id,current_owner_runtime_instance_id)
                VALUES(?,?,?,?,?,?,'untriaged',NULL,?,?,?)
                """,
                (
                    prompt_id, CHAMPION_ID, champion_runtime, "codex",
                    SHOTCALLER_ID, source_key, AT2, CHAMPION_ID, champion_runtime,
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
        captured_conflict = _capture_prompt(
            store,
            "watcher:Garen",
            SHOTCALLER_ID,
            "shotcaller",
            conflict,
            adapter_kind="codex",
            capture_event_id=capture_event_id,
        )
        with patch("league.canonical_watcher.notify_user_message") as notify:
            assert not _notify_direct_user_priority(
                store, SHOTCALLER_ID, "shotcaller", captured_conflict
            )
            notify.assert_not_called()
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
        after_conflict = tuple(store.connection.execute(
            "SELECT user_message_generation,wait_generation FROM watcher_scopes "
            "WHERE actor_agent_id=?",
            (SHOTCALLER_ID,),
        ).fetchone())
    assert captured_conflict["state"] == "quarantined"
    assert captured_conflict["wake_committed"] is False
    assert tuple(quarantine) == (
        "quarantined", "runtime_unverified", None, None, 0
    )
    assert tuple(owner) == (CHAMPION_ID, champion_runtime)
    assert after_conflict == before_conflict
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
    assert time.monotonic() - started < MAX_HOOK_LAUNCH_SECONDS
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
        if row["adapter_kind"] == "codex"
        and row["session_ref"] == session
        and row["body"] == payload["prompt"]
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
    with SQLiteStorage(state, request_wal=False) as store:
        rows = store.connection.execute(
            """
            SELECT prompt_id,state,reason,wake_actor_id,wake_scope_id,wake_committed
              FROM prompt_quarantine
             WHERE adapter_kind='codex' AND session_ref=? AND body=?
            """,
            (CHAMPION_ID, payload["prompt"]),
        ).fetchall()
        champion_scope = store.connection.execute(
            "SELECT user_message_generation,wait_generation FROM watcher_scopes WHERE actor_agent_id=?",
            (CHAMPION_ID,),
        ).fetchone()
    assert len(rows) == 1
    quarantined_prompt_id = str(rows[0]["prompt_id"])
    assert tuple(rows[0])[1:] == (
        "quarantined", "runtime_unverified", None, None, 0
    )
    assert champion_scope is None

    runtime_id = _register_champion_runtime(
        state, "later-verified", CHAMPION_ID
    )
    with SQLiteStorage(state, request_wal=False) as store:
        bound_receipt = store.bind_quarantined_prompt(
            quarantined_prompt_id,
            CHAMPION_ID,
            runtime_id,
            AT2,
            wake=False,
        )
    assert bound_receipt["triage_state"] == "untriaged"
    with SQLiteStorage(state, request_wal=False) as store:
        bound = store.connection.execute(
            """
            SELECT q.state,q.bound_actor_id,q.bound_runtime_instance_id,
                   q.wake_actor_id,q.wake_scope_id,q.wake_committed,
                   p.runtime_instance_id
              FROM prompt_quarantine q JOIN prompts p ON p.prompt_id=q.prompt_id
             WHERE q.prompt_id=?
            """,
            (quarantined_prompt_id,),
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
    with SQLiteStorage(state, request_wal=False) as store:
        prompts = store.connection.execute(
            """
            SELECT p.intake_actor_id,p.runtime_instance_id,p.triage_state,
                   pp.body,pp.body_hash,pp.byte_count
              FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
             WHERE p.adapter_kind='codex' AND p.session_ref=? AND pp.body=?
            """,
            (CHAMPION_ID, payload["prompt"]),
        ).fetchall()
        quarantined = store.connection.execute(
            "SELECT COUNT(*) FROM prompt_quarantine "
            "WHERE adapter_kind='codex' AND session_ref=? AND body=?",
            (CHAMPION_ID, payload["prompt"]),
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
    assert _watcher(env, "codex-stop-hook", payload=payload)["decision"] == "block"


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
    )["decision"] == "block"
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
    with SQLiteStorage(state, request_wal=False) as store:
        after = store.connection.execute(
            "SELECT user_message_generation,wait_generation FROM watcher_scopes WHERE actor_agent_id=?",
            (SHOTCALLER_ID,),
        ).fetchone()
        quarantine = store.connection.execute(
            "SELECT state,reason,wake_actor_id,wake_scope_id,wake_committed "
            "FROM prompt_quarantine WHERE adapter_kind='codex' "
            "AND session_ref=? AND body=?",
            (SHOTCALLER_ID, prompt["prompt"]),
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


def test_real_codex_stop_payload_rearms_per_prompt_event(root: Path) -> None:
    _, state, _ = seeded_state(root, "real-codex-stop-generation")
    env = _environment(root / "real-codex-stop-generation", state)
    _register_garen_runtime(
        state, "real-codex-stop-generation", session_ref=SHOTCALLER_ID
    )
    prompt_a = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:owner-visible-one",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Synthetic first real steer in the active turn.",
    }
    assert _watcher(env, "codex-user-prompt-hook", payload=prompt_a) == {}
    first = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:owner-visible-one",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "First end attempt.",
    }
    blocked = _watcher(env, "codex-stop-hook", payload=first)
    assert blocked["decision"] == "block"
    assert blocked["reason"] == (
        "League has unresolved obligations for Garen at wait generation 2."
    ), blocked
    assert "turn:owner-visible-one" not in str(blocked["reason"])

    with SQLiteStorage(state, request_wal=False) as store:
        before_feedback = tuple(
            store.connection.execute(
                "SELECT user_message_generation,wait_generation FROM watcher_scopes "
                "WHERE actor_agent_id=?",
                (SHOTCALLER_ID,),
            ).fetchone()
        )

    # Codex feeds League's exact reason into the same turn. That one durable
    # pending digest is consumed without becoming prompt intake or rearming.
    feedback = {
        "session_id": SHOTCALLER_ID,
        "turn_id": "turn:owner-visible-one",
        "hook_event_name": "UserPromptSubmit",
        "prompt": str(blocked["reason"]),
    }
    assert _watcher(env, "codex-user-prompt-hook", payload=feedback) == {}
    with SQLiteStorage(state, request_wal=False) as store:
        after_feedback = tuple(
            store.connection.execute(
                "SELECT user_message_generation,wait_generation FROM watcher_scopes "
                "WHERE actor_agent_id=?",
                (SHOTCALLER_ID,),
            ).fetchone()
        )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM prompt_payloads WHERE body=?", (feedback["prompt"],)
        ).fetchone()[0] == 0
    assert after_feedback == before_feedback

    retry = {
        **first,
        "stop_hook_active": True,
        "last_assistant_message": "Continuation end attempt.",
    }
    assert _watcher(env, "codex-stop-hook", payload=retry)["decision"] == "block"
    assert _watcher(env, "codex-stop-hook", payload=retry)["decision"] == "block"

    # Codex reuses turn_id for queued steers. A genuine second invocation is a
    # new durable event even when its prompt bytes deliberately repeat A.
    prompt_b = {
        **prompt_a,
        "prompt": prompt_a["prompt"],
    }
    assert _watcher(env, "codex-user-prompt-hook", payload=prompt_b) == {}
    next_block = _watcher(env, "codex-stop-hook", payload=retry)
    assert next_block["decision"] == "block"
    assert "Garen" in str(next_block["reason"])
    assert "turn:owner-visible-one" not in str(next_block["reason"])
    assert _watcher(env, "codex-stop-hook", payload=retry)["decision"] == "block"

    with SQLiteStorage(state, request_wal=False) as store:
        captured = store.connection.execute(
            """
            SELECT p.prompt_id,p.source_event_key,pp.body
              FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
             WHERE p.adapter_kind='codex' AND p.session_ref=? AND pp.body=?
             ORDER BY p.created_at,p.prompt_id
            """,
            (SHOTCALLER_ID, prompt_a["prompt"]),
        ).fetchall()
        scope = store.connection.execute(
            "SELECT last_event_id,user_message_generation,wait_generation "
            "FROM watcher_scopes WHERE actor_agent_id=?",
            (SHOTCALLER_ID,),
        ).fetchone()
    assert len(captured) == 2
    assert captured[0]["prompt_id"] != captured[1]["prompt_id"]
    assert captured[0]["source_event_key"] != captured[1]["source_event_key"]
    assert scope["last_event_id"] == captured[-1]["prompt_id"]


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
    assert stop_elapsed < MAX_HOOK_LAUNCH_SECONDS
    assert prompt.returncode == 0, prompt.stdout + prompt.stderr
    assert json.loads(prompt.stdout) == {}
    assert prompt_elapsed < MAX_HOOK_LAUNCH_SECONDS

    retry = _watcher(env, "codex-stop-hook", payload=stop_payload)
    assert retry["decision"] == "block"
    with SQLiteStorage(state, request_wal=False) as store:
        champion = store.agent_status(CHAMPION_ID)
        prompt_rows = store.connection.execute(
            """
            SELECT p.source_event_key,pp.body_hash,pp.byte_count
              FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
             WHERE p.adapter_kind='codex' AND p.session_ref=? AND pp.body=?
            """,
            (SHOTCALLER_ID, prompt_payload["prompt"]),
        ).fetchall()
        obligations = obligation_counts(store, SHOTCALLER_ID)
    holder.close()
    assert champion is not None and champion["version"] == 3
    assert champion["status"] == "blocked"
    assert len(prompt_rows) == 1
    encoded = prompt_payload["prompt"].encode("utf-8")
    assert prompt_rows[0]["source_event_key"].startswith("hook:")
    assert tuple(prompt_rows[0])[1:] == (
        hashlib.sha256(encoded).hexdigest(), len(encoded)
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
    _wait_for_watcher_registration(watcher_state, waiter)
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
    assert transitioned["result"]["delivery"]["effect_kind"] == "watcher_event"
    output, error = waiter.communicate(timeout=5)
    assert not error, error
    wake = json.loads(output)
    assert wake["event"] == "champion-update"
    assert wake["event_id"] == transitioned["result"]["event_id"]

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
    assert direct["result"]["delivery"]["state"] == "pending"
    assert direct["result"]["delivery"]["reason"] == "supervisor_unavailable"
    assert not prompt_log.exists()
    delivered = _watcher(
        direct_env,
        "--shotcaller",
        "Garen",
        "deliver",
        "--event-id",
        event_id,
    )
    assert delivered["state"] == "delivered" and delivered["idempotent"] is False
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


def test_task_transition_cli_dispatches_exact_watcher_receipt(root: Path) -> None:
    state, store, clock = create_context(root, "task-transition-cli-delivery")
    capture_p100(store, clock)
    store.claim_request("R3", "runtime:garen:one", "claim-r3", clock.after(120), clock.now())
    dispatch_request(store, clock, "R3", "claim-r3", "dispatch-r3", "repository-write", "champion")
    spec = AssignmentSpec(
            assignment_id="assignment:task-transition-cli",
            request_id="R3",
            claim_token="claim-r3",
            task_id="task:task-transition-cli",
            task_summary="Synthetic stable task-transition delivery",
            coordinator_agent_id=SHOTCALLER_ID,
            champion_agent_id=LUX_ID,
            callsign="Lux",
            repository="https://example.invalid/league.git",
            issue=23,
            branch="agent/synthetic/task-transition-cli",
            worktree="/synthetic/worktrees/task-transition-cli",
            issue_receipt=None,
        )
    active = AssignmentService(store, FakeLaunchAdapter(), clock, FakeIds()).assign(
        issue_bound_spec(store, spec, clock.now())
    )
    store.register_runtime(
        RuntimeRegistrationCommand(
            GAREN_RUNTIME_TWO,
            SHOTCALLER_ID,
            "codex-thread",
            "herdr",
            f"session:{GAREN_RUNTIME_TWO}",
            "synthetic:garen:two",
            "generation:garen:two",
            "closed",
            True,
            clock.now(),
        )
    )
    store.close()
    env = _environment(root / "task-transition-cli-delivery", state)
    waiter = subprocess.Popen(
        [env["TEST_INSTALLED_WATCHER"], "--shotcaller", "Garen", "supervise", "--poll-seconds", "0.05"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    _wait_for_watcher_registration(state, waiter)
    transitioned = _league_env(
        state,
        env,
        "task",
        "transition",
        "--task-id",
        active["task_id"],
        "--runtime-instance-id",
        active["runtime_instance_id"],
        "--expected-version",
        "3",
        "--state",
        "working",
        "--update",
        "Synthetic CLI task transition delivered.",
        "--next-action",
        "Complete the synthetic task.",
        "--transition-id",
        "transition:task-transition-cli",
        "--transition-key",
        "transition-key:task-transition-cli",
        "--event-id",
        "event:task-transition-cli",
        "--outbox-id",
        "outbox:task-transition-cli",
        "--recipient-agent-id",
        SHOTCALLER_ID,
        "--at",
        clock.now(),
    )
    assert transitioned["result"]["delivery"]["state"] == "delivered"
    assert transitioned["result"]["delivery"]["effect_kind"] == "watcher_event"
    output, error = waiter.communicate(timeout=5)
    assert not error, error
    wake = json.loads(output)
    assert wake["event"] == "champion-update"
    assert wake["event_id"] == transitioned["result"]["event_id"]
    assert wake["status"] == "working"


def test_watcher_readiness_timeout_terminates_exact_supervisor(root: Path) -> None:
    _, state, _ = seeded_state(root, "watcher-readiness-timeout")
    waiter = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_watcher_registration(state, waiter, timeout=0.05)
    except AssertionError as exc:
        assert "watcher registration did not become ready" in str(exc)
    else:
        raise AssertionError("missing watcher registration unexpectedly became ready")
    assert waiter.poll() is not None


def test_profile_bootstrap_is_inert_until_exact_binding_then_activates(
    root: Path,
) -> None:
    marker = "league.provider-hook-bootstrap.v1"
    cases = (
        (
            "codex",
            "codex-user-prompt-hook",
            "codex-pre-tool-hook",
            "codex-stop-hook",
            "session:bootstrap-codex",
            {
                "session_id": "session:bootstrap-codex",
                "turn_id": "turn:bootstrap",
            },
            "UserPromptSubmit",
            "PreToolUse",
            "Stop",
        ),
        (
            "cursor",
            "cursor-before-submit-hook",
            "cursor-pre-tool-hook",
            "cursor-stop-hook",
            "session:bootstrap-cursor",
            {
                "conversation_id": "session:bootstrap-cursor",
                "generation_id": "generation:bootstrap",
            },
            "beforeSubmitPrompt",
            "beforeShellExecution",
            "stop",
        ),
        (
            "pi",
            "pi-input-hook",
            "pi-pre-tool-hook",
            "pi-stop-hook",
            str(root / "pi/session.jsonl"),
            {
                "session_id": "session-pi-bootstrap",
                "session_path": str(root / "pi/session.jsonl"),
                "input_id": "input:bootstrap",
            },
            "PiInput",
            "PiToolCall",
            "PiStop",
        ),
    )
    for kind, prompt_command, pretool_command, stop_command, session, identity, prompt_event, pretool_event, stop_event in cases:
        label = f"profile-bootstrap-{kind}"
        _, state, _ = seeded_state(root, label)
        env = _environment(root / label, state)

        def mutation_snapshot() -> tuple[int, int, int, int]:
            with SQLiteStorage(state, request_wal=False) as store:
                return (
                    int(store.connection.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]),
                    int(store.connection.execute("SELECT COUNT(*) FROM prompt_quarantine").fetchone()[0]),
                    int(store.connection.execute("SELECT COALESCE(SUM(user_message_generation),0) FROM watcher_scopes").fetchone()[0]),
                    int(store.connection.execute("SELECT COALESCE(SUM(wait_generation),0) FROM watcher_scopes").fetchone()[0]),
                )

        before = mutation_snapshot()
        common = {**identity, "league_profile_bootstrap": marker}
        assert _watcher(
            env,
            prompt_command,
            payload={**common, "hook_event_name": prompt_event, "prompt": "ordinary unbound prompt"},
        ) == {"binding": "unbound"}
        assert _watcher(
            env,
            pretool_command,
            payload={**common, "hook_event_name": pretool_event, "authorized": True},
        ) == {"binding": "unbound"}
        assert _watcher(
            env,
            stop_command,
            payload={
                **common,
                "hook_event_name": stop_event,
                **({"stop_hook_active": True} if kind == "codex" else {}),
            },
        ) == {"binding": "unbound"}
        assert mutation_snapshot() == before

        _register_garen_runtime(
            state,
            label,
            session_ref=session,
            harness_kind=f"{kind}-thread",
        )
        captured = _watcher(
            env,
            prompt_command,
            payload={**common, "hook_event_name": prompt_event, "prompt": "promoted bound prompt"},
        )
        assert captured == {"binding": "bound"}
        authorized = _watcher(
            env,
            pretool_command,
            payload={**common, "hook_event_name": pretool_event, "authorized": True},
        )
        assert authorized == {
            "binding": "bound",
            "decision": "accept",
            "reason_code": "policy_accepted",
        }
        stopped = _watcher(
            env,
            stop_command,
            payload={
                **common,
                "hook_event_name": stop_event,
                **({"stop_hook_active": True} if kind == "codex" else {}),
            },
        )
        assert stopped["binding"] == "bound"

        invalid = {**common, "league_profile_bootstrap": "wrong"}
        result = subprocess.run(
            [env["TEST_INSTALLED_WATCHER"], prompt_command],
            input=json.dumps(
                {**invalid, "hook_event_name": prompt_event, "prompt": "invalid marker"}
            ),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        assert result.returncode == 2
        assert "hook_bootstrap_invalid" in result.stderr


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-canonical-watcher-") as temporary:
        root = Path(temporary)
        test_unbound_provider_stops_allow_without_mutation_when_broker_is_absent(root)
        test_bound_shotcallers_fail_closed_and_champion_gate_survives_absent_broker(root)
        test_stop_reason_uses_resolved_callsign_not_provider_turn_identity()
        test_explicit_and_session_stop_dispatch(root)
        test_supervise_wakes_and_stop_allows_after_settlement(root)
        test_working_and_progress_tasks_remain_supervised(root)
        test_supervise_user_priority(root)
        test_long_lived_supervisor_allows_concurrent_prompt_and_stop(root)
        test_provider_prompt_capture_identity_contracts(root)
        test_provider_pre_tool_policy_and_pi_stop_are_shared_and_fail_closed(root)
        test_queued_prompts_reusing_turn_id_are_unique_and_conflicts_quarantine(root)
        test_missing_identity_quarantines_then_binds_and_triages(root)
        test_unverified_champion_prompt_quarantines_without_shotcaller_wake(root)
        test_verified_champion_prompt_captures_without_shotcaller_wake(root)
        test_verified_runtime_session_routes_stop_and_pointer_state(root)
        test_quarantined_prompt_rearms_one_shot_stop(root)
        test_real_codex_stop_payload_rearms_per_prompt_event(root)
        test_transition_contention_keeps_stop_safe_and_prompt_durable(root)
        test_codex_stop_rejects_incomplete_real_payload(root)
        test_material_delivery_watcher_direct_dedup_and_unavailable(root)
        test_task_transition_cli_dispatches_exact_watcher_receipt(root)
        test_watcher_readiness_timeout_terminates_exact_supervisor(root)
        test_profile_bootstrap_is_inert_until_exact_binding_then_activates(root)
    print("PASS: installed SQLite Stop/supervise plus watcher/direct exact-once delivery and pending fallback")


if __name__ == "__main__":
    main()
