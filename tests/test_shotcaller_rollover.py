#!/usr/bin/env python3
"""Guarded Shotcaller handoff, snapshot, CAS, crash, and drain coverage."""

from __future__ import annotations

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
from league.storage import StorageRefusal  # noqa: E402
from storage_test_support import migrated_state  # noqa: E402


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
              (task_id,summary,state,version,current_owner_agent_id,updated_at,champion_agent_id)
            VALUES(?,?,'working',1,?,?,?)
            """,
            (task_id, f"Synthetic Champion task {ordinal}", agent_id, AT1, agent_id),
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
        test_guarded_switch_crash_retry_and_drain(root)
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
