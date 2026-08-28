#!/usr/bin/env python3
"""Explicit dispatch and recoverable visible-Champion assignment coverage."""

from __future__ import annotations

import tempfile
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
from league.storage import StorageRefusal  # noqa: E402
from lifecycle_fakes import FakeIds, FakeLaunchAdapter  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    LUX_ID,
    capture_p100,
    create_context,
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
    decision = store.dispatch_request(
        "R3",
        "claim-r3",
        "dispatch-r3",
        "repository-initialize",
        "champion",
        False,
        "user-model",
        "user-effort",
        "Taliyah",
        clock.now(),
    )
    assert decision["execution_mode"] == "champion"
    row = store.connection.execute(
        "SELECT requested_model,requested_effort,explicit_route,reason FROM request_dispatches WHERE request_id='R3'"
    ).fetchone()
    assert tuple(row[:3]) == ("user-model", "user-effort", "Taliyah")
    assert "visible Champion" in row["reason"]
    store.claim_request("R2", GAREN_RUNTIME, "claim-r2", clock.after(120), clock.now())
    hidden = store.dispatch_request(
        "R2",
        "claim-r2",
        "dispatch-r2-hidden",
        "read-only",
        "hidden",
        True,
        None,
        None,
        None,
        clock.now(),
    )
    assert hidden["execution_mode"] == "hidden"
    store.close()


def test_exact_receipt_and_failure_states(root: Path) -> None:
    _, store, clock = create_context(root, "exact-receipt")
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

    _, mismatch_store, mismatch_clock = create_context(root, "mismatch-receipt")
    capture_p100(mismatch_store, mismatch_clock)
    mismatch_store.claim_request(
        "R3", GAREN_RUNTIME, "claim-r3", mismatch_clock.after(120), mismatch_clock.now()
    )
    mismatch_store.dispatch_request(
        "R3",
        "claim-r3",
        "dispatch-r3",
        "repository-write",
        "champion",
        False,
        None,
        None,
        None,
        mismatch_clock.now(),
    )

    class MismatchAdapter(FakeLaunchAdapter):
        def launch(self, assignment_spec):
            receipt = super().launch(assignment_spec)
            receipt["worktree"] = "/synthetic/wrong-worktree"
            return receipt

    try:
        AssignmentService(mismatch_store, MismatchAdapter(), mismatch_clock, FakeIds()).assign(
            spec("claim-r3", suffix="mismatch")
        )
    except StorageRefusal as exc:
        assert exc.code == "receipt_mismatch"
    else:
        raise AssertionError("mismatched Champion receipt was accepted")
    assert mismatch_store.connection.execute(
        "SELECT state FROM task_assignments WHERE task_assignment_id='assignment:mismatch'"
    ).fetchone()[0] == "launching"
    mismatch_store.close()

    _, failed_store, failed_clock = create_context(root, "cleanup-pending")
    capture_p100(failed_store, failed_clock)
    failed_store.claim_request(
        "R3", GAREN_RUNTIME, "claim-r3", failed_clock.after(120), failed_clock.now()
    )
    failed_store.dispatch_request(
        "R3",
        "claim-r3",
        "dispatch-r3",
        "long-running",
        "champion",
        False,
        None,
        None,
        None,
        failed_clock.now(),
    )
    failure = FakeLaunchAdapter(
        failure=LaunchAdapterError(
            "synthetic_partial_launch", cleanup_required=True, cleanup_proven=False
        )
    )
    pending = AssignmentService(failed_store, failure, failed_clock, FakeIds()).assign(
        spec("claim-r3", suffix="cleanup")
    )
    assert pending["state"] == "cleanup_pending"
    assert failed_store.connection.execute(
        "SELECT cleanup_state FROM cleanup_obligations WHERE task_id='task:cleanup'"
    ).fetchone()[0] == "pending"
    assert failed_store.connection.execute(
        "SELECT COUNT(*) FROM callsign_leases WHERE callsign='Lux'"
    ).fetchone()[0] == 1
    failed_store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-assignment-dispatch-") as temporary:
        root = Path(temporary)
        test_empty_repository_refuses_direct_before_first_write(root)
        test_exact_receipt_and_failure_states(root)
    print("PASS: explicit dispatch before writes, preserved routing, verified assignment receipt, and cleanup-pending recovery")


if __name__ == "__main__":
    main()
