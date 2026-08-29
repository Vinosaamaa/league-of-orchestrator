#!/usr/bin/env python3
"""Focused authority, apply, and rollback checks for the live executor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.livecutover import run_live_cutover, verify_legacy_archive  # noqa: E402
from league.precutover import run_pre_cutover  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from test_pre_cutover import fixture_plan, write_json  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-live-cutover-") as temporary:
        root = Path(temporary)
        fixture = fixture_plan(root / "fixture")
        write_json(fixture["hook"], {"hooks": {}})
        cursor_hooks = fixture["live"] / "config/cursor-hooks.json"
        write_json(
            cursor_hooks,
            {"version": 1, "hooks": {"sessionStart": [{"command": "keep-me"}]}},
        )
        fixture["plan"]["current_targets"].append(
            {
                "target_id": "cursor-hooks",
                "kind": "hook_config",
                "path": str(cursor_hooks),
                "required": True,
            }
        )
        fixture["plan"]["proposed"]["hooks"].append(
            {"harness": "cursor", "target": str(cursor_hooks)}
        )
        write_json(fixture["plan_path"], fixture["plan"])
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
        watcher = subprocess.run(
            [
                str(Path(fixture["plan"]["proposed"]["watcher_launcher"])),
                "--shotcaller",
                "Garen",
                "status",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "LEAGUE_WRITER_POINTER": fixture["plan"]["proposed"]["writer_pointer"],
                "LEAGUE_STATE_ROOT": fixture["plan"]["proposed"]["state_root"],
            },
        )
        assert watcher.returncode == 0, watcher.stderr
        assert json.loads(watcher.stdout)["writer"] == "sqlite"
        hook_receipts = {item["harness"]: item for item in applied["hooks"]}
        assert hook_receipts["codex"]["added"] == ["UserPromptSubmit", "Stop"]
        assert hook_receipts["cursor"]["added"] == ["beforeSubmitPrompt", "stop"]
        cursor_document = json.loads(cursor_hooks.read_text())
        assert cursor_document["hooks"]["sessionStart"] == [{"command": "keep-me"}]
        assert applied["watcher_smoke"]["status"] == "passed"
        archive = (
            Path(fixture["plan"]["proposed"]["archive_root"])
            / applied["writer_generation"]
        )
        verified = verify_legacy_archive(archive)
        assert verified["verified"] is True
        assert (archive / "RESTORE.md").is_file()
        restore = (archive / "RESTORE.md").read_text(encoding="utf-8")
        assert "acceptance archive-verify" in restore
        assert "never copy by hand" in restore
        manifest = json.loads((archive / "archive-manifest.json").read_text())
        archived = {item["target_id"] for item in manifest["entries"]}
        assert {"hooks", "cursor-hooks", "installed", "legacy", "watcher-launcher"} <= archived
        archived_installed = archive / "legacy-system/installed/bin/agent-watcher"
        archived_installed.write_bytes(archived_installed.read_bytes() + b"tampered")
        try:
            verify_legacy_archive(archive)
        except StorageRefusal as exc:
            assert exc.code == "legacy_archive_mismatch"
        else:
            raise AssertionError("tampered legacy archive unexpectedly verified")
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
