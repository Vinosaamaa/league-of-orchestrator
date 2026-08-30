#!/usr/bin/env python3
"""Measure the zero-second-classifier inline triage prompt-shape matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from request_lifecycle_fixture import GAREN_RUNTIME, create_context  # noqa: E402
from storage_fixture import SHOTCALLER_ID  # noqa: E402


AT = "2026-01-01T01:00:00Z"
ARMS = ("cold_empty", "preseed_exact", "preseed_paraphrase")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(values: Sequence[float]) -> dict[str, float]:
    return {
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "minimum_ms": round(min(values), 3),
        "maximum_ms": round(max(values), 3),
    }


def _terminate_and_reap(process: subprocess.Popen[Any]) -> None:
    """Bound termination and close every benchmark child pipe on every path."""

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def terminate_and_reap(process: subprocess.Popen[Any]) -> None:
    """Stable focused-test contract for bounded benchmark child cleanup."""

    _terminate_and_reap(process)


def _load(path: Path) -> tuple[list[dict[str, Any]], str]:
    encoded = path.read_bytes()
    payload = json.loads(encoded)
    if payload.get("schema") != "league.semantic-prompt-shape-matrix.v1":
        raise RuntimeError("prompt-shape corpus schema is unsupported")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != 9:
        raise RuntimeError("prompt-shape corpus must contain exactly nine cells")
    observed = {(row.get("size"), len(row.get("intents", []))) for row in cases}
    expected = {(size, count) for size in ("short", "medium", "long") for count in (1, 3, 6)}
    if observed != expected:
        raise RuntimeError("prompt-shape corpus is not the required 3x3 matrix")
    return cases, _digest(encoded)


def _seed_existing(store: Any, clock: Any, summary: str) -> None:
    prompt_id = "prompt:shape:existing"
    store.intake_prompt(
        prompt_id,
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        f"session:{GAREN_RUNTIME}",
        "turn:shape:existing",
        summary,
        clock.now(),
    )
    store.triage_prompt(
        prompt_id,
        [
            {
                "prompt_item_id": "item:shape:existing",
                "ordinal": 1,
                "summary": summary,
                "disposition": "new_request",
                "request_id": "request:shape:existing",
                "next_attention_at": None,
            }
        ],
        clock.now(),
    )


def _unique_intents(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [intent for intent in case["intents"] if "repeat_of" not in intent]


def _sideband(case: dict[str, Any], arm: str, inventory: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    unique = _unique_intents(case)
    items: list[dict[str, Any]] = []
    created = 0
    linked = 0
    for index, intent in enumerate(unique):
        if index == 0 and arm != "cold_empty":
            candidate = inventory["requests"][0]
            items.append(
                {
                    "summary": intent["summary"],
                    "disposition": "duplicate",
                    "related_request_id": candidate["request_id"],
                    "related_request_version": candidate["version"],
                }
            )
            linked += 1
        else:
            items.append({"summary": intent["summary"], "disposition": "new_request"})
            created += 1
    plan = {
        "work_kind": "question",
        "requested_mode": "direct",
        "signals": {
            "pre_bounded": True,
            "read_only": True,
            "answer_or_routing_only": True,
            "expected_minutes": 2,
            "expected_task_action_calls": 1,
        },
    }
    return (
        {
            "candidate_inventory_digest": inventory["digest"],
            "decisions": [{"items": items}],
            "plans": [plan for _ in range(created)],
        },
        {
            "expected_request_mentions": len(case["intents"]),
            "expected_ordered_items": len(unique),
            "produced_ordered_items": len(items),
            "items_collapsed": len(case["intents"]) - len(unique),
            "items_linked": linked,
            "items_created": created,
            "false_merges": 0,
            "missed_duplicates": 0,
        },
    )


def _one(case: dict[str, Any], arm: str, sample: int, league: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="league-inline-shape-") as temporary:
        state, store, clock = create_context(Path(temporary), "state")
        if arm == "preseed_exact":
            _seed_existing(store, clock, _unique_intents(case)[0]["summary"])
        elif arm == "preseed_paraphrase":
            _seed_existing(
                store,
                clock,
                "Previously opened equivalent work: " + _unique_intents(case)[0]["summary"].lower(),
            )
        prompt_id = f"prompt:shape:{case['id']}:{sample}"
        source_key = f"turn:shape:{case['id']}:{sample}"
        capture_started = time.perf_counter_ns()
        first_capture = store.intake_prompt(
            prompt_id,
            SHOTCALLER_ID,
            GAREN_RUNTIME,
            "codex",
            f"session:{GAREN_RUNTIME}",
            source_key,
            case["prompt"],
            clock.now(),
        )
        repeated_capture = store.intake_prompt(
            prompt_id,
            SHOTCALLER_ID,
            GAREN_RUNTIME,
            "codex",
            f"session:{GAREN_RUNTIME}",
            source_key,
            case["prompt"],
            clock.now(),
        )
        capture_completed = time.perf_counter_ns()
        if first_capture["idempotent"] or not repeated_capture["idempotent"]:
            raise RuntimeError("exact source-event idempotency contract failed")
        store.close()
        started = time.perf_counter_ns()
        process = subprocess.Popen(
            [
                str(league),
                "--state-root",
                str(state),
                "request",
                "turn",
                "--owner-agent-id",
                SHOTCALLER_ID,
                "--at",
                AT,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            intake = json.loads(process.stdout.readline())
            intake_completed = time.perf_counter_ns()
            if not intake.get("ok") or intake["result"]["returned_count"] != 1:
                raise RuntimeError("inline request-turn intake failed")
            sideband, accuracy = _sideband(
                case, arm, intake["result"]["candidate_inventory"]
            )
            encode_started = time.perf_counter_ns()
            encoded = (_json(sideband) + "\n").encode("utf-8")
            encode_completed = time.perf_counter_ns()
            begin_started = time.perf_counter_ns()
            process.stdin.write(encoded)
            process.stdin.flush()
            begun = json.loads(process.stdout.readline())
            begin_completed = time.perf_counter_ns()
            if not begun.get("ok"):
                raise RuntimeError(
                    f"inline begin failed: {begun.get('error', {}).get('code')}"
                )
            commit = {
                "actions": [
                    {
                        "kind": "answer",
                        "request_index": index,
                        "content": f"Synthetic inline answer {index}",
                        "resolution_summary": f"Completed inline request {index}",
                    }
                    for index in range(1, accuracy["items_created"] + 1)
                ]
            }
            commit_started = time.perf_counter_ns()
            process.stdin.write((_json(commit) + "\n").encode("utf-8"))
            process.stdin.flush()
            committed = json.loads(process.stdout.readline())
            process.wait(timeout=20)
            completed = time.perf_counter_ns()
            if process.returncode != 0 or not committed.get("ok"):
                assert process.stderr is not None
                raise RuntimeError(
                    process.stderr.read().decode("utf-8", errors="replace")
                )
            return {
                "sample": sample,
                "capture_exact_event_ms": (capture_completed - capture_started) / 1_000_000,
                "prompt_to_first_output_ms": (intake_completed - started) / 1_000_000,
                "sideband_json_serialize_ms": (encode_completed - encode_started) / 1_000_000,
                "local_validate_dedup_commit_ms": (begin_completed - begin_started) / 1_000_000,
                "final_commit_ms": (completed - commit_started) / 1_000_000,
                "total_request_turn_ms": (completed - started) / 1_000_000,
                "separate_classifier_model_ms": 0.0,
                "separate_classifier_processes": 0,
                "exact_event_idempotent": True,
                **accuracy,
            }
        finally:
            _terminate_and_reap(process)


def run(args: argparse.Namespace) -> dict[str, Any]:
    cases, corpus_digest = _load(args.corpus)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    cells: list[dict[str, Any]] = []
    for case in cases:
        for arm in ARMS:
            samples = [_one(case, arm, sample, args.league_command) for sample in range(args.samples)]
            metric_names = (
                "capture_exact_event_ms",
                "prompt_to_first_output_ms",
                "sideband_json_serialize_ms",
                "local_validate_dedup_commit_ms",
                "final_commit_ms",
                "total_request_turn_ms",
            )
            cells.append(
                {
                    "case_id": case["id"],
                    "size": case["size"],
                    "intent_count": len(case["intents"]),
                    "arm": arm,
                    "raw_chars": len(case["prompt"]),
                    "raw_bytes": len(case["prompt"].encode("utf-8")),
                    "provider_input_tokens": None,
                    "provider_input_tokens_reason": "no second provider invocation on the inline path",
                    "expected_request_mentions": samples[0]["expected_request_mentions"],
                    "expected_ordered_items": samples[0]["expected_ordered_items"],
                    "produced_ordered_items": samples[0]["produced_ordered_items"],
                    "items_collapsed": samples[0]["items_collapsed"],
                    "items_linked": samples[0]["items_linked"],
                    "items_created": samples[0]["items_created"],
                    "false_merges": sum(row["false_merges"] for row in samples),
                    "missed_duplicates": sum(row["missed_duplicates"] for row in samples),
                    "exact_event_idempotent": all(row["exact_event_idempotent"] for row in samples),
                    "separate_classifier_processes": 0,
                    "separate_classifier_model_ms": {"median_ms": 0.0, "p95_ms": 0.0},
                    "metrics": {
                        name: _summary([row[name] for row in samples]) for name in metric_names
                    },
                }
            )
    return {
        "schema": "league.inline-triage-prompt-shapes.v1",
        "identity": {
            "source_revision": revision,
            "source_version": subprocess.run(
                [str(args.league_command), "--version"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "corpus_sha256": corpus_digest,
            "samples_per_cell": args.samples,
            "process_contract": "one League request-turn process, zero classifier processes",
        },
        "limitations": {
            "active_shotcaller_semantic_time": "not separately observable from the owner-response model turn",
            "semantic_sideband": "gold sideband proves local split/link/commit mechanics, not model quality",
            "installed_live_e2e": "not run; install and live-cutover authority were not granted",
        },
        "cells": cells,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument(
        "--corpus", type=Path, default=ROOT / "tests/fixtures/semantic_prompt_shape_matrix.v1.json"
    )
    parser.add_argument("--league-command", type=Path, default=ROOT / "bin/league")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 50:
        raise SystemExit("samples must be between 1 and 50")
    result = run(args)
    encoded = (_json(result) + "\n").encode("utf-8")
    if args.output:
        args.output.write_bytes(encoded)
    else:
        sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
