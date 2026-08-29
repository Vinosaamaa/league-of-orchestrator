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
from league.sqlite_store import CURRENT_SCHEMA_VERSION, SQLiteStorage  # noqa: E402
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


def add_retired_watcher_cursor(
    source: Path, fixture: dict[str, object], *, authorize: bool = True
) -> dict[str, object]:
    archive = source / "archives/Bard"
    status_path = archive / "status.json"
    updates_path = archive / "updates.jsonl"
    original_record = fixture["runtime_root"] / "rosters/Garen/champions/Bard"
    original_updates = str(original_record / "updates.jsonl")
    original_status = str(original_record / "status.json")
    lines = [
        stable_json(
            {
                "at": "2026-01-01T00:00:00Z",
                "status": "working",
                "update": "Synthetic retired assignment started.",
            }
        ),
        stable_json(
            {
                "at": "2026-01-01T00:01:00Z",
                "status": "ready_to_land",
                "update": "Synthetic retired assignment became ready.",
            }
        ),
        stable_json(
            {
                "at": "2026-01-01T00:02:00Z",
                "status": "completed",
                "update": "Synthetic retired assignment was archived.",
            }
        ),
    ]
    status = {
        "callsign": "Bard",
        "role": "champion",
        "shotcaller": "Garen",
        "kind": "codex-thread",
        "address": "w1:p8",
        "thread_id": "88888888-8888-4888-8888-888888888888",
        "backend": "herdr",
        "task_id": "synthetic-retired-task",
        "repository": REPOSITORY,
        "issue": 19,
        "branch": "agent/synthetic/retired",
        "worktree": str(fixture["runtime_root"] / "worktrees/retired"),
        "task": "Synthetic retired task",
        "status": "COMPLETED",
        "updated_at": "2026-01-01T00:02:00Z",
        "update": "Synthetic retired assignment was archived.",
        "blocker": None,
        "next": "Retain synthetic archive evidence.",
    }
    archive.mkdir(parents=True)
    status_path.write_text(stable_json(status) + "\n", encoding="utf-8")
    updates_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cursor_offset = len((lines[0] + "\n" + lines[1] + "\n").encode("utf-8"))
    first_id = hashlib.sha256(
        f"{original_updates}\0{0}\0{lines[0]}".encode("utf-8")
    ).hexdigest()
    second_offset = len((lines[0] + "\n").encode("utf-8"))
    second_id = hashlib.sha256(
        f"{original_updates}\0{second_offset}\0{lines[1]}".encode("utf-8")
    ).hexdigest()

    manifest = json.loads(Path(fixture["manifest"]).read_text(encoding="utf-8"))
    manifest["retained_files"].extend(
        [
            {
                "artifact_id": "T5-retired-Bard-status",
                "class": "legacy-archive",
                "path": "archives/Bard/status.json",
            },
            {
                "artifact_id": "T5-retired-Bard-updates",
                "class": "legacy-archive",
                "path": "archives/Bard/updates.jsonl",
            },
        ]
    )
    watcher_entry = manifest["canonical_sources"]["watcher_states"][0]
    if authorize:
        watcher_entry["retired_cursors"] = [
            {
                "source_path": original_updates,
                "expected_next_offset": cursor_offset,
                "retained_status_artifact_id": "T5-retired-Bard-status",
                "retained_updates_artifact_id": "T5-retired-Bard-updates",
                "expected_status_sha256": hashlib.sha256(status_path.read_bytes()).hexdigest(),
                "expected_updates_sha256": hashlib.sha256(updates_path.read_bytes()).hexdigest(),
                "disposition": "retained_non_active",
            }
        ]
    Path(fixture["manifest"]).write_text(stable_json(manifest) + "\n", encoding="utf-8")

    watcher_path = source / "watcher/Garen/state.json"
    watcher = json.loads(watcher_path.read_text(encoding="utf-8"))
    watcher["last_active"].append(original_status)
    watcher["offsets"][original_updates] = cursor_offset
    watcher["seen"].extend([first_id, second_id])
    watcher["delivered_events"][second_id] = {"channel": "watcher"}
    watcher["last_event_id"] = second_id
    watcher_path.write_text(stable_json(watcher) + "\n", encoding="utf-8")
    return {
        "authorization": watcher_entry.get("retired_cursors", [None])[0],
        "event_ids": {first_id, second_id},
        "source_path": original_updates,
    }


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
        assert report["target_schema_version"] == CURRENT_SCHEMA_VERSION
        assert plan["target_schema_version"] == CURRENT_SCHEMA_VERSION
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
        agents = {row["callsign"]: row for row in plan["rows"]["agent_instances"]}
        garen_record = str(fixture["runtime_root"] / "rosters/Garen/status.json")
        assert json.loads(agents["Garen"]["metadata_json"])[
            "legacy_visible_assignment"
        ] == {
            "record": garen_record,
            "record_directory": str(fixture["runtime_root"] / "rosters/Garen"),
            "locator_kind": "status_snapshot",
        }
        assert json.loads(agents["Thresh"]["metadata_json"])[
            "legacy_visible_assignment"
        ] == {
            "record": fixture["champion_record"],
            "record_directory": fixture["champion_record"],
            "locator_kind": "record_directory",
        }
        launch = plan["rows"]["launch_attempts"][0]
        assert launch["record_locator"] == str(
            fixture["runtime_root"] / "rosters/Garen/champions/Pyke"
        )
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
        rollback_agents = {
            row["callsign"]: row
            for row in rollback_value["tables"]["agent_instances"]
        }
        assert json.loads(rollback_agents["Garen"]["metadata_json"])[
            "legacy_visible_assignment"
        ]["record"] == garen_record
        assert json.loads(rollback_agents["Thresh"]["metadata_json"])[
            "legacy_visible_assignment"
        ]["record"] == fixture["champion_record"]
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


def test_visible_assignment_record_malformed_and_stale_refusals(root: Path) -> None:
    malformed_source = root / "malformed-visible-record"
    malformed_source.mkdir()
    fixture = write_complete_fixture(malformed_source)
    pool_path = malformed_source / "league-champions.json"
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    pool["in_use"]["Thresh"]["record"] = "relative/Garen/champions/Thresh"
    pool_path.write_text(stable_json(pool) + "\n", encoding="utf-8")
    refused(
        lambda: build_import_plan(malformed_source, fixture["manifest"]),
        "malformed_input",
    )

    stale_owner_source = root / "stale-visible-record-owner"
    stale_owner_source.mkdir()
    fixture = write_complete_fixture(stale_owner_source)
    pool_path = stale_owner_source / "league-champions.json"
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    pool["in_use"]["Thresh"]["record"] = str(
        fixture["runtime_root"] / "rosters/Other/champions/Thresh"
    )
    pool_path.write_text(stable_json(pool) + "\n", encoding="utf-8")
    refused(
        lambda: build_import_plan(stale_owner_source, fixture["manifest"]),
        "identity_collision",
    )

    stale_pending_source = root / "stale-visible-record-pending"
    stale_pending_source.mkdir()
    fixture = write_complete_fixture(stale_pending_source)
    pool_path = stale_pending_source / "league-champions.json"
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    pool["in_use"]["Pyke"]["record"] = str(
        fixture["runtime_root"] / "rosters/Garen/champions/Other"
    )
    pool_path.write_text(stable_json(pool) + "\n", encoding="utf-8")
    refused(
        lambda: build_import_plan(stale_pending_source, fixture["manifest"]),
        "identity_collision",
    )


def test_retired_watcher_cursor_classification(root: Path) -> None:
    source = root / "retired-cursor"
    source.mkdir()
    fixture = write_complete_fixture(source)
    retired = add_retired_watcher_cursor(source, fixture)
    watcher_path = source / "watcher/Garen/state.json"
    watcher_value = json.loads(watcher_path.read_text(encoding="utf-8"))
    unbound_seen = "a" * 64
    unbound_delivered = "b" * 64
    watcher_value["seen"].append(unbound_seen)
    watcher_value["delivered_events"][unbound_delivered] = {"channel": "watcher"}
    watcher_path.write_text(stable_json(watcher_value) + "\n", encoding="utf-8")
    manifest_value = json.loads(Path(fixture["manifest"]).read_text(encoding="utf-8"))
    manifest_value["canonical_sources"]["watcher_states"][0][
        "retired_unbound_receipts"
    ] = {
        "expected_watcher_sha256": hashlib.sha256(watcher_path.read_bytes()).hexdigest(),
        "seen_only": [unbound_seen],
        "delivered": [
            {
                "legacy_event_id": unbound_delivered,
                "channel": "watcher",
                "seen": False,
            }
        ],
        "disposition": "retained_unbound_no_delivery_history",
    }
    Path(fixture["manifest"]).write_text(
        stable_json(manifest_value) + "\n", encoding="utf-8"
    )
    source_sentinels = {
        path: path.read_bytes()
        for path in (
            watcher_path,
            source / "archives/Bard/status.json",
            source / "archives/Bard/updates.jsonl",
        )
    }
    plan = build_import_plan(source, fixture["manifest"])
    assert all(path.read_bytes() == before for path, before in source_sentinels.items())
    assert "Bard" not in {row["callsign"] for row in plan["rows"]["agent_instances"]}
    assert len(plan["rows"]["events"]) == 2
    assert len(plan["rows"]["watcher_cursors"]) == 1
    assert len(plan["rows"]["watcher_seen"]) == 2
    assert len(plan["rows"]["deliveries"]) == 2
    assert not retired["event_ids"] & {
        row["legacy_event_id"] for row in plan["rows"]["watcher_seen"]
    }
    watcher = plan["rows"]["watcher_scopes"][0]
    metadata = json.loads(watcher["metadata_json"])
    classification = metadata["retired_watcher_cursors"][0]
    assert classification["source_path"] == retired["source_path"]
    assert classification["cursor_event_count"] == 2
    assert classification["archived_event_count"] == 3
    assert classification["seen_count"] == 2
    assert classification["delivered_count"] == 1
    assert classification["was_last_active"] is True
    assert classification["was_last_event"] is True
    assert classification["disposition"] == "retained_non_active_no_history_or_delivery"
    assert watcher["last_event_id"] is None
    assert metadata["retired_unbound_receipts"]["seen_only_count"] == 1
    assert metadata["retired_unbound_receipts"]["delivered_count"] == 1
    assert metadata["retired_unbound_receipts"]["disposition"] == (
        "retained_unbound_no_delivery_history"
    )

    stale_receipt_manifest = copy.deepcopy(manifest_value)
    stale_receipt_manifest["canonical_sources"]["watcher_states"][0][
        "retired_unbound_receipts"
    ]["expected_watcher_sha256"] = "0" * 64
    Path(fixture["manifest"]).write_text(
        stable_json(stale_receipt_manifest) + "\n", encoding="utf-8"
    )
    refused(
        lambda: build_import_plan(source, fixture["manifest"]),
        "identity_collision",
    )
    Path(fixture["manifest"]).write_text(
        stable_json(manifest_value) + "\n", encoding="utf-8"
    )

    state, _ = migrated_state(root, "retired-cursor-state")
    with SQLiteStorage(state) as store:
        store.apply_import(plan, plan["report_digest"])
        rollback = json.loads(
            store.export_bytes(format_name="json", purpose="rollback", max_records=1000)
        )
        stored = rollback["tables"]["watcher_scopes"][0]
        assert json.loads(stored["metadata_json"])["retired_watcher_cursors"] == [
            classification
        ]
    assert all(path.read_bytes() == before for path, before in source_sentinels.items())

    missing = root / "retired-cursor-missing"
    missing.mkdir()
    missing_fixture = write_complete_fixture(missing)
    add_retired_watcher_cursor(missing, missing_fixture, authorize=False)
    refused(
        lambda: build_import_plan(missing, missing_fixture["manifest"]),
        "unknown_consumer",
    )

    stale = root / "retired-cursor-stale"
    stale.mkdir()
    stale_fixture = write_complete_fixture(stale)
    add_retired_watcher_cursor(stale, stale_fixture)
    stale_manifest = json.loads(Path(stale_fixture["manifest"]).read_text(encoding="utf-8"))
    stale_manifest["canonical_sources"]["watcher_states"][0]["retired_cursors"][0][
        "expected_updates_sha256"
    ] = "0" * 64
    Path(stale_fixture["manifest"]).write_text(
        stable_json(stale_manifest) + "\n", encoding="utf-8"
    )
    refused(
        lambda: build_import_plan(stale, stale_fixture["manifest"]),
        "identity_collision",
    )

    overlap = root / "retired-cursor-overlap"
    overlap.mkdir()
    overlap_fixture = write_complete_fixture(overlap)
    add_retired_watcher_cursor(overlap, overlap_fixture)
    overlap_manifest = json.loads(
        Path(overlap_fixture["manifest"]).read_text(encoding="utf-8")
    )
    overlap_declaration = overlap_manifest["canonical_sources"]["watcher_states"][0][
        "retired_cursors"
    ][0]
    overlap_declaration["retained_updates_artifact_id"] = overlap_declaration[
        "retained_status_artifact_id"
    ]
    Path(overlap_fixture["manifest"]).write_text(
        stable_json(overlap_manifest) + "\n", encoding="utf-8"
    )
    refused(
        lambda: build_import_plan(overlap, overlap_fixture["manifest"]),
        "identity_collision",
    )

    pending = root / "retired-cursor-pending"
    pending.mkdir()
    pending_fixture = write_complete_fixture(pending)
    pending_retired = add_retired_watcher_cursor(pending, pending_fixture)
    pending_watcher = pending / "watcher/Garen/state.json"
    pending_value = json.loads(pending_watcher.read_text(encoding="utf-8"))
    pending_id = next(
        event_id
        for event_id in pending_retired["event_ids"]
        if event_id not in pending_value["delivered_events"]
    )
    pending_value["pending_events"][pending_id] = {"event_id": pending_id}
    pending_watcher.write_text(stable_json(pending_value) + "\n", encoding="utf-8")
    refused(
        lambda: build_import_plan(pending, pending_fixture["manifest"]),
        "unknown_consumer",
    )

    malformed = root / "retired-cursor-malformed"
    malformed.mkdir()
    malformed_fixture = write_complete_fixture(malformed)
    add_retired_watcher_cursor(malformed, malformed_fixture)
    malformed_watcher = malformed / "watcher/Garen/state.json"
    malformed_value = json.loads(malformed_watcher.read_text(encoding="utf-8"))
    malformed_value["seen"].append({"not": "an event ID"})
    malformed_watcher.write_text(stable_json(malformed_value) + "\n", encoding="utf-8")
    refused(
        lambda: build_import_plan(malformed, malformed_fixture["manifest"]),
        "malformed_input",
    )


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
        stale = copy.deepcopy(plan)
        stale.pop("target_schema_version")
        stale["report"].pop("target_schema_version")
        refused(
            lambda: store.apply_import(stale, plan["report_digest"]),
            "import_plan_incompatible",
        )
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
        test_visible_assignment_record_malformed_and_stale_refusals(root)
        test_retired_watcher_cursor_classification(root)
        test_import_crash_atomicity_and_plan_tamper(root)
        test_descriptor_bound_read_survives_path_replacement(root)
    print("PASS: complete dry-run import, parity/export, malformed/collision refusal, FK, and crash atomicity")


if __name__ == "__main__":
    main()
