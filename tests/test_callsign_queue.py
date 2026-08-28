#!/usr/bin/env python3
"""Persistent shuffled callsign queue, history, retry, and concurrency coverage."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_handoff_schema import CHAMPION_SEED, SHUFFLE_VERSION  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from storage_test_support import migrated_state  # noqa: E402


AT1 = "2026-01-01T00:00:00Z"
AT2 = "2026-01-01T00:01:00Z"
AT3 = "2026-01-01T00:02:00Z"
AT4 = "2026-01-01T00:03:00Z"
CATALOG = (
    {"callsign": "Annie", "enabled": True, "capabilities": ["backend.herdr"]},
    {"callsign": "Braum", "enabled": True, "capabilities": ["backend.tmux"]},
    {"callsign": "Caitlyn", "enabled": True, "capabilities": ["backend.herdr"]},
    {"callsign": "Darius", "enabled": True, "capabilities": []},
    {"callsign": "Ezreal", "enabled": True, "capabilities": ["backend.herdr"]},
)


class InjectedCrash(RuntimeError):
    pass


def initialize(store: SQLiteStorage) -> dict:
    return store.reconcile_callsign_pool(
        "champion", 1, CHAMPION_SEED, SHUFFLE_VERSION, CATALOG, AT1
    )


def receipt(assignment: dict, suffix: str, capabilities: list[str]) -> dict:
    return {
        "schema": "league.runtime-acceptance.v1",
        "verified": True,
        "assignment_id": assignment["assignment_id"],
        "agent_id": assignment["agent_id"],
        "callsign": assignment["callsign"],
        "runtime_instance_id": f"runtime:{suffix}",
        "harness_kind": "synthetic",
        "backend_kind": "herdr",
        "session_identity": f"synthetic:{suffix}",
        "endpoint_identity": f"synthetic-endpoint:{suffix}",
        "endpoint_generation": f"generation:{suffix}",
        "routing_name": assignment["callsign"].lower(),
        "display_agent": "synthetic",
        "capabilities": capabilities,
    }


def test_initialized_once_and_stable(root: Path) -> None:
    state, _ = migrated_state(root, "stable-order")
    with SQLiteStorage(state) as store:
        first = initialize(store)
        order = [
            item["callsign"]
            for item in first["entries"]
            if item["state"] == "available"
        ]
        assert order != sorted(order)
        assert first["seed"] == CHAMPION_SEED
        assert first["shuffle_version"] == SHUFFLE_VERSION
        queue_version = first["queue_version"]
    with SQLiteStorage(state) as reopened:
        second = reopened.callsign_status("champion")
        assert [item["callsign"] for item in second["entries"]] == order
        assert second["queue_version"] == queue_version
        retry = reopened.reconcile_callsign_pool(
            "champion", queue_version, CHAMPION_SEED, SHUFFLE_VERSION, CATALOG, AT1
        )
        assert retry["idempotent"] and retry["queue_version"] == queue_version


def test_skip_rollback_release_rotation_and_history(root: Path) -> None:
    state, _ = migrated_state(root, "rotation")
    with SQLiteStorage(state) as store:
        initialized = initialize(store)
        before = {
            row["callsign"]: row["position"] for row in initialized["entries"]
        }
        compatible_order = [
            row["callsign"]
            for row in initialized["entries"]
            if row["callsign"] in {"Annie", "Caitlyn", "Ezreal"}
        ]
        first = store.allocate_callsign(
            "callsign-assignment:first",
            "agent:first",
            "champion",
            "task",
            "task:first",
            ["backend.herdr"],
            AT2,
        )
        assert first["callsign"] == compatible_order[0]
        after_reserve = store.callsign_status("champion")
        assert {
            row["callsign"]: row["position"] for row in after_reserve["entries"]
        } == before
        rolled_back = store.rollback_callsign(
            first["assignment_id"], 1, "failure-receipt:first", AT3
        )
        assert rolled_back["state"] == "rolled_back"
        retry = store.allocate_callsign(
            "callsign-assignment:retry",
            "agent:retry",
            "champion",
            "task",
            "task:retry",
            ["backend.herdr"],
            AT3,
        )
        assert retry["callsign"] == first["callsign"]
        active = store.activate_callsign(
            retry["assignment_id"], 1, receipt(retry, "retry", ["backend.herdr"]), AT3
        )
        assert active["state"] == "active"
        store.connection.execute(
            "UPDATE runtime_instances SET status='closed' WHERE runtime_instance_id='runtime:retry'"
        )
        released = store.release_callsign(
            retry["assignment_id"], 2, "release-receipt:retry", AT4
        )
        assert released["state"] == "released"
        available = [
            row for row in store.callsign_status("champion")["entries"]
            if row["state"] == "available"
        ]
        assert available[-1]["callsign"] == retry["callsign"]
        history = store.connection.execute(
            """
            SELECT callsign_assignment_id,state,subject_id FROM callsign_assignments
             WHERE callsign=? ORDER BY reserved_at,callsign_assignment_id
            """,
            (retry["callsign"],),
        ).fetchall()
        assert [(row["state"], row["subject_id"]) for row in history] == [
            ("rolled_back", "agent:agent:first"),
            ("released", "agent:agent:retry"),
        ]


def test_recent_sole_compatible_remains_eligible(root: Path) -> None:
    state, _ = migrated_state(root, "sole-compatible")
    catalog = tuple(
        {
            **entry,
            "capabilities": ["only.safe"] if entry["callsign"] == "Caitlyn" else [],
        }
        for entry in CATALOG
    )
    with SQLiteStorage(state) as store:
        store.reconcile_callsign_pool(
            "champion", 1, CHAMPION_SEED, SHUFFLE_VERSION, catalog, AT1
        )
        first = store.allocate_callsign(
            "callsign-assignment:sole-1",
            "agent:sole-1",
            "champion",
            "task",
            "task:sole-1",
            ["only.safe"],
            AT2,
        )
        assert first["callsign"] == "Caitlyn"
        store.activate_callsign(
            first["assignment_id"], 1, receipt(first, "sole-1", ["only.safe"]), AT2
        )
        store.connection.execute(
            "UPDATE runtime_instances SET status='closed' WHERE runtime_instance_id='runtime:sole-1'"
        )
        store.release_callsign(first["assignment_id"], 2, "release:sole-1", AT3)
        second = store.allocate_callsign(
            "callsign-assignment:sole-2",
            "agent:sole-2",
            "champion",
            "task",
            "task:sole-2",
            ["only.safe"],
            AT4,
        )
        assert second["callsign"] == "Caitlyn"


def test_concurrent_and_crash_safe_allocation(root: Path) -> None:
    state, _ = migrated_state(root, "concurrent")
    with SQLiteStorage(state) as seed:
        initialize(seed)
        before = seed.callsign_status("champion")

        def crash(point: str) -> None:
            if point == "after_callsign_queue_reservation":
                raise InjectedCrash(point)

        try:
            seed.allocate_callsign(
                "callsign-assignment:crash",
                "agent:crash",
                "champion",
                "task",
                "task:crash",
                [],
                AT2,
                fault=crash,
            )
        except InjectedCrash:
            pass
        else:
            raise AssertionError("allocation crash was not injected")
        assert seed.callsign_status("champion") == before
        assert seed.connection.execute(
            "SELECT COUNT(*) FROM callsign_assignments WHERE callsign_assignment_id='callsign-assignment:crash'"
        ).fetchone()[0] == 0

    left = SQLiteStorage(state, busy_timeout_ms=1000)
    right = SQLiteStorage(state, busy_timeout_ms=1000)
    barrier = threading.Barrier(2)
    outcomes: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def allocate(store: SQLiteStorage, suffix: str) -> None:
        barrier.wait()
        try:
            value = store.allocate_callsign(
                f"callsign-assignment:{suffix}",
                f"agent:{suffix}",
                "champion",
                "task",
                f"task:{suffix}",
                [],
                AT2,
            )
            with lock:
                outcomes.append(value)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=allocate, args=(left, "left")),
        threading.Thread(target=allocate, args=(right, "right")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors and len(outcomes) == 2
    assert len({value["callsign"] for value in outcomes}) == 2
    status = left.callsign_status("champion")
    assert status["counts"]["reserved"] == 2
    assert len(status["entries"]) == len(CATALOG)
    left.close()
    right.close()


def test_concurrent_identical_retry_is_one_reservation(root: Path) -> None:
    state, _ = migrated_state(root, "concurrent-retry")
    with SQLiteStorage(state) as seed:
        initialize(seed)
    left = SQLiteStorage(state, busy_timeout_ms=1000)
    right = SQLiteStorage(state, busy_timeout_ms=1000)
    barrier = threading.Barrier(2)
    outcomes: list[dict] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def retry(store: SQLiteStorage) -> None:
        barrier.wait()
        try:
            value = store.allocate_callsign(
                "callsign-assignment:same",
                "agent:same",
                "champion",
                "task",
                "task:same",
                ["backend.herdr"],
                AT2,
            )
            with guard:
                outcomes.append(value)
        except BaseException as exc:
            with guard:
                errors.append(exc)

    threads = [
        threading.Thread(target=retry, args=(left,)),
        threading.Thread(target=retry, args=(right,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 2
    assert len({value["callsign"] for value in outcomes}) == 1
    assert sorted(value["idempotent"] for value in outcomes) == [False, True]
    assert left.connection.execute(
        "SELECT COUNT(*) FROM callsign_assignments WHERE callsign_assignment_id='callsign-assignment:same'"
    ).fetchone()[0] == 1
    assert left.callsign_status("champion")["counts"]["reserved"] == 1
    left.close()
    right.close()


def test_refusal_reasons_and_deterministic_catalog_changes(root: Path) -> None:
    state, _ = migrated_state(root, "refusals")
    other, _ = migrated_state(root, "catalog-copy")
    with SQLiteStorage(state) as store, SQLiteStorage(other) as copy:
        first = initialize(store)
        second = initialize(copy)
        assert [row["callsign"] for row in first["entries"]] == [
            row["callsign"] for row in second["entries"]
        ]
        changed = tuple(
            {**entry, "enabled": entry["callsign"] != "Darius"} for entry in CATALOG
        ) + (
            {"callsign": "Fiora", "enabled": True, "capabilities": ["backend.tmux"]},
            {"callsign": "Galio", "enabled": True, "capabilities": ["backend.herdr"]},
        )
        changed_first = store.reconcile_callsign_pool(
            "champion",
            first["queue_version"],
            CHAMPION_SEED,
            SHUFFLE_VERSION,
            changed,
            AT2,
        )
        changed_second = copy.reconcile_callsign_pool(
            "champion",
            second["queue_version"],
            CHAMPION_SEED,
            SHUFFLE_VERSION,
            tuple(reversed(changed)),
            AT2,
        )
        assert [row["callsign"] for row in changed_first["entries"]] == [
            row["callsign"] for row in changed_second["entries"]
        ]
        try:
            store.reconcile_callsign_pool(
                "champion",
                changed_first["queue_version"],
                CHAMPION_SEED,
                SHUFFLE_VERSION,
                changed[:-1],
                AT3,
            )
        except StorageRefusal as exc:
            assert exc.code == "callsign_history_immutable"
        else:
            raise AssertionError("persisted callsign was removed instead of disabled")
        try:
            store.allocate_callsign(
                "callsign-assignment:none",
                "agent:none",
                "champion",
                "task",
                "task:none",
                ["capability.absent"],
                AT3,
            )
        except StorageRefusal as exc:
            assert exc.code == "callsign_unavailable"
            counts = json.loads(str(exc).split(": ", 1)[1])
            assert counts == {
                "active": 0,
                "incompatible": len(changed),
                "reasons": {
                    "disabled": 1,
                    "missing:capability.absent": len(changed) - 1,
                },
                "reserved": 0,
            }
        else:
            raise AssertionError("empty compatible availability was accepted")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-callsign-queue-") as temporary:
        root = Path(temporary)
        test_initialized_once_and_stable(root)
        test_skip_rollback_release_rotation_and_history(root)
        test_recent_sole_compatible_remains_eligible(root)
        test_concurrent_and_crash_safe_allocation(root)
        test_concurrent_identical_retry_is_one_reservation(root)
        test_refusal_reasons_and_deterministic_catalog_changes(root)
    print(
        "PASS: persisted shuffled callsign queue, compatibility, rollback, tail release, history, and concurrency"
    )


if __name__ == "__main__":
    main()
