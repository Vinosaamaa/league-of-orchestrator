"""Synthetic explicit-root setup for the grouped request lifecycle."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from league.sqlite_store import DATABASE_NAME, SQLiteStorage

from lifecycle_fakes import FakeClock
from storage_fixture import REPOSITORY, SHOTCALLER_ID, TASK_ID
from storage_test_support import seeded_state


JARVAN_ID = "44444444-4444-4444-8444-444444444444"
GAREN_RUNTIME = "runtime:garen:one"
GAREN_RUNTIME_TWO = "runtime:garen:two"
JARVAN_RUNTIME = "runtime:jarvan:one"
LUX_ID = "55555555-5555-4555-8555-555555555555"
AHRI_ID = "66666666-6666-4666-8666-666666666666"
SONA_ID = "77777777-7777-4777-8777-777777777777"


class SyntheticLifecycleSeeder:
    """Test-only setup adapter that keeps schema writes out of scenario code."""

    def __init__(self, store: SQLiteStorage, clock: FakeClock) -> None:
        self.store = store
        self.clock = clock

    def seed(self) -> None:
        with self.store._transaction():
            self.store.connection.executemany(
                """
                INSERT INTO callsigns(callsign,pool_role,enabled,pool_position,last_released_at)
                VALUES(?,?,1,?,NULL)
                """,
                (("Jarvan", "shotcaller", 1), ("Ahri", "champion", 99), ("Sona", "champion", 100)),
            )
        self.store.reserve_callsign(
            "Jarvan",
            JARVAN_ID,
            TASK_ID,
            "shotcaller",
            "working",
            "Synthetic Shotcaller ready",
            self.clock.now(),
        )


def _prepared_database(parent: Path) -> Path:
    state = parent / ".request-lifecycle-baseline" / "state"
    database = state / DATABASE_NAME
    if not database.exists():
        _, state, _ = seeded_state(parent, ".request-lifecycle-baseline")
        with SQLiteStorage(state) as store:
            if store.policy.journal_mode == "WAL":
                store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return database


def create_context(parent: Path, name: str = "request-lifecycle") -> tuple[Path, SQLiteStorage, FakeClock]:
    root = parent / name
    state = root / "state"
    state.mkdir(parents=True)
    shutil.copy2(_prepared_database(parent), state / DATABASE_NAME)
    os.chmod(state / DATABASE_NAME, 0o600)
    store = SQLiteStorage(state)
    clock = FakeClock()
    SyntheticLifecycleSeeder(store, clock).seed()
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
