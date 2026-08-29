#!/usr/bin/env python3
"""Synthetic command-level timing for chatty versus one-process request turns."""

from __future__ import annotations

import hashlib
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

from league.cli import (  # noqa: E402
    _mechanize_turn_decisions,
    _turn_commit_actions,
    _turn_dispatch_plans,
)
from request_lifecycle_fixture import GAREN_RUNTIME, create_context  # noqa: E402
from storage_fixture import SHOTCALLER_ID  # noqa: E402


PROMPT_COUNT = 6
AT = "2026-01-01T01:00:00Z"
COMMIT_AT = "2026-01-01T01:01:00Z"
LEASED_UNTIL = "2026-01-01T02:00:00Z"
LEAGUE = ROOT / "bin/league"


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


def _decision(prefix: str, ordinal: int) -> dict[str, object]:
    prompt_id = f"prompt:{prefix}:{ordinal}"
    request_id = f"request:{prefix}:{ordinal}"
    return {
        "prompt_id": prompt_id,
        "items": [
            {
                "prompt_item_id": f"item:{prefix}:{ordinal}",
                "ordinal": 1,
                "summary": f"Handle synthetic request {ordinal}",
                "disposition": "new_request",
                "request_id": request_id,
            }
        ],
    }


def _plan(prefix: str, ordinal: int) -> dict[str, object]:
    request_id = f"request:{prefix}:{ordinal}"
    return {
        "request_id": request_id,
        "runtime_instance_id": GAREN_RUNTIME,
        "claim_token": f"claim:{prefix}:{ordinal}",
        "leased_until": LEASED_UNTIL,
        "dispatch_id": f"dispatch:{prefix}:{ordinal}",
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


def _answer(prefix: str, ordinal: int) -> dict[str, object]:
    response = f"Synthetic response {ordinal}"
    return {
        "kind": "answer",
        "request_id": f"request:{prefix}:{ordinal}",
        "claim_token": f"claim:{prefix}:{ordinal}",
        "expected_version": 2,
        "response_ref_id": f"response:{prefix}:{ordinal}",
        "adapter_kind": "codex",
        "session_locator": f"session:{prefix}",
        "response_locator": f"turn:{prefix}:{ordinal}",
        "durability": "durable",
        "content_hash": hashlib.sha256(response.encode()).hexdigest(),
        "resolution_summary": f"Answered synthetic request {ordinal}",
        "event_id": f"event:answer:{prefix}:{ordinal}",
    }


def _run(state: Path, *arguments: str) -> dict[str, object]:
    result = subprocess.run(
        [str(LEAGUE), "--state-root", str(state), *arguments],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout or result.stderr)
    return json.loads(result.stdout)


def _cold_cli(state: Path) -> float:
    started = time.perf_counter()
    for _ in range(20):
        _run(
            state,
            "request",
            "unresolved",
            "--owner-agent-id",
            SHOTCALLER_ID,
        )
    return (time.perf_counter() - started) * 1000


def _chatty_turn(state: Path, prefix: str) -> float:
    started = time.perf_counter()
    intake = _run(
        state,
        "request",
        "untriaged",
        "--owner-agent-id",
        SHOTCALLER_ID,
    )["result"]
    for ordinal, prompt in enumerate(intake["prompts"], start=1):
        decision = _decision(prefix, ordinal)
        _run(
            state,
            "request",
            "triage",
            "--prompt-id",
            prompt["prompt_id"],
            "--items-json",
            json.dumps(decision["items"], separators=(",", ":")),
            "--at",
            AT,
        )
        _run(
            state,
            "request",
            "claim",
            "--request-id",
            f"request:{prefix}:{ordinal}",
            "--runtime-instance-id",
            GAREN_RUNTIME,
            "--claim-token",
            f"claim:{prefix}:{ordinal}",
            "--leased-until",
            LEASED_UNTIL,
            "--at",
            AT,
        )
        _run(
            state,
            "request",
            "dispatch",
            "--request-id",
            f"request:{prefix}:{ordinal}",
            "--claim-token",
            f"claim:{prefix}:{ordinal}",
            "--dispatch-id",
            f"dispatch:{prefix}:{ordinal}",
            "--work-kind",
            "short-check",
            "--requested-mode",
            "direct",
            "--pre-bounded",
            "--read-only",
            "--answer-or-routing-only",
            "--expected-minutes",
            "2",
            "--expected-task-action-calls",
            "1",
            "--at",
            AT,
        )
    for ordinal in range(1, PROMPT_COUNT + 1):
        answer = _answer(prefix, ordinal)
        arguments = [
            "request", "answer", "--request-id", answer["request_id"],
            "--claim-token", answer["claim_token"], "--expected-version", "2",
            "--response-ref-id", answer["response_ref_id"], "--adapter-kind", "codex",
            "--session-locator", answer["session_locator"], "--response-locator",
            answer["response_locator"], "--durability", "durable", "--content-hash",
            answer["content_hash"], "--resolution-summary", answer["resolution_summary"],
            "--event-id", answer["event_id"], "--at", COMMIT_AT,
        ]
        _run(state, *arguments)
    _run(state, "request", "unresolved", "--owner-agent-id", SHOTCALLER_ID)
    return (time.perf_counter() - started) * 1000


def _batched_turn(state: Path, prefix: str) -> dict[str, float]:
    started = time.perf_counter()
    process = subprocess.Popen(
        [
            str(LEAGUE), "--state-root", str(state), "request", "turn",
            "--owner-agent-id", SHOTCALLER_ID, "--at", AT,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert process.stdin is not None and process.stdout is not None
    intake = json.loads(process.stdout.readline())
    intake_at = time.perf_counter()
    if intake["result"]["returned_count"] != PROMPT_COUNT:
        raise RuntimeError("batched intake count mismatch")
    begin = {
        "decisions": [_semantic_decision(ordinal) for ordinal in range(1, PROMPT_COUNT + 1)],
        "plans": [_semantic_plan() for _ in range(PROMPT_COUNT)],
    }
    process.stdin.write(json.dumps(begin, separators=(",", ":")) + "\n")
    process.stdin.flush()
    begun = json.loads(process.stdout.readline())
    begun_at = time.perf_counter()
    if begun["result"]["phase"] != "begun":
        raise RuntimeError("batched begin failed")
    commit = {
        "actions": [_semantic_answer(ordinal) for ordinal in range(1, PROMPT_COUNT + 1)],
    }
    process.stdin.write(json.dumps(commit, separators=(",", ":")) + "\n")
    process.stdin.flush()
    final = json.loads(process.stdout.readline())
    boundary_at = time.perf_counter()
    if process.wait(timeout=30) != 0 or final["result"]["phase"] != "committed":
        raise RuntimeError("batched commit failed")
    completed_at = time.perf_counter()
    return {
        "startup_to_intake_ms": (intake_at - started) * 1000,
        "decisions_to_plan_ms": (begun_at - intake_at) * 1000,
        "commit_to_boundary_ms": (boundary_at - begun_at) * 1000,
        "boundary_to_exit_ms": (completed_at - boundary_at) * 1000,
        "total_ms": (completed_at - started) * 1000,
    }


def _database_turn(parent: Path, prefix: str) -> float:
    _, store, clock = create_context(parent, prefix)
    _capture(store, clock, prefix)
    intake = store.untriaged_intake(SHOTCALLER_ID)
    decisions, new_requests = _mechanize_turn_decisions(
        intake,
        [_semantic_decision(ordinal) for ordinal in range(1, PROMPT_COUNT + 1)],
        AT,
    )
    plans = _turn_dispatch_plans(
        [_semantic_plan() for _ in range(PROMPT_COUNT)], AT, new_requests
    )
    started = time.perf_counter()
    begun = store.begin_request_turn(
        SHOTCALLER_ID,
        tuple(item["prompt_id"] for item in intake["prompts"]),
        decisions,
        plans,
        AT,
    )
    actions = _turn_commit_actions(
        [_semantic_answer(ordinal) for ordinal in range(1, PROMPT_COUNT + 1)],
        COMMIT_AT,
        new_requests,
        begun,
    )
    store.commit_request_turn(SHOTCALLER_ID, actions, COMMIT_AT)
    store.request_turn_boundary(SHOTCALLER_ID)
    elapsed = (time.perf_counter() - started) * 1000
    store.close()
    return elapsed


def _summary(samples: list[float]) -> dict[str, float]:
    return {
        "minimum_ms": round(min(samples), 3),
        "median_ms": round(statistics.median(samples), 3),
        "maximum_ms": round(max(samples), 3),
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-turn-benchmark-") as temporary:
        root = Path(temporary)
        cold_state, cold_store, cold_clock = create_context(root, "cold")
        _capture(cold_store, cold_clock, "cold")
        cold_store.close()
        cold = _cold_cli(cold_state)
        chatty: list[float] = []
        batched: list[dict[str, float]] = []
        database: list[float] = []
        for iteration in range(3):
            prefix = f"chatty-{iteration}"
            state, store, clock = create_context(root, prefix)
            _capture(store, clock, prefix)
            store.close()
            chatty.append(_chatty_turn(state, prefix))
            prefix = f"batched-{iteration}"
            state, store, clock = create_context(root, prefix)
            _capture(store, clock, prefix)
            store.close()
            batched.append(_batched_turn(state, prefix))
            database.append(_database_turn(root, f"database-{iteration}"))
    result = {
        "schema": "league.request-turn-benchmark.v1",
        "prompt_count": PROMPT_COUNT,
        "cold_cli_unresolved": {
            "samples": 20,
            "process_launches": 20,
            "total_ms": round(cold, 3),
            "mean_ms": round(cold / 20, 3),
        },
        "chatty_turn": {"process_launches": 26, **_summary(chatty)},
        "one_process_turn": {
            "process_launches": 1,
            "startup_to_intake": _summary(
                [sample["startup_to_intake_ms"] for sample in batched]
            ),
            "decisions_to_plan": _summary(
                [sample["decisions_to_plan_ms"] for sample in batched]
            ),
            "commit_to_boundary": _summary(
                [sample["commit_to_boundary_ms"] for sample in batched]
            ),
            "boundary_to_exit": _summary(
                [sample["boundary_to_exit_ms"] for sample in batched]
            ),
            "total": _summary([sample["total_ms"] for sample in batched]),
        },
        "batch_database_phases": {"process_launches": 0, **_summary(database)},
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
