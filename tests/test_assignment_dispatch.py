#!/usr/bin/env python3
"""Explicit dispatch and recoverable visible-Champion assignment coverage."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.request_services import (  # noqa: E402
    AssignmentService,
    AssignmentSpec,
    DispatchService,
    LaunchAdapterError,
)
from league.storage import PrepareAssignmentCommand, StorageRefusal  # noqa: E402
from lifecycle_fakes import FakeIds, FakeLaunchAdapter  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    LUX_ID,
    capture_p100,
    create_context,
    dispatch_request,
)
from storage_fixture import REPOSITORY, SHOTCALLER_ID  # noqa: E402


def spec(claim: str, *, suffix: str = "one") -> AssignmentSpec:
    return AssignmentSpec(
        assignment_id=f"assignment:{suffix}",
        request_id="R3",
        claim_token=claim,
        task_id=f"task:{suffix}",
        task_summary="Synthetic visible Champion assignment",
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID,
        callsign="Lux",
        repository=REPOSITORY,
        issue=17,
        branch=f"agent/synthetic/{suffix}",
        worktree=f"/synthetic/worktrees/{suffix}",
    )


def test_empty_repository_refuses_direct_before_first_write(root: Path) -> None:
    _, store, clock = create_context(root, "empty-repo")
    capture_p100(store, clock)
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    empty_repo = root / "empty-repository"
    empty_repo.mkdir()
    first_write = empty_repo / "README.md"

    def mutate() -> None:
        first_write.write_text("should never be written\n", encoding="utf-8")

    try:
        DispatchService(store, clock, FakeIds()).run_direct(
            "R3", "claim-r3", "repository-initialize", mutate
        )
    except StorageRefusal as exc:
        assert exc.code == "champion_required"
    else:
        raise AssertionError("unsafe direct dispatch was accepted")
    assert not first_write.exists()
    decision = dispatch_request(
        store,
        clock,
        "R3",
        "claim-r3",
        "dispatch-r3",
        "repository-initialize",
        "champion",
        requested_model="user-model",
        requested_effort="user-effort",
        explicit_route="Taliyah",
    )
    assert decision["execution_mode"] == "champion"
    row = store.connection.execute(
        "SELECT requested_model,requested_effort,explicit_route,reason FROM request_dispatches WHERE request_id='R3'"
    ).fetchone()
    assert tuple(row[:3]) == ("user-model", "user-effort", "Taliyah")
    assert "visible Champion" in row["reason"]
    store.claim_request("R2", GAREN_RUNTIME, "claim-r2", clock.after(120), clock.now())
    hidden = dispatch_request(
        store,
        clock,
        "R2",
        "claim-r2",
        "dispatch-r2-hidden",
        "read-only",
        "hidden",
        hidden_supported=True,
    )
    assert hidden["execution_mode"] == "hidden"
    store.close()


def champion_context(root: Path, name: str, work_kind: str = "repository-write"):
    _, store, clock = create_context(root, name)
    capture_p100(store, clock)
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    dispatch_request(
        store, clock, "R3", "claim-r3", "dispatch-r3", work_kind, "champion"
    )
    return store, clock


def test_exact_receipt_activation_and_atomic_rollback(root: Path) -> None:
    store, clock = champion_context(root, "exact-receipt")
    ids = FakeIds()
    adapter = FakeLaunchAdapter()
    active = AssignmentService(store, adapter, clock, ids).assign(spec("claim-r3"))
    assert active["state"] == "active" and len(adapter.calls) == 1
    assignment = store.connection.execute(
        "SELECT state,acceptance_receipt_json,runtime_instance_id FROM task_assignments WHERE task_assignment_id=?",
        (active["assignment_id"],),
    ).fetchone()
    assert assignment["state"] == "active" and assignment["acceptance_receipt_json"]
    assert assignment["runtime_instance_id"] == active["runtime_instance_id"]
    committed_retry = store.activate_assignment(
        active["assignment_id"],
        active["version"],
        adapter.launch(spec("claim-r3")),
        "ignored-event-retry",
        "ignored-outbox-retry",
        clock.now(),
    )
    assert committed_retry["idempotent"]
    assert committed_retry["event_id"] == active["event_id"]
    assert committed_retry["outbox_id"] == active["outbox_id"]
    try:
        store.transition_task(
            active["task_id"],
            active["runtime_instance_id"],
            3,
            "completed",
            "Synthetic result that must roll back",
            "Coordinator synthesizes the result",
            None,
            "transition:rollback",
            "transition-key:rollback",
            "event:rollback",
            active["outbox_id"],
            SHOTCALLER_ID,
            clock.now(),
        )
    except StorageRefusal:
        pass
    else:
        raise AssertionError("task transition survived an outbox collision")
    task = store.connection.execute(
        "SELECT state,version,result_summary FROM tasks WHERE task_id=?", (active["task_id"],)
    ).fetchone()
    assert tuple(task) == ("in_progress", 3, None)
    assert store.connection.execute(
        "SELECT COUNT(*) FROM task_transitions WHERE transition_key='transition-key:rollback'"
    ).fetchone()[0] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_id='event:rollback'"
    ).fetchone()[0] == 0
    store.close()


def test_receipt_mismatch_creates_cleanup_pending(root: Path) -> None:
    store, clock = champion_context(root, "mismatch-receipt")
    class MismatchAdapter(FakeLaunchAdapter):
        def launch(self, assignment_spec):
            receipt = super().launch(assignment_spec)
            receipt["worktree"] = "/synthetic/wrong-worktree"
            return receipt

    outcome = AssignmentService(store, MismatchAdapter(), clock, FakeIds()).assign(
        spec("claim-r3", suffix="mismatch")
    )
    assert outcome["state"] == "cleanup_pending"
    assignment = store.connection.execute(
        "SELECT state,failure_class FROM task_assignments WHERE task_assignment_id='assignment:mismatch'"
    ).fetchone()
    assert tuple(assignment) == ("cleanup_pending", "launch_receipt_mismatch")
    assert store.connection.execute(
        "SELECT cleanup_state FROM cleanup_obligations WHERE task_id='task:mismatch'"
    ).fetchone()[0] == "pending"
    store.close()


def test_partial_launch_preserves_cleanup_pending(root: Path) -> None:
    store, clock = champion_context(root, "cleanup-pending", "long-running")
    failure = FakeLaunchAdapter(
        failure=LaunchAdapterError(
            "synthetic_partial_launch", cleanup_required=True, cleanup_proven=False
        )
    )
    pending = AssignmentService(store, failure, clock, FakeIds()).assign(
        spec("claim-r3", suffix="cleanup")
    )
    assert pending["state"] == "cleanup_pending"
    assert store.connection.execute(
        "SELECT cleanup_state FROM cleanup_obligations WHERE task_id='task:cleanup'"
    ).fetchone()[0] == "pending"
    assert store.connection.execute(
        "SELECT COUNT(*) FROM callsign_leases WHERE callsign='Lux'"
    ).fetchone()[0] == 1
    store.close()


def test_unwrapped_adapter_failure_cannot_strand_launching(root: Path) -> None:
    store, clock = champion_context(root, "adapter-operational-failure")

    class OperationalFailureAdapter(FakeLaunchAdapter):
        def launch(self, assignment_spec):
            raise RuntimeError("synthetic adapter failure")

    outcome = AssignmentService(
        store, OperationalFailureAdapter(), clock, FakeIds()
    ).assign(spec("claim-r3", suffix="operational"))
    assert outcome["state"] == "cleanup_pending"
    assignment = store.connection.execute(
        "SELECT state,failure_class FROM task_assignments WHERE task_assignment_id='assignment:operational'"
    ).fetchone()
    assert tuple(assignment) == ("cleanup_pending", "launch_adapter_runtimeerror")
    store.close()


def test_assignment_retry_compares_complete_launch_identity(root: Path) -> None:
    store, clock = champion_context(root, "assignment-retry-identity")
    base = spec("claim-r3", suffix="identity")
    command = PrepareAssignmentCommand(**vars(base), at=clock.now())
    created = store.prepare_assignment(command)
    assert created["state"] == "pending" and not created["idempotent"]
    assert store.prepare_assignment(command)["idempotent"]
    changes = (
        {"task_summary": "Different task summary"},
        {"repository": "synthetic://different-repository"},
        {"issue": 999},
        {"branch": "agent/synthetic/different-branch"},
        {"worktree": "/synthetic/worktrees/different-worktree"},
    )
    for change in changes:
        try:
            store.prepare_assignment(replace(command, **change))
        except StorageRefusal as exc:
            assert exc.code == "assignment_conflict"
        else:
            raise AssertionError(f"assignment retry accepted changed identity: {change}")
    store.close()


def test_task_transition_matrix_refuses_illegal_and_terminal_progression(root: Path) -> None:
    store, clock = champion_context(root, "task-transition-matrix")
    active = AssignmentService(store, FakeLaunchAdapter(), clock, FakeIds()).assign(
        spec("claim-r3", suffix="matrix")
    )
    try:
        store.transition_task(
            active["task_id"],
            active["runtime_instance_id"],
            3,
            "active",
            "Illegal reverse transition",
            "No action",
            None,
            "transition:matrix:illegal",
            "transition-key:matrix:illegal",
            "event:matrix:illegal",
            "outbox:matrix:illegal",
            SHOTCALLER_ID,
            clock.now(),
        )
    except StorageRefusal as exc:
        assert exc.code == "invalid_task_transition"
    else:
        raise AssertionError("illegal task transition was accepted")
    completed = store.transition_task(
        active["task_id"],
        active["runtime_instance_id"],
        3,
        "completed",
        "Synthetic terminal result",
        "Coordinator synthesizes the result",
        None,
        "transition:matrix:completed",
        "transition-key:matrix:completed",
        "event:matrix:completed",
        "outbox:matrix:completed",
        SHOTCALLER_ID,
        clock.now(),
    )
    assert completed["version"] == 4
    try:
        store.transition_task(
            active["task_id"],
            active["runtime_instance_id"],
            4,
            "working",
            "Contradictory post-terminal work",
            "No action",
            None,
            "transition:matrix:post-terminal",
            "transition-key:matrix:post-terminal",
            "event:matrix:post-terminal",
            "outbox:matrix:post-terminal",
            SHOTCALLER_ID,
            clock.now(),
        )
    except StorageRefusal as exc:
        assert exc.code == "task_terminal"
    else:
        raise AssertionError("terminal task accepted another transition")
    assert store.connection.execute(
        "SELECT COUNT(*) FROM task_transitions WHERE task_id=?", (active["task_id"],)
    ).fetchone()[0] == 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_id IN ('event:matrix:illegal','event:matrix:post-terminal')"
    ).fetchone()[0] == 0
    store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-assignment-dispatch-") as temporary:
        root = Path(temporary)
        test_empty_repository_refuses_direct_before_first_write(root)
        test_exact_receipt_activation_and_atomic_rollback(root)
        test_receipt_mismatch_creates_cleanup_pending(root)
        test_partial_launch_preserves_cleanup_pending(root)
        test_unwrapped_adapter_failure_cannot_strand_launching(root)
        test_assignment_retry_compares_complete_launch_identity(root)
        test_task_transition_matrix_refuses_illegal_and_terminal_progression(root)
    print("PASS: explicit dispatch, exact assignment identity, verified receipt, and all launch failures recover through cleanup-pending")


if __name__ == "__main__":
    main()
