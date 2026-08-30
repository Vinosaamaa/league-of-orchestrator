#!/usr/bin/env python3
"""Focused Calm filtering, pause/resume, and fenced wake acceptance."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import threading
import time
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.canonical_delivery import (  # noqa: E402
    InstalledDeliveryAdapter,
    dispatch_event,
)
from league.persistent_supervisor import (  # noqa: E402
    DEFAULT_LEASE_SECONDS,
    DEFAULT_RECOVERY_SECONDS,
    DEFAULT_RENEW_SECONDS,
    PersistentSupervisor,
    notify_user_message,
    pause_supervisor,
    resume_supervisor,
    send_supervisor_message,
    stop_supervisor,
    supervisor_status,
)
from league.request_services import AssignmentService, AssignmentSpec, DeliveryUnavailable  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.sqlite_watcher_ops import (  # noqa: E402
    ATTENTION_STATUSES,
    DEFAULT_UNREACHABLE_GRACE_SECONDS,
    ROUTINE_STATUSES,
    _attention_reason,
)
from league.storage import RuntimeRegistrationCommand, StorageRefusal  # noqa: E402
from league.storage_outbox import OutboxDispatchIdentity  # noqa: E402
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
from storage_fixture import CHAMPION_ID, REPOSITORY, SHOTCALLER_ID  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402


class FakeWakeAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.changed = threading.Condition()

    def send(self, binding, envelope) -> None:
        del binding
        with self.changed:
            self.calls.append(dict(envelope))
            self.changed.notify_all()

    def wait_for(self, event_id: str, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.changed:
            while not any(call.get("event_id") == event_id for call in self.calls):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.changed.wait(remaining)
        return True


class FakeRuntimeObserver:
    def __init__(self, state: str = "live") -> None:
        self.state = state
        self.calls = 0

    def observe(self, candidates):
        self.calls += 1
        return {
            str(candidate["assignment_id"]): {
                "state": self.state,
                "fingerprint": f"synthetic:{self.state}",
            }
            for candidate in candidates
        }


class PolicyStore:
    def __init__(self) -> None:
        self.connection = self

    def execute(self, statement, parameters=()):
        del parameters
        self.statement = statement
        return self

    def fetchone(self):
        if "active_count" in self.statement:
            return {"active_count": 0}
        if "cleanup_obligations" in self.statement:
            return None
        raise AssertionError(f"unexpected policy query: {self.statement}")


def _at() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _future(seconds: float) -> str:
    return (
        datetime.now().astimezone() + timedelta(seconds=seconds)
    ).isoformat(timespec="microseconds")


def _start(runtime: PersistentSupervisor):
    errors: list[BaseException] = []

    def run() -> None:
        try:
            runtime.run(emit_ready=False)
        except BaseException as exc:  # pragma: no cover - surfaced by caller
            errors.append(exc)

    thread = threading.Thread(target=run, name="synthetic-calm-supervisor")
    thread.start()
    assert runtime.ready.wait(timeout=5), errors
    return thread, errors


def _close_secondary_runtime(store: SQLiteStorage) -> None:
    store.register_runtime(
        RuntimeRegistrationCommand(
            GAREN_RUNTIME_TWO,
            SHOTCALLER_ID,
            "codex-thread",
            "herdr",
            f"session:{GAREN_RUNTIME_TWO}",
            "synthetic:garen:two",
            "generation:garen:two",
            "closed",
            False,
            _at(),
        )
    )


def _active_champion(
    parent: Path, name: str = "calm-active"
) -> tuple[Path, SQLiteStorage, dict[str, object]]:
    state, store, clock = create_context(parent, name)
    _close_secondary_runtime(store)
    capture_p100(store, clock)
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    dispatch_request(
        store, clock, "R3", "claim-r3", "dispatch-r3", "repository-write", "champion"
    )
    assignment = AssignmentSpec(
        assignment_id="assignment:calm",
        request_id="R3",
        claim_token="claim-r3",
        task_id="task:calm",
        task_summary="Synthetic Calm supervision task",
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID,
        callsign="Lux",
        repository=REPOSITORY,
        issue=66,
        branch="agent/synthetic/calm",
        worktree="/synthetic/worktrees/calm",
        issue_receipt=None,
    )
    active = AssignmentService(store, FakeLaunchAdapter(), clock, FakeIds()).assign(
        issue_bound_spec(store, assignment, clock.now())
    )
    store.register_runtime(
        RuntimeRegistrationCommand(
            str(active["runtime_instance_id"]),
            LUX_ID,
            "codex-thread",
            "herdr",
            f"thread:{LUX_ID}",
            "synthetic:lux",
            f"generation:{LUX_ID}",
            "active",
            True,
            _at(),
        )
    )
    binding = store.supervisor_binding("Garen")
    store.configure_supervision_policy(
        str(binding["scope_id"]), SHOTCALLER_ID, "calm", 5, _at()
    )
    return state, store, active


def _transition(
    store: SQLiteStorage,
    active: dict[str, object],
    version: int,
    state: str,
    suffix: str,
    *,
    attention_required: bool = False,
) -> dict[str, object]:
    return store.transition_task(
        str(active["task_id"]),
        str(active["runtime_instance_id"]),
        version,
        state,
        f"Synthetic {suffix} transition",
        "Continue bounded synthetic verification",
        "Synthetic owner decision required" if state == "blocked" else None,
        f"transition:{suffix}",
        f"transition-key:{suffix}",
        f"event:{suffix}",
        f"outbox:{suffix}",
        SHOTCALLER_ID,
        _at(),
        attention_required,
    )


def test_final_policy_and_timer_matrix() -> None:
    store = PolicyStore()

    def row(status: str, event_type: str = "task_transition", **detail):
        return {
            "detail_json": __import__("json").dumps(detail),
            "event_type": event_type,
            "status": status,
            "source_role": "champion",
            "task_id": "task:matrix",
            "recipient_agent_id": SHOTCALLER_ID,
        }

    for status in ROUTINE_STATUSES:
        assert _attention_reason(store, row(status)) is None, status
    assert _attention_reason(store, row("progress", "lease_renewed")) is None
    assert _attention_reason(store, row("delivered", "delivery_acknowledgement")) is None
    for status in ATTENTION_STATUSES:
        assert _attention_reason(store, row(status)) is not None, status
    for event_type in (
        "cleanup_refusal",
        "runtime_reconciliation_refusal",
        "delivery_refusal",
    ):
        assert _attention_reason(store, row("failed", event_type)) is not None
    assert _attention_reason(store, row("progress", attention_required=True)) is not None
    assert _attention_reason(store, row("completed")) == "tracked_lane_idle"

    assert (
        DEFAULT_RENEW_SECONDS,
        DEFAULT_LEASE_SECONDS,
        DEFAULT_UNREACHABLE_GRACE_SECONDS,
        DEFAULT_RECOVERY_SECONDS,
    ) == (20, 60, 60, 300)
    launchd = (ROOT / "config/league-supervisor.launchd.plist.in").read_text(
        encoding="utf-8"
    )
    assert "<key>ThrottleInterval</key>\n  <integer>5</integer>" in launchd
    assert "StartInterval" not in launchd


def test_in_flight_attention_delivery_is_not_dispatched_twice(root: Path) -> None:
    _, store, active = _active_champion(root, "calm-in-flight")
    attention = _transition(store, active, 3, "ready_to_land", "in-flight")
    identity = OutboxDispatchIdentity(
        str(attention["outbox_id"]),
        str(attention["event_id"]),
        SHOTCALLER_ID,
        "dispatcher:synthetic",
        "attempt:synthetic",
    )
    store.claim_outbox(identity, _future(60), _at())
    adapter = FakeDeliveryAdapter()
    receipt = dispatch_event(
        store,
        outbox_id=identity.outbox_id,
        event_id=identity.event_id,
        recipient_agent_id=identity.recipient_agent_id,
        at=_at(),
        adapter=adapter,
    )
    assert receipt["state"] == "in_flight"
    assert receipt["reason"] == "delivery_in_flight" and receipt["idempotent"]
    assert adapter.sent == []
    store.close()


def test_registration_and_silent_reconciliation_are_atomic(root: Path) -> None:
    _, store, _ = _active_champion(root, "calm-registration-atomic")
    try:
        binding = store.supervisor_binding("Garen")
        store.connection.execute(
            "UPDATE watcher_scopes SET metadata_json=? WHERE scope_id=?",
            ('{"supervision":"malformed"}', binding["scope_id"]),
        )
        try:
            store.register_watcher(
                str(binding["scope_id"]),
                "watcher:atomic",
                SHOTCALLER_ID,
                GAREN_RUNTIME,
                "unix:/synthetic/atomic.sock",
                _future(60),
                1,
                _at(),
            )
        except StorageRefusal as exc:
            assert exc.code == "supervision_policy_invalid"
        else:
            raise AssertionError("malformed reconciliation committed a watcher registration")
        assert store.watcher_registration(SHOTCALLER_ID) is None
    finally:
        store.close()


def test_calm_event_ipc_pause_resume_and_recovery(root: Path) -> None:
    state, store, active = _active_champion(root)
    first_wakes = FakeWakeAdapter()
    runtime = PersistentSupervisor(
        state,
        callsign="Garen",
        lease_seconds=0.8,
        renew_seconds=0.2,
        recovery_seconds=0.3,
        wake_adapter=first_wakes,
        runtime_observer=FakeRuntimeObserver(),
    )
    thread, errors = _start(runtime)
    status = supervisor_status(state, "Garen")
    assert status["live"] and status["mode"] == "calm"

    blocked = _transition(store, active, 3, "blocked", "attention")
    started = time.monotonic()
    delivered = dispatch_event(
        store,
        outbox_id=str(blocked["outbox_id"]),
        event_id=str(blocked["event_id"]),
        recipient_agent_id=SHOTCALLER_ID,
        at=_at(),
    )
    attention_latency = time.monotonic() - started
    assert delivered["state"] == "delivered" and attention_latency < 2
    assert first_wakes.wait_for("event:attention")
    priority_generation = runtime.user_priority_generation
    assert notify_user_message(store, SHOTCALLER_ID, "owner-priority")
    assert runtime.user_priority_generation == priority_generation + 1

    paused = pause_supervisor(state, "Garen")
    assert paused["paused"] and paused["live"] and paused["monitor_live"]
    assert paused["hooks_changed"] is False
    assert paused["in_flight_count"] == 2
    assert thread.is_alive() and supervisor_status(state, "Garen")["live"]

    routine = _transition(store, active, 4, "working", "routine")
    silent = dispatch_event(
        store,
        outbox_id=str(routine["outbox_id"]),
        event_id=str(routine["event_id"]),
        recipient_agent_id=SHOTCALLER_ID,
        at=_at(),
    )
    assert silent["state"] == "suppressed" and silent["effect_kind"] == "calm_silent"
    assert not first_wakes.wait_for("event:routine", timeout=0.1)

    champion_block = store.champion_stop_decision(LUX_ID, "terminal:one", _at())
    assert champion_block["decision"] == "block"
    assert store.champion_stop_decision(LUX_ID, "terminal:one", _at())["decision"] == "allow"
    explicit = _transition(
        store, active, 5, "progress", "explicit", attention_required=True
    )
    assert store.champion_stop_decision(LUX_ID, "terminal:two", _at())["status"] == "fresh_transition"
    direct = FakeDeliveryAdapter()
    paused_explicit = dispatch_event(
        store,
        outbox_id=str(explicit["outbox_id"]),
        event_id=str(explicit["event_id"]),
        recipient_agent_id=SHOTCALLER_ID,
        at=_at(),
        adapter=direct,
    )
    assert paused_explicit["state"] == "delivered"
    assert len(direct.sent) == 1 and direct.sent[0].channel == "direct"
    repeated_explicit = dispatch_event(
        store,
        outbox_id=str(explicit["outbox_id"]),
        event_id=str(explicit["event_id"]),
        recipient_agent_id=SHOTCALLER_ID,
        at=_at(),
        adapter=direct,
    )
    assert repeated_explicit["state"] == "delivered" and repeated_explicit["idempotent"]
    assert len(direct.sent) == 1
    assert not first_wakes.wait_for("event:explicit", timeout=0.1)

    resumed = resume_supervisor(state, "Garen")
    assert resumed["live"] and resumed["runtime_state"] == "supervising"
    reconciliation = resumed["silent_reconciliation"]
    assert reconciliation["returned_count"] >= 1
    assert any(row["event_id"] == "event:routine" for row in reconciliation["updates"])

    target = store.delivery_target(SHOTCALLER_ID, _at())
    assert target is not None and target["channel"] == "watcher"
    deadline = time.monotonic() + 2
    while supervisor_status(state, "Garen")["fence"] == target["fence"]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    try:
        InstalledDeliveryAdapter().send(
            "watcher",
            target,
            {
                "outbox_id": "outbox:stale",
                "event_id": "event:stale",
                "recipient_agent_id": SHOTCALLER_ID,
                "status": "blocked",
                "summary": "Synthetic stale watcher wake",
            },
        )
    except DeliveryUnavailable:
        pass
    else:
        raise AssertionError("stale watcher fence consumed a wake")

    lost = _transition(store, active, 6, "ready_to_land", "lost-notification")
    assert first_wakes.wait_for(str(lost["event_id"]), timeout=2)
    deadline = time.monotonic() + 2
    delay = 0.01
    while True:
        recovered = store.connection.execute(
            "SELECT state FROM delivery_outbox WHERE outbox_id=?", (lost["outbox_id"],)
        ).fetchone()
        if recovered["state"] == "delivered":
            break
        assert time.monotonic() < deadline
        time.sleep(delay)
        delay = min(delay * 2, 0.1)
    assert recovered["state"] == "delivered"

    stop_supervisor(state, "Garen")
    thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    store.close()


def test_paused_stop_and_unreachable_are_bounded(root: Path) -> None:
    _, state, _ = seeded_state(root, "calm-paused-stop")
    with SQLiteStorage(state) as store:
        store.register_runtime(
            RuntimeRegistrationCommand(
                GAREN_RUNTIME,
                SHOTCALLER_ID,
                "codex-thread",
                "herdr",
                f"session:{GAREN_RUNTIME}",
                "synthetic:garen",
                "generation:garen",
                "active",
                True,
                _at(),
            )
        )
        binding = store.supervisor_binding("Garen")
        scope = str(binding["scope_id"])
        store.configure_supervision_policy(scope, SHOTCALLER_ID, "calm", 5, _at())
        lease = _future(60)
        registered_at = _at()
        store.register_watcher(
            scope,
            "watcher:paused-stop",
            SHOTCALLER_ID,
            GAREN_RUNTIME,
            "unix:/synthetic/calm-paused-stop.sock",
            lease,
            1,
            registered_at,
        )
        retry = store.register_watcher(
            scope,
            "watcher:paused-stop",
            SHOTCALLER_ID,
            GAREN_RUNTIME,
            "unix:/synthetic/calm-paused-stop.sock",
            lease,
            1,
            registered_at,
        )
        assert retry["idempotent"] and retry["mode"] == "calm"
        assert retry["runtime_state"] == "supervising"
        store.pause_calm_supervision(
            SHOTCALLER_ID, "watcher:paused-stop", 1, _at()
        )
        delegated_only = store.stop_decision(
            scope, SHOTCALLER_ID, "terminal:delegated", _at(), block_on_fresh_terminal=True
        )
        assert delegated_only["decision"] == "allow"
        assert delegated_only["supervision_state"] == "paused"

        first = store.champion_stop_decision(CHAMPION_ID, "terminal:champion", _at())
        second = store.champion_stop_decision(CHAMPION_ID, "terminal:champion", _at())
        assert first["decision"] == "block" and second["decision"] == "allow"

        store.intake_prompt(
            "prompt:paused",
            SHOTCALLER_ID,
            GAREN_RUNTIME,
            "codex",
            f"session:{GAREN_RUNTIME}",
            "source:paused",
            "Synthetic owner obligation while Calm is paused",
            _at(),
        )
        store.note_user_message(scope, SHOTCALLER_ID, _at())
        actionable = store.stop_decision(
            scope, SHOTCALLER_ID, "terminal:actionable", _at(), block_on_fresh_terminal=True
        )
        repeated = store.stop_decision(
            scope, SHOTCALLER_ID, "terminal:actionable", _at(), block_on_fresh_terminal=True
        )
        assert actionable["decision"] == "block" and repeated["decision"] == "allow"
        assert actionable["obligations"]["untriaged_prompts"] == 1

    liveness_state, liveness_store, _ = _active_champion(root, "calm-liveness")
    binding = liveness_store.supervisor_binding("Garen")
    liveness_store.configure_supervision_policy(
        str(binding["scope_id"]), SHOTCALLER_ID, "calm", 1, _at()
    )
    liveness_store.close()
    missing = FakeRuntimeObserver("missing")
    wake = FakeWakeAdapter()
    direct = FakeDeliveryAdapter()
    monitor = PersistentSupervisor(
        liveness_state,
        callsign="Garen",
        lease_seconds=3,
        renew_seconds=1,
        recovery_seconds=300,
        wake_adapter=wake,
        delivery_adapter=direct,
        runtime_observer=missing,
    )
    monitor_thread, monitor_errors = _start(monitor)
    paused = pause_supervisor(liveness_state, "Garen")
    assert paused["wake_policy"] == "calm_paused" and monitor_thread.is_alive()
    deadline = time.monotonic() + 2
    delay_seconds = 0.01
    while missing.calls < 2:
        assert time.monotonic() < deadline
        time.sleep(delay_seconds)
        delay_seconds = min(delay_seconds * 2, 0.1)
    with SQLiteStorage(liveness_state) as observer:
        current = observer.watcher_registration(SHOTCALLER_ID)
        current_binding = observer.supervisor_binding("Garen")
    assert current is not None
    send_supervisor_message(
        str(current["wake_locator"]),
        {
            "kind": "runtime-observation",
            "fence": int(current["fence"]),
            "runtime_generation": current_binding["runtime_generation"],
        },
    )
    deadline = time.monotonic() + 2
    delay_seconds = 0.01
    with SQLiteStorage(liveness_state) as observer:
        while True:
            assignment = observer.assignment_launch_context("assignment:calm")
            reconciled_rows = observer.connection.execute(
                """
                SELECT e.event_id,o.state,o.last_outcome
                  FROM events e JOIN delivery_outbox o ON o.event_id=e.event_id
                 WHERE e.event_type='assignment_runtime_reconciled'
                """
            ).fetchall()
            if (
                assignment["state"] == "cleanup_pending"
                and reconciled_rows
                and reconciled_rows[0]["state"] == "delivered"
            ):
                break
            assert time.monotonic() < deadline
            time.sleep(delay_seconds)
            delay_seconds = min(delay_seconds * 2, 0.1)
    assert len(reconciled_rows) == 1
    assert tuple(reconciled_rows[0][1:]) == ("delivered", "acknowledged"), tuple(
        reconciled_rows[0]
    )
    assert len(direct.sent) == 1
    assert direct.sent[0].channel == "direct"
    assert direct.sent[0].envelope["event_id"] == reconciled_rows[0]["event_id"]
    assert not wake.wait_for(reconciled_rows[0]["event_id"], timeout=0.1)
    summary = resume_supervisor(liveness_state, "Garen")["silent_reconciliation"]
    assert all(row["event_id"] != reconciled_rows[0]["event_id"] for row in summary["updates"])
    stop_supervisor(liveness_state, "Garen")
    monitor_thread.join(timeout=5)
    assert not monitor_thread.is_alive() and not monitor_errors


def test_runtime_loss_grace_cancels_on_recovery(root: Path) -> None:
    state, store, _ = _active_champion(root, "calm-grace-cancel")
    store.configure_supervision_policy("watcher:Garen", SHOTCALLER_ID, "calm", 1, _at())
    store.close()
    observer = FakeRuntimeObserver("missing")
    monitor = PersistentSupervisor(
        state,
        callsign="Garen",
        lease_seconds=3,
        renew_seconds=1,
        recovery_seconds=300,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=FakeDeliveryAdapter(),
        runtime_observer=observer,
    )
    thread, errors = _start(monitor)
    deadline = time.monotonic() + 2
    while observer.calls < 1:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    observer.state = "live"
    time.sleep(0.05)
    with SQLiteStorage(state) as current_store:
        registration = current_store.watcher_registration(SHOTCALLER_ID)
        binding = current_store.supervisor_binding("Garen")
    assert registration is not None
    send_supervisor_message(
        str(registration["wake_locator"]),
        {
            "kind": "runtime-observation",
            "fence": int(registration["fence"]),
            "runtime_generation": binding["runtime_generation"],
        },
    )
    deadline = time.monotonic() + 2
    while observer.calls < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    time.sleep(1.05)
    with SQLiteStorage(state) as current_store:
        assignment = current_store.assignment_launch_context("assignment:calm")
        unreachable = current_store.connection.execute(
            "SELECT COUNT(*) count FROM events WHERE event_type='assignment_runtime_reconciled'"
        ).fetchone()
    assert assignment["state"] == "active" and unreachable["count"] == 0
    stop_supervisor(state, "Garen")
    thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-calm-supervision-") as temporary:
        root = Path(temporary)
        test_final_policy_and_timer_matrix()
        test_in_flight_attention_delivery_is_not_dispatched_twice(root)
        test_registration_and_silent_reconciliation_are_atomic(root)
        test_calm_event_ipc_pause_resume_and_recovery(root)
        test_paused_stop_and_unreachable_are_bounded(root)
        test_runtime_loss_grace_cancels_on_recovery(root)
    print(
        "PASS: final Calm ON/OFF matrix, owner priority, immediate IPC/direct attention, "
        "60/300/20/60/5 timer contract, pause/resume, silent replay, stale fencing, "
        "lost-notification recovery, grace cancellation, Stop guards, and one unreachable event"
    )


if __name__ == "__main__":
    main()
