#!/usr/bin/env python3
"""Focused reproducibility and command-budget check for the turn benchmark."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "benchmark_request_turn.py"),
            "--samples",
            "2",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = json.loads(completed.stdout)
    assert result["schema"] == "league.request-turn-comparison.v2"
    assert result["workload"]["item_count"] == 6
    assert result["retired_json"]["command_count_per_turn"] == 7
    assert result["retired_json"]["per_prompt_shellouts"] == 6
    assert result["one_process_sqlite"]["process_launches_per_turn"] == 1
    assert result["one_process_sqlite"]["command_count_per_turn"] == 1
    assert result["one_process_sqlite"]["per_prompt_shellouts"] == 0
    assert result["normal_turn_budget"] == {
        "request_turn_processes": 1,
        "per_prompt_status_unresolved_supervise_shellouts": 0,
        "maximum_phase_output_bytes": 1_100_000,
    }
    for path in ("retired_json", "one_process_sqlite"):
        assert set(result[path]["phases"]) == {
            "process_startup",
            "intake",
            "begin",
            "commit",
            "total",
        }
        for phase in result[path]["phases"].values():
            assert phase["median_ms"] >= 0 and phase["p95_ms"] >= phase["median_ms"]
        assert result[path]["maximum_phase_output_bytes"] <= 1_100_000
    print("PASS: retired JSON and one-process SQLite timing is reproducible and budgeted")


if __name__ == "__main__":
    main()
