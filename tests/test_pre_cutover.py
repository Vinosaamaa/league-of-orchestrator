#!/usr/bin/env python3
"""Focused no-home command, parity, failure, and supervision preflight tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LEAGUE = ROOT / "bin/league"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from league.acceptance import PROCESS_SENTINEL_SCHEMA, _sha256, _stable_bytes  # noqa: E402
from league.precutover import (  # noqa: E402
    LEGACY_RECONCILIATION_RECEIPT_SCHEMA,
    LEGACY_RECONCILIATION_SCHEMA,
    PLAN_SCHEMA,
    RECEIPT_SCHEMA,
    _write_immutable_json,
    run_pre_cutover,
)
from league.sqlite_store import CURRENT_SCHEMA_VERSION  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from storage_fixture import write_complete_fixture  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def refused(operation: Any, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def _manifest_paths(manifest: dict[str, Any]) -> list[str]:
    paths: set[str] = set()
    for roster in manifest["canonical_sources"]["rosters"]:
        paths.add(roster["status"])
        if roster["updates"] is not None:
            paths.add(roster["updates"])
    for family in (
        "pending_launches",
        "watcher_states",
        "visible_callsign_pools",
        "hidden_worker_pools",
        "lead_relay_states",
        "resource_registries",
    ):
        paths.update(item["path"] for item in manifest["canonical_sources"][family])
    paths.update(item["path"] for item in manifest["retained_files"])
    return sorted(paths)


def fixture_plan(base: Path) -> dict[str, Any]:
    live = base / "live"
    legacy = live / "legacy"
    legacy.mkdir(parents=True)
    fixture = write_complete_fixture(
        legacy, runtime_root=Path("/synthetic/precutover-test-runtime")
    )
    manifest_value = json.loads(fixture["manifest"].read_text(encoding="utf-8"))

    installed = live / "installed"
    (installed / "bin").mkdir(parents=True)
    (installed / "bin/league").write_bytes(b"#!/bin/sh\nprintf 'legacy league\\n'\n")
    (installed / "bin/agent-watcher").write_bytes(
        b"#!/bin/sh\nprintf 'legacy watcher\\n'\n"
    )
    os.chmod(installed / "bin/league", 0o755)
    os.chmod(installed / "bin/agent-watcher", 0o755)
    stable = live / "bin/league"
    watcher_launcher = live / "bin/agent-watcher"
    stable.parent.mkdir(parents=True)
    stable.symlink_to(installed / "bin/league")
    watcher_launcher.symlink_to(installed / "bin/agent-watcher")
    hook = live / "config/hooks.json"
    write_json(hook, {"hooks": [{"command": "synthetic-existing-handler"}]})
    watcher_state = live / "watcher-state"
    watcher_state.mkdir()
    write_json(watcher_state / "state.json", {"generation": 4, "enabled": True})
    processes = live / "processes.json"
    write_json(
        processes,
        {
            "schema": PROCESS_SENTINEL_SCHEMA,
            "processes": [
                {
                    "pid": 9001,
                    "process_start": "synthetic-start",
                    "endpoint": "synthetic://precutover/unchanged",
                }
            ],
        },
    )

    proposed = {
        "backup_root": str(live / "proposed/backups/cutover-1"),
        "release_prefix": str(live / "proposed/releases"),
        "stable_launcher": str(stable),
        "watcher_launcher": str(watcher_launcher),
        "state_root": str(live / "proposed/state"),
        "writer_pointer": str(live / "proposed/writer-pointer.json"),
        "archive_root": str(live / "proposed/archive"),
        "hooks": [{"harness": "codex", "target": str(hook)}],
    }
    targets = [
        ("archive", "archive_root", proposed["archive_root"], False),
        ("backups", "backup_root", proposed["backup_root"], False),
        ("hooks", "hook_config", hook, True),
        ("installed", "installed_bundle", installed, True),
        ("legacy", "legacy_state", legacy, True),
        ("release", "release_prefix", proposed["release_prefix"], False),
        ("sqlite", "sqlite_state", proposed["state_root"], False),
        ("stable", "stable_launcher", stable, True),
        ("watcher", "watcher_state", watcher_state, True),
        ("watcher-launcher", "watcher_launcher", watcher_launcher, True),
        ("writer", "writer_pointer", proposed["writer_pointer"], False),
    ]
    plan = {
        "schema": PLAN_SCHEMA,
        "legacy": {
            "manifest": str(fixture["manifest"]),
            "bindings": [
                {"relative_path": relative, "source": str(legacy / relative)}
                for relative in _manifest_paths(manifest_value)
            ],
        },
        "current_targets": [
            {
                "target_id": target_id,
                "kind": kind,
                "path": str(path),
                "required": required,
            }
            for target_id, kind, path, required in targets
        ],
        "proposed": proposed,
    }
    plan_path = live / "precutover-plan.json"
    write_json(plan_path, plan)
    return {
        "plan": plan,
        "plan_path": plan_path,
        "legacy": legacy,
        "hook": hook,
        "processes": processes,
        "live": live,
    }


def add_shotcaller_initialization_mismatch(fixture: dict[str, Any]) -> dict[str, Any]:
    """Add one sanitized Shotcaller status/update pair with sentence-only drift."""
    relative = "rosters/Garen/updates.jsonl"
    source = fixture["legacy"] / relative
    transition = {
        "at": "2026-01-01T00:01:00Z",
        "status": "working",
        "update": "Synthetic initialization was recorded by the transition log.",
    }
    write_json(source, transition)
    manifest = json.loads(
        fixture["legacy"].joinpath("import-manifest.json").read_text(encoding="utf-8")
    )
    manifest["canonical_sources"]["rosters"][0]["updates"] = relative
    write_json(fixture["legacy"] / "import-manifest.json", manifest)
    fixture["plan"]["legacy"]["bindings"].append(
        {"relative_path": relative, "source": str(source)}
    )
    fixture["plan"]["legacy"]["bindings"].sort(key=lambda item: item["relative_path"])
    write_json(fixture["plan_path"], fixture["plan"])
    return {
        "status": fixture["legacy"] / "rosters/Garen/status.json",
        "updates": source,
    }


def _content_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def authorize_shotcaller_reconciliation(
    fixture: dict[str, Any],
    pair: dict[str, Path],
    *,
    expected_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    authorization = {
        "schema": LEGACY_RECONCILIATION_SCHEMA,
        "artifact_pair": {
            "artifact_id": "R1-shotcaller",
            "status": "rosters/Garen/status.json",
            "updates": "rosters/Garen/updates.jsonl",
        },
        "expected_source_sha256": expected_hashes
        or {name: _content_sha256(path) for name, path in pair.items()},
        "resolution": {"authoritative": "status_snapshot"},
        "reason": "Synthetic legacy initialization sentences differ.",
    }
    fixture["plan"]["legacy"]["reconciliation"] = authorization
    write_json(fixture["plan_path"], fixture["plan"])
    return authorization


def run_fixture(
    fixture: dict[str, Any], temporary_root: Path, namespace: str
) -> dict[str, Any]:
    temporary_root.mkdir()
    return run_pre_cutover(
        temporary_root,
        namespace,
        plan_path=fixture["plan_path"],
        sentinel_paths=(fixture["legacy"],),
        config_sentinel=fixture["hook"],
        process_sentinel=fixture["processes"],
        source_root=ROOT,
    )


def command_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
    }


def command_preflight(
    fixture: dict[str, Any], temporary_root: Path, namespace: str
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(LEAGUE),
            "acceptance",
            "preflight",
            "--temporary-root",
            str(temporary_root),
            "--namespace",
            namespace,
            "--plan",
            str(fixture["plan_path"]),
            "--sentinel-path",
            str(fixture["legacy"]),
            "--config-sentinel",
            str(fixture["hook"]),
            "--process-sentinel",
            str(fixture["processes"]),
        ],
        cwd=ROOT,
        env=command_environment(),
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    envelope = json.loads(completed.stdout)
    assert envelope["ok"] is True and envelope["command"] == "acceptance.preflight"
    return envelope["result"]


def assert_receipt_operation(result: dict[str, Any]) -> None:
    assert set(result) == {
        "schema",
        "version",
        "namespace",
        "home",
        "operation",
        "determinism",
        "sentinels",
        "live_targets",
        "fixture_migration_shadow",
        "live_migration_shadow",
        "backup_rollback_rehearsal",
        "staged_inactive_install",
        "manifest_checks",
        "integrated_lifecycle",
        "runtime_contract_canaries",
        "supervision_benchmark",
        "cutover_fault_matrix",
        "fake_resource_canary",
        "mutation_manifest",
        "public_claims",
    }
    assert result["schema"] == RECEIPT_SCHEMA
    operation = result["operation"]
    assert operation["state"] == "awaiting_authority"
    assert [item["state"] for item in operation["history"]] == [
        "planned",
        "executing",
        "awaiting_authority",
    ]
    assert result["sentinels"]["unchanged"] is True
    live_targets = result["live_targets"]
    assert live_targets["unchanged"] is True
    assert live_targets["observation_scope"] == "before_after_snapshot_parity"
    assert live_targets["continuous_external_stability_proven"] is False
    assert live_targets["preflight_write_count"] == 0
    assert live_targets["target_count"] > 0


def assert_receipt_migration_and_install(result: dict[str, Any]) -> None:
    assert result["fixture_migration_shadow"]["exact_parity"] is True
    assert result["live_migration_shadow"]["exact_parity"] is True
    assert result["live_migration_shadow"]["source_unchanged"] is True
    assert result["live_migration_shadow"]["migration"]["to_version"] == CURRENT_SCHEMA_VERSION
    assert result["backup_rollback_rehearsal"]["restore_parity"] is True
    staged = result["staged_inactive_install"]
    assert staged["inactive_after_checks"] and not staged["global_install_performed"]
    assert staged["supervision"] == {
        "launcher_help_checked": True,
        "source_contract_checked": True,
        "reconciliation_interval_seconds": 30,
        "consecutive_observations": 2,
        "unchanged_output": "silent",
        "separate_15_second_policy": False,
    }
    assert len(
        {
            staged["source_manifest_sha256"],
            staged["release_manifest_sha256"],
            staged["staged_manifest_sha256"],
        }
    ) == 1
    checks = result["manifest_checks"]
    assert checks["version_parity"] and checks["source_release_staged_parity"]
    assert checks["current_installed_unchanged"]


def assert_receipt_lifecycle(result: dict[str, Any]) -> None:
    assert all(
        item["status"] == "passed"
        for item in result["integrated_lifecycle"].values()
        if isinstance(item, dict)
    )
    runtime = result["runtime_contract_canaries"]
    assert len(runtime["canaries"]) == 3 and not runtime["real_runtime_proven"]
    assert all(not item["real_runtime_proven"] for item in runtime["canaries"])
    assert {item["runtime"] for item in runtime["unverified"]} == {
        "cursor",
        "real-herdr-tmux",
    }
    assert result["cutover_fault_matrix"]["never_two_writers"]


def assert_receipt_supervision(result: dict[str, Any]) -> None:
    benchmark = result["supervision_benchmark"]
    assert benchmark["status"] == "passed"
    assert benchmark["presentation"]["initial_messages"] == 1
    assert benchmark["presentation"]["unchanged_messages"] == 0
    assert benchmark["missed_wake_reconciliation"] == {
        "simulation": True,
        "interval_seconds": 30,
        "consecutive_observations": 2,
        "earliest_fallback_seconds": 60,
        "separate_15_second_policy": False,
    }
    assert not benchmark["permanent_daemon_created"]
    assert not benchmark["transcript_polling_used"]
    assert benchmark["listener_terminated"]


def assert_receipt_mutation_and_claims(
    result: dict[str, Any], fixture: dict[str, Any]
) -> None:
    manifest = result["mutation_manifest"]
    assert not manifest["applied"] and manifest["authority_required"]
    assert all(not item["applied"] for item in manifest["operations"])
    assert manifest["supervision"]["normal_wake"] == "event_driven_registered_listener"
    assert manifest["supervision"]["unchanged_output"] == "silent"
    assert manifest["supervision"]["reconciliation"]["interval_seconds"] == 30
    assert manifest["supervision"]["reconciliation"]["consecutive_observations"] == 2
    assert not manifest["supervision"]["reconciliation"]["separate_15_second_policy"]
    expected_targets = {item["path"] for item in fixture["plan"]["current_targets"]}
    backup_targets = {
        item["target"]
        for item in manifest["operations"]
        if item["operation"] == "backup_current_target"
    }
    assert backup_targets == expected_targets
    backup_operations = [
        item for item in manifest["operations"] if item["operation"] == "backup_current_target"
    ]
    assert all(item["after"]["verification_required"] for item in backup_operations)
    assert all(
        item["rollback"]["source_created_by_operation"] == "backup_current_target"
        for item in backup_operations
    )
    assert all(value is False for value in result["public_claims"].values())


def assert_receipt(result: dict[str, Any], fixture: dict[str, Any]) -> None:
    assert_receipt_operation(result)
    assert_receipt_migration_and_install(result)
    assert_receipt_lifecycle(result)
    assert_receipt_supervision(result)
    assert_receipt_mutation_and_claims(result, fixture)


def test_command_e2e_and_deterministic_manifest(root: Path) -> None:
    fixture = fixture_plan(root)
    temporary_root = root / "sandbox"
    temporary_root.mkdir()
    proposed = [
        Path(fixture["plan"]["proposed"][key])
        for key in ("backup_root", "release_prefix", "state_root", "writer_pointer", "archive_root")
    ]
    before = {
        "legacy": fixture["legacy"].joinpath("import-manifest.json").read_bytes(),
        "hook": fixture["hook"].read_bytes(),
        "processes": fixture["processes"].read_bytes(),
    }
    first = command_preflight(fixture, temporary_root, "command-one")
    assert_receipt(first, fixture)
    manifest = dict(first["mutation_manifest"])
    digest = manifest.pop("manifest_sha256")
    assert digest == _sha256(_stable_bytes(manifest))
    assert first["determinism"]["clock"] == "2026-01-01T00:00:00Z"
    assert first["determinism"]["ids_allocated"] > 0
    assert before == {
        "legacy": fixture["legacy"].joinpath("import-manifest.json").read_bytes(),
        "hook": fixture["hook"].read_bytes(),
        "processes": fixture["processes"].read_bytes(),
    }
    assert all(not path.exists() for path in proposed)


def test_invalid_plan_and_root_overlap_refuse_before_home(root: Path) -> None:
    fixture = fixture_plan(root)
    temporary_root = root / "sandbox"
    temporary_root.mkdir()
    invalid = dict(fixture["plan"])
    invalid["proposed"] = dict(invalid["proposed"])
    invalid["proposed"]["archive_root"] = "relative/archive"
    invalid_path = fixture["live"] / "invalid-plan.json"
    write_json(invalid_path, invalid)
    refused(
        lambda: run_pre_cutover(
            temporary_root,
            "invalid",
            plan_path=invalid_path,
            sentinel_paths=(fixture["legacy"],),
            config_sentinel=fixture["hook"],
            process_sentinel=fixture["processes"],
            source_root=ROOT,
        ),
        "plan_invalid",
    )
    assert not (temporary_root / "league-invalid-precutover").exists()
    refused(
        lambda: run_pre_cutover(
            fixture["live"],
            "overlap",
            plan_path=fixture["plan_path"],
            sentinel_paths=(fixture["legacy"],),
            config_sentinel=fixture["hook"],
            process_sentinel=fixture["processes"],
            source_root=ROOT,
        ),
        "unsafe_root_overlap",
    )
    assert not (fixture["live"] / "league-overlap-precutover").exists()

    dotted = dict(fixture["plan"])
    dotted["current_targets"] = [dict(item) for item in dotted["current_targets"]]
    dotted["current_targets"][0]["path"] = str(fixture["live"] / ".." / "live")
    dotted_path = fixture["live"] / "dotted-plan.json"
    write_json(dotted_path, dotted)
    refused(
        lambda: run_pre_cutover(
            temporary_root,
            "dotted",
            plan_path=dotted_path,
            sentinel_paths=(fixture["legacy"],),
            config_sentinel=fixture["hook"],
            process_sentinel=fixture["processes"],
            source_root=ROOT,
        ),
        "plan_invalid",
    )
    assert not (temporary_root / "league-dotted-precutover").exists()

    reserved = dict(fixture["plan"])
    reserved["legacy"] = dict(reserved["legacy"])
    reserved["legacy"]["bindings"] = [
        dict(item) for item in reserved["legacy"]["bindings"]
    ]
    reserved["legacy"]["bindings"][0]["relative_path"] = "import-manifest.json"
    reserved_path = fixture["live"] / "reserved-binding-plan.json"
    write_json(reserved_path, reserved)
    refused(
        lambda: run_pre_cutover(
            temporary_root,
            "reserved",
            plan_path=reserved_path,
            sentinel_paths=(fixture["legacy"],),
            config_sentinel=fixture["hook"],
            process_sentinel=fixture["processes"],
            source_root=ROOT,
        ),
        "plan_invalid",
    )
    assert not (temporary_root / "league-reserved-precutover").exists()

    for namespace, replacement in (
        ("aliased", fixture["plan"]["proposed"]["backup_root"]),
        ("nested", str(Path(fixture["plan"]["proposed"]["backup_root"]) / "archive")),
    ):
        overlapping = dict(fixture["plan"])
        overlapping["proposed"] = dict(overlapping["proposed"])
        overlapping["proposed"]["archive_root"] = replacement
        overlapping_path = fixture["live"] / f"{namespace}-destination-plan.json"
        write_json(overlapping_path, overlapping)
        refused(
            lambda path=overlapping_path, name=namespace: run_pre_cutover(
                temporary_root,
                name,
                plan_path=path,
                sentinel_paths=(fixture["legacy"],),
                config_sentinel=fixture["hook"],
                process_sentinel=fixture["processes"],
                source_root=ROOT,
            ),
            "plan_invalid",
        )
        assert not (temporary_root / f"league-{namespace}-precutover").exists()


def test_shotcaller_initialization_mismatch_refuses_without_reconciliation(
    root: Path,
) -> None:
    fixture = fixture_plan(root)
    pair = add_shotcaller_initialization_mismatch(fixture)
    before = {name: path.read_bytes() for name, path in pair.items()}
    temporary_root = root / "sandbox"
    temporary_root.mkdir()
    refused(
        lambda: run_pre_cutover(
            temporary_root,
            "unreconciled",
            plan_path=fixture["plan_path"],
            sentinel_paths=(fixture["legacy"],),
            config_sentinel=fixture["hook"],
            process_sentinel=fixture["processes"],
            source_root=ROOT,
        ),
        "snapshot_event_mismatch",
    )
    assert before == {name: path.read_bytes() for name, path in pair.items()}


def test_exact_shotcaller_reconciliation_is_snapshot_only_and_deterministic(
    root: Path,
) -> None:
    fixture = fixture_plan(root)
    pair = add_shotcaller_initialization_mismatch(fixture)
    before = {name: path.read_bytes() for name, path in pair.items()}
    authorization = authorize_shotcaller_reconciliation(fixture, pair)
    first = run_fixture(fixture, root / "sandbox-one", "authorized")
    second = run_fixture(fixture, root / "sandbox-two", "authorized")
    assert_receipt(first, fixture)
    assert_receipt(second, fixture)
    first_receipt = first["live_migration_shadow"]["legacy_reconciliation"]
    second_receipt = second["live_migration_shadow"]["legacy_reconciliation"]
    assert first_receipt == second_receipt
    assert first["operation"] == second["operation"]
    assert first_receipt["schema"] == LEGACY_RECONCILIATION_RECEIPT_SCHEMA
    assert first_receipt["original_source_sha256"] == authorization[
        "expected_source_sha256"
    ]
    assert first_receipt["result"] == {
        "normalized": True,
        "scope": "temporary_snapshot_only",
        "live_source_mutated": False,
    }
    immutable_path = (
        root
        / "sandbox-one/league-authorized-precutover/legacy-reconciliation-receipt.json"
    )
    assert immutable_path.read_bytes() == _stable_bytes(first_receipt)
    assert immutable_path.stat().st_mode & 0o777 == 0o600
    immutable_before = immutable_path.read_bytes()
    try:
        _write_immutable_json(immutable_path, {"replacement": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("immutable reconciliation receipt was overwritten")
    assert immutable_path.read_bytes() == immutable_before
    assert before == {name: path.read_bytes() for name, path in pair.items()}


def test_wrong_reconciliation_hash_refuses_without_source_mutation(root: Path) -> None:
    fixture = fixture_plan(root)
    pair = add_shotcaller_initialization_mismatch(fixture)
    before = {name: path.read_bytes() for name, path in pair.items()}
    hashes = {name: _content_sha256(path) for name, path in pair.items()}
    hashes["updates"] = "0" * 64
    authorize_shotcaller_reconciliation(fixture, pair, expected_hashes=hashes)
    refused(
        lambda: run_fixture(fixture, root / "sandbox", "stale"),
        "reconciliation_stale",
    )
    assert before == {name: path.read_bytes() for name, path in pair.items()}


def test_reconciliation_scope_and_initialization_guards(root: Path) -> None:
    missing = fixture_plan(root / "missing")
    pair = add_shotcaller_initialization_mismatch(missing)
    authorization = authorize_shotcaller_reconciliation(missing, pair)
    authorization["artifact_pair"]["updates"] = "rosters/Garen/missing.jsonl"
    write_json(missing["plan_path"], missing["plan"])
    refused(
        lambda: run_fixture(missing, root / "missing-sandbox", "missing"),
        "reconciliation_missing",
    )

    broad = fixture_plan(root / "broad")
    pair = add_shotcaller_initialization_mismatch(broad)
    authorization = authorize_shotcaller_reconciliation(broad, pair)
    authorization["artifact_pair"]["status_prefix"] = "rosters"
    write_json(broad["plan_path"], broad["plan"])
    refused(
        lambda: run_fixture(broad, root / "broad-sandbox", "broad"),
        "plan_invalid",
    )

    duplicate = fixture_plan(root / "duplicate")
    pair = add_shotcaller_initialization_mismatch(duplicate)
    authorization = authorize_shotcaller_reconciliation(duplicate, pair)
    plan_text = duplicate["plan_path"].read_text(encoding="utf-8")
    duplicate_value = json.dumps(authorization, sort_keys=True, separators=(",", ":"))
    plan_text = plan_text.replace(
        '"reconciliation":', f'"reconciliation":{duplicate_value},"reconciliation":', 1
    )
    duplicate["plan_path"].write_text(plan_text, encoding="utf-8")
    refused(
        lambda: run_fixture(duplicate, root / "duplicate-sandbox", "duplicate"),
        "duplicate_key",
    )

    ambiguous = fixture_plan(root / "ambiguous")
    pair = add_shotcaller_initialization_mismatch(ambiguous)
    authorize_shotcaller_reconciliation(ambiguous, pair)
    manifest_path = ambiguous["legacy"] / "import-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    duplicate_roster = dict(manifest["canonical_sources"]["rosters"][0])
    duplicate_roster["artifact_id"] = "R1-shotcaller-alias"
    manifest["canonical_sources"]["rosters"].append(duplicate_roster)
    write_json(manifest_path, manifest)
    refused(
        lambda: run_fixture(ambiguous, root / "ambiguous-sandbox", "ambiguous"),
        "reconciliation_ambiguous",
    )

    non_shotcaller = fixture_plan(root / "non-shotcaller")
    pair = add_shotcaller_initialization_mismatch(non_shotcaller)
    status = json.loads(pair["status"].read_text(encoding="utf-8"))
    status["role"] = "champion"
    write_json(pair["status"], status)
    authorize_shotcaller_reconciliation(non_shotcaller, pair)
    refused(
        lambda: run_fixture(
            non_shotcaller, root / "non-shotcaller-sandbox", "non-shotcaller"
        ),
        "reconciliation_non_shotcaller",
    )

    post_initialization = fixture_plan(root / "post-initialization")
    pair = add_shotcaller_initialization_mismatch(post_initialization)
    with pair["updates"].open("ab") as handle:
        handle.write(
            _stable_bytes(
                {
                    "at": "2026-01-01T00:02:00Z",
                    "status": "progress",
                    "update": "Synthetic work continued after initialization.",
                }
            )
        )
    authorize_shotcaller_reconciliation(post_initialization, pair)
    refused(
        lambda: run_fixture(
            post_initialization,
            root / "post-initialization-sandbox",
            "post-initialization",
        ),
        "reconciliation_post_initialization",
    )


def test_backup_fault_blocks_resumably_without_live_change(root: Path) -> None:
    fixture = fixture_plan(root)
    temporary_root = root / "sandbox"
    temporary_root.mkdir()
    before = fixture["hook"].read_bytes()

    def fail(stage: str) -> None:
        if stage.startswith("after_backup:"):
            raise StorageRefusal("synthetic_backup_fault", "synthetic backup fault")

    refused(
        lambda: run_pre_cutover(
            temporary_root,
            "fault",
            plan_path=fixture["plan_path"],
            sentinel_paths=(fixture["legacy"],),
            config_sentinel=fixture["hook"],
            process_sentinel=fixture["processes"],
            source_root=ROOT,
            fault=fail,
        ),
        "synthetic_backup_fault",
    )
    operation = json.loads(
        (temporary_root / "league-fault-precutover/precutover-operation.json").read_text(
            encoding="utf-8"
        )
    )
    assert operation["state"] == "blocked"
    assert operation["resumable"] is True
    assert operation["error_code"] == "synthetic_backup_fault"
    assert [item["state"] for item in operation["history"]] == [
        "planned",
        "executing",
        "blocked",
    ]
    assert fixture["hook"].read_bytes() == before


def test_schema_contracts_are_current_and_state_specific() -> None:
    acceptance = json.loads(
        (ROOT / "schema/league-acceptance-receipt.schema.json").read_text(encoding="utf-8")
    )
    receipt = json.loads(
        (ROOT / "schema/league-pre-cutover-receipt.schema.json").read_text(encoding="utf-8")
    )
    plan = json.loads(
        (ROOT / "schema/league-pre-cutover-plan.schema.json").read_text(encoding="utf-8")
    )
    shared = json.loads(
        (ROOT / "schema/league-legacy-reconciliation.schema.json").read_text(
            encoding="utf-8"
        )
    )
    migration = acceptance["$defs"]["migrationReceipt"]
    assert migration["properties"]["to_version"] == {"const": CURRENT_SCHEMA_VERSION}
    assert migration["properties"]["applied"]["maxItems"] == CURRENT_SCHEMA_VERSION
    operation = receipt["$defs"]["operation"]
    assert operation.keys() == {"oneOf"}
    assert {item["properties"]["state"]["const"] for item in operation["oneOf"]} == {
        "awaiting_authority",
        "blocked",
    }
    assert all(item["additionalProperties"] is False for item in operation["oneOf"])
    assert "slice" not in receipt["$defs"]
    shared_reference = "league-legacy-reconciliation.schema.json#/$defs/"
    assert plan["$defs"]["legacyResolution"]["oneOf"][1]["properties"]["triple"] == {
        "$ref": f"{shared_reference}legacyTriple"
    }
    assert receipt["$defs"]["sha256"] == {"$ref": f"{shared_reference}sha256"}
    assert receipt["$defs"]["legacyTriple"] == {
        "$ref": f"{shared_reference}legacyTriple"
    }
    assert set(shared["$defs"]) == {"sha256", "legacyTriple"}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="league-precutover-test-") as directory:
        base = Path(directory)
        test_command_e2e_and_deterministic_manifest(base / "command")
        test_invalid_plan_and_root_overlap_refuse_before_home(base / "invalid")
        test_shotcaller_initialization_mismatch_refuses_without_reconciliation(
            base / "mismatch"
        )
        test_exact_shotcaller_reconciliation_is_snapshot_only_and_deterministic(
            base / "reconciled"
        )
        test_wrong_reconciliation_hash_refuses_without_source_mutation(
            base / "wrong-hash"
        )
        test_reconciliation_scope_and_initialization_guards(base / "guards")
        test_backup_fault_blocks_resumably_without_live_change(base / "fault")
    test_schema_contracts_are_current_and_state_specific()
    print("PASS pre-cutover isolated command, parity, supervision, and failure tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
