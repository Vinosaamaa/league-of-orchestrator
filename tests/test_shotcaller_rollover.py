#!/usr/bin/env python3
"""Guarded Shotcaller handoff, snapshot, CAS, crash, and drain coverage."""

from __future__ import annotations

import json
import hashlib
import os
import shlex
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
from league.rollover_snapshot import (  # noqa: E402
    HerdrRolloverSnapshotAdapter,
    RolloverSnapshotRefreshService,
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
    champion_names = ("Annie", "Braum", "Caitlyn", "Darius", "Ezreal")
    if champion_count > len(champion_names):
        champion_names += ("Fizz", "Janna", "Karma")[: champion_count - 5]
    champion_catalog = tuple(
        {
            "callsign": name,
            "enabled": True,
            "capabilities": ["task.execute"],
        }
        for name in champion_names
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
        "INSERT OR IGNORE INTO import_runs(run_id,report_digest,source_digest,applied_at) VALUES(?,?,?,?)",
        ("import:synthetic", "a" * 64, "b" * 64, AT1),
    )
    store.connection.execute(
        """
        INSERT OR IGNORE INTO imported_artifacts
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
        (f"legacy:synthetic:champion:{task_id.rsplit(':', 1)[-1]}", event_id),
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


def switch_rollover(store: SQLiteStorage, context: dict) -> tuple[dict, dict]:
    for champion_id in context["champion_ids"]:
        runtime = store.connection.execute(
            "SELECT runtime_generation FROM runtime_instances WHERE actor_agent_id=?",
            (champion_id,),
        ).fetchone()
        champion = store.agent_status(champion_id)
        if runtime is not None and str(runtime["runtime_generation"]).startswith(
            "generation:champion:"
        ):
            terminal_id = f"terminal:{champion_id}"
            runtime_generation = "herdr:" + hashlib.sha256(
                f"{terminal_id}\0{champion['thread_id']}".encode("utf-8")
            ).hexdigest()[:24]
            store.connection.execute(
                "UPDATE runtime_instances SET runtime_generation=? WHERE actor_agent_id=?",
                (runtime_generation, champion_id),
            )
    prepared = prepare(store, context["successor"])
    store.activate_callsign(
        context["successor"]["assignment_id"],
        1,
        runtime_receipt(context["successor"], "new-shotcaller", ["rollover.accept"]),
        AT3,
    )
    acknowledged = acknowledge(store, prepared, read_all_pages(store, prepared["operation_id"]))
    assert acknowledged["state"] == "acknowledged"
    switched = store.commit_rollover(
        prepared["operation_id"],
        1,
        1,
        "event:owner-changed:refresh",
        "outbox:owner-changed:refresh",
        AT5,
    )
    return prepared, switched


class ExactSnapshotInventory:
    def observe(self, descendants: list[dict]) -> list[dict]:
        observations = []
        for target in descendants:
            terminal_id = f"terminal:{target['champion_agent_id']}"
            observations.append(
                {
                    "schema": "league.rollover-snapshot-observation.v1",
                    "verified": True,
                    "champion_agent_id": target["champion_agent_id"],
                    "task_id": target["task_id"],
                    "callsign": target["callsign"],
                    "thread_id": target["thread_id"],
                    "endpoint": target["address"],
                    "routing_name": target["routing_name"],
                    "worktree": target["worktree"],
                    "terminal_id": terminal_id,
                    "state_change_seq": 1,
                    "runtime_generation": (
                        target["runtime"]["runtime_generation"]
                        if target["runtime"] is not None
                        else "herdr:"
                        + hashlib.sha256(
                            f"{terminal_id}\0{target['thread_id']}".encode("utf-8")
                        ).hexdigest()[:24]
                    ),
                    "status": "idle",
                    "canonical_row_digest": target["canonical_row_digest"],
                }
            )
        return observations


class FakeHerdrInventory:
    def __init__(self, agents: list[dict]) -> None:
        self.agents = agents
        self.calls: list[tuple[str, ...]] = []

    def run(
        self, argv: tuple[str, ...], *, timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"result": {"agents": self.agents}}),
            "",
        )


def _seed_legacy_null_route_refresh(
    store: SQLiteStorage,
    root: Path,
    label: str,
    *,
    champion_count: int = 1,
    imported: bool = True,
) -> dict:
    context = seed_rollover(store, champion_count=champion_count)
    agents = []
    versions = {}
    for ordinal, champion_id in enumerate(context["champion_ids"]):
        task_id = f"task:champion:{ordinal}"
        if imported:
            mark_exact_imported_legacy_partial(store, champion_id, task_id)
        callsign = store.agent_status(champion_id)["callsign"]
        thread_id = f"22222222-3333-4444-8555-{ordinal:012d}"
        pane_id = f"pane:legacy-null-route:{label}:{ordinal}"
        terminal_id = f"terminal:legacy-null-route:{label}:{ordinal}"
        worktree = (root / f"legacy-null-route-{label}-{ordinal}").resolve()
        worktree.mkdir()
        store.connection.execute(
            """
            UPDATE agent_instances
               SET kind='codex-thread',thread_id=?,backend='herdr',address=?,worktree=?,
                   routing_name=NULL,display_agent=NULL
             WHERE agent_id=?
            """,
            (thread_id, pane_id, str(worktree), champion_id),
        )
        generation = "herdr:" + hashlib.sha256(
            f"{terminal_id}\0{thread_id}".encode("utf-8")
        ).hexdigest()[:24]
        if not imported:
            store.connection.execute(
                """
                UPDATE runtime_instances
                   SET harness_kind='codex-thread',backend_kind='herdr',session_ref=?,
                       endpoint=?,runtime_generation=?,status='idle',verified=1
                 WHERE actor_agent_id=?
                """,
                (thread_id, pane_id, generation, champion_id),
            )
        elif ordinal % 2:
            store.connection.execute(
                """
                INSERT INTO runtime_instances
                  (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,
                   endpoint,runtime_generation,status,verified,last_seen_at,capabilities_json)
                VALUES(?,?,'codex-thread','herdr',?,?,?,'idle',1,?,'["hook.capture"]')
                """,
                (
                    f"runtime:legacy-null-route:{label}:{ordinal}",
                    champion_id,
                    thread_id,
                    pane_id,
                    generation,
                    AT5,
                ),
            )
        versions[champion_id] = store.agent_status(champion_id)["version"]
        agents.append(
            {
                "agent": "codex",
                "agent_session": {"value": thread_id},
                "agent_status": "done" if ordinal % 2 else "working",
                "interactive_ready": True,
                "cwd": str(worktree),
                "foreground_cwd": str(worktree),
                "name": str(callsign).lower(),
                "pane_id": pane_id,
                "state_change_seq": ordinal + 1,
                "terminal_id": terminal_id,
            }
        )
    prepared, switched = switch_rollover(store, context)
    return {
        "context": context,
        "prepared": prepared,
        "switched": switched,
        "agents": agents,
        "versions": versions,
    }


def _legacy_null_route_inputs(seed: dict, label: str) -> dict:
    return {
        "operation_id": seed["prepared"]["operation_id"],
        "refresh_id": f"refresh:legacy-null-route:{label}",
        "squad_id": SQUAD_ID,
        "predecessor_agent_id": OLD_ID,
        "successor_agent_id": NEW_ID,
        "expected_rollover_version": seed["switched"]["version"],
        "expected_snapshot_version": seed["prepared"]["snapshot"]["version"],
        "expected_snapshot_digest": seed["prepared"]["snapshot"]["digest"],
        "expires_at": "2026-01-01T03:00:00Z",
        "at": "2026-01-01T02:00:00Z",
    }


def test_snapshot_refresh_adopts_eight_exact_imported_null_routes(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-eight-legacy-null-routes")
    with SQLiteStorage(state) as store:
        seed = _seed_legacy_null_route_refresh(
            store, root, "eight", champion_count=8
        )
        source_rows = {
            row["champion_agent_id"]: dict(row)
            for row in store.connection.execute(
                """
                SELECT * FROM active_champion_snapshot_rows WHERE snapshot_id=?
                 ORDER BY champion_agent_id
                """,
                (seed["prepared"]["snapshot"]["snapshot_id"],),
            )
        }
        seed["agents"][0]["routing_name"] = ""
        seed["agents"][1]["routing_alias"] = None
        runner = FakeHerdrInventory(seed["agents"])
        service = RolloverSnapshotRefreshService(
            store, HerdrRolloverSnapshotAdapter(runner)
        )
        inputs = _legacy_null_route_inputs(seed, "eight")

        refreshed = service.refresh(**inputs)
        retried = service.refresh(**inputs)

        assert refreshed["idempotent"] is False
        assert retried == {**refreshed, "idempotent": True}
        assert len(refreshed["route_adoptions"]) == 8
        assert runner.calls == [("herdr", "agent", "list")] * 2
        refreshed_rows = {
            row["champion_agent_id"]: dict(row)
            for row in store.connection.execute(
                """
                SELECT * FROM active_champion_snapshot_rows WHERE snapshot_id=?
                 ORDER BY champion_agent_id
                """,
                (refreshed["snapshot"]["snapshot_id"],),
            )
        }
        for champion_id, expected_version in seed["versions"].items():
            champion = store.agent_status(champion_id)
            assert champion["routing_name"] == champion["callsign"].lower()
            assert champion["display_agent"] == "codex"
            assert champion["version"] == expected_version + 1
            adoption = next(
                item
                for item in refreshed["route_adoptions"]
                if item["champion_agent_id"] == champion_id
            )
            assert adoption["expected_agent_version"] == expected_version
            assert adoption["agent_version"] == expected_version + 1
            event = store.connection.execute(
                "SELECT entity_version,event_type,detail_json FROM events WHERE event_id=?",
                (adoption["event_id"],),
            ).fetchone()
            assert tuple(event)[:2] == (
                expected_version + 1,
                "rollover_descendant_route_adopted",
            )
            detail = json.loads(event["detail_json"])
            assert detail["receipt_digest"] == adoption["receipt_digest"]
            assert detail["receipt"]["routing_name"] == champion["callsign"].lower()
            assert source_rows[champion_id]["binding_digest"] != refreshed_rows[
                champion_id
            ]["binding_digest"]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='rollover_descendant_route_adopted'"
        ).fetchone()[0] == 8


def test_snapshot_refresh_null_route_refuses_live_guess_or_overlap(root: Path) -> None:
    cases = (
        ("title-only", "snapshot_refresh_live_mismatch"),
        ("mismatched-name", "snapshot_refresh_live_mismatch"),
        ("conflicting-explicit-route", "snapshot_refresh_live_mismatch"),
        ("route-overlap", "snapshot_refresh_live_ambiguous"),
        ("pane-overlap", "snapshot_refresh_live_ambiguous"),
        ("session-overlap", "snapshot_refresh_live_ambiguous"),
    )
    for label, expected_code in cases:
        state, _ = migrated_state(root, f"refresh-null-route-{label}")
        with SQLiteStorage(state) as store:
            seed = _seed_legacy_null_route_refresh(store, root, label)
            champion_id = seed["context"]["champion_ids"][0]
            agent = seed["agents"][0]
            if label == "title-only":
                agent["terminal_title"] = agent["name"]
                agent["name"] = None
            elif label == "mismatched-name":
                agent["name"] = "wrong-route"
            elif label == "conflicting-explicit-route":
                agent["routing_alias"] = "foreign"
            else:
                duplicate = dict(agent)
                duplicate["pane_id"] = f"pane:duplicate:{label}"
                duplicate["name"] = f"duplicate-{label}"
                duplicate["agent_session"] = {
                    "value": "99999999-8888-4777-8666-555555555555"
                }
                if label == "route-overlap":
                    duplicate["name"] = agent["name"]
                elif label == "pane-overlap":
                    duplicate["pane_id"] = agent["pane_id"]
                else:
                    duplicate["agent_session"] = agent["agent_session"]
                seed["agents"].append(duplicate)
            before = store.rollover_status(seed["prepared"]["operation_id"])
            try:
                RolloverSnapshotRefreshService(
                    store,
                    HerdrRolloverSnapshotAdapter(
                        FakeHerdrInventory(seed["agents"])
                    ),
                ).refresh(**_legacy_null_route_inputs(seed, label))
            except StorageRefusal as exc:
                assert exc.code == expected_code
            else:
                raise AssertionError(f"{label} null-route proof refreshed snapshot")
            champion = store.agent_status(champion_id)
            assert champion["routing_name"] is None
            assert champion["version"] == seed["versions"][champion_id]
            assert store.rollover_status(seed["prepared"]["operation_id"]) == before
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='rollover_descendant_route_adopted'"
            ).fetchone()[0] == 0


def test_snapshot_refresh_null_route_refuses_modern_or_successor_owner(
    root: Path,
) -> None:
    for label, imported in (("modern", False), ("successor", True)):
        state, _ = migrated_state(root, f"refresh-null-route-{label}")
        with SQLiteStorage(state) as store:
            seed = _seed_legacy_null_route_refresh(
                store, root, label, imported=imported
            )
            champion_id = seed["context"]["champion_ids"][0]
            if label == "successor":
                store.connection.execute(
                    """
                    UPDATE agent_instances
                       SET shotcaller_agent_id=?,version=version+1
                     WHERE agent_id=?
                    """,
                    (NEW_ID, champion_id),
                )
            try:
                RolloverSnapshotRefreshService(
                    store,
                    HerdrRolloverSnapshotAdapter(
                        FakeHerdrInventory(seed["agents"])
                    ),
                ).refresh(**_legacy_null_route_inputs(seed, label))
            except StorageRefusal as exc:
                assert exc.code == "snapshot_refresh_identity_changed"
            else:
                raise AssertionError(f"{label} null-route descendant was adopted")
            assert store.agent_status(champion_id)["routing_name"] is None
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='rollover_descendant_route_adopted'"
            ).fetchone()[0] == 0


def test_snapshot_refresh_null_route_cas_and_fault_restore_exact_state(
    root: Path,
) -> None:
    for label in ("agent-cas", "runtime-cas", "callsign-cas", "fault"):
        state, _ = migrated_state(root, f"refresh-null-route-{label}")
        with SQLiteStorage(state) as store:
            seed = _seed_legacy_null_route_refresh(
                store,
                root,
                label,
                champion_count=2 if label == "runtime-cas" else 1,
            )
            champion_id = seed["context"]["champion_ids"][
                1 if label == "runtime-cas" else 0
            ]
            callsign_versions_before = {
                row["agent_id"]: int(row["version"])
                for row in store.connection.execute(
                    "SELECT agent_id,version FROM callsign_assignments WHERE role='champion'"
                )
            }
            runtime_generations_before = {
                row["actor_agent_id"]: row["runtime_generation"]
                for row in store.connection.execute(
                    "SELECT actor_agent_id,runtime_generation FROM runtime_instances WHERE actor_agent_id LIKE 'agent:champion:%'"
                )
            }
            inputs = _legacy_null_route_inputs(seed, label)
            target = store.rollover_snapshot_refresh_target(**inputs)
            observations = HerdrRolloverSnapshotAdapter(
                FakeHerdrInventory(seed["agents"])
            ).observe(target["descendants"])

            def final_observer(_descendants: list[dict]) -> list[dict]:
                if label == "agent-cas":
                    store.connection.execute(
                        "UPDATE agent_instances SET version=version+1 WHERE agent_id=?",
                        (champion_id,),
                    )
                elif label == "runtime-cas":
                    store.connection.execute(
                        """
                        UPDATE runtime_instances SET runtime_generation='herdr:changed'
                         WHERE actor_agent_id=?
                        """,
                        (champion_id,),
                    )
                elif label == "callsign-cas":
                    store.connection.execute(
                        """
                        UPDATE callsign_assignments SET version=version+1
                         WHERE agent_id=? AND role='champion'
                        """,
                        (champion_id,),
                    )
                return observations

            def fault(point: str) -> None:
                if label == "fault" and point == "after_refresh_route_adoptions":
                    raise InjectedCrash(point)

            try:
                store.refresh_rollover_snapshot(
                    **inputs,
                    canonical_digest=target["canonical_digest"],
                    observations=observations,
                    final_observer=final_observer,
                    fault=fault,
                )
            except (StorageRefusal, InjectedCrash) as exc:
                if label != "fault":
                    assert isinstance(exc, StorageRefusal)
                    assert exc.code == "snapshot_refresh_concurrent_mutation"
                else:
                    assert str(exc) == "after_refresh_route_adoptions"
            else:
                raise AssertionError(f"{label} route-adoption boundary did not refuse")
            for restored_id in seed["context"]["champion_ids"]:
                champion = store.agent_status(restored_id)
                assert champion["routing_name"] is None
                assert champion["version"] == seed["versions"][restored_id]
            assert {
                row["agent_id"]: int(row["version"])
                for row in store.connection.execute(
                    "SELECT agent_id,version FROM callsign_assignments WHERE role='champion'"
                )
            } == callsign_versions_before
            assert {
                row["actor_agent_id"]: row["runtime_generation"]
                for row in store.connection.execute(
                    "SELECT actor_agent_id,runtime_generation FROM runtime_instances WHERE actor_agent_id LIKE 'agent:champion:%'"
                )
            } == runtime_generations_before
            assert store.rollover_status(seed["prepared"]["operation_id"])[
                "snapshot"
            ] == seed["prepared"]["snapshot"]
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='rollover_descendant_route_adopted'"
            ).fetchone()[0] == 0


def test_snapshot_refresh_cli_requires_the_exact_switched_identity() -> None:
    completed = subprocess.run(
        [str(LEAGUE), "rollover", "refresh-bindings", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    for option in (
        "--operation-id",
        "--refresh-id",
        "--squad-id",
        "--predecessor-agent-id",
        "--successor-agent-id",
        "--expected-rollover-version",
        "--expected-snapshot-version",
        "--expected-snapshot-digest",
        "--expires-at",
        "--at",
    ):
        assert option in completed.stdout


def test_snapshot_refresh_cli_runs_two_stable_herdr_inventories(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-cli")
    worktree = (root / "refresh-cli-worktree").resolve()
    worktree.mkdir()
    thread_id = "eeeeeeee-ffff-4aaa-8bbb-000000000000"
    pane_id = "pane:refresh-cli"
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        champion_id = context["champion_ids"][0]
        champion = store.agent_status(champion_id)
        route = champion["callsign"].lower()
        store.connection.execute(
            """
            UPDATE agent_instances
               SET kind='codex-thread',thread_id=?,address=?,backend='herdr',
                   routing_name=?,display_agent='codex',worktree=?
             WHERE agent_id=?
            """,
            (thread_id, pane_id, route, str(worktree), champion_id),
        )
        store.connection.execute(
            """
            UPDATE runtime_instances
               SET harness_kind='codex-thread',backend_kind='herdr',session_ref=?,endpoint=?,
                   runtime_generation=?,status='active',verified=1
             WHERE actor_agent_id=?
            """,
            (
                thread_id,
                pane_id,
                "herdr:"
                + hashlib.sha256(
                    f"terminal:refresh-cli\0{thread_id}".encode("utf-8")
                ).hexdigest()[:24],
                champion_id,
            ),
        )
        prepared, switched = switch_rollover(store, context)

    inventory = {
        "result": {
            "agents": [
                {
                    "agent": "codex",
                    "agent_session": {"value": thread_id},
                    "agent_status": "done",
                    "interactive_ready": True,
                    "cwd": str(worktree),
                    "foreground_cwd": str(worktree),
                    "name": route,
                    "pane_id": pane_id,
                    "state_change_seq": 3,
                    "terminal_id": "terminal:refresh-cli",
                }
            ]
        }
    }
    fake_bin = root / "refresh-cli-bin"
    fake_bin.mkdir()
    fake_herdr = fake_bin / "herdr"
    calls = root / "refresh-cli-herdr-calls"
    fake_herdr.write_text(
        "#!/bin/sh\nprintf 'observe\\n' >> "
        + shlex.quote(str(calls))
        + "\nprintf '%s\\n' "
        + shlex.quote(json.dumps(inventory))
        + "\n",
        encoding="utf-8",
    )
    fake_herdr.chmod(0o755)
    completed = subprocess.run(
        [
            str(LEAGUE),
            "--state-root",
            str(state),
            "rollover",
            "refresh-bindings",
            "--operation-id",
            prepared["operation_id"],
            "--refresh-id",
            "refresh:cli",
            "--squad-id",
            SQUAD_ID,
            "--predecessor-agent-id",
            OLD_ID,
            "--successor-agent-id",
            NEW_ID,
            "--expected-rollover-version",
            str(switched["version"]),
            "--expected-snapshot-version",
            str(prepared["snapshot"]["version"]),
            "--expected-snapshot-digest",
            prepared["snapshot"]["digest"],
            "--expires-at",
            "2026-01-01T03:00:00Z",
            "--at",
            "2026-01-01T02:00:00Z",
        ],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["command"] == "rollover.refresh-bindings"
    assert payload["result"]["snapshot"]["version"] == 2
    assert calls.read_text(encoding="utf-8").splitlines() == ["observe", "observe"]


def test_snapshot_refresh_adapter_requires_one_exact_live_identity(root: Path) -> None:
    worktree = (root / "snapshot-refresh-adapter").resolve()
    worktree.mkdir()
    thread_id = "dddddddd-eeee-4fff-8aaa-000000000000"
    target = {
        "champion_agent_id": "agent:refresh:champion",
        "task_id": "task:refresh:champion",
        "callsign": "Annie",
        "kind": "codex-thread",
        "thread_id": thread_id,
        "backend": "herdr",
        "routing_name": "annie",
        "display_agent": "codex",
        "address": "pane:refresh",
        "worktree": str(worktree),
        "canonical_row_digest": "a" * 64,
        "capabilities": ["task.execute"],
    }
    exact = {
        "agent": "codex",
        "agent_session": {"value": thread_id},
        "agent_status": "done",
        "interactive_ready": True,
        "cwd": str(worktree),
        "foreground_cwd": str(worktree),
        "name": "annie",
        "pane_id": "pane:refresh",
        "state_change_seq": 2,
        "terminal_id": "terminal:refresh",
    }

    class Inventory:
        def __init__(self, agents: list[dict]) -> None:
            self.agents = agents

        def run(
            self, argv: tuple[str, ...], *, timeout_seconds: int
        ) -> subprocess.CompletedProcess[str]:
            assert argv == ("herdr", "agent", "list") and timeout_seconds == 30
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"result": {"agents": self.agents}}), ""
            )

    observed = HerdrRolloverSnapshotAdapter(Inventory([exact])).observe([target])
    assert observed[0]["status"] == "idle"
    assert observed[0]["runtime_generation"] == "herdr:" + hashlib.sha256(
        f"terminal:refresh\0{thread_id}".encode("utf-8")
    ).hexdigest()[:24]
    cases = (
        ([], "snapshot_refresh_live_missing"),
        ([{**exact, "interactive_ready": False}], "snapshot_refresh_live_mismatch"),
        ([exact, {**exact, "pane_id": "pane:other"}], "snapshot_refresh_live_ambiguous"),
    )
    for agents, code in cases:
        try:
            HerdrRolloverSnapshotAdapter(Inventory(agents)).observe([target])
        except StorageRefusal as exc:
            assert exc.code == code
        else:
            raise AssertionError(f"Herdr refusal {code} was not enforced")
    for changed, malformed_agent in (
        ({**target, "address": None}, {**exact, "pane_id": None}),
        ({**target, "routing_name": None}, {**exact, "name": None}),
    ):
        try:
            HerdrRolloverSnapshotAdapter(Inventory([malformed_agent])).observe(
                [changed]
            )
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_live_mismatch"
        else:
            raise AssertionError("missing canonical Herdr locator passed refresh")


def test_snapshot_refresh_refuses_a_mismatched_canonical_runtime(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-runtime-mismatch")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        store.connection.execute(
            "UPDATE runtime_instances SET endpoint='pane:stale' WHERE actor_agent_id=?",
            (context["champion_ids"][0],),
        )
        prepared, switched = switch_rollover(store, context)
        try:
            RolloverSnapshotRefreshService(
                store, ExactSnapshotInventory()
            ).refresh(
                operation_id=prepared["operation_id"],
                refresh_id="refresh:runtime-mismatch",
                squad_id=SQUAD_ID,
                predecessor_agent_id=OLD_ID,
                successor_agent_id=NEW_ID,
                expected_rollover_version=switched["version"],
                expected_snapshot_version=prepared["snapshot"]["version"],
                expected_snapshot_digest=prepared["snapshot"]["digest"],
                expires_at="2026-01-01T03:00:00Z",
                at="2026-01-01T02:00:00Z",
            )
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_runtime_mismatch"
        else:
            raise AssertionError("mismatched canonical runtime refreshed the snapshot")


def _bind_exact_herdr_runtime_with_capabilities(
    store: SQLiteStorage,
    root: Path,
    champion_agent_id: str,
    runtime_capabilities: list[str],
) -> str:
    champion = store.agent_status(champion_agent_id)
    thread_id = "eeeeeeee-ffff-4aaa-8bbb-111111111111"
    pane_id = "pane:capability-superset"
    terminal_id = f"terminal:{champion_agent_id}"
    worktree = (root / "capability-superset-worktree").resolve()
    worktree.mkdir(exist_ok=True)
    generation = "herdr:" + hashlib.sha256(
        f"{terminal_id}\0{thread_id}".encode("utf-8")
    ).hexdigest()[:24]
    store.connection.execute(
        """
        UPDATE agent_instances
           SET kind='codex-thread',thread_id=?,backend='herdr',routing_name=?,
               display_agent='codex',address=?,worktree=?
         WHERE agent_id=?
        """,
        (
            thread_id,
            str(champion["callsign"]).lower(),
            pane_id,
            str(worktree),
            champion_agent_id,
        ),
    )
    store.connection.execute(
        """
        UPDATE runtime_instances
           SET harness_kind='codex-thread',backend_kind='herdr',session_ref=?,endpoint=?,
               runtime_generation=?,status='idle',verified=1,capabilities_json=?
         WHERE actor_agent_id=?
        """,
        (
            thread_id,
            pane_id,
            generation,
            json.dumps(runtime_capabilities, separators=(",", ":")),
            champion_agent_id,
        ),
    )
    return terminal_id


def test_runtime_capability_superset_refreshes_and_reconciles_without_downgrade(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "runtime-capability-superset")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        champion_id = context["champion_ids"][0]
        task_id = "task:champion:0"
        actual_capabilities = ["hook.capture", "task.execute"]
        terminal_id = _bind_exact_herdr_runtime_with_capabilities(
            store, root, champion_id, actual_capabilities
        )
        store.connection.execute(
            "UPDATE callsign_assignments SET requirements_json='[]' WHERE agent_id=?",
            (champion_id,),
        )
        prepared, switched = switch_rollover(store, context)

        refreshed = RolloverSnapshotRefreshService(
            store, ExactSnapshotInventory()
        ).refresh(
            operation_id=prepared["operation_id"],
            refresh_id="refresh:capability-superset",
            squad_id=SQUAD_ID,
            predecessor_agent_id=OLD_ID,
            successor_agent_id=NEW_ID,
            expected_rollover_version=switched["version"],
            expected_snapshot_version=prepared["snapshot"]["version"],
            expected_snapshot_digest=prepared["snapshot"]["digest"],
            expires_at="2026-01-01T03:00:00Z",
            at="2026-01-01T02:00:00Z",
        )
        assert refreshed["capability_bindings"] == [
            {
                "champion_agent_id": champion_id,
                "required_capabilities": [],
                "runtime_capabilities": actual_capabilities,
            }
        ]
        row = store.rollover_bindings(
            prepared["operation_id"], "2026-01-01T02:01:00Z"
        )["page"]["rows"][0]
        champion = store.agent_status(champion_id)
        callsign_version = store.connection.execute(
            "SELECT version FROM callsign_assignments WHERE agent_id=?",
            (champion_id,),
        ).fetchone()[0]
        target = store.rollover_descendant_target(
            prepared["operation_id"],
            "reconciliation:capability-superset",
            champion_id,
            task_id,
            refreshed["snapshot"]["digest"],
            row["row_digest"],
            refreshed["rollover_version"],
            champion["version"],
            1,
            0,
            callsign_version,
        )
        assert target["capabilities"] == actual_capabilities
        runtime_id = target["runtime"]["runtime_instance_id"]
        runtime = descendant_runtime_receipt(target, runtime_id, terminal_id)
        runtime["runtime_generation"] = target["runtime"]["runtime_generation"]
        inputs = (
            prepared["operation_id"],
            "reconciliation:capability-superset",
            champion_id,
            task_id,
            runtime_id,
            refreshed["snapshot"]["digest"],
            row["row_digest"],
            refreshed["rollover_version"],
            champion["version"],
            1,
            0,
            callsign_version,
        )
        first = store.reconcile_rollover_descendant(
            *inputs, runtime, (), "2026-01-01T02:02:00Z"
        )
        second = store.reconcile_rollover_descendant(
            *inputs, None, (), "2026-01-01T02:02:00Z"
        )
        assert first["receipt_digest"] == second["receipt_digest"]
        assert second["idempotent"] is True
        event = store.connection.execute(
            "SELECT detail_json FROM events WHERE event_id=?",
            ("reconciliation:capability-superset",),
        ).fetchone()
        receipt = json.loads(event["detail_json"])["receipt"]
        assert receipt["required_capabilities"] == []
        assert receipt["runtime_capabilities"] == actual_capabilities
        stored = store.connection.execute(
            "SELECT capabilities_json FROM runtime_instances WHERE runtime_instance_id=?",
            (runtime_id,),
        ).fetchone()[0]
        assert json.loads(stored) == actual_capabilities


def test_runtime_capability_contract_refuses_missing_and_unverified(
    root: Path,
) -> None:
    for label, runtime_capabilities, verified in (
        ("missing", ["task.execute"], 1),
        ("unverified", ["repo.write", "task.execute"], 0),
    ):
        state, _ = migrated_state(root, f"runtime-capability-{label}")
        with SQLiteStorage(state) as store:
            context = seed_rollover(store, champion_count=1)
            champion_id = context["champion_ids"][0]
            _bind_exact_herdr_runtime_with_capabilities(
                store, root, champion_id, runtime_capabilities
            )
            store.connection.execute(
                "UPDATE runtime_instances SET verified=? WHERE actor_agent_id=?",
                (verified, champion_id),
            )
            store.connection.execute(
                """
                UPDATE callsign_assignments SET requirements_json='["repo.write","task.execute"]'
                 WHERE agent_id=?
                """,
                (champion_id,),
            )
            prepared, switched = switch_rollover(store, context)
            try:
                RolloverSnapshotRefreshService(
                    store, ExactSnapshotInventory()
                ).refresh(
                    operation_id=prepared["operation_id"],
                    refresh_id=f"refresh:capability-{label}",
                    squad_id=SQUAD_ID,
                    predecessor_agent_id=OLD_ID,
                    successor_agent_id=NEW_ID,
                    expected_rollover_version=switched["version"],
                    expected_snapshot_version=prepared["snapshot"]["version"],
                    expected_snapshot_digest=prepared["snapshot"]["digest"],
                    expires_at="2026-01-01T03:00:00Z",
                    at="2026-01-01T02:00:00Z",
                )
            except StorageRefusal as exc:
                assert exc.code == "snapshot_refresh_runtime_mismatch"
            else:
                raise AssertionError(f"{label} runtime capability identity refreshed")
            status = store.rollover_status(prepared["operation_id"])
            assert status["version"] == switched["version"]
            assert status["snapshot"] == prepared["snapshot"]
            if label == "missing":
                row = store.rollover_bindings(
                    prepared["operation_id"], AT6
                )["page"]["rows"][0]
                champion = store.agent_status(champion_id)
                callsign_version = store.connection.execute(
                    "SELECT version FROM callsign_assignments WHERE agent_id=?",
                    (champion_id,),
                ).fetchone()[0]
                try:
                    store.rollover_descendant_target(
                        prepared["operation_id"],
                        "reconciliation:capability-missing",
                        champion_id,
                        "task:champion:0",
                        prepared["snapshot"]["digest"],
                        row["row_digest"],
                        switched["version"],
                        champion["version"],
                        1,
                        0,
                        callsign_version,
                    )
                except StorageRefusal as exc:
                    assert exc.code == "descendant_runtime_mismatch"
                else:
                    raise AssertionError(
                        "descendant target accepted a runtime missing requirements"
                    )


def test_descendant_reconciliation_refuses_capability_drift_and_unverified_runtime(
    root: Path,
) -> None:
    for label in ("changed", "unverified"):
        state, _ = migrated_state(root, f"descendant-capability-{label}")
        with SQLiteStorage(state) as store:
            context = seed_rollover(store, champion_count=1)
            champion_id = context["champion_ids"][0]
            task_id = "task:champion:0"
            actual_capabilities = ["hook.capture", "task.execute"]
            terminal_id = _bind_exact_herdr_runtime_with_capabilities(
                store, root, champion_id, actual_capabilities
            )
            store.connection.execute(
                "UPDATE callsign_assignments SET requirements_json='[]' WHERE agent_id=?",
                (champion_id,),
            )
            prepared, switched = switch_rollover(store, context)
            row = store.rollover_bindings(
                prepared["operation_id"], AT6
            )["page"]["rows"][0]
            champion = store.agent_status(champion_id)
            callsign_version = store.connection.execute(
                "SELECT version FROM callsign_assignments WHERE agent_id=?",
                (champion_id,),
            ).fetchone()[0]
            target = store.rollover_descendant_target(
                prepared["operation_id"],
                f"reconciliation:capability-{label}",
                champion_id,
                task_id,
                prepared["snapshot"]["digest"],
                row["row_digest"],
                switched["version"],
                champion["version"],
                1,
                0,
                callsign_version,
            )
            runtime_id = target["runtime"]["runtime_instance_id"]
            runtime = descendant_runtime_receipt(target, runtime_id, terminal_id)
            runtime["runtime_generation"] = target["runtime"]["runtime_generation"]
            if label == "changed":
                store.connection.execute(
                    "UPDATE runtime_instances SET capabilities_json=? WHERE runtime_instance_id=?",
                    (
                        '["hook.capture","repo.write","task.execute"]',
                        runtime_id,
                    ),
                )
            else:
                store.connection.execute(
                    "UPDATE runtime_instances SET verified=0 WHERE runtime_instance_id=?",
                    (runtime_id,),
                )
            try:
                store.reconcile_rollover_descendant(
                    prepared["operation_id"],
                    f"reconciliation:capability-{label}",
                    champion_id,
                    task_id,
                    runtime_id,
                    prepared["snapshot"]["digest"],
                    row["row_digest"],
                    switched["version"],
                    champion["version"],
                    1,
                    0,
                    callsign_version,
                    runtime,
                    (),
                    AT6,
                )
            except StorageRefusal as exc:
                assert exc.code == "descendant_runtime_mismatch"
            else:
                raise AssertionError(f"{label} canonical runtime reconciled")
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_id=?",
                (f"reconciliation:capability-{label}",),
            ).fetchone()[0] == 0
            assert store.agent_status(champion_id)["shotcaller_agent_id"] == OLD_ID


def test_snapshot_refresh_refuses_invalid_missing_runtime_generations_without_mutation(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "refresh-missing-runtime-generation")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        mark_exact_imported_legacy_partial(
            store, context["champion_ids"][0], "task:champion:0"
        )
        prepared, switched = switch_rollover(store, context)

        invalid_generations = (
            ("arbitrary", "herdr:arbitrary"),
            ("malformed", ""),
            (
                "changed",
                "herdr:"
                + hashlib.sha256(
                    b"terminal:other\0synthetic:champion:0"
                ).hexdigest()[:24],
            ),
        )
        for label, invalid_generation in invalid_generations:
            class InvalidGenerationInventory(ExactSnapshotInventory):
                def observe(self, descendants: list[dict]) -> list[dict]:
                    observations = super().observe(descendants)
                    observations[0]["runtime_generation"] = invalid_generation
                    return observations

            try:
                RolloverSnapshotRefreshService(
                    store, InvalidGenerationInventory()
                ).refresh(
                    operation_id=prepared["operation_id"],
                    refresh_id=f"refresh:missing-runtime-generation:{label}",
                    squad_id=SQUAD_ID,
                    predecessor_agent_id=OLD_ID,
                    successor_agent_id=NEW_ID,
                    expected_rollover_version=switched["version"],
                    expected_snapshot_version=prepared["snapshot"]["version"],
                    expected_snapshot_digest=prepared["snapshot"]["digest"],
                    expires_at="2026-01-01T03:00:00Z",
                    at="2026-01-01T02:00:00Z",
                )
            except StorageRefusal as exc:
                assert exc.code == "snapshot_refresh_live_proof_mismatch"
            else:
                raise AssertionError(
                    f"{label} missing-runtime generation refreshed the snapshot"
                )
            status = store.rollover_status(prepared["operation_id"])
            assert status["version"] == switched["version"]
            assert status["snapshot"] == prepared["snapshot"]
            assert store.connection.execute(
                "SELECT COUNT(*) FROM active_champion_snapshots WHERE operation_id=?",
                (prepared["operation_id"],),
            ).fetchone()[0] == 1
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='rollover_snapshot_refreshed'"
            ).fetchone()[0] == 0


def test_snapshot_refresh_requires_canonical_generation_to_match_observed_terminal(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "refresh-canonical-generation")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        prepared, switched = switch_rollover(store, context)

        class ChangedTerminalInventory(ExactSnapshotInventory):
            def observe(self, descendants: list[dict]) -> list[dict]:
                observations = super().observe(descendants)
                terminal_id = "terminal:other"
                observations[0]["terminal_id"] = terminal_id
                observations[0]["runtime_generation"] = "herdr:" + hashlib.sha256(
                    f"{terminal_id}\0{observations[0]['thread_id']}".encode("utf-8")
                ).hexdigest()[:24]
                return observations

        try:
            RolloverSnapshotRefreshService(
                store, ChangedTerminalInventory()
            ).refresh(
                operation_id=prepared["operation_id"],
                refresh_id="refresh:canonical-generation",
                squad_id=SQUAD_ID,
                predecessor_agent_id=OLD_ID,
                successor_agent_id=NEW_ID,
                expected_rollover_version=switched["version"],
                expected_snapshot_version=prepared["snapshot"]["version"],
                expected_snapshot_digest=prepared["snapshot"]["digest"],
                expires_at="2026-01-01T03:00:00Z",
                at="2026-01-01T02:00:00Z",
            )
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_live_proof_mismatch"
        else:
            raise AssertionError(
                "canonical generation detached from observed terminal refreshed snapshot"
            )
        status = store.rollover_status(prepared["operation_id"])
        assert status["version"] == switched["version"]
        assert status["snapshot"] == prepared["snapshot"]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM active_champion_snapshots WHERE operation_id=?",
            (prepared["operation_id"],),
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='rollover_snapshot_refreshed'"
        ).fetchone()[0] == 0


def test_switched_rollover_refreshes_only_the_expired_exact_snapshot(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-expired-snapshot")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=2)
        prepared, switched = switch_rollover(store, context)
        expired_rows = store.rollover_bindings(
            prepared["operation_id"], AT6
        )["page"]["rows"]
        refreshed = RolloverSnapshotRefreshService(
            store, ExactSnapshotInventory()
        ).refresh(
            operation_id=prepared["operation_id"],
            refresh_id="refresh:synthetic:1",
            squad_id=SQUAD_ID,
            predecessor_agent_id=OLD_ID,
            successor_agent_id=NEW_ID,
            expected_rollover_version=switched["version"],
            expected_snapshot_version=prepared["snapshot"]["version"],
            expected_snapshot_digest=prepared["snapshot"]["digest"],
            expires_at="2026-01-01T03:00:00Z",
            at="2026-01-01T02:00:00Z",
        )

        assert refreshed["schema"] == "league.rollover-snapshot-refresh.v1"
        assert refreshed["operation_id"] == prepared["operation_id"]
        assert refreshed["source_snapshot"] == prepared["snapshot"]
        assert refreshed["snapshot"]["version"] == 2
        assert refreshed["snapshot"]["snapshot_id"] != prepared["snapshot"]["snapshot_id"]
        assert refreshed["snapshot"]["expires_at"] == "2026-01-01T03:00:00Z"
        assert refreshed["rollover_version"] == switched["version"] + 1
        assert refreshed["descendant_count"] == 2
        assert refreshed["observation_digest"] == refreshed["final_observation_digest"]
        assert refreshed["idempotent"] is False
        page = store.rollover_bindings(
            prepared["operation_id"], "2026-01-01T02:01:00Z"
        )
        assert page["page"]["count"] == 2
        assert {row["row_digest"] for row in page["page"]["rows"]}.isdisjoint(
            row["row_digest"] for row in expired_rows
        )
        preserved = store.connection.execute(
            """
            SELECT snapshot_id,snapshot_version FROM active_champion_snapshots
             WHERE operation_id=? ORDER BY snapshot_version
            """,
            (prepared["operation_id"],),
        ).fetchall()
        assert [tuple(item) for item in preserved] == [
            (prepared["snapshot"]["snapshot_id"], 1),
            (refreshed["snapshot"]["snapshot_id"], 2),
        ]


def test_snapshot_refresh_refuses_before_expiry_without_live_observation(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-before-expiry")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        prepared, switched = switch_rollover(store, context)

        class UnexpectedInventory:
            def observe(self, descendants: list[dict]) -> list[dict]:
                raise AssertionError("live inventory ran before expiry validation")

        try:
            RolloverSnapshotRefreshService(store, UnexpectedInventory()).refresh(
                operation_id=prepared["operation_id"],
                refresh_id="refresh:too-early",
                squad_id=SQUAD_ID,
                predecessor_agent_id=OLD_ID,
                successor_agent_id=NEW_ID,
                expected_rollover_version=switched["version"],
                expected_snapshot_version=prepared["snapshot"]["version"],
                expected_snapshot_digest=prepared["snapshot"]["digest"],
                expires_at="2026-01-01T02:00:00Z",
                at=AT6,
            )
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_not_expired"
        else:
            raise AssertionError("unexpired rollover snapshot was refreshed")
        assert store.rollover_status(prepared["operation_id"])["snapshot"] == prepared["snapshot"]


def test_snapshot_refresh_refuses_a_changed_descendant_set(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-changed-set")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=2)
        prepared, switched = switch_rollover(store, context)
        store.connection.execute(
            "DELETE FROM squad_champions WHERE squad_id=? AND champion_agent_id=?",
            (SQUAD_ID, context["champion_ids"][1]),
        )
        try:
            RolloverSnapshotRefreshService(store, ExactSnapshotInventory()).refresh(
                operation_id=prepared["operation_id"],
                refresh_id="refresh:changed-set",
                squad_id=SQUAD_ID,
                predecessor_agent_id=OLD_ID,
                successor_agent_id=NEW_ID,
                expected_rollover_version=switched["version"],
                expected_snapshot_version=prepared["snapshot"]["version"],
                expected_snapshot_digest=prepared["snapshot"]["digest"],
                expires_at="2026-01-01T03:00:00Z",
                at="2026-01-01T02:00:00Z",
            )
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_set_changed"
        else:
            raise AssertionError("changed descendant set refreshed the snapshot")
        assert store.rollover_status(prepared["operation_id"])["snapshot"] == prepared["snapshot"]


def test_snapshot_refresh_retry_returns_the_identical_receipt(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-idempotent")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        prepared, switched = switch_rollover(store, context)
        service = RolloverSnapshotRefreshService(store, ExactSnapshotInventory())
        inputs = {
            "operation_id": prepared["operation_id"],
            "refresh_id": "refresh:idempotent",
            "squad_id": SQUAD_ID,
            "predecessor_agent_id": OLD_ID,
            "successor_agent_id": NEW_ID,
            "expected_rollover_version": switched["version"],
            "expected_snapshot_version": prepared["snapshot"]["version"],
            "expected_snapshot_digest": prepared["snapshot"]["digest"],
            "expires_at": "2026-01-01T03:00:00Z",
            "at": "2026-01-01T02:00:00Z",
        }
        first = service.refresh(**inputs)
        second = service.refresh(**inputs)
        assert first["receipt_digest"] == second["receipt_digest"]
        assert {key: value for key, value in first.items() if key != "idempotent"} == {
            key: value for key, value in second.items() if key != "idempotent"
        }
        assert first["idempotent"] is False
        assert second["idempotent"] is True


def _seed_partially_reconciled_refresh(
    store: SQLiteStorage,
    root: Path,
    label: str,
    *,
    outbox_count: int = 0,
    preexisting_assignment: bool = False,
    imported_legacy_partial: bool = False,
) -> dict:
    context = seed_rollover(store, champion_count=2)
    champion_id = context["champion_ids"][0]
    task_id = "task:champion:0"
    if imported_legacy_partial:
        assert preexisting_assignment is False
        mark_exact_imported_legacy_partial(store, champion_id, task_id)
        worktree = (root / f"partial-refresh-imported-{label}").resolve()
        worktree.mkdir()
        store.connection.execute(
            """
            UPDATE agent_instances
               SET kind='codex-thread',thread_id=?,backend='herdr',routing_name='annie',
                   display_agent='codex',address=?,worktree=?
             WHERE agent_id=?
            """,
            (
                f"11111111-2222-4333-8444-{hashlib.sha256(label.encode()).hexdigest()[:12]}",
                f"pane:partial-refresh-imported:{label}",
                str(worktree),
                champion_id,
            ),
        )
        terminal_id = f"terminal:{champion_id}"
    else:
        terminal_id = _bind_exact_herdr_runtime_with_capabilities(
            store, root, champion_id, ["hook.capture", "task.execute"]
        )
    assignment_version = 0
    if preexisting_assignment:
        champion = store.agent_status(champion_id)
        runtime_id = store.connection.execute(
            "SELECT runtime_instance_id FROM runtime_instances WHERE actor_agent_id=?",
            (champion_id,),
        ).fetchone()[0]
        store.connection.execute(
            """
            INSERT INTO task_assignments
              (task_assignment_id,task_id,request_id,coordinator_agent_id,
               champion_agent_id,runtime_instance_id,callsign,assignment_role,state,
               acceptance_receipt_json,cleanup_required,version,created_at,updated_at)
            VALUES(?,?,NULL,?,?,?,?, 'champion','active',?,0,1,?,?)
            """,
            (
                f"assignment:partial-refresh:{label}",
                task_id,
                OLD_ID,
                champion_id,
                runtime_id,
                champion["callsign"],
                json.dumps({"schema": "synthetic.preexisting-assignment.v1"}),
                AT5,
                AT5,
            ),
        )
        assignment_version = 1
    prepared, switched = switch_rollover(store, context)
    rows = {
        row["champion_agent_id"]: row
        for row in store.rollover_bindings(prepared["operation_id"], AT6)["page"][
            "rows"
        ]
    }
    pending_outbox_ids = []
    for ordinal in range(outbox_count):
        event_id = f"event:partial-refresh:{label}:{ordinal}"
        outbox_id = f"outbox:partial-refresh:{label}:{ordinal}"
        store.connection.execute(
            """
            INSERT INTO events
              (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
               update_text,occurred_at,detail_json,aggregate_kind,aggregate_id)
            VALUES(?,NULL,?,NULL,1,'diagnostic','working',?,?,'{}','task',?)
            """,
            (event_id, task_id, "Synthetic pending descendant delivery.", AT5, task_id),
        )
        store.connection.execute(
            """
            INSERT INTO delivery_outbox
              (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
            VALUES(?,?,?,'pending',?,0)
            """,
            (outbox_id, event_id, OLD_ID, AT5),
        )
        pending_outbox_ids.append(outbox_id)
    champion = store.agent_status(champion_id)
    callsign_version = store.connection.execute(
        "SELECT version FROM callsign_assignments WHERE agent_id=?",
        (champion_id,),
    ).fetchone()[0]
    target = store.rollover_descendant_target(
        prepared["operation_id"],
        f"reconciliation:partial-refresh:{label}",
        champion_id,
        task_id,
        prepared["snapshot"]["digest"],
        rows[champion_id]["row_digest"],
        switched["version"],
        champion["version"],
        1,
        assignment_version,
        callsign_version,
    )
    runtime_id = (
        target["runtime"]["runtime_instance_id"]
        if target["runtime"] is not None
        else f"runtime:partial-refresh-imported:{label}"
    )
    runtime_receipt_value = descendant_runtime_receipt(
        target, runtime_id, terminal_id
    )
    if target["runtime"] is not None:
        runtime_receipt_value["runtime_generation"] = target["runtime"][
            "runtime_generation"
        ]
    else:
        runtime_receipt_value["runtime_generation"] = "herdr:" + hashlib.sha256(
            f"{terminal_id}\0{target['thread_id']}".encode("utf-8")
        ).hexdigest()[:24]
    reconciled = store.reconcile_rollover_descendant(
        prepared["operation_id"],
        f"reconciliation:partial-refresh:{label}",
        champion_id,
        task_id,
        runtime_id,
        prepared["snapshot"]["digest"],
        rows[champion_id]["row_digest"],
        switched["version"],
        champion["version"],
        1,
        assignment_version,
        callsign_version,
        runtime_receipt_value,
        tuple(pending_outbox_ids),
        AT6,
    )
    return {
        "context": context,
        "prepared": prepared,
        "switched": switched,
        "champion_id": champion_id,
        "task_id": task_id,
        "reconciliation_id": f"reconciliation:partial-refresh:{label}",
        "reconciled": reconciled,
        "outbox_ids": tuple(pending_outbox_ids),
        "original_rows": rows,
    }


def _partial_refresh_inputs(partial: dict, label: str) -> dict:
    return {
        "operation_id": partial["prepared"]["operation_id"],
        "refresh_id": f"refresh:partial-progress:{label}",
        "squad_id": SQUAD_ID,
        "predecessor_agent_id": OLD_ID,
        "successor_agent_id": NEW_ID,
        "expected_rollover_version": partial["switched"]["version"],
        "expected_snapshot_version": partial["prepared"]["snapshot"]["version"],
        "expected_snapshot_digest": partial["prepared"]["snapshot"]["digest"],
        "expires_at": "2026-01-01T03:00:00Z",
        "at": "2026-01-01T02:00:00Z",
    }


def _rewrite_created_reconciliation_as_historical_imported_receipt(
    store: SQLiteStorage, partial: dict
) -> dict:
    """Reproduce the exact receipt emitted before runtime capability evidence landed."""

    event = store.connection.execute(
        "SELECT detail_json FROM events WHERE event_id=?",
        (partial["reconciliation_id"],),
    ).fetchone()
    detail = json.loads(event["detail_json"])
    receipt = dict(detail["receipt"])
    assert receipt["created_assignment"] is True
    assert receipt["source_shape"] == "imported_legacy_partial"
    del receipt["required_capabilities"]
    del receipt["runtime_capabilities"]
    receipt_digest = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    historical_detail = {"receipt": receipt, "receipt_digest": receipt_digest}
    store.connection.execute(
        "UPDATE events SET detail_json=? WHERE event_id=?",
        (
            json.dumps(historical_detail, sort_keys=True, separators=(",", ":")),
            partial["reconciliation_id"],
        ),
    )
    store.connection.execute(
        "UPDATE task_assignments SET acceptance_receipt_json=? WHERE task_assignment_id=?",
        (
            json.dumps(receipt, sort_keys=True, separators=(",", ":")),
            receipt["task_assignment_id"],
        ),
    )
    return {"receipt": receipt, "receipt_digest": receipt_digest}


def test_snapshot_refresh_accepts_exact_historical_imported_progress(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "refresh-historical-imported-progress")
    with SQLiteStorage(state) as store:
        partial = _seed_partially_reconciled_refresh(
            store,
            root,
            "historical-imported",
            outbox_count=2,
            imported_legacy_partial=True,
        )
        historical = _rewrite_created_reconciliation_as_historical_imported_receipt(
            store, partial
        )
        runtime_before = dict(
            store.connection.execute(
                "SELECT * FROM runtime_instances WHERE actor_agent_id=?",
                (partial["champion_id"],),
            ).fetchone()
        )
        assignment_before = dict(
            store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?",
                (historical["receipt"]["task_assignment_id"],),
            ).fetchone()
        )

        refreshed = RolloverSnapshotRefreshService(
            store, ExactSnapshotInventory()
        ).refresh(**_partial_refresh_inputs(partial, "historical-imported"))

        assert refreshed["progress_bindings"][0] == {
            "champion_agent_id": partial["champion_id"],
            "task_id": partial["task_id"],
            "state": "successor_reconciled",
            "reconciliation_id": partial["reconciliation_id"],
            "receipt_digest": historical["receipt_digest"],
        }
        assert dict(
            store.connection.execute(
                "SELECT * FROM runtime_instances WHERE actor_agent_id=?",
                (partial["champion_id"],),
            ).fetchone()
        ) == runtime_before
        assert dict(
            store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_assignment_id=?",
                (historical["receipt"]["task_assignment_id"],),
            ).fetchone()
        ) == assignment_before


def test_snapshot_refresh_refuses_inexact_historical_imported_receipts(
    root: Path,
) -> None:
    mutations = (
        ("missing", lambda receipt: receipt.pop("runtime_receipt_digest")),
        (
            "modern-incomplete",
            lambda receipt: receipt.__setitem__("required_capabilities", []),
        ),
        ("type", lambda receipt: receipt.__setitem__("pending_delivery_count", "0")),
    )
    for label, mutate in mutations:
        state, _ = migrated_state(root, f"refresh-historical-inexact-{label}")
        with SQLiteStorage(state) as store:
            partial = _seed_partially_reconciled_refresh(
                store,
                root,
                f"historical-inexact-{label}",
                imported_legacy_partial=True,
            )
            historical = _rewrite_created_reconciliation_as_historical_imported_receipt(
                store, partial
            )
            receipt = dict(historical["receipt"])
            mutate(receipt)
            detail = {
                "receipt": receipt,
                "receipt_digest": hashlib.sha256(
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ).hexdigest(),
            }
            store.connection.execute(
                "UPDATE events SET detail_json=? WHERE event_id=?",
                (
                    json.dumps(detail, sort_keys=True, separators=(",", ":")),
                    partial["reconciliation_id"],
                ),
            )
            store.connection.execute(
                "UPDATE task_assignments SET acceptance_receipt_json=? WHERE task_assignment_id=?",
                (
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                    historical["receipt"]["task_assignment_id"],
                ),
            )
            before = store.rollover_status(partial["prepared"]["operation_id"])
            try:
                RolloverSnapshotRefreshService(
                    store, ExactSnapshotInventory()
                ).refresh(**_partial_refresh_inputs(partial, f"historical-inexact-{label}"))
            except StorageRefusal as exc:
                assert exc.code == "snapshot_refresh_identity_changed"
            else:
                raise AssertionError(f"{label} historical receipt refreshed snapshot")
            after = store.rollover_status(partial["prepared"]["operation_id"])
            assert after["snapshot"] == before["snapshot"]
            assert after["version"] == before["version"]


def test_snapshot_refresh_refuses_historical_unenumerated_pending_delivery(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "refresh-historical-pending-overcount")
    with SQLiteStorage(state) as store:
        partial = _seed_partially_reconciled_refresh(
            store,
            root,
            "historical-pending-overcount",
            outbox_count=2,
            imported_legacy_partial=True,
        )
        historical = _rewrite_created_reconciliation_as_historical_imported_receipt(
            store, partial
        )
        receipt = dict(historical["receipt"])
        assert receipt["pending_delivery_count"] == 2
        assert len(receipt["retargeted_outbox_ids"]) == 2
        receipt["pending_delivery_count"] = 3
        detail = {
            "receipt": receipt,
            "receipt_digest": hashlib.sha256(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
        }
        store.connection.execute(
            "UPDATE events SET detail_json=? WHERE event_id=?",
            (
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
                partial["reconciliation_id"],
            ),
        )
        store.connection.execute(
            "UPDATE task_assignments SET acceptance_receipt_json=? WHERE task_assignment_id=?",
            (
                json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                receipt["task_assignment_id"],
            ),
        )
        before = store.rollover_status(partial["prepared"]["operation_id"])

        try:
            RolloverSnapshotRefreshService(
                store, ExactSnapshotInventory()
            ).refresh(
                **_partial_refresh_inputs(partial, "historical-pending-overcount")
            )
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_identity_changed"
        else:
            raise AssertionError("historical pending-delivery overcount refreshed snapshot")

        after = store.rollover_status(partial["prepared"]["operation_id"])
        assert after["snapshot"] == before["snapshot"]
        assert after["version"] == before["version"]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='rollover_snapshot_refreshed'"
        ).fetchone()[0] == 0


def test_snapshot_refresh_refuses_ambiguous_historical_receipt_or_missing_acceptance(
    root: Path,
) -> None:
    for label in ("ambiguous", "missing-acceptance"):
        state, _ = migrated_state(root, f"refresh-historical-{label}")
        with SQLiteStorage(state) as store:
            partial = _seed_partially_reconciled_refresh(
                store,
                root,
                f"historical-{label}",
                imported_legacy_partial=True,
            )
            historical = _rewrite_created_reconciliation_as_historical_imported_receipt(
                store, partial
            )
            if label == "ambiguous":
                event = store.connection.execute(
                    "SELECT * FROM events WHERE event_id=?",
                    (partial["reconciliation_id"],),
                ).fetchone()
                store.connection.execute(
                    """
                    INSERT INTO events
                      (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
                       update_text,occurred_at,detail_json,aggregate_kind,aggregate_id,
                       source_event_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"{partial['reconciliation_id']}:duplicate",
                        event["agent_id"],
                        event["task_id"],
                        event["squad_id"],
                        event["entity_version"],
                        event["event_type"],
                        event["status"],
                        event["update_text"],
                        event["occurred_at"],
                        event["detail_json"],
                        event["aggregate_kind"],
                        event["aggregate_id"],
                        event["source_event_id"],
                    ),
                )
            else:
                store.connection.execute(
                    "UPDATE task_assignments SET acceptance_receipt_json='{}' WHERE task_assignment_id=?",
                    (historical["receipt"]["task_assignment_id"],),
                )
            before = store.rollover_status(partial["prepared"]["operation_id"])
            try:
                RolloverSnapshotRefreshService(
                    store, ExactSnapshotInventory()
                ).refresh(**_partial_refresh_inputs(partial, f"historical-{label}"))
            except StorageRefusal as exc:
                assert exc.code == "snapshot_refresh_identity_changed"
            else:
                raise AssertionError(f"{label} historical proof refreshed snapshot")
            after = store.rollover_status(partial["prepared"]["operation_id"])
            assert after["snapshot"] == before["snapshot"]
            assert after["version"] == before["version"]


def test_snapshot_refresh_preserves_exact_mixed_progress_and_terminal_marker(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "refresh-partial-progress")
    with SQLiteStorage(state) as store:
        partial = _seed_partially_reconciled_refresh(
            store, root, "success", outbox_count=2
        )
        inputs = _partial_refresh_inputs(partial, "success")
        service = RolloverSnapshotRefreshService(store, ExactSnapshotInventory())
        first = service.refresh(**inputs)
        second = service.refresh(**inputs)

        assert first["receipt_digest"] == second["receipt_digest"]
        assert first["idempotent"] is False
        assert second["idempotent"] is True
        assert first["descendant_count"] == 2
        assert first["progress_bindings"] == [
            {
                "champion_agent_id": partial["context"]["champion_ids"][0],
                "task_id": "task:champion:0",
                "state": "successor_reconciled",
                "reconciliation_id": partial["reconciliation_id"],
                "receipt_digest": partial["reconciled"]["receipt_digest"],
            },
            {
                "champion_agent_id": partial["context"]["champion_ids"][1],
                "task_id": "task:champion:1",
                "state": "predecessor_pending",
                "reconciliation_id": None,
                "receipt_digest": None,
            },
        ]
        bindings = store.rollover_bindings(
            partial["prepared"]["operation_id"], "2026-01-01T02:01:00Z"
        )
        assert bindings["snapshot_count"] == 2
        assert {
            (row["champion_agent_id"], row["task_id"], row["callsign"])
            for row in bindings["page"]["rows"]
        } == {
            (row["champion_agent_id"], row["task_id"], row["callsign"])
            for row in partial["original_rows"].values()
        }
        assert bindings["terminal_markers"] == [first["progress_bindings"][0]]
        assert store.agent_status(partial["champion_id"])[
            "shotcaller_agent_id"
        ] == NEW_ID
        assert json.loads(
            store.connection.execute(
                "SELECT capabilities_json FROM runtime_instances WHERE actor_agent_id=?",
                (partial["champion_id"],),
            ).fetchone()[0]
        ) == ["hook.capture", "task.execute"]


def test_snapshot_refresh_refuses_forged_or_missing_successor_receipt(
    root: Path,
) -> None:
    for label in ("missing", "forged"):
        state, _ = migrated_state(root, f"refresh-partial-{label}")
        with SQLiteStorage(state) as store:
            partial = _seed_partially_reconciled_refresh(store, root, label)
            if label == "missing":
                store.connection.execute(
                    "DELETE FROM events WHERE event_id=?",
                    (partial["reconciliation_id"],),
                )
            else:
                row = store.connection.execute(
                    "SELECT detail_json FROM events WHERE event_id=?",
                    (partial["reconciliation_id"],),
                ).fetchone()
                detail = json.loads(row["detail_json"])
                detail["receipt"]["successor_agent_id"] = "agent:forged-successor"
                detail["receipt_digest"] = hashlib.sha256(
                    json.dumps(
                        detail["receipt"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                store.connection.execute(
                    "UPDATE events SET detail_json=? WHERE event_id=?",
                    (
                        json.dumps(detail, sort_keys=True, separators=(",", ":")),
                        partial["reconciliation_id"],
                    ),
                )
            try:
                RolloverSnapshotRefreshService(
                    store, ExactSnapshotInventory()
                ).refresh(**_partial_refresh_inputs(partial, label))
            except StorageRefusal as exc:
                assert exc.code == "snapshot_refresh_identity_changed"
            else:
                raise AssertionError(f"{label} successor proof refreshed snapshot")
            status = store.rollover_status(partial["prepared"]["operation_id"])
            assert status["snapshot"] == partial["prepared"]["snapshot"]
            assert status["version"] == partial["switched"]["version"]


def test_snapshot_refresh_requires_complete_exact_existing_assignment_receipt(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "refresh-partial-exact-receipt")
    with SQLiteStorage(state) as store:
        partial = _seed_partially_reconciled_refresh(
            store,
            root,
            "exact-receipt",
            preexisting_assignment=True,
        )
        event = store.connection.execute(
            "SELECT detail_json FROM events WHERE event_id=?",
            (partial["reconciliation_id"],),
        ).fetchone()
        canonical_detail = json.loads(event["detail_json"])
        canonical_receipt = canonical_detail["receipt"]
        assert canonical_receipt["created_assignment"] is False
        before = store.rollover_status(partial["prepared"]["operation_id"])

        mutations = []
        for key, value in canonical_receipt.items():
            missing = dict(canonical_receipt)
            del missing[key]
            mutations.append((f"missing-{key}", missing))
            changed = dict(canonical_receipt)
            changed[key] = (
                0
                if value is None or type(value) is bool
                else None
                if isinstance(value, (str, int, list))
                else "wrong-type"
            )
            mutations.append((f"type-{key}", changed))
        extra = dict(canonical_receipt)
        extra["unexpected_live_evidence"] = "forged"
        mutations.append(("extra-field", extra))

        for label, mutated_receipt in mutations:
            mutated_detail = {
                "receipt": mutated_receipt,
                "receipt_digest": hashlib.sha256(
                    json.dumps(
                        mutated_receipt,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
            store.connection.execute(
                "UPDATE events SET detail_json=? WHERE event_id=?",
                (
                    json.dumps(
                        mutated_detail,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    partial["reconciliation_id"],
                ),
            )
            try:
                RolloverSnapshotRefreshService(
                    store, ExactSnapshotInventory()
                ).refresh(
                    **_partial_refresh_inputs(partial, f"exact-receipt-{label}")
                )
            except StorageRefusal as exc:
                assert exc.code == "snapshot_refresh_identity_changed"
            else:
                raise AssertionError(f"{label} incomplete receipt refreshed snapshot")
            after = store.rollover_status(partial["prepared"]["operation_id"])
            assert after["snapshot"] == before["snapshot"]
            assert after["version"] == before["version"]
            assert store.connection.execute(
                """
                SELECT COUNT(*) FROM events
                 WHERE event_type='rollover_snapshot_refreshed'
                """
            ).fetchone()[0] == 0
            store.connection.execute(
                "UPDATE events SET detail_json=? WHERE event_id=?",
                (
                    json.dumps(
                        canonical_detail,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    partial["reconciliation_id"],
                ),
            )


def test_snapshot_refresh_refuses_partially_retargeted_descendant_outbox(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "refresh-partial-outbox")
    with SQLiteStorage(state) as store:
        partial = _seed_partially_reconciled_refresh(
            store, root, "outbox", outbox_count=2
        )
        store.connection.execute(
            "UPDATE delivery_outbox SET recipient_agent_id=? WHERE outbox_id=?",
            (OLD_ID, partial["outbox_ids"][0]),
        )
        try:
            RolloverSnapshotRefreshService(
                store, ExactSnapshotInventory()
            ).refresh(**_partial_refresh_inputs(partial, "outbox"))
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_identity_changed"
        else:
            raise AssertionError("partially retargeted outbox refreshed snapshot")
        assert store.rollover_status(partial["prepared"]["operation_id"])[
            "snapshot"
        ] == partial["prepared"]["snapshot"]


def test_partial_progress_refresh_keeps_expiry_and_crash_boundaries(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "refresh-partial-expiry-crash")
    with SQLiteStorage(state) as store:
        partial = _seed_partially_reconciled_refresh(store, root, "expiry-crash")
        inputs = _partial_refresh_inputs(partial, "expiry-crash")
        too_early = {**inputs, "at": AT6, "expires_at": "2026-01-01T02:00:00Z"}
        try:
            RolloverSnapshotRefreshService(
                store, ExactSnapshotInventory()
            ).refresh(**too_early)
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_not_expired"
        else:
            raise AssertionError("mixed snapshot refreshed before expiry")

        target = store.rollover_snapshot_refresh_target(**inputs)
        observations = ExactSnapshotInventory().observe(target["descendants"])

        def crash(point: str) -> None:
            if point == "after_refresh_rows":
                raise InjectedCrash(point)

        try:
            store.refresh_rollover_snapshot(
                **inputs,
                canonical_digest=target["canonical_digest"],
                observations=observations,
                final_observer=lambda _descendants: observations,
                fault=crash,
            )
        except InjectedCrash as exc:
            assert str(exc) == "after_refresh_rows"
        else:
            raise AssertionError("mixed refresh crash boundary did not fire")
        status = store.rollover_status(partial["prepared"]["operation_id"])
        assert status["snapshot"] == partial["prepared"]["snapshot"]
        assert status["version"] == partial["switched"]["version"]
        recovered = store.refresh_rollover_snapshot(
            **inputs,
            canonical_digest=target["canonical_digest"],
            observations=observations,
            final_observer=lambda _descendants: observations,
        )
        assert recovered["snapshot"]["version"] == 2


def test_refreshed_snapshot_drives_descendant_reconciliation(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-then-reconcile")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        mark_exact_imported_legacy_partial(
            store, context["champion_ids"][0], "task:champion:0"
        )
        prepared, switched = switch_rollover(store, context)
        refreshed = RolloverSnapshotRefreshService(
            store, ExactSnapshotInventory()
        ).refresh(
            operation_id=prepared["operation_id"],
            refresh_id="refresh:then-reconcile",
            squad_id=SQUAD_ID,
            predecessor_agent_id=OLD_ID,
            successor_agent_id=NEW_ID,
            expected_rollover_version=switched["version"],
            expected_snapshot_version=prepared["snapshot"]["version"],
            expected_snapshot_digest=prepared["snapshot"]["digest"],
            expires_at="2026-01-01T03:00:00Z",
            at="2026-01-01T02:00:00Z",
        )
        row = store.rollover_bindings(
            prepared["operation_id"], "2026-01-01T02:01:00Z"
        )["page"]["rows"][0]
        champion = store.agent_status(context["champion_ids"][0])

        class ExactRuntime:
            def verify(self, target: dict, runtime_instance_id: str) -> dict:
                return descendant_runtime_receipt(target, runtime_instance_id)

        result = RolloverDescendantService(store, ExactRuntime()).reconcile(
            operation_id=prepared["operation_id"],
            reconciliation_id="reconciliation:refreshed-snapshot",
            champion_agent_id=context["champion_ids"][0],
            task_id=row["task_id"],
            runtime_instance_id="runtime:champion:0",
            snapshot_digest=refreshed["snapshot"]["digest"],
            snapshot_row_digest=row["row_digest"],
            expected_rollover_version=refreshed["rollover_version"],
            expected_agent_version=champion["version"],
            expected_task_version=1,
            expected_assignment_version=0,
            expected_callsign_assignment_version=1,
            pending_outbox_ids=(),
            at="2026-01-01T02:02:00Z",
        )
        assert result["successor_agent_id"] == NEW_ID


def test_snapshot_refresh_refuses_concurrent_canonical_mutation(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-concurrent-mutation")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        prepared, switched = switch_rollover(store, context)

        class RacingInventory(ExactSnapshotInventory):
            def observe(self, descendants: list[dict]) -> list[dict]:
                observations = super().observe(descendants)
                with SQLiteStorage(state) as racer:
                    racer.connection.execute(
                        "UPDATE agent_instances SET display_agent='changed' WHERE agent_id=?",
                        (context["champion_ids"][0],),
                    )
                return observations

        try:
            RolloverSnapshotRefreshService(store, RacingInventory()).refresh(
                operation_id=prepared["operation_id"],
                refresh_id="refresh:concurrent-mutation",
                squad_id=SQUAD_ID,
                predecessor_agent_id=OLD_ID,
                successor_agent_id=NEW_ID,
                expected_rollover_version=switched["version"],
                expected_snapshot_version=prepared["snapshot"]["version"],
                expected_snapshot_digest=prepared["snapshot"]["digest"],
                expires_at="2026-01-01T03:00:00Z",
                at="2026-01-01T02:00:00Z",
            )
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_concurrent_mutation"
        else:
            raise AssertionError("concurrent descendant mutation refreshed the snapshot")
        assert store.rollover_status(prepared["operation_id"])["snapshot"] == prepared["snapshot"]


def test_snapshot_refresh_refuses_a_changed_final_live_observation(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-final-live-race")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        prepared, switched = switch_rollover(store, context)

        mutations = (
            ("endpoint", "endpoint", "pane:changed"),
            ("route", "routing_name", "changed-route"),
            (
                "session",
                "thread_id",
                "ffffffff-ffff-4fff-8fff-ffffffffffff",
            ),
            ("terminal", "terminal_id", "terminal:changed"),
            ("sequence", "state_change_seq", 2),
        )
        for label, field, value in mutations:
            class RacingInventory(ExactSnapshotInventory):
                def __init__(self) -> None:
                    self.calls = 0

                def observe(self, descendants: list[dict]) -> list[dict]:
                    observations = super().observe(descendants)
                    self.calls += 1
                    if self.calls == 2:
                        observations[0][field] = value
                    return observations

            inventory = RacingInventory()
            try:
                RolloverSnapshotRefreshService(store, inventory).refresh(
                    operation_id=prepared["operation_id"],
                    refresh_id=f"refresh:final-live-race:{label}",
                    squad_id=SQUAD_ID,
                    predecessor_agent_id=OLD_ID,
                    successor_agent_id=NEW_ID,
                    expected_rollover_version=switched["version"],
                    expected_snapshot_version=prepared["snapshot"]["version"],
                    expected_snapshot_digest=prepared["snapshot"]["digest"],
                    expires_at="2026-01-01T03:00:00Z",
                    at="2026-01-01T02:00:00Z",
                )
            except StorageRefusal as exc:
                assert exc.code == "snapshot_refresh_live_changed"
            else:
                raise AssertionError(
                    f"changed final Herdr {label} refreshed the snapshot"
                )
            assert inventory.calls == 2
            status = store.rollover_status(prepared["operation_id"])
            assert status["version"] == switched["version"]
            assert status["snapshot"] == prepared["snapshot"]
            assert store.connection.execute(
                "SELECT COUNT(*) FROM active_champion_snapshots WHERE operation_id=?",
                (prepared["operation_id"],),
            ).fetchone()[0] == 1
            assert store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='rollover_snapshot_refreshed'"
            ).fetchone()[0] == 0


def test_snapshot_refresh_reobserves_inside_the_final_pointer_boundary(
    root: Path,
) -> None:
    state, _ = migrated_state(root, "refresh-final-pointer-boundary")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        prepared, switched = switch_rollover(store, context)

        class PointerBoundaryInventory(ExactSnapshotInventory):
            def __init__(self) -> None:
                self.calls = 0

            def observe(self, descendants: list[dict]) -> list[dict]:
                observations = super().observe(descendants)
                self.calls += 1
                if self.calls == 2:
                    assert store.connection.in_transaction is True
                    with SQLiteStorage(state) as writer_probe:
                        writer_probe.connection.execute("BEGIN IMMEDIATE")
                        writer_probe.connection.execute("ROLLBACK")
                    operation = store.connection.execute(
                        "SELECT version,snapshot_id FROM rollover_operations WHERE operation_id=?",
                        (prepared["operation_id"],),
                    ).fetchone()
                    assert tuple(operation) == (
                        switched["version"],
                        prepared["snapshot"]["snapshot_id"],
                    )
                    assert store.connection.execute(
                        "SELECT COUNT(*) FROM active_champion_snapshots WHERE operation_id=?",
                        (prepared["operation_id"],),
                    ).fetchone()[0] == 1
                    observations[0]["state_change_seq"] += 1
                return observations

        inventory = PointerBoundaryInventory()
        try:
            RolloverSnapshotRefreshService(store, inventory).refresh(
                operation_id=prepared["operation_id"],
                refresh_id="refresh:final-pointer-boundary",
                squad_id=SQUAD_ID,
                predecessor_agent_id=OLD_ID,
                successor_agent_id=NEW_ID,
                expected_rollover_version=switched["version"],
                expected_snapshot_version=prepared["snapshot"]["version"],
                expected_snapshot_digest=prepared["snapshot"]["digest"],
                expires_at="2026-01-01T03:00:00Z",
                at="2026-01-01T02:00:00Z",
            )
        except StorageRefusal as exc:
            assert exc.code == "snapshot_refresh_live_changed"
        else:
            raise AssertionError("final live change advanced the rollover pointer")
        assert inventory.calls == 2
        status = store.rollover_status(prepared["operation_id"])
        assert status["version"] == switched["version"]
        assert status["snapshot"] == prepared["snapshot"]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM active_champion_snapshots WHERE operation_id=?",
            (prepared["operation_id"],),
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='rollover_snapshot_refreshed'"
        ).fetchone()[0] == 0


def test_snapshot_refresh_faults_restore_the_expired_snapshot(root: Path) -> None:
    points = (
        "after_refresh_snapshot",
        "after_refresh_rows",
        "after_refresh_operation_cas",
        "after_refresh_event",
    )
    for point in points:
        state, _ = migrated_state(root, f"refresh-fault-{point}")
        with SQLiteStorage(state) as store:
            context = seed_rollover(store, champion_count=1)
            prepared, switched = switch_rollover(store, context)
            inputs = {
                "operation_id": prepared["operation_id"],
                "refresh_id": f"refresh:fault:{point}",
                "squad_id": SQUAD_ID,
                "predecessor_agent_id": OLD_ID,
                "successor_agent_id": NEW_ID,
                "expected_rollover_version": switched["version"],
                "expected_snapshot_version": prepared["snapshot"]["version"],
                "expected_snapshot_digest": prepared["snapshot"]["digest"],
                "expires_at": "2026-01-01T03:00:00Z",
                "at": "2026-01-01T02:00:00Z",
            }
            target = store.rollover_snapshot_refresh_target(**inputs)
            observations = ExactSnapshotInventory().observe(target["descendants"])

            def crash(observed: str) -> None:
                if observed == point:
                    raise InjectedCrash(point)

            try:
                store.refresh_rollover_snapshot(
                    **inputs,
                    canonical_digest=target["canonical_digest"],
                    observations=observations,
                    final_observer=lambda _descendants: observations,
                    fault=crash,
                )
            except InjectedCrash as exc:
                assert str(exc) == point
            else:
                raise AssertionError(f"snapshot refresh fault {point} was not injected")
            status = store.rollover_status(prepared["operation_id"])
            assert status["version"] == switched["version"]
            assert status["snapshot"] == prepared["snapshot"]
            recovered = store.refresh_rollover_snapshot(
                **inputs,
                canonical_digest=target["canonical_digest"],
                observations=observations,
                final_observer=lambda _descendants: observations,
            )
            assert recovered["snapshot"]["version"] == 2


def test_snapshot_refresh_refuses_changed_rollover_identity(root: Path) -> None:
    state, _ = migrated_state(root, "refresh-changed-identity")
    with SQLiteStorage(state) as store:
        context = seed_rollover(store, champion_count=1)
        prepared, switched = switch_rollover(store, context)
        base = {
            "operation_id": prepared["operation_id"],
            "refresh_id": "refresh:changed-identity",
            "squad_id": SQUAD_ID,
            "predecessor_agent_id": OLD_ID,
            "successor_agent_id": NEW_ID,
            "expected_rollover_version": switched["version"],
            "expected_snapshot_version": prepared["snapshot"]["version"],
            "expected_snapshot_digest": prepared["snapshot"]["digest"],
            "expires_at": "2026-01-01T03:00:00Z",
            "at": "2026-01-01T02:00:00Z",
        }
        changed = (
            {**base, "squad_id": "squad:other"},
            {**base, "predecessor_agent_id": "agent:other-predecessor"},
            {**base, "successor_agent_id": "agent:other-successor"},
        )
        for inputs in changed:
            try:
                RolloverSnapshotRefreshService(
                    store, ExactSnapshotInventory()
                ).refresh(**inputs)
            except StorageRefusal as exc:
                assert exc.code == "snapshot_refresh_identity_changed"
            else:
                raise AssertionError("changed rollover identity refreshed the snapshot")
        try:
            RolloverSnapshotRefreshService(store, ExactSnapshotInventory()).refresh(
                **{**base, "operation_id": "rollover:other"}
            )
        except StorageRefusal as exc:
            assert exc.code == "rollover_unknown"
        else:
            raise AssertionError("changed operation identity refreshed the snapshot")


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
        test_snapshot_refresh_cli_requires_the_exact_switched_identity()
        test_snapshot_refresh_cli_runs_two_stable_herdr_inventories(root)
        test_snapshot_refresh_adopts_eight_exact_imported_null_routes(root)
        test_snapshot_refresh_null_route_refuses_live_guess_or_overlap(root)
        test_snapshot_refresh_null_route_refuses_modern_or_successor_owner(root)
        test_snapshot_refresh_null_route_cas_and_fault_restore_exact_state(root)
        test_snapshot_refresh_adapter_requires_one_exact_live_identity(root)
        test_snapshot_refresh_refuses_a_mismatched_canonical_runtime(root)
        test_runtime_capability_superset_refreshes_and_reconciles_without_downgrade(root)
        test_runtime_capability_contract_refuses_missing_and_unverified(root)
        test_descendant_reconciliation_refuses_capability_drift_and_unverified_runtime(root)
        test_snapshot_refresh_refuses_invalid_missing_runtime_generations_without_mutation(root)
        test_snapshot_refresh_requires_canonical_generation_to_match_observed_terminal(root)
        test_switched_rollover_refreshes_only_the_expired_exact_snapshot(root)
        test_snapshot_refresh_refuses_before_expiry_without_live_observation(root)
        test_snapshot_refresh_refuses_a_changed_descendant_set(root)
        test_snapshot_refresh_retry_returns_the_identical_receipt(root)
        test_snapshot_refresh_accepts_exact_historical_imported_progress(root)
        test_snapshot_refresh_refuses_inexact_historical_imported_receipts(root)
        test_snapshot_refresh_refuses_historical_unenumerated_pending_delivery(root)
        test_snapshot_refresh_refuses_ambiguous_historical_receipt_or_missing_acceptance(root)
        test_snapshot_refresh_preserves_exact_mixed_progress_and_terminal_marker(root)
        test_snapshot_refresh_refuses_forged_or_missing_successor_receipt(root)
        test_snapshot_refresh_requires_complete_exact_existing_assignment_receipt(root)
        test_snapshot_refresh_refuses_partially_retargeted_descendant_outbox(root)
        test_partial_progress_refresh_keeps_expiry_and_crash_boundaries(root)
        test_refreshed_snapshot_drives_descendant_reconciliation(root)
        test_snapshot_refresh_refuses_concurrent_canonical_mutation(root)
        test_snapshot_refresh_refuses_a_changed_final_live_observation(root)
        test_snapshot_refresh_reobserves_inside_the_final_pointer_boundary(root)
        test_snapshot_refresh_faults_restore_the_expired_snapshot(root)
        test_snapshot_refresh_refuses_changed_rollover_identity(root)
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
