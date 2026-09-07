#!/usr/bin/env python3
"""Focused two-writer, busy-timeout, and transaction crash tests."""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from storage_fixture import CHAMPION_ID  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402


AT3 = "2026-01-01T00:02:00Z"
AT4 = "2026-01-01T00:03:00Z"
AT5 = "2026-01-01T00:04:00Z"


class InjectedCrash(RuntimeError):
    pass


def test_two_writer_expected_version(state: Path) -> None:
    first = SQLiteStorage(state, busy_timeout_ms=1000)
    second = SQLiteStorage(state, busy_timeout_ms=1000)
    barrier = threading.Barrier(2)
    guard = threading.Lock()
    outcomes: list[str] = []

    def write(store: SQLiteStorage, update: str) -> None:
        barrier.wait()
        try:
            store.transition(CHAMPION_ID, 2, "progress", update, AT3)
            outcome = "committed"
        except StorageRefusal as exc:
            assert exc.code == "version_conflict"
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
    assert first.agent_status(CHAMPION_ID)["version"] == 3
    first.close()
    second.close()


def test_hot_connection_validates_established_wal_without_mode_change(
    state: Path,
) -> None:
    supervisor = SQLiteStorage(state, busy_timeout_ms=1000)
    assert supervisor.policy.journal_mode == "WAL"
    hook = SQLiteStorage(state, busy_timeout_ms=50, request_wal=False)
    try:
        assert hook.policy.journal_mode == "WAL"
        assert hook.policy.wal_allowed is True
        assert supervisor.agent_status(CHAMPION_ID) is not None
        assert hook.agent_status(CHAMPION_ID) is not None
    finally:
        hook.close()
        supervisor.close()


def test_bounded_busy_and_crash_rollback(state: Path) -> None:
    holder = SQLiteStorage(state, busy_timeout_ms=1000)
    contender = SQLiteStorage(state, busy_timeout_ms=50)
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold(point: str) -> None:
        if point == "after_event_insert":
            entered.set()
            assert release.wait(timeout=5)

    def writer() -> None:
        try:
            holder.transition(CHAMPION_ID, 2, "progress", "holder", AT4, fault=hold)
        except BaseException as exc:  # captured for assertion in the parent thread
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        contender.transition(CHAMPION_ID, 2, "progress", "contender", AT4)
    except StorageRefusal as exc:
        assert exc.code == "busy" and exc.retryable
    else:
        raise AssertionError("bounded busy timeout did not refuse")
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    assert holder.agent_status(CHAMPION_ID)["version"] == 3

    def crash(point: str) -> None:
        if point == "after_event_insert":
            raise InjectedCrash(point)

    try:
        holder.transition(CHAMPION_ID, 3, "failed", "crash", AT5, fault=crash)
    except InjectedCrash:
        pass
    else:
        raise AssertionError("transition crash was not injected")
    assert holder.agent_status(CHAMPION_ID)["version"] == 3
    assert holder.agent_status(CHAMPION_ID)["status"] == "progress"
    assert holder.integrity()["ok"]
    holder.close()
    contender.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-storage-concurrency-") as temporary:
        root = Path(temporary)
        _, writer_state, _ = seeded_state(root, "two-writer")
        test_two_writer_expected_version(writer_state)
        _, mode_state, _ = seeded_state(root, "established-wal")
        test_hot_connection_validates_established_wal_without_mode_change(mode_state)
        _, crash_state, _ = seeded_state(root, "busy-crash")
        test_bounded_busy_and_crash_rollback(crash_state)
    print("PASS: two-writer CAS, bounded busy refusal, and crash rollback")


if __name__ == "__main__":
    main()
