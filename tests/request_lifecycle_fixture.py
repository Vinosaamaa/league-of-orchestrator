"""Synthetic explicit-root setup for the grouped request lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from league.sqlite_store import SQLiteStorage

from lifecycle_fakes import FakeClock
from storage_fixture import REPOSITORY, SHOTCALLER_ID
from storage_test_support import seeded_state


JARVAN_ID = "44444444-4444-4444-8444-444444444444"
GAREN_RUNTIME = "runtime:garen:one"
GAREN_RUNTIME_TWO = "runtime:garen:two"
JARVAN_RUNTIME = "runtime:jarvan:one"
LUX_ID = "55555555-5555-4555-8555-555555555555"
AHRI_ID = "66666666-6666-4666-8666-666666666666"
SONA_ID = "77777777-7777-4777-8777-777777777777"


def create_context(parent: Path, name: str = "request-lifecycle") -> tuple[Path, SQLiteStorage, FakeClock]:
    _, state, _ = seeded_state(parent, name)
    store = SQLiteStorage(state)
    clock = FakeClock()
    with store._transaction():
        store.connection.execute(
            "INSERT INTO callsigns(callsign,pool_role,enabled,pool_position,last_released_at) VALUES('Jarvan','shotcaller',1,1,NULL)"
        )
        store.connection.execute(
            """
            INSERT INTO agent_instances
              (agent_id,callsign,role,shotcaller_agent_id,task_id,kind,address,thread_id,
               backend,routing_name,display_agent,repository,issue,branch,worktree,status,
               version,updated_at,update_text,blocker,next_action,metadata_json,retired_at)
            VALUES(?, 'Jarvan','shotcaller',NULL,NULL,'codex-thread','synthetic:jarvan','thread:jarvan',
                   'herdr','jarvan','codex',NULL,NULL,NULL,NULL,'working',1,?,
                   'Synthetic Shotcaller ready',NULL,'Accept routed work','{}',NULL)
            """,
            (JARVAN_ID, clock.now()),
        )
        store.connection.execute(
            "INSERT INTO callsign_leases(callsign,agent_id,launch_attempt_id,reserved_at) VALUES('Jarvan',?,NULL,?)",
            (JARVAN_ID, clock.now()),
        )
        store.connection.execute(
            "INSERT OR IGNORE INTO callsigns(callsign,pool_role,enabled,pool_position,last_released_at) VALUES('Ahri','champion',1,99,NULL)"
        )
        store.connection.execute(
            "INSERT OR IGNORE INTO callsigns(callsign,pool_role,enabled,pool_position,last_released_at) VALUES('Sona','champion',1,100,NULL)"
        )
    for runtime_id, actor_id, endpoint, generation in (
        (GAREN_RUNTIME, SHOTCALLER_ID, "synthetic:garen:one", "generation:garen:one"),
        (GAREN_RUNTIME_TWO, SHOTCALLER_ID, "synthetic:garen:two", "generation:garen:two"),
        (JARVAN_RUNTIME, JARVAN_ID, "synthetic:jarvan", "generation:jarvan"),
    ):
        store.register_runtime(
            runtime_id,
            actor_id,
            "codex-thread",
            "herdr",
            f"session:{runtime_id}",
            endpoint,
            generation,
            "active",
            True,
            clock.now(),
        )
    return state, store, clock


def capture_p100(store: SQLiteStorage, clock: FakeClock) -> dict[str, Any]:
    prompt = store.intake_prompt(
        "P100",
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        "session:p100",
        "source:p100",
        "Answer A; investigate B; implement C",
        clock.now(),
    )
    triage = store.triage_prompt(
        "P100",
        [
            {
                "prompt_item_id": "PI100-1",
                "ordinal": 1,
                "summary": "Answer a bounded question",
                "disposition": "new_request",
                "request_id": "R1",
            },
            {
                "prompt_item_id": "PI100-2",
                "ordinal": 2,
                "summary": "Investigate a routed concern",
                "disposition": "new_request",
                "request_id": "R2",
            },
            {
                "prompt_item_id": "PI100-3",
                "ordinal": 3,
                "summary": "Coordinate a local Champion",
                "disposition": "new_request",
                "request_id": "R3",
            },
        ],
        clock.now(),
    )
    return {"prompt": prompt, "triage": triage, "repository": REPOSITORY}
