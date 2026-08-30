#!/usr/bin/env python3
"""Small subprocess smoke for the paired semantic-triage benchmark."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_semantic_triage_ablation.py"
SPEC = importlib.util.spec_from_file_location("semantic_triage_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)


def _fake_model(cases, model_root, args):
    assert model_root.is_dir()
    assert args.model == "synthetic-model"
    return BENCHMARK.gold_payload(cases), {
        "model_process_startup_ms": 1.0,
        "semantic_model_ms": 2.0,
        "model_completion_tail_ms": 0.5,
        "model_total_ms": 3.5,
        "model_process_exit_tail_ms": 0.25,
        "model_wall_ms": 3.75,
        "decision_json_parse_ms": 0.1,
    }


def main() -> None:
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    BENCHMARK.terminate_and_reap(sleeper)
    assert sleeper.poll() is not None
    assert all(
        stream is None or stream.closed
        for stream in (sleeper.stdin, sleeper.stdout, sleeper.stderr)
    )
    stalled = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        try:
            BENCHMARK.write_process_input(
                stalled, b"x" * 1_100_000, time.monotonic() + 0.05
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("stalled child input did not honor the deadline")
    finally:
        BENCHMARK.terminate_and_reap(stalled)

    corpus = ROOT / "tests" / "fixtures" / "semantic_triage_corpus.v1.json"
    cases, _ = BENCHMARK.load_corpus(corpus)
    selected = BENCHMARK.select_batch(cases, 25, 0, BENCHMARK.DEFAULT_SEED)
    args = argparse.Namespace(
        league_command=ROOT / "bin" / "league",
        watcher_command=ROOT / "bin" / "agent-watcher",
        codex_command=Path("/synthetic/codex"),
        output_schema=ROOT / "schema" / "league-semantic-triage-batch.schema.json",
        model="synthetic-model",
        reasoning_effort="xhigh",
        seed=BENCHMARK.DEFAULT_SEED,
    )
    with tempfile.TemporaryDirectory(prefix="league-semantic-benchmark-smoke-") as temporary:
        pair = BENCHMARK.run_pair(
            Path(temporary), selected, "cold", 25, 0, args, _fake_model
        )
    assert pair["journal_mode"] == "WAL"
    assert len(pair["fixture_digest"]) == 64
    assert pair["pair_order"] in (["off", "on"], ["on", "off"])
    assert len(pair["hook"]["hook_ms"]) == 25
    assert pair["hook"]["first_hook_to_wake_ms"] >= 0
    assert pair["off"]["semantic_model_ms"] == 0
    assert pair["on"]["semantic_model_ms"] == 2.0
    assert pair["off"]["accuracy"]["accuracy"] == 1.0
    assert pair["on"]["accuracy"]["accuracy"] == 1.0
    assert pair["off"]["league_processes"] == pair["on"]["league_processes"] == 1
    summary = BENCHMARK.aggregate_samples([pair])
    assert summary["samples"] == 1
    assert summary["triage_on_accuracy"]["accuracy"] == 1.0
    assert summary["paired_delta_on_minus_off"]["semantic_model_ms"]["median_ms"] == 2.0
    print(
        "PASS: one paired OFF/ON subprocess smoke preserves bounded readiness, "
        "child cleanup, hook, fixture, one-process, journal, timing, and accuracy contracts"
    )


if __name__ == "__main__":
    main()
