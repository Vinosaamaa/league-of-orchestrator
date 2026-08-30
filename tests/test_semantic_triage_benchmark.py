#!/usr/bin/env python3
"""Focused in-process semantic-triage benchmark contract coverage."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_semantic_triage_ablation.py"
SPEC = importlib.util.spec_from_file_location("semantic_triage_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)


def main() -> None:
    corpus = ROOT / "tests" / "fixtures" / "semantic_triage_corpus.v1.json"
    schema = ROOT / "schema" / "league-semantic-triage-batch.schema.json"
    cases, digest = BENCHMARK.load_corpus(corpus)
    assert len(cases) == 120 and len(digest) == 64
    assert len({row["prompt"] for row in cases}) == 120
    assert json.loads(schema.read_text(encoding="utf-8"))["additionalProperties"] is False
    selected = BENCHMARK.select_batch(cases, 25, 0, BENCHMARK.DEFAULT_SEED)
    imperfect = json.loads(json.dumps(BENCHMARK.gold_payload(selected)))
    imperfect["decisions"][0]["items"][0]["disposition"] = "context"
    imperfect["decisions"][0]["plan"] = None
    accuracy = BENCHMARK.score_accuracy(imperfect, selected)
    assert accuracy["correct"] == 24 and accuracy["mismatch_case_ids"] == [selected[0]["id"]]
    print(
        "PASS: semantic benchmark corpus, schema, selection, gold payload, and scoring contracts"
    )


if __name__ == "__main__":
    main()
