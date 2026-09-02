#!/usr/bin/env python3
"""Focused migration, policy, backup, rollback, and corruption tests."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_store import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    SQLiteStorage,
    journal_policy,
)
from league.issue_first import issue_scope_digest, normalize_issue_title  # noqa: E402
from league.storage import PrepareAssignmentCommand, StorageRefusal  # noqa: E402
from storage_test_support import migrated_state  # noqa: E402


class InjectedCrash(RuntimeError):
    pass


def refused(operation, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def test_loaded_runtime_gate(root: Path) -> None:
    assert journal_policy((3, 51, 2)) == ("DELETE", "loaded_sqlite_below_3.51.3")
    assert journal_policy((3, 51, 3)) == ("WAL", None)
    assert journal_policy(None) == ("DELETE", "loaded_sqlite_version_unverifiable")
    assert journal_policy((3, 53, 4), request_wal=False) == ("DELETE", "wal_not_requested")
    _, receipt = migrated_state(root, "rollback", request_wal=False)
    assert receipt["policy"]["journal_mode"] == "DELETE"
    assert receipt["policy"]["wal_refusal"] == "wal_not_requested"


def test_transactional_upgrade_backup_and_rollback(root: Path) -> None:
    state, first = migrated_state(root, "upgrade", target_version=1)
    assert first["applied"] == [1]
    assert first["to_version"] == 1

    def crash(point: str) -> None:
        if point == "after_migration_2":
            raise InjectedCrash(point)

    try:
        with SQLiteStorage.for_migration(state) as store:
            store.migrate(backup_name="backups/pre-v2.sqlite3", fault=crash)
    except InjectedCrash:
        pass
    else:
        raise AssertionError("migration crash was not injected")
    assert (state / "backups/pre-v2.sqlite3").is_file()
    with SQLiteStorage.for_migration(state) as store:
        unchanged = store.migrate(target_version=1)
        assert unchanged["from_version"] == unchanged["to_version"] == 1
        upgraded = store.migrate(backup_name="backups/pre-v2-retry.sqlite3")
        assert upgraded["from_version"] == 1
        assert upgraded["to_version"] == CURRENT_SCHEMA_VERSION
        assert upgraded["applied"] == list(range(2, CURRENT_SCHEMA_VERSION + 1))
        assert upgraded["backup"]["database_schema_version"] == 1
        assert store.integrity()["ok"]
        indexes = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            "ix_tasks_coordinator_state",
            "ix_assignments_state",
            "ix_requests_unresolved",
            "ix_outbox_recipient_state",
            "ix_runtime_task_state",
            "ux_routing_escalation_child",
            "ix_cleanup_actions",
            "ix_projects_repository_key",
            "ix_project_alias_lookup",
            "ix_project_suggestion_squad",
            "ix_roster_tasks_project_state",
            "ix_events_occurred",
            "ix_callsign_queue_scan",
            "ux_squad_accepting_shotcaller",
            "ux_owner_changed_per_rollover",
            "ux_pending_squad_registration",
            "ux_pending_shotcaller_registration",
            "ux_hidden_dispatch_assignment",
            "ix_request_progress_latest",
            "ix_request_progress_due",
            "ix_report_requests_updated",
            "ix_activity_evidence_time",
            "ix_report_specs_created",
            "ix_repository_artifacts_task",
            "ux_live_runtime_session_identity",
            "ux_active_continuation_archive",
            "ix_thread_archives_issue",
            "ix_thread_incarnations_lineage",
            "ix_continuation_state",
            "ix_mode_actions_goal_state",
            "ix_mode_actions_reopen_receipt",
            "ix_mode_repairs_goal_state",
            "ix_issue_selection_receipts_repository_issue",
            "ix_issue_bindings_repository_issue",
            "ix_tasks_request_routing",
            "ix_prompts_recovery",
            "ix_runtime_owner_health",
            "ix_cursor_steering_state",
            "ux_provider_launch_one_project_fork",
            "ix_provider_restart_state",
            "ix_pi_session_migration_state",
            "ux_runtime_replacement_open_assignment",
            "ux_runtime_replacement_successor_agent",
            "ux_runtime_replacement_successor_runtime",
            "ix_stopped_agent_retirements_completed",
        } <= indexes
        assert [migration.version for migration in MIGRATIONS] == list(
            range(1, CURRENT_SCHEMA_VERSION + 1)
        )
        assert [(item.version, item.name, item.checksum) for item in MIGRATIONS[4:]] == [
            (5, "advisory-project-catalog-and-roster-indexes", "5477db9879d6a4a9a29bb8188b398bd6db9a7a786e40e86ab819a0a938790faf"),
            (6, "guarded-rollover-and-shuffled-callsign-queue", "879ef4addfe6725e31c31a5aa1db9078d7c066a26610eaa2753f749c6e53ab75"),
            (7, "bounded-reporting-and-outbound-privacy", "bebe90eb841eac2a0b42d3f89e321cb4f3f8b23b02d92febf5a4ea2a50727cde"),
            (8, "bounded-routing-policy-and-request-progress", "593e2cf05d0200463800b6be7cbf5918a9b5fc3304f793d2ec3fad30b538e80c"),
            (9, "repository-owned-artifact-publication", "9231da781de45a8e912cd7193034a0b1b56f3a13e5e737e5681f18f6c6e3c852"),
            (10, "prompt-runtime-quarantine", "b2f75ee64473d8be4188052258c4e69c9cd5f12df1adb8535d55c1b523633df5"),
            (11, "prompt-quarantine-watcher-generation", "211fb5b225a63065a4607bebf38171f57d182d12e32701ad882d47b7ce5845e4"),
            (12, "nullable-request-rollover-descendant-assignments", "65dbbea863761a0157e14fd2c15b8a09eb67e10ae89c71d7e4e9315d7dc1b8d2"),
            (13, "standalone-shotcaller-callsign-scope", "f429a924be1e26331d6f5535410bc390cd66bcd92f2890f231f4c2f08f3ef1cc"),
            (14, "immutable-prompt-provenance-current-owner", "e9afa0921c02d7464453b6fc24a4c73defb952d6cfc6d7829a7b502e81ff178c"),
            (15, "exact-stop-feedback-suppression", "5c7fed923ba5684c209350dab248d813fa313647229be2d373ff8cef78e91574"),
            (16, "issue-coupled-cleanup-and-exact-thread-continuation", "a7fee02de43dbbde897b67e44c00e37805bf82790917d2f5392be70e4143ef3f"),
            (17, "immutable-switched-rollover-snapshot-revisions", "69dabdd22e3a4d099eb574ff11833681188e53ccf0d6ac9d787d7ed1e9764b26"),
            (18, "scoped-autonomous-delivery-and-issue-first-assignment", "b517b9103fedcc0db8a1f0dd7d06d475f309f3a135d87356209ab34dbd957631"),
            (19, "agent-authored-request-reconciliation", "038abf84401775c692954c5eea8f12e2f19a23d6b67ab727245f651751f438c0"),
            (20, "autonomous-protected-gate-authority-propagation", "b36865213f931b6522f2f8c807dcea60c3949a08eab05772c6ad8567fbdcf71a"),
            (21, "cursor-steering-intent-receipt", "7f6029e3d16a361afda80eab6d04624f99a50a4d37a0a2a4bd0fca3fc471bd66"),
            (22, "pi-provider-launch-descriptor", "00db025c97fd622984900c4db9712a2e3f3ea34125bed2af770c1e04d8aed83f"),
            (23, "adapter-neutral-champion-runtime-replacement", "b7c70f0db8bd4ccc8135f7d3d8b7471a470220cad6b86e86614f67415df75251"),
            (24, "stopped-agent-total-retirement", "d870bffa84298f2e2148bb7ea7259bef23b746f52bd69f03383bf08b8a16c131"),
        ]
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_v5_to_v6_rebuild_rolls_back_and_initializes_shuffled_order(root: Path) -> None:
    state, _ = migrated_state(root, "v5-to-v6", target_version=5)
    with SQLiteStorage.for_migration(state) as store:
        store.connection.executemany(
            """
            INSERT INTO callsigns(callsign,pool_role,enabled,pool_position,last_released_at)
            VALUES(?,'champion',1,?,NULL)
            """,
            (("Alpha", 1), ("Beta", 2), ("Gamma", 3)),
        )

        def crash(point: str) -> None:
            if point == "after_migration_6":
                raise InjectedCrash(point)

        try:
            store.migrate(backup_name="backups/pre-v6.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("v6 migration crash was not injected")
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='callsign_queue'"
        ).fetchone()[0] == 0
        store.migrate(backup_name="backups/pre-v6-retry.sqlite3")
        order = [
            row[0]
            for row in store.connection.execute(
                """
                SELECT callsign FROM callsign_queue
                 WHERE pool_role='champion' ORDER BY queue_position
                """
            )
        ]
        assert order != sorted(order)
        assert store.integrity()["ok"]


def test_v6_to_v7_rolls_back_and_applies_privacy_defaults(root: Path) -> None:
    state, _ = migrated_state(root, "v6-to-v7", target_version=6)
    with SQLiteStorage.for_migration(state) as store:
        store.connection.execute(
            """
            INSERT INTO projects(project_id,repository,state,version,updated_at,summary)
            VALUES('project:v6','https://example.invalid/synthetic/v6.git','active',1,
                   '2026-01-01T00:00:00Z','Synthetic v6 project')
            """
        )

        def crash(point: str) -> None:
            if point == "after_migration_7":
                raise InjectedCrash(point)

        try:
            store.migrate(backup_name="backups/pre-v7.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("v7 migration crash was not injected")
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 6
        columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(projects)")
        }
        assert "export_policy" not in columns
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='report_specs'"
        ).fetchone()[0] == 0
        store.migrate(backup_name="backups/pre-v7-retry.sqlite3")
        project = store.connection.execute(
            """
            SELECT repository_visibility,export_policy,root_classification,
                   repository_classification FROM projects WHERE project_id='project:v6'
            """
        ).fetchone()
        assert tuple(project) == ("unknown", "deny", "local_only", "local_only")
        assert store.integrity()["ok"]


def test_v15_to_v16_rolls_back_before_thread_lineage_cutover(root: Path) -> None:
    state, _ = migrated_state(root, "v15-to-v16", target_version=15)
    with SQLiteStorage.for_migration(state) as store:
        assert store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_runtime_session_identity'"
        ).fetchone() is not None

        def crash(point: str) -> None:
            if point == "after_migration_16":
                raise InjectedCrash(point)

        try:
            store.migrate(backup_name="backups/pre-v16.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("v16 migration crash was not injected")
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 15
        assert store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_runtime_session_identity'"
        ).fetchone() is not None
        assert store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_lineages'"
        ).fetchone() is None

        receipt = store.migrate(backup_name="backups/pre-v16-retry.sqlite3")
        assert receipt["from_version"] == 15 and receipt["applied"] == [16]
        assert store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_live_runtime_session_identity'"
        ).fetchone() is not None
        assert store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_lineages'"
        ).fetchone() is not None
        assert store.integrity()["ok"]


def test_v21_to_v22_rolls_back_unified_pi_migration_tables(root: Path) -> None:
    state, _ = migrated_state(root, "v21-to-v22", target_version=21)
    with SQLiteStorage.for_migration(state) as store:
        def crash(point: str) -> None:
            if point == "after_migration_22":
                raise InjectedCrash(point)

        try:
            store.migrate(target_version=22, backup_name="backups/pre-v22.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("v22 migration crash was not injected")
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 21
        assert store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pi_session_migrations'"
        ).fetchone() is None
        receipt = store.migrate(target_version=22, backup_name="backups/pre-v22-retry.sqlite3")
        assert receipt["from_version"] == 21 and receipt["applied"] == [22]


def test_schema_refusals_without_test_sql(root: Path) -> None:
    future, _ = migrated_state(root, "future")
    database = future / "league.sqlite3"
    payload = bytearray(database.read_bytes())
    payload[60:64] = (CURRENT_SCHEMA_VERSION + 1).to_bytes(4, "big")
    database.write_bytes(payload)
    refused(lambda: SQLiteStorage(future), "schema_newer")

    drift, _ = migrated_state(root, "drift")
    database = drift / "league.sqlite3"
    payload = bytearray(database.read_bytes())
    checksum = MIGRATIONS[0].checksum.encode("ascii")
    offset = payload.find(checksum)
    assert offset >= 0
    payload[offset : offset + len(checksum)] = b"0" * len(checksum)
    database.write_bytes(payload)
    refused(lambda: SQLiteStorage(drift), "migration_drift")


def test_v15_to_v16_rolls_back_before_thread_lineage_cutover(root: Path) -> None:
    source = ROOT / "src/league/sqlite_continuation_schema.py"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        "5fbe8039100354ac8c7ad4a3b0add87ed41b5e4b9c01fc86678d404146637d45"
    )
    state, _ = migrated_state(root, "v15-to-v16", target_version=15)
    with SQLiteStorage.for_migration(state) as store:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='index' AND name='ux_runtime_session_identity'"
        ).fetchone()[0] == 1

        def crash(point: str) -> None:
            if point == "after_migration_16":
                raise InjectedCrash(point)

        try:
            store.migrate(
                backup_name="backups/pre-v16.sqlite3",
                target_version=16,
                fault=crash,
            )
        except InjectedCrash:
            pass
        else:
            raise AssertionError("v16 migration crash was not injected")
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 15
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='index' AND name='ux_runtime_session_identity'"
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='thread_lineages'"
        ).fetchone()[0] == 0

        receipt = store.migrate(
            backup_name="backups/pre-v16-retry.sqlite3", target_version=16
        )
        assert receipt["from_version"] == 15
        assert receipt["to_version"] == 16
        assert receipt["applied"] == [16]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='index' AND name='ux_live_runtime_session_identity'"
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='thread_lineages'"
        ).fetchone()[0] == 1
        assert store.integrity()["ok"]


def test_v17_active_assignment_requires_migration18_issue_reconciliation(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "v17-active-assignment", target_version=17)
    at = "2026-01-01T00:00:00Z"
    repository = "https://example.invalid/repo.git"
    task_id = "task:v17-active"
    task_summary = "Implement authentication"
    with SQLiteStorage.for_migration(state) as store:
        store.connection.executemany(
            "INSERT INTO callsigns(callsign,pool_role,enabled,pool_position) VALUES(?,'champion',1,?)",
            (("Ashe", 1), ("Lux", 2)),
        )
        store.connection.execute(
            """
            INSERT INTO agent_instances
              (agent_id,callsign,role,kind,status,version,updated_at,update_text,next_action)
            VALUES('agent:ashe','Ashe','shotcaller','codex','working',1,?,'working','coordinate')
            """,
            (at,),
        )
        store.connection.execute(
            """
            INSERT INTO requests
              (request_id,summary,requester_agent_id,owner_agent_id,execution_mode,state,
               version,created_at,updated_at)
            VALUES('request:v17',?,'agent:ashe','agent:ashe','champion','in_progress',2,?,?)
            """,
            (task_summary, at, at),
        )
        store.connection.execute(
            "INSERT INTO tasks(task_id,summary,state,version,updated_at,request_id) VALUES(?,?,'working',3,?,'request:v17')",
            (task_id, task_summary, at),
        )
        store.connection.execute(
            """
            INSERT INTO agent_instances
              (agent_id,callsign,role,shotcaller_agent_id,task_id,kind,repository,issue,
               branch,worktree,status,version,updated_at,update_text,next_action)
            VALUES('agent:lux','Lux','champion','agent:ashe',?,'codex',?,81,
                   'agent/synthetic/81','/synthetic/worktree','working',1,?,'working','implement')
            """,
            (task_id, repository, at),
        )
        store.connection.execute(
            """
            INSERT INTO callsign_assignments
              (callsign_assignment_id,callsign,subject_id,agent_id,role,scope_kind,scope_id,
               state,queue_version,requirements_json,version,reserved_at,activated_at)
            VALUES('callsign-assignment:assignment:v17','Lux',?,'agent:lux','champion',
                   'task',?,'active',1,'[]',2,?,?)
            """,
            (task_id, task_id, at, at),
        )
        store.connection.execute(
            """
            INSERT INTO task_assignments
              (task_assignment_id,task_id,request_id,coordinator_agent_id,champion_agent_id,
               callsign,assignment_role,state,acceptance_receipt_json,version,created_at,updated_at)
            VALUES('assignment:v17',?,'request:v17','agent:ashe','agent:lux','Lux',
                   'champion','active','{}',4,?,?)
            """,
            (task_id, at, at),
        )
        migrated = store.migrate(
            backup_name="backups/pre-v18.sqlite3", target_version=18
        )
        assert migrated["from_version"] == 17 and migrated["applied"] == [18]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM repository_issue_bindings"
        ).fetchone()[0] == 0

        receipt = {
            "schema": "league.repository-issue.v1",
            "repository": repository,
            "repository_key": "example.invalid/repo",
            "issue": 81,
            "issue_url": "https://example.invalid/repo/issues/81",
            "issue_state": "open",
            "issue_title": task_summary,
            "normalized_title": normalize_issue_title(task_summary),
            "issue_body_digest": "a" * 64,
            "semantic_scope_digest": "b" * 64,
            "task_scope_digest": issue_scope_digest(
                repository, 81, task_id, task_summary
            ),
            "issue_selection_receipt_digest": "c" * 64,
            "verifier_kind": "synthetic-fixture",
            "verified_at": at,
        }
        receipt["receipt_digest"] = hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        refused(
            lambda: store.prepare_assignment(
                PrepareAssignmentCommand(
                    assignment_id="assignment:v17",
                    request_id="request:v17",
                    claim_token="claim:v17",
                    task_id=task_id,
                    task_summary=task_summary,
                    coordinator_agent_id="agent:ashe",
                    champion_agent_id="agent:lux",
                    repository=repository,
                    issue=81,
                    branch="agent/synthetic/81",
                    worktree="/synthetic/worktree",
                    at=at,
                    issue_receipt=receipt,
                )
            ),
            "assignment_issue_reconciliation_required",
        )


def test_v18_to_v19_rolls_back_request_reconciliation(root: Path) -> None:
    state, _ = migrated_state(root, "v18-to-v19", target_version=18)
    with SQLiteStorage.for_migration(state) as store:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='request_reconciliations'"
        ).fetchone()[0] == 0

        def crash(point: str) -> None:
            if point == "after_migration_19":
                raise InjectedCrash(point)

        try:
            store.migrate(backup_name="backups/pre-v19.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("v19 migration crash was not injected")
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 18
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='request_reconciliations'"
        ).fetchone()[0] == 0

        receipt = store.migrate(
            backup_name="backups/pre-v19-retry.sqlite3", target_version=19
        )
        assert receipt["from_version"] == 18
        assert receipt["to_version"] == 19
        assert receipt["applied"] == [19]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='request_reconciliations'"
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='index' AND name='ix_requests_owner_updated'"
        ).fetchone()[0] == 1
        assert store.integrity()["ok"]


def test_v19_to_v20_rolls_back_protected_gate_receipts(root: Path) -> None:
    state, _ = migrated_state(root, "v19-to-v20", target_version=19)
    with SQLiteStorage.for_migration(state) as store:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='protected_gate_uses'"
        ).fetchone()[0] == 0

        def crash(point: str) -> None:
            if point == "after_migration_20":
                raise InjectedCrash(point)

        try:
            store.migrate(backup_name="backups/pre-v20.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("v20 migration crash was not injected")
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 19
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='protected_gate_uses'"
        ).fetchone()[0] == 0

        receipt = store.migrate(
            target_version=20, backup_name="backups/pre-v20-retry.sqlite3"
        )
        assert receipt["from_version"] == 19
        assert receipt["to_version"] == 20
        assert receipt["applied"] == [20]
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"protected_gate_uses", "protected_gate_settlements"} <= tables
        action_columns = {
            row[1]
            for row in store.connection.execute(
                "PRAGMA table_info(autonomous_action_uses)"
            )
        }
        assert "goal_version_at_use" in action_columns
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='index' AND name='ix_protected_gate_uses_name_scope'"
        ).fetchone()[0] == 1
        triggers = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "protected_gate_uses_immutable_update",
            "protected_gate_uses_immutable_delete",
            "protected_gate_settlements_immutable_update",
            "protected_gate_settlements_immutable_delete",
        } <= triggers
        assert store.integrity()["ok"]


def test_v20_to_v21_cursor_steering_rolls_back_and_retries(root: Path) -> None:
    state, _ = migrated_state(root, "v20-to-v21", target_version=20)
    with SQLiteStorage.for_migration(state) as store:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='cursor_steering_effects'"
        ).fetchone()[0] == 0

        def crash(point: str) -> None:
            if point == "after_migration_21":
                raise InjectedCrash(point)

        try:
            store.migrate(backup_name="backups/pre-v21.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("v21 migration crash was not injected")
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 20
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='cursor_steering_effects'"
        ).fetchone()[0] == 0

        receipt = store.migrate(
            backup_name="backups/pre-v21-retry.sqlite3", target_version=21
        )
        assert receipt["from_version"] == 20
        assert receipt["to_version"] == 21
        assert receipt["applied"] == [21]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='cursor_steering_effects'"
        ).fetchone()[0] == 1
        assert store.integrity()["ok"]


def test_v3_upgrade_preserves_cleanup_and_indexes_legacy_project(root: Path) -> None:
    state, _ = migrated_state(root, "v3-cleanup", target_version=3)
    with SQLiteStorage.for_migration(state) as store:
        store.connection.execute(
            """
            INSERT INTO tasks(task_id,summary,state,version,updated_at)
            VALUES('task:v3-cleanup','synthetic v3 cleanup','completed',1,'2026-01-01T00:00:00Z')
            """
        )
        store.connection.execute(
            """
            INSERT INTO projects(project_id,repository,state,version,updated_at)
            VALUES('project:legacy','https://EXAMPLE.invalid/synthetic/legacy.git',
                   'active',1,'2026-01-01T00:00:00Z')
            """
        )
        store.connection.execute(
            """
            INSERT INTO cleanup_obligations
              (cleanup_obligation_id,task_id,cleanup_state,required_policy,next_action,version,updated_at)
            VALUES('cleanup:v3-cleanup','task:v3-cleanup','pending','terminal_task',
                   'Reconcile exact task resources',1,'2026-01-01T00:00:00Z')
            """
        )
        receipt = store.migrate(backup_name="backups/pre-v4.sqlite3")
        assert receipt["from_version"] == 3
        assert receipt["applied"] == list(range(4, CURRENT_SCHEMA_VERSION + 1))
        row = store.connection.execute(
            "SELECT * FROM cleanup_obligations WHERE task_id='task:v3-cleanup'"
        ).fetchone()
        assert row["cleanup_obligation_id"] == "cleanup:v3-cleanup"
        assert row["cleanup_state"] == "pending" and row["version"] == 1
        assert row["owner_id"] is None and row["task_class"] is None
        project = store.resolve_project("git@example.invalid:synthetic/legacy.git")
        assert project is not None and project["project_id"] == "project:legacy"
        repository_key = store.connection.execute(
            "SELECT repository_key FROM projects WHERE project_id='project:legacy'"
        ).fetchone()[0]
        assert repository_key == "example.invalid/synthetic/legacy"
        assert store.integrity()["ok"]


def test_backup_collision_and_corruption(root: Path) -> None:
    state, _ = migrated_state(root, "backup")
    with SQLiteStorage(state) as store:
        receipt = store.backup("backups/verified.sqlite3")
        assert receipt["integrity"] == "ok"
        assert len(receipt["sha256"]) == 64
        refused(lambda: store.backup("backups/verified.sqlite3"), "output_collision")

        def crash(point: str) -> None:
            if point == "after_backup_copy":
                raise InjectedCrash(point)

        try:
            store.backup("backups/retryable.sqlite3", fault=crash)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("backup crash was not injected")
        assert not (state / "backups/retryable.sqlite3").exists()
        assert store.backup("backups/retryable.sqlite3")["integrity"] == "ok"

    corrupt, _ = migrated_state(root, "corrupt")
    database = corrupt / "league.sqlite3"
    payload = bytearray(database.read_bytes())
    payload[:16] = b"not-a-database!!"
    database.write_bytes(payload)
    refused(lambda: SQLiteStorage(corrupt), "database_error")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-storage-migration-") as temporary:
        root = Path(temporary)
        test_loaded_runtime_gate(root)
        test_transactional_upgrade_backup_and_rollback(root)
        test_v5_to_v6_rebuild_rolls_back_and_initializes_shuffled_order(root)
        test_v6_to_v7_rolls_back_and_applies_privacy_defaults(root)
        test_v15_to_v16_rolls_back_before_thread_lineage_cutover(root)
        test_schema_refusals_without_test_sql(root)
        test_v17_active_assignment_requires_migration18_issue_reconciliation(root)
        test_v18_to_v19_rolls_back_request_reconciliation(root)
        test_v19_to_v20_rolls_back_protected_gate_receipts(root)
        test_v20_to_v21_cursor_steering_rolls_back_and_retries(root)
        test_v21_to_v22_rolls_back_unified_pi_migration_tables(root)
        test_v3_upgrade_preserves_cleanup_and_indexes_legacy_project(root)
        test_backup_collision_and_corruption(root)
    print("PASS: SQLite runtime gate, migrations, verified backup, rollback, drift, and corruption refusal")


if __name__ == "__main__":
    main()
