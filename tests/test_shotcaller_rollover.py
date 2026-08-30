#!/usr/bin/env python3
"""Guarded Shotcaller handoff, snapshot, CAS, crash, and drain coverage."""

from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_handoff_schema import (  # noqa: E402
    CHAMPION_SEED,
    SHOTCALLER_SEED,
    SHUFFLE_VERSION,
)
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.rollover_descendant import (  # noqa: E402
    HerdrDescendantRuntimeAdapter,
    RolloverDescendantService,
)
from league.storage import RuntimeRegistrationCommand, StorageRefusal  # noqa: E402
from storage_fixture import (  # noqa: E402
    CHAMPION_ID as IMPORTED_CHAMPION_ID,
    TASK_ID as IMPORTED_TASK_ID,
)
from storage_test_support import migrated_state, seeded_state  # noqa: E402


AT1 = "2026-01-01T00:00:00Z"
AT2 = "2026-01-01T00:01:00Z"
AT3 = "2026-01-01T00:02:00Z"
AT4 = "2026-01-01T00:03:00Z"
AT5 = "2026-01-01T00:04:00Z"
AT6 = "2026-01-01T00:05:00Z"
EXPIRES = "2026-01-01T01:00:00Z"
SQUAD_ID = "squad:synthetic"
OLD_ID = "agent:old-shotcaller"
NEW_ID = "agent:new-shotcaller"
OLD_ASSIGNMENT = "callsign-assignment:old-shotcaller"
NEW_ASSIGNMENT = "callsign-assignment:new-shotcaller"
LEAGUE = ROOT / "bin/league"


class InjectedCrash(RuntimeError):
    pass


def runtime_receipt(assignment: dict, suffix: str, caps: list[str]) -> dict:
    return {
        "schema": "league.runtime-acceptance.v1",
        "verified": True,
        "assignment_id": assignment["assignment_id"],
        "agent_id": assignment["agent_id"],
        "callsign": assignment["callsign"],
        "runtime_instance_id": f"runtime:{suffix}",
        "harness_kind": "synthetic",
        "backend_kind": "herdr",
        "session_identity": f"synthetic:{suffix}",
        "endpoint_identity": f"synthetic-endpoint:{suffix}",
        "endpoint_generation": f"generation:{suffix}",
        "routing_name": assignment["callsign"].lower(),
        "display_agent": "synthetic",
        "capabilities": caps,
    }


def descendant_runtime_receipt(
    target: dict, runtime_instance_id: str, terminal_id: str = "terminal:synthetic"
) -> dict:
    return {
        "schema": "league.rollover-descendant-runtime.v1",
        "verified": True,
        "champion_agent_id": target["champion_agent_id"],
        "task_id": target["task_id"],
        "runtime_instance_id": runtime_instance_id,
        "harness_kind": "codex-thread",
        "backend_kind": "herdr",
        "session_ref": target["thread_id"],
        "endpoint": target["address"],
        "runtime_generation": f"herdr:{terminal_id}",
        "status": "active",
        "callsign": target["callsign"],
        "routing_name": target["routing_name"],
        "display_agent": target["display_agent"],
        "worktree": target["worktree"],
        "terminal_id": terminal_id,
        "state_change_seq": 1,
        "snapshot_row_digest": target["snapshot_row_digest"],
        "capabilities": target["capabilities"],
    }


def plan(*, page_bound: int = 2, unsafe: bool = False) -> dict:
    value = {
        "schema": "league.shotcaller-handoff-plan.v1",
        "scope": {"kind": "squad", "id": SQUAD_ID},
        "authority": "Explicit same-scope replacement authority",
        "non_goals": ["No merge, deploy, install, or new task authority"],
        "unresolved": ["One synthetic coordination obligation"],
        "pending_decisions": [],
        "next_actions": ["Acknowledge the exact bounded handoff"],
        "obligations": ["Preserve active Champion bindings"],
        "policy_digest": "policy-digest-v1",
        "instruction_digest": "instruction-digest-v1",
        "expires_at": EXPIRES,
        "page_bound": page_bound,
    }
    if unsafe:
        value["next_actions"] = ["Read /Users/synthetic/private-state"]
    return value


def seed_rollover(store: SQLiteStorage, *, champion_count: int = 3) -> dict:
    store.reconcile_callsign_pool(
        "shotcaller",
        1,
        SHOTCALLER_SEED,
        SHUFFLE_VERSION,
        (
            {"callsign": "Garen", "enabled": True, "capabilities": ["rollover.accept"]},
            {"callsign": "Leona", "enabled": True, "capabilities": ["rollover.accept"]},
            {"callsign": "Shen", "enabled": True, "capabilities": ["rollover.accept"]},
        ),
        AT1,
    )
    old = store.allocate_callsign(
        OLD_ASSIGNMENT,
        OLD_ID,
        "shotcaller",
        "squad",
        SQUAD_ID,
        ["rollover.accept"],
        AT1,
    )
    store.activate_callsign(
        old["assignment_id"],
        1,
        runtime_receipt(old, "old-shotcaller", ["rollover.accept"]),
        AT1,
    )
    store.connection.execute(
        """
        INSERT INTO squads(squad_id,shotcaller_agent_id,state,version,updated_at,owner_fence)
        VALUES(?,?,'active',1,?,1)
        """,
        (SQUAD_ID, OLD_ID, AT1),
    )
    store.connection.execute(
        """
        INSERT INTO shotcaller_intake(agent_id,squad_id,state,fence,version,updated_at)
        VALUES(?,?,'accepting',1,1,?)
        """,
        (OLD_ID, SQUAD_ID, AT1),
    )
    champion_catalog = tuple(
        {
            "callsign": name,
            "enabled": True,
            "capabilities": ["task.execute"],
        }
        for name in ("Annie", "Braum", "Caitlyn", "Darius", "Ezreal")
    )
    store.reconcile_callsign_pool(
        "champion", 1, CHAMPION_SEED, SHUFFLE_VERSION, champion_catalog, AT1
    )
    champion_ids: list[str] = []
    for ordinal in range(champion_count):
        agent_id = f"agent:champion:{ordinal}"
        task_id = f"task:champion:{ordinal}"
        assignment = store.allocate_callsign(
            f"callsign-assignment:champion:{ordinal}",
            agent_id,
            "champion",
            "task",
            task_id,
            ["task.execute"],
            AT1,
        )
        store.activate_callsign(
            assignment["assignment_id"],
            1,
            runtime_receipt(assignment, f"champion:{ordinal}", ["task.execute"]),
            AT1,
        )
        store.connection.execute(
            """
            INSERT INTO tasks
              (task_id,summary,state,version,current_owner_agent_id,updated_at,champion_agent_id,
               coordinator_agent_id)
            VALUES(?,?,'working',1,?,?,?,?)
            """,
            (task_id, f"Synthetic Champion task {ordinal}", agent_id, AT1, agent_id, OLD_ID),
        )
        store.connection.execute(
            """
            UPDATE agent_instances SET task_id=?,shotcaller_agent_id=?,repository=?,issue=?,
                   branch=?,worktree=? WHERE agent_id=?
            """,
            (
                task_id,
                OLD_ID,
                "https://example.invalid/synthetic.git",
                800 + ordinal,
                f"agent/synthetic/{ordinal}",
                f"/synthetic/worktrees/{ordinal}",
                agent_id,
            ),
        )
        store.connection.execute(
            "INSERT INTO squad_champions(squad_id,champion_agent_id,joined_at) VALUES(?,?,?)",
            (SQUAD_ID, agent_id, AT1),
        )
        champion_ids.append(agent_id)
    successor = store.allocate_callsign(
        NEW_ASSIGNMENT,
        NEW_ID,
        "shotcaller",
        "squad",
        SQUAD_ID,
        ["rollover.accept"],
        AT2,
    )
    return {"old": old, "successor": successor, "champion_ids": champion_ids}


def mark_exact_imported_legacy_partial(
    store: SQLiteStorage, champion_agent_id: str, task_id: str
) -> None:
    """Reproduce the exact task/callsign shape emitted by the legacy importer."""

    callsign = store.agent_status(champion_agent_id)["callsign"]
    imported_assignment_id = "imported:" + hashlib.sha256(
        f"champion\0{callsign}".encode("utf-8")
    ).hexdigest()[:24]
    store.connection.execute(
        """
        UPDATE tasks
           SET request_id=NULL,champion_agent_id=NULL,coordinator_agent_id=NULL,
               current_owner_agent_id=?,current_owner_squad_id=NULL,state='active',version=1
         WHERE task_id=?
        """,
        (champion_agent_id, task_id),
    )
    store.connection.execute(
        "DELETE FROM task_assignments WHERE task_id=?", (task_id,)
    )
    store.connection.execute(
        """
        UPDATE callsign_assignments
           SET callsign_assignment_id=?,runtime_instance_id=NULL,requirements_json='[]',version=1
         WHERE agent_id=? AND role='champion'
        """,
        (imported_assignment_id, champion_agent_id),
    )
    store.connection.execute(
        "DELETE FROM runtime_instances WHERE actor_agent_id=?", (champion_agent_id,)
    )
    store.connection.execute(
        "INSERT INTO import_runs(run_id,report_digest,source_digest,applied_at) VALUES(?,?,?,?)",
        ("import:synthetic", "a" * 64, "b" * 64, AT1),
    )
    store.connection.execute(
        """
        INSERT INTO imported_artifacts
          (artifact_id,kind,digest,record_count,source_order,import_run_id)
        VALUES(?, 'roster', ?, 2, 0, 'import:synthetic')
        """,
        ("roster:synthetic", "c" * 64),
    )
    next_version = int(store.agent_status(champion_agent_id)["version"]) + 1
    event_id = f"agent:{champion_agent_id}:{next_version}"
    store.connection.execute(
        """
        INSERT INTO events
          (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
           update_text,occurred_at,detail_json,aggregate_kind,aggregate_id)
        VALUES(?, ?, NULL, NULL, ?, 'legacy_transition', 'working', ?, ?, '{}',
               'agent', ?)
        """,
        (
            event_id,
            champion_agent_id,
            next_version,
            "Synthetic imported legacy transition.",
            AT1,
            champion_agent_id,
        ),
    )
    store.connection.execute(
        "INSERT INTO legacy_event_aliases(legacy_event_id,event_id,source_order) VALUES(?,?,0)",
        ("legacy:synthetic:champion:0", event_id),
    )
    store.connection.execute(
        "UPDATE agent_instances SET version=? WHERE agent_id=?",
        (next_version, champion_agent_id),
    )


def imported_legacy_partial_shape(
    store: SQLiteStorage, champion_agent_id: str, task_id: str
) -> dict:
    """Normalize every field that distinguishes an imported task shell."""

    champion = store.agent_status(champion_agent_id)
    assert champion is not None
    task = store.connection.execute(
        "SELECT * FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    callsign = store.connection.execute(
        "SELECT * FROM callsign_assignments WHERE agent_id=? AND role='champion'",
        (champion_agent_id,),
    ).fetchall()
    runs = store.connection.execute("SELECT run_id FROM import_runs").fetchall()
    artifacts = store.connection.execute(
        "SELECT kind,import_run_id FROM imported_artifacts WHERE kind='roster'"
    ).fetchall()
    aliases = store.connection.execute(
        """
        SELECT e.agent_id,e.event_type
          FROM legacy_event_aliases a JOIN events e ON e.event_id=a.event_id
         WHERE e.agent_id=?
        """,
        (champion_agent_id,),
    ).fetchall()
    membership = store.connection.execute(
        """
        SELECT s.shotcaller_agent_id
          FROM squad_champions c JOIN squads s ON s.squad_id=c.squad_id
         WHERE c.champion_agent_id=?
        """,
        (champion_agent_id,),
    ).fetchall()
    outboxes = store.connection.execute(
        """
        SELECT o.outbox_id
          FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
         WHERE e.agent_id=? OR e.task_id=? ORDER BY o.outbox_id
        """,
        (champion_agent_id, task_id),
    ).fetchall()
    expected_assignment_id = "imported:" + hashlib.sha256(
        f"champion\0{champion['callsign']}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "task": {
            "request_id": task["request_id"],
            "state": task["state"],
            "version": int(task["version"]),
            "current_owner_is_champion": task["current_owner_agent_id"]
            == champion_agent_id,
            "current_owner_squad_id": task["current_owner_squad_id"],
            "champion_agent_id": task["champion_agent_id"],
            "coordinator_agent_id": task["coordinator_agent_id"],
        },
        "task_assignment_count": store.connection.execute(
            "SELECT COUNT(*) FROM task_assignments WHERE task_id=?", (task_id,)
        ).fetchone()[0],
        "callsign": {
            "count": len(callsign),
            "deterministic_id": len(callsign) == 1
            and callsign[0]["callsign_assignment_id"] == expected_assignment_id,
            "scope_exact": len(callsign) == 1
            and callsign[0]["scope_kind"] == "task"
            and callsign[0]["scope_id"] == task_id,
            "state": None if len(callsign) != 1 else callsign[0]["state"],
            "runtime_instance_id": None
            if len(callsign) != 1
            else callsign[0]["runtime_instance_id"],
            "requirements_json": None
            if len(callsign) != 1
            else callsign[0]["requirements_json"],
            "version": None if len(callsign) != 1 else int(callsign[0]["version"]),
        },
        "import_provenance": {
            "one_run": len(runs) == 1,
            "roster_present": bool(artifacts),
            "rosters_link_run": len(runs) == 1
            and all(row["import_run_id"] == runs[0]["run_id"] for row in artifacts),
            "legacy_alias_present": bool(aliases),
            "legacy_alias_exact": all(
                row["agent_id"] == champion_agent_id
                and row["event_type"] == "legacy_transition"
                for row in aliases
            ),
        },
        "runtime_count": store.connection.execute(
            "SELECT COUNT(*) FROM runtime_instances WHERE actor_agent_id=?",
            (champion_agent_id,),
        ).fetchone()[0],
        "squad_membership": len(membership) == 1
        and membership[0]["shotcaller_agent_id"] == champion["shotcaller_agent_id"],
        "outbox_ids": [row["outbox_id"] for row in outboxes],
    }


def test_manual_imported_legacy_partial_fixture_matches_supported_importer(
    root: Path,
) -> None:
    manual_state, _ = migrated_state(root, "manual-imported-shape")
    with SQLiteStorage(manual_state) as store:
        context = seed_rollover(store, champion_count=1)
        manual_champion_id = context["champion_ids"][0]
        manual_task_id = "task:champion:0"
        mark_exact_imported_legacy_partial(store, manual_champion_id, manual_task_id)
        manual_shape = imported_legacy_partial_shape(
            store, manual_champion_id, manual_task_id
        )

    _, imported_state, _ = seeded_state(root, "supported-imported-shape")
    with SQLiteStorage(imported_state) as store:
        supported_shape = imported_legacy_partial_shape(
            store, IMPORTED_CHAMPION_ID, IMPORTED_TASK_ID
        )

    assert manual_shape == supported_shape == {
        "task": {
            "request_id": None,
            "state": "active",
            "version": 1,
            "current_owner_is_champion": True,
            "current_owner_squad_id": None,
            "champion_agent_id": None,
            "coordinator_agent_id": None,
        },
        "task_assignment_count": 0,
        "callsign": {
            "count": 1,
            "deterministic_id": True,
            "scope_exact": True,
            "state": "active",
            "runtime_instance_id": None,
            "requirements_json": "[]",
            "version": 1,
        },
        "import_provenance": {
            "one_run": True,
            "roster_present": True,
            "rosters_link_run": True,
            "legacy_alias_present": True,
            "legacy_alias_exact": True,
        },
        "runtime_count": 0,
        "squad_membership": True,
        "outbox_ids": [],
    }


def prepare(store: SQLiteStorage, successor: dict) -> dict:
    return store.prepare_rollover(
        "rollover:synthetic",
        SQUAD_ID,
        OLD_ID,
        NEW_ID,
        successor["assignment_id"],
        1,
        1,
        "explicit",
        "authority-receipt-digest",
        ["rollover.accept"],
        plan(),
        AT2,
    )


def read_all_pages(store: SQLiteStorage, operation_id: str) -> list[dict]:
    pages: list[dict] = []
    cursor = None
    while True:
        page = store.rollover_bindings(
            operation_id, AT3, cursor=cursor, limit=2
        )
        pages.append(page["page"])
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


def acknowledge(store: SQLiteStorage, prepared: dict, pages: list[dict]) -> dict:
    snapshot = prepared["snapshot"]
    return store.acknowledge_rollover(
        prepared["operation_id"],
        NEW_ID,
        "runtime:new-shotcaller",
        prepared["handoff_digest"],
        snapshot["version"],
        snapshot["count"],
        snapshot["digest"],
        pages,
        AT4,
    )


def test_guarded_switch_crash_retry_and_drain(root: Path) -> None:
    state, _ = migrated_state(root, "switch")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store)
        incoming_owner = context["champion_ids"][0]
        store.connection.execute(
            """
            INSERT INTO requests
              (request_id,summary,requester_agent_id,owner_agent_id,return_to_agent_id,
               state,version,created_at,updated_at,pending_owner_agent_id,pending_owner_squad_id)
            VALUES('request:routed-before-rollover','Pending Squad route',?,?,?,
                   'routed',1,?,?,?,?)
            """,
            (
                incoming_owner,
                incoming_owner,
                incoming_owner,
                AT2,
                AT2,
                OLD_ID,
                SQUAD_ID,
            ),
        )
        before_bindings = {
            row["agent_id"]: tuple(row)
            for row in store.connection.execute(
                """
                SELECT agent_id,task_id,thread_id,repository,issue,branch,worktree
                  FROM agent_instances WHERE agent_id LIKE 'agent:champion:%' ORDER BY agent_id
                """
            )
        }
        prepared = prepare(store, context["successor"])
        assert prepared["state"] == "prepared"
        assert prepared["snapshot"]["count"] == len(context["champion_ids"])
        assert prepared["snapshot"]["page_bound"] == 2
        assert "rows" not in prepared["snapshot"]
        default_page = store.rollover_bindings(prepared["operation_id"], AT3)
        assert default_page["page"]["count"] == 2
        try:
            store.rollover_bindings(
                prepared["operation_id"],
                AT3,
                cursor=default_page["next_cursor"] + "x",
            )
        except StorageRefusal as exc:
            assert exc.code == "invalid_cursor"
        else:
            raise AssertionError("tampered snapshot cursor was accepted")
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        assert len(pages) == 2
        try:
            acknowledge(store, prepared, pages[:1])
        except StorageRefusal as exc:
            assert exc.code == "active_champion_snapshot_incomplete"
        else:
            raise AssertionError("partial active-Champion snapshot was acknowledged")
        acknowledged = acknowledge(store, prepared, pages)
        assert acknowledged["state"] == "acknowledged"

        def crash(point: str) -> None:
            if point == "after_owner_event":
                raise InjectedCrash(point)

        try:
            store.commit_rollover(
                prepared["operation_id"],
                1,
                1,
                "event:owner-changed",
                "outbox:owner-changed",
                AT5,
                fault=crash,
            )
        except InjectedCrash:
            pass
        else:
            raise AssertionError("owner-switch crash was not injected")
        squad = store.connection.execute(
            "SELECT shotcaller_agent_id,version,owner_fence FROM squads WHERE squad_id=?",
            (SQUAD_ID,),
        ).fetchone()
        assert tuple(squad) == (OLD_ID, 1, 1)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='owner_changed'"
        ).fetchone()[0] == 0
        assert store.rollover_status(prepared["operation_id"])["state"] == "acknowledged"

        switched = store.commit_rollover(
            prepared["operation_id"],
            1,
            1,
            "event:owner-changed",
            "outbox:owner-changed",
            AT5,
        )
        assert switched["state"] == "switched"
        retry = store.commit_rollover(
            prepared["operation_id"],
            1,
            1,
            "event:owner-changed",
            "outbox:owner-changed",
            AT5,
        )
        assert retry["idempotent"]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='owner_changed'"
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM delivery_outbox WHERE event_id='event:owner-changed'"
        ).fetchone()[0] == 1
        intake = {
            row["agent_id"]: row["state"]
            for row in store.connection.execute(
                "SELECT agent_id,state FROM shotcaller_intake WHERE squad_id=?", (SQUAD_ID,)
            )
        }
        assert intake == {OLD_ID: "draining", NEW_ID: "accepting"}
        pending = store.connection.execute(
            """
            SELECT owner_agent_id,pending_owner_agent_id,pending_owner_squad_id,version
              FROM requests WHERE request_id='request:routed-before-rollover'
            """
        ).fetchone()
        assert tuple(pending) == (incoming_owner, NEW_ID, SQUAD_ID, 2)
        try:
            store.intake_prompt(
                "prompt:old-after-switch",
                OLD_ID,
                "runtime:old-shotcaller",
                "synthetic",
                "synthetic:old-shotcaller",
                "source:old-after-switch",
                "Synthetic intake that the draining predecessor must refuse.",
                AT5,
            )
        except StorageRefusal as exc:
            assert exc.code == "owner_draining"
        else:
            raise AssertionError("draining predecessor accepted new intake")
        accepted_prompt = store.intake_prompt(
            "prompt:new-after-switch",
            NEW_ID,
            "runtime:new-shotcaller",
            "synthetic",
            "synthetic:new-shotcaller",
            "source:new-after-switch",
            "Synthetic intake accepted by the exact successor.",
            AT5,
        )
        assert accepted_prompt["prompt_id"] == "prompt:new-after-switch"
        after_bindings = {
            row["agent_id"]: tuple(row)
            for row in store.connection.execute(
                """
                SELECT agent_id,task_id,thread_id,repository,issue,branch,worktree
                  FROM agent_instances WHERE agent_id LIKE 'agent:champion:%' ORDER BY agent_id
                """
            )
        }
        assert after_bindings == before_bindings
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squad_champions WHERE squad_id=?", (SQUAD_ID,)
        ).fetchone()[0] == len(context["champion_ids"])

        store.connection.execute(
            "UPDATE runtime_instances SET status='closed' WHERE actor_agent_id=?", (OLD_ID,)
        )
        completed = store.complete_rollover_drain(
            prepared["operation_id"],
            switched["version"],
            {
                "schema": "league.rollover-drain-receipt.v1",
                "verified": True,
                "operation_id": prepared["operation_id"],
                "predecessor_agent_id": OLD_ID,
                "successor_agent_id": NEW_ID,
                "owner_event_id": "event:owner-changed",
                "archive_digest": "archive-digest",
                "resource_receipt_digest": "resource-receipt-digest",
                "callsign_release_receipt_digest": "callsign-release-digest",
            },
            AT6,
        )
        assert completed["state"] == "completed"
        assert store.connection.execute(
            "SELECT state FROM shotcaller_intake WHERE agent_id=?", (OLD_ID,)
        ).fetchone()[0] == "closed"
        assert store.connection.execute(
            "SELECT retired_at FROM agent_instances WHERE agent_id=?", (OLD_ID,)
        ).fetchone()[0] == AT6


def test_switched_rollover_reconciles_exact_imported_descendant(root: Path) -> None:
    state, _ = migrated_state(root, "reconcile-imported-descendant")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        champion_id = context["champion_ids"][0]
        task_id = "task:champion:0"
        mark_exact_imported_legacy_partial(store, champion_id, task_id)
        worktree = root / "reconcile-imported-descendant" / "champion-worktree"
        worktree.mkdir()
        worktree = worktree.resolve()
        thread_id = "11111111-2222-4333-8444-555555555555"
        store.connection.execute(
            "UPDATE callsign_assignments SET runtime_instance_id=NULL WHERE agent_id=?",
            (champion_id,),
        )
        store.connection.execute(
            "DELETE FROM runtime_instances WHERE actor_agent_id=?", (champion_id,)
        )
        store.connection.execute(
            """
            UPDATE agent_instances
               SET kind='codex-thread',thread_id=?,backend='herdr',routing_name='annie',
                   display_agent='codex',address='pane:champion:0',worktree=?
             WHERE agent_id=?
            """,
            (thread_id, str(worktree), champion_id),
        )
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        switched = store.commit_rollover(
            prepared["operation_id"],
            1,
            1,
            "event:owner-changed",
            "outbox:owner-changed",
            AT5,
        )
        for suffix, recipient in (
            ("already-successor", NEW_ID),
            ("still-predecessor", OLD_ID),
        ):
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
                   update_text,occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,?,NULL,NULL,1,'diagnostic','working',?,?, '{}','agent',?)
                """,
                (
                    f"event:descendant:{suffix}",
                    champion_id,
                    f"Synthetic descendant delivery {suffix}.",
                    AT5,
                    champion_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO delivery_outbox
                  (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                VALUES(?,?,?,'pending',?,0)
                """,
                (
                    f"outbox:descendant:{suffix}",
                    f"event:descendant:{suffix}",
                    recipient,
                    AT5,
                ),
            )
        before_snapshot = store.rollover_bindings(prepared["operation_id"], AT6)
        champion = store.agent_status(champion_id)
        assert champion is not None
        assert champion["shotcaller_agent_id"] == OLD_ID
        assert store.connection.execute(
            "SELECT 1 FROM task_assignments WHERE task_id=?", (task_id,)
        ).fetchone() is None

    inventory = {
        "result": {
            "agents": [
                {
                    "agent": "codex",
                    "agent_session": {"value": thread_id},
                    "agent_status": "working",
                    "interactive_ready": True,
                    "cwd": str(worktree),
                    "foreground_cwd": str(worktree),
                    "name": "annie",
                    "pane_id": "pane:champion:0",
                    "state_change_seq": 9,
                    "terminal_id": "terminal:champion:0",
                }
            ]
        }
    }

    class FakeHerdr:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, argv: tuple[str, ...], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(inventory), "")

    fake_herdr = FakeHerdr()
    with SQLiteStorage(state) as store:
        service = RolloverDescendantService(
            store, HerdrDescendantRuntimeAdapter(fake_herdr)
        )
        arguments = {
            "operation_id": prepared["operation_id"],
            "reconciliation_id": "reconciliation:synthetic:champion:0",
            "champion_agent_id": champion_id,
            "task_id": task_id,
            "runtime_instance_id": "runtime:champion:0:reconciled",
            "snapshot_digest": prepared["snapshot"]["digest"],
            "snapshot_row_digest": pages[0]["rows"][0]["row_digest"],
            "expected_rollover_version": switched["version"],
            "expected_agent_version": champion["version"],
            "expected_task_version": 1,
            "expected_assignment_version": 0,
            "expected_callsign_assignment_version": 1,
            "pending_outbox_ids": ("outbox:descendant:still-predecessor",),
            "at": AT6,
        }
        reconciled = service.reconcile(**arguments)
        retried = service.reconcile(**arguments)
    assert reconciled == {
        "champion_agent_id": champion_id,
        "created_assignment": True,
        "created_runtime": True,
        "idempotent": False,
        "operation_id": prepared["operation_id"],
        "pending_delivery_count": 2,
        "receipt_digest": reconciled["receipt_digest"],
        "reconciliation_id": "reconciliation:synthetic:champion:0",
        "retargeted_outbox_ids": ["outbox:descendant:still-predecessor"],
        "runtime_instance_id": "runtime:champion:0:reconciled",
        "successor_agent_id": NEW_ID,
        "task_id": task_id,
        "task_version": 2,
    }
    assert retried == {**reconciled, "idempotent": True}
    assert fake_herdr.calls == [("herdr", "agent", "list")]

    with SQLiteStorage(state) as store:
        assert store.rollover_bindings(prepared["operation_id"], AT6) == before_snapshot
        assert store.agent_status(champion_id)["shotcaller_agent_id"] == NEW_ID
        event_detail = json.loads(
            store.connection.execute(
                "SELECT detail_json FROM events WHERE event_id=?",
                ("reconciliation:synthetic:champion:0",),
            ).fetchone()[0]
        )
        assert event_detail["receipt_digest"] == reconciled["receipt_digest"]
        assert event_detail["receipt"]["source_shape"] == "imported_legacy_partial"
        assert len(event_detail["receipt"]["import_provenance_digest"]) == 64
        assert event_detail["receipt"]["reason"] == (
            "committed_rollover_imported_legacy_partial_binding"
        )
        assignment_receipt = json.loads(
            store.connection.execute(
                "SELECT acceptance_receipt_json FROM task_assignments WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
        )
        assert assignment_receipt == event_detail["receipt"]
        transitioned = store.transition_task(
            task_id,
            "runtime:champion:0:reconciled",
            2,
            "blocked",
            "Synthetic imported descendant now reaches the committed successor.",
            "Await the exact successor decision.",
            "Synthetic blocker.",
            "transition:synthetic:champion:0:2",
            "task:champion:0:blocked:2",
            "event:synthetic:champion:0:2",
            "outbox:synthetic:champion:0:2",
            NEW_ID,
            AT6,
        )
        assert transitioned["version"] == 3
        pending = store.connection.execute(
            "SELECT recipient_agent_id,state FROM delivery_outbox WHERE outbox_id=?",
            (transitioned["outbox_id"],),
        ).fetchone()
        assert tuple(pending) == (NEW_ID, "pending")


def test_imported_descendant_reconciliation_faults_roll_back_every_boundary(
    root: Path,
) -> None:
    fault_points = (
        "after_descendant_runtime",
        "after_descendant_assignment",
        "after_descendant_task",
        "after_descendant_callsign",
        "after_descendant_agent",
        "after_descendant_outbox",
        "before_descendant_event",
        "after_descendant_event",
    )
    for ordinal, fault_point in enumerate(fault_points):
        state, _ = migrated_state(root, f"imported-fault-{ordinal}")
        with SQLiteStorage(state) as store:
            context = seed_rollover(store, champion_count=1)
            champion_id = context["champion_ids"][0]
            task_id = "task:champion:0"
            mark_exact_imported_legacy_partial(store, champion_id, task_id)
            worktree = (root / f"imported-fault-worktree-{ordinal}").resolve()
            worktree.mkdir()
            thread_id = f"11111111-2222-4333-8444-{ordinal:012d}"
            store.connection.execute(
                """
                UPDATE agent_instances
                   SET kind='codex-thread',thread_id=?,backend='herdr',routing_name='annie',
                       display_agent='codex',address=?,worktree=?
                 WHERE agent_id=?
                """,
                (thread_id, f"pane:imported-fault:{ordinal}", str(worktree), champion_id),
            )
            prepared = prepare(store, context["successor"])
            store.activate_callsign(
                context["successor"]["assignment_id"],
                1,
                runtime_receipt(
                    context["successor"], "new-shotcaller", ["rollover.accept"]
                ),
                AT3,
            )
            pages = read_all_pages(store, prepared["operation_id"])
            acknowledge(store, prepared, pages)
            switched = store.commit_rollover(
                prepared["operation_id"],
                1,
                1,
                "event:owner-changed",
                "outbox:owner-changed",
                AT5,
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
                   update_text,occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,NULL,?,NULL,1,'diagnostic','working',?,?,'{}','task',?)
                """,
                (
                    f"event:imported-fault:{ordinal}",
                    task_id,
                    "Synthetic pending descendant delivery.",
                    AT5,
                    task_id,
                ),
            )
            outbox_id = f"outbox:imported-fault:{ordinal}"
            store.connection.execute(
                """
                INSERT INTO delivery_outbox
                  (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                VALUES(?,?,?,'pending',?,0)
                """,
                (outbox_id, f"event:imported-fault:{ordinal}", OLD_ID, AT5),
            )
            champion = store.agent_status(champion_id)
            target = store.rollover_descendant_target(
                prepared["operation_id"],
                f"reconciliation:imported-fault:{ordinal}",
                champion_id,
                task_id,
                prepared["snapshot"]["digest"],
                pages[0]["rows"][0]["row_digest"],
                switched["version"],
                champion["version"],
                1,
                0,
                1,
            )
            receipt = descendant_runtime_receipt(
                target, f"runtime:imported-fault:{ordinal}"
            )

            def crash(point: str) -> None:
                if point == fault_point:
                    raise InjectedCrash(point)

            try:
                store.reconcile_rollover_descendant(
                    prepared["operation_id"],
                    f"reconciliation:imported-fault:{ordinal}",
                    champion_id,
                    task_id,
                    f"runtime:imported-fault:{ordinal}",
                    prepared["snapshot"]["digest"],
                    pages[0]["rows"][0]["row_digest"],
                    switched["version"],
                    champion["version"],
                    1,
                    0,
                    1,
                    receipt,
                    (outbox_id,),
                    AT6,
                    fault=crash,
                )
            except InjectedCrash as exc:
                assert str(exc) == fault_point
            else:
                raise AssertionError(f"fault {fault_point} did not interrupt reconciliation")

            task = store.connection.execute(
                """
                SELECT champion_agent_id,coordinator_agent_id,current_owner_agent_id,version
                  FROM tasks WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
            assert tuple(task) == (None, None, champion_id, 1)
            assert store.connection.execute(
                "SELECT COUNT(*) FROM task_assignments WHERE task_id=?", (task_id,)
            ).fetchone()[0] == 0
            assert store.connection.execute(
                "SELECT COUNT(*) FROM runtime_instances WHERE actor_agent_id=?",
                (champion_id,),
            ).fetchone()[0] == 0
            callsign = store.connection.execute(
                """
                SELECT runtime_instance_id,version FROM callsign_assignments
                 WHERE agent_id=? AND role='champion'
                """,
                (champion_id,),
            ).fetchone()
            assert tuple(callsign) == (None, 1)
            agent = store.agent_status(champion_id)
            assert agent["shotcaller_agent_id"] == OLD_ID
            assert agent["version"] == champion["version"]
            outbox = store.connection.execute(
                "SELECT recipient_agent_id,state FROM delivery_outbox WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
            assert tuple(outbox) == (OLD_ID, "pending")
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_id=?",
                (f"reconciliation:imported-fault:{ordinal}",),
            ).fetchone()[0] == 0


def test_imported_descendant_requires_exact_import_provenance(root: Path) -> None:
    state, _ = migrated_state(root, "imported-provenance-refusal")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        champion_id = context["champion_ids"][0]
        task_id = "task:champion:0"
        mark_exact_imported_legacy_partial(store, champion_id, task_id)
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(context["successor"], "new-shotcaller", ["rollover.accept"]),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        switched = store.commit_rollover(
            prepared["operation_id"], 1, 1, "event:owner-changed", "outbox:owner-changed", AT5
        )
        champion = store.agent_status(champion_id)
        store.connection.execute("DELETE FROM imported_artifacts")
        try:
            store.rollover_descendant_target(
                prepared["operation_id"],
                "reconciliation:missing-import-proof",
                champion_id,
                task_id,
                prepared["snapshot"]["digest"],
                pages[0]["rows"][0]["row_digest"],
                switched["version"],
                champion["version"],
                1,
                0,
                1,
            )
        except StorageRefusal as exc:
            assert exc.code == "descendant_import_provenance_unverified"
        else:
            raise AssertionError("imported task shell without exact provenance was accepted")


def test_switched_rollover_reconciles_exact_predecessor_intake_and_obligations(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "reconcile-predecessor-intake")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=0)
        request_total = 3
        prompt_total = 4
        obligation_total = 2
        for ordinal in range(request_total):
            store.connection.execute(
                """
                INSERT INTO requests
                  (request_id,summary,requester_agent_id,owner_agent_id,return_to_agent_id,
                   state,version,created_at,updated_at)
                VALUES(?,?,?,?,?,'open',1,?,?)
                """,
                (
                    f"request:predecessor:{ordinal:02d}",
                    f"Synthetic predecessor request {ordinal}",
                    OLD_ID,
                    OLD_ID,
                    OLD_ID,
                    AT2,
                    AT2,
                ),
            )
        for ordinal in range(prompt_total):
            store.intake_prompt(
                f"prompt:predecessor:{ordinal:02d}",
                OLD_ID,
                "runtime:old-shotcaller",
                "codex",
                "synthetic:old-shotcaller",
                f"source:predecessor:{ordinal:02d}",
                f"Synthetic retained prompt {ordinal}.",
                AT2,
            )
        for ordinal in range(obligation_total):
            store.connection.execute(
                """
                INSERT INTO obligations
                  (obligation_id,owner_agent_id,kind,aggregate_id,dedupe_key,state,
                   next_attention_at,details_json,created_at,updated_at)
                VALUES(?,?,?,?,?,'open',NULL,'{}',?,?)
                """,
                (
                    f"obligation:predecessor:{ordinal}",
                    OLD_ID,
                    "request_followup",
                    f"request:predecessor:{ordinal:02d}",
                    f"rollover-predecessor:{ordinal}",
                    AT2,
                    AT2,
                ),
            )
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        switched = store.commit_rollover(
            prepared["operation_id"],
            1,
            1,
            "event:owner-changed",
            "outbox:owner-changed",
            AT5,
        )
        store.connection.execute(
            "UPDATE runtime_instances SET status='closed' WHERE actor_agent_id=?", (OLD_ID,)
        )
        try:
            store.complete_rollover_drain(
                prepared["operation_id"],
                switched["version"],
                {
                    "schema": "league.rollover-drain-receipt.v1",
                    "verified": True,
                    "operation_id": prepared["operation_id"],
                    "predecessor_agent_id": OLD_ID,
                    "successor_agent_id": NEW_ID,
                    "owner_event_id": "event:owner-changed",
                    "archive_digest": "archive-digest",
                    "resource_receipt_digest": "resource-receipt-digest",
                    "callsign_release_receipt_digest": "callsign-release-digest",
                },
                AT6,
            )
        except StorageRefusal as exc:
            assert exc.code == "drain_incomplete"
        else:
            raise AssertionError("predecessor drain ignored unreconciled intake")
        planned_intake = store.rollover_intake_plan(
            prepared["operation_id"],
            prepared["snapshot"]["digest"],
            switched["version"],
        )
        assert planned_intake["has_more"] is False
        plan_value = planned_intake["plan"]
        plan_path = root / "reconcile-predecessor-intake" / "intake-plan.json"
        plan_path.write_text(
            json.dumps(plan_value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    command = subprocess.run(
        [
            str(LEAGUE),
            "--state-root",
            str(state),
            "rollover",
            "reconcile-intake",
            "--operation-id",
            prepared["operation_id"],
            "--reconciliation-id",
            "reconciliation:predecessor-intake",
            "--snapshot-digest",
            prepared["snapshot"]["digest"],
            "--expected-rollover-version",
            str(switched["version"]),
            "--plan",
            str(plan_path),
            "--at",
            AT6,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert command.returncode == 0, command.stdout + command.stderr
    result = json.loads(command.stdout)["result"]
    assert result["request_count"] == request_total
    assert result["prompt_count"] == prompt_total
    assert result["obligation_count"] == obligation_total
    assert result["unresolved_count"] == request_total + prompt_total
    assert result["idempotent"] is False
    retry = subprocess.run(command.args, text=True, capture_output=True, check=False)
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert json.loads(retry.stdout)["result"] == {**result, "idempotent": True}

    with SQLiteStorage(state) as store:
        assert store.unresolved_requests(OLD_ID)["unresolved_count"] == 0
        successor = store.unresolved_requests(NEW_ID, limit=100)
        assert successor["unresolved_count"] == request_total + prompt_total
        assert successor["untriaged_prompt_count"] == prompt_total
        assert successor["open_obligation_count"] == obligation_total
        intake = store.untriaged_intake(NEW_ID, limit=100)
        assert intake["returned_count"] == prompt_total
        assert all(
            row["runtime_instance_id"] == "runtime:old-shotcaller"
            and row["owner_runtime_instance_id"] == "runtime:new-shotcaller"
            for row in intake["prompts"]
        )
        provenance = store.connection.execute(
            """
            SELECT intake_actor_id,runtime_instance_id,session_ref,source_event_key,
                   current_owner_agent_id,current_owner_runtime_instance_id
              FROM prompts WHERE prompt_id='prompt:predecessor:00'
            """
        ).fetchone()
        assert tuple(provenance) == (
            OLD_ID,
            "runtime:old-shotcaller",
            "synthetic:old-shotcaller",
            "source:predecessor:00",
            NEW_ID,
            "runtime:new-shotcaller",
        )
        triaged = store.triage_prompt(
            "prompt:predecessor:00",
            [
                {
                    "prompt_item_id": "prompt-item:inherited:00",
                    "ordinal": 1,
                    "summary": "Synthetic inherited prompt",
                    "disposition": "new_request",
                    "request_id": "request:inherited:00",
                }
            ],
            AT6,
        )
        assert triaged["request_count"] == 1
        inherited_request = store.connection.execute(
            "SELECT requester_agent_id,owner_agent_id FROM requests WHERE request_id=?",
            ("request:inherited:00",),
        ).fetchone()
        assert tuple(inherited_request) == (OLD_ID, NEW_ID)
        event = store.connection.execute(
            "SELECT detail_json FROM events WHERE event_type='rollover_intake_reconciled'"
        ).fetchone()
        receipt = json.loads(event["detail_json"])
        assert receipt["plan_digest"] == hashlib.sha256(
            json.dumps(plan_value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        assert receipt["original_prompt_runtime_digest"]


def test_intake_reconciliation_refuses_partial_or_stale_plan(root: Path) -> None:
    state, _ = migrated_state(root, "stale-intake-reconciliation")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=0)
        store.intake_prompt(
            "prompt:must-be-declared",
            OLD_ID,
            "runtime:old-shotcaller",
            "codex",
            "synthetic:old-shotcaller",
            "source:must-be-declared",
            "Synthetic prompt that cannot be silently omitted.",
            AT2,
        )
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        switched = store.commit_rollover(
            prepared["operation_id"], 1, 1, "event:owner-changed", "outbox:owner-changed", AT5
        )
        incomplete = {
            "schema": "league.rollover-intake-reconciliation.v1",
            "operation_id": prepared["operation_id"],
            "predecessor_agent_id": OLD_ID,
            "successor_agent_id": NEW_ID,
            "successor_runtime_instance_id": "runtime:new-shotcaller",
            "requests": [],
            "prompts": [],
            "obligations": [],
        }
        try:
            store.reconcile_rollover_intake(
                prepared["operation_id"],
                "reconciliation:partial-intake",
                prepared["snapshot"]["digest"],
                switched["version"],
                incomplete,
                AT6,
            )
        except StorageRefusal as exc:
            assert exc.code == "intake_reconciliation_stale"
        else:
            raise AssertionError("partial predecessor intake plan was accepted")
        assert store.unresolved_requests(OLD_ID)["untriaged_prompt_count"] == 1


def test_intake_reconciliation_pages_more_than_five_hundred_exact_records(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "paged-intake-reconciliation")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=0)
        total = 501
        store.connection.executemany(
            """
            INSERT INTO requests
              (request_id,summary,requester_agent_id,owner_agent_id,return_to_agent_id,
               state,version,created_at,updated_at)
            VALUES(?,?,?,?,?,'open',1,?,?)
            """,
            [
                (
                    f"request:paged:{ordinal:04d}",
                    f"Synthetic paged request {ordinal}",
                    OLD_ID,
                    OLD_ID,
                    OLD_ID,
                    AT2,
                    AT2,
                )
                for ordinal in range(total)
            ],
        )
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        switched = store.commit_rollover(
            prepared["operation_id"], 1, 1, "event:owner-changed", "outbox:owner-changed", AT5
        )
        first = store.rollover_intake_plan(
            prepared["operation_id"], prepared["snapshot"]["digest"], switched["version"]
        )
        assert first["has_more"] is True and first["counts"]["requests"] == 500
        first_result = store.reconcile_rollover_intake(
            prepared["operation_id"],
            "reconciliation:intake-page:1",
            prepared["snapshot"]["digest"],
            switched["version"],
            first["plan"],
            AT6,
        )
        assert first_result["has_more"] is True
        second = store.rollover_intake_plan(
            prepared["operation_id"], prepared["snapshot"]["digest"], switched["version"]
        )
        assert second["has_more"] is False and second["counts"]["requests"] == 1
        second_result = store.reconcile_rollover_intake(
            prepared["operation_id"],
            "reconciliation:intake-page:2",
            prepared["snapshot"]["digest"],
            switched["version"],
            second["plan"],
            AT6,
        )
        assert second_result["request_count"] == 1 and second_result["has_more"] is False
        assert store.unresolved_requests(OLD_ID)["unresolved_count"] == 0
        assert store.unresolved_requests(NEW_ID, limit=500)["unresolved_count"] == total


def test_successive_rollovers_preserve_original_prompt_provenance(root: Path) -> None:
    state, _ = migrated_state(root, "successive-intake-rollovers")
    third_id = "agent:third-shotcaller"
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=0)
        store.intake_prompt(
            "prompt:original-provenance",
            OLD_ID,
            "runtime:old-shotcaller",
            "codex",
            "synthetic:old-shotcaller",
            "source:original-provenance",
            "Synthetic prompt retained through two owner rollovers.",
            AT2,
        )
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        first_switch = store.commit_rollover(
            prepared["operation_id"], 1, 1, "event:owner-changed", "outbox:owner-changed", AT5
        )
        first_plan = store.rollover_intake_plan(
            prepared["operation_id"], prepared["snapshot"]["digest"], first_switch["version"]
        )
        store.reconcile_rollover_intake(
            prepared["operation_id"],
            "reconciliation:intake:first-successor",
            prepared["snapshot"]["digest"],
            first_switch["version"],
            first_plan["plan"],
            AT6,
        )

        third = store.allocate_callsign(
            "callsign-assignment:third-shotcaller",
            third_id,
            "shotcaller",
            "squad",
            SQUAD_ID,
            ["rollover.accept"],
            AT6,
        )
        second_prepared = store.prepare_rollover(
            "rollover:synthetic:second",
            SQUAD_ID,
            NEW_ID,
            third_id,
            third["assignment_id"],
            2,
            2,
            "explicit",
            "authority-receipt-digest:second",
            ["rollover.accept"],
            plan(),
            AT6,
        )
        store.activate_callsign(
            third["assignment_id"],
            1,
            runtime_receipt(third, "third-shotcaller", ["rollover.accept"]),
            AT6,
        )
        second_pages = read_all_pages(store, second_prepared["operation_id"])
        second_snapshot = second_prepared["snapshot"]
        store.acknowledge_rollover(
            second_prepared["operation_id"],
            third_id,
            "runtime:third-shotcaller",
            second_prepared["handoff_digest"],
            second_snapshot["version"],
            second_snapshot["count"],
            second_snapshot["digest"],
            second_pages,
            AT6,
        )
        second_switch = store.commit_rollover(
            second_prepared["operation_id"],
            2,
            2,
            "event:owner-changed:second",
            "outbox:owner-changed:second",
            AT6,
        )
        second_plan = store.rollover_intake_plan(
            second_prepared["operation_id"],
            second_snapshot["digest"],
            second_switch["version"],
        )
        store.reconcile_rollover_intake(
            second_prepared["operation_id"],
            "reconciliation:intake:second-successor",
            second_snapshot["digest"],
            second_switch["version"],
            second_plan["plan"],
            AT6,
        )
        provenance = store.connection.execute(
            """
            SELECT intake_actor_id,runtime_instance_id,session_ref,source_event_key,
                   current_owner_agent_id,current_owner_runtime_instance_id
              FROM prompts WHERE prompt_id='prompt:original-provenance'
            """
        ).fetchone()
        assert tuple(provenance) == (
            OLD_ID,
            "runtime:old-shotcaller",
            "synthetic:old-shotcaller",
            "source:original-provenance",
            third_id,
            "runtime:third-shotcaller",
        )
        rollback_export = json.loads(
            store.export_bytes(format_name="json", purpose="rollback", max_records=5000)
        )
        exported_prompt = next(
            row
            for row in rollback_export["tables"]["prompts"]
            if row["prompt_id"] == "prompt:original-provenance"
        )
        assert exported_prompt["intake_actor_id"] == OLD_ID
        assert exported_prompt["runtime_instance_id"] == "runtime:old-shotcaller"
        assert exported_prompt["current_owner_agent_id"] == third_id
        assert (
            exported_prompt["current_owner_runtime_instance_id"]
            == "runtime:third-shotcaller"
        )


def test_rollover_retargets_only_frozen_descendant_deliveries(root: Path) -> None:
    state, _ = migrated_state(root, "bounded-descendant-delivery")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        champion_id = context["champion_ids"][0]
        champion = store.agent_status(champion_id)
        assert champion is not None
        descendant = store.transition(
            champion_id,
            champion["version"],
            "working",
            "Synthetic frozen descendant update.",
            AT2,
        )
        store.connection.execute(
            """
            INSERT INTO events
              (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
               update_text,occurred_at,detail_json,aggregate_kind,aggregate_id)
            VALUES('event:predecessor-private',?,NULL,NULL,99,'diagnostic','working',
                   'Synthetic predecessor-private event.',?,'{}','agent',?)
            """,
            (OLD_ID, AT2, OLD_ID),
        )
        store.connection.execute(
            """
            INSERT INTO delivery_outbox
              (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
            VALUES('outbox:predecessor-private','event:predecessor-private',?,'pending',?,0)
            """,
            (OLD_ID, AT2),
        )
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        store.commit_rollover(
            prepared["operation_id"],
            1,
            1,
            "event:owner-changed",
            "outbox:owner-changed",
            AT5,
        )
        recipients = {
            row["outbox_id"]: row["recipient_agent_id"]
            for row in store.connection.execute(
                """
                SELECT outbox_id,recipient_agent_id FROM delivery_outbox
                 WHERE outbox_id IN (?,?) ORDER BY outbox_id
                """,
                (descendant["outbox_id"], "outbox:predecessor-private"),
            )
        }
        assert recipients == {
            descendant["outbox_id"]: OLD_ID,
            "outbox:predecessor-private": OLD_ID,
        }


def test_descendant_reconciliation_fences_claim_race_and_stale_versions(root: Path) -> None:
    state, _ = migrated_state(root, "descendant-claim-race")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        champion_id = context["champion_ids"][0]
        worktree = (root / "descendant-claim-race" / "champion-worktree").resolve()
        worktree.mkdir()
        thread_id = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        store.connection.execute(
            "UPDATE callsign_assignments SET runtime_instance_id=NULL WHERE agent_id=?",
            (champion_id,),
        )
        store.connection.execute(
            "DELETE FROM runtime_instances WHERE actor_agent_id=?", (champion_id,)
        )
        store.connection.execute(
            """
            UPDATE agent_instances SET kind='codex-thread',thread_id=?,backend='herdr',
                   routing_name='annie',display_agent='codex',address='pane:race',worktree=?
             WHERE agent_id=?
            """,
            (thread_id, str(worktree), champion_id),
        )
        champion = store.agent_status(champion_id)
        assert champion is not None
        descendant = store.transition(
            champion_id,
            champion["version"],
            "working",
            "Synthetic descendant delivery raced with a claim.",
            AT2,
        )
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        switched = store.commit_rollover(
            prepared["operation_id"], 1, 1, "event:owner-changed", "outbox:owner-changed", AT5
        )
        champion = store.agent_status(champion_id)
        assert champion is not None
        for assignment_version, callsign_version in ((1, 2), (0, 3)):
            try:
                store.rollover_descendant_target(
                    prepared["operation_id"],
                    f"reconciliation:stale-version:{assignment_version}:{callsign_version}",
                    champion_id,
                    "task:champion:0",
                    prepared["snapshot"]["digest"],
                    pages[0]["rows"][0]["row_digest"],
                    switched["version"],
                    champion["version"],
                    1,
                    assignment_version,
                    callsign_version,
                )
            except StorageRefusal as exc:
                assert exc.code == "version_conflict"
            else:
                raise AssertionError("stale descendant assignment version was accepted")

    class RacingAdapter:
        def verify(self, target: dict, runtime_instance_id: str) -> dict:
            with SQLiteStorage(state) as racer:
                racer.connection.execute(
                    "UPDATE delivery_outbox SET state='in_flight' WHERE outbox_id=? AND state='pending'",
                    (descendant["outbox_id"],),
                )
            return descendant_runtime_receipt(target, runtime_instance_id, "terminal:race")

    with SQLiteStorage(state) as store:
        service = RolloverDescendantService(store, RacingAdapter())
        try:
            service.reconcile(
                operation_id=prepared["operation_id"],
                reconciliation_id="reconciliation:claim-race",
                champion_agent_id=champion_id,
                task_id="task:champion:0",
                runtime_instance_id="runtime:champion:race",
                snapshot_digest=prepared["snapshot"]["digest"],
                snapshot_row_digest=pages[0]["rows"][0]["row_digest"],
                expected_rollover_version=switched["version"],
                expected_agent_version=champion["version"],
                expected_task_version=1,
                expected_assignment_version=0,
                expected_callsign_assignment_version=2,
                pending_outbox_ids=(descendant["outbox_id"],),
                at=AT6,
            )
        except StorageRefusal as exc:
            assert exc.code == "descendant_delivery_inflight"
        else:
            raise AssertionError("claimed descendant delivery was retargeted")
        task = store.connection.execute(
            "SELECT coordinator_agent_id,version FROM tasks WHERE task_id='task:champion:0'"
        ).fetchone()
        assert tuple(task) == (OLD_ID, 1)
        assert store.agent_status(champion_id)["shotcaller_agent_id"] == OLD_ID
        outbox = store.connection.execute(
            "SELECT recipient_agent_id,state FROM delivery_outbox WHERE outbox_id=?",
            (descendant["outbox_id"],),
        ).fetchone()
        assert tuple(outbox) == (OLD_ID, "in_flight")


def test_descendant_reconciliation_refuses_stale_snapshot_row(root: Path) -> None:
    state, _ = migrated_state(root, "stale-descendant-snapshot")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        switched = store.commit_rollover(
            prepared["operation_id"], 1, 1, "event:owner-changed", "outbox:owner-changed", AT5
        )
        champion_id = context["champion_ids"][0]
        champion = store.agent_status(champion_id)
        assert champion is not None
        try:
            store.reconcile_rollover_descendant(
                prepared["operation_id"],
                "reconciliation:stale-snapshot",
                champion_id,
                "task:champion:0",
                "runtime:champion:0",
                prepared["snapshot"]["digest"],
                "0" * 64,
                switched["version"],
                champion["version"],
                1,
                0,
                2,
                None,
                (),
                AT6,
            )
        except StorageRefusal as exc:
            assert exc.code == "descendant_snapshot_mismatch"
        else:
            raise AssertionError("stale descendant snapshot row was reconciled")
        assert store.agent_status(champion_id)["shotcaller_agent_id"] == OLD_ID


def test_descendant_runtime_adapter_refuses_missing_closed_mismatch_and_ambiguity(
    root: Path,
) -> None:
    worktree = (root / "descendant-adapter-refusals").resolve()
    worktree.mkdir()
    thread_id = "cccccccc-dddd-4eee-8fff-000000000000"
    target = {
        "champion_agent_id": "agent:synthetic:champion",
        "task_id": "task:synthetic:champion",
        "callsign": "Annie",
        "kind": "codex-thread",
        "thread_id": thread_id,
        "backend": "herdr",
        "routing_name": "annie",
        "display_agent": "codex",
        "address": "pane:synthetic",
        "worktree": str(worktree),
        "snapshot_row_digest": "a" * 64,
        "capabilities": ["task.execute"],
    }
    exact = {
        "agent": "codex",
        "agent_session": {"value": thread_id},
        "agent_status": "working",
        "interactive_ready": True,
        "cwd": str(worktree),
        "foreground_cwd": str(worktree),
        "name": "annie",
        "pane_id": "pane:synthetic",
        "state_change_seq": 2,
        "terminal_id": "terminal:synthetic",
    }

    class Inventory:
        def __init__(self, agents: list[dict]) -> None:
            self.agents = agents

        def run(self, argv: tuple[str, ...], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
            assert argv == ("herdr", "agent", "list") and timeout_seconds == 30
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"result": {"agents": self.agents}}), ""
            )

    cases = (
        ([], "descendant_runtime_missing"),
        ([{**exact, "agent_status": "closed"}], "descendant_runtime_closed"),
        ([{**exact, "interactive_ready": False}], "descendant_runtime_mismatch"),
        ([{key: value for key, value in exact.items() if key != "interactive_ready"}],
         "descendant_runtime_mismatch"),
        ([{**exact, "cwd": str(worktree / "other")}], "descendant_runtime_mismatch"),
        ([exact, {**exact, "pane_id": "pane:other"}], "descendant_runtime_ambiguous"),
    )
    for agents, code in cases:
        try:
            HerdrDescendantRuntimeAdapter(Inventory(agents)).verify(
                target, "runtime:synthetic:champion"
            )
        except StorageRefusal as exc:
            assert exc.code == code
        else:
            raise AssertionError(f"descendant runtime adapter accepted {code}")


def test_descendant_runtime_adapter_normalizes_exact_done_to_idle_only_after_identity(
    root: Path,
) -> None:
    worktree = (root / "descendant-adapter-done").resolve()
    worktree.mkdir()
    thread_id = "dddddddd-eeee-4fff-8000-111111111111"
    target = {
        "champion_agent_id": "agent:synthetic:done",
        "task_id": "task:synthetic:done",
        "callsign": "Annie",
        "kind": "codex-thread",
        "thread_id": thread_id,
        "backend": "herdr",
        "routing_name": "annie",
        "display_agent": "codex",
        "address": "pane:synthetic:done",
        "worktree": str(worktree),
        "snapshot_row_digest": "d" * 64,
        "capabilities": [],
    }
    exact = {
        "agent": "codex",
        "agent_session": {"value": thread_id},
        "agent_status": "done",
        "interactive_ready": True,
        "cwd": str(worktree),
        "foreground_cwd": str(worktree),
        "name": "annie",
        "pane_id": "pane:synthetic:done",
        "state_change_seq": 7,
        "terminal_id": "terminal:synthetic:done",
    }

    class Inventory:
        def __init__(self, agent: dict) -> None:
            self.agent = agent

        def run(
            self, argv: tuple[str, ...], *, timeout_seconds: int
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"result": {"agents": [self.agent]}}), ""
            )

    receipt = HerdrDescendantRuntimeAdapter(Inventory(exact)).verify(
        target, "runtime:synthetic:done"
    )
    assert receipt["status"] == "idle"
    for unready in (
        {**exact, "interactive_ready": False},
        {key: value for key, value in exact.items() if key != "interactive_ready"},
    ):
        try:
            HerdrDescendantRuntimeAdapter(Inventory(unready)).verify(
                target, "runtime:synthetic:done"
            )
        except StorageRefusal as exc:
            assert exc.code == "descendant_runtime_mismatch"
        else:
            raise AssertionError("unready done endpoint was normalized to idle")
    try:
        HerdrDescendantRuntimeAdapter(
            Inventory({**exact, "foreground_cwd": str(worktree / "other")})
        ).verify(target, "runtime:synthetic:done")
    except StorageRefusal as exc:
        assert exc.code == "descendant_runtime_mismatch"
    else:
        raise AssertionError("done endpoint normalized before full identity verification")


def test_descendant_reconciliation_refuses_ambiguous_runtime(root: Path) -> None:
    state, _ = migrated_state(root, "ambiguous-descendant-runtime")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        switched = store.commit_rollover(
            prepared["operation_id"], 1, 1, "event:owner-changed", "outbox:owner-changed", AT5
        )
        champion_id = context["champion_ids"][0]
        champion = store.agent_status(champion_id)
        assert champion is not None
        store.register_runtime(
            RuntimeRegistrationCommand(
                runtime_instance_id="runtime:champion:ambiguous",
                actor_agent_id=champion_id,
                harness_kind="synthetic-secondary",
                backend_kind="herdr",
                session_ref="synthetic:champion:ambiguous",
                endpoint="synthetic-endpoint:champion:ambiguous",
                runtime_generation="generation:champion:ambiguous",
                status="active",
                verified=True,
                at=AT6,
                capabilities=("task.execute",),
            )
        )
        try:
            store.reconcile_rollover_descendant(
                prepared["operation_id"],
                "reconciliation:ambiguous-runtime",
                champion_id,
                "task:champion:0",
                "runtime:champion:0",
                prepared["snapshot"]["digest"],
                pages[0]["rows"][0]["row_digest"],
                switched["version"],
                champion["version"],
                1,
                0,
                2,
                None,
                (),
                AT6,
            )
        except StorageRefusal as exc:
            assert exc.code == "descendant_runtime_ambiguous"
        else:
            raise AssertionError("ambiguous descendant runtime was reconciled")
        assert store.agent_status(champion_id)["shotcaller_agent_id"] == OLD_ID


def test_descendant_reconciliation_requires_exact_pending_delivery_set(root: Path) -> None:
    state, _ = migrated_state(root, "stale-descendant-delivery-set")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        champion_id = context["champion_ids"][0]
        worktree = (root / "stale-descendant-delivery-set" / "champion-worktree").resolve()
        worktree.mkdir()
        thread_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        store.connection.execute(
            "UPDATE callsign_assignments SET runtime_instance_id=NULL WHERE agent_id=?",
            (champion_id,),
        )
        store.connection.execute(
            "DELETE FROM runtime_instances WHERE actor_agent_id=?", (champion_id,)
        )
        store.connection.execute(
            """
            UPDATE agent_instances SET kind='codex-thread',thread_id=?,backend='herdr',
                   routing_name='annie',display_agent='codex',address='pane:stale',worktree=?
             WHERE agent_id=?
            """,
            (thread_id, str(worktree), champion_id),
        )
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        acknowledge(store, prepared, pages)
        switched = store.commit_rollover(
            prepared["operation_id"], 1, 1, "event:owner-changed", "outbox:owner-changed", AT5
        )
        champion = store.agent_status(champion_id)
        assert champion is not None
        store.connection.execute(
            """
            INSERT INTO events
              (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
               update_text,occurred_at,detail_json,aggregate_kind,aggregate_id)
            VALUES('event:descendant:undeclared',?,NULL,NULL,1,'diagnostic','working',
                   'Synthetic undeclared descendant event.',?,'{}','agent',?)
            """,
            (champion_id, AT5, champion_id),
        )
        store.connection.execute(
            """
            INSERT INTO delivery_outbox
              (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
            VALUES('outbox:descendant:undeclared','event:descendant:undeclared',?,'pending',?,0)
            """,
            (OLD_ID, AT5),
        )
        target = store.rollover_descendant_target(
            prepared["operation_id"],
            "reconciliation:stale-delivery-set",
            champion_id,
            "task:champion:0",
            prepared["snapshot"]["digest"],
            pages[0]["rows"][0]["row_digest"],
            switched["version"],
            champion["version"],
            1,
            0,
            2,
        )
        exact_runtime = {
            "schema": "league.rollover-descendant-runtime.v1",
            "verified": True,
            "champion_agent_id": champion_id,
            "task_id": "task:champion:0",
            "runtime_instance_id": "runtime:champion:0:reconciled",
            "harness_kind": "codex-thread",
            "backend_kind": "herdr",
            "session_ref": thread_id,
            "endpoint": "pane:stale",
            "runtime_generation": "herdr:synthetic-generation",
            "status": "active",
            "callsign": target["callsign"],
            "routing_name": "annie",
            "display_agent": "codex",
            "worktree": str(worktree),
            "terminal_id": "terminal:stale",
            "state_change_seq": 1,
            "snapshot_row_digest": pages[0]["rows"][0]["row_digest"],
            "capabilities": target["capabilities"],
        }
        try:
            store.reconcile_rollover_descendant(
                prepared["operation_id"],
                "reconciliation:stale-delivery-set",
                champion_id,
                "task:champion:0",
                "runtime:champion:0:reconciled",
                prepared["snapshot"]["digest"],
                pages[0]["rows"][0]["row_digest"],
                switched["version"],
                champion["version"],
                1,
                0,
                2,
                exact_runtime,
                (),
                AT6,
            )
        except StorageRefusal as exc:
            assert exc.code == "descendant_delivery_set_stale"
        else:
            raise AssertionError("missing pending descendant delivery was silently ignored")
        assert store.agent_status(champion_id)["shotcaller_agent_id"] == OLD_ID


def test_pre_switch_abort_restores_reservation(root: Path) -> None:
    state, _ = migrated_state(root, "abort")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=0)
        original_position = store.connection.execute(
            "SELECT queue_position FROM callsign_queue WHERE callsign=?",
            (context["successor"]["callsign"],),
        ).fetchone()[0]
        prepared = prepare(store, context["successor"])
        aborted = store.abort_rollover(
            prepared["operation_id"],
            prepared["version"],
            {
                "schema": "league.rollover-abort-receipt.v1",
                "verified": True,
                "operation_id": prepared["operation_id"],
                "successor_agent_id": NEW_ID,
                "runtime_instance_id": "not-created",
                "runtime_cleanup_receipt_digest": "not-created",
                "cleanup_digest": "no-runtime-created",
            },
            AT3,
        )
        assert aborted["state"] == "aborted"
        queue = store.connection.execute(
            "SELECT state,queue_position FROM callsign_queue WHERE callsign=?",
            (context["successor"]["callsign"],),
        ).fetchone()
        assert tuple(queue) == ("available", original_position)
        assert store.connection.execute(
            "SELECT shotcaller_agent_id FROM squads WHERE squad_id=?", (SQUAD_ID,)
        ).fetchone()[0] == OLD_ID
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='owner_changed'"
        ).fetchone()[0] == 0


def test_pre_switch_abort_releases_cleaned_active_successor(root: Path) -> None:
    state, _ = migrated_state(root, "abort-active")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=0)
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        store.connection.execute(
            "UPDATE runtime_instances SET status='closed' WHERE runtime_instance_id='runtime:new-shotcaller'"
        )
        aborted = store.abort_rollover(
            prepared["operation_id"],
            prepared["version"],
            {
                "schema": "league.rollover-abort-receipt.v1",
                "verified": True,
                "operation_id": prepared["operation_id"],
                "successor_agent_id": NEW_ID,
                "runtime_instance_id": "runtime:new-shotcaller",
                "runtime_cleanup_receipt_digest": "runtime-cleanup-digest",
                "cleanup_digest": "active-successor-cleanup-digest",
            },
            AT4,
        )
        assert aborted["state"] == "aborted"
        queue = store.callsign_status("shotcaller")["entries"]
        available = [item for item in queue if item["state"] == "available"]
        assert available[-1]["callsign"] == context["successor"]["callsign"]
        assert store.connection.execute(
            "SELECT shotcaller_agent_id FROM squads WHERE squad_id=?", (SQUAD_ID,)
        ).fetchone()[0] == OLD_ID


def test_public_safety_and_snapshot_staleness(root: Path) -> None:
    state, _ = migrated_state(root, "public-safety")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        try:
            store.prepare_rollover(
                "rollover:unsafe",
                SQUAD_ID,
                OLD_ID,
                NEW_ID,
                context["successor"]["assignment_id"],
                1,
                1,
                "explicit",
                "authority-digest",
                ["rollover.accept"],
                plan(unsafe=True),
                AT2,
            )
        except StorageRefusal as exc:
            assert exc.code == "handoff_unsafe"
        else:
            raise AssertionError("local path entered public handoff")
        prepared = prepare(store, context["successor"])
        try:
            store.rollover_bindings(prepared["operation_id"], "2026-01-01T02:00:00Z")
        except StorageRefusal as exc:
            assert exc.code == "active_champion_snapshot_stale"
        else:
            raise AssertionError("expired binding snapshot was read")


def test_acknowledgement_requires_the_accepted_runtime(root: Path) -> None:
    state, _ = migrated_state(root, "exact-runtime")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=0)
        prepared = prepare(store, context["successor"])
        store.activate_callsign(
            context["successor"]["assignment_id"],
            1,
            runtime_receipt(
                context["successor"], "new-shotcaller", ["rollover.accept"]
            ),
            AT3,
        )
        pages = read_all_pages(store, prepared["operation_id"])
        store.connection.execute(
            """
            INSERT INTO runtime_instances
              (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,
               endpoint,runtime_generation,status,verified,last_seen_at,capabilities_json)
            VALUES('runtime:replacement',?,'synthetic','herdr','synthetic:replacement',
                   'synthetic-endpoint:replacement','generation:replacement',
                   'active',1,?,'["rollover.accept"]')
            """,
            (NEW_ID, AT4),
        )
        store.connection.execute(
            """
            UPDATE agent_instances SET thread_id='synthetic:replacement',
                   address='synthetic-endpoint:replacement' WHERE agent_id=?
            """,
            (NEW_ID,),
        )
        snapshot = prepared["snapshot"]
        try:
            store.acknowledge_rollover(
                prepared["operation_id"],
                NEW_ID,
                "runtime:replacement",
                prepared["handoff_digest"],
                snapshot["version"],
                snapshot["count"],
                snapshot["digest"],
                pages,
                AT4,
            )
        except StorageRefusal as exc:
            assert exc.code == "successor_identity_mismatch"
        else:
            raise AssertionError("rollover acknowledged a runtime other than callsign acceptance")


def test_retired_squad_refuses_stale_accepting_intake(root: Path) -> None:
    state, _ = migrated_state(root, "retired-squad")
    with SQLiteStorage(state) as store:
        seed_rollover(store, champion_count=0)
        store.connection.execute(
            "UPDATE squads SET state='retired' WHERE squad_id=?", (SQUAD_ID,)
        )
        try:
            store.intake_prompt(
                "prompt:retired-squad",
                OLD_ID,
                "runtime:old-shotcaller",
                "synthetic",
                "synthetic:old-shotcaller",
                "source:retired-squad",
                "Synthetic intake that a retired Squad must refuse.",
                AT2,
            )
        except StorageRefusal as exc:
            assert exc.code == "owner_superseded"
        else:
            raise AssertionError("retired Squad accepted new intake")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-shotcaller-rollover-") as temporary:
        root = Path(temporary)
        test_manual_imported_legacy_partial_fixture_matches_supported_importer(root)
        test_guarded_switch_crash_retry_and_drain(root)
        test_switched_rollover_reconciles_exact_imported_descendant(root)
        test_imported_descendant_reconciliation_faults_roll_back_every_boundary(root)
        test_imported_descendant_requires_exact_import_provenance(root)
        test_switched_rollover_reconciles_exact_predecessor_intake_and_obligations(root)
        test_intake_reconciliation_refuses_partial_or_stale_plan(root)
        test_intake_reconciliation_pages_more_than_five_hundred_exact_records(root)
        test_successive_rollovers_preserve_original_prompt_provenance(root)
        test_rollover_retargets_only_frozen_descendant_deliveries(root)
        test_descendant_reconciliation_fences_claim_race_and_stale_versions(root)
        test_descendant_reconciliation_refuses_stale_snapshot_row(root)
        test_descendant_runtime_adapter_refuses_missing_closed_mismatch_and_ambiguity(root)
        test_descendant_runtime_adapter_normalizes_exact_done_to_idle_only_after_identity(root)
        test_descendant_reconciliation_refuses_ambiguous_runtime(root)
        test_descendant_reconciliation_requires_exact_pending_delivery_set(root)
        test_pre_switch_abort_restores_reservation(root)
        test_pre_switch_abort_releases_cleaned_active_successor(root)
        test_public_safety_and_snapshot_staleness(root)
        test_acknowledgement_requires_the_accepted_runtime(root)
        test_retired_squad_refuses_stale_accepting_intake(root)
    print(
        "PASS: bounded Shotcaller handoff, exact acknowledgement, atomic owner switch, crash retry, and drain"
    )


if __name__ == "__main__":
    main()
