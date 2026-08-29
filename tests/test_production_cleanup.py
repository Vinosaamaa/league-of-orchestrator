#!/usr/bin/env python3
"""Canonical SQLite production cleanup and one synthetic crash-resume E2E."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.cleanup import CleanupPlanner, cleanup_action_digest  # noqa: E402
from league.production_cleanup import ProductionCleanup  # noqa: E402
from league.real_cleanup import SubprocessRunner  # noqa: E402
from league.real_canary import (  # noqa: E402
    CHAMPION_ID,
    LIFECYCLE_TASK_ID,
    SHOTCALLER_ID,
    _cleanup_files,
    _create_git_canary,
    _setup_sqlite,
)
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from test_real_cleanup import FakeHerdrRunner, herdr_identity  # noqa: E402
from storage_test_support import invoke_cli, migrated_state  # noqa: E402


AT_PLAN = "2026-01-01T01:02:00Z"
AT_RESUME = "2026-01-01T01:04:00Z"
LEASE_FIRST = "2026-01-01T01:03:00Z"
LEASE_RESUME = "2026-01-01T01:14:00Z"


class HybridRunner:
    def __init__(self, herdr: FakeHerdrRunner) -> None:
        self.herdr = herdr
        self.system = SubprocessRunner()

    def run(self, arguments, *, allow_failure: bool = False):
        if arguments[0] == "herdr":
            return self.herdr.run(arguments, allow_failure=allow_failure)
        return self.system.run(arguments, allow_failure=allow_failure)


class RolloverPlanningStore:
    def __init__(self) -> None:
        self.plan = None
        self.target = {
            "operation_id": "rollover:one",
            "predecessor_agent_id": "shotcaller:old",
            "successor_agent_id": "shotcaller:new",
            "state": "switched",
            "version": 4,
            "owner_event_id": "event:owner-changed",
            "owner_outbox_id": "outbox:owner-changed",
            "squad_id": "squad:one",
            "successor_runtime_instance_id": "runtime:new",
        }

    def unresolved_repository_publications(self, task_id):
        return []

    def task_resources(self, task_id):
        return []

    def rollover_cleanup_target(self, operation_id):
        return dict(self.target) if operation_id == self.target["operation_id"] else None

    def plan_cleanup(self, plan):
        self.plan = dict(plan)
        return {"operation_id": plan["operation_id"], "fence": 0, "state": "cleanup_pending"}


class CompletedRolloverStore:
    def __init__(self, operation, target) -> None:
        self.operation = operation
        self.target = target
        self.receipt = None

    def cleanup_operation(self, operation_id):
        return self.operation if operation_id == self.operation["operation_id"] else None

    def complete_rollover_drain(self, operation_id, expected_version, receipt, at):
        assert operation_id == self.target["operation_id"]
        assert expected_version == self.target["version"]
        self.receipt = dict(receipt)
        return {"operation_id": operation_id, "state": "completed", "version": expected_version + 1}


def test_rollover_predecessor_requires_exact_switch_and_emits_drain_receipt() -> None:
    store = RolloverPlanningStore()
    final_actions = []
    for ordinal, action_kind in enumerate(
        ("session_exit", "endpoint_close", "callsign_release"), start=1
    ):
        final_actions.append(
            {
                "action_kind": action_kind,
                "adapter_kind": {
                    "session_exit": "harness",
                    "endpoint_close": "backend",
                    "callsign_release": "callsign",
                }[action_kind],
                "expected_identity": {"action": action_kind, "generation": "exact"},
                "intended_state": {"completed": True, "action": action_kind},
            }
        )
    manifest = {
        "task_id": "task:rollover-drain",
        "owner": {"id": "shotcaller:old", "role": "shotcaller", "persistent": True},
        "task_class": "analysis",
        "disposition": "completed",
        "pending_decisions_clear": True,
        "expected_cleanup_version": 1,
        "identity": {"task_id": "task:rollover-drain", "owner_id": "shotcaller:old"},
        "legacy_identity": {"task_id": "task:rollover-drain", "owner_id": "shotcaller:old"},
        "proof": {
            "identity": {"exact": True},
            "endpoint": {"terminal_or_idle": True},
        },
        "resources": [
            {
                "resource_id": "process:rollover-predecessor",
                "task_id": "task:rollover-drain",
                "owner_id": "shotcaller:old",
                "owner_role": "task",
                "resource_type": "process",
                "lifetime": "task_owned",
                "expected_identity": {
                    "pid": 4242,
                    "process_start": "synthetic-start",
                },
                "cleanup_action": "terminate",
                "adapter_kind": "process",
                "applicable": True,
                "applicability_reason": "Exact predecessor drain process.",
            }
        ],
        "rollover": {"operation_id": "rollover:one", "expected_version": 4},
        "final_actions": final_actions,
    }
    CleanupPlanner(store).plan(
        manifest, operation_id="cleanup:rollover-one", at=AT_PLAN
    )
    assert store.plan is not None
    assert any(
        action["resource_id"] == "process:rollover-predecessor"
        for action in store.plan["actions"]
    )
    actions = []
    for action in store.plan["actions"]:
        value = dict(action)
        value["operation_id"] = store.plan["operation_id"]
        value["receipt"] = {"receipt_hash": f"receipt:{value['ordinal']}"}
        actions.append(value)
    operation = {"operation_id": store.plan["operation_id"], "actions": actions}
    completed_store = CompletedRolloverStore(operation, store.target)
    result = ProductionCleanup(completed_store)._complete_rollover(
        {"operation": operation, "rollover": store.target}, AT_RESUME
    )
    assert result["state"] == "completed"
    receipt = completed_store.receipt
    assert receipt is not None and receipt["verified"] is True
    callsign = next(action for action in actions if action["action_kind"] == "callsign_release")
    assert receipt["callsign_release_receipt_digest"] == cleanup_action_digest(callsign)


def _resource(
    resource_id: str,
    lifetime: str,
    cleanup_action: str,
    adapter_kind: str,
    expected_identity: dict,
) -> dict:
    return {
        "resource_id": resource_id,
        "task_id": LIFECYCLE_TASK_ID,
        "owner_id": CHAMPION_ID,
        "owner_role": "champion",
        "resource_type": "synthetic-shared" if lifetime == "shared_lease" else "synthetic-retained",
        "lifetime": lifetime,
        "expected_identity": expected_identity,
        "cleanup_action": cleanup_action,
        "adapter_kind": adapter_kind,
        "applicable": True,
        "applicability_reason": "Synthetic production cleanup fixture.",
    }


def test_production_cleanup_crash_resume_and_lease_scope(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    git = _create_git_canary(root)
    herdr = herdr_identity()
    setup = _setup_sqlite(root, ROOT, git, herdr)
    state = root / "league/state"
    manifest_path, _ = _cleanup_files(
        root,
        git,
        herdr,
        f"callsign-assignment:{setup['assignment']['assignment_id']}",
        "Lux",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shared_identity = {
        "resource_id": "shared:cleanup-canary",
        "task_id": LIFECYCLE_TASK_ID,
        "owner_agent_id": CHAMPION_ID,
        "kind": "shared-agent-chrome",
        "endpoint": "shared-agent-chrome",
        "generation": "generation:shared-cleanup",
    }
    shared = _resource(
        shared_identity["resource_id"],
        "shared_lease",
        "release_lease",
        "lease",
        shared_identity,
    )
    retained = _resource(
        "persistent:cleanup-canary",
        "persistent_retain",
        "retain",
        "retain",
        {"resource_id": "persistent:cleanup-canary", "generation": "persistent"},
    )
    manifest["resources"] = [shared, retained]

    runner = FakeHerdrRunner()
    with SQLiteStorage(state, request_wal=False) as store:
        store.connection.execute(
            """
            INSERT INTO resource_leases
              (resource_id,task_id,owner_agent_id,kind,endpoint,generation,state,metadata_json)
            VALUES(?,?,?,?,?,?,'active','{}')
            """,
            (
                shared_identity["resource_id"],
                shared_identity["task_id"],
                shared_identity["owner_agent_id"],
                shared_identity["kind"],
                shared_identity["endpoint"],
                shared_identity["generation"],
            ),
        )
        store.transition_task(
            LIFECYCLE_TASK_ID,
            herdr["runtime_instance_id"],
            3,
            "completed",
            "Synthetic Champion completed",
            "Automatically execute exact cleanup",
            None,
            "transition:production-cleanup",
            "transition-key:production-cleanup",
            "event:production-cleanup",
            "outbox:production-cleanup",
            SHOTCALLER_ID,
            "2026-01-01T01:01:00Z",
        )
        planned = CleanupPlanner(store).plan(
            manifest, operation_id="operation:production-cleanup", at=AT_PLAN
        )
        context = store.cleanup_execution_context(planned["operation_id"])
        assert context["task_identity"]["task_id"] == LIFECYCLE_TASK_ID
        assert context["disposition"] == "completed"
        assert {item["adapter_kind"] for item in context["adapter_policy"]} >= {
            "archive",
            "harness",
            "backend",
            "git",
            "callsign",
            "lease",
        }
        store.connection.execute(
            """
            INSERT INTO task_resources
              (resource_id,task_id,owner_id,owner_role,resource_type,lifetime,
               expected_identity_json,cleanup_action,adapter_kind,applicable,
               applicability_reason,state,version,registered_at,updated_at)
            VALUES(?,?,?,?,?,'persistent_retain',?,'retain','retain',1,?,'active',1,?,?)
            """,
            (
                "persistent:late-foreign-plan",
                LIFECYCLE_TASK_ID,
                CHAMPION_ID,
                "champion",
                "synthetic-retained",
                '{"generation":"late","resource_id":"persistent:late-foreign-plan"}',
                "Late resource must never be guessed into an existing plan.",
                AT_PLAN,
                AT_PLAN,
            ),
        )
        try:
            store.cleanup_execution_context(planned["operation_id"])
        except StorageRefusal as exc:
            assert exc.code == "cleanup_resource_changed"
        else:
            raise AssertionError("late resource was guessed into an immutable cleanup plan")
        store.connection.execute(
            "DELETE FROM task_resources WHERE resource_id='persistent:late-foreign-plan'"
        )
        service = ProductionCleanup(store, runner=HybridRunner(runner))

        def crash(event: object) -> None:
            if getattr(event, "action_kind", None) == "session_exit":
                raise RuntimeError("synthetic process interruption after session exit")

        try:
            service.execute(
                planned["operation_id"],
                expected_fence=0,
                executor_id="executor:first",
                leased_until=LEASE_FIRST,
                at=AT_PLAN,
                fault=crash,
            )
        except RuntimeError as exc:
            assert "session exit" in str(exc)
        else:
            raise AssertionError("synthetic interruption did not fire")
        interrupted = store.cleanup_operation(planned["operation_id"])
        assert interrupted is not None and interrupted["state"] == "executing"
        assert interrupted["fence"] == 1
        assert runner.agent == "done"

        resumed = service.execute(
            planned["operation_id"],
            expected_fence=1,
            executor_id="executor:resume",
            leased_until=LEASE_RESUME,
            at=AT_RESUME,
        )
        assert resumed["mode"] == "automatic_champion"
        assert resumed["execution"]["state"] == "cleanup_completed"
        assert store.resource_lease_for_cleanup(shared_identity["resource_id"])["state"] == "released"
        persistent = store.connection.execute(
            "SELECT state FROM task_resources WHERE resource_id='persistent:cleanup-canary'"
        ).fetchone()
        assert persistent["state"] == "active"
        assert not Path(git["worktree"]).exists()
        assert runner.pane is False

        duplicate = service.execute(
            planned["operation_id"],
            expected_fence=2,
            executor_id="executor:duplicate",
            leased_until=LEASE_RESUME,
            at=AT_RESUME,
        )
        assert duplicate["execution"]["idempotent"] is True
        assert store.connection.execute(
            "SELECT COUNT(*) FROM teardown_receipts WHERE operation_id=?",
            (planned["operation_id"],),
        ).fetchone()[0] == 1


def test_stable_execute_command_refuses_unknown_operation(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    state, _ = migrated_state(root, "unknown-operation", request_wal=False)
    result = invoke_cli(
        state,
        "cleanup",
        "execute",
        "--operation-id",
        "operation:missing",
        "--expected-fence",
        "0",
        "--executor-id",
        "executor:test",
        "--leased-until",
        LEASE_FIRST,
        "--at",
        AT_PLAN,
        expected=2,
    )
    assert result["command"] == "cleanup.execute"
    assert result["error"]["code"] == "cleanup_operation_unknown"


def main() -> None:
    test_rollover_predecessor_requires_exact_switch_and_emits_drain_receipt()
    with tempfile.TemporaryDirectory(prefix="league-production-cleanup-") as temporary:
        root = Path(temporary)
        test_production_cleanup_crash_resume_and_lease_scope(root / "e2e")
        test_stable_execute_command_refuses_unknown_operation(root / "cli")
    print(
        "PASS: canonical SQLite production cleanup, exact shared-lease release, "
        "persistent retention, crash resume, and duplicate safety"
    )


if __name__ == "__main__":
    main()
