#!/usr/bin/env python3
"""Compare the retired JSON turn boundary with one-process SQLite."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from request_lifecycle_fixture import GAREN_RUNTIME, create_context  # noqa: E402
from storage_fixture import SHOTCALLER_ID  # noqa: E402


PROMPT_COUNT = 6
AT = "2026-01-01T01:00:00Z"
MAX_PHASE_OUTPUT_BYTES = 1_100_000
LEGACY_CALLSIGNS = ("Annie", "Ashe", "Braum", "Caitlyn", "Ekko", "Fiora")


def _capture(store, clock, prefix: str) -> None:
    for ordinal in range(1, PROMPT_COUNT + 1):
        store.intake_prompt(
            f"prompt:{prefix}:{ordinal}",
            SHOTCALLER_ID,
            GAREN_RUNTIME,
            "codex",
            f"session:{prefix}",
            f"source:{prefix}:{ordinal}",
            f"Exact synthetic prompt {ordinal}",
            clock.now(),
        )


def _semantic_decision(ordinal: int) -> dict[str, object]:
    return {
        "items": [
            {
                "summary": f"Handle synthetic request {ordinal}",
                "disposition": "new_request",
            }
        ]
    }


def _semantic_plan() -> dict[str, object]:
    return {
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


def _semantic_answer(ordinal: int) -> dict[str, object]:
    return {
        "kind": "answer",
        "request_index": ordinal,
        "content": f"Synthetic response {ordinal}",
        "resolution_summary": f"Answered synthetic request {ordinal}",
    }


def _legacy_status(callsign: str, ordinal: int) -> dict[str, object]:
    return {
        "callsign": callsign,
        "role": "champion",
        "shotcaller": "Garen",
        "kind": "codex-thread",
        "address": f"%{ordinal}",
        "thread_id": f"00000000-0000-4000-8000-{ordinal:012d}",
        "backend": "tmux",
        "task_id": f"legacy-turn-{ordinal}",
        "repository": None,
        "issue": None,
        "branch": None,
        "worktree": None,
        "task": f"Handle synthetic request {ordinal}",
        "status": "working",
        "updated_at": AT,
        "update": f"Handle synthetic request {ordinal}",
        "blocker": None,
        "next": f"Return synthetic response {ordinal}",
    }


def _prepare_legacy(root: Path) -> tuple[Path, Path]:
    records = root / "records"
    state = root / "state"
    shotcaller = records / "Garen"
    shotcaller.mkdir(parents=True)
    (shotcaller / "status.json").write_text(
        json.dumps(
            {
                "callsign": "Garen",
                "role": "shotcaller",
                "shotcaller": None,
                "kind": "codex-thread",
                "address": "synthetic-session",
                "thread_id": "synthetic-session",
                "task": "Synthetic turn benchmark",
                "status": "active",
                "updated_at": AT,
                "update": "Synthetic Shotcaller is active.",
                "blocker": None,
                "next": "Measure the bounded turn.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    for ordinal, callsign in enumerate(LEGACY_CALLSIGNS, start=1):
        record = shotcaller / "champions" / callsign
        record.mkdir(parents=True)
        status = _legacy_status(callsign, ordinal)
        (record / "status.json").write_text(
            json.dumps(status, sort_keys=True) + "\n", encoding="utf-8"
        )
        (record / "updates.jsonl").write_text(
            json.dumps(
                {
                    "at": AT,
                    "status": "working",
                    "update": status["update"],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return records, state


def _invoke(
    command: list[str], environment: dict[str, str]
) -> tuple[float, float, bytes]:
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        started = time.perf_counter()
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            env=environment,
        )
        spawned = time.perf_counter()
        process.wait(timeout=30)
        completed = time.perf_counter()
        for stream in (stdout_file, stderr_file):
            if stream.tell() > MAX_PHASE_OUTPUT_BYTES:
                raise RuntimeError("benchmark command output exceeded its bound")
            stream.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
    if process.returncode != 0:
        raise RuntimeError((stdout or stderr).decode("utf-8", errors="replace"))
    return (spawned - started) * 1000, (completed - spawned) * 1000, stdout


def _legacy_turn(root: Path, legacy_command: Path) -> dict[str, float | int]:
    records, state = _prepare_legacy(root)
    environment = {
        **os.environ,
        "HOME": str(root / "home"),
        "LEAGUE_WRITER_POINTER": str(root / "absent-writer-pointer.json"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    prefix = [
        str(legacy_command),
        "--records-root",
        str(records),
        "--state-dir",
        str(state),
        "--shotcaller",
        "Garen",
    ]
    total_started = time.perf_counter()
    startup_ms, intake_ms, output = _invoke(prefix + ["status"], environment)
    status = json.loads(output)
    if status.get("active_champions") != PROMPT_COUNT:
        raise RuntimeError("retired JSON intake did not observe every synthetic item")
    commit_ms = 0.0
    max_output_bytes = len(output)
    for ordinal, callsign in enumerate(LEGACY_CALLSIGNS, start=1):
        record = records / "Garen" / "champions" / callsign
        spawned_ms, operation_ms, output = _invoke(
            prefix
            + [
                "transition",
                "--record",
                str(record),
                "--status",
                "completed",
                "--update",
                f"Answered synthetic request {ordinal}",
                "--next",
                "Return the result to the Shotcaller.",
                "--at",
                AT,
                "--no-deliver",
            ],
            environment,
        )
        startup_ms += spawned_ms
        commit_ms += operation_ms
        max_output_bytes = max(max_output_bytes, len(output))
    total_ms = (time.perf_counter() - total_started) * 1000
    return {
        "process_startup_ms": startup_ms,
        "intake_ms": intake_ms,
        # The retired contract had no durable prompt triage/claim begin phase.
        "begin_ms": 0.0,
        "commit_ms": commit_ms,
        "total_ms": total_ms,
        "process_launches": 1 + PROMPT_COUNT,
        "command_count": 1 + PROMPT_COUNT,
        "per_prompt_shellouts": PROMPT_COUNT,
        "max_output_bytes": max_output_bytes,
    }


def _sqlite_turn(
    state: Path, sqlite_command: Path
) -> dict[str, float | int]:
    command = [
        str(sqlite_command),
        "--state-root",
        str(state),
        "request",
        "turn",
        "--owner-agent-id",
        SHOTCALLER_ID,
        "--at",
        AT,
    ]
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    started = time.perf_counter()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            env=environment,
        )
        spawned = time.perf_counter()
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("one-process turn pipes are unavailable")
        intake_line = process.stdout.readline(MAX_PHASE_OUTPUT_BYTES + 1)
        intake_at = time.perf_counter()
        intake = json.loads(intake_line)
        if intake["result"]["returned_count"] != PROMPT_COUNT:
            raise RuntimeError("one-process intake count mismatch")
        begin = {
            "decisions": [_semantic_decision(item) for item in range(1, PROMPT_COUNT + 1)],
            "plans": [_semantic_plan() for _ in range(PROMPT_COUNT)],
        }
        process.stdin.write(json.dumps(begin, separators=(",", ":")).encode() + b"\n")
        process.stdin.flush()
        begun_line = process.stdout.readline(MAX_PHASE_OUTPUT_BYTES + 1)
        begun_at = time.perf_counter()
        begun = json.loads(begun_line)
        if begun["result"]["phase"] != "begun" or process.poll() is not None:
            raise RuntimeError("one-process begin failed or exited early")
        commit = {
            "actions": [_semantic_answer(item) for item in range(1, PROMPT_COUNT + 1)]
        }
        process.stdin.write(json.dumps(commit, separators=(",", ":")).encode() + b"\n")
        process.stdin.flush()
        committed_line = process.stdout.readline(MAX_PHASE_OUTPUT_BYTES + 1)
        committed = json.loads(committed_line)
        returncode = process.wait(timeout=30)
        completed = time.perf_counter()
        if errors.tell() > MAX_PHASE_OUTPUT_BYTES:
            raise RuntimeError("one-process turn error output exceeded its bound")
        errors.seek(0)
        error = errors.read()
    if returncode != 0 or committed["result"]["phase"] != "committed":
        raise RuntimeError(error.decode("utf-8", errors="replace"))
    maximum = max(len(intake_line), len(begun_line), len(committed_line))
    if maximum > MAX_PHASE_OUTPUT_BYTES:
        raise RuntimeError("one-process turn output exceeded its bound")
    return {
        "process_startup_ms": (spawned - started) * 1000,
        "intake_ms": (intake_at - spawned) * 1000,
        "begin_ms": (begun_at - intake_at) * 1000,
        "commit_ms": (completed - begun_at) * 1000,
        "total_ms": (completed - started) * 1000,
        "process_launches": 1,
        "command_count": 1,
        "per_prompt_shellouts": 0,
        "max_output_bytes": maximum,
    }


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(samples: list[float]) -> dict[str, float]:
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "minimum_ms": round(min(samples), 3),
        "maximum_ms": round(max(samples), 3),
    }


def _path_summary(samples: list[dict[str, float | int]]) -> dict[str, object]:
    phases = {
        name: _summary([float(sample[f"{name}_ms"]) for sample in samples])
        for name in ("process_startup", "intake", "begin", "commit", "total")
    }
    return {
        "process_launches_per_turn": samples[0]["process_launches"],
        "command_count_per_turn": samples[0]["command_count"],
        "per_prompt_shellouts": samples[0]["per_prompt_shellouts"],
        "maximum_phase_output_bytes": max(
            int(sample["max_output_bytes"]) for sample in samples
        ),
        "phases": phases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--legacy-command", type=Path, default=ROOT / "bin/agent-watcher")
    parser.add_argument("--sqlite-command", type=Path, default=ROOT / "bin/league")
    args = parser.parse_args(argv)
    if not 2 <= args.samples <= 50:
        raise SystemExit("--samples must be between 2 and 50")
    legacy: list[dict[str, float | int]] = []
    sqlite: list[dict[str, float | int]] = []
    with tempfile.TemporaryDirectory(prefix="league-turn-benchmark-") as temporary:
        root = Path(temporary)
        for iteration in range(args.samples):
            legacy.append(_legacy_turn(root / f"legacy-{iteration}", args.legacy_command))
            prefix = f"sqlite-{iteration}"
            state, store, clock = create_context(root, prefix)
            _capture(store, clock, prefix)
            store.close()
            sqlite.append(_sqlite_turn(state, args.sqlite_command))
    result = {
        "schema": "league.request-turn-comparison.v2",
        "samples": args.samples,
        "workload": {
            "item_count": PROMPT_COUNT,
            "semantic_reasoning_included": False,
            "fixture_scope": "synthetic temporary roots only",
            "legacy_begin_persistence": False,
            "note": (
                "The retired JSON contract had no durable semantic triage/claim phase; "
                "its begin time is therefore zero and the comparison favors the retired path."
            ),
        },
        "retired_json": _path_summary(legacy),
        "one_process_sqlite": _path_summary(sqlite),
        "normal_turn_budget": {
            "request_turn_processes": 1,
            "per_prompt_status_unresolved_supervise_shellouts": 0,
            "maximum_phase_output_bytes": MAX_PHASE_OUTPUT_BYTES,
        },
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
