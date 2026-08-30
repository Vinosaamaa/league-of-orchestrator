#!/usr/bin/env python3
"""Focused paired semantic-triage benchmark harness coverage."""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_semantic_triage_ablation.py"
SPEC = importlib.util.spec_from_file_location("semantic_triage_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)


def _fake_model(cases, model_root, args):
    assert model_root.is_dir()
    assert args.model == "synthetic-model"
    return BENCHMARK._gold_payload(cases), {
        "model_process_startup_ms": 1.0,
        "semantic_model_ms": 2.0,
        "model_completion_tail_ms": 0.5,
        "model_total_ms": 3.5,
        "model_process_exit_tail_ms": 0.25,
        "model_wall_ms": 3.75,
        "decision_json_parse_ms": 0.1,
    }


def main() -> None:
    readiness_source = inspect.getsource(BENCHMARK._wait_for_supervisor_ready)
    assert "watcher_readiness" in readiness_source and '"export"' not in readiness_source
    for function in (
        BENCHMARK._codex_model_runner,
        BENCHMARK._capture_with_hook,
        BENCHMARK._turn,
    ):
        source = inspect.getsource(function)
        assert "finally:" in source and "_terminate_and_reap" in source
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    BENCHMARK._terminate_and_reap(sleeper)
    assert sleeper.poll() is not None
    assert all(
        stream is None or stream.closed
        for stream in (sleeper.stdin, sleeper.stdout, sleeper.stderr)
    )

    corpus = ROOT / "tests" / "fixtures" / "semantic_triage_corpus.v1.json"
    schema = ROOT / "schema" / "league-semantic-triage-batch.schema.json"
    cases, digest = BENCHMARK._load_corpus(corpus)
    assert len(cases) == 120 and len(digest) == 64
    assert len({row["prompt"] for row in cases}) == 120
    assert json.loads(schema.read_text(encoding="utf-8"))["additionalProperties"] is False
    selected = BENCHMARK._batch(cases, 25, 0, BENCHMARK.DEFAULT_SEED)
    imperfect = json.loads(json.dumps(BENCHMARK._gold_payload(selected)))
    imperfect["decisions"][0]["items"][0]["disposition"] = "context"
    imperfect["decisions"][0]["plan"] = None
    accuracy = BENCHMARK._accuracy(imperfect, selected)
    assert accuracy["correct"] == 24 and accuracy["mismatch_case_ids"] == [selected[0]["id"]]
    args = argparse.Namespace(
        league_command=ROOT / "bin" / "league",
        watcher_command=ROOT / "bin" / "agent-watcher",
        codex_command=Path("/synthetic/codex"),
        output_schema=schema,
        model="synthetic-model",
        reasoning_effort="xhigh",
        seed=BENCHMARK.DEFAULT_SEED,
    )
    with tempfile.TemporaryDirectory(prefix="league-semantic-benchmark-test-") as temporary:
        pair = BENCHMARK._pair(
            Path(temporary),
            selected,
            "cold",
            25,
            0,
            args,
            _fake_model,
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
    summary = BENCHMARK._aggregate([pair])
    assert summary["samples"] == 1
    assert summary["triage_on_accuracy"]["accuracy"] == 1.0
    assert summary["paired_delta_on_minus_off"]["semantic_model_ms"]["median_ms"] == 2.0
    print(
        "PASS: paired OFF/ON harness preserves bounded readiness, child cleanup, "
        "hook, fixture, one-process, journal, timing, and accuracy contracts"
    )


if __name__ == "__main__":
    main()
