#!/usr/bin/env python3
"""Focused command, failure, sentinel, staging, and cutover acceptance tests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEAGUE = ROOT / "bin/league"
EXPECTED_MIGRATION_REPORT = "9844f64151bfbe880699700795797c52707535935234522c4d306f6625f0b91f"
EXPECTED_MIGRATION_SOURCE = "0f7d57871907fabdab99b01b39e280d6ea2d12901c4a22ea765e9bbe24241499"
EXPECTED_MIGRATION_PARITY = "4091f020741dd6251bf9aec10425cc3a248900912cad70a413d8f5664ccb85e6"
sys.path.insert(0, str(ROOT / "src"))

from league import MAX_ACCEPTANCE_SENTINEL_PATHS  # noqa: E402
from league.acceptance import (  # noqa: E402
    POINTER_STAGES,
    PROCESS_SENTINEL_SCHEMA,
    SentinelSet,
    run_acceptance,
    validate_hook_fixture,
)
from league.sqlite_store import CURRENT_SCHEMA_VERSION  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402


def refused(operation, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def live_sentinels(root: Path, name: str) -> tuple[Path, Path, Path]:
    live = root / name
    live.mkdir()
    byte_path = live / "live-bytes"
    byte_path.mkdir()
    (byte_path / "watcher.bin").write_bytes(b"synthetic-live-watcher\0unchanged\n")
    config = live / "hooks.json"
    write_json(config, {"hooks": [{"command": "synthetic-existing-handler"}]})
    processes = live / "processes.json"
    write_json(
        processes,
        {
            "schema": PROCESS_SENTINEL_SCHEMA,
            "processes": [
                {
                    "pid": 9001,
                    "process_start": "synthetic-live-start",
                    "endpoint": "synthetic://live/unchanged",
                }
            ],
        },
    )
    return byte_path, config, processes


def assert_receipt_shape(result: dict[str, object]) -> None:
    assert set(result) == {
        "schema",
        "version",
        "namespace",
        "home",
        "operation",
        "determinism",
        "sentinels",
        "migration_shadow",
        "staged_install",
        "cutover",
        "canary",
        "adapters",
        "pending_assertions",
        "runtime_claims",
    }
    assert result["schema"] == "league.acceptance-receipt.v1"
    assert result["operation"]["state"] == "completed"
    assert result["operation"]["attempt"] >= 1
    assert set(result["determinism"]) == {"clock", "ids_allocated"}


def assert_sentinel_and_migration_receipts(result: dict[str, object]) -> None:
    assert set(result["sentinels"]) == {
        "unchanged",
        "byte_paths",
        "config_sha256",
        "process_sha256",
        "process_count",
    }
    assert result["sentinels"]["unchanged"] is True
    shadow = result["migration_shadow"]
    assert shadow["dry_run"]["eligible"] is True
    assert shadow["dry_run"]["report_digest"] == EXPECTED_MIGRATION_REPORT
    assert shadow["dry_run"]["source_digest"] == EXPECTED_MIGRATION_SOURCE
    assert shadow["apply"]["applied"] is True
    assert shadow["exact_parity"] is True and shadow["legacy_unchanged"] is True
    assert shadow["parity_sha256"] == EXPECTED_MIGRATION_PARITY


def assert_staged_install_receipt(result: dict[str, object]) -> None:
    staged = result["staged_install"]
    assert set(staged) == {
        "prefix",
        "version",
        "source_release_staged_parity",
        "source_manifest_sha256",
        "release_manifest_sha256",
        "staged_manifest_sha256",
        "file_count",
        "launcher_resolution",
        "help_checked",
        "schemas_checked",
        "schema_migration",
        "hook_fixtures",
        "permissions_checked",
        "path_leaks",
        "rollback",
    }
    assert staged["source_release_staged_parity"] is True
    assert len(
        {
            staged["source_manifest_sha256"],
            staged["release_manifest_sha256"],
            staged["staged_manifest_sha256"],
        }
    ) == 1
    assert staged["launcher_resolution"] and staged["help_checked"]
    assert staged["schema_migration"] == {
        "to_version": CURRENT_SCHEMA_VERSION,
        "journal_mode": "DELETE",
        "integrity": True,
    }
    assert staged["permissions_checked"] and not staged["path_leaks"]
    assert staged["rollback"]["completed"] is True
    assert {item["harness"] for item in staged["hook_fixtures"]} == {
        "codex",
        "cursor",
        "pi",
    }


def assert_cutover_receipt(result: dict[str, object]) -> None:
    matrix = result["cutover"]
    assert set(matrix) == {
        "pointer_schema",
        "generation_bound",
        "exclusive_lock",
        "crash_recovery_journal",
        "fault_stages",
        "cases",
        "never_two_writers",
    }
    assert matrix["fault_stages"] == list(POINTER_STAGES)
    assert matrix["exclusive_lock"] and matrix["generation_bound"]
    assert matrix["crash_recovery_journal"]
    assert matrix["never_two_writers"]
    assert all(
        case["coherent"] and case["max_active_writers"] <= 1
        for case in matrix["cases"]
    )
    assert any(case["terminal_state"] == "blocked" for case in matrix["cases"])
    assert matrix["cases"][0]["journal_state"] == "completed"
    assert matrix["cases"][0]["startup_reconciled"] is False
    assert matrix["cases"][0]["process_restart_simulated"] is False
    assert matrix["cases"][0]["crash_signal"] is None
    assert matrix["cases"][0]["recovery_exit"] is None
    assert all(
        case["journal_state"] == "reconciled"
        and case["startup_reconciled"]
        and case["process_restart_simulated"]
        and case["crash_signal"] == "SIGKILL"
        and case["recovery_exit"] == 0
        for case in matrix["cases"][1:]
    )
    for case in matrix["cases"]:
        states = [item["state"] for item in case["history"]]
        assert states[:2] == ["planned", "executing"]
        assert states[-1] in {"completed", "blocked"}


def assert_canary_and_pending_receipts(result: dict[str, object]) -> None:
    assert result["canary"]["registered_exactly"]
    assert set(result["canary"]) == {
        "registered_exactly",
        "resource_id",
        "generation",
        "cleanup_exact",
        "wrong_generation_refused",
        "adapter_receipt",
        "real_runtime_proven",
    }
    assert result["canary"]["cleanup_exact"]
    assert result["canary"]["wrong_generation_refused"]
    assert result["canary"]["real_runtime_proven"] is False
    assert {item["slice"] for item in result["pending_assertions"]} == {
        "request",
        "assignment",
        "watcher",
        "stop",
        "teardown",
    }
    assert all(
        item["status"] == "pending" and item["passed"] is False
        for item in result["pending_assertions"]
    )
    assert {item["runtime"] for item in result["runtime_claims"]} == {
        "codex",
        "cursor",
        "pi",
        "herdr",
        "tmux",
    }
    assert all(
        item["status"] == "unverified" and item["mock_proof"] is False
        for item in result["runtime_claims"]
    )
    assert all(not adapter["real"] for adapter in result["adapters"].values())


def assert_receipt(result: dict[str, object]) -> None:
    assert_receipt_shape(result)
    assert_sentinel_and_migration_receipts(result)
    assert_staged_install_receipt(result)
    assert_cutover_receipt(result)
    assert_canary_and_pending_receipts(result)


def test_foundation_through_command_without_home(root: Path) -> None:
    root.mkdir()
    temporary_root = root / "command-root"
    temporary_root.mkdir()
    byte_path, config, processes = live_sentinels(root, "command-live")
    before = (
        (byte_path / "watcher.bin").read_bytes(),
        config.read_bytes(),
        processes.read_bytes(),
    )
    refused(
        lambda: run_acceptance(
            temporary_root,
            "command",
            sentinel_paths=(byte_path,),
            config_sentinel=config,
            process_sentinel=processes,
            source_root=root / "missing-source",
        ),
        "fixture_missing",
    )
    home = temporary_root / "league-command"
    blocked = json.loads((home / "acceptance-operation.json").read_text())
    assert blocked["state"] == "blocked" and blocked["attempt"] == 1
    assert blocked["history"][-1] == {
        "state": "blocked",
        "at": "2026-01-01T00:00:00Z",
        "attempt": 1,
        "error_code": "fixture_missing",
        "resumable": True,
    }
    assert not (home / "acceptance-receipt.json").exists()
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
    }
    command = subprocess.run(
        [
            str(LEAGUE),
            "acceptance",
            "run",
            "--temporary-root",
            str(temporary_root),
            "--namespace",
            "command",
            "--sentinel-path",
            str(byte_path),
            "--config-sentinel",
            str(config),
            "--process-sentinel",
            str(processes),
        ],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert command.returncode == 0, command.stdout + command.stderr
    envelope = json.loads(command.stdout)
    assert envelope["schema"] == "league.command.v1"
    assert envelope["ok"] is True and envelope["command"] == "acceptance.run"
    result = envelope["result"]
    assert_receipt(result)
    assert result["operation"]["attempt"] == 2
    assert [item["state"] for item in result["operation"]["history"]] == [
        "planned",
        "executing",
        "blocked",
        "executing",
        "completed",
    ]
    home = Path(result["home"])
    assert home.parent == temporary_root and home.name == "league-command"
    assert home.stat().st_mode & 0o777 == 0o700
    assert json.loads((home / "acceptance-receipt.json").read_text()) == result
    assert (home / "attempts/attempt-0001").is_dir()
    assert (home / "attempts/attempt-0002").is_dir()
    after = (
        (byte_path / "watcher.bin").read_bytes(),
        config.read_bytes(),
        processes.read_bytes(),
    )
    assert after == before
    collision = subprocess.run(
        command.args,
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert collision.returncode == 2
    assert json.loads(collision.stdout)["error"]["code"] == "namespace_collision"
    ambiguous = subprocess.run(
        [
            str(LEAGUE),
            "--state-root",
            str(temporary_root),
            "acceptance",
            "run",
            "--temporary-root",
            str(temporary_root),
            "--namespace",
            "ambiguous",
            "--sentinel-path",
            str(byte_path),
            "--config-sentinel",
            str(config),
            "--process-sentinel",
            str(processes),
        ],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert ambiguous.returncode == 2
    assert json.loads(ambiguous.stdout)["error"]["code"] == "invalid_acceptance_root"


def test_fail_closed_inputs(root: Path) -> None:
    root.mkdir()
    byte_path, config, processes = live_sentinels(root, "failure-live")
    sentinels = SentinelSet((byte_path,), config, processes)
    config.write_text('{"hooks":[]}\n', encoding="utf-8")
    refused(sentinels.verify, "sentinel_changed")
    symlink = root / "config-link.json"
    symlink.symlink_to(config)
    refused(lambda: SentinelSet((byte_path,), symlink, processes), "invalid_sentinel")
    duplicate_processes = root / "duplicate-processes.json"
    duplicate_processes.write_text(
        '{"schema":"league.synthetic-process-sentinel.v1","processes":[],"processes":[]}\n',
        encoding="utf-8",
    )
    refused(
        lambda: SentinelSet((byte_path,), config, duplicate_processes),
        "invalid_sentinel",
    )
    refused(
        lambda: run_acceptance(
            Path("relative"),
            "failure",
            sentinel_paths=(byte_path,),
            config_sentinel=config,
            process_sentinel=processes,
            source_root=ROOT,
        ),
        "invalid_temporary_root",
    )
    refused(
        lambda: run_acceptance(
            root.resolve(),
            "too-many",
            sentinel_paths=(byte_path,) * (MAX_ACCEPTANCE_SENTINEL_PATHS + 1),
            config_sentinel=config,
            process_sentinel=processes,
            source_root=ROOT,
        ),
        "too_many_sentinels",
    )
    bounded_command = subprocess.run(
        [
            str(LEAGUE),
            "acceptance",
            "run",
            "--temporary-root",
            str(root.resolve()),
            "--namespace",
            "bounded",
            *(
                ["--sentinel-path", str(byte_path)]
                * (MAX_ACCEPTANCE_SENTINEL_PATHS + 1)
            ),
            "--config-sentinel",
            str(config),
            "--process-sentinel",
            str(processes),
        ],
        cwd=root,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C",
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert bounded_command.returncode == 2
    assert "at most 16 sentinel paths are allowed" in bounded_command.stderr
    refused(
        lambda: validate_hook_fixture("codex", {"schema": "wrong", "harness": "codex"}),
        "invalid_hook_fixture",
    )


def test_schema_and_command_inventory() -> None:
    schema = json.loads(
        (ROOT / "schema/league-acceptance-receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["migration_shadow"]["properties"]["migration"] == {
        "$ref": "#/$defs/migrationReceipt"
    }
    assert len(schema["$defs"]["operationHistory"]["oneOf"]) == 4
    assert all(
        schema["properties"][name]["additionalProperties"] is False
        for name in (
            "determinism",
            "sentinels",
            "migration_shadow",
            "staged_install",
            "cutover",
            "canary",
            "adapters",
        )
    )
    for name in (
        "league-pre-cutover-plan.schema.json",
        "league-pre-cutover-receipt.schema.json",
        "league-cleanup-canary-adapters.schema.json",
        "league-real-cleanup-artifact-profile.schema.json",
        "league-real-cleanup-canary-receipt.schema.json",
    ):
        added = json.loads((ROOT / "schema" / name).read_text(encoding="utf-8"))
        assert added["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert added["additionalProperties"] is False
    inventory = subprocess.run(
        [str(LEAGUE), "help", "inventory"],
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    inventory_value = json.loads(inventory.stdout)["result"]
    assert "acceptance.cleanup-canary" in inventory_value["commands"]
    assert "league-real-cleanup-artifact-profile.schema.json" in inventory_value["schemas"]
    assert "league-real-cleanup-canary-receipt.schema.json" in inventory_value["schemas"]
    help_result = subprocess.run(
        [str(LEAGUE), "--help"], text=True, capture_output=True, check=True, timeout=10
    )
    assert "acceptance" in help_result.stdout
    version = subprocess.run(
        [str(LEAGUE), "--version"], text=True, capture_output=True, check=True, timeout=10
    )
    assert version.stdout.strip() == "league 0.2.20"


def test_issue_23_incident_artifacts_are_complete_and_public_safe() -> None:
    markdown = ROOT / "docs/incident-23-sqlite-hot-path-journal-mode-contention.md"
    html = ROOT / "docs/incident-23-sqlite-hot-path-journal-mode-contention.html"
    markdown_text = markdown.read_text(encoding="utf-8")
    html_text = html.read_text(encoding="utf-8")
    for heading in (
        "Executive summary",
        "Exact symptom and error",
        "What previously worked",
        "What failed",
        "Chronological timeline",
        "Technical root cause",
        "Why prior canaries missed it",
        "User impact",
        "Immediate containment",
        "Corrective code",
        "Acceptance matrix",
        "Rollback",
        "Remaining risks",
        "Action items",
    ):
        assert heading.lower() in markdown_text.lower()
        assert heading.lower() in html_text.lower()
    forbidden = re.compile(
        r"/Users/|wenkxu|4239|"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
        r"i reenabled|is this correct",
        re.IGNORECASE,
    )
    assert forbidden.search(markdown_text) is None
    assert forbidden.search(html_text) is None
    assert not re.search(r"<script|<link|\s(?:src|href)=", html_text, re.IGNORECASE)
    HTMLParser().feed(html_text)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-acceptance-test-") as temporary:
        root = Path(temporary)
        test_foundation_through_command_without_home(root / "command")
        test_fail_closed_inputs(root / "failure")
        test_schema_and_command_inventory()
        test_issue_23_incident_artifacts_are_complete_and_public_safe()
    print(
        "PASS: explicit-root sandbox, fake adapters, sentinels, migration parity, staged rollback, "
        "generation-fenced fault matrix, resumable receipts, exact canary cleanup, "
        "and honest pending claims"
    )


if __name__ == "__main__":
    main()
