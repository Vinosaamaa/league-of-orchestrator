#!/usr/bin/env python3
"""Focused decision tests for the non-live SQLite storage prototype."""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototypes"))

from run_storage_benchmark import REPOSITORY, run_benchmark  # noqa: E402
from sqlite_store import (  # noqa: E402
    SQLiteStorePrototype,
    StoreRefusal,
    choose_journal_mode,
    stable_export_bytes,
)


AT = "2026-08-28T00:00:00Z"


def assert_refused(operation, message: str) -> None:
    try:
        operation()
    except StoreRefusal:
        return
    raise AssertionError(message)


def seed(store: SQLiteStorePrototype) -> None:
    store.create_project("project-1", REPOSITORY, AT)
    store.create_task("task-1", "project-1", "synthetic task", AT)
    for callsign, agent_id in (("Riven", "agent-1"), ("Garen", "agent-2")):
        store.add_callsign(callsign)
        store.reserve_callsign(callsign, agent_id, "task-1", AT)


def test_runtime_gate(root: Path) -> None:
    assert choose_journal_mode((3, 51, 2)) == "DELETE"
    assert choose_journal_mode((3, 51, 3)) == "WAL"
    assert choose_journal_mode((3, 53, 4), request_wal=False) == "DELETE"
    with SQLiteStorePrototype(root / "rollback.sqlite3", request_wal=False) as store:
        assert store.policy.journal_mode == "DELETE"
        assert store.policy.foreign_keys
    expected = "WAL" if sqlite3.sqlite_version_info >= (3, 51, 3) else "DELETE"
    with SQLiteStorePrototype(root / "actual.sqlite3") as store:
        assert store.policy.loaded_runtime == sqlite3.sqlite_version_info[:3]
        assert store.policy.journal_mode == expected


def test_transactions_export_and_backup(root: Path) -> None:
    database = root / "contract.sqlite3"
    with SQLiteStorePrototype(database) as store:
        seed(store)
        assert_refused(
            lambda: store.create_task("orphan-task", "missing-project", "invalid", AT),
            "foreign keys must reject an unknown project",
        )
        event_id = store.transition("agent-1", 1, "progress", "synthetic update", AT)
        assert_refused(
            lambda: store.transition("agent-1", 1, "failed", "stale update", AT),
            "a stale transition must append no event",
        )
        store.claim_delivery(event_id, "agent-2", "claim-1")
        store.acknowledge_delivery(event_id, "agent-2", "claim-1", AT)
        store.create_task("task-2", "project-1", "other synthetic task", AT)
        store.add_callsign("Lux")
        store.reserve_callsign("Lux", "agent-3", "task-2", AT)
        assert_refused(
            lambda: store.transfer_task_owner("task-1", 1, "agent-3", AT),
            "task ownership must reject an agent assigned to another task",
        )
        transfer_event = store.transfer_task_owner("task-1", 1, "agent-2", AT)
        assert transfer_event == "task:task-1:2"
        assert store.project_lookup(REPOSITORY)["project_id"] == "project-1"
        assert store.integrity() == {
            "integrity": ["ok"],
            "foreign_key_violations": [],
            "ok": True,
        }
        first = stable_export_bytes(store.export())
        second = stable_export_bytes(store.export())
        assert first == second
        private_roots = (b"/" + b"Users" + b"/", b"/" + b"private" + b"/")
        assert all(marker not in first for marker in private_roots)
        exported = json.loads(first)
        assert exported["canonical"] is False
        receipt = store.backup(root / "backup.sqlite3")
        assert receipt == {
            "kind": "sqlite-backup",
            "schema": 1,
            "integrity": "ok",
            "foreign_key_violations": 0,
        }


def test_two_writer_compare_and_swap(root: Path) -> None:
    database = root / "concurrent.sqlite3"
    with SQLiteStorePrototype(database, busy_timeout_ms=1000) as first:
        seed(first)
        second = SQLiteStorePrototype(database, busy_timeout_ms=1000)
        try:
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            guard = threading.Lock()

            def write(store: SQLiteStorePrototype, update: str) -> None:
                barrier.wait()
                try:
                    store.transition("agent-1", 1, "progress", update, AT)
                    outcome = "committed"
                except StoreRefusal:
                    outcome = "refused"
                with guard:
                    outcomes.append(outcome)

            left = threading.Thread(target=write, args=(first, "writer-left"))
            right = threading.Thread(target=write, args=(second, "writer-right"))
            left.start()
            right.start()
            left.join(timeout=5)
            right.join(timeout=5)
            assert not left.is_alive() and not right.is_alive()
            assert sorted(outcomes) == ["committed", "refused"]
            assert first.agent_snapshot("agent-1")["version"] == 2
            assert first.integrity()["ok"]
        finally:
            second.close()


def test_migration_refusals(root: Path) -> None:
    database = root / "future.sqlite3"
    with SQLiteStorePrototype(database):
        pass
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()
    assert_refused(
        lambda: SQLiteStorePrototype(database),
        "an unknown future migration must fail closed",
    )
    checksum_database = root / "checksum.sqlite3"
    with SQLiteStorePrototype(checksum_database):
        pass
    connection = sqlite3.connect(checksum_database)
    connection.execute("UPDATE schema_migrations SET checksum='mismatch' WHERE version=1")
    connection.commit()
    connection.close()
    assert_refused(
        lambda: SQLiteStorePrototype(checksum_database),
        "migration checksum drift must fail closed",
    )


def test_bounded_benchmark() -> None:
    evidence = run_benchmark(agents=4, transitions=2, repetitions=2)
    assert evidence["workload"] == {
        "agents": 4,
        "transitions_per_agent": 2,
        "current_state_reads_per_agent": 1,
        "repetitions": 2,
    }
    assert evidence["correctness"]["snapshot_event_parity"]
    assert evidence["correctness"]["sqlite_integrity_check"] == "ok"
    assert evidence["filesystem_json_jsonl"]["operations_per_repetition"] == 12
    assert evidence["sqlite"]["operations_per_repetition"] == 12


def test_public_decision_artifacts() -> None:
    audit = (ROOT / "docs/research/json-jsonl-state-dependency-audit.md").read_text(
        encoding="utf-8"
    )
    adr = (ROOT / "docs/adr/0002-sqlite-canonical-store.md").read_text(
        encoding="utf-8"
    )
    evidence = (ROOT / "docs/research/sqlite-storage-decision-evidence.md").read_text(
        encoding="utf-8"
    )
    benchmark = (ROOT / "docs/research/sqlite-storage-prototype-benchmark.md").read_text(
        encoding="utf-8"
    )
    expected_ids = {
        *(f"R{number}" for number in range(1, 5)),
        *(f"L{number}" for number in range(1, 4)),
        *(f"W{number}" for number in range(1, 4)),
        *(f"C{number}" for number in range(1, 3)),
        *(f"D{number}" for number in range(1, 6)),
        *(f"P{number}" for number in range(1, 7)),
        *(f"T{number}" for number in range(1, 7)),
        *(f"H{number}" for number in range(1, 4)),
        *(f"A{number}" for number in range(1, 3)),
        *(f"I{number}" for number in range(1, 4)),
        *(f"S{number}" for number in range(1, 4)),
    }
    for artifact_id in expected_ids:
        assert f"| {artifact_id} |" in audit, f"audit matrix missing {artifact_id}"
    combined = "\n".join((audit, adr, evidence, benchmark))
    private_markers = (
        "".join(("/", "Users", "/")),
        "".join(("/", "private", "/")),
        "".join(("wen", "kxu")),
    )
    assert all(marker not in combined for marker in private_markers)
    for required in (
        "one embedded SQLite",
        "never execute SQL",
        "no permanent dual canonical",
        "3.51.3",
        "rollback journal",
        "foreign_keys",
        "busy timeout",
        "integrity_check",
        "foreign_key_check",
    ):
        assert required in combined, f"decision evidence missing {required}"
    production_source = (ROOT / "src/agent_watcher.py").read_text(encoding="utf-8")
    production_tree = ast.parse(production_source)
    production_imports = {
        alias.name.split(".")[0]
        for node in ast.walk(production_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(production_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "sqlite3" not in production_imports


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-storage-prototype-test-") as temporary:
        root = Path(temporary)
        paths = {name: root / name for name in ("runtime", "contract", "concurrency", "migrations")}
        for path in paths.values():
            path.mkdir()
        test_runtime_gate(paths["runtime"])
        test_transactions_export_and_backup(paths["contract"])
        test_two_writer_compare_and_swap(paths["concurrency"])
        test_migration_refusals(paths["migrations"])
    test_bounded_benchmark()
    test_public_decision_artifacts()
    print(
        "PASS: runtime WAL gate, migrations, atomic transition/owner transfer, "
        "delivery acknowledgement, two-writer CAS, integrity, backup/export, and bounded benchmark"
    )


if __name__ == "__main__":
    main()
