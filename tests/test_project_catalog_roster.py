#!/usr/bin/env python3
"""Focused advisory catalog and one-transaction project Roster tests."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.sqlite_project_ops import resolve_project_routing_identity  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from storage_fixture import AT1, AT2, CHAMPION_ID, REPOSITORY, TASK_ID  # noqa: E402
from storage_test_support import invoke_cli, migrated_state, seeded_state  # noqa: E402


AT3 = "2026-01-01T00:02:00Z"
AT4 = "2026-01-01T00:03:00Z"
AT5 = "2026-01-01T00:04:00Z"
ROOT_MARKER = "/synthetic/private/projects/league"


def refused(operation, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def add_squad(store: SQLiteStorage, callsign: str, squad_id: str, agent_id: str, state: str) -> None:
    with store._transaction():
        store.connection.execute(
            "INSERT INTO callsigns(callsign,pool_role,enabled,pool_position,last_released_at) VALUES(?,'shotcaller',1,NULL,NULL)",
            (callsign,),
        )
        store.connection.execute(
            """
            INSERT INTO agent_instances
              (agent_id,callsign,role,kind,status,version,updated_at,update_text,next_action,
               metadata_json,retired_at)
            VALUES(?,?,'shotcaller','synthetic','working',1,?,'Synthetic coordination.',
                   'Continue synthetic coordination.','{}',NULL)
            """,
            (agent_id, callsign, AT2),
        )
        store.connection.execute(
            "INSERT INTO squads(squad_id,shotcaller_agent_id,state,version,updated_at) VALUES(?,?,?,1,?)",
            (squad_id, agent_id, state, AT2),
        )


def catalog_project(store: SQLiteStorage) -> dict[str, object]:
    imported = store.resolve_project(REPOSITORY)
    assert imported is not None
    return store.put_project(
        str(imported["project_id"]),
        int(imported["version"]),
        "Synthetic League coordination",
        "https://example.invalid/league.git",
        ROOT_MARKER,
        "LOL",
        ["league", "orchestrator"],
        "active",
        AT3,
    )


def test_exact_identity_ambiguity_redaction_and_cli(root: Path) -> None:
    _, state, _ = seeded_state(root, "catalog")
    with SQLiteStorage(state) as store:
        project = catalog_project(store)
        project_id = str(project["project_id"])
        assert project["version"] == 2 and not project["idempotent"]
        retry = catalog_project(store)
        assert retry["idempotent"] and retry["version"] == 2
        assert store.resolve_project("https://EXAMPLE.invalid/league")["project_id"] == project_id
        assert store.resolve_project(root=f"{ROOT_MARKER}/")["project_id"] == project_id
        assert store.resolve_project(code="lol")["project_id"] == project_id
        assert store.resolve_project(alias="ORCHESTRATOR")["project_id"] == project_id
        traced: list[str] = []
        store.connection.set_trace_callback(traced.append)
        assert resolve_project_routing_identity(
            store, "https://EXAMPLE.invalid/league"
        ) == (project_id, "active")
        store.connection.set_trace_callback(None)
        assert sum("SELECT project_id,state FROM projects" in item for item in traced) == 1
        assert not any("project_aliases" in item for item in traced)

        store.put_project(
            "project:other",
            0,
            "Other synthetic project",
            "git@example.invalid:other/repository.git",
            "/synthetic/private/projects/other",
            "OTHER",
            ["league"],
            "active",
            AT3,
        )
        refused(lambda: store.resolve_project(alias="league"), "ambiguous_project")
        refused(
            lambda: store.put_project(
                "project:collision",
                0,
                "Collision",
                "git@example.invalid:league.git",
                "/synthetic/private/projects/collision",
                None,
                [],
                "active",
                AT3,
            ),
            "project_identity_conflict",
        )
        outbound = json.dumps(store.list_projects(visibility="outbound"), sort_keys=True)
        assert ROOT_MARKER not in outbound and REPOSITORY not in outbound
        assert outbound.count("[redacted]") >= 4

    schema = json.loads(
        (ROOT / "schema/league-project-catalog.schema.json").read_text(encoding="utf-8")
    )
    properties = schema["$defs"]["project"]["properties"]
    assert properties["summary"]["maxLength"] == 240
    assert properties["aliases"]["items"]["maxLength"] == 64
    assert properties["code"]["maxLength"] == 24
    assert properties["root"]["maxLength"] == 2048
    assert properties["repository"]["maxLength"] == 2048

    resolved = invoke_cli(
        state,
        "project",
        "resolve",
        "--code",
        "LOL",
        "--visibility",
        "outbound",
    )
    assert resolved["command"] == "project.resolve"
    assert resolved["result"]["project"]["root"] == "[redacted]"
    listing = invoke_cli(state, "project", "list", "--visibility", "outbound")
    assert listing["result"]["schema"] == "league.project-catalog.v1"


def test_many_to_many_advice_never_rebinds(root: Path) -> None:
    _, state, _ = seeded_state(root, "suggestions")
    with SQLiteStorage(state) as store:
        project = catalog_project(store)
        project_id = str(project["project_id"])
        add_squad(
            store,
            "Shen",
            "squad:Shen",
            "33333333-3333-4333-8333-333333333333",
            "active",
        )
        add_squad(
            store,
            "Sona",
            "squad:Sona",
            "44444444-4444-4444-8444-444444444444",
            "retired",
        )
        add_squad(
            store,
            "Janna",
            "squad:Janna",
            "55555555-5555-4555-8555-555555555555",
            "active",
        )
        task_before = dict(
            store.connection.execute(
                "SELECT summary,current_owner_agent_id,current_owner_squad_id,version FROM tasks WHERE task_id=?",
                (TASK_ID,),
            ).fetchone()
        )
        events_before = store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        mapped = store.set_project_suggestions(
            project_id,
            int(project["version"]),
            ["squad:Garen", "squad:Shen", "squad:Sona"],
            AT4,
        )
        assert len(mapped["suggestions"]) == 3
        assert store.set_project_suggestions(
            project_id,
            int(project["version"]),
            ["squad:Garen", "squad:Shen", "squad:Sona"],
            AT4,
        )["idempotent"]
        second = store.put_project(
            "project:shared-squad",
            0,
            "Shared synthetic coverage",
            "https://example.invalid/shared/repository.git",
            "/synthetic/private/projects/shared",
            None,
            [],
            "active",
            AT3,
        )
        store.set_project_suggestions(
            "project:shared-squad", int(second["version"]), ["squad:Garen"], AT4
        )
        advice = store.project_advice(
            project_id, explicit_squad_id="squad:ExplicitChoice"
        )
        assert advice["explicit_route"] == {
            "squad_id": "squad:ExplicitChoice",
            "known": False,
            "available": False,
            "source": "explicit",
        }
        explicit_active = store.project_advice(
            project_id, explicit_squad_id="squad:Janna"
        )["explicit_route"]
        assert explicit_active == {
            "squad_id": "squad:Janna",
            "known": True,
            "available": True,
            "source": "explicit",
        }
        assert {item["squad_id"] for item in advice["available_suggestions"]} == {
            "squad:Garen",
            "squad:Shen",
        }
        assert next(
            item for item in advice["suggestions"] if item["squad_id"] == "squad:Sona"
        )["unavailable_reason"] == "squad_retired"
        task_after = dict(
            store.connection.execute(
                "SELECT summary,current_owner_agent_id,current_owner_squad_id,version FROM tasks WHERE task_id=?",
                (TASK_ID,),
            ).fetchone()
        )
        assert task_after == task_before
        assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
        assert advice["binding_changed"] is False


def test_catalog_two_writer_cas_and_deterministic_export(root: Path) -> None:
    _, state, _ = seeded_state(root, "concurrency")
    with SQLiteStorage(state) as store:
        project = catalog_project(store)
        project_id = str(project["project_id"])
    first = SQLiteStorage(state, busy_timeout_ms=1000)
    second = SQLiteStorage(state, busy_timeout_ms=1000)
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    outcomes: list[str] = []

    def writer(store: SQLiteStorage, summary: str) -> None:
        barrier.wait()
        try:
            store.put_project(
                project_id,
                2,
                summary,
                REPOSITORY,
                ROOT_MARKER,
                "LOL",
                ["league", "orchestrator"],
                "active",
                AT4,
            )
            result = "committed"
        except StorageRefusal as exc:
            assert exc.code == "version_conflict"
            result = "refused"
        with lock:
            outcomes.append(result)

    left = threading.Thread(target=writer, args=(first, "Writer left"))
    right = threading.Thread(target=writer, args=(second, "Writer right"))
    left.start()
    right.start()
    left.join(timeout=5)
    right.join(timeout=5)
    assert sorted(outcomes) == ["committed", "refused"]
    rollback = first.export_bytes(format_name="json", purpose="rollback", max_records=1000)
    assert rollback == first.export_bytes(format_name="json", purpose="rollback", max_records=1000)
    exported = json.loads(rollback)
    assert exported["tables"]["project_aliases"]
    assert ROOT_MARKER.encode() in rollback
    inspection = first.export_bytes(format_name="json", purpose="inspection", max_records=1000)
    assert ROOT_MARKER.encode() not in inspection and REPOSITORY.encode() not in inspection
    first.close()
    second.close()


def add_roster_tasks(store: SQLiteStorage, project_id: str) -> None:
    rows = [
        ("task:blocked", project_id, "Blocked synthetic task", "blocked", AT3),
        ("task:ready", project_id, "Ready synthetic task", "ready_to_land", AT3),
        ("task:stale", project_id, "Stale synthetic task", "working", AT1),
        ("task:unresolved", None, "Unresolved synthetic task", "active", AT3),
        ("task:old-complete", project_id, "Old completed synthetic task", "completed", AT1),
    ]
    with store._transaction():
        store.connection.executemany(
            """
            INSERT INTO tasks
              (task_id,project_id,summary,state,version,current_owner_agent_id,
               current_owner_squad_id,updated_at)
            VALUES(?,?,?,?,1,NULL,NULL,?)
            """,
            rows,
        )


def test_roster_groups_evidence_bounds_empty_and_public_safety(root: Path) -> None:
    empty_state, _ = migrated_state(root, "empty")
    with SQLiteStorage(empty_state) as store:
        empty = store.roster_snapshot(
            as_of=AT5, recent_since=AT2, stale_before=AT2, visibility="outbound"
        )
        assert empty["counts"] == {
            "needs_action": 0,
            "recently_finished": 0,
            "underway": 0,
            "unresolved": 0,
            "projects": 0,
            "squads": 0,
        }

    _, state, _ = seeded_state(root, "roster")
    with SQLiteStorage(state) as store:
        project = catalog_project(store)
        project_id = str(project["project_id"])
        add_roster_tasks(store, project_id)
        snapshot = store.roster_snapshot(
            as_of=AT5,
            recent_since=AT2,
            stale_before=AT2,
            limit=100,
            visibility="local",
        )
        assert snapshot["snapshot"]["transaction"] == "one-bounded-read"
        assert snapshot["canonical"] is False and snapshot["read_only"] is True
        project_group = next(
            group for group in snapshot["projects"]
            if group["project"] and group["project"]["project_id"] == project_id
        )
        needs = {item.get("task_id") for item in project_group["groups"]["needs_action"]}
        recent = {item.get("task_id") for item in project_group["groups"]["recently_finished"]}
        underway = {item.get("task_id") for item in project_group["groups"]["underway"]}
        assert {"task:blocked", "task:stale"} <= needs
        assert "task:ready" in recent and "task:old-complete" not in recent
        assert TASK_ID in underway
        unresolved_group = next(group for group in snapshot["projects"] if group["project"] is None)
        assert "task:unresolved" in {
            item.get("task_id") for item in unresolved_group["groups"]["unresolved"]
        }
        task_item = next(item for item in project_group["groups"]["underway"] if item.get("task_id") == TASK_ID)
        assert any(link["key"] == {"task_id": TASK_ID} for link in task_item["evidence_links"])
        assert all(link["locator"].startswith("league://") for link in task_item["evidence_links"])
        large = store.roster_snapshot(
            as_of=AT5, recent_since=AT2, stale_before=AT2, limit=2, visibility="outbound"
        )
        assert large["truncated"]["items"]
        outbound = json.dumps(large, sort_keys=True)
        assert ROOT_MARKER not in outbound and REPOSITORY not in outbound
        assert "Synthetic storage task" not in outbound

    command = invoke_cli(
        state,
        "roster",
        "snapshot",
        "--as-of",
        AT5,
        "--recent-since",
        AT2,
        "--stale-before",
        AT2,
        "--limit",
        "10",
    )
    assert command["command"] == "roster.snapshot"
    assert command["result"]["schema"] == "league.roster-snapshot.v1"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-project-roster-") as temporary:
        root = Path(temporary)
        test_exact_identity_ambiguity_redaction_and_cli(root)
        test_many_to_many_advice_never_rebinds(root)
        test_catalog_two_writer_cas_and_deterministic_export(root)
        test_roster_groups_evidence_bounds_empty_and_public_safety(root)
    print("PASS: canonical catalog, advisory Squads, CAS, deterministic export, and bounded project Roster")


if __name__ == "__main__":
    main()
