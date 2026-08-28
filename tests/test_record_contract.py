#!/usr/bin/env python3
"""Focused strict durable-record contract regressions."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "agent-watcher"
STATUS_EXAMPLE = ROOT / "examples" / "agent-status.example.json"
UPDATES_EXAMPLE = ROOT / "examples" / "agent-updates.example.jsonl"
MODULE_PATH = ROOT / "src" / "agent_watcher.py"
AT = "2026-08-26T01:30:00-07:00"
THREAD_ID = "00000000-0000-4000-8000-000000000017"
sys.path.insert(0, str(ROOT / "tests"))

from process_adapter import fake_process_environment  # noqa: E402


def run(records: Path, state: Path, *args: str, check: bool = False):
    environment = fake_process_environment(state / "process-adapter")
    result = subprocess.run(
        [str(CLI), "--records-root", str(records), "--state-dir", str(state), *args],
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr)
    return result


def write_shotcaller(records: Path) -> None:
    directory = records / "Garen"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "status.json").write_text(
        json.dumps(
            {
                "callsign": "Garen",
                "role": "shotcaller",
                "shotcaller": None,
                "kind": "codex-thread",
                "address": "garen-session",
                "thread_id": "garen-session",
                "task": "Shotcaller coordination",
                "status": "active",
                "updated_at": AT,
                "update": "Shotcaller is active.",
                "blocker": None,
                "next": "Receive lifecycle transitions.",
            }
        )
        + "\n"
    )


def valid_record(records: Path, status: str = "working", update: str = "Started strict validation.") -> Path:
    directory = records / "Garen" / "champions" / "Bard"
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "callsign": "Bard",
        "role": "champion",
        "shotcaller": "Garen",
        "kind": "codex-thread",
        "address": "w1:p1W",
        "thread_id": THREAD_ID,
        "backend": "herdr",
        "task_id": "example-repository-17",
        "repository": "https://example.invalid/example/repository",
        "issue": 17,
        "branch": "agent/bard/17-example",
        "worktree": "/example/worktrees/repository-17-example",
        "task": "Example lifecycle routing",
        "status": status,
        "updated_at": AT,
        "update": update,
        "blocker": None,
        "next": "Continue strict validation.",
    }
    (directory / "status.json").write_text(json.dumps(snapshot) + "\n")
    (directory / "updates.jsonl").write_text(
        json.dumps({"at": AT, "status": status, "update": update}) + "\n"
    )
    return directory


def assert_contract_error(result, fragment: str) -> None:
    assert result.returncode != 0, result.stdout
    assert "record contract violation" in result.stderr, result.stderr
    assert fragment in result.stderr, result.stderr


def test_malformed_and_duplicate_json(root: Path) -> None:
    records, state = root / "malformed-records", root / "malformed-state"
    record = valid_record(records)
    (record / "status.json").write_text("{\n")
    assert_contract_error(run(records, state, "status"), "status.json")

    records, state = root / "update-records", root / "update-state"
    record = valid_record(records)
    (record / "updates.jsonl").write_text('{"at":\n')
    assert_contract_error(run(records, state, "status"), "updates.jsonl")

    records, state = root / "duplicate-records", root / "duplicate-state"
    record = valid_record(records)
    (record / "updates.jsonl").write_text(
        '{"at":"2026-08-26T01:30:00-07:00","status":"working",'
        '"status":"blocked","update":"duplicate status"}\n'
    )
    assert_contract_error(run(records, state, "status"), "duplicate JSON key: status")


def test_invalid_types_status_and_mismatch(root: Path) -> None:
    records, state = root / "type-records", root / "type-state"
    record = valid_record(records)
    snapshot = json.loads((record / "status.json").read_text())
    snapshot["next"] = 7
    (record / "status.json").write_text(json.dumps(snapshot) + "\n")
    assert_contract_error(run(records, state, "status"), "next must be a non-empty string")

    records, state = root / "missing-records", root / "missing-state"
    record = valid_record(records)
    snapshot = json.loads((record / "status.json").read_text())
    del snapshot["blocker"]
    (record / "status.json").write_text(json.dumps(snapshot) + "\n")
    assert_contract_error(run(records, state, "status"), "blocker field is required")

    records, state = root / "status-records", root / "status-state"
    record = valid_record(records)
    transition = {"at": AT, "status": "invented", "update": "unsupported"}
    (record / "updates.jsonl").write_text(json.dumps(transition) + "\n")
    assert_contract_error(run(records, state, "status"), "unsupported lifecycle status")

    records, state = root / "mismatch-records", root / "mismatch-state"
    record = valid_record(records)
    snapshot = json.loads((record / "status.json").read_text())
    snapshot["update"] = "Snapshot diverged from the durable log."
    (record / "status.json").write_text(json.dumps(snapshot) + "\n")
    assert_contract_error(run(records, state, "status"), "latest transition does not match")


def test_ready_gate_valid_pair_and_explicit_legacy(root: Path) -> None:
    records, state = root / "ready-records", root / "ready-state"
    write_shotcaller(records)
    record = valid_record(records, "ready_to_land", "Exact PR head is ready.")
    valid = run(records, state, "status", check=True)
    assert json.loads(valid.stdout)["active_champions"] == 1

    snapshot = json.loads((record / "status.json").read_text())
    snapshot["update"] = "Conflicting ready snapshot."
    (record / "status.json").write_text(json.dumps(snapshot) + "\n")
    refused = run(
        records,
        state,
        "--shotcaller",
        "Garen",
        "supervise",
        "--poll-seconds",
        "0.02",
    )
    assert_contract_error(refused, "latest transition does not match")

    records, state = root / "legacy-records", root / "legacy-state"
    directory = records / "Garen" / "champions" / "Zilean"
    directory.mkdir(parents=True)
    (directory / "status.json").write_text(
        json.dumps({"callsign": "Zilean", "role": "champion", "status": "working"}) + "\n"
    )
    (directory / "updates.jsonl").write_text(
        json.dumps({"at": "start", "status": "working", "update": "legacy"}) + "\n"
    )
    assert run(records, state, "status").returncode != 0
    classified = run(records, state, "--record-format", "legacy", "status", check=True)
    assert json.loads(classified.stdout)["active_champions"] == 1


def test_exact_identity_placeholders_and_runtime_nulls(root: Path) -> None:
    records, state = root / "example-records", root / "example-state"
    example = records / "Garen" / "champions" / "Bard"
    example.mkdir(parents=True)
    (example / "status.json").write_bytes(STATUS_EXAMPLE.read_bytes())
    (example / "updates.jsonl").write_bytes(UPDATES_EXAMPLE.read_bytes())
    assert run(records, state, "status", check=True).returncode == 0

    records, state = root / "exact-records", root / "exact-state"
    valid_record(records)
    assert run(records, state, "status", check=True).returncode == 0

    records, state = root / "runtime-records", root / "runtime-state"
    record = valid_record(records)
    snapshot = json.loads((record / "status.json").read_text())
    snapshot.update({"repository": None, "issue": None, "branch": None, "worktree": None})
    (record / "status.json").write_text(json.dumps(snapshot) + "\n")
    assert run(records, state, "status", check=True).returncode == 0

    for field, placeholder in (
        ("thread_id", "unavailable"),
        ("thread_id", "current-codex-thread"),
        ("task_id", "placeholder"),
    ):
        records = root / f"placeholder-{field}-{placeholder}"
        state = root / f"placeholder-state-{field}-{placeholder}"
        record = valid_record(records)
        snapshot = json.loads((record / "status.json").read_text())
        snapshot[field] = placeholder
        (record / "status.json").write_text(json.dumps(snapshot) + "\n")
        assert_contract_error(run(records, state, "status"), field)

    records, state = root / "partial-records", root / "partial-state"
    record = valid_record(records)
    snapshot = json.loads((record / "status.json").read_text())
    snapshot["worktree"] = None
    (record / "status.json").write_text(json.dumps(snapshot) + "\n")
    assert_contract_error(run(records, state, "status"), "all be exact or all null")


def test_live_herdr_thread_mismatch(root: Path) -> None:
    binary = root / "herdr-bin"
    binary.mkdir()
    herdr = binary / "herdr"
    herdr.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"result\":{\"agents\":[{\"pane_id\":\"w1:p1W\","
        "\"agent_session\":{\"value\":\"00000000-0000-4000-8000-000000000015\"}}]}}'\n"
    )
    herdr.chmod(0o755)
    spec = importlib.util.spec_from_file_location("agent_watcher_identity_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_path = os.environ["PATH"]
    os.environ["PATH"] = f"{binary}:{original_path}"
    try:
        try:
            module._verify_live_endpoint(
                "herdr",
                {
                    "session": "isolated",
                    "pane_id": "w1:p1W",
                    "thread_id": THREAD_ID,
                },
            )
        except module.WatcherError as exc:
            assert "live Herdr endpoint identity conflicts" in str(exc)
        else:
            raise AssertionError("Herdr pane with a conflicting Codex UUID was accepted")
    finally:
        os.environ["PATH"] = original_path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agent-record-contract.") as value:
        root = Path(value)
        test_malformed_and_duplicate_json(root)
        test_invalid_types_status_and_mismatch(root)
        test_ready_gate_valid_pair_and_explicit_legacy(root)
        test_exact_identity_placeholders_and_runtime_nulls(root)
        test_live_herdr_thread_mismatch(root)
    print("PASS: strict records, exact Champion identity, runtime nulls, and explicit legacy classification")


if __name__ == "__main__":
    main()
