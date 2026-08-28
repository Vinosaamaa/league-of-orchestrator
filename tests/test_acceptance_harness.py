#!/usr/bin/env python3
"""Focused command, failure, sentinel, staging, and cutover acceptance tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEAGUE = ROOT / "bin/league"
sys.path.insert(0, str(ROOT / "src"))

from league.acceptance import (  # noqa: E402
    POINTER_STAGES,
    PROCESS_SENTINEL_SCHEMA,
    SentinelSet,
    run_acceptance,
    validate_hook_fixture,
)
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


def assert_receipt(result: dict[str, object]) -> None:
    assert set(result) == {
        "schema",
        "version",
        "namespace",
        "home",
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
    assert set(result["determinism"]) == {"clock", "ids_allocated"}
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
    assert shadow["apply"]["applied"] is True
    assert shadow["exact_parity"] is True and shadow["legacy_unchanged"] is True
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
        "to_version": 2,
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
    matrix = result["cutover"]
    assert set(matrix) == {
        "pointer_schema",
        "generation_bound",
        "exclusive_lock",
        "fault_stages",
        "cases",
        "never_two_writers",
    }
    assert matrix["fault_stages"] == list(POINTER_STAGES)
    assert matrix["exclusive_lock"] and matrix["generation_bound"]
    assert matrix["never_two_writers"]
    assert all(
        case["coherent"] and case["max_active_writers"] <= 1
        for case in matrix["cases"]
    )
    assert any(case["terminal_state"] == "blocked" for case in matrix["cases"])
    for case in matrix["cases"]:
        states = [item["state"] for item in case["history"]]
        assert states[:2] == ["planned", "executing"]
        assert states[-1] in {"completed", "blocked"}
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


def test_full_foundation(root: Path) -> None:
    root.mkdir()
    temporary_root = root / "temporary-root"
    temporary_root.mkdir()
    byte_path, config, processes = live_sentinels(root, "caller-live")
    before = (
        (byte_path / "watcher.bin").read_bytes(),
        config.read_bytes(),
        processes.read_bytes(),
    )
    result = run_acceptance(
        temporary_root,
        "foundation",
        sentinel_paths=(byte_path,),
        config_sentinel=config,
        process_sentinel=processes,
        source_root=ROOT,
    )
    assert_receipt(result)
    home = Path(result["home"])
    assert home.parent == temporary_root and home.name == "league-foundation"
    assert home.stat().st_mode & 0o777 == 0o700
    assert json.loads((home / "acceptance-receipt.json").read_text()) == result
    after = (
        (byte_path / "watcher.bin").read_bytes(),
        config.read_bytes(),
        processes.read_bytes(),
    )
    assert after == before
    refused(
        lambda: run_acceptance(
            temporary_root,
            "foundation",
            sentinel_paths=(byte_path,),
            config_sentinel=config,
            process_sentinel=processes,
            source_root=ROOT,
        ),
        "namespace_collision",
    )


def test_command_without_home(root: Path) -> None:
    root.mkdir()
    temporary_root = root / "command-root"
    temporary_root.mkdir()
    byte_path, config, processes = live_sentinels(root, "command-live")
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
    assert_receipt(envelope["result"])
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
    help_result = subprocess.run(
        [str(LEAGUE), "--help"], text=True, capture_output=True, check=True, timeout=10
    )
    assert "acceptance" in help_result.stdout
    version = subprocess.run(
        [str(LEAGUE), "--version"], text=True, capture_output=True, check=True, timeout=10
    )
    assert version.stdout.strip() == "league 0.1.0"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-acceptance-test-") as temporary:
        root = Path(temporary)
        test_full_foundation(root / "full")
        test_command_without_home(root / "command")
        test_fail_closed_inputs(root / "failure")
        test_schema_and_command_inventory()
    print(
        "PASS: explicit-root sandbox, fake adapters, sentinels, migration parity, staged rollback, "
        "generation-fenced fault matrix, resumable receipts, exact canary cleanup, "
        "and honest pending claims"
    )


if __name__ == "__main__":
    main()
