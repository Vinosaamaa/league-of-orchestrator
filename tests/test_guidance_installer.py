#!/usr/bin/env python3
"""Explicit-root staging tests for the source-managed cross-harness guide."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from league.guidance import SUPPORTED_HARNESSES, stage_guidance  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402


def main() -> None:
    source = ROOT / "global-agent-instructions" / "shared-AGENTS.md"
    original = source.read_bytes()
    assert b"final rendered payload" in original
    with tempfile.TemporaryDirectory(prefix="league-guidance-stage-") as temporary:
        root = Path(temporary)
        for harness in sorted(SUPPORTED_HARNESSES):
            destination = root / harness
            destination.mkdir()
            receipt = stage_guidance(source.resolve(), harness, destination.resolve())
            assert (destination / "AGENTS.md").read_bytes() == original
            assert receipt["source_sha256"] == hashlib.sha256(original).hexdigest()
            assert receipt["installed_sha256"] == receipt["source_sha256"]
            assert receipt["target_included"] is False
        replacement = root / "replacement"
        replacement.mkdir()
        (replacement / "AGENTS.md").write_text("prior synthetic guidance\n", encoding="utf-8")
        receipt = stage_guidance(source.resolve(), "codex", replacement.resolve())
        assert receipt["rollback_available"] is True
        backups = list(replacement.glob(".AGENTS.md.league-backup-*"))
        assert len(backups) == 1 and backups[0].read_text() == "prior synthetic guidance\n"
        try:
            stage_guidance(source.resolve(), "unsupported", replacement.resolve())
        except StorageRefusal as exc:
            assert exc.code == "unsupported_harness"
        else:
            raise AssertionError("unsupported harness was staged")
    assert source.read_bytes() == original
    print("PASS: explicit-root Codex/Cursor/Pi staging, parity, backup, and no global mutation")


if __name__ == "__main__":
    main()
