#!/usr/bin/env python3
"""Measure paired semantic-triage OFF/ON turns on synthetic temporary state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import random
import select
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from request_lifecycle_fixture import GAREN_RUNTIME, GAREN_RUNTIME_TWO, create_context  # noqa: E402
from storage_fixture import SHOTCALLER_ID  # noqa: E402
from league.storage import RuntimeRegistrationCommand  # noqa: E402


AT = "2026-01-01T01:00:00Z"
MODEL_SESSION_REF = f"session:{GAREN_RUNTIME}"
MAX_LINE_BYTES = 1_100_000
PROCESS_TIMEOUT_SECONDS = 180
DEFAULT_SEED = 66_001
REQUIRED_CATEGORIES = {
    "direct_question",
    "bounded_read_only",
    "repository_change",
    "supervised_benchmark",
    "acknowledgement",
    "context_only",
}
ModelRunner = Callable[[Sequence[dict[str, Any]], Path, argparse.Namespace], tuple[dict[str, Any], dict[str, float]]]


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _command_output(command: Sequence[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(command),
        check=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=30,
    )
    return completed.stdout.strip()


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"median_ms": 0.0, "p95_ms": 0.0, "minimum_ms": 0.0, "maximum_ms": 0.0}
    return {
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "minimum_ms": round(min(values), 3),
        "maximum_ms": round(max(values), 3),
    }


def _load_corpus(path: Path) -> tuple[list[dict[str, Any]], str]:
    encoded = path.read_bytes()
    value = json.loads(encoded)
    if value.get("schema") != "league.semantic-triage-corpus.v1":
        raise RuntimeError("semantic corpus schema is unsupported")
    categories = value.get("categories")
    if not isinstance(categories, list) or {row.get("name") for row in categories} != REQUIRED_CATEGORIES:
        raise RuntimeError("semantic corpus categories are incomplete")
    if any(not isinstance(row.get("prompts"), list) or len(row["prompts"]) != 20 for row in categories):
        raise RuntimeError("every semantic corpus category must contain exactly twenty prompts")
    cases: list[dict[str, Any]] = []
    for ordinal in range(20):
        for category in categories:
            prompt = category["prompts"][ordinal]
            if not isinstance(prompt, str) or not prompt or "\n" in prompt:
                raise RuntimeError("semantic corpus prompts must be non-empty single-line strings")
            cases.append(
                {
                    "id": f"case-{len(cases) + 1:03d}",
                    "category": category["name"],
                    "prompt": prompt,
                    "gold": category["gold"],
                }
            )
    if len(cases) != 120 or len({row["prompt"] for row in cases}) != 120:
        raise RuntimeError("semantic corpus must expand to 120 unique prompts")
    return cases, _sha256_bytes(encoded)


def _batch(cases: Sequence[dict[str, Any]], count: int, sample: int, seed: int) -> list[dict[str, Any]]:
    start = (seed + sample * count) % len(cases)
    selected = [cases[(start + offset) % len(cases)] for offset in range(count)]
    if sum(row["gold"]["disposition"] == "new_request" for row in selected) > 20:
        raise RuntimeError("corpus ordering exceeded the request-turn plan bound")
    return selected


def _gold_payload(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    for case in cases:
        decisions.append(
            {
                "items": [
                    {
                        "summary": f"Classify {case['id']} as {case['category'].replace('_', ' ')}",
                        "disposition": case["gold"]["disposition"],
                    }
                ],
                "plan": case["gold"]["plan"],
            }
        )
    return {"decisions": decisions}


def _validate_model_payload(payload: Any, cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"decisions"}:
        raise RuntimeError("model triage output must contain only decisions")
    decisions = payload["decisions"]
    if not isinstance(decisions, list):
        raise RuntimeError("model triage decisions must be an array")
    if len(decisions) != len(cases):
        raise RuntimeError(
            "model triage decision count is invalid: "
            f"expected {len(cases)}, observed {len(decisions)}"
        )
    allowed_dispositions = {"new_request", "acknowledgement", "context"}
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {"items", "plan"}:
            raise RuntimeError("model decision shape is invalid")
        items = decision["items"]
        if not isinstance(items, list) or len(items) != 1:
            raise RuntimeError("benchmark model decisions must contain exactly one item")
        item = items[0]
        if (
            not isinstance(item, dict)
            or set(item) != {"summary", "disposition"}
            or not isinstance(item["summary"], str)
            or not item["summary"]
            or item["disposition"] not in allowed_dispositions
        ):
            raise RuntimeError("model semantic item is invalid")
        plan = decision["plan"]
        if item["disposition"] == "new_request" and not isinstance(plan, dict):
            raise RuntimeError("new request decision requires its adjacent routing plan")
        if item["disposition"] != "new_request" and plan is not None:
            raise RuntimeError("non-request decision must carry a null routing plan")
    required_plan = {"work_kind", "requested_mode", "signals"}
    required_signals = {
        "pre_bounded",
        "read_only",
        "answer_or_routing_only",
        "expected_minutes",
        "expected_task_action_calls",
        "creates_artifact",
        "mutates_state",
        "project_implementation",
        "runs_tests",
        "runs_benchmark",
    }
    for plan in (decision["plan"] for decision in decisions if decision["plan"] is not None):
        if not isinstance(plan, dict) or set(plan) != required_plan:
            raise RuntimeError("model routing plan shape is invalid")
        if plan["work_kind"] not in {"question", "read-only", "repository-write", "supervised-test"}:
            raise RuntimeError("model work kind is outside the benchmark vocabulary")
        if plan["requested_mode"] not in {"direct", "champion"}:
            raise RuntimeError("model requested mode is outside the benchmark vocabulary")
        signals = plan["signals"]
        if not isinstance(signals, dict) or set(signals) != required_signals:
            raise RuntimeError("model routing signals are incomplete")
    return payload


def _league_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "decisions": [
            {"items": decision["items"]} for decision in payload["decisions"]
        ],
        "plans": [
            decision["plan"]
            for decision in payload["decisions"]
            if decision["plan"] is not None
        ],
    }


def _accuracy(payload: dict[str, Any], cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    correct = 0
    by_category: dict[str, list[int]] = {}
    mismatches: list[str] = []
    for case, decision in zip(cases, payload["decisions"]):
        expected_disposition = case["gold"]["disposition"]
        observed_disposition = decision["items"][0]["disposition"]
        case_correct = observed_disposition == expected_disposition
        case_correct = case_correct and decision["plan"] == case["gold"]["plan"]
        values = by_category.setdefault(case["category"], [0, 0])
        values[1] += 1
        if case_correct:
            correct += 1
            values[0] += 1
        elif len(mismatches) < 20:
            mismatches.append(case["id"])
    return {
        "correct": correct,
        "total": len(cases),
        "accuracy": round(correct / len(cases), 6),
        "by_category": {
            name: {"correct": values[0], "total": values[1]}
            for name, values in sorted(by_category.items())
        },
        "mismatch_case_ids": mismatches,
    }


def _model_prompt(cases: Sequence[dict[str, Any]]) -> str:
    inputs = [{"id": row["id"], "text": row["prompt"]} for row in cases]
    policy = {
        "plain_explanatory_question": {
            "work_kind": "question",
            "requested_mode": "direct",
            "signals": {
                "pre_bounded": True,
                "read_only": True,
                "answer_or_routing_only": True,
                "expected_minutes": 2,
                "expected_task_action_calls": 1,
                "creates_artifact": False,
                "mutates_state": False,
                "project_implementation": False,
                "runs_tests": False,
                "runs_benchmark": False,
            },
        },
        "bounded_inspection_of_supplied_material": {
            "work_kind": "read-only",
            "requested_mode": "direct",
            "signals": {
                "pre_bounded": True,
                "read_only": True,
                "answer_or_routing_only": True,
                "expected_minutes": 4,
                "expected_task_action_calls": 2,
                "creates_artifact": False,
                "mutates_state": False,
                "project_implementation": False,
                "runs_tests": False,
                "runs_benchmark": False,
            },
        },
        "repository_change": {
            "work_kind": "repository-write",
            "requested_mode": "champion",
            "signals": {
                "pre_bounded": False,
                "read_only": False,
                "answer_or_routing_only": False,
                "expected_minutes": 20,
                "expected_task_action_calls": 4,
                "creates_artifact": True,
                "mutates_state": True,
                "project_implementation": True,
                "runs_tests": False,
                "runs_benchmark": False,
            },
        },
        "test_or_benchmark_execution": {
            "work_kind": "supervised-test",
            "requested_mode": "champion",
            "signals": {
                "pre_bounded": False,
                "read_only": False,
                "answer_or_routing_only": False,
                "expected_minutes": 15,
                "expected_task_action_calls": 4,
                "creates_artifact": False,
                "mutates_state": False,
                "project_implementation": False,
                "runs_tests": True,
                "runs_benchmark": True,
            },
        },
    }
    return (
        "Classify this ordered League prompt batch. Do not call tools. Return only the JSON object "
        "required by the supplied output schema. Produce exactly one decision with one semantic item "
        "per input, in input order. Use acknowledgement only for a message that merely confirms or "
        "thanks with no requested work. Use context only for information explicitly supplied only as "
        "background or reference. Everything else is new_request. Plans correspond only to new_request "
        "items in order. Every decision must carry its own adjacent plan object for a new request or null "
        "for acknowledgement/context. Copy the complete matching plan object from POLICY exactly; "
        "a bounded inspection of supplied material is answer_or_routing_only because its only deliverable is "
        "the answer. Summaries must be short and must not copy private reasoning.\n\nPOLICY="
        + _stable_json(policy)
        + "\n\nINPUT="
        + _stable_json(inputs)
    )


def _read_process_line(process: subprocess.Popen[bytes], deadline: float) -> tuple[bytes, int]:
    if process.stdout is None:
        raise RuntimeError("subprocess output pipe is unavailable")
    remaining = deadline - time.monotonic()
    readable, _, _ = select.select([process.stdout], [], [], max(0.0, remaining))
    if not readable:
        raise RuntimeError("subprocess output timed out")
    line = process.stdout.readline(MAX_LINE_BYTES + 1)
    observed_ns = time.perf_counter_ns()
    if not line or len(line) > MAX_LINE_BYTES:
        raise RuntimeError("subprocess output is missing or exceeds its bound")
    return line, observed_ns


def _codex_model_runner(
    cases: Sequence[dict[str, Any]], model_root: Path, args: argparse.Namespace
) -> tuple[dict[str, Any], dict[str, float]]:
    schema = json.loads(args.output_schema.read_text(encoding="utf-8"))
    schema["properties"]["decisions"]["minItems"] = len(cases)
    schema["properties"]["decisions"]["maxItems"] = len(cases)
    derived_schema = model_root / "semantic-output-schema.json"
    derived_schema.write_text(_stable_json(schema) + "\n", encoding="utf-8")
    command = [
        str(args.codex_command),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(model_root),
        "--model",
        args.model,
        "--config",
        f'model_reasoning_effort="{args.reasoning_effort}"',
        "--output-schema",
        str(derived_schema),
        "--json",
        "-",
    ]
    prompt = _model_prompt(cases).encode("utf-8")
    started_ns = time.perf_counter_ns()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        spawned_ns = time.perf_counter_ns()
        if process.stdin is None:
            raise RuntimeError("Codex input pipe is unavailable")
        process.stdin.write(prompt)
        process.stdin.close()
        deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
        turn_started_ns: int | None = None
        message_ns: int | None = None
        completed_ns: int | None = None
        message: str | None = None
        while True:
            line, observed_ns = _read_process_line(process, deadline)
            event = json.loads(line)
            event_type = event.get("type")
            if event_type == "turn.started":
                turn_started_ns = observed_ns
            elif event_type == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                message = event["item"].get("text")
                message_ns = observed_ns
            elif event_type == "turn.completed":
                completed_ns = observed_ns
                break
            elif event_type in {"turn.failed", "error"}:
                raise RuntimeError("Codex semantic triage failed")
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        exited_ns = time.perf_counter_ns()
        errors.seek(0)
        error = errors.read(MAX_LINE_BYTES + 1)
    if returncode != 0 or turn_started_ns is None or message_ns is None or completed_ns is None:
        raise RuntimeError(error.decode("utf-8", errors="replace") or "Codex semantic triage was incomplete")
    parse_started_ns = time.perf_counter_ns()
    payload = json.loads(message or "")
    parse_completed_ns = time.perf_counter_ns()
    _validate_model_payload(payload, cases)
    return payload, {
        "model_process_startup_ms": (turn_started_ns - spawned_ns) / 1_000_000,
        "semantic_model_ms": (message_ns - turn_started_ns) / 1_000_000,
        "model_completion_tail_ms": (completed_ns - message_ns) / 1_000_000,
        "model_total_ms": (completed_ns - started_ns) / 1_000_000,
        "model_process_exit_tail_ms": (exited_ns - completed_ns) / 1_000_000,
        "model_wall_ms": (exited_ns - started_ns) / 1_000_000,
        "decision_json_parse_ms": (parse_completed_ns - parse_started_ns) / 1_000_000,
    }


def _watcher_environment(root: Path, state: Path, watcher: Path) -> dict[str, str]:
    pointer = root / "league-writer-pointer.json"
    pointer.write_text(
        _stable_json({"writer": "sqlite", "generation": "benchmark-generation"}) + "\n",
        encoding="utf-8",
    )
    return {
        **os.environ,
        "LEAGUE_WRITER_POINTER": str(pointer),
        "LEAGUE_STATE_ROOT": str(state),
        "PYTHONDONTWRITEBYTECODE": "1",
        "BENCHMARK_WATCHER": str(watcher),
    }


def _wait_for_supervisor_ready(
    league: Path,
    state: Path,
    waiter: subprocess.Popen[str],
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Observe canonical watcher readiness through the stable export facade."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if waiter.poll() is not None:
            output, error = waiter.communicate()
            raise RuntimeError(f"installed watcher exited before readiness: {output}{error}")
        exported = subprocess.run(
            [
                str(league),
                "--state-root",
                str(state),
                "storage",
                "export",
                "--format",
                "json",
                "--purpose",
                "inspection",
                "--max-records",
                "10000",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=30,
            check=False,
        )
        if exported.returncode == 0:
            snapshot = json.loads(exported.stdout)
            registrations = snapshot["tables"]["watcher_registrations"]
            scopes = snapshot["tables"]["watcher_scopes"]
            if registrations and any(int(row["wait_active"]) == 1 for row in scopes):
                return
        time.sleep(0.02)
    waiter.terminate()
    raise RuntimeError("installed watcher did not publish canonical readiness")


def _capture_with_hook(
    root: Path,
    state: Path,
    cases: Sequence[dict[str, Any]],
    league: Path,
    watcher: Path,
) -> dict[str, Any]:
    environment = _watcher_environment(root, state, watcher)
    waiter = subprocess.Popen(
        [str(watcher), "--shotcaller", "Garen", "supervise", "--poll-seconds", "0.01"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        text=True,
    )
    _wait_for_supervisor_ready(league, state, waiter)
    observed: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=1)

    def read_wake() -> None:
        assert waiter.stdout is not None
        line = waiter.stdout.readline(MAX_LINE_BYTES + 1)
        observed.put((time.perf_counter_ns(), line))

    reader = threading.Thread(target=read_wake, name="league-benchmark-wake-reader")
    reader.start()
    hook_durations: list[float] = []
    capture_started_ns = time.perf_counter_ns()
    first_started_ns = 0
    first_completed_ns = 0
    for index, case in enumerate(cases):
        payload = {
            "session_id": MODEL_SESSION_REF,
            "turn_id": f"benchmark-turn-{case['id']}",
            "hook_event_name": "UserPromptSubmit",
            "prompt": case["prompt"],
        }
        started_ns = time.perf_counter_ns()
        if index == 0:
            first_started_ns = started_ns
        completed = subprocess.run(
            [str(watcher), "codex-user-prompt-hook"],
            input=_stable_json(payload),
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
            check=False,
        )
        completed_ns = time.perf_counter_ns()
        if index == 0:
            first_completed_ns = completed_ns
        if completed.returncode != 0 or json.loads(completed.stdout) != {}:
            raise RuntimeError(completed.stderr or "installed prompt hook failed")
        hook_durations.append((completed_ns - started_ns) / 1_000_000)
    duplicate = {
        "session_id": MODEL_SESSION_REF,
        "turn_id": f"benchmark-turn-{cases[0]['id']}",
        "hook_event_name": "UserPromptSubmit",
        "prompt": cases[0]["prompt"],
    }
    duplicate_started_ns = time.perf_counter_ns()
    duplicate_result = subprocess.run(
        [str(watcher), "codex-user-prompt-hook"],
        input=_stable_json(duplicate),
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )
    duplicate_completed_ns = time.perf_counter_ns()
    capture_completed_ns = duplicate_completed_ns
    if duplicate_result.returncode != 0 or json.loads(duplicate_result.stdout) != {}:
        raise RuntimeError(duplicate_result.stderr or "installed duplicate prompt hook failed")
    try:
        wake_ns, wake_line = observed.get(timeout=5)
    except queue.Empty as exc:
        waiter.terminate()
        raise RuntimeError("installed prompt wake did not arrive") from exc
    reader.join(timeout=1)
    returncode = waiter.wait(timeout=5)
    assert waiter.stderr is not None
    error = waiter.stderr.read(MAX_LINE_BYTES + 1)
    if returncode != 0:
        raise RuntimeError(error or "installed supervisor failed")
    wake = json.loads(wake_line)
    if wake.get("event") != "user-message" or wake.get("priority") != "user":
        raise RuntimeError("installed prompt wake did not preserve user priority")
    return {
        "hook_ms": hook_durations,
        "hook_batch_ms": (capture_completed_ns - capture_started_ns) / 1_000_000,
        "first_hook_to_wake_ms": (wake_ns - first_started_ns) / 1_000_000,
        "first_hook_process_ms": (first_completed_ns - first_started_ns) / 1_000_000,
        "wake_minus_hook_exit_ms": (wake_ns - first_completed_ns) / 1_000_000,
        "duplicate_hook_ms": (duplicate_completed_ns - duplicate_started_ns) / 1_000_000,
    }


def _fixture_snapshot(
    league: Path, state: Path, expected_count: int
) -> tuple[str, list[str]]:
    result = subprocess.run(
        [
            str(league),
            "--state-root",
            str(state),
            "request",
            "untriaged",
            "--owner-agent-id",
            SHOTCALLER_ID,
            "--limit",
            str(expected_count),
            "--max-bytes",
            "1000000",
        ],
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    payload = json.loads(result.stdout)
    prompts = payload["result"]["prompts"]
    if len(prompts) != expected_count:
        raise RuntimeError("installed hook capture count does not match the batch")
    return (
        _sha256_bytes(_stable_json(prompts).encode("utf-8")),
        [str(prompt["body"]) for prompt in prompts],
    )


def _turn(
    state: Path,
    cases: Sequence[dict[str, Any]],
    arm: str,
    model_root: Path,
    args: argparse.Namespace,
    model_runner: ModelRunner,
) -> dict[str, Any]:
    command = [
        str(args.league_command),
        "--state-root",
        str(state),
        "request",
        "turn",
        "--owner-agent-id",
        SHOTCALLER_ID,
        "--at",
        AT,
        "--limit",
        str(len(cases)),
    ]
    started_ns = time.perf_counter_ns()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        spawned_ns = time.perf_counter_ns()
        if process.stdin is None:
            raise RuntimeError("League turn input pipe is unavailable")
        deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
        intake_line, intake_ns = _read_process_line(process, deadline)
        parse_started_ns = time.perf_counter_ns()
        intake = json.loads(intake_line)
        parse_completed_ns = time.perf_counter_ns()
        if intake["result"]["returned_count"] != len(cases):
            raise RuntimeError("League turn intake count differs from the frozen batch")
        if [row["body"] for row in intake["result"]["prompts"]] != [row["prompt"] for row in cases]:
            raise RuntimeError("League turn changed frozen prompt order or bytes")
        if arm == "on":
            semantic, model_metrics = model_runner(cases, model_root, args)
        else:
            precomputed = _stable_json(_gold_payload(cases))
            off_parse_started_ns = time.perf_counter_ns()
            semantic = json.loads(precomputed)
            off_parse_completed_ns = time.perf_counter_ns()
            model_metrics = {
                "model_process_startup_ms": 0.0,
                "semantic_model_ms": 0.0,
                "model_completion_tail_ms": 0.0,
                "model_total_ms": 0.0,
                "model_process_exit_tail_ms": 0.0,
                "model_wall_ms": 0.0,
                "decision_json_parse_ms": (off_parse_completed_ns - off_parse_started_ns) / 1_000_000,
            }
        _validate_model_payload(semantic, cases)
        accuracy = _accuracy(semantic, cases)
        league_semantic = _league_payload(semantic)
        league_semantic["candidate_inventory_digest"] = intake["result"][
            "candidate_inventory"
        ]["digest"]
        encode_started_ns = time.perf_counter_ns()
        begin_encoded = (_stable_json(league_semantic) + "\n").encode("utf-8")
        encode_completed_ns = time.perf_counter_ns()
        handoff_started_ns = time.perf_counter_ns()
        process.stdin.write(begin_encoded)
        process.stdin.flush()
        handoff_completed_ns = time.perf_counter_ns()
        begun_line, begun_ns = _read_process_line(process, deadline)
        begun_parse_started_ns = time.perf_counter_ns()
        begun = json.loads(begun_line)
        begun_parse_completed_ns = time.perf_counter_ns()
        if not begun.get("ok"):
            error_value = begun.get("error", {})
            raise RuntimeError(
                "League turn begin refused: "
                f"{error_value.get('code', 'unknown')}: "
                f"{error_value.get('message', 'no message')}"
            )
        if begun["result"]["phase"] != "begun" or process.poll() is not None:
            raise RuntimeError("League turn begin phase failed")
        new_count = sum(
            item["disposition"] == "new_request"
            for decision in league_semantic["decisions"]
            for item in decision["items"]
        )
        actions = {
            "actions": [
                {
                    "kind": "answer",
                    "request_index": index,
                    "content": f"Synthetic benchmark answer {index}",
                    "resolution_summary": f"Completed synthetic benchmark request {index}",
                }
                for index in range(1, new_count + 1)
            ]
        }
        commit_encode_started_ns = time.perf_counter_ns()
        commit_encoded = (_stable_json(actions) + "\n").encode("utf-8")
        commit_encode_completed_ns = time.perf_counter_ns()
        process.stdin.write(commit_encoded)
        process.stdin.flush()
        committed_line, committed_ns = _read_process_line(process, deadline)
        committed_parse_started_ns = time.perf_counter_ns()
        committed = json.loads(committed_line)
        committed_parse_completed_ns = time.perf_counter_ns()
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        completed_ns = time.perf_counter_ns()
        errors.seek(0)
        error = errors.read(MAX_LINE_BYTES + 1)
    if returncode != 0 or committed["result"]["phase"] != "committed":
        raise RuntimeError(error.decode("utf-8", errors="replace") or "League turn commit failed")
    return {
        "arm": arm,
        "process_startup_ms": (spawned_ns - started_ns) / 1_000_000,
        "league_intake_ms": (intake_ns - spawned_ns) / 1_000_000,
        "first_output_ms": (intake_ns - started_ns) / 1_000_000,
        **model_metrics,
        "decision_json_serialize_ms": (encode_completed_ns - encode_started_ns) / 1_000_000,
        "decision_handoff_ms": (handoff_completed_ns - handoff_started_ns) / 1_000_000,
        "sqlite_dedup_begin_roundtrip_ms": (begun_ns - handoff_completed_ns) / 1_000_000,
        "commit_json_serialize_ms": (commit_encode_completed_ns - commit_encode_started_ns) / 1_000_000,
        "sqlite_final_commit_roundtrip_ms": (committed_ns - commit_encode_completed_ns) / 1_000_000,
        "league_json_parse_ms": (
            (parse_completed_ns - parse_started_ns)
            + (begun_parse_completed_ns - begun_parse_started_ns)
            + (committed_parse_completed_ns - committed_parse_started_ns)
        ) / 1_000_000,
        "one_process_total_ms": (completed_ns - started_ns) / 1_000_000,
        "league_processes": 1,
        "semantic_model_processes": 1 if arm == "on" else 0,
        "accuracy": accuracy,
    }


def _identity(args: argparse.Namespace, corpus_sha256: str) -> dict[str, Any]:
    source_head = _command_output(["git", "rev-parse", "HEAD"], cwd=ROOT)
    source_tree = _command_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT)
    if source_head != args.expected_source_revision:
        raise RuntimeError("source HEAD differs from the required benchmark revision")
    candidate_version = _command_output([str(args.league_command), "--version"])
    installed_version = _command_output([str(args.installed_reference_command), "--version"])
    codex_version = _command_output([str(args.codex_command), "--version"]).splitlines()[-1]
    candidate_root = args.league_command.resolve().parents[1]
    installed_root = args.installed_reference_command.resolve().parents[1]
    relevant = (
        "bin/league",
        "bin/agent-watcher",
        "src/league/cli.py",
        "src/league/canonical_watcher.py",
        "src/league/storage_request.py",
        "src/league/sqlite_request_ops.py",
        "src/league/sqlite_store.py",
    )
    source_hashes = {name: _sha256_file(ROOT / name) for name in relevant}
    candidate_hashes = {name: _sha256_file(candidate_root / name) for name in relevant}
    installed_hashes = {name: _sha256_file(installed_root / name) for name in relevant}
    candidate_source_parity = {
        name: candidate_hashes[name] == source_hashes[name] for name in relevant
    }
    installed_source_parity = {
        name: installed_hashes[name] == source_hashes[name] for name in relevant
    }
    if candidate_version != f"league {args.expected_candidate_version}":
        raise RuntimeError("candidate version differs from the exact expected identity")
    if installed_version != f"league {args.expected_installed_version}":
        raise RuntimeError("installed reference version differs from the exact expected identity")
    if args.candidate_kind == "source" and (
        candidate_root != ROOT or not all(candidate_source_parity.values())
    ):
        raise RuntimeError("source candidate bytes do not match the exact benchmark checkout")
    if args.candidate_kind == "installed" and (
        args.league_command.resolve() != args.installed_reference_command.resolve()
    ):
        raise RuntimeError("installed candidate does not resolve to the installed reference")
    return {
        "source_revision": source_head,
        "source_tree": source_tree,
        "source_version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "candidate_kind": args.candidate_kind,
        "candidate_version": candidate_version.removeprefix("league "),
        "candidate_relevant_sha256": candidate_hashes,
        "candidate_source_parity": candidate_source_parity,
        "installed_reference_version": installed_version.removeprefix("league "),
        "installed_reference_revision": args.installed_reference_revision,
        "installed_reference_relevant_sha256": installed_hashes,
        "installed_reference_source_parity": installed_source_parity,
        "loaded_sqlite_version": sqlite3.sqlite_version,
        "codex_version": codex_version.removeprefix("codex-cli "),
        "requested_model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "corpus_sha256": corpus_sha256,
        "output_schema_sha256": _sha256_file(args.output_schema),
    }


def _pair(
    parent: Path,
    cases: Sequence[dict[str, Any]],
    thermal: str,
    batch_size: int,
    sample: int,
    args: argparse.Namespace,
    model_runner: ModelRunner,
) -> dict[str, Any]:
    pair_root = parent / f"{thermal}-{batch_size}-{sample:02d}"
    pair_root.mkdir(parents=True)
    state, store, clock = create_context(pair_root, "base")
    journal_mode = store.policy.journal_mode
    store.register_runtime(
        RuntimeRegistrationCommand(
            runtime_instance_id=GAREN_RUNTIME_TWO,
            actor_agent_id=SHOTCALLER_ID,
            harness_kind="codex-thread",
            backend_kind="herdr",
            session_ref=f"session:{GAREN_RUNTIME_TWO}",
            endpoint="synthetic:garen:two",
            runtime_generation="generation:garen:two",
            status="closed",
            verified=False,
            at=clock.now(),
        )
    )
    store.close()
    hook = _capture_with_hook(
        pair_root, state, cases, args.league_command, args.watcher_command
    )
    fixture_digest, ordered_bodies = _fixture_snapshot(
        args.league_command, state, len(cases)
    )
    by_body = {str(case["prompt"]): case for case in cases}
    if len(by_body) != len(cases) or any(body not in by_body for body in ordered_bodies):
        raise RuntimeError("installed intake could not be reconciled to the frozen corpus")
    ordered_cases = [by_body[body] for body in ordered_bodies]
    arm_roots = {name: pair_root / name for name in ("off", "on")}
    for target in arm_roots.values():
        shutil.copytree(state, target)
    order = ["off", "on"]
    random.Random(args.seed + batch_size * 1000 + sample + (10_000 if thermal == "warm" else 0)).shuffle(order)
    measurements: dict[str, dict[str, Any]] = {}
    for arm in order:
        model_root = pair_root / f"model-{arm}"
        model_root.mkdir()
        measurements[arm] = _turn(
            arm_roots[arm], ordered_cases, arm, model_root, args, model_runner
        )
    return {
        "thermal": thermal,
        "batch_size": batch_size,
        "sample": sample,
        "pair_order": order,
        "fixture_digest": fixture_digest,
        "journal_mode": journal_mode,
        "hook": hook,
        "off": measurements["off"],
        "on": measurements["on"],
    }


def _aggregate(pairs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    metrics = (
        "first_output_ms",
        "one_process_total_ms",
        "model_process_startup_ms",
        "semantic_model_ms",
        "model_total_ms",
        "model_process_exit_tail_ms",
        "model_wall_ms",
        "decision_json_parse_ms",
        "decision_json_serialize_ms",
        "decision_handoff_ms",
        "sqlite_dedup_begin_roundtrip_ms",
        "sqlite_final_commit_roundtrip_ms",
        "league_json_parse_ms",
    )
    arms: dict[str, Any] = {}
    for arm in ("off", "on"):
        arms[arm] = {
            metric: _summary([float(pair[arm][metric]) for pair in pairs])
            for metric in metrics
        }
    delta = {
        metric: _summary([float(pair["on"][metric]) - float(pair["off"][metric]) for pair in pairs])
        for metric in metrics
    }
    hook_values = [value for pair in pairs for value in pair["hook"]["hook_ms"]]
    hook = {
        "per_prompt_hook_ms": _summary(hook_values),
        "batch_capture_ms": _summary([pair["hook"]["hook_batch_ms"] for pair in pairs]),
        "first_hook_to_wake_ms": _summary([pair["hook"]["first_hook_to_wake_ms"] for pair in pairs]),
        "first_hook_process_ms": _summary([pair["hook"]["first_hook_process_ms"] for pair in pairs]),
        "duplicate_hook_ms": _summary([pair["hook"]["duplicate_hook_ms"] for pair in pairs]),
    }
    accuracy_total = sum(pair["on"]["accuracy"]["total"] for pair in pairs)
    accuracy_correct = sum(pair["on"]["accuracy"]["correct"] for pair in pairs)
    mismatches = sorted(
        {
            case_id
            for pair in pairs
            for case_id in pair["on"]["accuracy"]["mismatch_case_ids"]
        }
    )
    return {
        "samples": len(pairs),
        "arms": arms,
        "paired_delta_on_minus_off": delta,
        "hook_capture_wake": hook,
        "triage_on_accuracy": {
            "correct": accuracy_correct,
            "total": accuracy_total,
            "accuracy": round(accuracy_correct / accuracy_total, 6),
            "mismatch_case_ids": mismatches,
        },
    }


def run_benchmark(
    args: argparse.Namespace, *, model_runner: ModelRunner = _codex_model_runner
) -> dict[str, Any]:
    cases, corpus_sha256 = _load_corpus(args.corpus)
    identity = _identity(args, corpus_sha256)
    cells: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="league-66-semantic-ablation-") as temporary:
        parent = Path(temporary)
        for thermal in args.thermal_states:
            for batch_size in args.batch_sizes:
                if thermal == "warm":
                    warm_cases = _batch(cases, batch_size, -1, args.seed)
                    _pair(parent / "warmups", warm_cases, thermal, batch_size, -1, args, model_runner)
                measured = [
                    _pair(
                        parent,
                        _batch(cases, batch_size, sample, args.seed),
                        thermal,
                        batch_size,
                        sample,
                        args,
                        model_runner,
                    )
                    for sample in range(args.samples)
                ]
                if len({pair["journal_mode"] for pair in measured}) != 1:
                    raise RuntimeError("paired samples selected different journal modes")
                cells.append(
                    {
                        "thermal": thermal,
                        "batch_size": batch_size,
                        "fixture_digests": sorted({pair["fixture_digest"] for pair in measured}),
                        "journal_mode": measured[0]["journal_mode"],
                        "summary": _aggregate(measured),
                        "pairs": measured,
                    }
                )
    return {
        "schema": "league.semantic-triage-ablation.v1",
        "status": "measured",
        "scope": {
            "triage_off_is_diagnostic_only": True,
            "triage_on_is_release_contract": True,
            "live_state_used": False,
            "temporary_synthetic_roots_only": True,
            "cold_definition": "new League and Codex processes with no benchmark warm-up",
            "warm_definition": "one unmeasured same-batch pair warms host/provider paths; measured turns still use new processes",
            "semantic_model_interval": "Codex JSONL turn.started to completed agent_message",
            "schema_cardinality": "temporary derived schema fixes the decision count to the exact batch",
            "sqlite_interval": "decision handoff completion to installed begun receipt; includes installed parse, transaction, and response encoding",
        },
        "identity": identity,
        "matrix": {
            "samples_per_cell": args.samples,
            "batch_sizes": args.batch_sizes,
            "thermal_states": args.thermal_states,
            "seed": args.seed,
            "pair_order": "deterministic randomized OFF/ON order",
            "corpus_cases": len(cases),
        },
        "cells": cells,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--batch-sizes", default="1,6,25")
    parser.add_argument("--thermal-states", default="cold,warm")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--corpus", type=Path, default=ROOT / "tests" / "fixtures" / "semantic_triage_corpus.v1.json")
    parser.add_argument("--output-schema", type=Path, default=ROOT / "schema" / "league-semantic-triage-batch.schema.json")
    parser.add_argument("--league-command", type=Path, required=True)
    parser.add_argument("--watcher-command", type=Path, required=True)
    parser.add_argument("--candidate-kind", choices=("installed", "source"), required=True)
    parser.add_argument("--installed-reference-command", type=Path, required=True)
    parser.add_argument("--installed-reference-revision", required=True)
    parser.add_argument("--expected-candidate-version", required=True)
    parser.add_argument("--expected-installed-version", required=True)
    parser.add_argument("--codex-command", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--reasoning-effort", default="xhigh")
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.batch_sizes = [int(value) for value in args.batch_sizes.split(",")]
    args.thermal_states = args.thermal_states.split(",")
    if not 1 <= args.samples <= 30:
        raise SystemExit("--samples must be between 1 and 30")
    if not args.batch_sizes or any(value not in {1, 6, 25} for value in args.batch_sizes):
        raise SystemExit("--batch-sizes must contain only 1,6,25")
    if not args.thermal_states or any(value not in {"cold", "warm"} for value in args.thermal_states):
        raise SystemExit("--thermal-states must contain only cold,warm")
    result = run_benchmark(args)
    encoded = _stable_json(result) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
