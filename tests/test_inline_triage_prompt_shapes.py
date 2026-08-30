#!/usr/bin/env python3
"""Focused smoke for the 3x3 inline prompt-shape benchmark."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
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
    print("PASS: 3x3 inline prompt-shape matrix covers cold/exact/paraphrase arms with zero classifier processes")


if __name__ == "__main__":
    main()
