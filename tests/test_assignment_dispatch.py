#!/usr/bin/env python3
"""Explicit dispatch and recoverable visible-Champion assignment coverage."""

from __future__ import annotations

import tempfile
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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
from lifecycle_fakes import FakeIds, FakeLaunchAdapter, issue_bound_spec  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    LUX_ID,
    capture_p100,
    create_context,
    dispatch_request,
)
from storage_fixture import REPOSITORY, SHOTCALLER_ID  # noqa: E402
from storage_test_support import invoke_cli  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402


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
        issue_receipt=None,
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
        requested_model="synthetic-scientist",
        requested_effort="low",
        hidden_subtask="Summarize the bounded synthetic record",
        hidden_scope_budget="Read one synthetic record and return one summary",
    )
    assert hidden["execution_mode"] == "hidden"
    store.close()


def test_durable_work_kinds_cannot_hide_implementation_ownership(root: Path) -> None:
    for suffix, work_kind, mode in (
        ("research", "durable-research", "direct"),
        ("benchmark", "benchmark", "direct"),
        ("release", "release", "hidden"),
        ("debugging", "debugging", "hidden"),
        ("bug-fix", "bug-fix", "direct"),
    ):
        store, clock = champion_context(root, f"durable-{suffix}", "read-only")
        try:
            dispatch_request(
                store,
                clock,
                "R3",
                "claim-r3",
                f"dispatch-{suffix}",
                work_kind,
                mode,
                hidden_supported=True,
                requested_model="synthetic-model" if mode == "hidden" else None,
                requested_effort="low" if mode == "hidden" else None,
                hidden_subtask="Do not own implementation" if mode == "hidden" else None,
                hidden_scope_budget="One bounded note" if mode == "hidden" else None,
            )
        except StorageRefusal as exc:
            assert exc.code == "champion_required", (work_kind, exc.code)
        else:
            raise AssertionError(f"{work_kind} incorrectly accepted {mode} execution")
        store.close()


def test_cli_prepare_cannot_bypass_owner_issue_verification(root: Path) -> None:
    state, store, clock = create_context(root, "prepare-issue-bypass")
    adapter = FakeLaunchAdapter()
    try:
        AssignmentService(store, adapter, clock, FakeIds()).assign(
            spec("claim:missing", suffix="service-bypass")
        )
    except StorageRefusal as exc:
        assert exc.code == "issue_verification_required"
    else:
        raise AssertionError("AssignmentService accepted missing issue evidence")
    assert adapter.calls == []
    store.close()
    refusal = invoke_cli(
        state,
        "assign",
        "prepare",
        "--assignment-id",
        "assignment:issue-bypass",
        "--request-id",
        "request:issue-bypass",
        "--claim-token",
        "claim:issue-bypass",
        "--task-id",
        "task:issue-bypass",
        "--task-summary",
        "Synthetic issue bypass",
        "--coordinator-agent-id",
        SHOTCALLER_ID,
        "--champion-agent-id",
        LUX_ID,
        "--repository",
        REPOSITORY,
        "--issue",
        "81",
        "--branch",
        "agent/synthetic/81",
        "--worktree",
        "/synthetic/worktree",
        "--at",
        "2026-01-01T00:00:00Z",
        expected=2,
    )
    assert refusal["error"]["code"] == "issue_verification_required"


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
    bound = issue_bound_spec(store, spec("claim-r3"), clock.now())
    active = AssignmentService(store, adapter, clock, ids).assign(bound)
    assert active["state"] == "active" and len(adapter.calls) == 1
    assignment = store.connection.execute(
        "SELECT state,acceptance_receipt_json,runtime_instance_id FROM task_assignments WHERE task_assignment_id=?",
        (active["assignment_id"],),
    ).fetchone()
    assert assignment["state"] == "active" and assignment["acceptance_receipt_json"]
    assert assignment["runtime_instance_id"] == active["runtime_instance_id"]
    # Synthetic membership normally established by the visible launch service.
    store.connection.execute(
        "INSERT INTO squad_champions(squad_id,champion_agent_id,joined_at) VALUES('squad:Garen',?,?)",
        (LUX_ID, clock.now()),
    )
    startup = store.startup_context(LUX_ID, active["runtime_instance_id"], clock.now())
    assert startup["verified"] and startup["task"]["task_id"] == active["task_id"]
    reservation_id = f"callsign-assignment:{active['assignment_id']}"
    # Historical producer omitted this field after committing valid activation.
    store.connection.execute("UPDATE callsign_assignments SET runtime_instance_id=NULL WHERE callsign_assignment_id=?",
                             (reservation_id,))
    receipt = adapter.launch(bound)
    before = tuple(store.connection.iterdump())
    for version, changed in ((active["version"] - 1, receipt),
                             (active["version"], {**receipt, "thread_id": "foreign"})):
        try:
            store.activate_assignment(active["assignment_id"], version, changed,
                                      "ignored-event-retry", "ignored-outbox-retry", clock.now())
        except StorageRefusal:
            pass
        else:
            raise AssertionError("changed repair evidence accepted")
        assert tuple(store.connection.iterdump()) == before
    repaired = store.activate_assignment(active["assignment_id"], active["version"], receipt,
                                         "ignored-event-retry", "ignored-outbox-retry", clock.now())
    assert repaired["idempotent"] and repaired["outbox_id"] == active["outbox_id"]
    assert store.startup_context(LUX_ID, active["runtime_instance_id"], clock.now())["verified"]
    repaired_state = tuple(store.connection.iterdump())
    committed_retry = store.activate_assignment(
        active["assignment_id"],
        active["version"],
        adapter.launch(bound),
        "ignored-event-retry",
        "ignored-outbox-retry",
        clock.now(),
    )
    assert committed_retry["idempotent"]
    assert committed_retry["event_id"] == active["event_id"]
    assert committed_retry["outbox_id"] == active["outbox_id"]
    assert tuple(store.connection.iterdump()) == repaired_state
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


def test_canonical_task_semantics_refuse_self_matching_unrelated_issue(root: Path) -> None:
    store, clock = champion_context(root, "semantic-binding")
    release_spec = replace(
        spec("claim-r3", suffix="semantic-binding"),
        task_summary="Publish release notes",
    )
    bound = issue_bound_spec(store, release_spec, clock.now())
    active = AssignmentService(store, FakeLaunchAdapter(), clock, FakeIds()).assign(bound)
    assert active["state"] == "active"

    store.connection.execute(
        "UPDATE tasks SET summary='Implement authentication' WHERE task_id=?",
        (bound.task_id,),
    )
    retry = PrepareAssignmentCommand(
        assignment_id=bound.assignment_id,
        request_id=bound.request_id,
        claim_token=bound.claim_token,
        task_id=bound.task_id,
        task_summary="Implement authentication",
        coordinator_agent_id=bound.coordinator_agent_id,
        champion_agent_id=bound.champion_agent_id,
        repository=bound.repository,
        issue=bound.issue,
        branch=bound.branch,
        worktree=bound.worktree,
        at=clock.now(),
        required_capabilities=bound.required_capabilities,
        issue_receipt=bound.issue_receipt,
    )
    try:
        store.prepare_assignment(retry)
    except StorageRefusal as exc:
        assert exc.code == "issue_semantic_binding_mismatch"
    else:
        raise AssertionError(
            "an authentication task accepted a self-matching release-notes issue"
        )
    store.close()


def test_receipt_mismatch_creates_cleanup_pending(root: Path) -> None:
    store, clock = champion_context(root, "mismatch-receipt")
    class MismatchAdapter(FakeLaunchAdapter):
        def launch(self, assignment_spec):
            receipt = super().launch(assignment_spec)
            receipt["worktree"] = "/synthetic/wrong-worktree"
            return receipt

    bound = issue_bound_spec(store, spec("claim-r3", suffix="mismatch"), clock.now())
    outcome = AssignmentService(store, MismatchAdapter(), clock, FakeIds()).assign(bound)
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
    bound = issue_bound_spec(store, spec("claim-r3", suffix="cleanup"), clock.now())
    pending = AssignmentService(store, failure, clock, FakeIds()).assign(bound)
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

    bound = issue_bound_spec(store, spec("claim-r3", suffix="operational"), clock.now())
    outcome = AssignmentService(
        store, OperationalFailureAdapter(), clock, FakeIds()
    ).assign(bound)
    assert outcome["state"] == "cleanup_pending"
    assignment = store.connection.execute(
        "SELECT state,failure_class FROM task_assignments WHERE task_assignment_id='assignment:operational'"
    ).fetchone()
    assert tuple(assignment) == ("cleanup_pending", "launch_adapter_runtimeerror")
    store.close()


def test_proven_failed_launch_settles_only_cleanup(root: Path) -> None:
    store, clock = champion_context(root, "proven-failed-launch")
    bound = issue_bound_spec(store, spec("claim-r3", suffix="settle"), clock.now())
    pending = AssignmentService(
        store,
        FakeLaunchAdapter(failure=LaunchAdapterError(
            "synthetic_partial_launch", cleanup_required=True, cleanup_proven=False
        )),
        clock, FakeIds(),
    ).assign(bound)
    result = store.block_assignment(
        bound.assignment_id, pending["version"], "synthetic_partial_launch",
        False, True, clock.now(),
    )
    tables = json.loads(store.export_bytes(
        format_name="json", purpose="rollback", max_records=1000
    ))["tables"]
    obligation = next(r for r in tables["cleanup_obligations"] if r["task_id"] == bound.task_id)
    assert obligation["cleanup_state"] == "cleanup_completed"
    assert obligation["version"] == 2 and obligation["next_action"] == "None"
    assignment = next(r for r in tables["task_assignments"] if r["task_assignment_id"] == bound.assignment_id)
    reservation = next(r for r in tables["callsign_assignments"] if r["scope_id"] == bound.task_id)
    assert result["state"] == assignment["state"] == "blocked"
    assert assignment["cleanup_receipt"] == reservation["failure_receipt_digest"]
    assert reservation["state"] == "rolled_back"
    assert next(r for r in tables["tasks"] if r["task_id"] == bound.task_id)["state"] == "blocked"
    assert next(r for r in tables["requests"] if r["request_id"] == "R3")["state"] != "answered"
    store.close()


def test_failed_launch_settlement_retry_is_exact(root: Path) -> None:
    store, clock = champion_context(root, "failed-launch-retry")
    bound = issue_bound_spec(store, spec("claim-r3", suffix="retry-cleanup"), clock.now())
    pending = AssignmentService(
        store, FakeLaunchAdapter(failure=LaunchAdapterError(
            "synthetic_partial_launch", cleanup_required=True, cleanup_proven=False
        )), clock, FakeIds(),
    ).assign(bound)
    args = (bound.assignment_id, pending["version"], "synthetic_partial_launch", False, True, clock.now())
    result = store.block_assignment(*args)
    before = store.export_bytes(format_name="json", purpose="rollback", max_records=1000)
    retry = store.block_assignment(*args)
    assert retry["idempotent"] is True and retry["version"] == result["version"]
    assert store.export_bytes(format_name="json", purpose="rollback", max_records=1000) == before
    store.close()


def test_already_blocked_failed_launch_recovers_exact_receipt(root: Path) -> None:
    store, clock = champion_context(root, "legacy-failed-launch")
    bound = issue_bound_spec(store, spec("claim-r3", suffix="legacy-cleanup"), clock.now())
    pending = AssignmentService(
        store, FakeLaunchAdapter(failure=LaunchAdapterError(
            "synthetic_partial_launch", cleanup_required=True, cleanup_proven=False
        )), clock, FakeIds(),
    ).assign(bound)
    blocked = store.block_assignment(
        bound.assignment_id, pending["version"], "synthetic_partial_launch", False, True, clock.now()
    )
    # Historical fixture only: the old writer rolled back the reservation but
    # omitted these two settlement writes. Never applied to canonical state.
    store.connection.execute(
        "UPDATE task_assignments SET cleanup_receipt=NULL WHERE task_assignment_id=?",
        (bound.assignment_id,),
    )
    store.connection.execute(
        "UPDATE cleanup_obligations SET cleanup_state='pending',version=1 WHERE task_id=?",
        (bound.task_id,),
    )
    before = json.loads(store.export_bytes(format_name="json", purpose="rollback", max_records=1000))["tables"]
    args = (bound.assignment_id, blocked["version"], "synthetic_partial_launch", False, True, clock.now())
    recovered = store.block_assignment(*args)
    after = json.loads(store.export_bytes(format_name="json", purpose="rollback", max_records=1000))["tables"]
    assert recovered["version"] == blocked["version"] + 1
    assert next(r for r in after["cleanup_obligations"] if r["task_id"] == bound.task_id)["cleanup_state"] == "cleanup_completed"
    for table in ("tasks", "requests", "agent_instances", "callsign_assignments", "callsign_queue", "delivery_outbox"):
        assert before[table] == after[table], table
    assert store.block_assignment(*args)["idempotent"] is True
    store.close()


def test_failed_launch_foreign_policy_refuses_atomically(root: Path) -> None:
    store, clock = champion_context(root, "foreign-cleanup-policy")
    bound = issue_bound_spec(store, spec("claim-r3", suffix="foreign-policy"), clock.now())
    pending = AssignmentService(
        store, FakeLaunchAdapter(failure=LaunchAdapterError(
            "synthetic_partial_launch", cleanup_required=True, cleanup_proven=False
        )), clock, FakeIds(),
    ).assign(bound)
    # Synthetic conflicting policy, never a live record mutation.
    store.connection.execute(
        "UPDATE cleanup_obligations SET required_policy='pr_ci:completed:v1' WHERE task_id=?",
        (bound.task_id,),
    )
    before = store.export_bytes(format_name="json", purpose="rollback", max_records=1000)
    try:
        store.block_assignment(bound.assignment_id, pending["version"], "synthetic_partial_launch", False, True, clock.now())
    except StorageRefusal as exc:
        assert exc.code == "cleanup_conflict"
    else:
        raise AssertionError("foreign cleanup policy allowed reservation release")
    assert store.export_bytes(format_name="json", purpose="rollback", max_records=1000) == before
    store.close()


def _failed_launch_fixture(root: Path, name: str, phase: str):
    store, clock = champion_context(root, name)
    bound = issue_bound_spec(store, spec("claim-r3", suffix=name), clock.now())
    result = AssignmentService(
        store, FakeLaunchAdapter(failure=LaunchAdapterError(
            "synthetic_partial_launch", cleanup_required=True, cleanup_proven=False
        )), clock, FakeIds(),
    ).assign(bound)
    if phase != "pending":
        result = store.block_assignment(
            bound.assignment_id, result["version"], "synthetic_partial_launch", False, True, clock.now()
        )
    if phase == "legacy":
        # Reconstruct the old writer's omission in this isolated fixture.
        store.connection.execute("UPDATE task_assignments SET cleanup_receipt=NULL WHERE task_assignment_id=?", (bound.assignment_id,))
        store.connection.execute("UPDATE cleanup_obligations SET cleanup_state='pending',version=1 WHERE task_id=?", (bound.task_id,))
    return store, clock, bound, result["version"]


def test_failed_launch_settlement_guards_all_entry_states(root: Path) -> None:
    cases = {
        "unproven": "cleanup_unproven",
        "stale": "assignment_conflict",
        "failure_changed": "receipt_conflict",
        "foreign_policy": "cleanup_conflict",
        "foreign_owner": "cleanup_owner_refused",
        "runtime": "cleanup_unproven",
        "task_owned": "cleanup_unproven",
        "shared_lease": "cleanup_unproven",
        "persistent_retain": "cleanup_unproven",
        "planned": "cleanup_conflict",
        "executing": "cleanup_conflict",
        "blocked": "cleanup_conflict",
        "completed": "cleanup_conflict",
    }
    for phase in ("pending", "legacy", "settled"):
        for case, code in cases.items():
            store, clock, bound, version = _failed_launch_fixture(root, f"guard-{phase}-{case}", phase)
            if case == "foreign_policy":
                store.connection.execute("UPDATE cleanup_obligations SET required_policy='pr_ci:completed:v1' WHERE task_id=?", (bound.task_id,))
            elif case == "foreign_owner":
                store.connection.execute("UPDATE tasks SET current_owner_agent_id=? WHERE task_id=?", (SHOTCALLER_ID, bound.task_id))
            elif case == "runtime":
                store.connection.execute("UPDATE task_assignments SET runtime_instance_id=? WHERE task_assignment_id=?", (GAREN_RUNTIME, bound.assignment_id))
            elif case in {"task_owned", "shared_lease", "persistent_retain"}:
                action, adapter = {
                    "task_owned": ("terminate", "process"),
                    "shared_lease": ("release_lease", "lease"),
                    "persistent_retain": ("retain", "retain"),
                }[case]
                store.register_task_resource({
                    "resource_id": f"resource:{bound.task_id}", "task_id": bound.task_id,
                    "owner_id": LUX_ID, "owner_role": "champion", "resource_type": "synthetic",
                    "lifetime": case, "expected_identity": {"id": "synthetic:resource"},
                    "cleanup_action": action, "adapter_kind": adapter,
                    "applicable": True, "applicability_reason": "synthetic guard fixture",
                }, clock.now())
            elif case in {"planned", "executing", "blocked", "completed"}:
                store.connection.execute(
                    """INSERT INTO cleanup_operations
                       (operation_id,cleanup_obligation_id,cleanup_revision,plan_digest,state,fence,created_at,updated_at)
                       VALUES(?,?,1,?,?,1,?,?)""",
                    (f"operation:{bound.task_id}", f"cleanup:{bound.task_id}", "a" * 64, case, clock.now(), clock.now()),
                )
            before = store.export_bytes(format_name="json", purpose="rollback", max_records=1000)
            try:
                store.block_assignment(
                    bound.assignment_id, version - 2 if case == "stale" else version,
                    "different_failure" if case == "failure_changed" else "synthetic_partial_launch",
                    False, case != "unproven", clock.now(),
                )
            except StorageRefusal as exc:
                assert exc.code == code, (phase, case, exc.code)
            else:
                raise AssertionError(f"{phase}/{case} unexpectedly settled")
            assert store.export_bytes(format_name="json", purpose="rollback", max_records=1000) == before, (phase, case)
            store.close()


def test_failed_launch_settlement_write_faults_roll_back(root: Path) -> None:
    for phase, boundaries in (
        ("pending", ((sqlite3.SQLITE_UPDATE, "tasks", "state"),
                     (sqlite3.SQLITE_UPDATE, "callsign_queue", "state"),
                     (sqlite3.SQLITE_UPDATE, "agent_instances", "retired_at"),
                     (sqlite3.SQLITE_INSERT, "events", None),
                     (sqlite3.SQLITE_UPDATE, "cleanup_obligations", "cleanup_state"),
                     (sqlite3.SQLITE_UPDATE, "task_assignments", "cleanup_receipt"))),
        ("legacy", ((sqlite3.SQLITE_UPDATE, "cleanup_obligations", "cleanup_state"),
                    (sqlite3.SQLITE_UPDATE, "task_assignments", "cleanup_receipt"),
                    (sqlite3.SQLITE_UPDATE, "task_assignments", "version"))),
    ):
        for index, boundary in enumerate(boundaries):
            store, clock, bound, version = _failed_launch_fixture(root, f"fault-{phase}-{index}", phase)
            before = store.export_bytes(format_name="json", purpose="rollback", max_records=1000)
            denied = []
            def inject(action, table, column, *_):
                if (action, table, column) == boundary:
                    denied.append(boundary)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            store.connection.set_authorizer(inject)
            try:
                store.block_assignment(bound.assignment_id, version, "synthetic_partial_launch", False, True, clock.now())
            except StorageRefusal as exc:
                assert exc.code == "database_error", exc.code
            else:
                raise AssertionError(f"write fault {boundary} did not roll back")
            finally:
                store.connection.set_authorizer(None)
            assert denied, boundary
            assert store.export_bytes(format_name="json", purpose="rollback", max_records=1000) == before
            store.close()


def test_failed_launch_recovery_refuses_missing_or_changed_receipts(root: Path) -> None:
    for case in ("rollback_digest", "missing_obligation", "settled_missing_obligation", "missing_failure", "settlement_digest", "unretired", "acceptance"):
        phase = "settled" if case == "settled_missing_obligation" else "legacy"
        store, clock, bound, version = _failed_launch_fixture(root, f"receipt-{case}", phase)
        if case == "rollback_digest":
            store.connection.execute("UPDATE callsign_assignments SET failure_receipt_digest=? WHERE scope_id=?", ("b" * 64, bound.task_id))
        elif case in {"missing_obligation", "settled_missing_obligation"}:
            store.connection.execute("DELETE FROM cleanup_obligations WHERE task_id=?", (bound.task_id,))
        elif case == "missing_failure":
            store.connection.execute("UPDATE task_assignments SET failure_class=NULL WHERE task_assignment_id=?", (bound.assignment_id,))
        elif case == "settlement_digest":
            store.connection.execute("UPDATE task_assignments SET cleanup_receipt=? WHERE task_assignment_id=?", ("b" * 64, bound.assignment_id))
        elif case == "unretired":
            store.connection.execute("UPDATE agent_instances SET retired_at=NULL WHERE agent_id=?", (LUX_ID,))
        else:
            store.connection.execute("UPDATE task_assignments SET acceptance_receipt_json='{}' WHERE task_assignment_id=?", (bound.assignment_id,))
        before = store.export_bytes(format_name="json", purpose="rollback", max_records=1000)
        try:
            store.block_assignment(bound.assignment_id, version, "synthetic_partial_launch", False, True, clock.now())
        except StorageRefusal as exc:
            assert exc.code == ("cleanup_unproven" if case == "acceptance" else "receipt_conflict")
        else:
            raise AssertionError(f"{case} allowed fabricated recovery")
        assert store.export_bytes(format_name="json", purpose="rollback", max_records=1000) == before
        store.close()


def test_failed_launch_recovery_cli_and_concurrent_retry(root: Path) -> None:
    store, clock, bound, version = _failed_launch_fixture(root, "concurrent-settle", "legacy")
    # The fixture's public state root is the only database opened by competitors.
    state = root / "concurrent-settle" / "state"
    store.close()
    gate = Barrier(2)
    def settle():
        with SQLiteStorage(state, allow_create=False) as contender:
            gate.wait(timeout=5)
            return contender.block_assignment(bound.assignment_id, version, "synthetic_partial_launch", False, True, clock.now())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(settle)
        second = pool.submit(settle)
        results = [first.result(), second.result()]
    assert sorted(r["idempotent"] for r in results) == [False, True]
    assert {r["version"] for r in results} == {version + 1}
    result = invoke_cli(state, "assign", "block", "--assignment-id", bound.assignment_id,
                        "--expected-version", str(version), "--failure-class", "synthetic_partial_launch",
                        "--cleanup-proven", "--at", clock.now())
    assert result["result"]["idempotent"] is True


def test_assignment_retry_compares_complete_launch_identity(root: Path) -> None:
    store, clock = champion_context(root, "assignment-retry-identity")
    base = issue_bound_spec(store, spec("claim-r3", suffix="identity"), clock.now())
    command = PrepareAssignmentCommand(
        **{
            key: value
            for key, value in vars(base).items()
            if key not in {"callsign", "routing_name", "launch_operation_id"}
        },
        at=clock.now(),
    )
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
    bound = issue_bound_spec(store, spec("claim-r3", suffix="matrix"), clock.now())
    active = AssignmentService(store, FakeLaunchAdapter(), clock, FakeIds()).assign(bound)
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
    runtime_seen_before = store.connection.execute(
        "SELECT last_seen_at FROM runtime_instances WHERE runtime_instance_id=?",
        (active["runtime_instance_id"],),
    ).fetchone()[0]
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
    runtime_seen_after = store.connection.execute(
        "SELECT last_seen_at FROM runtime_instances WHERE runtime_instance_id=?",
        (active["runtime_instance_id"],),
    ).fetchone()[0]
    assert runtime_seen_after == runtime_seen_before
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
        test_durable_work_kinds_cannot_hide_implementation_ownership(root)
        test_cli_prepare_cannot_bypass_owner_issue_verification(root)
        test_exact_receipt_activation_and_atomic_rollback(root)
        test_canonical_task_semantics_refuse_self_matching_unrelated_issue(root)
        test_receipt_mismatch_creates_cleanup_pending(root)
        test_partial_launch_preserves_cleanup_pending(root)
        test_proven_failed_launch_settles_only_cleanup(root)
        test_failed_launch_settlement_retry_is_exact(root)
        test_already_blocked_failed_launch_recovers_exact_receipt(root)
        test_failed_launch_foreign_policy_refuses_atomically(root)
        test_failed_launch_settlement_guards_all_entry_states(root)
        test_failed_launch_settlement_write_faults_roll_back(root)
        test_failed_launch_recovery_refuses_missing_or_changed_receipts(root)
        test_failed_launch_recovery_cli_and_concurrent_retry(root)
        test_unwrapped_adapter_failure_cannot_strand_launching(root)
        test_assignment_retry_compares_complete_launch_identity(root)
        test_task_transition_matrix_refuses_illegal_and_terminal_progression(root)
    print("PASS: explicit dispatch, exact assignment identity, verified receipt, and all launch failures recover through cleanup-pending")


if __name__ == "__main__":
    main()
