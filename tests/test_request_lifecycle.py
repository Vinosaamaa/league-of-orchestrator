#!/usr/bin/env python3
"""P100 and durable request-inbox acceptance coverage."""

from __future__ import annotations

import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.request_services import (  # noqa: E402
    AssignmentService,
    AssignmentSpec,
    DeliveryService,
)
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import (  # noqa: E402
    AnswerRequestCommand,
    RequestResultCommand,
    StorageRefusal,
)
from lifecycle_fakes import FakeDeliveryAdapter, FakeIds, FakeLaunchAdapter  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    AHRI_ID,
    GAREN_RUNTIME,
    JARVAN_ID,
    JARVAN_RUNTIME,
    LUX_ID,
    SONA_ID,
    capture_p100,
    create_context,
)
from storage_fixture import REPOSITORY, SHOTCALLER_ID  # noqa: E402


def assign(
    store: SQLiteStorage,
    clock,
    ids: FakeIds,
    *,
    assignment_id: str,
    request_id: str,
    claim: str,
    task_id: str,
    coordinator: str,
    champion: str,
    callsign: str,
) -> dict:
    return AssignmentService(store, FakeLaunchAdapter(), clock, ids).assign(
        AssignmentSpec(
            assignment_id=assignment_id,
            request_id=request_id,
            claim_token=claim,
            task_id=task_id,
            task_summary=f"Synthetic {task_id}",
            coordinator_agent_id=coordinator,
            champion_agent_id=champion,
            callsign=callsign,
            repository=REPOSITORY,
            issue=17,
            branch=f"agent/synthetic/{task_id}",
            worktree=f"/synthetic/worktrees/{task_id}",
        )
    )


def complete_task(
    store: SQLiteStorage,
    clock,
    ids: FakeIds,
    assignment: dict,
    coordinator: str,
) -> dict:
    task_id = assignment["task_id"]
    return store.transition_task(
        task_id,
        assignment["runtime_instance_id"],
        3,
        "completed",
        f"{task_id} complete",
        "Coordinator synthesizes the result",
        None,
        ids.new("transition"),
        f"source:{task_id}:complete",
        ids.new("event"),
        ids.new("outbox"),
        coordinator,
        clock.now(),
    )


def deliver(store: SQLiteStorage, clock, ids: FakeIds, transition: dict, recipient: str) -> dict:
    adapter = FakeDeliveryAdapter()
    service = DeliveryService(
        store, adapter, clock, ids, dispatcher_id="dispatcher:p100"
    )
    result = service.dispatch_source(
        transition["outbox_id"], transition["event_id"], recipient
    )
    assert result["state"] == "delivered"
    assert adapter.sent[-1].envelope["event_id"] == transition["event_id"]
    return result


def test_p100(root: Path) -> None:
    state, store, clock = create_context(root, "p100")
    ids = FakeIds()
    captured = capture_p100(store, clock)
    assert captured["prompt"]["idempotent"] is False
    assert captured["triage"]["item_count"] == 3
    assert captured["triage"]["request_count"] == 3
    assert store.intake_prompt(
        "ignored-retry-id",
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        "session:p100",
        "source:p100",
        "Answer A; investigate B; implement C",
        clock.now(),
    )["idempotent"]

    store.claim_request("R1", GAREN_RUNTIME, "claim-r1", clock.after(120), clock.now())
    assert store.connection.execute(
        "SELECT event_type FROM events WHERE event_id='request:R1:claim:1'"
    ).fetchone()[0] == "request_claimed"
    dispatch_r1 = store.dispatch_request(
        "R1",
        "claim-r1",
        "dispatch-r1",
        "question",
        "direct",
        False,
        None,
        None,
        None,
        clock.now(),
    )
    assert dispatch_r1["execution_mode"] == "direct"
    answer_r1 = store.answer_request(
        AnswerRequestCommand(
            "R1",
            "claim-r1",
            2,
            "response-r1",
            "codex",
            "session:p100",
            "response:r1",
            "durable",
            "hash-r1",
            "Bounded answer delivered",
            "event-r1-answer",
            clock.now(),
        )
    )
    assert answer_r1["state"] == "answered"
    assert store.connection.execute(
        "SELECT runtime_instance_id FROM response_references WHERE response_ref_id='response-r1'"
    ).fetchone()[0] == GAREN_RUNTIME
    assert store.answer_request(
        AnswerRequestCommand(
            "R1",
            "claim-r1",
            2,
            "ignored-response-r1-retry",
            "codex",
            "session:p100",
            "ignored:retry",
            "durable",
            "hash-r1",
            "Bounded answer delivered",
            "ignored-event-r1-retry",
            clock.now(),
        )
    )["idempotent"]

    store.claim_request("R2", GAREN_RUNTIME, "claim-r2-garen", clock.after(120), clock.now())
    routed = store.route_request(
        "R2",
        "claim-r2-garen",
        1,
        JARVAN_ID,
        "event-r2-route",
        "outbox-r2-route",
        clock.now(),
    )
    assert store.route_request(
        "R2",
        "claim-r2-garen",
        1,
        JARVAN_ID,
        "event-r2-route",
        "outbox-r2-route",
        clock.now(),
    )["idempotent"]
    deliver(store, clock, ids, routed, JARVAN_ID)
    accepted = store.claim_request(
        "R2", JARVAN_RUNTIME, "claim-r2-jarvan", clock.after(300), clock.now()
    )
    assert accepted["accepted"] and accepted["state"] == "accepted"
    dispatch_r2 = store.dispatch_request(
        "R2",
        "claim-r2-jarvan",
        "dispatch-r2",
        "long-running",
        "champion",
        False,
        "synthetic-model",
        "high",
        "Jarvan",
        clock.now(),
    )
    assert dispatch_r2["request_version"] == 4
    first = assign(
        store,
        clock,
        ids,
        assignment_id="A201",
        request_id="R2",
        claim="claim-r2-jarvan",
        task_id="T201",
        coordinator=JARVAN_ID,
        champion=LUX_ID,
        callsign="Lux",
    )
    second = assign(
        store,
        clock,
        ids,
        assignment_id="A202",
        request_id="R2",
        claim="claim-r2-jarvan",
        task_id="T202",
        coordinator=JARVAN_ID,
        champion=AHRI_ID,
        callsign="Ahri",
    )
    for assignment in (first, second):
        transition = complete_task(store, clock, ids, assignment, JARVAN_ID)
        deliver(store, clock, ids, transition, JARVAN_ID)
    assert store.connection.execute(
        "SELECT state FROM requests WHERE request_id='R2'"
    ).fetchone()[0] == "in_progress"
    try:
        store.record_request_result(
            RequestResultCommand(
                "R2",
                "claim-r2-jarvan",
                4,
                "RES-ROLLBACK",
                "result-r2-rollback",
                "success",
                "This result must roll back with its colliding outbox",
                ("T201", "T202"),
                clock.now(),
                True,
                "event-r2-return-rollback",
                routed["outbox_id"],
            )
        )
    except StorageRefusal:
        pass
    else:
        raise AssertionError("routed result survived an outbox collision")
    unchanged = store.connection.execute(
        "SELECT owner_agent_id,state,latest_result_id,version FROM requests WHERE request_id='R2'"
    ).fetchone()
    assert tuple(unchanged) == (JARVAN_ID, "in_progress", None, 4)
    assert store.connection.execute(
        "SELECT COUNT(*) FROM request_results WHERE result_id='RES-ROLLBACK'"
    ).fetchone()[0] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_id='event-r2-return-rollback'"
    ).fetchone()[0] == 0
    returned = store.record_request_result(
        RequestResultCommand(
            "R2",
            "claim-r2-jarvan",
            4,
            "RES9",
            "result-r2",
            "success",
            "Combined dependency and failure findings",
            ("T201", "T202"),
            clock.now(),
            True,
            "event-r2-return",
            "outbox-r2-return",
        )
    )
    assert returned["owner_agent_id"] == SHOTCALLER_ID
    assert returned["state"] == "awaiting_requester"
    assert store.record_request_result(
        RequestResultCommand(
            "R2",
            "claim-r2-jarvan",
            4,
            "ignored-result-retry",
            "result-r2",
            "success",
            "Combined dependency and failure findings",
            ("T201", "T202"),
            clock.now(),
            True,
            "ignored-event-r2-return-retry",
            "ignored-outbox-r2-return-retry",
        )
    ) == {
        "request_id": "R2",
        "result_id": "RES9",
        "state": "awaiting_requester",
        "owner_agent_id": SHOTCALLER_ID,
        "version": 5,
        "event_id": "event-r2-return",
        "outbox_id": "outbox-r2-return",
        "idempotent": True,
    }
    deliver(store, clock, ids, returned, SHOTCALLER_ID)
    claimed_back = store.claim_request(
        "R2", GAREN_RUNTIME, "claim-r2-returned", clock.after(120), clock.now()
    )
    assert claimed_back["state"] == "awaiting_requester"
    answer_r2 = store.answer_request(
        AnswerRequestCommand(
            "R2",
            "claim-r2-returned",
            5,
            "response-r2",
            "codex",
            "session:p100",
            "response:r2",
            "durable",
            "hash-r2",
            "User-facing routed synthesis delivered",
            "event-r2-answer",
            clock.now(),
        )
    )
    assert answer_r2["state"] == "answered"

    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(300), clock.now())
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
    local = assign(
        store,
        clock,
        ids,
        assignment_id="A301",
        request_id="R3",
        claim="claim-r3",
        task_id="T301",
        coordinator=SHOTCALLER_ID,
        champion=SONA_ID,
        callsign="Sona",
    )
    local_transition = complete_task(store, clock, ids, local, SHOTCALLER_ID)
    deliver(store, clock, ids, local_transition, SHOTCALLER_ID)
    local_result = store.record_request_result(
        RequestResultCommand(
            "R3",
            "claim-r3",
            2,
            "RES-R3",
            "result-r3",
            "success",
            "Local Champion result synthesized",
            ("T301",),
            clock.now(),
            False,
            None,
            None,
        )
    )
    assert local_result["state"] == "in_progress"
    store.answer_request(
        AnswerRequestCommand(
            "R3",
            "claim-r3",
            3,
            "response-r3",
            "codex",
            "session:p100",
            "response:r3",
            "durable",
            "hash-r3",
            "Local Champion result delivered",
            "event-r3-answer",
            clock.now(),
        )
    )

    unresolved = store.unresolved_requests(SHOTCALLER_ID, before_action="reply")
    assert unresolved["safe_to_finish"] and unresolved["unresolved_count"] == 0
    assert store.connection.execute(
        "SELECT state FROM requests WHERE request_id='R2'"
    ).fetchone()[0] == "answered"
    assert store.connection.execute(
        "SELECT COUNT(*) FROM prompt_payloads WHERE prompt_id='P100'"
    ).fetchone()[0] == 1
    store.close()
    with SQLiteStorage(state) as reopened:
        assert reopened.unresolved_requests(SHOTCALLER_ID, before_action="handoff")[
            "safe_to_finish"
        ]


def test_restart_followup_new_prompt_and_cancellation(root: Path) -> None:
    state, store, clock = create_context(root, "restart")
    capture_p100(store, clock)
    store.claim_request("R1", GAREN_RUNTIME, "cancel-r1", clock.after(90), clock.now())
    cancelled = store.set_request_state(
        "R1",
        "cancel-r1",
        1,
        "cancelled",
        "User cancelled the bounded question",
        "event-r1-cancel",
        clock.now(),
    )
    assert cancelled["state"] == "cancelled"
    store.claim_request("R2", GAREN_RUNTIME, "claim-r2", clock.after(120), clock.now())
    blocked = store.set_request_state(
        "R2",
        "claim-r2",
        1,
        "blocked",
        "Synthetic dependency unavailable",
        "event-r2-blocked",
        clock.now(),
    )
    deferred = store.set_request_state(
        "R2",
        "claim-r2",
        blocked["version"],
        "deferred",
        "Resume after the synthetic dependency",
        "event-r2-deferred",
        clock.now(),
        next_attention_at=clock.after(3600),
    )
    assert deferred["state"] == "deferred"
    store.release_request_claim("R2", GAREN_RUNTIME, "claim-r2", clock.now())
    store.intake_prompt(
        "P101",
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        "session:p100",
        "source:p101",
        "Add context to the investigation",
        clock.now(),
    )
    store.triage_prompt(
        "P101",
        [
            {
                "prompt_item_id": "PI101-1",
                "ordinal": 1,
                "summary": "Additional context for R2",
                "disposition": "follow_up",
                "request_id": "R2",
            }
        ],
        clock.now(),
    )
    store.intake_prompt(
        "P102",
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        "session:p100",
        "source:p102",
        "Answer A; investigate B; implement C",
        clock.now(),
    )
    store.triage_prompt(
        "P102",
        [
            {
                "prompt_item_id": "PI102-1",
                "ordinal": 1,
                "summary": "Genuinely new repeated wording",
                "disposition": "new_request",
                "request_id": "R4",
            }
        ],
        clock.now(),
    )
    before = store.unresolved_requests(SHOTCALLER_ID, before_action="wait")
    assert {item["request_id"] for item in before["requests"]} == {"R2", "R3", "R4"}
    with store._transaction():
        store.connection.execute(
            "UPDATE prompt_payloads SET body=NULL,pruned_at=? WHERE prompt_id='P100'",
            (clock.now(),),
        )
    store.close()
    with SQLiteStorage(state) as restarted:
        after = restarted.unresolved_requests(SHOTCALLER_ID, before_action="end")
        assert after["unresolved_count"] == 3
        assert restarted.connection.execute(
            "SELECT COUNT(*) FROM request_sources WHERE request_id='R2'"
        ).fetchone()[0] == 2
        assert restarted.connection.execute(
            "SELECT body,body_hash,pruned_at FROM prompt_payloads WHERE prompt_id='P100'"
        ).fetchone()[0] is None


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-request-lifecycle-") as temporary:
        root = Path(temporary)
        test_p100(root)
        test_restart_followup_new_prompt_and_cancellation(root)
    print("PASS: P100 direct R1, routed R2, local Champion R3, prompt-once, compaction/restart, follow-up, and cancellation")


if __name__ == "__main__":
    main()
