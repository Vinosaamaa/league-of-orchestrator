#!/usr/bin/env python3
"""Retained frozen membership after supported Champion runtime replacement."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.rollover_snapshot import RolloverSnapshotRefreshService
from league.cleanup import CleanupAdapterRegistry, CleanupExecutor, CleanupPlanner
from league.real_cleanup import CallsignAdapter
from test_runtime_replacement import (
    FakeMultiplexer, InitialLaunch, active_fixture, replacement_spec, service_fixture,
)
from league.request_services import AssignmentService, AssignmentSpec
from league.storage import StorageRefusal
from lifecycle_fakes import issue_bound_spec
from request_lifecycle_fixture import GAREN_RUNTIME, dispatch_request
from storage_fixture import SHOTCALLER_ID
from test_issue_continuation import Ids
from test_issue_continuation import (
    _prepare_fixture, _continuation_spec, StateCleanupAdapter, RuntimeCloseAdapter,
    FakeIssueAdapter, ContinuationIssueReopener, ExactThreadLaunchAdapter,
    AT_RETRY, LEASE_RETRY, AT_PLAN, AT_EXECUTE, LEASE, REPOSITORY, THREAD_ID,
)
from test_shotcaller_rollover import (
    OLD_ID, NEW_ID, SQUAD_ID, ExactSnapshotInventory,
    switch_rollover, runtime_receipt, OLD_ASSIGNMENT, NEW_ASSIGNMENT, AT1,
    plan, acknowledge, read_all_pages,
)


class InitialWithTerminal(InitialLaunch):
    def launch(self, spec):
        result = super().launch(spec)
        terminal = f"terminal:{spec.champion_agent_id}"
        result["runtime_generation"] = "herdr:" + hashlib.sha256(
            f"{terminal}\0{result['thread_id']}".encode()
        ).hexdigest()[:24]
        return result


def shotcallers(store, *, existing_squad=None):
    pool = store.callsign_status("shotcaller")
    catalog = [dict(callsign=e["callsign"], enabled=e["enabled"], capabilities=[]) for e in pool["entries"]]
    catalog += [dict(callsign=n, enabled=True, capabilities=["rollover.accept"]) for n in ("RolloverOld", "RolloverNext")]
    store.reconcile_callsign_pool("shotcaller", pool["queue_version"], pool["seed"], pool["shuffle_version"], catalog, AT1)
    if existing_squad is None:
        old = store.allocate_callsign(OLD_ASSIGNMENT, OLD_ID, "shotcaller", "squad", SQUAD_ID, ["rollover.accept"], AT1)
        store.activate_callsign(OLD_ASSIGNMENT, 1, runtime_receipt(old, "old-shotcaller", ["rollover.accept"]), AT1)
        store.connection.execute("INSERT INTO squads(squad_id,shotcaller_agent_id,state,version,updated_at,owner_fence) VALUES(?,?,'active',1,?,1)", (SQUAD_ID, OLD_ID, AT1))
        store.connection.execute("INSERT INTO shotcaller_intake(agent_id,squad_id,state,fence,version,updated_at) VALUES(?,?,'accepting',1,1,?)", (OLD_ID, SQUAD_ID, AT1))
    successor = store.allocate_callsign(NEW_ASSIGNMENT, NEW_ID, "shotcaller", "squad", existing_squad or SQUAD_ID, ["rollover.accept"], AT1)
    return {"successor": successor}


def fixture(root: Path, *, retired_count=1, live_count=0, failed_attempt=False):
    # AssignmentService produces real issue/assignment/runtime acceptance.
    store, clock, assignment, agent, task, worktree = active_fixture(root, "codex", "codex")
    context = shotcallers(store)
    originals = [(assignment, agent, task, worktree)]
    for index in range(1, retired_count + live_count):
        request_id = f"R{index}"
        store.claim_request(request_id, GAREN_RUNTIME, f"claim-extra-{index}", clock.after(120), clock.now())
        dispatch_request(store, clock, request_id, f"claim-extra-{index}", f"dispatch-extra-{index}", "repository-write", "champion")
        extra_worktree = root / f"extra-worktree-{index}"
        extra_worktree.mkdir()
        extra = AssignmentSpec(assignment_id=f"assignment:extra:{index}", request_id=request_id,
            claim_token=f"claim-extra-{index}", task_id=f"task:extra:{index}", task_summary=f"Extra synthetic original {index}",
            coordinator_agent_id=SHOTCALLER_ID, champion_agent_id=f"agent:extra:{index}", repository="https://example.invalid/league.git",
            issue=84 + index, branch=f"agent/synthetic/extra-{index}", worktree=str(extra_worktree), issue_receipt=None)
        assigned = AssignmentService(store, InitialWithTerminal("codex", "codex"), clock, Ids(f"extra:{index}")).assign(issue_bound_spec(store, extra, clock.now()))
        assert assigned["state"] == "active", assigned
        originals.append((dict(store.connection.execute("SELECT * FROM task_assignments WHERE task_assignment_id=?", (extra.assignment_id,)).fetchone()),
            dict(store.connection.execute("SELECT * FROM agent_instances WHERE agent_id=?", (extra.champion_agent_id,)).fetchone()),
            dict(store.connection.execute("SELECT * FROM tasks WHERE task_id=?", (extra.task_id,)).fetchone()), extra_worktree))
    for original_assignment, original_agent, original_task, _ in originals:
        store.connection.execute("UPDATE agent_instances SET shotcaller_agent_id=? WHERE agent_id=?", (OLD_ID, original_agent["agent_id"]))
        store.connection.execute("UPDATE tasks SET coordinator_agent_id=? WHERE task_id=?", (OLD_ID, original_task["task_id"]))
        store.connection.execute("UPDATE task_assignments SET coordinator_agent_id=? WHERE task_assignment_id=?", (OLD_ID, original_assignment["task_assignment_id"]))
        store.connection.execute("INSERT INTO squad_champions(squad_id,champion_agent_id,joined_at) VALUES(?,?,?)", (SQUAD_ID, original_agent["agent_id"], clock.now()))
    context["champion_ids"] = [item[1]["agent_id"] for item in originals]
    prepared, switched = switch_rollover(store, context)
    assignment = dict(store.connection.execute("SELECT * FROM task_assignments WHERE task_assignment_id=?", (assignment["task_assignment_id"],)).fetchone())
    agent = dict(store.connection.execute("SELECT * FROM agent_instances WHERE agent_id=?", (agent["agent_id"],)).fetchone())
    task = dict(store.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone())
    specs = []
    for index, (original_assignment, original_agent, original_task, original_worktree) in enumerate(originals[:retired_count]):
        original_assignment["coordinator_agent_id"] = OLD_ID
        original_agent["shotcaller_agent_id"] = OLD_ID
        original_task["coordinator_agent_id"] = OLD_ID
        runtime = store.connection.execute("SELECT * FROM runtime_instances WHERE runtime_instance_id=?", (original_assignment["runtime_instance_id"],)).fetchone()
        mux = FakeMultiplexer()
        mux.native[original_agent["agent_id"]] = dict(agent_id=original_agent["agent_id"], runtime_instance_id=runtime["runtime_instance_id"], session_ref=runtime["session_ref"], endpoint=runtime["endpoint"], runtime_generation=runtime["runtime_generation"], cwd=original_agent["worktree"], routing_name=original_agent["routing_name"], provider_kind="codex", adapter_kind="codex")
        service, *_ = service_fixture(store, clock, mux)
        if failed_attempt:
            failed = replacement_spec(original_assignment, original_agent, original_task,
                original_worktree, "codex", "codex", f"failed-original-{index}")
            mux.fail_retirement = True
            rolled_back = service.replace(failed)
            assert rolled_back["state"] == "rolled_back", rolled_back
            assert mux.route_rollbacks == 1
            assert failed.request["successor_agent_id"] not in mux.native
            assert mux.native[original_agent["agent_id"]]["routing_name"] == original_agent["routing_name"]
            mux.fail_retirement = False
            original_assignment = dict(store.connection.execute("SELECT * FROM task_assignments WHERE task_assignment_id=?",
                (original_assignment["task_assignment_id"],)).fetchone())
            original_agent = dict(store.connection.execute("SELECT * FROM agent_instances WHERE agent_id=?",
                (original_agent["agent_id"],)).fetchone())
            original_task = dict(store.connection.execute("SELECT * FROM tasks WHERE task_id=?",
                (original_task["task_id"],)).fetchone())
        spec = replacement_spec(original_assignment, original_agent, original_task, original_worktree, "codex", "codex", f"retired-original-{index}")
        result = service.replace(spec)
        assert result["state"] == "completed", result
        specs.append(spec)
    spec = specs[0]
    return store, prepared, switched, agent, spec


def test_completed_replacement_preserves_frozen_original(root: Path) -> None:
    store, prepared, switched, agent, spec = fixture(root)
    try:
        identities = protected_identity(store)
        result = RolloverSnapshotRefreshService(store, ExactSnapshotInventory()).refresh(
            operation_id=prepared["operation_id"], refresh_id="refresh:retired-original",
            squad_id=SQUAD_ID, predecessor_agent_id=OLD_ID, successor_agent_id=NEW_ID,
            expected_rollover_version=switched["version"],
            expected_snapshot_version=prepared["snapshot"]["version"],
            expected_snapshot_digest=prepared["snapshot"]["digest"],
            expires_at="2026-01-01T03:00:00Z", at="2026-01-01T02:00:00Z",
        )
        assert result["descendant_count"] == 1
        assert result["progress_bindings"][0]["state"] == "retired_handoff_satisfied"
        assert result["progress_bindings"][0]["champion_agent_id"] == agent["agent_id"]
        assert store.agent_status(agent["agent_id"])["retired_at"] is not None
        page = store.rollover_bindings(prepared["operation_id"], "2026-01-01T02:01:00Z")
        assert page["snapshot_count"] == 1
        assert page["terminal_markers"] == result["progress_bindings"]
        assert protected_identity(store) == identities
    finally:
        store.close()


def test_failed_then_completed_replacement(root):
    store, prepared, switched, agent, _ = fixture(root, failed_attempt=True)
    try:
        identities = protected_identity(store)
        inputs = refresh_inputs(prepared, switched)
        service = RolloverSnapshotRefreshService(store, ExactSnapshotInventory())
        result = service.refresh(**inputs)
        marker = result["progress_bindings"][0]
        assert marker["state"] == "retired_handoff_satisfied"
        assert marker["champion_agent_id"] == agent["agent_id"]
        assert marker["reconciliation_id"] == "replacement:retired-original-0"
        assert protected_identity(store) == identities
        before = dump(store)
        assert service.refresh(**inputs)["idempotent"]
        assert dump(store) == before
    finally:
        store.close()


def test_competing_replacement_still_refuses(root):
    for state in ("completed", "recovery_required"):
        case = root / state
        case.mkdir()
        store, prepared, switched, *_ = fixture(case, failed_attempt=True)
        try:
            # Start with genuine compensated/successful producer receipts, then
            # adversarially claim the prior attempt remains a competing handoff.
            store.connection.execute("UPDATE runtime_replacements SET state=? WHERE operation_id='replacement:failed-original-0'", (state,))
            before = dump(store)
            try:
                store.rollover_snapshot_refresh_target(**refresh_inputs(prepared, switched))
            except StorageRefusal as exc:
                assert exc.code == "snapshot_terminal_proof_invalid", exc.code
            else:
                raise AssertionError(state)
            assert dump(store) == before
        finally:
            store.close()


def refresh_inputs(prepared, switched):
    return dict(operation_id=prepared["operation_id"], refresh_id="refresh:retired-original",
        squad_id=prepared["squad_id"], predecessor_agent_id=prepared["predecessor_agent_id"], successor_agent_id=NEW_ID,
        expected_rollover_version=switched["version"], expected_snapshot_version=prepared["snapshot"]["version"],
        expected_snapshot_digest=prepared["snapshot"]["digest"], expires_at="2026-01-01T03:00:00Z", at="2026-01-01T02:00:00Z")


def continuation_fixture(root):
    name = "terminal-continuation"
    store, clock, original, manifest = _prepare_fixture(root, name)
    squad_id = "squad:Garen"
    context = shotcallers(store, existing_squad=squad_id)
    # Freeze the original's real issuing Shotcaller; retain issue-selection and
    # acceptance coordinator bindings exactly as their producers wrote them.
    store.connection.execute("DELETE FROM squad_champions WHERE squad_id=?", (squad_id,))
    store.connection.execute("INSERT OR IGNORE INTO shotcaller_intake(agent_id,squad_id,state,fence,version,updated_at) VALUES(?,?,'accepting',1,1,?)", (SHOTCALLER_ID, squad_id, AT1))
    store.connection.execute("INSERT INTO squad_champions(squad_id,champion_agent_id,joined_at) VALUES(?,?,?)", (squad_id, original.champion_agent_id, clock.now()))
    context["champion_ids"] = [original.champion_agent_id]
    prepared = store.prepare_rollover("rollover:synthetic", squad_id, SHOTCALLER_ID, NEW_ID,
        NEW_ASSIGNMENT, 1, 1, "explicit", "authority-receipt-digest", ["rollover.accept"], {**plan(), "scope": {"kind": "squad", "id": squad_id}}, "2026-01-01T00:02:00Z")
    store.activate_callsign(NEW_ASSIGNMENT, 1, runtime_receipt(context["successor"], "new-shotcaller", ["rollover.accept"]), "2026-01-01T00:03:00Z")
    acknowledge(store, prepared, read_all_pages(store, prepared["operation_id"]))
    store.release_request_claim("R3", GAREN_RUNTIME, "claim-r3", clock.now())
    switched = store.commit_rollover(prepared["operation_id"], 1, 1, "event:owner-changed:continuation", "outbox:owner-changed:continuation", "2026-01-01T00:04:00Z")
    issue = FakeIssueAdapter({"value": "open"})
    execute_continuation_cleanup(store, manifest, issue)
    continuation = _continuation_spec(root, name, manifest["continuation_archive"]["archive_id"])
    store.prepare_continuation(continuation)
    reopened = ContinuationIssueReopener(store, issue).execute(continuation["operation_id"], expected_version=1,
        expected_fence=0, executor_id="executor:reopen", leased_until=LEASE_RETRY, at=AT_RETRY)
    store.mark_continuation_launching(continuation["operation_id"], reopened["version"], AT_RETRY)
    store.claim_request("R2", GAREN_RUNTIME, "claim-r2", clock.after(120), clock.now())
    dispatch_request(store, clock, "R2", "claim-r2", "dispatch-r2", "repository-write", "champion")
    successor = AssignmentSpec(assignment_id=continuation["assignment_id"], request_id="R2", claim_token="claim-r2",
        task_id=continuation["new_task_id"], task_summary="Exact continued original",
        coordinator_agent_id=SHOTCALLER_ID, champion_agent_id=continuation["new_agent_id"], repository=REPOSITORY,
        issue=83, branch=continuation["branch"], worktree=continuation["worktree"], issue_receipt=None)
    active = AssignmentService(store, ExactThreadLaunchAdapter(THREAD_ID), clock, Ids("terminal-continuation:successor")).assign(
        issue_bound_spec(store, successor, clock.now(), repository=REPOSITORY))
    assert active["state"] == "active", active
    return store, prepared, switched, original


def execute_continuation_cleanup(store, manifest, issue):
    operation_id = "cleanup:terminal-continuation"
    release = next(a for a in manifest["final_actions"] if a["action_kind"] == "callsign_release")
    assignment = store.connection.execute("SELECT * FROM callsign_assignments WHERE agent_id=?",
        (manifest["owner"]["id"],)).fetchone()
    release["expected_identity"] = dict(assignment_id=assignment["callsign_assignment_id"],
        callsign=assignment["callsign"], expected_version=assignment["version"])
    release["intended_state"] = {**release["expected_identity"], "state": "released"}
    planned = CleanupPlanner(store).plan(manifest, operation_id=operation_id, at=AT_PLAN)
    operation = store.cleanup_operation(operation_id)
    states = {a["action_id"]: dict(a["expected_identity"]) for a in operation["actions"]}
    effects = []
    registry = CleanupAdapterRegistry()
    for kind in ("archive", "harness", "git"):
        registry.register(StateCleanupAdapter(kind, states, effects))
    runtime = store.connection.execute("SELECT * FROM runtime_instances WHERE runtime_instance_id=?",
        (manifest["continuation_archive"]["runtime_instance_id"],)).fetchone()
    registry.register(RuntimeCloseAdapter(store, states, effects, dict(runtime)))
    callsign = next(a for a in operation["actions"] if a["adapter_kind"] == "callsign")
    # Callsign release is a repository-local effect, not an external adapter:
    # use the production adapter and its immutable action-plan digest.
    registry.register(CallsignAdapter(store, callsign["expected_identity"], AT_EXECUTE))
    registry.register(issue)
    return CleanupExecutor(store, registry).execute(operation_id, expected_fence=planned["fence"],
        executor_id="executor:terminal-cleanup", leased_until=LEASE, at=AT_EXECUTE)


def test_supported_continuation_terminal_proof(root):
    store, prepared, switched, original = continuation_fixture(root)
    try:
        before = store.thread_archive("archive:terminal-continuation:original")
        identities = protected_identity(store)
        result = RolloverSnapshotRefreshService(store, ExactSnapshotInventory()).refresh(**refresh_inputs(prepared, switched))
        assert result["progress_bindings"][0]["state"] == "retired_handoff_satisfied"
        assert result["progress_bindings"][0]["champion_agent_id"] == original.champion_agent_id
        assert store.thread_archive("archive:terminal-continuation:original") == before
        assert protected_identity(store) == identities
    finally:
        store.close()


def dump(store):
    return "\n".join(store.connection.iterdump())


def protected_identity(store):
    # Only the rollover snapshot/receipt may change; lifecycle producers retain
    # exclusive authority over every historical/current runtime and assignment.
    return {table: [tuple(r) for r in store.connection.execute(f"SELECT * FROM {table}")]
        for table in ("agent_instances", "runtime_instances", "tasks", "task_assignments",
            "callsign_assignments", "squad_champions", "thread_archives", "thread_lineages",
            "thread_incarnations", "continuation_operations", "runtime_replacements", "delivery_outbox")}


def test_two_retired_mixed_and_retry(root):
    store, prepared, switched, agent, spec = fixture(root, retired_count=2, live_count=1)
    try:
        original = store.rollover_bindings(prepared["operation_id"], "2026-01-01T00:10:00Z")["page"]["rows"]
        inputs = refresh_inputs(prepared, switched)
        service = RolloverSnapshotRefreshService(store, ExactSnapshotInventory())
        first = service.refresh(**inputs)
        before_retry = dump(store)
        second = service.refresh(**inputs)
        assert dump(store) == before_retry
        assert second["receipt_digest"] == first["receipt_digest"] and second["idempotent"]
        assert [p["state"] for p in first["progress_bindings"]].count("retired_handoff_satisfied") == 2
        assert [p["state"] for p in first["progress_bindings"]].count("predecessor_pending") == 1
        page = store.rollover_bindings(prepared["operation_id"], "2026-01-01T02:01:00Z")
        assert [(r["champion_agent_id"], r["task_id"], r["callsign"]) for r in page["page"]["rows"]] == [(r["champion_agent_id"], r["task_id"], r["callsign"]) for r in original]
        old_rows = {r["champion_agent_id"]: r for r in original}
        for row in page["page"]["rows"]:
            if row["champion_agent_id"] in {p["champion_agent_id"] for p in page["terminal_markers"]}:
                assert row["binding_digest"] == old_rows[row["champion_agent_id"]]["binding_digest"]
        # Another expiry preserves the same original members, never successors.
        later = dict(inputs, refresh_id="refresh:second-expiry", expected_rollover_version=first["rollover_version"],
            expected_snapshot_version=first["snapshot"]["version"], expected_snapshot_digest=first["snapshot"]["digest"],
            at="2026-01-01T04:00:00Z", expires_at="2026-01-01T05:00:00Z")
        assert service.refresh(**later)["descendant_count"] == 3
    finally:
        store.close()


def test_tamper_and_stale_refuse_without_writes(root):
    cases = (
        ("replacement-runtime", "UPDATE runtime_replacements SET predecessor_runtime_instance_id='runtime:garen:one'", ()),
        ("intent", "UPDATE runtime_replacements SET intent_digest=?", ("0" * 64,)),
        ("completion", "UPDATE runtime_replacements SET completion_receipt_json='{}'", ()),
        ("completed-rollback", "UPDATE runtime_replacements SET rollback_receipt_json='{}'", ()),
        ("retirement", "UPDATE runtime_replacements SET retirement_receipt_json='{}'", ()),
        ("incomplete", "UPDATE runtime_replacements SET state='retiring'", ()),
        ("old-generation", "UPDATE runtime_instances SET runtime_generation='foreign' WHERE actor_agent_id=?", ("55555555-5555-4555-8555-555555555555",)),
        ("old-version", "UPDATE agent_instances SET version=version+1 WHERE agent_id=?", ("55555555-5555-4555-8555-555555555555",)),
        ("successor-cwd", "UPDATE agent_instances SET worktree='foreign' WHERE agent_id='successor-retired-original-0'", ()),
        ("acceptance", "UPDATE task_assignments SET acceptance_receipt_json='{}' WHERE task_assignment_id='assignment:replacement'", ()),
        ("callsign", "UPDATE callsign_assignments SET acceptance_digest=? WHERE callsign_assignment_id='callsign-assignment:assignment:replacement'", ("0" * 64,)),
    )
    for label, sql, args in cases:
        case = root / label
        case.mkdir()
        store, prepared, switched, *_ = fixture(case)
        try:
            store.connection.execute(sql, args)
            before = dump(store)
            try:
                RolloverSnapshotRefreshService(store, ExactSnapshotInventory()).refresh(**refresh_inputs(prepared, switched))
            except StorageRefusal as exc:
                assert exc.code == "snapshot_terminal_proof_invalid", (label, exc.code)
            else:
                raise AssertionError(label)
            assert dump(store) == before
        finally:
            store.close()


def test_terminal_cas_and_rollback(root):
    store, prepared, switched, *_ = fixture(root, retired_count=2)
    try:
        inputs = refresh_inputs(prepared, switched)
        target = store.rollover_snapshot_refresh_target(**inputs)
        store.connection.execute("UPDATE runtime_replacements SET version=version+1")
        before = dump(store)
        try:
            store.refresh_rollover_snapshot(**inputs, canonical_digest=target["canonical_digest"], observations=[], final_observer=lambda _: [])
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_concurrent_mutation", exc.code
        else:
            raise AssertionError("stale terminal preflight accepted")
        assert dump(store) == before
        target = store.rollover_snapshot_refresh_target(**inputs)
        def change_during_observation(_):
            store.connection.execute("UPDATE runtime_replacements SET version=version+1")
            return []
        try:
            store.refresh_rollover_snapshot(**inputs, canonical_digest=target["canonical_digest"],
                observations=[], final_observer=change_during_observation)
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_concurrent_mutation", exc.code
        else:
            raise AssertionError("terminal proof changed during final observation")
        assert dump(store) == before
        for point in ("after_refresh_operation_cas", "after_refresh_snapshot", "after_refresh_rows"):
            target = store.rollover_snapshot_refresh_target(**inputs)
            def crash(stage):
                if stage == point:
                    raise RuntimeError("synthetic crash")
            try:
                store.refresh_rollover_snapshot(**inputs, canonical_digest=target["canonical_digest"], observations=[], final_observer=lambda _: [], fault=crash)
            except RuntimeError as exc:
                assert str(exc) == "synthetic crash"
            else:
                raise AssertionError(point)
            assert dump(store) == before
        assert RolloverSnapshotRefreshService(store, ExactSnapshotInventory()).refresh(**inputs)["descendant_count"] == 2
    finally:
        store.close()


def test_continuation_tamper_refuses(root):
    cases = (
        ("cleanup", "UPDATE cleanup_operations SET state='executing'"),
        ("close", "UPDATE cleanup_action_receipts SET after_json='{}'"),
        ("teardown", "UPDATE teardown_receipts SET receipt_hash='foreign'"),
        ("reopen", "UPDATE continuation_operations SET issue_receipt_json='{}'"),
        ("resume", "UPDATE continuation_operations SET state='launching'"),
        ("thread", "UPDATE thread_lineages SET thread_identity='codex:00000000-0000-4000-8000-000000000000'"),
        ("acceptance", "UPDATE task_assignments SET acceptance_receipt_json='{}'"),
        ("capability", "UPDATE thread_lineages SET resume_capabilities_json='[]'"),
        ("incarnation", "DELETE FROM thread_incarnations WHERE continuation_operation_id IS NOT NULL"),
        ("release", "UPDATE callsign_assignments SET release_receipt_digest=NULL WHERE state='released'"),
        ("foreign-release", "UPDATE callsign_assignments SET release_receipt_digest='foreign' WHERE state='released'"),
        ("frozen-row", "UPDATE active_champion_snapshot_rows SET row_digest='foreign'"),
    )
    for label, sql in cases:
        case = root / label
        case.mkdir()
        store, prepared, switched, _ = continuation_fixture(case)
        try:
            store.connection.execute(sql)
            before = dump(store)
            try:
                RolloverSnapshotRefreshService(store, ExactSnapshotInventory()).refresh(**refresh_inputs(prepared, switched))
            except StorageRefusal:
                pass
            else:
                raise AssertionError(label)
            assert dump(store) == before
        finally:
            store.close()


def test_stale_scope_and_retired_reconcile_refuse(root):
    store, prepared, switched, agent, _ = fixture(root, live_count=1)
    try:
        inputs = refresh_inputs(prepared, switched)
        for changed in ({"expected_rollover_version": 1}, {"expected_snapshot_version": 2},
                        {"expected_snapshot_digest": "0" * 64}):
            before = dump(store)
            try:
                store.rollover_snapshot_refresh_target(**dict(inputs, **changed))
            except StorageRefusal:
                pass
            else:
                raise AssertionError(changed)
            assert dump(store) == before
        frozen = store.rollover_bindings(prepared["operation_id"], "2026-01-01T00:10:00Z")["page"]["rows"][0]
        current = store.agent_status(agent["agent_id"])
        before = dump(store)
        try:
            store.rollover_descendant_target(prepared["operation_id"], "reconcile:retired", agent["agent_id"],
                agent["task_id"], prepared["snapshot"]["digest"], frozen["row_digest"], switched["version"],
                current["version"], 1, 1, 1)
        except StorageRefusal as exc:
            assert exc.code == "descendant_identity_stale", exc.code
        else:
            raise AssertionError("retired original offered for live reconciliation")
        assert dump(store) == before
        store.connection.execute("DELETE FROM squad_champions WHERE champion_agent_id='agent:extra:1'")
        before = dump(store)
        try:
            store.rollover_snapshot_refresh_target(**inputs)
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_set_changed", exc.code
        else:
            raise AssertionError("unproven missing live member accepted")
        assert dump(store) == before
    finally:
        store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-retired-rollover-") as directory:
        root = Path(directory)
        for test in (test_completed_replacement_preserves_frozen_original, test_two_retired_mixed_and_retry,
                     test_tamper_and_stale_refuse_without_writes, test_terminal_cas_and_rollback,
                     test_supported_continuation_terminal_proof, test_continuation_tamper_refuses,
                     test_stale_scope_and_retired_reconcile_refuse,
                     test_failed_then_completed_replacement, test_competing_replacement_still_refuses):
            case = root / test.__name__
            case.mkdir()
            test(case)
    print("PASS: retired original rollover membership")


if __name__ == "__main__":
    main()
