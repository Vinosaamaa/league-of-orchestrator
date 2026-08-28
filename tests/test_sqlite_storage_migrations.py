#!/usr/bin/env python3
"""Focused migration, policy, backup, rollback, and corruption tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_store import MIGRATIONS, SQLiteStorage, journal_policy  # noqa: E402
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
        assert upgraded["to_version"] == 3
        assert upgraded["applied"] == [2, 3]
        assert upgraded["backup"]["database_schema_version"] == 1
        assert store.integrity()["ok"]


def test_schema_refusals_without_test_sql(root: Path) -> None:
    future, _ = migrated_state(root, "future")
    database = future / "league.sqlite3"
    payload = bytearray(database.read_bytes())
    payload[60:64] = (4).to_bytes(4, "big")
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
        test_schema_refusals_without_test_sql(root)
        test_backup_collision_and_corruption(root)
    print("PASS: SQLite runtime gate, migrations, verified backup, rollback, drift, and corruption refusal")


if __name__ == "__main__":
    main()
