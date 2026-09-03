#!/usr/bin/env python3
"""Focused command, failure, sentinel, staging, and cutover acceptance tests."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEAGUE = ROOT / "bin/league"
EXPECTED_MIGRATION_REPORT = "5f567a4dbb700e8843432627fb8a08e4f618fb744084abbbe4496df881579eb6"
EXPECTED_MIGRATION_SOURCE = "0f7d57871907fabdab99b01b39e280d6ea2d12901c4a22ea765e9bbe24241499"
EXPECTED_MIGRATION_PARITY = "2515229a441ce24e6e31bf49215c2d9ad7db82a390126291f7d953d8cd768894"
sys.path.insert(0, str(ROOT / "src"))

from league import MAX_ACCEPTANCE_SENTINEL_PATHS  # noqa: E402
from league.acceptance import (  # noqa: E402
    MAX_RELEASE_FILE_BYTES,
    POINTER_STAGES,
    PROCESS_SENTINEL_SCHEMA,
    STAGING_RESERVATION_FILENAME,
    SentinelSet,
    _release_files,
    _staged_install,
    run_acceptance,
    validate_hook_fixture,
)
from league.sqlite_store import CURRENT_SCHEMA_VERSION  # noqa: E402
from league.routing import load_routing_config  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402


def refused(operation, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    result: dict[str, tuple[str, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", path.readlink().as_posix())
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        else:
            result[relative] = ("directory", "")
    return result


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
    assert shadow["dry_run"]["report_digest"] == EXPECTED_MIGRATION_REPORT, (
        shadow["dry_run"]["report_digest"],
        EXPECTED_MIGRATION_REPORT,
    )
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
        "guidance",
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
    guidance = staged["guidance"]
    assert guidance["source"] == "global-agent-instructions/league/AGENTS.md"
    assert guidance["target"] == "league/AGENTS.md"
    assert guidance["source_sha256"] == guidance["installed_sha256"]
    assert guidance["prior_sha256"] == guidance["restored_sha256"]
    assert len(
        {
            guidance["universal_before_sha256"],
            guidance["universal_after_install_sha256"],
            guidance["universal_after_rollback_sha256"],
        }
    ) == 1
    assert guidance["universal_unchanged"] is True
    assert guidance["rollback_completed"] is True
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
    assert version.stdout.strip() == "league 0.2.61"


def test_forbidden_universal_guide_manifest_precedes_install_mutation(root: Path) -> None:
    root.mkdir()
    pointer = root / "current"
    pointer.write_text("synthetic-prior-pointer\n", encoding="utf-8")
    before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.iterdir()}
    refused(
        lambda: _staged_install(root, ROOT, guidance_targets=("AGENTS.md",)),
        "universal_guidance_forbidden",
    )
    after = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.iterdir()}
    assert after == before


def test_existing_release_identity_precedes_install_mutation(root: Path) -> None:
    for name, relative in (
        ("release", Path("stage-prefix/releases/0.2.61")),
        ("bundle", Path("release-bundle/0.2.61")),
    ):
        collision = root / name
        collision.mkdir(parents=True)
        candidate = collision / relative
        candidate.mkdir(parents=True)
        (candidate / "retained-byte").write_bytes(b"existing release identity\n")
        pointer = collision / "stage-prefix/current"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.symlink_to("releases/0.2.28")
        before = tree_snapshot(collision)
        refused(
            lambda collision=collision: _staged_install(collision, ROOT),
            "staged_release_identity_exists",
        )
        assert tree_snapshot(collision) == before


class InjectedStageCrash(RuntimeError):
    pass


def copy_release_source(destination: Path) -> None:
    for source in _release_files(ROOT):
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def refusal_home(root: Path, name: str) -> tuple[Path, dict[str, tuple[str, bytes | str]]]:
    home = root / name
    home.mkdir()
    (home / "AGENTS.md").write_bytes(b"toolkit-owned universal guide\n")
    return home, tree_snapshot(home)


def test_version_staging_is_regular_and_exact(root: Path) -> None:
    root.mkdir()
    source_version = ROOT / "VERSION"
    assert stat.S_ISREG(source_version.lstat().st_mode)
    normal = root / "normal"
    receipt = _staged_install(normal, ROOT)
    bundle_version = normal / "release-bundle/0.2.61/VERSION"
    staged_version = normal / "stage-prefix/releases/0.2.61/VERSION"
    for candidate in (bundle_version, staged_version):
        assert stat.S_ISREG(candidate.lstat().st_mode)
        assert candidate.read_bytes() == source_version.read_bytes()
    for relative in (
        Path("integrations/pi/league-runtime.ts"),
        Path("integrations/pi/league-hooks.mjs"),
        Path("integrations/pi/league-bash.sb"),
        Path("config/league-model-routing.example.json"),
        Path("config/league-supervisor.launchd.plist.in"),
    ):
        source = ROOT / relative
        bundled = normal / "release-bundle/0.2.61" / relative
        staged = normal / "stage-prefix/releases/0.2.61" / relative
        assert bundled.read_bytes() == staged.read_bytes() == source.read_bytes()
    installed_routing = load_routing_config(
        normal
        / "stage-prefix/releases/0.2.61/config/league-model-routing.example.json"
    )
    assert installed_routing["schema"] == 3
    assert installed_routing["policy"]["quality_baseline"] == "WORKER_STRONG"
    assert installed_routing["evaluations"]["openai/WORKER_FAST"][
        "representative_tasks"
    ] == 0
    assert receipt["source_release_staged_parity"] is True
    assert receipt["guidance"]["universal_unchanged"] is True
    assert receipt["guidance"]["rollback_completed"] is True


def test_release_source_symlinks_and_oversize_refuse_before_mutation(
    root: Path,
) -> None:
    root.mkdir()
    source_version = ROOT / "VERSION"
    linked_source = root / "linked-source"
    copy_release_source(linked_source)
    version_target = root / "untrusted-version-target"
    version_target.write_bytes(source_version.read_bytes())
    (linked_source / "VERSION").unlink()
    (linked_source / "VERSION").symlink_to(version_target)
    linked_home, linked_before = refusal_home(root, "symlink-refusal")
    refused(
        lambda: _staged_install(linked_home, linked_source),
        "release_incomplete",
    )
    assert tree_snapshot(linked_home) == linked_before

    ancestor_source = root / "ancestor-source"
    copy_release_source(ancestor_source)
    schema_target = root / "untrusted-schema-target"
    (ancestor_source / "schema").rename(schema_target)
    (ancestor_source / "schema").symlink_to(schema_target)
    ancestor_home, ancestor_before = refusal_home(root, "ancestor-refusal")
    refused(
        lambda: _staged_install(ancestor_home, ancestor_source),
        "release_incomplete",
    )
    assert tree_snapshot(ancestor_home) == ancestor_before

    oversized_source = root / "oversized-source"
    copy_release_source(oversized_source)
    (oversized_source / "src/league/report_template.html").write_bytes(
        b"x" * (MAX_RELEASE_FILE_BYTES + 1)
    )
    oversized_home, oversized_before = refusal_home(root, "oversized-refusal")
    refused(
        lambda: _staged_install(oversized_home, oversized_source),
        "release_incomplete",
    )
    assert tree_snapshot(oversized_home) == oversized_before


def test_staging_crash_cleanup_and_retry(root: Path) -> None:
    root.mkdir()
    source_version = ROOT / "VERSION"
    retry_home = root / "crash-retry"
    retry_home.mkdir()
    retry_universal = retry_home / "AGENTS.md"
    retry_universal.write_bytes(b"toolkit-owned universal guide\n")
    universal_before = retry_universal.read_bytes()

    def crash_after_version(event: str) -> None:
        if event == "after_release_file:VERSION":
            raise InjectedStageCrash(event)

    try:
        _staged_install(retry_home, ROOT, fault=crash_after_version)
    except InjectedStageCrash:
        pass
    else:
        raise AssertionError("expected injected staging crash")
    assert not (retry_home / "release-bundle/0.2.61").exists()
    assert not (retry_home / "stage-prefix/releases/0.2.61").exists()
    assert retry_universal.read_bytes() == universal_before
    retry = _staged_install(retry_home, ROOT)
    retried_version = retry_home / "stage-prefix/releases/0.2.61/VERSION"
    assert stat.S_ISREG(retried_version.lstat().st_mode)
    assert retried_version.read_bytes() == source_version.read_bytes()
    assert retry["source_release_staged_parity"] is True
    assert retry["guidance"]["universal_unchanged"] is True
    assert retry["guidance"]["rollback_completed"] is True
    assert retry_universal.read_bytes() == universal_before


def crash_staging_process(root: Path, name: str) -> Path:
    crash_home = root / name
    crash_script = """
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from league.acceptance import _staged_install

def crash(event):
    if event == "after_release_file:VERSION":
        os._exit(73)

_staged_install(Path(sys.argv[2]), Path(sys.argv[3]), fault=crash)
"""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
    }
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            crash_script,
            str(ROOT / "src"),
            str(crash_home),
            str(ROOT),
        ],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    for candidate in (
        crash_home / "release-bundle/0.2.61",
        crash_home / "stage-prefix/releases/0.2.61",
    ):
        assert (candidate / STAGING_RESERVATION_FILENAME).is_file()
        assert (candidate / "VERSION").is_file()
    return crash_home


def test_separate_process_version_crash_recovers_and_retries(root: Path) -> None:
    root.mkdir()
    crash_home = crash_staging_process(root, "process-crash")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
    }

    retry_script = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from league.acceptance import _staged_install

_staged_install(Path(sys.argv[2]), Path(sys.argv[3]))
"""
    retried = subprocess.run(
        [
            sys.executable,
            "-c",
            retry_script,
            str(ROOT / "src"),
            str(crash_home),
            str(ROOT),
        ],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert (
        crash_home / "stage-prefix/releases/0.2.61/VERSION"
    ).read_bytes() == (ROOT / "VERSION").read_bytes()


def test_partial_stage_recovery_mismatches_refuse(root: Path) -> None:
    root.mkdir()

    marker_home = crash_staging_process(root, "marker-mismatch")
    marker_path = (
        marker_home
        / "stage-prefix/releases/0.2.61"
        / STAGING_RESERVATION_FILENAME
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["token"] = "0" * 64
    write_json(marker_path, marker)
    refused(
        lambda: _staged_install(marker_home, ROOT),
        "staged_release_identity_exists",
    )

    source_home = crash_staging_process(root, "source-mismatch")
    for directory in (
        source_home / "release-bundle/0.2.61",
        source_home / "stage-prefix/releases/0.2.61",
    ):
        path = directory / STAGING_RESERVATION_FILENAME
        marker = json.loads(path.read_text(encoding="utf-8"))
        marker["source_version_sha256"] = "0" * 64
        write_json(path, marker)
    refused(
        lambda: _staged_install(source_home, ROOT),
        "staged_release_identity_exists",
    )

    inode_home = crash_staging_process(root, "inode-mismatch")
    bundle = inode_home / "release-bundle/0.2.61"
    displaced = root / "inode-mismatch-original"
    bundle.rename(displaced)
    shutil.copytree(displaced, bundle)
    refused(
        lambda: _staged_install(inode_home, ROOT),
        "staged_release_identity_exists",
    )

    extra_home = crash_staging_process(root, "extra-content")
    (extra_home / "release-bundle/0.2.61/extra-byte").write_bytes(b"unexpected\n")
    refused(
        lambda: _staged_install(extra_home, ROOT),
        "staged_release_identity_exists",
    )


def test_staging_cleanup_preserves_replacements_and_original_refusal(
    root: Path,
) -> None:
    root.mkdir()
    source_version = ROOT / "VERSION"
    swapped_home = root / "swapped-reservation"
    moved_bundle = root / "original-reserved-bundle"

    def swap_reserved_bundle(event: str) -> None:
        if event != "after_release_file:VERSION":
            return
        bundle = swapped_home / "release-bundle/0.2.61"
        bundle.rename(moved_bundle)
        bundle.mkdir()
        (bundle / "replacement-byte").write_bytes(b"must remain\n")
        raise InjectedStageCrash(event)

    try:
        _staged_install(swapped_home, ROOT, fault=swap_reserved_bundle)
    except InjectedStageCrash:
        pass
    else:
        raise AssertionError("expected injected reservation swap")
    assert (swapped_home / "release-bundle/0.2.61/replacement-byte").read_bytes() == (
        b"must remain\n"
    )
    assert (moved_bundle / "VERSION").read_bytes() == source_version.read_bytes()
    assert not (swapped_home / "stage-prefix/releases/0.2.61").exists()

    symlink_home = root / "symlink-swap"
    displaced_bundle = root / "symlink-displaced-bundle"
    foreign_target = root / "foreign-replacement-target"
    foreign_target.mkdir()
    (foreign_target / "foreign-byte").write_bytes(b"must remain\n")

    def swap_reserved_bundle_to_symlink(event: str) -> None:
        if event != "after_release_file:VERSION":
            return
        bundle = symlink_home / "release-bundle/0.2.61"
        bundle.rename(displaced_bundle)
        bundle.symlink_to(foreign_target, target_is_directory=True)
        raise InjectedStageCrash(event)

    try:
        _staged_install(symlink_home, ROOT, fault=swap_reserved_bundle_to_symlink)
    except InjectedStageCrash:
        pass
    else:
        raise AssertionError("expected injected symlink reservation swap")
    restored_link = symlink_home / "release-bundle/0.2.61"
    assert restored_link.is_symlink()
    assert restored_link.readlink() == foreign_target
    assert (foreign_target / "foreign-byte").read_bytes() == b"must remain\n"
    assert (displaced_bundle / "VERSION").read_bytes() == source_version.read_bytes()

    subdirectory_home = root / "subdirectory-swap"
    foreign_bundle = root / "foreign-bundle-bin"
    foreign_release = root / "foreign-release-bin"
    foreign_bundle.mkdir()
    foreign_release.mkdir()

    def swap_staged_subdirectories(event: str) -> None:
        if event != "after_release_file:VERSION":
            return
        (subdirectory_home / "release-bundle/0.2.61/bin").symlink_to(
            foreign_bundle, target_is_directory=True
        )
        (subdirectory_home / "stage-prefix/releases/0.2.61/bin").symlink_to(
            foreign_release, target_is_directory=True
        )

    refused(
        lambda: _staged_install(
            subdirectory_home, ROOT, fault=swap_staged_subdirectories
        ),
        "staged_parity_failed",
    )
    assert not any(foreign_bundle.iterdir())
    assert not any(foreign_release.iterdir())

    failure_home = root / "cleanup-failure"

    def fail_stage_and_cleanup(event: str) -> None:
        if event == "after_release_file:VERSION":
            raise StorageRefusal(
                "synthetic_staging_refusal", "synthetic staging refusal"
            )
        if event.startswith("before_reserved_cleanup:"):
            raise PermissionError("synthetic cleanup failure")

    refused(
        lambda: _staged_install(failure_home, ROOT, fault=fail_stage_and_cleanup),
        "synthetic_staging_refusal",
    )


def test_post_switch_validation_failure_restores_prior_pointer(root: Path) -> None:
    root.mkdir()
    home = root / "post-switch"

    def fail_launcher_validation(event: str) -> None:
        if event == "before_staged_launcher_validation":
            raise StorageRefusal(
                "synthetic_launcher_failure", "synthetic launcher failure"
            )

    refused(
        lambda: _staged_install(home, ROOT, fault=fail_launcher_validation),
        "synthetic_launcher_failure",
    )
    current = home / "stage-prefix/current"
    stable = home / "stage-prefix/bin/league"
    assert current.readlink().as_posix() == "releases/0.0.0-legacy"
    version = subprocess.run(
        [str(stable), "--version"],
        cwd=home,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert version.stdout.strip() == "league 0.0.0-legacy"


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
        test_forbidden_universal_guide_manifest_precedes_install_mutation(
            root / "forbidden-guide-manifest"
        )
        test_existing_release_identity_precedes_install_mutation(
            root / "release-identity-collision"
        )
        test_version_staging_is_regular_and_exact(root / "version-regular-file")
        test_release_source_symlinks_and_oversize_refuse_before_mutation(
            root / "release-source-refusal"
        )
        test_staging_crash_cleanup_and_retry(root / "staging-crash-retry")
        test_separate_process_version_crash_recovers_and_retries(
            root / "separate-process-crash"
        )
        test_partial_stage_recovery_mismatches_refuse(
            root / "partial-stage-refusals"
        )
        test_staging_cleanup_preserves_replacements_and_original_refusal(
            root / "staging-cleanup"
        )
        test_post_switch_validation_failure_restores_prior_pointer(
            root / "post-switch-rollback"
        )
        test_schema_and_command_inventory()
        test_issue_23_incident_artifacts_are_complete_and_public_safe()
    print(
        "PASS: explicit-root sandbox, fake adapters, sentinels, migration parity, staged rollback, "
        "generation-fenced fault matrix, resumable receipts, exact canary cleanup, "
        "regular-file staging with crash retry, and honest pending claims"
    )


if __name__ == "__main__":
    main()
