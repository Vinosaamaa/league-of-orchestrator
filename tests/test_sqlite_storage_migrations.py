#!/usr/bin/env python3
"""Focused migration, policy, backup, rollback, and corruption tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_store import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    SQLiteStorage,
    journal_policy,
)
from league.storage import StorageRefusal  # noqa: E402
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


def test_loaded_runtime_gate(root: Path) -> None:
    assert journal_policy((3, 51, 2)) == ("DELETE", "loaded_sqlite_below_3.51.3")
    assert journal_policy((3, 51, 3)) == ("WAL", None)
    assert journal_policy(None) == ("DELETE", "loaded_sqlite_version_unverifiable")
    assert journal_policy((3, 53, 4), request_wal=False) == ("DELETE", "wal_not_requested")
    _, receipt = migrated_state(root, "rollback", request_wal=False)
    assert receipt["policy"]["journal_mode"] == "DELETE"
    assert receipt["policy"]["wal_refusal"] == "wal_not_requested"


def test_transactional_upgrade_backup_and_rollback(root: Path) -> None:
    state, first = migrated_state(root, "upgrade", target_version=1)
    assert first["applied"] == [1]
    assert first["to_version"] == 1

    def crash(point: str) -> None:
        if point == "after_migration_2":
            raise InjectedCrash(point)

    try:
        with SQLiteStorage.for_migration(state) as store:
            store.migrate(backup_name="backups/pre-v2.sqlite3", fault=crash)
    except InjectedCrash:
        pass
    else:
        raise AssertionError("migration crash was not injected")
    assert (state / "backups/pre-v2.sqlite3").is_file()
    with SQLiteStorage.for_migration(state) as store:
        unchanged = store.migrate(target_version=1)
        assert unchanged["from_version"] == unchanged["to_version"] == 1
        upgraded = store.migrate(backup_name="backups/pre-v2-retry.sqlite3")
        assert upgraded["from_version"] == 1
        assert upgraded["to_version"] == CURRENT_SCHEMA_VERSION
        assert upgraded["applied"] == list(range(2, CURRENT_SCHEMA_VERSION + 1))
        assert upgraded["backup"]["database_schema_version"] == 1
        assert store.integrity()["ok"]
        indexes = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            "ix_tasks_coordinator_state",
            "ix_assignments_state",
            "ix_requests_unresolved",
            "ix_outbox_recipient_state",
            "ix_runtime_task_state",
            "ux_routing_escalation_child",
            "ix_cleanup_actions",
            "ix_projects_repository_key",
            "ix_project_alias_lookup",
            "ix_project_suggestion_squad",
            "ix_roster_tasks_project_state",
            "ix_events_occurred",
            "ix_callsign_queue_scan",
            "ux_squad_accepting_shotcaller",
            "ux_owner_changed_per_rollover",
            "ux_pending_squad_registration",
            "ux_pending_shotcaller_registration",
            "ux_hidden_dispatch_assignment",
            "ix_request_progress_latest",
            "ix_request_progress_due",
            "ix_report_requests_updated",
            "ix_activity_evidence_time",
            "ix_report_specs_created",
            "ix_repository_artifacts_task",
        } <= indexes
        assert [migration.version for migration in MIGRATIONS] == list(
            range(1, CURRENT_SCHEMA_VERSION + 1)
        )
        assert MIGRATIONS[-7].name == "advisory-project-catalog-and-roster-indexes"
        assert MIGRATIONS[-7].checksum == "5477db9879d6a4a9a29bb8188b398bd6db9a7a786e40e86ab819a0a938790faf"
        assert MIGRATIONS[-6].name == "guarded-rollover-and-shuffled-callsign-queue"
        assert MIGRATIONS[-6].checksum == "879ef4addfe6725e31c31a5aa1db9078d7c066a26610eaa2753f749c6e53ab75"
        assert MIGRATIONS[-5].name == "bounded-reporting-and-outbound-privacy"
        assert MIGRATIONS[-5].checksum == "bebe90eb841eac2a0b42d3f89e321cb4f3f8b23b02d92febf5a4ea2a50727cde"
        assert MIGRATIONS[-4].name == "bounded-routing-policy-and-request-progress"
        assert MIGRATIONS[-4].checksum == "593e2cf05d0200463800b6be7cbf5918a9b5fc3304f793d2ec3fad30b538e80c"
        assert MIGRATIONS[-3].name == "repository-owned-artifact-publication"
        assert MIGRATIONS[-3].checksum == "9231da781de45a8e912cd7193034a0b1b56f3a13e5e737e5681f18f6c6e3c852"
        assert MIGRATIONS[-2].name == "prompt-runtime-quarantine"
        assert MIGRATIONS[-2].checksum == "b2f75ee64473d8be4188052258c4e69c9cd5f12df1adb8535d55c1b523633df5"
        assert MIGRATIONS[-1].name == "prompt-quarantine-watcher-generation"
        assert MIGRATIONS[-1].checksum == "211fb5b225a63065a4607bebf38171f57d182d12e32701ad882d47b7ce5845e4"
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_v5_to_v6_rebuild_rolls_back_and_initializes_shuffled_order(root: Path) -> None:
    state, _ = migrated_state(root, "v5-to-v6", target_version=5)
    with SQLiteStorage.for_migration(state) as store:
        store.connection.executemany(
            """
            INSERT INTO callsigns(callsign,pool_role,enabled,pool_position,last_released_at)
            VALUES(?,'champion',1,?,NULL)
            """,
            (("Alpha", 1), ("Beta", 2), ("Gamma", 3)),
        )

        def crash(point: str) -> None:
            if point == "after_migration_6":
                raise InjectedCrash(point)

        try:
            store.migrate(backup_name="backups/pre-v6.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("v6 migration crash was not injected")
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='callsign_queue'"
        ).fetchone()[0] == 0
        store.migrate(backup_name="backups/pre-v6-retry.sqlite3")
        order = [
            row[0]
            for row in store.connection.execute(
                """
                SELECT callsign FROM callsign_queue
                 WHERE pool_role='champion' ORDER BY queue_position
                """
            )
        ]
        assert order != sorted(order)
        assert store.integrity()["ok"]


def test_v6_to_v7_rolls_back_and_applies_privacy_defaults(root: Path) -> None:
    state, _ = migrated_state(root, "v6-to-v7", target_version=6)
    with SQLiteStorage.for_migration(state) as store:
        store.connection.execute(
            """
            INSERT INTO projects(project_id,repository,state,version,updated_at,summary)
            VALUES('project:v6','https://example.invalid/synthetic/v6.git','active',1,
                   '2026-01-01T00:00:00Z','Synthetic v6 project')
            """
        )

        def crash(point: str) -> None:
            if point == "after_migration_7":
                raise InjectedCrash(point)

        try:
            store.migrate(backup_name="backups/pre-v7.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("v7 migration crash was not injected")
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 6
        columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(projects)")
        }
        assert "export_policy" not in columns
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='report_specs'"
        ).fetchone()[0] == 0
        store.migrate(backup_name="backups/pre-v7-retry.sqlite3")
        project = store.connection.execute(
            """
            SELECT repository_visibility,export_policy,root_classification,
                   repository_classification FROM projects WHERE project_id='project:v6'
            """
        ).fetchone()
        assert tuple(project) == ("unknown", "deny", "local_only", "local_only")
        assert store.integrity()["ok"]


def test_schema_refusals_without_test_sql(root: Path) -> None:
    future, _ = migrated_state(root, "future")
    database = future / "league.sqlite3"
    payload = bytearray(database.read_bytes())
    payload[60:64] = (CURRENT_SCHEMA_VERSION + 1).to_bytes(4, "big")
    database.write_bytes(payload)
    refused(lambda: SQLiteStorage(future), "schema_newer")

    drift, _ = migrated_state(root, "drift")
    database = drift / "league.sqlite3"
    payload = bytearray(database.read_bytes())
    checksum = MIGRATIONS[0].checksum.encode("ascii")
    offset = payload.find(checksum)
    assert offset >= 0
    payload[offset : offset + len(checksum)] = b"0" * len(checksum)
    database.write_bytes(payload)
    refused(lambda: SQLiteStorage(drift), "migration_drift")


def test_v3_upgrade_preserves_cleanup_and_indexes_legacy_project(root: Path) -> None:
    state, _ = migrated_state(root, "v3-cleanup", target_version=3)
    with SQLiteStorage.for_migration(state) as store:
        store.connection.execute(
            """
            INSERT INTO tasks(task_id,summary,state,version,updated_at)
            VALUES('task:v3-cleanup','synthetic v3 cleanup','completed',1,'2026-01-01T00:00:00Z')
            """
        )
        store.connection.execute(
            """
            INSERT INTO projects(project_id,repository,state,version,updated_at)
            VALUES('project:legacy','https://EXAMPLE.invalid/synthetic/legacy.git',
                   'active',1,'2026-01-01T00:00:00Z')
            """
        )
        store.connection.execute(
            """
            INSERT INTO cleanup_obligations
              (cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,version,updated_at)
            VALUES('cleanup:v3-cleanup','task:v3-cleanup','pending','terminal_task',
                   'Reconcile exact task resources',1,'2026-01-01T00:00:00Z')
            """
        )
        receipt = store.migrate(backup_name="backups/pre-v4.sqlite3")
        assert receipt["from_version"] == 3
        assert receipt["applied"] == list(range(4, CURRENT_SCHEMA_VERSION + 1))
        row = store.connection.execute(
            "SELECT * FROM cleanup_obligations WHERE task_id='task:v3-cleanup'"
        ).fetchone()
        assert row["cleanup_obligation_id"] == "cleanup:v3-cleanup"
        assert row["cleanup_state"] == "pending" and row["version"] == 1
        assert row["owner_id"] is None and row["task_class"] is None
        project = store.resolve_project("git@example.invalid:synthetic/legacy.git")
        assert project is not None and project["project_id"] == "project:legacy"
        repository_key = store.connection.execute(
            "SELECT repository_key FROM projects WHERE project_id='project:legacy'"
        ).fetchone()[0]
        assert repository_key == "example.invalid/synthetic/legacy"
        assert store.integrity()["ok"]


def test_backup_collision_and_corruption(root: Path) -> None:
    state, _ = migrated_state(root, "backup")
    with SQLiteStorage(state) as store:
        receipt = store.backup("backups/verified.sqlite3")
        assert receipt["integrity"] == "ok"
        assert len(receipt["sha256"]) == 64
        refused(lambda: store.backup("backups/verified.sqlite3"), "output_collision")

        def crash(point: str) -> None:
            if point == "after_backup_copy":
                raise InjectedCrash(point)

        try:
            store.backup("backups/retryable.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("backup crash was not injected")
        assert not (state / "backups/retryable.sqlite3").exists()
        assert store.backup("backups/retryable.sqlite3")["integrity"] == "ok"

    corrupt, _ = migrated_state(root, "corrupt")
    database = corrupt / "league.sqlite3"
    payload = bytearray(database.read_bytes())
    payload[:16] = b"not-a-database!!"
    database.write_bytes(payload)
    refused(lambda: SQLiteStorage(corrupt), "database_error")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-storage-migration-") as temporary:
        root = Path(temporary)
        test_loaded_runtime_gate(root)
        test_transactional_upgrade_backup_and_rollback(root)
        test_v5_to_v6_rebuild_rolls_back_and_initializes_shuffled_order(root)
        test_v6_to_v7_rolls_back_and_applies_privacy_defaults(root)
        test_schema_refusals_without_test_sql(root)
        test_v3_upgrade_preserves_cleanup_and_indexes_legacy_project(root)
        test_backup_collision_and_corruption(root)
    print("PASS: SQLite runtime gate, migrations, verified backup, rollback, drift, and corruption refusal")


if __name__ == "__main__":
    main()
