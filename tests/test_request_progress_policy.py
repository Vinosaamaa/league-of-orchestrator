#!/usr/bin/env python3
"""Exact immediate, coalesced, deduplicated, and overdue parent progress."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.storage_request import DispatchRequestCommand, RequestProgressCommand  # noqa: E402
from request_lifecycle_fixture import GAREN_RUNTIME, capture_p100, create_context  # noqa: E402
from storage_fixture import SHOTCALLER_ID  # noqa: E402


def _request(root: Path, name: str):
    _, store, clock = create_context(root, name)
    capture_p100(store, clock)
    store.claim_request("R1", GAREN_RUNTIME, "claim-progress", clock.after(7200), clock.now())
    dispatch = store.dispatch_request(
        DispatchRequestCommand(
            request_id="R1",
            claim_token="claim-progress",
            dispatch_id=f"dispatch:{name}",
            work_kind="question",
            requested_mode="direct",
            hidden_supported=False,
            requested_model=None,
            requested_effort=None,
            explicit_route=None,
            at=clock.now(),
            pre_bounded=True,
            read_only=True,
            answer_or_routing_only=True,
            expected_minutes=2,
            expected_task_action_calls=1,
        )
    )
    return store, clock, dispatch["request_version"]


def _progress(store, clock, version: int, generation: int, reason: str, **changes):
    value = {
        "settled_count": 0,
        "total_count": 3,
        "current_phase": "Implementation",
        "blocker_count": 0,
        "blocker_severity": "none",
        "user_action_required": False,
        "deadline_change": None,
        "next_action": "Continue the bounded routing slice",
    }
    value.update(changes)
    return store.emit_request_progress(
        RequestProgressCommand(
            progress_id=f"progress:{generation}",
            request_id="R1",
            claim_token="claim-progress",
            expected_version=version,
            progress_generation=generation,
            reason_code=reason,
            event_id=f"event:progress:{generation}",
            outbox_id=f"outbox:progress:{generation}",
            at=clock.now(),
            **value,
        )
    )


def test_immediate_and_coalesced_classes(root: Path) -> None:
    store, clock, version = _request(root, "progress-classes")
    try:
        accepted = _progress(store, clock, version, 1, "route_accepted")
        assert accepted["emitted"] and accepted["immediate"]
        clock.advance(60)
        first = _progress(
            store,
            clock,
            version,
            2,
            "milestone",
            current_phase="Tests",
            next_action="Run focused test one",
        )
        clock.advance(120)
        second = _progress(
            store,
            clock,
            version,
            3,
            "milestone",
            current_phase="Tests",
            next_action="Run focused test two",
        )
        clock.advance(60)
        third = _progress(
            store,
            clock,
            version,
            4,
            "partial_completion",
            settled_count=1,
            current_phase="Tests",
            next_action="Two children continue",
        )
        assert all(item["buffered"] and not item["emitted"] for item in (first, second, third))
        assert store.connection.execute(
            "SELECT COUNT(*) FROM request_progress_events WHERE urgency='routine'"
        ).fetchone()[0] == 0
        clock.advance(660)
        summary = _progress(
            store,
            clock,
            version,
            5,
            "partial_completion",
            settled_count=1,
            current_phase="Tests",
            next_action="Two children continue",
        )
        assert summary["emitted"] and not summary["immediate"]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM request_progress_events WHERE urgency='routine'"
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_local_blocker_batches_parent_decision_is_immediate(root: Path) -> None:
    store, clock, version = _request(root, "progress-blockers")
    try:
        local = _progress(
            store,
            clock,
            version,
            1,
            "recoverable_child_blocker",
            blocker_count=1,
            blocker_severity="low",
            next_action="Use the local fallback and continue",
        )
        assert local["buffered"] and not local["emitted"]
        urgent = _progress(
            store,
            clock,
            version,
            2,
            "parent_critical_blocker",
            blocker_count=1,
            blocker_severity="high",
            user_action_required=True,
            next_action="Requester chooses the required authority boundary",
        )
        assert urgent["emitted"] and urgent["immediate"]
        buffer = store.connection.execute(
            "SELECT state FROM request_progress_buffers WHERE request_id='R1'"
        ).fetchone()
        assert buffer["state"] == "superseded"
    finally:
        store.close()


def test_final_supersedes_and_unchanged_never_heartbeats(root: Path) -> None:
    store, clock, version = _request(root, "progress-final")
    try:
        _progress(store, clock, version, 1, "milestone")
        final = _progress(
            store,
            clock,
            version,
            2,
            "request_resolved",
            settled_count=3,
            current_phase="Resolved",
            next_action="Review the final result",
        )
        assert final["immediate"]
        unchanged = _progress(
            store,
            clock,
            version,
            3,
            "milestone",
            settled_count=3,
            current_phase="Resolved",
            next_action="Review the final result",
        )
        assert unchanged["suppressed"] == "unchanged"
        before = store.connection.execute(
            "SELECT COUNT(*) FROM request_progress_events"
        ).fetchone()[0]
        clock.advance(3600)
        reconciled = store.reconcile_request_progress(SHOTCALLER_ID, clock.now())
        after = store.connection.execute(
            "SELECT COUNT(*) FROM request_progress_events"
        ).fetchone()[0]
        assert reconciled["examined"] == 0 and before == after
    finally:
        store.close()


def test_due_grace_escalates_exactly_once(root: Path) -> None:
    store, clock, version = _request(root, "progress-overdue")
    try:
        _progress(store, clock, version, 1, "milestone")
        clock.advance(901)
        due = store.reconcile_request_progress(SHOTCALLER_ID, clock.now())
        assert due["created"] == 1 and due["escalated"] == 0
        clock.advance(300)
        overdue = store.reconcile_request_progress(SHOTCALLER_ID, clock.now())
        assert overdue["escalated"] == 1
        again = store.reconcile_request_progress(SHOTCALLER_ID, clock.now())
        assert again["escalated"] == 0 and again["examined"] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM request_progress_events WHERE urgency='overdue'"
        ).fetchone()[0] == 1
    finally:
        store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-request-progress-") as temporary:
        root = Path(temporary)
        test_immediate_and_coalesced_classes(root)
        test_local_blocker_batches_parent_decision_is_immediate(root)
        test_final_supersedes_and_unchanged_never_heartbeats(root)
        test_due_grace_escalates_exactly_once(root)
    print("PASS: immediate parent events, 15-minute coalescing, no heartbeats, and one overdue escalation")


if __name__ == "__main__":
    main()
