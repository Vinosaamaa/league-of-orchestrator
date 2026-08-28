#!/usr/bin/env python3
"""Hidden scientists reuse assignment launch state and deliver terminal-only."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.orchestration import OrchestrationSignals  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from league.storage_assignment import (  # noqa: E402
    FinishHiddenAssignmentCommand,
    PrepareAssignmentCommand,
)
from league.storage_request import DispatchRequestCommand  # noqa: E402
from league.storage_watcher import RuntimeRegistrationCommand  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    LUX_ID,
    capture_p100,
    create_context,
)
from storage_fixture import REPOSITORY, SHOTCALLER_ID  # noqa: E402


def _activate_hidden(root: Path, name: str):
    _, store, clock = create_context(root, name)
    capture_p100(store, clock)
    store.claim_request("R1", GAREN_RUNTIME, "claim-hidden", clock.after(7200), clock.now())
    dispatch = store.dispatch_request(
        DispatchRequestCommand(
            request_id="R1",
            claim_token="claim-hidden",
            dispatch_id="dispatch-hidden",
            work_kind="read-only",
            requested_mode="hidden",
            hidden_supported=True,
            requested_model="provider/strong",
            requested_effort="high",
            explicit_route=None,
            at=clock.now(),
            orchestration=OrchestrationSignals(True, True, False, 3, 2),
            hidden_subtask="Compare two bounded outputs",
            hidden_scope_budget="Two inputs and one comparison only",
        )
    )
    assert dispatch["execution_mode"] == "hidden"
    prepared = store.prepare_assignment(
        PrepareAssignmentCommand(
            assignment_id="assignment-hidden",
            request_id="R1",
            claim_token="claim-hidden",
            task_id="task-hidden",
            task_summary="Bounded comparison",
            coordinator_agent_id=SHOTCALLER_ID,
            champion_agent_id="worker:hidden:one",
            repository="",
            issue=0,
            branch="",
            worktree="",
            at=clock.now(),
            assignment_role="hidden-worker",
            dispatch_id="dispatch-hidden",
        )
    )
    store.mark_assignment_launching("assignment-hidden", 1, clock.now())
    receipt = {
        "verified": True,
        "assignment_id": "assignment-hidden",
        "task_id": "task-hidden",
        "hidden_worker_agent_id": "worker:hidden:one",
        "callsign": prepared["callsign"],
        "runtime_instance_id": "runtime:hidden:one",
        "thread_id": "session:hidden:one",
        "endpoint": "synthetic:hidden:one",
        "runtime_generation": "generation:hidden:one",
        "harness_kind": "codex-thread",
        "backend_kind": "herdr",
        "routing_name": prepared["callsign"].lower(),
        "display_agent": "synthetic",
        "capabilities": [],
        "bounded_subtask": "Compare two bounded outputs",
        "model": "provider/strong",
        "effort": "high",
        "routing_reason_code": "hidden_scientist",
        "time_budget_minutes": 3,
        "scope_budget_actions": 2,
    }
    active = store.activate_assignment(
        "assignment-hidden", 2, receipt, "event:hidden:active", "outbox:hidden:active", clock.now()
    )
    return store, clock, active


def _release_hidden(store, clock) -> str:
    store.register_runtime(
        RuntimeRegistrationCommand(
            runtime_instance_id="runtime:hidden:one",
            actor_agent_id="worker:hidden:one",
            harness_kind="codex-thread",
            backend_kind="herdr",
            session_ref="session:hidden:one",
            endpoint="synthetic:hidden:one",
            runtime_generation="generation:hidden:one",
            status="closed",
            verified=True,
            at=clock.now(),
        )
    )
    cleanup = "a" * 64
    store.release_callsign("callsign-assignment:assignment-hidden", 2, cleanup, clock.now())
    return cleanup


def test_persistence_terminal_delivery_and_roster_exclusion(root: Path) -> None:
    store, clock, active = _activate_hidden(root, "hidden-complete")
    try:
        row = store.connection.execute(
            "SELECT * FROM task_assignments WHERE task_assignment_id='assignment-hidden'"
        ).fetchone()
        assert tuple(
            row[key]
            for key in (
                "assignment_role",
                "bounded_subtask",
                "model",
                "effort",
                "routing_reason_code",
                "time_budget_minutes",
                "scope_budget_actions",
            )
        ) == (
            "hidden-worker",
            "Compare two bounded outputs",
            "provider/strong",
            "high",
            "hidden_scientist",
            3,
            2,
        )
        agent = store.connection.execute(
            "SELECT repository,issue,branch,worktree FROM agent_instances WHERE agent_id='worker:hidden:one'"
        ).fetchone()
        assert tuple(agent) == (None, None, None, None)
        try:
            store.transition_task(
                "task-hidden",
                active["runtime_instance_id"],
                3,
                "working",
                "unchanged",
                "continue",
                None,
                "transition:hidden:routine",
                "source:hidden:routine",
                "event:hidden:routine",
                "outbox:hidden:routine",
                SHOTCALLER_ID,
                clock.now(),
            )
        except StorageRefusal as exc:
            assert exc.code == "hidden_terminal_only"
        else:
            raise AssertionError("hidden scientist emitted routine progress")
        cleanup = _release_hidden(store, clock)
        terminal = store.finish_hidden_assignment(
            FinishHiddenAssignmentCommand(
                assignment_id="assignment-hidden",
                runtime_instance_id="runtime:hidden:one",
                expected_version=3,
                status="completed",
                result_summary="The bounded comparison agrees",
                cleanup_receipt=cleanup,
                unpublished_state_receipt="b" * 64,
                transition_id="transition:hidden:done",
                transition_key="source:hidden:done",
                event_id="event:hidden:done",
                outbox_id="outbox:hidden:done",
                at=clock.now(),
            )
        )
        assert terminal["state"] == "completed"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM delivery_outbox WHERE event_id='event:hidden:done'"
        ).fetchone()[0] == 1
        roster = store.roster_snapshot(
            as_of=clock.now(),
            recent_since=clock.after(-60),
            stale_before=clock.after(-60),
            visibility="local",
        )
        encoded = str(roster)
        assert "worker:hidden:one" not in encoded and "task-hidden" not in encoded
    finally:
        store.close()


def test_scope_expansion_promotes_to_new_visible_champion(root: Path) -> None:
    store, clock, _ = _activate_hidden(root, "hidden-promote")
    try:
        promoted = store.prepare_assignment(
            PrepareAssignmentCommand(
                assignment_id="assignment-visible",
                request_id="R1",
                claim_token="claim-hidden",
                task_id="task-visible",
                task_summary="Substantive follow-up",
                coordinator_agent_id=SHOTCALLER_ID,
                champion_agent_id=LUX_ID,
                repository=REPOSITORY,
                issue=36,
                branch="agent/synthetic/visible",
                worktree="/synthetic/worktrees/visible",
                at=clock.now(),
                assignment_role="champion",
                promoted_from_assignment_id="assignment-hidden",
            )
        )
        assert promoted["assignment_role"] == "champion"
        cleanup = _release_hidden(store, clock)
        terminal = store.finish_hidden_assignment(
            FinishHiddenAssignmentCommand(
                assignment_id="assignment-hidden",
                runtime_instance_id="runtime:hidden:one",
                expected_version=3,
                status="promotion_required",
                result_summary="Scope exceeded the read-only scientist boundary",
                cleanup_receipt=cleanup,
                unpublished_state_receipt="c" * 64,
                transition_id="transition:hidden:promote",
                transition_key="source:hidden:promote",
                event_id="event:hidden:promote",
                outbox_id="outbox:hidden:promote",
                at=clock.now(),
            )
        )
        assert terminal["state"] == "promotion_required"
        links = store.connection.execute(
            """
            SELECT assignment_role,promoted_to_assignment_id
              FROM task_assignments WHERE task_assignment_id='assignment-hidden'
            """
        ).fetchone()
        assert tuple(links) == ("hidden-worker", "assignment-visible")
        roster = store.roster_snapshot(
            as_of=clock.now(),
            recent_since=clock.after(-60),
            stale_before=clock.after(-60),
            visibility="local",
        )
        encoded = str(roster)
        assert "task-visible" in encoded and "task-hidden" not in encoded
    finally:
        store.close()


def test_stale_runtime_reconciliation_is_durable_and_terminal_only(root: Path) -> None:
    store, clock, _ = _activate_hidden(root, "hidden-stale-runtime")
    try:
        before = store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='hidden_scientist_terminal'"
        ).fetchone()[0]
        store.register_runtime(
            RuntimeRegistrationCommand(
                runtime_instance_id="runtime:hidden:one",
                actor_agent_id="worker:hidden:one",
                harness_kind="codex-thread",
                backend_kind="herdr",
                session_ref="session:hidden:one",
                endpoint="synthetic:hidden:one",
                runtime_generation="generation:hidden:one",
                status="failed",
                verified=True,
                at=clock.now(),
            )
        )
        stale = store.reconcile_assignment_runtime("assignment-hidden", clock.now())
        assert stale["state"] == "cleanup_pending"
        assert stale["runtime_status"] == "stale" and stale["version"] == 4
        assert store.reconcile_assignment_runtime(
            "assignment-hidden", clock.now()
        )["idempotent"]
        assert store.connection.execute(
            "SELECT required_policy FROM cleanup_obligations WHERE task_id='task-hidden'"
        ).fetchone()[0] == "stale_assignment_runtime"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='hidden_scientist_terminal'"
        ).fetchone()[0] == before
        cleanup = _release_hidden(store, clock)
        terminal = store.finish_hidden_assignment(
            FinishHiddenAssignmentCommand(
                assignment_id="assignment-hidden",
                runtime_instance_id="runtime:hidden:one",
                expected_version=4,
                status="failed",
                result_summary="The exact hidden runtime became stale",
                cleanup_receipt=cleanup,
                unpublished_state_receipt="d" * 64,
                transition_id="transition:hidden:stale",
                transition_key="source:hidden:stale",
                event_id="event:hidden:stale",
                outbox_id="outbox:hidden:stale",
                at=clock.now(),
            )
        )
        assert terminal["state"] == "failed" and terminal["version"] == 5
        assert store.connection.execute(
            "SELECT cleanup_state FROM cleanup_obligations WHERE task_id='task-hidden'"
        ).fetchone()[0] == "cleanup_completed"
    finally:
        store.close()


def test_unsafe_hidden_dispatch_requires_champion(root: Path) -> None:
    _, store, clock = create_context(root, "hidden-refusal")
    capture_p100(store, clock)
    try:
        store.claim_request("R1", GAREN_RUNTIME, "claim-hidden", clock.after(7200), clock.now())
        try:
            store.dispatch_request(
                DispatchRequestCommand(
                    request_id="R1",
                    claim_token="claim-hidden",
                    dispatch_id="dispatch-hidden",
                    work_kind="read-only",
                    requested_mode="hidden",
                    hidden_supported=True,
                    requested_model="provider/strong",
                    requested_effort="high",
                    explicit_route=None,
                    at=clock.now(),
                    orchestration=OrchestrationSignals(
                        True, True, False, 3, 2, runs_tests=True
                    ),
                    hidden_subtask="Run one test",
                    hidden_scope_budget="One test only",
                )
            )
        except StorageRefusal as exc:
            assert exc.code == "champion_required"
        else:
            raise AssertionError("test work was accepted as a hidden scientist")
    finally:
        store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-hidden-scientist-") as temporary:
        root = Path(temporary)
        test_persistence_terminal_delivery_and_roster_exclusion(root)
        test_scope_expansion_promotes_to_new_visible_champion(root)
        test_stale_runtime_reconciliation_is_durable_and_terminal_only(root)
        test_unsafe_hidden_dispatch_requires_champion(root)
    print("PASS: hidden assignment reuse, stale-runtime fencing, terminal-only delivery, cleanup, and visible promotion")


if __name__ == "__main__":
    main()
