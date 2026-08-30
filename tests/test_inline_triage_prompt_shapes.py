#!/usr/bin/env python3
"""Focused smoke for the 3x3 inline prompt-shape benchmark."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_inline_triage_prompt_shapes.py"
SPEC = importlib.util.spec_from_file_location("inline_triage_prompt_shapes", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)


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

    with tempfile.TemporaryDirectory(prefix="league-inline-shape-test-") as temporary:
        output = Path(temporary) / "receipt.json"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/benchmark_inline_triage_prompt_shapes.py"),
                "--samples",
                "1",
                "--output",
                str(output),
            ],
            check=True,
            timeout=120,
        )
        result = json.loads(output.read_text())
    assert result["schema"] == "league.inline-triage-prompt-shapes.v1"
    assert len(result["cells"]) == 27
    assert {(row["size"], row["intent_count"]) for row in result["cells"]} == {
        (size, count) for size in ("short", "medium", "long") for count in (1, 3, 6)
    }
    assert all(row["separate_classifier_processes"] == 0 for row in result["cells"])
    assert all(row["false_merges"] == 0 and row["missed_duplicates"] == 0 for row in result["cells"])
    assert all(row["exact_event_idempotent"] for row in result["cells"])
    print(
        "PASS: 3x3 inline prompt-shape matrix covers cold/exact/paraphrase arms "
        "with zero classifier processes and bounded child cleanup"
    )


if __name__ == "__main__":
    main()
