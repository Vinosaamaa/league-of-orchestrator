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
from league.storage import (  # noqa: E402
    OutboxDispatchIdentity,
    PrepareAssignmentCommand,
    StorageRefusal,
)
from league.sqlite_watcher_ops import stop_feedback_reason  # noqa: E402
from league.request_services import AssignmentSpec  # noqa: E402
from lifecycle_fakes import issue_bound_spec  # noqa: E402
from storage_fixture import CHAMPION_ID, REPOSITORY, SHOTCALLER_ID  # noqa: E402


def register_watcher(store, clock, scope="Garen-lifecycle", *, block=True, fence=1):
    return store.register_watcher(
        scope,
        f"watcher:persistent:{scope}",
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        f"unix:/tmp/league-test-{scope}.sock",
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
            **{
                key: value
                for key, value in vars(bound).items()
                if key not in {"callsign", "routing_name", "launch_operation_id"}
            },
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


def test_attached_shotcaller_blocks_every_stop_with_obligations(root: Path) -> None:
    _, store, clock = create_context(root, "combined-stop")
    capture_p100(store, clock)
    add_combined_obligations(store, clock)
    register_watcher(store, clock)
    first = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal-generation-1", clock.now()
    )
    assert first["decision"] == "block" and first["status"] == "blocked_attached"
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
    assert repeated["decision"] == "block" and repeated["terminal_fresh"] is False
    third = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal-generation-2", clock.now()
    )
    assert third["decision"] == "block" and third["terminal_fresh"] is True
    rearmed = store.rearm_wait(
        "Garen-lifecycle", SHOTCALLER_ID, "event:fresh-wait", clock.now()
    )
    assert rearmed["wait_generation"] > first["wait_generation"]
    fresh = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal-generation-3", clock.now()
    )
    assert fresh["decision"] == "block" and fresh["wait_generation"] == rearmed["wait_generation"]
    store.close()


def test_detachment_requires_verified_live_watcher_handoff(root: Path) -> None:
    _, store, clock = create_context(root, "priority-stop")
    capture_p100(store, clock)
    add_combined_obligations(store, clock)
    register_watcher(store, clock)
    user = store.note_user_message(
        "Garen-lifecycle", SHOTCALLER_ID, clock.now()
    )
    priority = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal:user", clock.now()
    )
    assert priority["decision"] == "block" and priority["status"] == "blocked_attached"
    next_stop = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal:user", clock.now()
    )
    assert next_stop["decision"] == "block"
    rearmed = store.rearm_wait(
        "Garen-lifecycle", SHOTCALLER_ID, "event:explicit", clock.now()
    )
    before = store.connection.total_changes
    try:
        store.set_allow_stop_once("Garen-lifecycle", SHOTCALLER_ID)
    except StorageRefusal as exc:
        assert exc.code == "owner_stop_required"
    else:
        raise AssertionError("retired generic one-shot Stop bypass was accepted")
    assert store.connection.total_changes == before
    # A legacy imported bit is data only; it can no longer bypass supervision.
    with store._transaction():
        store.connection.execute(
            "UPDATE watcher_scopes SET allow_stop_once=1 WHERE scope_id='Garen-lifecycle'"
        )
    for terminal in ("terminal:explicit", "terminal:explicit", "terminal:rearmed"):
        blocked = store.stop_decision(
            "Garen-lifecycle", SHOTCALLER_ID, terminal, clock.now()
        )
        assert blocked["decision"] == "block"
        assert blocked["status"] == "blocked_attached"
        assert blocked["wait_generation"] == rearmed["wait_generation"]
    detached = store.set_supervision_attachment(
        "Garen-lifecycle", SHOTCALLER_ID, "detached", clock.now()
    )
    assert detached["detachment_receipt"]["wake_locator"] == (
        "unix:/tmp/league-test-Garen-lifecycle.sock"
    )
    owner_action = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal:detached-owner", clock.now()
    )
    assert owner_action["decision"] == "block"
    assert owner_action["status"] == "blocked_detached_owner_action"
    scope = store.connection.execute(
        """
        SELECT stop_blocked,wait_active,last_blocked_wait_generation,
               pending_stop_feedback_digest,pending_stop_terminal_generation,
               pending_stop_wait_generation
          FROM watcher_scopes WHERE scope_id='Garen-lifecycle'
        """
    ).fetchone()
    assert scope["stop_blocked"] == 1 and scope["wait_active"] == 1
    assert scope["last_blocked_wait_generation"] == owner_action["wait_generation"]
    assert scope["pending_stop_terminal_generation"] == "terminal:detached-owner"
    assert scope["pending_stop_wait_generation"] == owner_action["wait_generation"]
    assert scope["pending_stop_feedback_digest"] is not None
    feedback = stop_feedback_reason(
        "Garen",
        owner_action["wait_generation"],
        tuple(owner_action["unresolved_summaries"]),
    )
    assert store.consume_stop_feedback(
        "Garen-lifecycle",
        SHOTCALLER_ID,
        "terminal:detached-owner",
        feedback,
    )
    assert not store.consume_stop_feedback(
        "Garen-lifecycle",
        SHOTCALLER_ID,
        "terminal:detached-owner",
        feedback,
    )
    with store._transaction():
        store.connection.execute(
            "UPDATE requests SET state='answered' WHERE owner_agent_id=?",
            (SHOTCALLER_ID,),
        )
    handed_off = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal:detached", clock.now()
    )
    assert handed_off["decision"] == "allow"
    assert handed_off["status"] == "detached_handoff_verified"
    store.release_watcher(
        "watcher:persistent:Garen-lifecycle", SHOTCALLER_ID, 1, clock.now()
    )
    unavailable = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal:no-watcher", clock.now()
    )
    assert unavailable["decision"] == "block"
    assert unavailable["status"] == "supervisor_unavailable", unavailable
    scope = store.connection.execute(
        """
        SELECT stop_blocked,wait_active,last_blocked_wait_generation,
               pending_stop_feedback_digest,pending_stop_terminal_generation,
               pending_stop_wait_generation
          FROM watcher_scopes WHERE scope_id='Garen-lifecycle'
        """
    ).fetchone()
    assert scope["stop_blocked"] == 1 and scope["wait_active"] == 1
    assert scope["last_blocked_wait_generation"] == unavailable["wait_generation"]
    assert scope["pending_stop_terminal_generation"] == "terminal:no-watcher"
    assert scope["pending_stop_wait_generation"] == unavailable["wait_generation"]
    assert scope["pending_stop_feedback_digest"] is not None
    assert user["user_message_generation"] == 1
    store.close()


def test_detached_stop_blocks_owner_decision_task_states(root: Path) -> None:
    _, store, clock = create_context(root, "detached-decision-states")
    capture_p100(store, clock)
    add_combined_obligations(store, clock)
    register_watcher(store, clock)
    store.set_supervision_attachment(
        "Garen-lifecycle", SHOTCALLER_ID, "detached", clock.now()
    )
    with store._transaction():
        store.connection.execute("UPDATE requests SET state='answered'")
        store.connection.execute(
            "UPDATE tasks SET state='blocked' WHERE task_id='task:pending'"
        )
    blocked = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal:blocked-task", clock.now()
    )
    assert blocked["decision"] == "block"
    assert blocked["status"] == "blocked_detached_owner_action"
    assert blocked["obligations"]["decision_tasks"] == 1
    feedback = stop_feedback_reason("Garen", blocked["wait_generation"])
    assert store.consume_stop_feedback(
        "Garen-lifecycle",
        SHOTCALLER_ID,
        "terminal:blocked-task",
        feedback,
    )
    assert not store.consume_stop_feedback(
        "Garen-lifecycle",
        SHOTCALLER_ID,
        "terminal:blocked-task",
        feedback,
    )

    with store._transaction():
        store.connection.execute(
            "UPDATE tasks SET state='ready_to_land' WHERE task_id='task:pending'"
        )
    ready = store.stop_decision(
        "Garen-lifecycle", SHOTCALLER_ID, "terminal:ready-task", clock.now()
    )
    assert ready["decision"] == "block"
    assert ready["status"] == "blocked_detached_owner_action"
    assert ready["obligations"]["decision_tasks"] == 1
    scope = store.connection.execute(
        """
        SELECT stop_blocked,wait_active,pending_stop_feedback_digest,
               pending_stop_terminal_generation,pending_stop_wait_generation
          FROM watcher_scopes WHERE scope_id='Garen-lifecycle'
        """
    ).fetchone()
    assert scope["stop_blocked"] == 1 and scope["wait_active"] == 1
    assert scope["pending_stop_feedback_digest"] is not None
    assert scope["pending_stop_terminal_generation"] == "terminal:ready-task"
    assert scope["pending_stop_wait_generation"] == ready["wait_generation"]
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
        test_attached_shotcaller_blocks_every_stop_with_obligations(root)
        test_detachment_requires_verified_live_watcher_handoff(root)
        test_detached_stop_blocks_owner_decision_task_states(root)
        test_role_awareness_reconciliation_and_distinct_leases(root)
    print("PASS: attached obligations block every Stop, detached handoff is verified, stale output refuses, and user priority is preserved")


if __name__ == "__main__":
    main()
