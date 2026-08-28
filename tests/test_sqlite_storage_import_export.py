#!/usr/bin/env python3
"""Focused dry-run import, parity, export, collision, and crash tests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import league.importer as importer  # noqa: E402
from league.importer import AUDIT_COVERAGE, build_import_plan  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from storage_fixture import (  # noqa: E402
    AT2,
    CHAMPION_ID,
    REPOSITORY,
    SHOTCALLER_ID,
    TASK_ID,
    stable_json,
    write_complete_fixture,
)
from storage_test_support import migrated_state  # noqa: E402


class InjectedCrash(RuntimeError):
    pass


def refused(operation, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def test_complete_dry_run_apply_and_round_trip(root: Path) -> None:
    source = root / "source"
    source.mkdir()
    fixture = write_complete_fixture(source)
    state, _ = migrated_state(root, "state")
    with SQLiteStorage(state) as store:
        plan = build_import_plan(
            source, fixture["manifest"], target_counts=store.import_target_counts()
        )
        report = plan["report"]
        assert report["schema"] == "league.import-report.v1"
        assert report["dry_run"] and not report["applied"] and report["eligible"]
        assert report["unknown_consumers"] == []
        assert report["target_collisions"] == {}
        assert report["audit_coverage"] == AUDIT_COVERAGE
        assert report["artifact_counts"] == {
            "roster": 2,
            "pending_launch": 1,
            "watcher_state": 1,
            "visible_callsign_pool": 1,
            "hidden_worker_pool": 1,
            "lead_relay_state": 1,
            "resource_registry": 1,
        }
        assert [item["source_order"] for item in report["ordering"]] == list(
            range(len(report["ordering"]))
        )
        assert report["retained_files"][0]["artifact_id"] == "T5-evidence"
        applied = store.apply_import(plan, report["report_digest"])
        assert applied["applied"] and not applied["dry_run"]
        assert store.integrity()["ok"]
        snapshot = store.agent_status(CHAMPION_ID)
        assert snapshot is not None
        assert snapshot["status"] == "progress"
        assert snapshot["version"] == 2
        assert snapshot["shotcaller_agent_id"] == SHOTCALLER_ID
        project = store.resolve_project(REPOSITORY)
        assert project is not None and project["repository"] == REPOSITORY

        inspection = store.export_bytes(
            format_name="json", purpose="inspection", max_records=1000
        )
        assert inspection == store.export_bytes(
            format_name="json", purpose="inspection", max_records=1000
        )
        assert fixture["private_marker"].encode("utf-8") not in inspection
        inspection_value = json.loads(inspection)
        assert inspection_value["canonical"] is False
        assert inspection_value["purpose"] == "inspection"
        assert inspection_value["tables"]["agent_instances"][1]["worktree"] == "[redacted]"

        rollback = store.export_bytes(format_name="json", purpose="rollback", max_records=1000)
        assert fixture["champion_worktree"].encode("utf-8") in rollback
        rollback_value = json.loads(rollback)
        assert rollback_value["purpose"] == "rollback"
        assert rollback_value["tables"]["events"][-1]["status"] == "progress"
        assert {row["state"] for row in rollback_value["tables"]["deliveries"]} == {
            "accepted",
            "pending",
        }
        assert all(
            row["state"] != "acknowledged" for row in rollback_value["tables"]["deliveries"]
        )
        available_champions = [
            row["callsign"]
            for row in rollback_value["tables"]["callsigns"]
            if row["pool_role"] == "champion" and row["pool_position"] is not None
        ]
        assert available_champions == ["Lux"]
        task = next(row for row in rollback_value["tables"]["tasks"] if row["task_id"] == TASK_ID)
        assert task["current_owner_agent_id"] == CHAMPION_ID
        jsonl = store.export_bytes(format_name="jsonl", purpose="inspection", max_records=1000)
        lines = [json.loads(line) for line in jsonl.splitlines()]
        assert lines[0]["kind"] == "metadata"
        assert len(lines) == lines[0]["record_count"] + 1
        assert all(line["canonical"] is False for line in lines)
        refused(
            lambda: store.export_bytes(format_name="json", purpose="inspection", max_records=1),
            "export_too_large",
        )
        refused(lambda: store.apply_import(plan, report["report_digest"]), "import_collision")

        collision_plan = build_import_plan(
            source, fixture["manifest"], target_counts=store.import_target_counts()
        )
        assert not collision_plan["report"]["eligible"]
        assert collision_plan["report"]["target_collisions"]["agent_instances"] == 3


def test_malformed_duplicate_unknown_and_foreign_key_refusals(root: Path) -> None:
    duplicate_source = root / "duplicate"
    duplicate_source.mkdir()
    fixture = write_complete_fixture(duplicate_source)
    status = duplicate_source / "rosters/Garen/champions/Thresh/status.json"
    status.write_text('{"callsign":"Thresh","callsign":"Other"}\n', encoding="utf-8")
    refused(lambda: build_import_plan(duplicate_source, fixture["manifest"]), "duplicate_key")

    malformed_source = root / "malformed"
    malformed_source.mkdir()
    fixture = write_complete_fixture(malformed_source)
    updates = malformed_source / "rosters/Garen/champions/Thresh/updates.jsonl"
    updates.write_bytes(updates.read_bytes().rstrip(b"\n"))
    refused(lambda: build_import_plan(malformed_source, fixture["manifest"]), "malformed_input")

    unknown_source = root / "unknown"
    unknown_source.mkdir()
    fixture = write_complete_fixture(unknown_source)
    manifest = json.loads(fixture["manifest"].read_text(encoding="utf-8"))
    manifest["unknown_consumers"] = ["synthetic-unknown-consumer"]
    fixture["manifest"].write_text(stable_json(manifest) + "\n", encoding="utf-8")
    refused(lambda: build_import_plan(unknown_source, fixture["manifest"]), "unknown_consumer")

    state_source = root / "foreign-source"
    state_source.mkdir()
    fixture = write_complete_fixture(state_source)
    state, _ = migrated_state(root, "foreign-state")
    with SQLiteStorage(state) as store:
        plan = build_import_plan(state_source, fixture["manifest"], target_counts=store.import_target_counts())
        store.apply_import(plan, plan["report_digest"])
        refused(
            lambda: store.allocate_callsign(
                "callsign-assignment:conflict",
                "11111111-1111-4111-8111-111111111111",
                "champion",
                "task",
                "missing-task",
                [],
                AT2,
            ),
            "agent_conflict",
        )
        assert store.agent_status("33333333-3333-4333-8333-333333333333") is None
        assert store.integrity()["ok"]


def test_import_crash_atomicity_and_plan_tamper(root: Path) -> None:
    source = root / "crash-source"
    source.mkdir()
    fixture = write_complete_fixture(source)
    state, _ = migrated_state(root, "crash-state")
    with SQLiteStorage(state) as store:
        plan = build_import_plan(source, fixture["manifest"], target_counts=store.import_target_counts())

        def crash(point: str) -> None:
            if point == "after_import_events":
                raise InjectedCrash(point)

        try:
            store.apply_import(plan, plan["report_digest"], fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("import crash was not injected")
        assert all(count == 0 for count in store.import_target_counts().values())
        assert store.integrity()["ok"]

        tampered = copy.deepcopy(plan)
        tampered["rows"]["agent_instances"][0]["task_id"] = "missing-task"
        refused(lambda: store.apply_import(tampered, plan["report_digest"]), "import_plan_invalid")
        assert all(count == 0 for count in store.import_target_counts().values())


def test_descriptor_bound_read_survives_path_replacement(root: Path) -> None:
    source = root / "descriptor-source"
    source.mkdir()
    fixture = write_complete_fixture(source)
    retained = source / "archives/synthetic-evidence.txt"
    original = b"x" * 70_000
    retained.write_bytes(original)
    target_inode = retained.stat().st_ino
    original_read = importer.os.read
    swapped = False

    def swapping_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        data = original_read(descriptor, size)
        if not swapped and os.fstat(descriptor).st_ino == target_inode:
            retained.rename(source / "archives/original-open-file.txt")
            retained.write_bytes(b"replacement after validated open")
            swapped = True
        return data

    importer.os.read = swapping_read
    try:
        plan = build_import_plan(source, fixture["manifest"])
    finally:
        importer.os.read = original_read
    assert swapped
    retained_report = plan["report"]["retained_files"][0]
    assert retained_report["bytes"] == len(original)
    assert retained_report["digest"] == hashlib.sha256(original).hexdigest()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-storage-import-") as temporary:
        root = Path(temporary)
        test_complete_dry_run_apply_and_round_trip(root)
        test_malformed_duplicate_unknown_and_foreign_key_refusals(root)
        test_import_crash_atomicity_and_plan_tamper(root)
        test_descriptor_bound_read_survives_path_replacement(root)
    print("PASS: complete dry-run import, parity/export, malformed/collision refusal, FK, and crash atomicity")


if __name__ == "__main__":
    main()
