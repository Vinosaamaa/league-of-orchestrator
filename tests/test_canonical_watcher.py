#!/usr/bin/env python3
"""Installed-shape SQLite watcher dispatch, Stop, and supervise regressions."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from storage_fixture import AT2, CHAMPION_ID, SHOTCALLER_ID  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402


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
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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
            {"session_id": SHOTCALLER_ID, "hook_event_name": "Stop"},
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
    _register_garen_runtime(state, "settlement", session_ref=SHOTCALLER_ID)
    _fake_herdr(root / "supervise", env)
    first = _watcher(
        env,
        "codex-stop-hook",
        payload={"session_id": SHOTCALLER_ID, "hook_event_name": "Stop", "turn": "one"},
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
        payload={"session_id": SHOTCALLER_ID, "hook_event_name": "Stop", "turn": "two"},
    )
    assert allowed == {}, allowed


def test_supervise_user_priority(root: Path) -> None:
    _, state, _ = seeded_state(root, "user-priority")
    env = _environment(root / "user-priority", state)
    _register_garen_runtime(
        state, "user-priority", session_ref=SHOTCALLER_ID
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
        prompts = [
            row
            for row in exported["tables"]["prompts"]
            if row["source_event_key"] == payload[event_field]
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
            payload={"hook_event_name": "Stop", "session_id": SHOTCALLER_ID},
        )
        assert stop["decision"] == "block"


def test_material_delivery_watcher_direct_dedup_and_unavailable(root: Path) -> None:
    _, watcher_state, _ = seeded_state(root, "watcher-delivery")
    watcher_env = _environment(root / "watcher-delivery", watcher_state)
    _register_garen_runtime(
        watcher_state, "watcher", session_ref=SHOTCALLER_ID
    )
    waiter = subprocess.Popen(
        [watcher_env["TEST_INSTALLED_WATCHER"], "--shotcaller", "Garen", "supervise", "--poll-seconds", "0.1"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=watcher_env,
    )
    time.sleep(0.15)
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
    unavailable_env["PATH"] = "/usr/bin:/bin"
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
        test_supervise_user_priority(root)
        test_codex_and_cursor_prompt_capture_exactly_once(root)
        test_material_delivery_watcher_direct_dedup_and_unavailable(root)
    print("PASS: installed SQLite Stop/supervise plus watcher/direct exact-once delivery and pending fallback")


if __name__ == "__main__":
    main()
