#!/usr/bin/env python3
"""Focused autonomous-delivery authorization and action lifecycle coverage."""

from __future__ import annotations

import tempfile
import threading
import json
import shutil
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import league.cli as league_cli  # noqa: E402
from league.protected_gate import ProtectedGateExecutor  # noqa: E402
from league.sqlite_store import DATABASE_NAME, SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from request_lifecycle_fixture import create_context  # noqa: E402
from request_lifecycle_fixture import LUX_ID  # noqa: E402
from storage_fixture import REPOSITORY, SHOTCALLER_ID, write_json  # noqa: E402
from storage_test_support import invoke_cli  # noqa: E402


AT = "2026-01-01T00:10:00Z"


def _grant(path: Path) -> Path:
    write_json(
        path,
        {
            "schema": "league.autonomous-grant.v1",
            "grant_id": "grant:synthetic:1",
            "goal_id": "goal:synthetic-delivery",
            "issuer": {"kind": "summoner", "id": "summoner:synthetic-owner"},
            "shotcaller_agent_id": SHOTCALLER_ID,
            "exact_goal": "Deliver the synthetic issue through verified cleanup.",
            "scope": {
                "project_ids": [],
                "repositories": [REPOSITORY],
                "environments": ["staging"],
                "deployment_targets": ["service:synthetic"],
            },
            "allowed_actions": [
                "land",
                "release",
                "deploy",
                "verify",
                "repair",
                "cleanup",
                "issue_reopen",
                "live_reconcile",
                "retire",
                "shotcaller_create",
                "squad_register",
            ],
            "exclusions": ["production_data_mutation"],
            "sensitive_inclusions": [],
            "resource_boundary": {"changed_files": 12, "cost_microunits": 5000},
            "starts_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-01-02T00:00:00Z",
            "limits": {
                "max_attempts": 8,
                "max_concurrency": 1,
                "max_cost_microunits": 5000,
                "max_changed_files": 12,
                "max_duration_seconds": 3600,
                "max_repair_attempts": 2,
            },
            "revision": 1,
        },
    )
    return path


def _authorize(state: Path, root: Path) -> dict:
    return invoke_cli(
        state,
        "mode",
        "authorize",
        "--grant",
        str(_grant(root / "grant.json")),
        "--expected-goal-version",
        "0",
        "--at",
        AT,
    )["result"]


def _action(
    path: Path,
    *,
    suffix: str = "land",
    action_kind: str = "land",
    attempts: int = 1,
    cost_microunits: int = 1000,
    changed_files: int = 4,
    duration_seconds: int = 300,
) -> Path:
    write_json(
        path,
        {
            "schema": "league.autonomous-action.v1",
            "action_use_id": f"action:{suffix}",
            "idempotency_key": f"idempotency:{suffix}",
            "goal_id": "goal:synthetic-delivery",
            "grant_id": "grant:synthetic:1",
            "actor_agent_id": SHOTCALLER_ID,
            "action_kind": action_kind,
            "scope": {
                "project_id": None,
                "repository": REPOSITORY,
                "environment": "staging",
                "deployment_target": "service:synthetic",
            },
            "risk_categories": [],
            "sensitive_categories": [],
            "resources": {
                "changed_files": changed_files,
                "cost_microunits": cost_microunits,
            },
            "usage": {
                "attempts": attempts,
                "cost_microunits": cost_microunits,
                "changed_files": changed_files,
                "duration_seconds": duration_seconds,
            },
        },
    )
    return path


def test_manual_default_and_exact_grant_authorization(root: Path) -> None:
    state, store, _ = create_context(root, "grant")
    store.close()
    manual = invoke_cli(state, "mode", "status", "--goal-id", "goal:missing", "--at", AT)
    assert manual["result"] == {
        "schema": "league.mode-status.v1",
        "mode": "manual",
        "goal_id": "goal:missing",
        "goal_state": "awaiting_authority",
        "grant": None,
        "limits": {},
        "usage": {},
        "next_irreversible_action": "authorize",
    }
    grant_path = _grant(root / "grant.json")
    incomplete = json.loads(grant_path.read_text(encoding="utf-8"))
    incomplete["limits"].pop("max_concurrency")
    incomplete_path = root / "incomplete-grant.json"
    write_json(incomplete_path, incomplete)
    refusal = invoke_cli(
        state,
        "mode",
        "authorize",
        "--grant",
        str(incomplete_path),
        "--expected-goal-version",
        "0",
        "--at",
        AT,
        expected=2,
    )
    assert refusal["error"]["code"] == "mode_grant_invalid"
    authorized = invoke_cli(
        state,
        "mode",
        "authorize",
        "--grant",
        str(grant_path),
        "--expected-goal-version",
        "0",
        "--at",
        AT,
    )["result"]
    assert authorized["mode"] == "autonomous_delivery"
    assert authorized["goal_state"] == "implementing"
    assert authorized["grant"]["canonical_digest"]
    assert authorized["grant"]["revision"] == 1
    assert authorized["idempotent"] is False
    retry = invoke_cli(
        state,
        "mode",
        "authorize",
        "--grant",
        str(grant_path),
        "--expected-goal-version",
        "0",
        "--at",
        AT,
    )["result"]
    assert retry["idempotent"] is True
    status = invoke_cli(
        state, "mode", "status", "--goal-id", "goal:synthetic-delivery", "--at", AT
    )["result"]
    assert status["mode"] == "autonomous_delivery"
    assert status["grant"]["scope"]["repositories"] == [REPOSITORY]
    assert status["limits"]["max_attempts"] == 8
    assert status["usage"]["attempts"] == 0


def test_exact_action_use_settlement_and_repair_loop(root: Path) -> None:
    state, store, _ = create_context(root, "action")
    store.close()
    _authorize(state, root)
    ready = invoke_cli(
        state,
        "mode",
        "transition",
        "--goal-id",
        "goal:synthetic-delivery",
        "--expected-goal-version",
        "1",
        "--state",
        "ready_to_land",
        "--at",
        AT,
    )["result"]
    assert ready["goal_state"] == "ready_to_land" and ready["goal_version"] == 2
    action_path = _action(root / "land.json")
    used = invoke_cli(
        state,
        "mode",
        "use",
        "--action",
        str(action_path),
        "--expected-goal-version",
        "2",
        "--at",
        AT,
    )["result"]
    assert used["state"] == "in_progress"
    assert used["goal_state"] == "landing" and used["goal_version"] == 3
    assert used["external_action_owner"] == SHOTCALLER_ID
    assert len(used["use_receipt_digest"]) == 64
    retry = invoke_cli(
        state,
        "mode",
        "use",
        "--action",
        str(action_path),
        "--expected-goal-version",
        "2",
        "--at",
        "2026-01-01T00:10:01Z",
    )["result"]
    assert retry["idempotent"] is True
    failed = invoke_cli(
        state,
        "mode",
        "settle",
        "--action-use-id",
        "action:land",
        "--goal-id",
        "goal:synthetic-delivery",
        "--expected-goal-version",
        "3",
        "--use-receipt-digest",
        used["use_receipt_digest"],
        "--outcome",
        "failed",
        "--result-receipt-digest",
        "a" * 64,
        "--failure-class",
        "synthetic_verification_failure",
        "--at",
        "2026-01-01T00:11:00Z",
    )["result"]
    assert failed["goal_state"] == "repair_pending"
    assert failed["repair"] == {
        "repair_id": "repair:action:land",
        "state": "pending",
        "attempts_used": 0,
        "max_attempts": 2,
    }
    status = invoke_cli(
        state,
        "mode",
        "status",
        "--goal-id",
        "goal:synthetic-delivery",
        "--at",
        "2026-01-01T00:11:00Z",
    )["result"]
    assert status["goal_state"] == "repair_pending"
    assert status["usage"] == {
        "attempts": 1,
        "cost_microunits": 1000,
        "changed_files": 4,
        "duration_seconds": 300,
        "concurrency": 0,
    }

    repair_action = _action(
        root / "repair.json",
        suffix="repair",
        action_kind="repair",
        cost_microunits=100,
        changed_files=1,
        duration_seconds=60,
    )
    repair_use = invoke_cli(
        state,
        "mode",
        "use",
        "--action",
        str(repair_action),
        "--expected-goal-version",
        "4",
        "--at",
        "2026-01-01T00:12:00Z",
    )["result"]
    repaired = invoke_cli(
        state,
        "mode",
        "settle",
        "--action-use-id",
        "action:repair",
        "--goal-id",
        "goal:synthetic-delivery",
        "--expected-goal-version",
        "5",
        "--use-receipt-digest",
        repair_use["use_receipt_digest"],
        "--outcome",
        "succeeded",
        "--result-receipt-digest",
        "b" * 64,
        "--at",
        "2026-01-01T00:13:00Z",
    )["result"]
    assert repaired["goal_state"] == "verifying"
    assert repaired["repair"]["state"] == "completed"

    verify_action = _action(
        root / "verify.json",
        suffix="verify",
        action_kind="verify",
        cost_microunits=100,
        changed_files=1,
        duration_seconds=60,
    )
    verify_use = invoke_cli(
        state,
        "mode",
        "use",
        "--action",
        str(verify_action),
        "--expected-goal-version",
        "6",
        "--at",
        "2026-01-01T00:14:00Z",
    )["result"]
    delivered = invoke_cli(
        state,
        "mode",
        "settle",
        "--action-use-id",
        "action:verify",
        "--goal-id",
        "goal:synthetic-delivery",
        "--expected-goal-version",
        "7",
        "--use-receipt-digest",
        verify_use["use_receipt_digest"],
        "--outcome",
        "succeeded",
        "--result-receipt-digest",
        "c" * 64,
        "--at",
        "2026-01-01T00:15:00Z",
    )["result"]
    assert delivered["goal_state"] == "delivered"
    cleanup_pending = invoke_cli(
        state,
        "mode",
        "transition",
        "--goal-id",
        "goal:synthetic-delivery",
        "--expected-goal-version",
        "8",
        "--state",
        "cleanup_pending",
        "--at",
        "2026-01-01T00:16:00Z",
    )["result"]
    assert cleanup_pending["goal_state"] == "cleanup_pending"
    cleanup_action = _action(
        root / "cleanup.json",
        suffix="cleanup",
        action_kind="cleanup",
        cost_microunits=100,
        changed_files=1,
        duration_seconds=60,
    )
    cleanup_use = invoke_cli(
        state,
        "mode",
        "use",
        "--action",
        str(cleanup_action),
        "--expected-goal-version",
        "9",
        "--at",
        "2026-01-01T00:17:00Z",
    )["result"]
    cleaned = invoke_cli(
        state,
        "mode",
        "settle",
        "--action-use-id",
        "action:cleanup",
        "--goal-id",
        "goal:synthetic-delivery",
        "--expected-goal-version",
        "10",
        "--use-receipt-digest",
        cleanup_use["use_receipt_digest"],
        "--outcome",
        "succeeded",
        "--result-receipt-digest",
        "d" * 64,
        "--at",
        "2026-01-01T00:18:00Z",
    )["result"]
    assert cleaned["goal_state"] == "cleaned"


def test_scope_limits_revocation_expiry_and_two_writer_cas(root: Path) -> None:
    state, store, _ = create_context(root, "boundaries")
    store.close()
    _authorize(state, root)
    invoke_cli(
        state,
        "mode",
        "transition",
        "--goal-id",
        "goal:synthetic-delivery",
        "--expected-goal-version",
        "1",
        "--state",
        "ready_to_land",
        "--at",
        AT,
    )
    refused_action = {
        "schema": "league.autonomous-action.v1",
        "action_use_id": "action:refused",
        "idempotency_key": "idempotency:refused",
        "goal_id": "goal:synthetic-delivery",
        "grant_id": "grant:synthetic:1",
        "actor_agent_id": SHOTCALLER_ID,
        "action_kind": "land",
        "scope": {
            "project_id": None,
            "repository": REPOSITORY,
            "environment": "staging",
            "deployment_target": "service:synthetic",
        },
        "risk_categories": [],
        "sensitive_categories": ["credentials"],
        "resources": {"changed_files": 1, "cost_microunits": 1},
        "usage": {
            "attempts": 1,
            "cost_microunits": 1,
            "changed_files": 1,
            "duration_seconds": 1,
        },
    }
    write_json(root / "refused.json", refused_action)
    refusal = invoke_cli(
        state,
        "mode",
        "use",
        "--action",
        str(root / "refused.json"),
        "--expected-goal-version",
        "2",
        "--at",
        AT,
        expected=2,
    )
    assert refusal["error"]["code"] == "sensitive_scope_refused"
    refused_action["sensitive_categories"] = []
    refused_action["actor_agent_id"] = LUX_ID
    write_json(root / "champion.json", refused_action)
    owner_refusal = invoke_cli(
        state,
        "mode",
        "use",
        "--action",
        str(root / "champion.json"),
        "--expected-goal-version",
        "2",
        "--at",
        AT,
        expected=2,
    )
    assert owner_refusal["error"]["code"] == "action_owner_refused"

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def writer(suffix: str) -> None:
        action_path = _action(root / f"{suffix}.json", suffix=suffix)
        with SQLiteStorage(state, busy_timeout_ms=2000) as candidate:
            from json import loads

            value = loads(action_path.read_text(encoding="utf-8"))
            barrier.wait()
            try:
                candidate.use_mode_action(value, 2, AT)
                outcomes.append("committed")
            except StorageRefusal as exc:
                outcomes.append(exc.code)

    threads = [threading.Thread(target=writer, args=(suffix,)) for suffix in ("cas-a", "cas-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["committed", "goal_version_conflict"]

    concurrent = invoke_cli(
        state,
        "mode",
        "use",
        "--action",
        str(_action(root / "concurrent.json", suffix="concurrent", action_kind="release")),
        "--expected-goal-version",
        "3",
        "--at",
        AT,
        expected=2,
    )
    assert concurrent["error"]["code"] == "mode_limit_exceeded"

    status = invoke_cli(
        state, "mode", "status", "--goal-id", "goal:synthetic-delivery", "--at", AT
    )["result"]
    refused_revoker = invoke_cli(
        state,
        "mode",
        "revoke",
        "--grant-id",
        "grant:synthetic:1",
        "--revoked-by",
        "shotcaller:synthetic-owner",
        "--reason",
        "Unproven revoker",
        "--expected-goal-version",
        str(status["goal_version"]),
        "--at",
        "2026-01-01T00:11:00Z",
        expected=2,
    )
    assert refused_revoker["error"]["code"] == "grant_revoker_refused"
    unchanged = invoke_cli(
        state, "mode", "status", "--goal-id", "goal:synthetic-delivery", "--at", AT
    )["result"]
    assert unchanged["goal_version"] == status["goal_version"]
    assert unchanged["grant"]["status"] == "active"
    revoked = invoke_cli(
        state,
        "mode",
        "revoke",
        "--grant-id",
        "grant:synthetic:1",
        "--revoked-by",
        "summoner:synthetic-owner",
        "--reason",
        "Synthetic immediate revocation",
        "--expected-goal-version",
        str(status["goal_version"]),
        "--at",
        "2026-01-01T00:12:00Z",
    )["result"]
    assert revoked["mode"] == "manual"
    assert revoked["grant"]["status"] == "revoked"
    after_revoke = _action(root / "after-revoke.json", suffix="after-revoke", action_kind="release")
    denied = invoke_cli(
        state,
        "mode",
        "use",
        "--action",
        str(after_revoke),
        "--expected-goal-version",
        str(revoked["goal_version"]),
        "--at",
        "2026-01-01T00:13:00Z",
        expected=2,
    )
    assert denied["error"]["code"] == "grant_revoked"

    expired_state, expired_store, _ = create_context(root, "expired")
    expired_store.close()
    _authorize(expired_state, root / "expired")
    invoke_cli(
        expired_state,
        "mode",
        "transition",
        "--goal-id",
        "goal:synthetic-delivery",
        "--expected-goal-version",
        "1",
        "--state",
        "ready_to_land",
        "--at",
        AT,
    )
    expired = invoke_cli(
        expired_state,
        "mode",
        "use",
        "--action",
        str(_action(root / "expired-action.json", suffix="expired")),
        "--expected-goal-version",
        "2",
        "--at",
        "2026-01-02T00:00:00Z",
        expected=2,
    )
    assert expired["error"]["code"] == "grant_expired"


def test_every_numeric_limit_and_immutable_revision(root: Path) -> None:
    limit_cases = {
        "attempts": {"attempts": 9},
        "cost": {"cost_microunits": 5001},
        "files": {"changed_files": 13},
        "duration": {"duration_seconds": 3601},
    }
    for name, override in limit_cases.items():
        case_root = root / name
        state, store, _ = create_context(case_root, "context")
        store.close()
        grant_path = _grant(case_root / "grant.json")
        grant = json.loads(grant_path.read_text(encoding="utf-8"))
        grant["resource_boundary"] = {
            "changed_files": 100_000,
            "cost_microunits": 100_000,
        }
        write_json(grant_path, grant)
        invoke_cli(
            state,
            "mode",
            "authorize",
            "--grant",
            str(grant_path),
            "--expected-goal-version",
            "0",
            "--at",
            AT,
        )
        invoke_cli(
            state,
            "mode",
            "transition",
            "--goal-id",
            "goal:synthetic-delivery",
            "--expected-goal-version",
            "1",
            "--state",
            "ready_to_land",
            "--at",
            AT,
        )
        action_path = _action(case_root / "action.json", suffix=f"limit-{name}", **override)
        refused = invoke_cli(
            state,
            "mode",
            "use",
            "--action",
            str(action_path),
            "--expected-goal-version",
            "2",
            "--at",
            AT,
            expected=2,
        )
        assert refused["error"]["code"] == "mode_limit_exceeded", (name, refused)

    revision_root = root / "revision"
    state, store, _ = create_context(revision_root, "context")
    store.close()
    first_path = _grant(revision_root / "grant-1.json")
    invoke_cli(
        state,
        "mode",
        "authorize",
        "--grant",
        str(first_path),
        "--expected-goal-version",
        "0",
        "--at",
        AT,
    )
    second = json.loads(first_path.read_text(encoding="utf-8"))
    second["grant_id"] = "grant:synthetic:2"
    second["revision"] = 2
    second["scope"]["environments"].append("production")
    second_path = revision_root / "grant-2.json"
    write_json(second_path, second)
    revised = invoke_cli(
        state,
        "mode",
        "authorize",
        "--grant",
        str(second_path),
        "--expected-goal-version",
        "1",
        "--at",
        AT,
    )["result"]
    assert revised["grant"]["grant_id"] == "grant:synthetic:2"
    assert revised["grant"]["revision"] == 2
    stale = invoke_cli(
        state,
        "mode",
        "use",
        "--action",
        str(_action(revision_root / "stale.json", suffix="stale")),
        "--expected-goal-version",
        "2",
        "--at",
        AT,
        expected=2,
    )
    assert stale["error"]["code"] == "grant_stale"


def test_mode_records_survive_verified_backup_and_bounded_export(root: Path) -> None:
    state, store, _ = create_context(root, "transfer")
    store.close()
    authorized = _authorize(state, root)
    with SQLiteStorage(state) as source:
        backup = source.backup("backups/mode.sqlite3")
        assert backup["database_schema_version"] == 20
        inspection = json.loads(
            source.export_bytes(
                format_name="json", purpose="inspection", max_records=10_000
            )
        )
        rollback = json.loads(
            source.export_bytes(
                format_name="json", purpose="rollback", max_records=10_000
            )
        )
    inspected_grant = inspection["tables"]["authorization_grants"][0]
    assert inspected_grant["exact_goal"] == "[redacted]"
    restored_grant = rollback["tables"]["authorization_grants"][0]
    assert restored_grant["canonical_digest"] == authorized["grant"]["canonical_digest"]
    restored = root / "restored"
    restored.mkdir()
    shutil.copy2(state / "backups/mode.sqlite3", restored / DATABASE_NAME)
    with SQLiteStorage(restored) as recovered:
        status = recovered.mode_status("goal:synthetic-delivery", AT)
        assert status["mode"] == "autonomous_delivery"
        assert status["grant"]["canonical_digest"] == authorized["grant"]["canonical_digest"]


def test_one_grant_propagates_across_protected_gates_without_reprompt(
    root: Path,
) -> None:
    state, store, _ = create_context(root, "protected-gate-propagation")
    store.close()
    _authorize(state, root)
    callbacks: list[str] = []
    with SQLiteStorage(state) as active:
        executor = ProtectedGateExecutor(active)
        goal_version = 1
        cases = (
            ("shotcaller.create", "shotcaller_create"),
            ("squad.register", "squad_register"),
            ("assign.reconcile-legacy-display", "live_reconcile"),
            ("rollover.drain", "retire"),
        )
        for index, (gate_name, action_kind) in enumerate(cases, start=1):
            action = json.loads(
                _action(
                    root / f"protected-{index}.json",
                    suffix=f"protected-{index}",
                    action_kind=action_kind,
                    cost_microunits=0,
                    changed_files=0,
                    duration_seconds=1,
                ).read_text(encoding="utf-8")
            )
            receipt = executor.execute(
                gate_name=gate_name,
                gate_scope={"synthetic_target": f"target:{index}"},
                action=action,
                expected_goal_version=goal_version,
                at=AT,
                operation=lambda _, name=gate_name: callbacks.append(name)
                or {"gate": name, "result": "ok"},
            )
            assert receipt["mode_action"]["state"] == "succeeded"
            assert receipt["mode_action"]["goal_state"] == "implementing"
            assert receipt["protected_gate"]["gate_name"] == gate_name
            assert receipt["protected_gate"]["outcome"] == "succeeded"
            goal_version = receipt["mode_action"]["goal_version"]

        adjacent = json.loads(
            _action(
                root / "protected-adjacent.json",
                suffix="protected-adjacent",
                action_kind="teardown",
                cost_microunits=0,
                changed_files=0,
                duration_seconds=1,
            ).read_text(encoding="utf-8")
        )
        try:
            executor.execute(
                gate_name="cleanup.execute",
                gate_scope={"synthetic_target": "target:adjacent"},
                action=adjacent,
                expected_goal_version=goal_version,
                at=AT,
                operation=lambda _: callbacks.append("out-of-scope"),
            )
        except StorageRefusal as exc:
            assert exc.code == "action_not_allowed"
        else:
            raise AssertionError("an adjacent ungranted protected gate executed")

        assert callbacks == [name for name, _ in cases]
        assert active.connection.execute(
            "SELECT COUNT(*) FROM authorization_grants"
        ).fetchone()[0] == 1
        assert active.connection.execute(
            "SELECT COUNT(*) FROM protected_gate_uses"
        ).fetchone()[0] == 4
        assert active.connection.execute(
            "SELECT COUNT(*) FROM protected_gate_settlements"
        ).fetchone()[0] == 4
        assert active.connection.execute(
            "SELECT COUNT(*) FROM autonomous_action_uses"
        ).fetchone()[0] == 4
        rollback = json.loads(
            active.export_bytes(
                format_name="json", purpose="rollback", max_records=10_000
            )
        )
        assert len(rollback["tables"]["protected_gate_uses"]) == 4
        assert len(rollback["tables"]["protected_gate_settlements"]) == 4
        backup = active.backup("backups/protected-gates.sqlite3")
        assert backup["database_schema_version"] == 20
    recovered = root / "protected-gate-recovered"
    recovered.mkdir()
    shutil.copy2(
        state / "backups/protected-gates.sqlite3", recovered / DATABASE_NAME
    )
    with SQLiteStorage(recovered) as restored:
        assert restored.connection.execute(
            "SELECT COUNT(*) FROM protected_gate_uses"
        ).fetchone()[0] == 4
        assert restored.connection.execute(
            "SELECT COUNT(*) FROM protected_gate_settlements"
        ).fetchone()[0] == 4


def test_protected_gate_two_writer_cas_runs_one_operation(root: Path) -> None:
    state, store, _ = create_context(root, "protected-gate-cas")
    store.close()
    _authorize(state, root)
    barrier = threading.Barrier(2)
    callbacks: list[str] = []
    outcomes: list[str] = []

    def writer(suffix: str) -> None:
        action = json.loads(
            _action(
                root / f"protected-cas-{suffix}.json",
                suffix=f"protected-cas-{suffix}",
                action_kind="live_reconcile",
                cost_microunits=0,
                changed_files=0,
                duration_seconds=1,
            ).read_text(encoding="utf-8")
        )
        with SQLiteStorage(state, busy_timeout_ms=2000) as candidate:
            barrier.wait()
            try:
                ProtectedGateExecutor(candidate).execute(
                    gate_name="assign.reconcile-runtime",
                    gate_scope={"assignment_id": f"assignment:{suffix}"},
                    action=action,
                    expected_goal_version=1,
                    at=AT,
                    operation=lambda _: callbacks.append(suffix)
                    or {"assignment_id": f"assignment:{suffix}"},
                )
                outcomes.append("committed")
            except StorageRefusal as exc:
                outcomes.append(exc.code)

    threads = [threading.Thread(target=writer, args=(suffix,)) for suffix in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["committed", "goal_version_conflict"]
    assert len(callbacks) == 1
    with SQLiteStorage(state) as checked:
        assert checked.connection.execute(
            "SELECT COUNT(*) FROM protected_gate_uses"
        ).fetchone()[0] == 1
        assert checked.connection.execute(
            "SELECT COUNT(*) FROM protected_gate_settlements"
        ).fetchone()[0] == 1


def test_cli_protected_gate_consumes_and_settles_mode_authority(root: Path) -> None:
    state, store, _ = create_context(root, "protected-gate-cli")
    store.close()
    _authorize(state, root)
    action_path = _action(
        root / "protected-cli.json",
        suffix="protected-cli",
        action_kind="live_reconcile",
        cost_microunits=0,
        changed_files=0,
        duration_seconds=1,
    )
    calls: list[str] = []
    original = league_cli.HANDLERS["assign.reconcile-runtime"]

    def synthetic_handler(_, args):
        calls.append(args.assignment_id)
        return {"assignment_id": args.assignment_id, "state": "reconciled"}, None

    league_cli.HANDLERS["assign.reconcile-runtime"] = synthetic_handler
    try:
        incomplete = invoke_cli(
            state,
            "assign",
            "reconcile-runtime",
            "--assignment-id",
            "assignment:synthetic-cli",
            "--at",
            AT,
            "--mode-action",
            str(action_path),
            expected=2,
        )
        assert incomplete["error"]["code"] == "protected_gate_authority_incomplete"
        assert calls == []
        executed = invoke_cli(
            state,
            "assign",
            "reconcile-runtime",
            "--assignment-id",
            "assignment:synthetic-cli",
            "--at",
            AT,
            "--mode-action",
            str(action_path),
            "--expected-mode-goal-version",
            "1",
        )["result"]
    finally:
        league_cli.HANDLERS["assign.reconcile-runtime"] = original
    assert calls == ["assignment:synthetic-cli"]
    assert executed["operation"]["state"] == "reconciled"
    assert executed["mode_action"]["state"] == "succeeded"
    assert executed["protected_gate"]["gate_name"] == "assign.reconcile-runtime"
    assert executed["protected_gate"]["outcome"] == "succeeded"


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        test_manual_default_and_exact_grant_authorization(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        test_exact_action_use_settlement_and_repair_loop(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        test_scope_limits_revocation_expiry_and_two_writer_cas(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        test_every_numeric_limit_and_immutable_revision(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        test_one_grant_propagates_across_protected_gates_without_reprompt(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        test_protected_gate_two_writer_cas_runs_one_operation(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        test_cli_protected_gate_consumes_and_settles_mode_authority(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        test_mode_records_survive_verified_backup_and_bounded_export(Path(raw))
    print("PASS: autonomous delivery authorization and action lifecycle")


if __name__ == "__main__":
    main()
