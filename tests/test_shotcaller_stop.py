#!/usr/bin/env python3
"""Role-aware bounded Stop-hook continuity and obligation reconciliation tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    LUX_ID,
    capture_p100,
    create_context,
    dispatch_request,
)
from league.storage import OutboxDispatchIdentity, PrepareAssignmentCommand  # noqa: E402
from league.request_services import AssignmentSpec  # noqa: E402
from lifecycle_fakes import issue_bound_spec  # noqa: E402
from storage_fixture import CHAMPION_ID, REPOSITORY, SHOTCALLER_ID  # noqa: E402


def register_watcher(store, clock, scope="Garen-lifecycle", *, block=True, fence=1):
    return store.register_watcher(
        scope,
        f"watcher:{scope}",
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        f"wake:{scope}",
        clock.after(300),
        fence,
        clock.now(),
        block_on_obligations=block,
    )


def add_combined_obligations(store, clock) -> None:
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(300), clock.now())
    dispatch_request(
        store,
        clock,
        "R3",
        "claim-r3",
        "dispatch-r3",
        "repository-write",
        "champion",
    )
    bound = issue_bound_spec(
        store,
        AssignmentSpec(
            assignment_id="assignment:pending",
            request_id="R3",
            claim_token="claim-r3",
            task_id="task:pending",
            task_summary="Pending synthetic Champion",
            coordinator_agent_id=SHOTCALLER_ID,
            champion_agent_id=LUX_ID,
            repository=REPOSITORY,
            issue=17,
            branch="agent/synthetic/pending",
            worktree="/synthetic/worktrees/pending",
            issue_receipt=None,
        ),
        clock.now(),
    )
    store.prepare_assignment(
        PrepareAssignmentCommand(
            **{key: value for key, value in vars(bound).items() if key != "callsign"},
            at=clock.now(),
        )
    )
    with store._transaction():
        store.connection.execute(
            """
            INSERT INTO cleanup_obligations
              (cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,version,updated_at)
            VALUES('cleanup:pending','task:pending','pending','synthetic','Verify synthetic resource',1,?)
            """,
            (clock.now(),),
        )
        store.connection.execute(
            """
            INSERT INTO events
              (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,
               detail_json,request_id,aggregate_kind,aggregate_id)
            VALUES('event:pending:garen',?,NULL,100,'agent_transition','completed',
                   'Synthetic pending delivery',?,'{}',NULL,'agent',?)
            """,
            (CHAMPION_ID, clock.now(), CHAMPION_ID),
        )
        store.connection.execute(
            """
            INSERT INTO delivery_outbox
              (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
            VALUES('outbox:pending:garen','event:pending:garen',?,'pending',?,0)
            """,
            (SHOTCALLER_ID, clock.now()),
        )


def test_combined_obligations_one_block_per_generation(root: Path) -> None:
    _, store, clock = create_context(root, "combined-stop")
    capture_p100(store, clock)
    add_combined_obligations(store, clock)
    register_watcher(store, clock)
    first = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal-generation-1", clock.now()
    )
    assert first["decision"] == "block" and first["status"] == "blocked_once"
    assert all(first["obligations"][name] > 0 for name in (
        "active_champions",
        "pending_assignments",
        "unresolved_requests",
        "pending_deliveries",
        "cleanup_obligations",
    ))
    repeated = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal-generation-1", clock.now()
    )
    assert repeated["decision"] == "allow" and repeated["terminal_fresh"] is False
    third = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal-generation-2", clock.now()
    )
    assert third["decision"] == "allow" and third["terminal_fresh"] is True
    rearmed = store.rearm_wait(
        "Garen-lifecycle", SHOTCALLER_ID, "event:fresh-wait", clock.now()
    )
    assert rearmed["wait_generation"] > first["wait_generation"]
    fresh = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal-generation-3", clock.now()
    )
    assert fresh["decision"] == "block" and fresh["wait_generation"] == rearmed["wait_generation"]
    store.close()


def test_user_priority_explicit_allow_and_configuration(root: Path) -> None:
    _, store, clock = create_context(root, "priority-stop")
    capture_p100(store, clock)
    register_watcher(store, clock)
    user = store.note_user_message(
        "Garen-lifecycle", SHOTCALLER_ID, clock.now()
    )
    priority = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal:user", clock.now()
    )
    assert priority["decision"] == "block" and priority["status"] == "blocked_once"
    next_stop = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal:user", clock.now()
    )
    assert next_stop["decision"] == "allow"
    store.rearm_wait("Garen-lifecycle", SHOTCALLER_ID, "event:explicit", clock.now())
    store.set_allow_stop_once("Garen-lifecycle", SHOTCALLER_ID)
    allowed = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal:explicit", clock.now()
    )
    assert allowed["decision"] == "allow" and allowed["priority"] == "explicit_allow_stop_once"
    register_watcher(store, clock, "Garen-no-block", block=False, fence=2)
    unconfigured = store.stop_decision(
        "Garen-no-block", SHOTCALLER_ID, "terminal:no-block", clock.now()
    )
    assert unconfigured["decision"] == "allow" and unconfigured["status"] == "unavailable"
    assert user["user_message_generation"] == 1
    store.close()


def test_role_awareness_reconciliation_and_distinct_leases(root: Path) -> None:
    _, store, clock = create_context(root, "role-stop")
    capture_p100(store, clock)
    for action in ("reply", "wait", "handoff", "end"):
        result = store.unresolved_requests(SHOTCALLER_ID, before_action=action)
        assert not result["safe_to_finish"] and result["unresolved_count"] == 3
    champion = store.stop_decision(
        "Thresh", CHAMPION_ID, "terminal:champion", clock.now()
    )
    assert champion["status"] == "not_shotcaller" and champion["decision"] == "allow"
    register_watcher(store, clock)
    store.claim_request("R1", GAREN_RUNTIME, "claim-r1", clock.after(60), clock.now())
    counts = {
        "request_claim": store.connection.execute(
            "SELECT COUNT(*) FROM request_claims WHERE released_at IS NULL"
        ).fetchone()[0],
        "watcher_registration": store.connection.execute(
            "SELECT COUNT(*) FROM watcher_registrations WHERE actor_agent_id=?", (SHOTCALLER_ID,)
        ).fetchone()[0],
        "outbox_dispatch": store.connection.execute(
            "SELECT COUNT(*) FROM outbox_dispatch_leases"
        ).fetchone()[0],
    }
    assert counts == {"request_claim": 1, "watcher_registration": 1, "outbox_dispatch": 0}
    store.close()

    _, clear_store, clear_clock = create_context(root, "clear-stop")
    champion = clear_store.agent_status(CHAMPION_ID)
    settled = clear_store.transition(
        CHAMPION_ID,
        champion["version"],
        "completed",
        "Synthetic Champion obligations settled.",
        clear_clock.now(),
    )
    identity = OutboxDispatchIdentity(
        settled["outbox_id"],
        settled["event_id"],
        settled["recipient_agent_id"],
        "dispatcher:clear-stop",
        "attempt:clear-stop",
    )
    claim = clear_store.claim_outbox(
        identity, clear_clock.after(30), clear_clock.now()
    )
    clear_store.acknowledge_outbox(
        identity,
        claim["fence"],
        "watcher",
        "watcher_event",
        "effect:clear-stop",
        clear_clock.now(),
    )
    clear = clear_store.stop_decision(
        "Garen-clear", SHOTCALLER_ID, "terminal:clear", clear_clock.now()
    )
    assert clear["decision"] == "allow" and clear["status"] == "allowed"
    clear_store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-shotcaller-stop-") as temporary:
        root = Path(temporary)
        test_combined_obligations_one_block_per_generation(root)
        test_user_priority_explicit_allow_and_configuration(root)
        test_role_awareness_reconciliation_and_distinct_leases(root)
    print("PASS: role-aware Stop obligations, one block per fresh generation, stale-output refusal, user priority, and final allow")


if __name__ == "__main__":
    main()
