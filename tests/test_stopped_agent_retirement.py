#!/usr/bin/env python3
"""Exact stopped-agent retirement without repository cleanup."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys
import subprocess
import json
import os
import threading


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.multiplexer_adapters import MultiplexerAdapterRegistry
from league.multiplexer_adapters.herdr.adapter import HerdrMultiplexerAdapter
from league.agent_adapters import builtin_agent_adapter_registry
from league.sqlite_handoff_schema import (
    CHAMPION_SEED,
    SHOTCALLER_SEED,
    SHUFFLE_VERSION,
)
from league.sqlite_store import SQLiteStorage
from league.stopped_retirement import RetirementSpec, StoppedAgentRetirement
from league.storage import StorageRefusal
from league.storage import RuntimeRegistrationCommand
from storage_test_support import migrated_state


AT1 = "2026-09-02T16:00:00Z"
AT2 = "2026-09-02T16:01:00Z"
AT3 = "2026-09-02T16:02:00Z"


class StoppedMultiplexer:
    kind = "herdr"
    capabilities = frozenset({"stopped_retirement"})

    def __init__(self) -> None:
        self.targets: list[dict[str, object]] = []
        self.adapter_providers: list[tuple[str, str]] = []

    def verify_stopped_agent(self, **inputs):
        assert isinstance(inputs.get("process_names"), frozenset)
        assert inputs["process_names"]
        target = dict(inputs["target"])
        self.targets.append(target)
        self.adapter_providers.append(
            (inputs["adapter_kind"], inputs["provider_kind"])
        )
        return {
            "schema": "league.stopped-agent-proof.v1",
            "verified": True,
            "adapter_kind": inputs["adapter_kind"],
            "provider_kind": inputs["provider_kind"],
            "multiplexer_kind": self.kind,
            "runtime_instance_id": target["runtime_instance_id"],
            "session_ref": target["session_ref"],
            "endpoint": target["endpoint"],
            "runtime_generation": target["runtime_generation"],
            "endpoint_absent": True,
        }


class TransactionCheckingMultiplexer(StoppedMultiplexer):
    def __init__(self, store: SQLiteStorage) -> None:
        super().__init__()
        self.store = store
        self.observed_transaction = False

    def verify_stopped_agent(self, **inputs):
        self.observed_transaction = self.store.connection.in_transaction
        assert self.observed_transaction
        return super().verify_stopped_agent(**inputs)


class OversizedProofMultiplexer(StoppedMultiplexer):
    def verify_stopped_agent(self, **inputs):
        proof = dict(super().verify_stopped_agent(**inputs))
        proof["detail"] = "x" * 20_000
        return proof


class BlockingMultiplexer(StoppedMultiplexer):
    def __init__(self) -> None:
        super().__init__()
        self.proof_started = threading.Event()
        self.release_proof = threading.Event()

    def verify_stopped_agent(self, **inputs):
        self.proof_started.set()
        assert self.release_proof.wait(timeout=5)
        return super().verify_stopped_agent(**inputs)


def _active_cursor(
    store: SQLiteStorage,
    retained: Path,
    *,
    harness_kind: str = "cursor",
    provider_kind: str = "cursor",
    multiplexer_kind: str = "herdr",
) -> dict[str, object]:
    store.reconcile_callsign_pool(
        "champion",
        1,
        CHAMPION_SEED,
        SHUFFLE_VERSION,
        [{"callsign": "Lux", "enabled": True, "capabilities": ["backend.herdr"]}],
        AT1,
    )
    assignment = store.allocate_callsign(
        "callsign-assignment:stopped",
        "agent:stopped",
        "champion",
        "task",
        "task:transferred",
        ["backend.herdr"],
        AT1,
    )
    store.activate_callsign(
        assignment["assignment_id"],
        1,
        {
            "schema": "league.runtime-acceptance.v1",
            "verified": True,
            "assignment_id": assignment["assignment_id"],
            "agent_id": "agent:stopped",
            "callsign": "Lux",
            "runtime_instance_id": "runtime:stopped",
            "harness_kind": harness_kind,
            "backend_kind": multiplexer_kind,
            "session_identity": "cursor-session-exact",
            "endpoint_identity": "workspace:pane-stopped",
            "endpoint_generation": "generation-exact",
            "routing_name": "lux",
            "display_agent": provider_kind,
            "capabilities": ["backend.herdr"],
        },
        AT2,
    )
    store.connection.execute(
        """
        UPDATE agent_instances
           SET repository=?,branch='retained-branch',worktree=?
         WHERE agent_id='agent:stopped'
        """,
        ("https://example.invalid/retained.git", str(retained)),
    )
    return assignment


def _spec(
    assignment_id: str,
    *,
    operation_id: str = "retirement:stopped",
    provider_kind: str = "cursor",
    multiplexer_kind: str = "herdr",
) -> RetirementSpec:
    return RetirementSpec(
        operation_id=operation_id,
        agent_id="agent:stopped",
        runtime_instance_id="runtime:stopped",
        session_ref="cursor-session-exact",
        endpoint="workspace:pane-stopped",
        runtime_generation="generation-exact",
        provider_kind=provider_kind,
        multiplexer_kind=multiplexer_kind,
        expected_agent_version=2,
        callsign_assignment_id=assignment_id,
        expected_callsign_version=2,
        terminal_status="cancelled",
        at=AT3,
    )


def _attach_squad_membership(store: SQLiteStorage) -> None:
    store.reconcile_callsign_pool(
        "shotcaller",
        1,
        SHOTCALLER_SEED,
        SHUFFLE_VERSION,
        [{"callsign": "Ashe", "enabled": True, "capabilities": ["backend.herdr"]}],
        AT1,
    )
    owner = store.allocate_callsign(
        "callsign-assignment:owner",
        "agent:owner",
        "shotcaller",
        "shotcaller",
        "shotcaller:owner",
        ["backend.herdr"],
        AT1,
    )
    store.activate_callsign(
        str(owner["assignment_id"]),
        1,
        {
            "schema": "league.runtime-acceptance.v1",
            "verified": True,
            "assignment_id": owner["assignment_id"],
            "agent_id": "agent:owner",
            "callsign": owner["callsign"],
            "runtime_instance_id": "runtime:owner",
            "harness_kind": "codex",
            "backend_kind": "herdr",
            "session_identity": "owner-session-exact",
            "endpoint_identity": "workspace:pane-owner",
            "endpoint_generation": "generation-owner",
            "routing_name": str(owner["callsign"]).lower(),
            "display_agent": "codex",
            "capabilities": ["backend.herdr"],
        },
        AT2,
    )
    store.connection.execute(
        """
        INSERT INTO squads
          (squad_id,shotcaller_agent_id,state,version,updated_at,owner_fence)
        VALUES('squad:retirement','agent:owner','active',1,?,1)
        """,
        (AT2,),
    )
    store.connection.execute(
        """
        INSERT INTO squad_champions(squad_id,champion_agent_id,joined_at)
        VALUES('squad:retirement','agent:stopped',?)
        """,
        (AT2,),
    )
    store.connection.execute(
        """
        UPDATE agent_instances SET shotcaller_agent_id='agent:owner'
         WHERE agent_id='agent:stopped'
        """
    )


def _registry(multiplexer: StoppedMultiplexer) -> MultiplexerAdapterRegistry:
    registry = MultiplexerAdapterRegistry()
    registry.register(multiplexer)
    return registry


def test_runtime_and_multiplexer_adapters_are_registry_dispatched(root: Path) -> None:
    fixtures = (
        ("codex-thread", "codex", "herdr", "codex"),
        ("cursor-thread", "cursor", "herdr", "cursor"),
        ("pi-thread", "cursor", "herdr", "pi"),
        ("pi-thread", "codex", "tmux", "pi"),
    )
    for index, (runtime_kind, provider_kind, multiplexer_kind, adapter_kind) in enumerate(fixtures):
        case = root / str(index)
        case.mkdir(parents=True)
        retained = case / "retained-worktree"
        retained.mkdir()
        state, _ = migrated_state(case, "state")
        multiplexer = StoppedMultiplexer()
        multiplexer.kind = multiplexer_kind
        with SQLiteStorage(state) as store:
            assignment = _active_cursor(
                store,
                retained,
                harness_kind=runtime_kind,
                provider_kind=provider_kind,
                multiplexer_kind=multiplexer_kind,
            )
            result = StoppedAgentRetirement(
                store, multiplexer_registry=_registry(multiplexer)
            ).retire(
                _spec(
                    str(assignment["assignment_id"]),
                    operation_id=f"retirement:matrix:{index}",
                    provider_kind=provider_kind,
                    multiplexer_kind=multiplexer_kind,
                )
            )
            assert result["adapter_kind"] == adapter_kind
            assert result["provider_kind"] == provider_kind
            assert result["multiplexer_kind"] == multiplexer_kind
            assert multiplexer.adapter_providers == [(adapter_kind, provider_kind)]


def test_stopped_endpoint_retirement_is_atomic_and_preserves_repository_state(
    root: Path,
) -> None:
    root.mkdir(parents=True)
    retained = root / "retained-worktree"
    retained.mkdir()
    marker = retained / "dirty-unpublished.txt"
    marker.write_text("preserve me\n", encoding="utf-8")
    state, _ = migrated_state(root, "state")
    multiplexer = StoppedMultiplexer()
    registry = _registry(multiplexer)

    with SQLiteStorage(state) as store:
        assignment = _active_cursor(store, retained)
        _attach_squad_membership(store)
        callsign_plan = store.connection.execute(
            "EXPLAIN QUERY PLAN SELECT callsign_assignment_id FROM callsign_assignments "
            "WHERE agent_id=? AND state='active' ORDER BY callsign_assignment_id LIMIT 2",
            ("agent:stopped",),
        ).fetchall()
        assert any(
            "ix_callsign_assignments_agent_state" in str(row["detail"])
            for row in callsign_plan
        )
        task_plan = store.connection.execute(
            "EXPLAIN QUERY PLAN SELECT task_assignment_id FROM task_assignments "
            "WHERE champion_agent_id=? AND state IN "
            "('pending','launching','active','cleanup_pending') LIMIT 1",
            ("agent:stopped",),
        ).fetchall()
        assert any(
            "ix_task_assignments_champion_state" in str(row["detail"])
            for row in task_plan
        )
        try:
            store.release_callsign(
                str(assignment["assignment_id"]),
                2,
                "pre-retirement-release-proof",
                AT3,
            )
        except StorageRefusal as exc:
            assert exc.code == "runtime_active"
        else:
            raise AssertionError("stale active runtime did not fence callsign release")
        result = StoppedAgentRetirement(
            store, multiplexer_registry=registry
        ).retire(
            _spec(str(assignment["assignment_id"]))
        )

        assert result["state"] == "completed"
        assert result["idempotent"] is False
        runtime = store.connection.execute(
            "SELECT status,verified FROM runtime_instances WHERE runtime_instance_id='runtime:stopped'"
        ).fetchone()
        assert tuple(runtime) == ("closed", 0)
        agent = store.agent_status("agent:stopped")
        assert agent is not None
        assert agent["status"] == "cancelled"
        assert agent["retired_at"] == AT3
        callsign = store.callsign_assignment_status(str(assignment["assignment_id"]))
        assert callsign is not None and callsign["state"] == "released"
        queue = store.callsign_status("champion")
        assert queue["counts"]["available"] == 1
        assert queue["entries"][0]["callsign"] == "Lux"
        assert queue["entries"][0]["state"] == "available"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squad_champions WHERE champion_agent_id='agent:stopped'"
        ).fetchone()[0] == 0
        assert store.agent_status("agent:owner")["retired_at"] is None
        roster = store.roster_snapshot(
            as_of=AT3,
            recent_since=AT1,
            stale_before=AT1,
            visibility="local",
        )
        roster_agents = [
            item["agent_id"]
            for project in roster["projects"]
            for group in project["groups"].values()
            for item in group
            if item["kind"] == "agent"
        ]
        assert "agent:stopped" not in roster_agents
        inspection = json.loads(
            store.export_bytes(
                format_name="json", purpose="inspection", max_records=1000
            )
        )
        retirement_row = inspection["tables"]["stopped_agent_retirements"][0]
        for field in (
            "session_ref",
            "endpoint",
            "runtime_generation",
            "proof_json",
            "receipt_json",
        ):
            assert retirement_row[field] == "[redacted]"

    assert marker.read_text(encoding="utf-8") == "preserve me\n"
    assert retained.is_dir()
    assert len(multiplexer.targets) == 1


def test_exact_retry_after_storage_restart_is_idempotent(root: Path) -> None:
    root.mkdir(parents=True)
    retained = root / "retained-worktree"
    retained.mkdir()
    state, _ = migrated_state(root, "state")
    first_adapter = StoppedMultiplexer()
    with SQLiteStorage(state) as store:
        assignment = _active_cursor(store, retained)
        spec = _spec(str(assignment["assignment_id"]), operation_id="retirement:restart")
        first = StoppedAgentRetirement(
            store, multiplexer_registry=_registry(first_adapter)
        ).retire(spec)
    retry_adapter = StoppedMultiplexer()
    with SQLiteStorage(state) as reopened:
        second = StoppedAgentRetirement(
            reopened, multiplexer_registry=_registry(retry_adapter)
        ).retire(spec)
        assert second["idempotent"] is True
        assert {
            key: value for key, value in second.items() if key != "idempotent"
        } == {
            key: value for key, value in first.items() if key != "idempotent"
        }
        assert second["proof_digest"] == first["proof_digest"]
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM stopped_agent_retirements WHERE operation_id='retirement:restart'"
        ).fetchone()[0] == 1
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_id='retirement:retirement:restart'"
        ).fetchone()[0] == 1
    assert len(first_adapter.targets) == 1
    assert retry_adapter.targets == []


def test_absence_proof_is_linearized_inside_canonical_transaction(root: Path) -> None:
    root.mkdir(parents=True)
    retained = root / "retained-worktree"
    retained.mkdir()
    state, _ = migrated_state(root, "state")
    with SQLiteStorage(state) as store:
        assignment = _active_cursor(store, retained)
        multiplexer = TransactionCheckingMultiplexer(store)
        result = StoppedAgentRetirement(
            store, multiplexer_registry=_registry(multiplexer)
        ).retire(
            _spec(
                str(assignment["assignment_id"]),
                operation_id="retirement:linearized-proof",
            )
        )
        assert result["state"] == "completed"
        assert multiplexer.observed_transaction


def test_supported_runtime_resume_cannot_interleave_with_bounded_proof(
    root: Path,
) -> None:
    root.mkdir(parents=True)
    retained = root / "retained-worktree"
    retained.mkdir()
    state, _ = migrated_state(root, "state")
    with SQLiteStorage(state) as setup:
        assignment = _active_cursor(setup, retained)
    multiplexer = BlockingMultiplexer()
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def retire() -> None:
        try:
            with SQLiteStorage(state) as owner:
                results.append(
                    StoppedAgentRetirement(
                        owner, multiplexer_registry=_registry(multiplexer)
                    ).retire(
                        _spec(
                            str(assignment["assignment_id"]),
                            operation_id="retirement:concurrency",
                        )
                    )
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=retire, name="synthetic-retirement-owner")
    thread.start()
    assert multiplexer.proof_started.wait(timeout=5)
    try:
        with SQLiteStorage(state, busy_timeout_ms=50) as competing:
            competing.register_runtime(
                RuntimeRegistrationCommand(
                    runtime_instance_id="runtime:stopped",
                    actor_agent_id="agent:stopped",
                    harness_kind="cursor-thread",
                    backend_kind="herdr",
                    session_ref="cursor-session-exact",
                    endpoint="workspace:pane-stopped",
                    runtime_generation="generation-exact",
                    status="active",
                    verified=True,
                    at="2026-09-02T16:01:30Z",
                )
            )
    except StorageRefusal as exc:
        assert exc.code == "busy" and exc.retryable
    else:
        raise AssertionError("supported runtime resume interleaved with retirement proof")
    finally:
        multiplexer.release_proof.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1 and results[0]["state"] == "completed"


def test_failure_rolls_back_every_canonical_retirement_change(root: Path) -> None:
    root.mkdir(parents=True)
    for interrupted_phase in ("after_runtime_closed", "after_callsign_released"):
        case = root / interrupted_phase
        case.mkdir()
        retained = case / "retained-worktree"
        retained.mkdir()
        state, _ = migrated_state(case, "state")
        adapter = StoppedMultiplexer()
        with SQLiteStorage(state) as store:
            assignment = _active_cursor(store, retained)
            spec = _spec(
                str(assignment["assignment_id"]),
                operation_id=f"retirement:rollback:{interrupted_phase}",
            )

            def interrupt(phase: str) -> None:
                if phase == interrupted_phase:
                    raise RuntimeError("synthetic interruption")

            try:
                StoppedAgentRetirement(
                    store, multiplexer_registry=_registry(adapter)
                ).retire(spec, fault=interrupt)
            except RuntimeError as exc:
                assert str(exc) == "synthetic interruption"
            else:
                raise AssertionError("expected synthetic interruption")
            runtime = store.connection.execute(
                "SELECT status,verified FROM runtime_instances WHERE runtime_instance_id='runtime:stopped'"
            ).fetchone()
            assert tuple(runtime) == ("active", 1)
            assert store.agent_status("agent:stopped")["retired_at"] is None
            assert store.callsign_assignment_status(
                str(assignment["assignment_id"])
            )["state"] == "active"
            assert store.stopped_agent_retirement(spec.operation_id) is None
        with SQLiteStorage(state) as restarted:
            completed = StoppedAgentRetirement(
                restarted, multiplexer_registry=_registry(adapter)
            ).retire(spec)
            assert completed["state"] == "completed"


class LiveMultiplexer(StoppedMultiplexer):
    def verify_stopped_agent(self, **inputs):
        self.targets.append(dict(inputs["target"]))
        raise StorageRefusal(
            "stopped_retirement_endpoint_live", "exact endpoint remains live"
        )


class HerdrInventoryRunner:
    def __init__(
        self,
        agents: list[dict[str, object]],
        *,
        process_info: dict[str, object] | None = None,
        process_failure: subprocess.CompletedProcess[str] | None = None,
    ) -> None:
        self.agents = agents
        self.process_info = process_info
        self.process_failure = process_failure
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments, timeout_seconds: int = 30):
        del timeout_seconds
        self.calls.append(tuple(arguments))
        if tuple(arguments[1:3]) == ("pane", "process-info"):
            if self.process_info is None:
                if self.process_failure is not None:
                    return self.process_failure
                return subprocess.CompletedProcess(
                    arguments,
                    1,
                    "",
                    json.dumps({"error": {"code": "pane_not_found"}}),
                )
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps({"result": {"process_info": self.process_info}}),
                "",
            )
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps({"result": {"agents": self.agents}}),
            "",
        )


def test_herdr_proves_absence_and_refuses_live_or_ambiguous_identity() -> None:
    target = {
        "runtime_instance_id": "runtime:stopped",
        "session_ref": "cursor-session-exact",
        "endpoint": "workspace:pane-stopped",
        "runtime_generation": "generation-exact",
        "routing_name": "lux",
    }
    cursor = builtin_agent_adapter_registry().adapter("cursor")
    absent_runner = HerdrInventoryRunner([])
    proof = cursor.verify_stopped_retirement(
        target=target,
        provider_kind="cursor",
        multiplexer=HerdrMultiplexerAdapter(absent_runner, binary="test-herdr"),
    )
    assert proof["verified"] is True and proof["endpoint_absent"] is True
    assert absent_runner.calls == [
        ("test-herdr", "pane", "process-info", "--pane", "workspace:pane-stopped"),
        ("test-herdr", "agent", "list"),
    ]

    orphan_runner = HerdrInventoryRunner(
        [],
        process_info={
            "foreground_processes": [
                {
                    "pid": 123,
                    "argv0": "cursor-agent",
                    "cwd": "/synthetic/retained",
                    "process_start": "synthetic-start",
                }
            ]
        },
    )
    try:
        cursor.verify_stopped_retirement(
            target=target,
            provider_kind="cursor",
            multiplexer=HerdrMultiplexerAdapter(orphan_runner, binary="test-herdr"),
        )
    except StorageRefusal as exc:
        assert exc.code == "stopped_retirement_endpoint_live"
    else:
        raise AssertionError("orphan provider process was treated as absent")

    live = {
        "name": "lux",
        "pane_id": "workspace:pane-stopped",
        "agent": "cursor",
        "display_agent": "cursor",
        "agent_session": {"value": "cursor-session-exact"},
    }
    for agents, code in (
        ([live], "stopped_retirement_endpoint_live"),
        ([live, {**live, "pane_id": "workspace:pane-second"}],
         "stopped_retirement_identity_ambiguous"),
        ([{**live, "display_agent": "codex"}],
         "stopped_retirement_identity_mismatch"),
    ):
        try:
            cursor.verify_stopped_retirement(
                target=target,
                provider_kind="cursor",
                multiplexer=HerdrMultiplexerAdapter(
                    HerdrInventoryRunner(
                        agents,
                        process_info={
                            "foreground_processes": [
                                {
                                    "pid": 123,
                                    "argv0": "cursor-agent",
                                    "cwd": "/synthetic/retained",
                                    "process_start": "synthetic-start",
                                }
                            ]
                        },
                    ),
                    binary="test-herdr",
                ),
            )
        except StorageRefusal as exc:
            assert exc.code == code, (exc.code, code)
        else:
            raise AssertionError(f"Herdr retirement proof accepted {code}")


def test_herdr_absent_pane_uses_runner_stderr_failure_envelope() -> None:
    target = {
        "runtime_instance_id": "runtime:stopped",
        "session_ref": "cursor-session-exact",
        "endpoint": "workspace:pane-stopped",
        "runtime_generation": "generation-exact",
        "routing_name": "lux",
    }
    runner = HerdrInventoryRunner([])
    proof = builtin_agent_adapter_registry().adapter("cursor").verify_stopped_retirement(
        target=target,
        provider_kind="cursor",
        multiplexer=HerdrMultiplexerAdapter(runner, binary="test-herdr"),
    )
    assert proof["verified"] is True
    assert proof["endpoint_absent"] is True
    assert runner.calls[0] == (
        "test-herdr",
        "pane",
        "process-info",
        "--pane",
        "workspace:pane-stopped",
    )


def test_herdr_refuses_noncanonical_absent_pane_contracts() -> None:
    target = {
        "runtime_instance_id": "runtime:stopped",
        "session_ref": "cursor-session-exact",
        "endpoint": "workspace:pane-stopped",
        "runtime_generation": "generation-exact",
        "routing_name": "lux",
    }
    failures = (
        subprocess.CompletedProcess(
            ("test-herdr",),
            1,
            json.dumps({"error": {"code": "pane_not_found"}}),
            "",
        ),
        subprocess.CompletedProcess(
            ("test-herdr",),
            1,
            "",
            json.dumps({"error": {"code": "not_found"}}),
        ),
        subprocess.CompletedProcess(
            ("test-herdr",),
            2,
            "",
            json.dumps({"error": {"code": "pane_not_found"}}),
        ),
        subprocess.CompletedProcess(
            ("test-herdr",),
            1,
            "",
            json.dumps(
                {"error": {"code": "pane_not_found", "detail": "x" * 70_000}}
            ),
        ),
        subprocess.CompletedProcess(
            ("test-herdr",),
            1,
            "",
            '{"error":{"code":"pane_not_found","detail":NaN}}',
        ),
        subprocess.CompletedProcess(
            ("test-herdr",),
            1,
            "",
            '{"error":{"code":"pane_not_found","detail":Infinity}}',
        ),
        subprocess.CompletedProcess(
            ("test-herdr",),
            1,
            "",
            '{"error":{"code":"pane_not_found","detail":-Infinity}}',
        ),
        subprocess.CompletedProcess(
            ("test-herdr",),
            1,
            "",
            json.dumps(
                {
                    "error": {"code": "pane_not_found"},
                    "result": {"process_info": {"foreground_processes": []}},
                }
            ),
        ),
        subprocess.CompletedProcess(
            ("test-herdr",),
            0,
            json.dumps(
                {
                    "result": {"process_info": {"foreground_processes": []}},
                    "error": {"code": "pane_not_found"},
                }
            ),
            "",
        ),
    )
    cursor = builtin_agent_adapter_registry().adapter("cursor")
    for completed in failures:
        try:
            cursor.verify_stopped_retirement(
                target=target,
                provider_kind="cursor",
                multiplexer=HerdrMultiplexerAdapter(
                    HerdrInventoryRunner([], process_failure=completed),
                    binary="test-herdr",
                ),
            )
        except StorageRefusal as exc:
            assert exc.code == "stopped_retirement_process_unavailable"
        else:
            raise AssertionError("noncanonical absent-pane proof was accepted")


def test_live_endpoint_and_stale_generation_refuse_without_mutation(root: Path) -> None:
    root.mkdir(parents=True)
    retained = root / "retained-worktree"
    retained.mkdir()
    state, _ = migrated_state(root, "state")
    with SQLiteStorage(state) as store:
        assignment = _active_cursor(store, retained)
        live = LiveMultiplexer()
        try:
            StoppedAgentRetirement(
                store, multiplexer_registry=_registry(live)
            ).retire(_spec(str(assignment["assignment_id"]), operation_id="retirement:live"))
        except StorageRefusal as exc:
            assert exc.code == "stopped_retirement_endpoint_live"
        else:
            raise AssertionError("live endpoint retirement was accepted")

        stale = _spec(str(assignment["assignment_id"]), operation_id="retirement:stale")
        stale = RetirementSpec(
            **{**stale.__dict__, "runtime_generation": "generation-stale"}
        )
        try:
            StoppedAgentRetirement(
                store, multiplexer_registry=_registry(StoppedMultiplexer())
            ).retire(stale)
        except StorageRefusal as exc:
            assert exc.code == "stopped_retirement_identity_mismatch"
        else:
            raise AssertionError("stale runtime generation was accepted")

        runtime = store.connection.execute(
            "SELECT status,verified FROM runtime_instances WHERE runtime_instance_id='runtime:stopped'"
        ).fetchone()
        assert tuple(runtime) == ("active", 1)
        assert store.agent_status("agent:stopped")["retired_at"] is None
        assert store.callsign_assignment_status(str(assignment["assignment_id"]))["state"] == "active"


def test_provider_operation_and_unsupported_multiplexer_refuse_exactly(root: Path) -> None:
    root.mkdir(parents=True)
    for name, expected_code in (
        ("provider", "stopped_retirement_provider_mismatch"),
        ("operation", "stopped_retirement_operation_conflict"),
        ("multiplexer", "stopped_retirement_multiplexer_unsupported"),
    ):
        case = root / name
        case.mkdir()
        retained = case / "retained-worktree"
        retained.mkdir()
        state, _ = migrated_state(case, "state")
        with SQLiteStorage(state) as store:
            if name == "multiplexer":
                assignment = _active_cursor(
                    store, retained, multiplexer_kind="tmux"
                )
                spec = _spec(
                    str(assignment["assignment_id"]),
                    operation_id="retirement:unsupported",
                    multiplexer_kind="tmux",
                )
                retirement = StoppedAgentRetirement(store)
            else:
                assignment = _active_cursor(store, retained)
                retirement = StoppedAgentRetirement(
                    store,
                    multiplexer_registry=_registry(StoppedMultiplexer()),
                )
                spec = _spec(
                    str(assignment["assignment_id"]),
                    operation_id=f"retirement:{name}",
                    provider_kind="codex" if name == "provider" else "cursor",
                )
                if name == "operation":
                    retirement.retire(spec)
                    spec = RetirementSpec(
                        **{**spec.__dict__, "terminal_status": "failed"}
                    )
            try:
                retirement.retire(spec)
            except StorageRefusal as exc:
                assert exc.code == expected_code, (name, exc.code, expected_code)
            else:
                raise AssertionError(f"{name} retirement refusal was not enforced")
            if name != "operation":
                runtime = store.connection.execute(
                    "SELECT status,verified FROM runtime_instances WHERE runtime_instance_id='runtime:stopped'"
                ).fetchone()
                assert tuple(runtime) == ("active", 1)
                assert store.agent_status("agent:stopped")["retired_at"] is None


def test_untransferred_task_ownership_refuses_without_partial_retirement(root: Path) -> None:
    root.mkdir(parents=True)
    retained = root / "retained-worktree"
    retained.mkdir()
    state, _ = migrated_state(root, "state")
    with SQLiteStorage(state) as store:
        assignment = _active_cursor(store, retained)
        store.connection.execute(
            """
            INSERT INTO tasks
              (task_id,summary,state,version,current_owner_agent_id,updated_at)
            VALUES('task:owned','still owned','working',1,'agent:stopped',?)
            """,
            (AT2,),
        )
        store.connection.execute(
            "UPDATE agent_instances SET task_id='task:owned' WHERE agent_id='agent:stopped'"
        )
        try:
            StoppedAgentRetirement(
                store,
                multiplexer_registry=_registry(StoppedMultiplexer()),
            ).retire(
                _spec(
                    str(assignment["assignment_id"]),
                    operation_id="retirement:owned",
                )
            )
        except StorageRefusal as exc:
            assert exc.code == "stopped_retirement_work_untransferred"
        else:
            raise AssertionError("retirement orphaned canonical task ownership")
        runtime = store.connection.execute(
            "SELECT status,verified FROM runtime_instances WHERE runtime_instance_id='runtime:stopped'"
        ).fetchone()
        assert tuple(runtime) == ("active", 1)
        assert store.agent_status("agent:stopped")["retired_at"] is None


def test_invalid_timestamp_refuses_before_adapter_inspection(root: Path) -> None:
    root.mkdir(parents=True)
    retained = root / "retained-worktree"
    retained.mkdir()
    state, _ = migrated_state(root, "state")
    multiplexer = StoppedMultiplexer()
    with SQLiteStorage(state) as store:
        assignment = _active_cursor(store, retained)
        spec = _spec(
            str(assignment["assignment_id"]),
            operation_id="retirement:invalid-time",
        )
        spec = RetirementSpec(**{**spec.__dict__, "at": "not-a-time"})
        try:
            StoppedAgentRetirement(
                store, multiplexer_registry=_registry(multiplexer)
            ).retire(spec)
        except StorageRefusal as exc:
            assert exc.code == "stopped_retirement_invalid"
        else:
            raise AssertionError("invalid time reached retirement execution")
        assert multiplexer.targets == []


def test_provider_alias_is_normalized_and_exact_retries_share_one_receipt(
    root: Path,
) -> None:
    root.mkdir(parents=True)
    retained = root / "retained-worktree"
    retained.mkdir()
    state, _ = migrated_state(root, "state")
    multiplexer = StoppedMultiplexer()
    with SQLiteStorage(state) as store:
        assignment = _active_cursor(
            store,
            retained,
            harness_kind="pi-thread",
            provider_kind="codex",
        )
        alias = _spec(
            str(assignment["assignment_id"]),
            operation_id="retirement:provider-alias",
            provider_kind="openai-codex",
        )
        first = StoppedAgentRetirement(
            store, multiplexer_registry=_registry(multiplexer)
        ).retire(alias)
        assert first["provider_kind"] == "codex"
        canonical = RetirementSpec(**{**alias.__dict__, "provider_kind": "codex"})
        second = StoppedAgentRetirement(
            store, multiplexer_registry=_registry(StoppedMultiplexer())
        ).retire(canonical)
        assert second["idempotent"] is True
        assert second["provider_kind"] == "codex"


def test_oversized_input_and_proof_refuse_before_canonical_mutation(root: Path) -> None:
    root.mkdir(parents=True)
    for case_name in ("input", "proof"):
        case = root / case_name
        case.mkdir()
        retained = case / "retained-worktree"
        retained.mkdir()
        state, _ = migrated_state(case, "state")
        multiplexer = (
            StoppedMultiplexer()
            if case_name == "input"
            else OversizedProofMultiplexer()
        )
        with SQLiteStorage(state) as store:
            assignment = _active_cursor(store, retained)
            spec = _spec(
                str(assignment["assignment_id"]),
                operation_id=f"retirement:oversized-{case_name}",
            )
            if case_name == "input":
                spec = RetirementSpec(**{**spec.__dict__, "session_ref": "x" * 2049})
            try:
                StoppedAgentRetirement(
                    store, multiplexer_registry=_registry(multiplexer)
                ).retire(spec)
            except StorageRefusal as exc:
                expected = (
                    "stopped_retirement_invalid"
                    if case_name == "input"
                    else "stopped_retirement_proof_invalid"
                )
                assert exc.code == expected, (case_name, exc.code, expected)
            else:
                raise AssertionError(f"oversized {case_name} was persisted")
            runtime = store.connection.execute(
                "SELECT status,verified FROM runtime_instances WHERE runtime_instance_id='runtime:stopped'"
            ).fetchone()
            assert tuple(runtime) == ("active", 1)


def test_stable_cli_exposes_exact_retirement_identity() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "league"),
            "runtime",
            "retire-stopped-agent",
            "--help",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    for flag in (
        "--operation-id",
        "--agent-id",
        "--runtime-instance-id",
        "--session-ref",
        "--endpoint",
        "--runtime-generation",
        "--provider-kind",
        "--multiplexer-kind",
        "--expected-agent-version",
        "--callsign-assignment-id",
        "--expected-callsign-version",
        "--terminal-status",
        "--at",
    ):
        assert flag in completed.stdout


def test_stable_cli_retires_imported_hook_runtime_end_to_end(root: Path) -> None:
    root.mkdir(parents=True)
    retained = root / "retained-worktree"
    retained.mkdir()
    marker = retained / "unpublished.patch"
    marker.write_text("unpublished\n", encoding="utf-8")
    state, _ = migrated_state(root, "state")
    with SQLiteStorage(state) as store:
        assignment = _active_cursor(store, retained)
        store.connection.execute(
            """
            UPDATE callsign_assignments SET runtime_instance_id=NULL
             WHERE callsign_assignment_id=?
            """,
            (assignment["assignment_id"],),
        )
    fake_herdr = root / "fake-herdr"
    fake_herdr.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = \"pane process-info\" ]; then\n"
        "  printf '%s\\n' '{\"error\":{\"code\":\"pane_not_found\"}}' >&2\n"
        "  exit 1\n"
        "fi\n"
        "printf '%s\\n' '{\"result\":{\"agents\":[]}}'\n",
        encoding="utf-8",
    )
    fake_herdr.chmod(0o700)
    environment = dict(os.environ)
    environment["HERDR_BIN_PATH"] = str(fake_herdr)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "league"),
            "--state-root",
            str(state),
            "runtime",
            "retire-stopped-agent",
            "--operation-id",
            "retirement:cli-imported",
            "--agent-id",
            "agent:stopped",
            "--runtime-instance-id",
            "runtime:stopped",
            "--session-ref",
            "cursor-session-exact",
            "--endpoint",
            "workspace:pane-stopped",
            "--runtime-generation",
            "generation-exact",
            "--provider-kind",
            "cursor",
            "--multiplexer-kind",
            "herdr",
            "--expected-agent-version",
            "2",
            "--callsign-assignment-id",
            str(assignment["assignment_id"]),
            "--expected-callsign-version",
            "2",
            "--terminal-status",
            "cancelled",
            "--at",
            AT3,
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    envelope = json.loads(completed.stdout)
    assert envelope["ok"] is True
    assert envelope["result"]["state"] == "completed"
    assert envelope["result"]["repository_cleanup"] is False
    with SQLiteStorage(state) as store:
        assert store.stopped_agent_retirement("retirement:cli-imported") is not None
        assert store.agent_status("agent:stopped")["retired_at"] == AT3
    assert marker.read_text(encoding="utf-8") == "unpublished\n"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-stopped-retirement-") as temporary:
        test_stopped_endpoint_retirement_is_atomic_and_preserves_repository_state(
            Path(temporary) / "atomic"
        )
        test_exact_retry_after_storage_restart_is_idempotent(
            Path(temporary) / "restart"
        )
        test_absence_proof_is_linearized_inside_canonical_transaction(
            Path(temporary) / "linearized"
        )
        test_supported_runtime_resume_cannot_interleave_with_bounded_proof(
            Path(temporary) / "concurrency"
        )
        test_failure_rolls_back_every_canonical_retirement_change(
            Path(temporary) / "rollback"
        )
        test_live_endpoint_and_stale_generation_refuse_without_mutation(
            Path(temporary) / "refusal"
        )
        test_runtime_and_multiplexer_adapters_are_registry_dispatched(
            Path(temporary) / "registry"
        )
        test_provider_operation_and_unsupported_multiplexer_refuse_exactly(
            Path(temporary) / "boundary"
        )
        test_untransferred_task_ownership_refuses_without_partial_retirement(
            Path(temporary) / "owned"
        )
        test_invalid_timestamp_refuses_before_adapter_inspection(
            Path(temporary) / "invalid-time"
        )
        test_provider_alias_is_normalized_and_exact_retries_share_one_receipt(
            Path(temporary) / "provider-alias"
        )
        test_oversized_input_and_proof_refuse_before_canonical_mutation(
            Path(temporary) / "bounded"
        )
        test_stable_cli_retires_imported_hook_runtime_end_to_end(
            Path(temporary) / "cli-e2e"
        )
    test_stable_cli_exposes_exact_retirement_identity()
    test_herdr_proves_absence_and_refuses_live_or_ambiguous_identity()
    test_herdr_absent_pane_uses_runner_stderr_failure_envelope()
    test_herdr_refuses_noncanonical_absent_pane_contracts()
    print("PASS: exact stopped-agent retirement preserves repository state")


if __name__ == "__main__":
    main()
