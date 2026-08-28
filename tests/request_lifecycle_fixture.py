"""Synthetic explicit-root setup for the grouped request lifecycle."""

from __future__ import annotations

import os
import hashlib
import shutil
from pathlib import Path
from typing import Any

from league.sqlite_store import DATABASE_NAME, SQLiteStorage
from league.storage import DispatchRequestCommand, RuntimeRegistrationCommand

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
        for role, additions in (
            ("shotcaller", ("Jarvan",)),
            ("champion", ("Ahri", "Sona")),
        ):
            status = self.store.callsign_status(role)
            catalog = [
                {
                    "callsign": entry["callsign"],
                    "enabled": entry["enabled"],
                    "capabilities": [],
                }
                for entry in status["entries"]
            ]
            catalog.extend(
                {"callsign": callsign, "enabled": True, "capabilities": []}
                for callsign in additions
            )
            self.store.reconcile_callsign_pool(
                role,
                status["queue_version"],
                status["seed"],
                status["shuffle_version"],
                catalog,
                self.clock.now(),
            )
        self.store.allocate_callsign(
            "callsign-assignment:jarvan-request-fixture",
            JARVAN_ID,
            "shotcaller",
            "squad",
            "squad:Jarvan",
            [],
            self.clock.now(),
        )

    def add_pending_delivery(
        self,
        *,
        event_id: str,
        outbox_id: str,
        recipient_agent_id: str,
        source_agent_id: str,
        update: str,
    ) -> None:
        with self.store._transaction():
            self.store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at,
                   detail_json,request_id,aggregate_kind,aggregate_id)
                VALUES(?,?,NULL,99,'agent_transition','completed',?,?,'{}',NULL,'agent',?)
                """,
                (event_id, source_agent_id, update, self.clock.now(), source_agent_id),
            )
            self.store.connection.execute(
                """
                INSERT INTO delivery_outbox
                  (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                VALUES(?,?,?,'pending',?,0)
                """,
                (outbox_id, event_id, recipient_agent_id, self.clock.now()),
            )

    def set_prompt_payload_body(self, prompt_id: str, body: str) -> None:
        encoded = body.encode("utf-8")
        with self.store._transaction():
            self.store.connection.execute(
                """
                UPDATE prompt_payloads
                   SET body=?,body_hash=?,byte_count=?,pruned_at=NULL
                 WHERE prompt_id=?
                """,
                (body, hashlib.sha256(encoded).hexdigest(), len(encoded), prompt_id),
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
            RuntimeRegistrationCommand(
                runtime_instance_id=runtime_id,
                actor_agent_id=actor_id,
                harness_kind="codex-thread",
                backend_kind="herdr",
                session_ref=f"session:{runtime_id}",
                endpoint=endpoint,
                runtime_generation=generation,
                status="active",
                verified=True,
                at=clock.now(),
            )
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


def dispatch_request(
    store: SQLiteStorage,
    clock: FakeClock,
    request_id: str,
    claim_token: str,
    dispatch_id: str,
    work_kind: str,
    requested_mode: str | None,
    *,
    hidden_supported: bool = False,
    requested_model: str | None = None,
    requested_effort: str | None = None,
    explicit_route: str | None = None,
) -> dict[str, Any]:
    return store.dispatch_request(
        DispatchRequestCommand(
            request_id=request_id,
            claim_token=claim_token,
            dispatch_id=dispatch_id,
            work_kind=work_kind,
            requested_mode=requested_mode,
            hidden_supported=hidden_supported,
            requested_model=requested_model,
            requested_effort=requested_effort,
            explicit_route=explicit_route,
            at=clock.now(),
        )
    )


def observe_runtime(
    store: SQLiteStorage,
    clock: FakeClock,
    *,
    runtime_instance_id: str,
    actor_agent_id: str,
    endpoint: str,
    runtime_generation: str,
    status: str,
    verified: bool = True,
) -> dict[str, Any]:
    return store.register_runtime(
        RuntimeRegistrationCommand(
            runtime_instance_id=runtime_instance_id,
            actor_agent_id=actor_agent_id,
            harness_kind="codex-thread",
            backend_kind="herdr",
            session_ref=f"session:{runtime_instance_id}",
            endpoint=endpoint,
            runtime_generation=runtime_generation,
            status=status,
            verified=verified,
            at=clock.now(),
        )
    )
