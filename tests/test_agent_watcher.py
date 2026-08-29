#!/usr/bin/env python3
"""Focused baseline watcher regressions."""

from __future__ import annotations

import fcntl
import json
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "agent-watcher"
CHAMPION_THREAD_ID = "00000000-0000-4000-8000-000000000015"
sys.path.insert(0, str(ROOT / "tests"))

from process_adapter import fake_process_environment  # noqa: E402


def fail(message: str) -> None:
    raise AssertionError(message)


def run(args, *, root: Path, state: Path, environment: dict[str, str], check=True, timeout=20):
    result = subprocess.run(
        [str(CLI), "--records-root", str(root), "--state-dir", str(state), *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        env=environment,
    )
    if check and result.returncode != 0:
        fail(f"{args}: {result.returncode}: {result.stderr}")
    return result


def write_status(root: Path, status: str = "working") -> tuple[Path, Path]:
    record = root / "Garen" / "champions" / "Zilean"
    record.mkdir(parents=True)
    (record / "status.json").write_text(
        json.dumps(
            {
                "callsign": "Zilean",
                "role": "champion",
                "shotcaller": "Garen",
                "kind": "codex-thread",
                "address": "%2",
                "thread_id": CHAMPION_THREAD_ID,
                "backend": "tmux",
                "task_id": "runtime-watcher-zilean",
                "repository": None,
                "issue": None,
                "branch": None,
                "worktree": None,
                "task": "Watcher regression",
                "status": status,
                "updated_at": "2026-08-26T01:00:00-07:00",
                "update": "Started the watcher regression.",
                "blocker": None,
                "next": "Wait for one material transition.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    updates = record / "updates.jsonl"
    updates.write_text(
        json.dumps(
            {
                "at": "2026-08-26T01:00:00-07:00",
                "status": status,
                "update": "Started the watcher regression.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return record / "status.json", updates


def append_transition(status_path: Path, updates: Path, at: str, status: str, update: str) -> None:
    with updates.open("r", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with updates.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": at, "status": status, "update": update}) + "\n")
        snapshot = json.loads(status_path.read_text())
        snapshot.update({"status": status, "updated_at": at, "update": update})
        status_path.write_text(json.dumps(snapshot) + "\n")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def wait_process(root: Path, state: Path, environment: dict[str, str]):
    return subprocess.Popen(
        [str(CLI), "--records-root", str(root), "--state-dir", str(state), "wait", "--poll-seconds", "0.03", "--liveness-seconds", "0.05"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )

def wait_until_baselined(state: Path, process: subprocess.Popen):
    deadline = time.monotonic() + 20
    state_file = state / "state.json"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            fail(f"wait ended before baseline: {process.stderr.read()}")
        if state_file.exists():
            try:
                if json.loads(state_file.read_text(encoding="utf-8")).get("initialized"):
                    return
            except json.JSONDecodeError:
                pass
        time.sleep(0.02)
    process.terminate()
    _, error = process.communicate(timeout=2)
    fail(f"watcher did not persist its baseline: {error}")


def test_controls_and_stop_hook(root: Path, state: Path, environment: dict[str, str]):
    write_status(root)
    before = (root / "Garen" / "champions" / "Zilean" / "status.json").read_bytes()
    rejected = run(["allow-stop"], root=root, state=state, environment=environment, check=False)
    assert rejected.returncode != 0, "allow-stop without --once was accepted"
    assert json.loads(run(["status"], root=root, state=state, environment=environment).stdout)["enabled"] is True
    run(["disable"], root=root, state=state, environment=environment)
    assert json.loads(run(["status"], root=root, state=state, environment=environment).stdout)["enabled"] is False
    run(["enable"], root=root, state=state, environment=environment)
    run(["allow-stop", "--once"], root=root, state=state, environment=environment)
    assert json.loads(run(["codex-stop-hook"], root=root, state=state, environment=environment).stdout) == {}
    blocked = json.loads(run(["codex-stop-hook"], root=root, state=state, environment=environment).stdout)
    assert blocked["decision"] == "block"
    assert (root / "Garen" / "champions" / "Zilean" / "status.json").read_bytes() == before


def test_durable_offset_dedup_and_material_wake(root: Path, state: Path, environment: dict[str, str]):
    status_path, updates = write_status(root)
    process = wait_process(root, state, environment)
    wait_until_baselined(state, process)
    pid = process.pid
    append_transition(
        status_path,
        updates,
        "2026-08-26T01:01:00-07:00",
        "progress",
        "Non-material progress stays silent.",
    )
    time.sleep(0.12)
    assert process.poll() is None, "non-material progress woke the blocking wait"
    append_transition(
        status_path,
        updates,
        "2026-08-26T01:02:00-07:00",
        "blocked",
        "Material blocker requires Shotcaller attention.",
    )
    try:
        output, _ = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.communicate(timeout=2)
        fail("material blocked transition did not wake the blocking wait")
    event = json.loads(output)
    assert process.pid == pid and event["event"] == "champion-update"
    duplicate = wait_process(root, state, environment)
    time.sleep(0.12)
    assert duplicate.poll() is None, "durable cursor did not suppress the duplicate event"
    duplicate.terminate()
    duplicate.communicate(timeout=2)


def test_liveness_is_silent(root: Path, state: Path, environment: dict[str, str]):
    write_status(root)
    process = wait_process(root, state, environment)
    ready, _, _ = select.select([process.stdout], [], [], 0.18)
    assert not ready, "silent liveness check woke the caller"
    process.terminate()
    process.communicate(timeout=2)


def test_bounded_failure_fails_open(root: Path, state: Path, environment: dict[str, str]):
    missing = root / "missing"
    result = run(["wait", "--poll-seconds", "0.02", "--repair-command", "false"], root=missing, state=state, environment=environment, timeout=3)
    event = json.loads(result.stdout)
    assert event["event"] == "watcher-unavailable" and event["fail_open"] is True
    assert "fail-open" in result.stderr


def test_teardown_fails_closed(root: Path, state: Path, environment: dict[str, str]):
    _, updates = write_status(root)
    evidence = root / "unsafe.json"
    evidence.write_text(
        json.dumps({
            "identity": {"socket": "test", "pane_id": "%1"},
            "records": [str(updates)],
            "grace_elapsed": True,
        })
        + "\n",
        encoding="utf-8",
    )
    result = run(
        ["teardown", "--adapter", "tmux", "--evidence", str(evidence), "--archive-dir", str(root / "archive"), "--execute"],
        root=root,
        state=state,
        environment=environment,
        check=False,
    )
    assert result.returncode != 0 and "teardown refused" in result.stderr
    assert updates.exists(), "unsafe teardown mutated a Champion record"


def test_legacy_teardown_proof_is_refused(root: Path, state: Path, environment: dict[str, str]):
    _, updates = write_status(root)
    identity = {"socket": "isolated", "pane_id": "%1"}
    evidence = root / "safe.json"
    evidence.write_text(
        json.dumps(
            {
                "identity": identity,
                "expected_identity": identity,
                "records": [str(updates)],
                "grace_elapsed": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = run(
        ["teardown", "--adapter", "tmux", "--evidence", str(evidence), "--archive-dir", str(root / "archive")],
        root=root,
        state=state,
        environment=environment,
        check=False,
    )
    assert result.returncode != 0 and "manifest schema must be 2" in result.stderr
    assert updates.exists(), "refused legacy teardown changed a Champion record"


def main():
    with tempfile.TemporaryDirectory(prefix="agent-watcher-test.") as directory:
        temporary = Path(directory)
        environment = fake_process_environment(temporary)
        environment["LEAGUE_WRITER_POINTER"] = str(
            temporary / "absent-writer-pointer.json"
        )
        root = temporary / "records"
        state = temporary / "state"
        test_controls_and_stop_hook(root, state, environment)
        test_durable_offset_dedup_and_material_wake(root / "offset", state / "offset", environment)
        test_liveness_is_silent(root / "liveness", state / "liveness", environment)
        test_bounded_failure_fails_open(root / "failure", state / "failure", environment)
        test_teardown_fails_closed(root / "teardown", state / "teardown", environment)
        test_legacy_teardown_proof_is_refused(root / "teardown-plan", state / "teardown-plan", environment)
    print("PASS: controls, one-shot Stop permission, durable offsets/deduplication, silent liveness, material wake, bounded failure, and fail-closed teardown")


if __name__ == "__main__":
    main()
