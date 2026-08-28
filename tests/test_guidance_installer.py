#!/usr/bin/env python3
"""Explicit-root staging tests for the source-managed cross-harness guide."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from league.guidance import MAX_GUIDANCE_BYTES, SUPPORTED_HARNESSES, stage_guidance  # noqa: E402
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
        oversized = root / "oversized"
        oversized.mkdir()
        oversized_target = oversized / "AGENTS.md"
        oversized_target.write_bytes(b"x" * (MAX_GUIDANCE_BYTES + 1))
        try:
            stage_guidance(source.resolve(), "codex", oversized.resolve())
        except StorageRefusal as exc:
            assert exc.code == "guidance_target_unsafe"
        else:
            raise AssertionError("oversized existing guidance was read for backup")
        assert oversized_target.stat().st_size == MAX_GUIDANCE_BYTES + 1
        collision = root / "collision"
        collision.mkdir()
        collision_target = collision / "AGENTS.md"
        collision_target.write_text("prior synthetic guidance\n", encoding="utf-8")
        staging_file = collision / ".AGENTS.md.league-stage"
        staging_file.write_text("unrelated interrupted stage\n", encoding="utf-8")
        try:
            stage_guidance(source.resolve(), "codex", collision.resolve())
        except StorageRefusal as exc:
            assert exc.code == "guidance_stage_collision"
        else:
            raise AssertionError("staging collision mutated the destination")
        assert collision_target.read_text(encoding="utf-8") == "prior synthetic guidance\n"
        assert list(collision.glob(".AGENTS.md.league-backup-*")) == []
        assert staging_file.read_text(encoding="utf-8") == "unrelated interrupted stage\n"
        oversized_source = root / "oversized-source.md"
        oversized_source.write_bytes(b"x" * (MAX_GUIDANCE_BYTES + 1))
        try:
            stage_guidance(oversized_source.resolve(), "codex", (root / "codex").resolve())
        except StorageRefusal as exc:
            assert exc.code == "invalid_guidance_source"
        else:
            raise AssertionError("oversized guidance source was read for staging")
    assert source.read_bytes() == original
    print("PASS: explicit-root Codex/Cursor/Pi staging, parity, backup, and no global mutation")


if __name__ == "__main__":
    main()
