#!/usr/bin/env python3
"""Focused authority, apply, and rollback checks for the live executor."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.livecutover import run_live_cutover  # noqa: E402
from league.precutover import run_pre_cutover  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from test_pre_cutover import fixture_plan, write_json  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-live-cutover-") as temporary:
        root = Path(temporary)
        fixture = fixture_plan(root / "fixture")
        acceptance = root / "acceptance"
        acceptance.mkdir()
        sentinel = root / "sentinel"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        config = root / "config.json"
        write_json(config, {"schema": "league.caller-config-sentinel.v1"})
        result = run_pre_cutover(
            acceptance,
            "focused-live",
            plan_path=fixture["plan_path"],
            sentinel_paths=(sentinel,),
            config_sentinel=config,
            process_sentinel=fixture["processes"],
        )
        receipt = Path(result["home"]) / "precutover-receipt.json"
        digest = result["mutation_manifest"]["manifest_sha256"]
        applied = run_live_cutover(
            acceptance,
            "focused-live",
            plan_path=fixture["plan_path"],
            authority_receipt=receipt,
            authority_digest=digest,
            source_root=ROOT,
        )
        assert applied["state"] == "completed"
        assert Path(fixture["plan"]["proposed"]["state_root"]).is_dir()
        assert Path(fixture["plan"]["proposed"]["writer_pointer"]).is_file()
        assert json.loads(
            Path(fixture["plan"]["proposed"]["writer_pointer"]).read_text()
        )["writer"] == "sqlite"
        try:
            run_live_cutover(
                acceptance,
                "wrong-authority",
                plan_path=fixture["plan_path"],
                authority_receipt=receipt,
                authority_digest="0" * 64,
                source_root=ROOT,
            )
        except StorageRefusal as exc:
            assert exc.code == "cutover_authority_invalid"
        else:
            raise AssertionError("wrong authority unexpectedly applied")
    print("PASS: authority-bound live cutover apply and refusal")


if __name__ == "__main__":
    main()
