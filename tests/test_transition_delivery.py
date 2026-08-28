#!/usr/bin/env python3
"""Source-event binding, delivery deduplication, fairness, close, and reconnect tests."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.request_services import (  # noqa: E402
    AssignmentService,
    AssignmentSpec,
    DeliveryService,
    DeliveryUnavailable,
)
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from lifecycle_fakes import FakeDeliveryAdapter, FakeIds, FakeLaunchAdapter  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    JARVAN_ID,
    JARVAN_RUNTIME,
    LUX_ID,
    capture_p100,
    create_context,
)
from storage_fixture import CHAMPION_ID, REPOSITORY, SHOTCALLER_ID  # noqa: E402


def route_r2(store, clock):
    store.claim_request("R2", GAREN_RUNTIME, "route-r2", clock.after(120), clock.now())
    return store.route_request(
        "R2",
        "route-r2",
        1,
        JARVAN_ID,
        "event:heimerdinger:source",
        "outbox:heimerdinger:source",
        clock.now(),
    )


def add_older_pending(store, clock) -> None:
    with store._transaction():
        store.connection.execute(
            """
            INSERT INTO events
              (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,
               detail_json,request_id,aggregate_kind,aggregate_id)
            VALUES('event:aatrox:older',?,NULL,99,'agent_transition','completed',
                   'Older unrelated completion',?,'{}',NULL,'agent',?)
            """,
            (CHAMPION_ID, clock.now(), CHAMPION_ID),
        )
        store.connection.execute(
            """
            INSERT INTO delivery_outbox
              (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
            VALUES('outbox:aatrox:older','event:aatrox:older',?,'pending',?,0)
            """,
            (SHOTCALLER_ID, clock.now()),
        )


def test_heimerdinger_source_binding_and_fair_drain(root: Path) -> None:
    _, store, clock = create_context(root, "heimerdinger")
    capture_p100(store, clock)
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    store.dispatch_request(
        "R3",
        "claim-r3",
        "dispatch-r3",
        "repository-write",
        "champion",
        False,
        None,
        None,
        None,
        clock.now(),
    )
    ids = FakeIds()
    active = AssignmentService(store, FakeLaunchAdapter(), clock, ids).assign(
        AssignmentSpec(
            assignment_id="assignment:heimerdinger",
            request_id="R3",
            claim_token="claim-r3",
            task_id="task:heimerdinger",
            task_summary="Synthetic Heimerdinger source transition",
            coordinator_agent_id=SHOTCALLER_ID,
            champion_agent_id=LUX_ID,
            callsign="Lux",
            repository=REPOSITORY,
            issue=3,
            branch="agent/synthetic/heimerdinger",
            worktree="/synthetic/worktrees/heimerdinger",
        )
    )
    DeliveryService(
        store,
        FakeDeliveryAdapter(),
        clock,
        ids,
        dispatcher_id="dispatcher:assignment",
    ).dispatch_source(active["outbox_id"], active["event_id"], LUX_ID)
    add_older_pending(store, clock)
    source = store.transition_task(
        active["task_id"],
        active["runtime_instance_id"],
        3,
        "completed",
        "Heimerdinger completion",
        "Shotcaller synthesizes the exact result",
        None,
        "transition:heimerdinger",
        "transition-key:heimerdinger",
        "event:heimerdinger:source",
        "outbox:heimerdinger:source",
        SHOTCALLER_ID,
        clock.now(),
    )
    adapter = FakeDeliveryAdapter()
    service = DeliveryService(store, adapter, clock, ids, dispatcher_id="dispatcher:one")
    outcome = service.dispatch_source_then_drain(
        source["outbox_id"], source["event_id"], SHOTCALLER_ID
    )
    assert outcome["source"]["event_id"] == "event:heimerdinger:source"
    assert [item.envelope["event_id"] for item in adapter.sent] == [
        "event:heimerdinger:source",
        "event:aatrox:older",
    ]
    assert adapter.sent[0].envelope["source_agent_id"] == LUX_ID
    assert adapter.sent[0].envelope["source_runtime_instance_id"] == active["runtime_instance_id"]
    assert adapter.sent[0].envelope["source_runtime_generation"] == f"generation:{LUX_ID}"
    assert store.connection.execute(
        "SELECT state FROM delivery_outbox WHERE outbox_id='outbox:heimerdinger:source'"
    ).fetchone()[0] == "delivered"
    assert store.connection.execute(
        "SELECT state FROM delivery_outbox WHERE outbox_id='outbox:aatrox:older'"
    ).fetchone()[0] == "delivered"
    receipt = store.connection.execute(
        "SELECT event_id,recipient_agent_id,effect_id FROM recipient_receipts WHERE event_id=?",
        (source["event_id"],),
    ).fetchone()
    assert receipt["event_id"] == source["event_id"] and receipt["recipient_agent_id"] == SHOTCALLER_ID
    duplicate = store.acknowledge_outbox(
        source["outbox_id"],
        source["event_id"],
        SHOTCALLER_ID,
        "dispatcher:one",
        1,
        "attempt-fake-1",
        "direct",
        "inbox_event",
        receipt["effect_id"],
        clock.now(),
    )
    assert duplicate["idempotent"]
    assert store.connection.execute(
        "SELECT COUNT(*) FROM recipient_receipts WHERE event_id=? AND recipient_agent_id=?",
        (source["event_id"], SHOTCALLER_ID),
    ).fetchone()[0] == 1
    store.close()


def test_mismatched_receipt_stays_pending(root: Path) -> None:
    _, store, clock = create_context(root, "mismatch")
    capture_p100(store, clock)
    source = route_r2(store, clock)
    service = DeliveryService(
        store,
        FakeDeliveryAdapter(mismatch_event_id="event:wrong"),
        clock,
        FakeIds(),
        dispatcher_id="dispatcher:mismatch",
    )
    try:
        service.dispatch_source(source["outbox_id"], source["event_id"], JARVAN_ID)
    except StorageRefusal as exc:
        assert exc.code == "receipt_mismatch"
    else:
        raise AssertionError("cross-wired recipient receipt was accepted")
    assert store.connection.execute(
        "SELECT state FROM delivery_outbox WHERE outbox_id=?", (source["outbox_id"],)
    ).fetchone()[0] == "pending"
    assert store.connection.execute(
        "SELECT COUNT(*) FROM recipient_receipts WHERE event_id=?", (source["event_id"],)
    ).fetchone()[0] == 0
    store.close()


def test_closed_endpoint_durability_reconnect_and_watcher_ownership(root: Path) -> None:
    _, store, clock = create_context(root, "closed-reconnect")
    capture_p100(store, clock)
    source = route_r2(store, clock)
    store.register_runtime(
        JARVAN_RUNTIME,
        JARVAN_ID,
        "codex-thread",
        "herdr",
        f"session:{JARVAN_RUNTIME}",
        "synthetic:jarvan",
        "generation:jarvan",
        "closed",
        True,
        clock.now(),
    )
    ids = FakeIds()
    service = DeliveryService(
        store, FakeDeliveryAdapter(), clock, ids, dispatcher_id="dispatcher:closed"
    )
    try:
        service.dispatch_source(source["outbox_id"], source["event_id"], JARVAN_ID)
    except DeliveryUnavailable:
        pass
    else:
        raise AssertionError("closed recipient unexpectedly accepted delivery")
    assert store.connection.execute(
        "SELECT state FROM delivery_outbox WHERE outbox_id=?", (source["outbox_id"],)
    ).fetchone()[0] == "pending"
    clock.advance(31)
    store.register_runtime(
        JARVAN_RUNTIME,
        JARVAN_ID,
        "codex-thread",
        "herdr",
        f"session:{JARVAN_RUNTIME}",
        "synthetic:jarvan",
        "generation:jarvan",
        "idle",
        True,
        clock.now(),
    )
    watcher_expiry = clock.after(120)
    store.register_watcher(
        "Jarvan",
        "watcher:jarvan",
        JARVAN_ID,
        JARVAN_RUNTIME,
        "wake:jarvan",
        watcher_expiry,
        1,
        clock.now(),
    )
    adapter = FakeDeliveryAdapter()
    resumed = DeliveryService(
        store, adapter, clock, ids, dispatcher_id="dispatcher:reconnected"
    ).dispatch_source(source["outbox_id"], source["event_id"], JARVAN_ID)
    assert resumed["state"] == "delivered"
    assert adapter.sent[0].channel == "watcher"
    clock.advance(121)
    direct_retry_adapter = FakeDeliveryAdapter()
    duplicate = DeliveryService(
        store,
        direct_retry_adapter,
        clock,
        ids,
        dispatcher_id="dispatcher:direct-retry",
    ).dispatch_source(source["outbox_id"], source["event_id"], JARVAN_ID)
    assert duplicate["idempotent"] and direct_retry_adapter.sent == []
    store.close()


def test_direct_fallback_only_without_active_watcher(root: Path) -> None:
    _, store, clock = create_context(root, "direct-fallback")
    capture_p100(store, clock)
    source = route_r2(store, clock)
    adapter = FakeDeliveryAdapter()
    DeliveryService(
        store, adapter, clock, FakeIds(), dispatcher_id="dispatcher:direct"
    ).dispatch_source(source["outbox_id"], source["event_id"], JARVAN_ID)
    assert adapter.sent[0].channel == "direct"
    store.close()


def test_concurrent_source_transitions_commit_once(root: Path) -> None:
    state, setup, clock = create_context(root, "concurrent-transition")
    capture_p100(setup, clock)
    setup.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    setup.dispatch_request(
        "R3",
        "claim-r3",
        "dispatch-r3",
        "repository-write",
        "champion",
        False,
        None,
        None,
        None,
        clock.now(),
    )
    active = AssignmentService(setup, FakeLaunchAdapter(), clock, FakeIds()).assign(
        AssignmentSpec(
            assignment_id="assignment:concurrent",
            request_id="R3",
            claim_token="claim-r3",
            task_id="task:concurrent",
            task_summary="Synthetic concurrent transition",
            coordinator_agent_id=SHOTCALLER_ID,
            champion_agent_id=LUX_ID,
            callsign="Lux",
            repository=REPOSITORY,
            issue=3,
            branch="agent/synthetic/concurrent-transition",
            worktree="/synthetic/worktrees/concurrent-transition",
        )
    )
    setup.close()
    first = SQLiteStorage(state, busy_timeout_ms=1000)
    second = SQLiteStorage(state, busy_timeout_ms=1000)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def transition(store, suffix):
        barrier.wait()
        try:
            store.transition_task(
                active["task_id"],
                active["runtime_instance_id"],
                3,
                "completed",
                f"Concurrent completion {suffix}",
                "Coordinator synthesizes the result",
                None,
                f"transition:concurrent:{suffix}",
                f"transition-key:concurrent:{suffix}",
                f"event:concurrent:{suffix}",
                f"outbox:concurrent:{suffix}",
                SHOTCALLER_ID,
                clock.now(),
            )
        except StorageRefusal as exc:
            result = exc.code
        else:
            result = "ok"
        with lock:
            outcomes.append(result)

    threads = (
        threading.Thread(target=transition, args=(first, "one")),
        threading.Thread(target=transition, args=(second, "two")),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["ok", "version_conflict"]
    assert first.connection.execute(
        "SELECT COUNT(*) FROM task_transitions WHERE task_id='task:concurrent'"
    ).fetchone()[0] == 1
    assert first.connection.execute(
        "SELECT COUNT(*) FROM events WHERE aggregate_kind='task' AND aggregate_id='task:concurrent'"
    ).fetchone()[0] == 1
    assert first.connection.execute(
        """
        SELECT COUNT(*) FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
         WHERE e.aggregate_kind='task' AND e.aggregate_id='task:concurrent'
        """
    ).fetchone()[0] == 1
    try:
        first.transition_task(
            active["task_id"],
            active["runtime_instance_id"],
            4,
            "malformed",
            "Malformed transition",
            "No action",
            None,
            "transition:malformed",
            "transition-key:malformed",
            "event:malformed",
            "outbox:malformed",
            SHOTCALLER_ID,
            clock.now(),
        )
    except StorageRefusal as exc:
        assert exc.code == "invalid_task_transition"
    else:
        raise AssertionError("malformed task transition was accepted")
    first.close()
    second.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-transition-delivery-") as temporary:
        root = Path(temporary)
        test_heimerdinger_source_binding_and_fair_drain(root)
        test_mismatched_receipt_stays_pending(root)
        test_closed_endpoint_durability_reconnect_and_watcher_ownership(root)
        test_direct_fallback_only_without_active_watcher(root)
        test_concurrent_source_transitions_commit_once(root)
    print("PASS: Heimerdinger source binding, fair drain, duplicate effect suppression, concurrent transitions, close/reconnect, and watcher/direct ownership")


if __name__ == "__main__":
    main()
