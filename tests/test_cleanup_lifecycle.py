#!/usr/bin/env python3
"""Policy, resource, receipt, and crash-resumable cleanup regressions."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.cleanup import CleanupAdapterRegistry, CleanupExecutor, CleanupPlanner  # noqa: E402
from league.cli import MAX_JSON_INPUT_BYTES  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from runtime_doubles import StateCleanupAdapter  # noqa: E402
from storage_fixture import CHAMPION_ID, TASK_ID  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402
from storage_test_support import invoke_cli  # noqa: E402


AT3 = "2026-01-01T00:02:00Z"
AT4 = "2026-01-01T00:03:00Z"
AT5 = "2026-01-01T00:04:00Z"


def complete_proof() -> dict:
    return {
        "identity": {"exact": True},
        "endpoint": {"terminal_or_idle": True},
        "git": {"exact_registration": True, "clean": True, "no_unpublished": True},
        "publication": {"exact_head": True, "ci_green": True, "integrated": True},
        "deployment": {"exact_revision": True, "smoke_passed": True},
        "decision": {"explicit": True},
        "failure": {"preserved": True},
    }


def resource(resource_id: str, lifetime: str, action: str) -> dict:
    return {
        "resource_id": resource_id,
        "task_id": TASK_ID,
        "owner_id": CHAMPION_ID,
        "owner_role": "champion",
        "resource_type": "synthetic",
        "lifetime": lifetime,
        "expected_identity": {"resource_id": resource_id, "generation": "exact"},
        "cleanup_action": action,
        "adapter_kind": "fixture",
        "applicable": True,
        "applicability_reason": "Synthetic task-scoped cleanup contract.",
    }


def manifest(
    task_class: str = "deployed_service",
    disposition: str = "completed",
    *,
    with_resources: bool = True,
) -> dict:
    identity = {"task_id": TASK_ID, "owner_id": CHAMPION_ID, "generation": "exact"}
    return {
        "task_id": TASK_ID,
        "owner": {"id": CHAMPION_ID, "role": "champion", "persistent": False},
        "task_class": task_class,
        "disposition": disposition,
        "pending_decisions_clear": True,
        "expected_cleanup_version": 0,
        "identity": identity,
        "legacy_identity": dict(identity),
        "proof": complete_proof(),
        "resources": (
            [
                resource("task-process", "task_owned", "terminate"),
            ]
            if with_resources
            else []
        ),
        "final_actions": [
            {
                "action_kind": name,
                "adapter_kind": "fixture",
                "expected_identity": {"action": name, "generation": "exact"},
                "intended_state": {"completed": True, "action": name},
            }
            for name in (
                ["session_exit", "endpoint_close"]
                + ([] if task_class == "analysis" else ["worktree_remove", "branch_delete"])
                + ["callsign_release"]
            )
        ],
    }


def assert_plan_refused(
    planner: CleanupPlanner,
    value: dict,
    operation_id: str,
    code: str,
) -> None:
    try:
        planner.plan(value, operation_id=operation_id, at=AT3)
    except StorageRefusal as exc:
        assert exc.code == code, (operation_id, exc.code)
    else:
        raise AssertionError(f"cleanup refusal was not enforced: {operation_id}")


def test_supported_policies_reach_cleanup_completed(root: Path) -> None:
    cases = (
        ("analysis", "completed"),
        ("local_git", "completed"),
        ("pr_ci", "completed"),
        ("deployed_service", "completed"),
        ("local_git", "rejected"),
        ("analysis", "cancelled"),
        ("local_git", "failed"),
    )
    for index, (task_class, disposition) in enumerate(cases):
        _, state, _ = seeded_state(root, f"policy-{index}")
        with SQLiteStorage(state) as store:
            result = CleanupPlanner(store).plan(
                manifest(task_class, disposition, with_resources=False),
                operation_id=f"operation:policy-{index}",
                at=AT3,
            )
            assert result["state"] == "cleanup_pending"
            operation = store.cleanup_operation(result["operation_id"])
            assert operation is not None and operation["actions"][0]["action_kind"] == "archive_identity_evidence"
            assert operation["actions"][0]["intended_state"]["proof"] == complete_proof()
            states = {
                action["action_id"]: dict(action["expected_identity"])
                for action in operation["actions"]
            }
            registry = CleanupAdapterRegistry()
            registry.register(StateCleanupAdapter("archive", states, []))
            registry.register(StateCleanupAdapter("fixture", states, []))
            completed = CleanupExecutor(store, registry).execute(
                result["operation_id"],
                expected_fence=0,
                executor_id=f"executor:policy-{index}",
                leased_until=AT5,
                at=AT4,
            )
            assert completed["state"] == "cleanup_completed"
            archive_after = store.connection.execute(
                "SELECT after_json FROM cleanup_action_receipts WHERE action_id=?",
                (operation["actions"][0]["action_id"],),
            ).fetchone()[0]
            assert json.loads(archive_after)["archived"] is True


def test_cleanup_and_resource_commands_are_bounded(root: Path) -> None:
    _, cli_state, _ = seeded_state(root, "policy-cli")
    cli_manifest = root / "policy-cli.json"
    cli_manifest.write_text(json.dumps(manifest("analysis", with_resources=False)) + "\n")
    command = invoke_cli(
        cli_state,
        "cleanup",
        "plan",
        "--manifest",
        str(cli_manifest),
        "--operation-id",
        "operation:cli",
        "--at",
        AT3,
    )
    assert command["ok"] is True and command["result"]["state"] == "cleanup_pending"

    _, resource_cli_state, _ = seeded_state(root, "resource-cli")
    resource_spec = root / "resource-cli.json"
    resource_spec.write_text(
        json.dumps(resource("registered-shared", "shared_lease", "release_lease")) + "\n"
    )
    registered = invoke_cli(
        resource_cli_state,
        "resource",
        "register",
        "--spec",
        str(resource_spec),
        "--at",
        AT3,
    )
    assert registered["ok"] is True and registered["result"]["resource_id"] == "registered-shared"

    oversized = root / "oversized-manifest.json"
    oversized.write_bytes(b"{" + (b" " * MAX_JSON_INPUT_BYTES))
    refused = invoke_cli(
        cli_state,
        "cleanup",
        "plan",
        "--manifest",
        str(oversized),
        "--operation-id",
        "operation:oversized",
        "--at",
        AT3,
        expected=2,
    )
    assert refused["error"]["code"] == "input_too_large"


def test_cleanup_manifest_and_resource_refusals(root: Path) -> None:
    _, state, _ = seeded_state(root, "refusals")
    with SQLiteStorage(state) as store:
        planner = CleanupPlanner(store)
        for name, mutate, code in (
            ("missing-proof", lambda value: value.pop("proof"), "cleanup_proof_missing"),
            ("pending", lambda value: value.update(pending_decisions_clear=False), "pending_decision"),
            ("dirty", lambda value: value["proof"]["git"].update(clean=False), "cleanup_proof_missing"),
            ("unpublished", lambda value: value["proof"]["git"].update(no_unpublished=False), "cleanup_proof_missing"),
            ("ambiguous", lambda value: value["proof"]["identity"].update(exact=False), "cleanup_proof_missing"),
            ("legacy", lambda value: value.update(legacy_identity={"different": True}), "legacy_identity_mismatch"),
            ("shotcaller", lambda value: value["owner"].update(role="shotcaller"), "cleanup_owner_refused"),
        ):
            value = manifest(with_resources=False)
            mutate(value)
            assert_plan_refused(planner, value, f"operation:{name}", code)

        for item, code in (
            (resource("shared-stop", "shared_lease", "release_lease"), "shared_resource_refused"),
            (resource("persistent-stop", "persistent_retain", "retain"), "persistent_resource_refused"),
        ):
            value = manifest(with_resources=False)
            value["resources"] = [item]
            assert_plan_refused(planner, value, f"operation:{item['resource_id']}", code)

        for item, code in (
            (resource("shared-invalid", "shared_lease", "terminate"), "shared_resource_refused"),
            (resource("persistent-invalid", "persistent_retain", "terminate"), "persistent_resource_refused"),
        ):
            try:
                planner.register_resource(item, AT3)
            except StorageRefusal as exc:
                assert exc.code == code
            else:
                raise AssertionError("invalid shared/persistent cleanup action was registered")


def test_registered_resources_cannot_be_hidden(root: Path) -> None:
    _, state, _ = seeded_state(root, "registered-shared")
    with SQLiteStorage(state) as store:
        planner = CleanupPlanner(store)
        planner.register_resource(resource("registered-shared", "shared_lease", "release_lease"), AT3)
        assert_plan_refused(
            planner,
            manifest(with_resources=False),
            "operation:hidden-shared",
            "shared_resource_refused",
        )

    _, omitted_state, _ = seeded_state(root, "omitted-task-resource")
    with SQLiteStorage(omitted_state) as store:
        planner = CleanupPlanner(store)
        planner.register_resource(resource("registered-task", "task_owned", "terminate"), AT3)
        assert_plan_refused(
            planner,
            manifest(with_resources=False),
            "operation:omitted-task-resource",
            "resource_proof_missing",
        )


def planned_execution(root: Path, suffix: str) -> tuple[SQLiteStorage, CleanupExecutor, dict, dict, list[str]]:
    _, state, _ = seeded_state(root, suffix)
    store = SQLiteStorage(state)
    operation_id = f"operation:{suffix}"
    CleanupPlanner(store).plan(manifest(), operation_id=operation_id, at=AT3)
    operation = store.cleanup_operation(operation_id)
    assert operation is not None
    states = {action["action_id"]: dict(action["expected_identity"]) for action in operation["actions"]}
    effects: list[str] = []
    registry = CleanupAdapterRegistry()
    registry.register(StateCleanupAdapter("archive", states, effects))
    registry.register(StateCleanupAdapter("fixture", states, effects))
    return store, CleanupExecutor(store, registry), operation, states, effects


def test_crash_after_every_external_action_and_duplicate(root: Path) -> None:
    probe, _, operation, _, _ = planned_execution(root, "probe")
    action_kinds = [action["action_kind"] for action in operation["actions"]]
    probe.close()
    for index, action_kind in enumerate(action_kinds):
        # Each rollback boundary gets a fresh database so no prior crash case
        # can mask a duplicate effect or receipt-recovery defect.
        store, executor, operation, _, effects = planned_execution(root, f"crash-{index}")

        def crash(point: str, target: str = action_kind) -> None:
            if point == f"after_external_action:{target}":
                raise RuntimeError(point)

        try:
            executor.execute(
                operation["operation_id"],
                expected_fence=0,
                executor_id="executor:first",
                leased_until=AT4,
                at=AT3,
                fault=crash,
            )
        except RuntimeError as exc:
            assert action_kind in str(exc)
        else:
            raise AssertionError(f"crash boundary did not fire: {action_kind}")
        crashed_action = next(action for action in operation["actions"] if action["action_kind"] == action_kind)
        assert effects.count(crashed_action["action_id"]) == 1
        resumed = executor.execute(
            operation["operation_id"],
            expected_fence=1,
            executor_id="executor:resume",
            leased_until=AT5,
            at=AT4,
        )
        assert resumed["state"] == "cleanup_completed"
        assert effects.count(crashed_action["action_id"]) == 1, "crash recovery repeated an external effect"
        ordinals = {
            action["action_id"]: action["ordinal"] for action in operation["actions"]
        }
        effect_ordinals = [ordinals[action_id] for action_id in effects]
        assert effect_ordinals == sorted(effect_ordinals)
        duplicate = executor.execute(
            operation["operation_id"],
            expected_fence=2,
            executor_id="executor:duplicate",
            leased_until=AT4,
            at=AT4,
        )
        assert duplicate["idempotent"] is True
        assert store.integrity()["ok"]
        exported = json.loads(store.export_bytes(format_name="json", purpose="inspection", max_records=1000))
        assert all(
            row["before_json"] == "[redacted]"
            for row in exported["tables"]["cleanup_action_receipts"]
        )
        store.close()


def test_already_closed_or_missing_exact_resources_and_stale_identity(root: Path) -> None:
    store, executor, operation, states, effects = planned_execution(root, "already")
    endpoint = next(action for action in operation["actions"] if action["action_kind"] == "endpoint_close")
    states[endpoint["action_id"]] = dict(endpoint["intended_state"])
    process = next(action for action in operation["actions"] if action["action_kind"] == "terminate")
    states[process["action_id"]] = dict(process["intended_state"])
    result = executor.execute(
        operation["operation_id"], expected_fence=0, executor_id="executor:already", leased_until=AT4, at=AT3
    )
    assert result["state"] == "cleanup_completed"
    assert endpoint["action_id"] not in effects
    assert process["action_id"] not in effects
    store.close()

    store, executor, operation, states, effects = planned_execution(root, "stale")
    target = operation["actions"][1]
    states[target["action_id"]] = {"resource_id": "reused", "generation": "different"}
    try:
        executor.execute(
            operation["operation_id"], expected_fence=0, executor_id="executor:stale", leased_until=AT4, at=AT3
        )
    except StorageRefusal as exc:
        assert exc.code == "cleanup_identity_mismatch"
    else:
        raise AssertionError("stale resource identity was cleaned")
    assert effects == [], "cleanup changed state before all action identities passed preflight"
    try:
        executor.execute(
            operation["operation_id"],
            expected_fence=1,
            executor_id="executor:early-takeover",
            leased_until=AT5,
            at=AT3,
        )
    except StorageRefusal as exc:
        assert exc.code == "cleanup_busy" and exc.retryable is True
    else:
        raise AssertionError("cleanup executor stole an unexpired lease")
    store.close()


def test_public_candidate_has_no_private_paths() -> None:
    candidates = [
        ROOT / "src/league/adapter_types.py",
        ROOT / "src/league/adapters.py",
        ROOT / "src/league/runtime.py",
        ROOT / "src/league/routing.py",
        ROOT / "src/league/cleanup.py",
        ROOT / "src/league/sqlite_runtime_ops.py",
    ]
    forbidden = (
        b"/" + b"Users/",
        b"file" + b"://",
        b"BEGIN " + b"PRIVATE KEY",
        b"access_" + b"token",
    )
    for path in candidates:
        data = path.read_bytes()
        assert not any(marker in data for marker in forbidden), path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-cleanup-lifecycle-") as temporary:
        root = Path(temporary)
        test_supported_policies_reach_cleanup_completed(root)
        test_cleanup_and_resource_commands_are_bounded(root)
        test_cleanup_manifest_and_resource_refusals(root)
        test_registered_resources_cannot_be_hidden(root)
        test_crash_after_every_external_action_and_duplicate(root)
        test_already_closed_or_missing_exact_resources_and_stale_identity(root)
    test_public_candidate_has_no_private_paths()
    print("PASS: task-class policies, typed resources, proof-first cleanup, crash recovery, immutable receipts, duplicate safety, and public-path scan")


if __name__ == "__main__":
    main()
