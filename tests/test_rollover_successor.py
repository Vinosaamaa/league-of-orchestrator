#!/usr/bin/env python3
"""Focused current-API startup/rollover proof; temporary state, no live providers."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.rollover_service import ShotcallerRolloverRunner
from league.rollover_descendant import RolloverDescendantService
from league.rollover_snapshot import RolloverSnapshotRefreshService
from league.sqlite_store import CURRENT_SCHEMA_VERSION, SQLiteStorage
from league.storage import StorageRefusal
from storage_test_support import invoke_cli, migrated_state
from test_shotcaller_rollover import (
    AT1, AT2, AT3, AT4, AT5, AT6, OLD_ID, NEW_ID, SQUAD_ID,
    plan, runtime_receipt, seed_rollover, read_all_pages, descendant_runtime_receipt,
    ExactSnapshotInventory,
)

def _seed_champion_assignment(store: SQLiteStorage, champion_id: str) -> None:
    task_id = "task:champion:0"
    request_id = "request:champion:0"
    runtime_id = "runtime:champion:0"
    callsign = store.connection.execute(
        "SELECT callsign FROM agent_instances WHERE agent_id=?", (champion_id,)
    ).fetchone()[0]
    store.connection.execute(
        """
        INSERT INTO requests
          (request_id,summary,requester_agent_id,owner_agent_id,return_to_agent_id,
           execution_mode,state,version,created_at,updated_at,route_reason_code,
           route_policy_version,route_confidence)
        VALUES(?, 'Private synthetic request summary', ?, ?, ?, 'champion',
               'in_progress',1,?,?, 'explicit_champion','policy:v1','explicit')
        """,
        (request_id, OLD_ID, champion_id, OLD_ID, AT1, AT1),
    )
    store.connection.execute(
        """
        UPDATE tasks SET request_id=?,coordinator_agent_id=?,champion_agent_id=?,project_id=NULL
         WHERE task_id=?
        """,
        (request_id, OLD_ID, champion_id, task_id),
    )
    store.connection.execute(
        """
        INSERT INTO task_assignments
          (task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
           runtime_instance_id,callsign,assignment_role,state,cleanup_required,version,
           created_at,updated_at)
        VALUES('task-assignment:champion:0',?,?,?,?,?,?,'champion','active',0,1,?,?)
        """,
        (task_id, request_id, OLD_ID, champion_id, runtime_id, callsign, AT1, AT1),
    )
    store.connection.execute(
        """
        INSERT INTO obligations
          (obligation_id,owner_agent_id,kind,aggregate_id,dedupe_key,state,
           created_at,updated_at)
        VALUES('obligation:handoff',?,'coordination',?,'handoff:old-owner','open',?,?)
        """,
        (OLD_ID, SQUAD_ID, AT2, AT2),
    )



def native(store, actor, kind):
    # Fixture materialization, not native launch/acceptance evidence.
    store.connection.execute(
        "UPDATE agent_instances SET kind=?,display_agent=? WHERE agent_id=?",
        (kind + "-thread", kind, actor),
    )
    store.connection.execute(
        "UPDATE runtime_instances SET harness_kind=? WHERE actor_agent_id=?",
        (kind + "-thread", actor),
    )


def setup(root, name, old="codex", new="cursor", *, champions=1, activate=True, bootstrap=False):
    state, _ = migrated_state(root, name)
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=champions, bootstrap_successor=bootstrap)
        native(store, OLD_ID, old)
        if champions:
            _seed_champion_assignment(store, context["champion_ids"][0])
            native(store, context["champion_ids"][0], old)
        if activate:
            store.activate_callsign(
                context["successor"]["assignment_id"], 1,
                runtime_receipt(context["successor"], "new-shotcaller", ["rollover.accept"]), AT3,
            )
            native(store, NEW_ID, new)
    manifest = {
        "schema": "league.shotcaller-rollover-run.v1",
        "operation_id": "rollover:synthetic", "squad_id": SQUAD_ID,
        "predecessor_agent_id": OLD_ID, "successor_agent_id": NEW_ID,
        "predecessor_runtime_instance_id": "runtime:old-shotcaller",
        "successor_runtime_instance_id": "runtime:new-shotcaller",
        "callsign_assignment_id": context["successor"]["assignment_id"],
        "expected_owner_version": 1, "expected_owner_fence": 1,
        "authority_kind": "explicit", "authority_digest": "explicit-test-authority",
        "required_capabilities": ["rollover.accept"], "plan": plan(),
        "owner_event_id": "event:owner-changed", "owner_outbox_id": "outbox:owner-changed",
    }
    return state, manifest, context


def refused(code, call):
    try:
        call()
    except StorageRefusal as exc:
        assert exc.code == code, (code, exc.code)
    else:
        raise AssertionError("expected " + code)


def bindings_identity(store):
    return [
        tuple(row) for row in store.connection.execute(
            "SELECT agent_id,task_id,thread_id,repository,issue,branch,worktree,callsign "
            "FROM agent_instances WHERE role='champion' ORDER BY agent_id"
        )
    ]


def test_native_directions_and_current_owner_boundary(root):
    for old, new in (("codex", "cursor"), ("cursor", "codex")):
        state, manifest, context = setup(root, old + "-to-" + new, old, new, champions=3)
        with SQLiteStorage(state) as store:
            runner = ShotcallerRolloverRunner(store)
            before = bindings_identity(store)
            first = runner.run(manifest, at=AT4)
            assert first["operation"]["state"] == "prepared"
            assert first["bindings"]["page"]["count"] == 2
            assert first["bindings"]["next_cursor"] is not None
            assert store.rollover_status(manifest["operation_id"])["acknowledgement_digest"] is None
            successor = store.startup_context(NEW_ID, "runtime:new-shotcaller", AT4)
            assert successor["permitted_next_actions"] == ["rollover.acknowledge"]
            champion = store.startup_context(context["champion_ids"][0], "runtime:champion:0", AT4)
            assert champion["owning_shotcaller"]["agent_id"] == OLD_ID
            assert champion["runtime"]["harness_kind"] == old + "-thread"
            pages = {"pages": read_all_pages(store, manifest["operation_id"])}
            partial = {"pages": pages["pages"][:1]}
            refused("active_champion_snapshot_incomplete", lambda: runner.run(manifest, at=AT4, pages=partial))
            assert store.rollover_status(manifest["operation_id"])["state"] == "prepared"
            switched = runner.run(manifest, at=AT5, pages=pages)
            assert switched["operation"]["state"] == "switched"
            assert bindings_identity(store) == before
            # Owner commit does not substitute for exact descendant/intake reconciliation.
            assert store.connection.execute(
                "SELECT owner_agent_id FROM obligations WHERE obligation_id='obligation:handoff'"
            ).fetchone()[0] == OLD_ID
            assert store.connection.execute(
                "SELECT coordinator_agent_id FROM task_assignments"
            ).fetchone()[0] == OLD_ID
            refused("startup_owner_unreconciled", lambda: store.startup_context(
                context["champion_ids"][0], "runtime:champion:0", AT5
            ))
            assert "rollover.reconcile-descendant" in switched["next_actions"]
            for _ in range(2):
                assert runner.run(manifest, at=AT5, pages=pages)["operation"]["state"] == "switched"
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='owner_changed'"
            ).fetchone()[0] == 1
            assert store.connection.execute(
                "SELECT COUNT(*) FROM delivery_outbox WHERE event_id='event:owner-changed'"
            ).fetchone()[0] == 1
            assert dict(store.connection.execute(
                "SELECT agent_id,state FROM shotcaller_intake"
            ).fetchall()) == {OLD_ID: "draining", NEW_ID: "accepting"}
            rendered = json.dumps([champion, successor, switched])
            for private in ("/synthetic/worktrees", "example.invalid", "synthetic-endpoint", "Private synthetic"):
                assert private not in rendered
            assert store.connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION


def test_bootstrap_successor_and_late_acceptance(root):
    state, manifest, context = setup(root, "bootstrap", champions=0, bootstrap=True)
    with SQLiteStorage(state) as store:
        runner = ShotcallerRolloverRunner(store)
        runner.run(manifest, at=AT4)
        assert store.startup_context(NEW_ID, "runtime:new-shotcaller", AT4)["identity"]["role"] == "shotcaller"
    state, manifest, context = setup(root, "late-acceptance", champions=0, activate=False)
    with SQLiteStorage(state) as store:
        runner = ShotcallerRolloverRunner(store)
        assert runner.run(manifest, at=AT4)["operation"]["state"] == "prepared"
        store.activate_callsign(
            context["successor"]["assignment_id"], 1,
            runtime_receipt(context["successor"], "new-shotcaller", ["rollover.accept"]), AT4,
        )
        native(store, NEW_ID, "cursor")
        pages = {"pages": read_all_pages(store, manifest["operation_id"])}
        assert runner.run(manifest, at=AT5, pages=pages)["operation"]["state"] == "switched"


def test_context_identity_privacy_and_bound(root):
    state, manifest, context = setup(root, "identity")
    with SQLiteStorage(state) as store:
        runner = ShotcallerRolloverRunner(store)
        runner.run(manifest, at=AT4)
        refused("startup_identity_stale", lambda: store.startup_context(NEW_ID, "runtime:wrong", AT4))
        store.connection.execute("UPDATE runtime_instances SET status='idle' WHERE actor_agent_id=?", (NEW_ID,))
        assert store.startup_context(NEW_ID, "runtime:new-shotcaller", AT4)["verified"]
        store.connection.execute("UPDATE runtime_instances SET endpoint='foreign' WHERE actor_agent_id=?", (NEW_ID,))
        refused("startup_identity_stale", lambda: store.startup_context(NEW_ID, "runtime:new-shotcaller", AT4))
        store.connection.execute(
            "UPDATE runtime_instances SET endpoint='synthetic-endpoint:new-shotcaller',status='active' WHERE actor_agent_id=?", (NEW_ID,)
        )
        store.connection.execute(
            "INSERT INTO runtime_instances (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,"
            "session_ref,endpoint,runtime_generation,status,verified,last_seen_at,capabilities_json) "
            "SELECT 'runtime:duplicate',actor_agent_id,harness_kind,backend_kind,"
            "'synthetic:duplicate',endpoint,'generation:duplicate',status,verified,last_seen_at,capabilities_json "
            "FROM runtime_instances WHERE actor_agent_id=?", (NEW_ID,)
        )
        refused("startup_identity_ambiguous", lambda: store.startup_context(NEW_ID, "runtime:new-shotcaller", AT4))
        store.connection.execute("DELETE FROM runtime_instances WHERE runtime_instance_id='runtime:duplicate'")
        store.connection.execute("UPDATE runtime_instances SET capabilities_json='[]' WHERE actor_agent_id=?", (NEW_ID,))
        refused("startup_capability_invalid", lambda: store.startup_context(NEW_ID, "runtime:new-shotcaller", AT4))
        store.connection.execute(
            "UPDATE runtime_instances SET capabilities_json='[\"rollover.accept\"]' WHERE actor_agent_id=?", (NEW_ID,)
        )
        # Opaque free text is not echoed even if private content reached a historical plan.
        unsafe = plan()
        unsafe["obligations"] = ["private raw output"] * 129
        store.connection.execute(
            "UPDATE rollover_operations SET plan_json=? WHERE operation_id=?",
            (json.dumps(unsafe), manifest["operation_id"]),
        )
        refused("startup_context_too_large", lambda: store.startup_context(NEW_ID, "runtime:new-shotcaller", AT4))


def test_changed_retry_and_unknown_provider_refuse_without_writes(root):
    state, manifest, context = setup(root, "retry-preflight", champions=0)
    with SQLiteStorage(state) as store:
        native(store, OLD_ID, "unregistered")
        before = store.connection.total_changes
        refused("adapter_unknown", lambda: ShotcallerRolloverRunner(store).run(manifest, at=AT4))
        assert store.connection.total_changes == before
        assert store.rollover_status(manifest["operation_id"]) is None
        native(store, OLD_ID, "codex")
        runner = ShotcallerRolloverRunner(store)
        runner.run(manifest, at=AT4)
        for key, value in (
            ("authority_digest", "different"), ("expected_owner_version", 2),
            ("callsign_assignment_id", "foreign"), ("squad_id", "squad:foreign"),
        ):
            changed = copy.deepcopy(manifest)
            changed[key] = value
            before = store.connection.total_changes
            try:
                runner.run(changed, at=AT4)
            except StorageRefusal:
                pass
            else:
                raise AssertionError(key)
            assert store.connection.total_changes == before
        changed = copy.deepcopy(manifest)
        changed["authority_kind"] = "automatic"
        refused("rollover_authority_required", lambda: runner.run(changed, at=AT4))
        assert store.rollover_status(manifest["operation_id"])["state"] == "prepared"


def test_commit_crash_retry_in_separate_process(root):
    state, manifest, context = setup(root, "process-crash", champions=0)
    manifest_path = root / "process-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with SQLiteStorage(state) as store:
        runner = ShotcallerRolloverRunner(store)
        runner.run(manifest, at=AT4)
        pages = {"pages": read_all_pages(store, manifest["operation_id"])}
    pages_path = root / "pages.json"
    pages_path.write_text(json.dumps(pages), encoding="utf-8")
    script = """
import json, os, sys
from league.sqlite_store import SQLiteStorage
from league.rollover_service import ShotcallerRolloverRunner
with SQLiteStorage(sys.argv[1]) as store:
    original = store.commit_rollover
    def crash(*args, **kwargs):
        result = original(*args, **kwargs)
        os._exit(73)
    store.commit_rollover = crash
    ShotcallerRolloverRunner(store).run(
        json.load(open(sys.argv[2])), at=sys.argv[4], pages=json.load(open(sys.argv[3]))
    )
"""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    crash = subprocess.run(
        [sys.executable, "-c", script, str(state), str(manifest_path), str(pages_path), AT5], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
    )
    assert crash.returncode == 73, crash.stderr
    retry = subprocess.run(
        [sys.executable, str(ROOT / "bin/league"), "--state-root", str(state), "rollover", "run",
         "--manifest", str(manifest_path), "--at", AT6],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
    )
    assert retry.returncode == 0, retry.stdout
    assert b"switched" in retry.stdout
    with SQLiteStorage(state) as store:
        assert store.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='owner_changed'").fetchone()[0] == 1
        assert store.connection.execute("SELECT COUNT(*) FROM delivery_outbox WHERE event_id='event:owner-changed'").fetchone()[0] == 1


def test_abort_drain_receipts_and_expired_plan_retry(root):
    state, manifest, context = setup(root, "abort", champions=0, activate=False)
    with SQLiteStorage(state) as store:
        runner = ShotcallerRolloverRunner(store)
        runner.run(manifest, at=AT4)
        receipt = {
            "schema": "league.rollover-abort-receipt.v1", "verified": True,
            "operation_id": manifest["operation_id"], "successor_agent_id": NEW_ID,
            "runtime_instance_id": "not-created", "runtime_cleanup_receipt_digest": "not-created",
            "cleanup_digest": "synthetic-no-runtime",
        }
        assert runner.run(manifest, at=AT5, abort_receipt=receipt)["operation"]["state"] == "aborted"
        assert runner.run(manifest, at="2026-01-02T00:00:00Z", abort_receipt=receipt)["operation"]["state"] == "aborted"
    state, manifest, context = setup(root, "drain")
    with SQLiteStorage(state) as store:
        runner = ShotcallerRolloverRunner(store)
        runner.run(manifest, at=AT4)
        runner.run(manifest, at=AT5, pages={"pages": read_all_pages(store, manifest["operation_id"])})
        receipt = {
            "schema": "league.rollover-drain-receipt.v1", "verified": True,
            "operation_id": manifest["operation_id"], "predecessor_agent_id": OLD_ID,
            "successor_agent_id": NEW_ID, "owner_event_id": manifest["owner_event_id"],
            "archive_digest": "synthetic-archive", "resource_receipt_digest": "synthetic-resource",
            "callsign_release_receipt_digest": "synthetic-release",
        }
        refused("drain_incomplete", lambda: runner.run(manifest, at=AT6, drain_receipt=receipt))
        assert store.connection.execute("SELECT status FROM runtime_instances WHERE actor_agent_id=?", (OLD_ID,)).fetchone()[0] == "active"
        store.connection.execute("UPDATE obligations SET state='satisfied' WHERE owner_agent_id=?", (OLD_ID,))
        refused("runtime_active", lambda: runner.run(manifest, at=AT6, drain_receipt=receipt))
        store.connection.execute("UPDATE runtime_instances SET status='closed' WHERE actor_agent_id=?", (OLD_ID,))
        assert runner.run(manifest, at=AT6, drain_receipt=receipt)["operation"]["state"] == "completed"
        assert runner.run(manifest, at="2026-01-02T00:00:00Z", drain_receipt=receipt)["operation"]["state"] == "completed"


def test_expired_changed_set_uses_existing_exact_survivor_reconciliation(root):
    state, manifest, context = setup(root, "expired-changed-set", champions=2)
    late = "2026-01-01T02:00:00Z"
    with SQLiteStorage(state) as store:
        runner = ShotcallerRolloverRunner(store)
        prepared = runner.run(manifest, at=AT4)["operation"]
        saved_pages = read_all_pages(store, manifest["operation_id"])
        switched = runner.run(manifest, at=AT5, pages={"pages": saved_pages})["operation"]
        # A different frozen member has left; never edit or shrink the snapshot.
        store.connection.execute(
            "DELETE FROM squad_champions WHERE squad_id=? AND champion_agent_id=?",
            (SQUAD_ID, context["champion_ids"][1]),
        )
        before = store.connection.total_changes
        refused("snapshot_refresh_set_changed", lambda: store.rollover_snapshot_refresh_target(
            manifest["operation_id"], "refresh:changed-set", SQUAD_ID, OLD_ID, NEW_ID,
            switched["version"], prepared["snapshot"]["version"], prepared["snapshot"]["digest"],
            "2026-01-01T03:00:00Z", late,
        ))
        assert store.connection.total_changes == before
        refused("active_champion_snapshot_stale", lambda: store.rollover_bindings(manifest["operation_id"], late))
        champion = context["champion_ids"][0]
        inputs = {
            "operation_id": manifest["operation_id"], "reconciliation_id": "reconcile:survivor",
            "champion_agent_id": champion, "task_id": "task:champion:0",
            "runtime_instance_id": "runtime:champion:0", "snapshot_digest": prepared["snapshot"]["digest"],
            "snapshot_row_digest": saved_pages[0]["rows"][0]["row_digest"],
            "expected_rollover_version": switched["version"],
            "expected_agent_version": store.agent_status(champion)["version"],
            "expected_task_version": 1, "expected_assignment_version": 1,
            "expected_callsign_assignment_version": 2, "pending_outbox_ids": (), "at": late,
        }

        class ExactSyntheticRuntime:
            def __init__(self):
                self.calls = 0

            def verify(self, target, runtime_id):
                self.calls += 1
                result = descendant_runtime_receipt(target, runtime_id)
                result["runtime_generation"] = target["runtime"]["runtime_generation"]
                return result

        adapter = ExactSyntheticRuntime()
        service = RolloverDescendantService(store, adapter)
        # Wrong retained receipt refuses before even invoking the native boundary.
        changed = {**inputs, "snapshot_row_digest": "0" * 64}
        refused("descendant_snapshot_mismatch", lambda: service.reconcile(**changed))
        assert adapter.calls == 0
        identity = bindings_identity(store)
        first = service.reconcile(**inputs)
        assert first["idempotent"] is False
        assert service.reconcile(**inputs)["idempotent"] is True
        assert adapter.calls == 1
        assert bindings_identity(store) == identity
        assert store.rollover_status(manifest["operation_id"])["snapshot"] == prepared["snapshot"]
        startup = store.startup_context(champion, "runtime:champion:0", late)
        assert startup["owning_shotcaller"]["agent_id"] == NEW_ID
        assert startup["requesting_shotcaller"]["agent_id"] == OLD_ID


def test_precommit_failure_and_concurrent_retry(root):
    state, manifest, context = setup(root, "concurrent", champions=0)
    with SQLiteStorage(state) as store:
        runner = ShotcallerRolloverRunner(store)
        runner.run(manifest, at=AT4)
        pages = {"pages": read_all_pages(store, manifest["operation_id"])}
        original = store.commit_rollover

        def fail(*args, **kwargs):
            raise StorageRefusal("synthetic_commit_failure", "synthetic refusal")

        store.commit_rollover = fail
        refused("synthetic_commit_failure", lambda: runner.run(manifest, at=AT5, pages=pages))
        assert store.rollover_status(manifest["operation_id"])["state"] == "acknowledged"
        store.commit_rollover = original
        refused("rollover_conflict", lambda: runner.run(manifest, at=AT5, drain_receipt={}))
        assert store.rollover_status(manifest["operation_id"])["state"] == "acknowledged"
    manifest_path = root / "concurrent-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    command = [sys.executable, str(ROOT / "bin/league"), "--state-root", str(state),
               "rollover", "run", "--manifest", str(manifest_path), "--at", AT6]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, (stdout, stderr)
        assert b"switched" in stdout
    with SQLiteStorage(state) as store:
        assert store.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='owner_changed'").fetchone()[0] == 1


def test_context_uses_refreshed_snapshot_and_cli_inventory(root):
    state, manifest, context = setup(root, "refresh-context", champions=0)
    late = "2026-01-01T02:00:00Z"
    with SQLiteStorage(state) as store:
        runner = ShotcallerRolloverRunner(store)
        prepared = runner.run(manifest, at=AT4)["operation"]
        switched = runner.run(manifest, at=AT5, pages={"pages": read_all_pages(store, manifest["operation_id"])})["operation"]
        refused("startup_identity_stale", lambda: store.startup_context(NEW_ID, "runtime:new-shotcaller", late))
        refreshed = RolloverSnapshotRefreshService(store, ExactSnapshotInventory()).refresh(
            operation_id=manifest["operation_id"], refresh_id="refresh:context", squad_id=SQUAD_ID,
            predecessor_agent_id=OLD_ID, successor_agent_id=NEW_ID,
            expected_rollover_version=switched["version"],
            expected_snapshot_version=prepared["snapshot"]["version"],
            expected_snapshot_digest=prepared["snapshot"]["digest"],
            expires_at="2026-01-01T03:00:00Z", at=late,
        )
        assert store.startup_context(NEW_ID, "runtime:new-shotcaller", late)["verified"]
        assert runner.run(manifest, at=late)["operation"]["snapshot"] == refreshed["snapshot"]
    output = invoke_cli(state, "agent", "startup-context", "--agent-id", NEW_ID,
                        "--runtime-instance-id", "runtime:new-shotcaller", "--at", late, raw=True)
    assert b'league.startup-context.v1' in output
    assert b'synthetic-endpoint' not in output
    inventory = invoke_cli(state, "help", "inventory", raw=True)
    assert b'agent.startup-context' in inventory and b'rollover.run' in inventory
    assert b'league-startup-context.schema.json' in inventory
    assert b'league-rollover-provider-adapters.schema.json' not in inventory


def main():
    tests = [value for key, value in globals().items() if key.startswith("test_")]
    with tempfile.TemporaryDirectory(prefix="league-rollover-successor-") as temporary:
        root = Path(temporary)
        for test in tests:
            test(root)
            print("PASS", test.__name__)
    print(f"{len(tests)} focused rollover/startup tests passed")


if __name__ == "__main__":
    main()
