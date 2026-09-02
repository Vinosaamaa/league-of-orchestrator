#!/usr/bin/env python3
"""Independent service ownership and exact-once Champion transition delivery."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.persistent_supervisor import (  # noqa: E402
    PersistentSupervisor,
    handoff_transition_delivery,
    stop_supervisor,
    supervisor_status,
)
from league.request_services import AssignmentService, AssignmentSpec  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import RuntimeRegistrationCommand  # noqa: E402
from league.display_replay import canonical_presentations  # noqa: E402
from league.restored_agent import reconcile_restored_agents  # noqa: E402
from lifecycle_fakes import (  # noqa: E402
    FakeDeliveryAdapter,
    FakeIds,
    FakeLaunchAdapter,
    issue_bound_spec,
)
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    GAREN_RUNTIME_TWO,
    LUX_ID,
    capture_p100,
    create_context,
    dispatch_request,
)
from storage_fixture import REPOSITORY, SHOTCALLER_ID  # noqa: E402
from test_multiplexer_metadata import (  # noqa: E402
    AT as RESTART_AT,
    RestoredHerdr,
    _add_standalone_cursor_champion,
    canonical_state,
)


class LiveRuntimeObserver:
    def observe(self, candidates):
        return {
            str(candidate["assignment_id"]): {
                "state": "live", "fingerprint": "synthetic-live",
            }
            for candidate in candidates
        }


def start(runtime: PersistentSupervisor):
    errors: list[BaseException] = []

    def run() -> None:
        try:
            runtime.run(emit_ready=False)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=run, name="synthetic-persistent-supervisor")
    thread.start()
    assert runtime.ready.wait(timeout=5), errors
    return thread, errors


def environment(state: Path, root: Path) -> dict[str, str]:
    pointer = root / "writer-pointer.json"
    pointer.write_text('{"writer":"sqlite"}\n', encoding="utf-8")
    return {
        **os.environ,
        "LEAGUE_STATE_ROOT": str(state),
        "LEAGUE_WRITER_POINTER": str(pointer),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT / "src"),
    }


def run_json(arguments: list[str], env: dict[str, str], payload: dict | None = None) -> dict:
    completed = subprocess.run(
        arguments,
        input=None if payload is None else json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def active_champion(root: Path):
    state, store, clock = create_context(root, "active")
    store.register_runtime(
        RuntimeRegistrationCommand(
            GAREN_RUNTIME_TWO, SHOTCALLER_ID, "codex-thread", "herdr",
            f"session:{GAREN_RUNTIME_TWO}", "synthetic:garen:two",
            "generation:garen:two", "closed", False, clock.now(),
        )
    )
    capture_p100(store, clock)
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    dispatch_request(
        store, clock, "R3", "claim-r3", "dispatch-r3",
        "repository-write", "champion",
    )
    spec = AssignmentSpec(
        assignment_id="assignment:supervisor-delivery", request_id="R3",
        claim_token="claim-r3", task_id="task:supervisor-delivery",
        task_summary="Synthetic supervisor delivery", coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID, callsign="Lux", repository=REPOSITORY, issue=84,
        branch="agent/synthetic/supervisor-delivery",
        worktree="/synthetic/worktrees/supervisor-delivery", issue_receipt=None,
    )
    active = AssignmentService(store, FakeLaunchAdapter(), clock, FakeIds()).assign(
        issue_bound_spec(store, spec, clock.now())
    )
    store.close()
    return state, clock, active


def transition_command(active: dict, at: str) -> list[str]:
    return [
        str(ROOT / "bin" / "league"), "--state-root", str(active["state_root"]),
        "task", "transition", "--task-id", str(active["task_id"]),
        "--runtime-instance-id", str(active["runtime_instance_id"]),
        "--expected-version", "3", "--state", "working",
        "--update", "Synthetic transition owned by the persistent service.",
        "--next-action", "Verify exactly-once service delivery.",
        "--transition-id", "transition:supervisor-delivery",
        "--transition-key", "transition-key:supervisor-delivery",
        "--event-id", "event:supervisor-delivery",
        "--outbox-id", "outbox:supervisor-delivery",
        "--recipient-agent-id", SHOTCALLER_ID, "--at", at,
    ]


def test_transition_commits_then_service_delivers_once(root: Path) -> None:
    state, clock, active = active_champion(root)
    active["state_root"] = state
    adapter = FakeDeliveryAdapter()
    runtime = PersistentSupervisor(
        state, callsign="Garen", lease_seconds=1.0, renew_seconds=0.25,
        recovery_seconds=30, delivery_adapter=adapter,
        runtime_observer=LiveRuntimeObserver(),
    )
    thread, errors = start(runtime)
    env = environment(state, root)
    try:
        service = run_json(
            [str(ROOT / "bin" / "agent-watcher"), "--shotcaller", "Garen", "service-status"],
            env,
        )
        assert service["live"] and service["monitor_live"]
        assert supervisor_status(state, "Garen")["live"]

        command = transition_command(active, clock.now())
        first = run_json(command, env)["result"]
        assert first["delivery"]["state"] == "scheduled"
        deadline = time.monotonic() + 3
        while len(adapter.sent) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(adapter.sent) == 1
        assert adapter.sent[0].envelope["event_id"] == first["event_id"]

        duplicate = run_json(command, env)["result"]
        assert duplicate["idempotent"] and duplicate["delivery"]["state"] == "scheduled"
        time.sleep(0.1)
        assert len(adapter.sent) == 1
        with SQLiteStorage(state) as observer:
            row = observer.connection.execute(
                "SELECT state,attempt_count FROM delivery_outbox WHERE outbox_id=?",
                (first["outbox_id"],),
            ).fetchone()
            receipts = observer.connection.execute(
                "SELECT COUNT(*) FROM recipient_receipts WHERE event_id=? AND recipient_agent_id=?",
                (first["event_id"], SHOTCALLER_ID),
            ).fetchone()[0]
            transitions = observer.connection.execute(
                "SELECT COUNT(*) FROM task_transitions WHERE task_id=?",
                (active["task_id"],),
            ).fetchone()[0]
        assert tuple(row) == ("delivered", 1)
        assert receipts == 1 and transitions == 1
    finally:
        stop_supervisor(state, "Garen")
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def test_stop_exposes_missing_supervisor_without_handoff(root: Path) -> None:
    state, store, clock = create_context(root, "missing")
    store.register_runtime(
        RuntimeRegistrationCommand(
            GAREN_RUNTIME_TWO, SHOTCALLER_ID, "codex-thread", "herdr",
            f"session:{GAREN_RUNTIME_TWO}", "synthetic:garen:two",
            "generation:garen:two", "closed", False, clock.now(),
        )
    )
    binding = store.supervisor_binding("Garen")
    store.register_watcher(
        binding["scope_id"],
        "watcher:persistent:synthetic-missing",
        binding["actor_agent_id"],
        binding["runtime_instance_id"],
        "unix:/tmp/league-supervisor-synthetic-missing.sock",
        clock.after(60),
        1,
        clock.now(),
    )
    store.release_watcher(
        "watcher:persistent:synthetic-missing",
        binding["actor_agent_id"],
        1,
        clock.now(),
    )
    assert store.persistent_supervision_required(binding["actor_agent_id"])
    before = store.connection.execute("SELECT COUNT(*) FROM watcher_scopes").fetchone()[0]
    store.close()
    env = environment(state, root)
    payload = {
        "session_id": f"session:{GAREN_RUNTIME}", "turn_id": "turn:missing-monitor",
        "hook_event_name": "Stop", "stop_hook_active": True,
    }
    result = run_json(
        [str(ROOT / "bin" / "agent-watcher"), "codex-stop-hook"], env, payload
    )
    assert result["decision"] == "block"
    assert result["reason"].startswith("supervisor_unavailable:")
    assert "no handoff was claimed" in result["reason"]
    with SQLiteStorage(state) as observer:
        after = observer.connection.execute("SELECT COUNT(*) FROM watcher_scopes").fetchone()[0]
    assert after == before


def test_restart_reconcile_real_supervisor_and_exactly_once_delivery(
    root: Path,
) -> None:
    """Compose restored identity, metadata, watcher health, and delivery."""

    state = canonical_state(root)
    with SQLiteStorage(state) as store:
        _add_standalone_cursor_champion(store, root)
        presentations = canonical_presentations(store)
    herdr = RestoredHerdr(presentations)
    herdr.unavailable_reads = 0
    original_processes = json.loads(json.dumps(herdr.processes))
    delivery = FakeDeliveryAdapter()
    runtime = PersistentSupervisor(
        state,
        callsign="Ashe",
        lease_seconds=2.0,
        renew_seconds=0.5,
        recovery_seconds=30,
        delivery_adapter=delivery,
        runtime_observer=LiveRuntimeObserver(),
    )
    thread, errors = start(runtime)
    try:
        with SQLiteStorage(state) as store:
            reconciled = reconcile_restored_agents(
                store,
                multiplexer_kind="herdr",
                at=RESTART_AT,
                timeout_ms=0,
                herdr_runner=herdr,
            )
            assert reconciled["created_processes"] == 0
            assert reconciled["resumed_sessions"] == 0
            assert reconciled["prompted_sessions"] == 0
            assert reconciled["closed_processes"] == 0
            vayne = store.agent_status("agent:vayne")
        status = supervisor_status(state, "Ashe")
        assert status["live"] is True and status["monitor_live"] is True
        assert herdr.processes == original_processes
        by_name = {item["name"]: item for item in herdr.agents}
        for callsign, provider, role in (
            ("ashe", "codex", "shotcaller"),
            ("ambessa", "cursor", "champion"),
            ("heimerdinger", "codex", "champion"),
            ("kaisa", "cursor", "champion"),
            ("vayne", "cursor", "champion"),
        ):
            agent = by_name[callsign]
            assert agent["display_agent"] == provider
            assert agent["tokens"]["orchestrator_role"] == role
            assert agent["terminal_title"]

        env = environment(state, root)
        command = [
            str(ROOT / "bin" / "league"),
            "--state-root",
            str(state),
            "agent",
            "transition",
            "--agent-id",
            "agent:vayne",
            "--expected-version",
            str(vayne["version"]),
            "--status",
            "progress",
            "--update",
            "Synthetic restored Cursor Champion transition.",
            "--at",
            RESTART_AT,
        ]
        first = run_json(command, env)["result"]
        assert first["delivery"]["state"] == "scheduled"
        deadline = time.monotonic() + 3
        while len(delivery.sent) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(delivery.sent) == 1
        with SQLiteStorage(state) as store:
            duplicate = handoff_transition_delivery(
                store,
                outbox_id=first["outbox_id"],
                event_id=first["event_id"],
                recipient_agent_id="agent:ashe",
                at=RESTART_AT,
            )
        assert duplicate["state"] == "scheduled"
        time.sleep(0.1)
        assert len(delivery.sent) == 1
        with SQLiteStorage(state) as store:
            outbox = store.connection.execute(
                "SELECT state,attempt_count FROM delivery_outbox WHERE outbox_id=?",
                (first["outbox_id"],),
            ).fetchone()
            receipts = store.connection.execute(
                "SELECT COUNT(*) FROM recipient_receipts WHERE event_id=? AND recipient_agent_id='agent:ashe'",
                (first["event_id"],),
            ).fetchone()[0]
        assert tuple(outbox) == ("delivered", 1)
        assert receipts == 1
    finally:
        stop_supervisor(state, "Ashe")
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-supervisor-delivery-") as temporary:
        root = Path(temporary)
        test_transition_commits_then_service_delivers_once(root / "delivery")
        test_stop_exposes_missing_supervisor_without_handoff(root / "stop")
        test_restart_reconcile_real_supervisor_and_exactly_once_delivery(
            root / "restart"
        )
    print("PASS: persistent service is live, Stop fails visibly, and transition outbox delivers exactly once")


if __name__ == "__main__":
    main()
