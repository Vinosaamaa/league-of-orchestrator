#!/usr/bin/env python3
"""Focused multi-Squad persistent supervision and wake-isolation acceptance."""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.persistent_supervisor import (  # noqa: E402
    PersistentSupervisor,
    SupervisorUnavailable,
    notify_user_message,
    pause_supervisor,
    resume_supervisor,
    send_supervisor_message,
    supervisor_status,
)
from league.canonical_delivery import dispatch_event  # noqa: E402
from league.canonical_watcher import handle_brokered_hook  # noqa: E402
from league.sqlite_handoff_schema import SHOTCALLER_SEED, SHUFFLE_VERSION  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import RuntimeRegistrationCommand  # noqa: E402
from lifecycle_fakes import FakeDeliveryAdapter  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME_TWO,
    JARVAN_ID,
    JARVAN_RUNTIME,
    create_context,
)
from storage_fixture import SHOTCALLER_ID  # noqa: E402


AZIR_ID = "88888888-8888-4888-8888-888888888888"
AZIR_RUNTIME = "runtime:azir:one"


class FakeWakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def send(self, binding, envelope) -> None:
        self.calls.append((dict(binding), dict(envelope)))


class CountingRuntimeObserver:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], ...]] = []

    def observe(self, candidates):
        batch = tuple(dict(candidate) for candidate in candidates)
        self.calls.append(batch)
        return {
            str(candidate["assignment_id"]): {
                "state": "live",
                "fingerprint": "synthetic-live",
            }
            for candidate in candidates
        }


def _accept_squad(store, clock, *, suffix: str, actor_id: str, runtime_id: str) -> None:
    store.register_squad(
        registration_id=f"registration:{suffix}",
        squad_id=f"squad:{suffix}",
        requester_agent_id=SHOTCALLER_ID,
        shotcaller_agent_id=actor_id,
        runtime_instance_id=runtime_id,
        project_ids=(),
        capabilities=("request.route",),
        expires_at=clock.after(600),
        event_id=f"event:registration:{suffix}",
        outbox_id=f"outbox:registration:{suffix}",
        at=clock.now(),
    )
    store.accept_squad(
        registration_id=f"registration:{suffix}",
        shotcaller_agent_id=actor_id,
        runtime_instance_id=runtime_id,
        decision="accept",
        event_id=f"event:accept:{suffix}",
        outbox_id=f"outbox:accept:{suffix}",
        at=clock.now(),
    )


def _add_azir(store, clock) -> None:
    status = store.callsign_status("shotcaller")
    catalog = [
        {
            "callsign": entry["callsign"],
            "enabled": entry["enabled"],
            "capabilities": [],
        }
        for entry in status["entries"]
    ]
    catalog.append({"callsign": "Azir", "enabled": True, "capabilities": []})
    store.reconcile_callsign_pool(
        "shotcaller",
        status["queue_version"],
        SHOTCALLER_SEED,
        SHUFFLE_VERSION,
        catalog,
        clock.now(),
    )
    reserved = store.allocate_callsign(
        "callsign-assignment:azir",
        AZIR_ID,
        "shotcaller",
        "squad",
        "squad:Azir",
        (),
        clock.now(),
    )
    store.activate_callsign(
        "callsign-assignment:azir",
        reserved["version"],
        {
            "schema": "league.runtime-acceptance.v1",
            "verified": True,
            "assignment_id": "callsign-assignment:azir",
            "agent_id": AZIR_ID,
            "callsign": reserved["callsign"],
            "runtime_instance_id": AZIR_RUNTIME,
            "harness_kind": "codex-thread",
            "backend_kind": "herdr",
            "session_identity": "session:runtime:azir:one",
            "endpoint_identity": "synthetic:azir",
            "endpoint_generation": "generation:azir:one",
            "routing_name": reserved["callsign"].lower(),
            "display_agent": "Codex",
            "capabilities": [],
        },
        clock.now(),
    )
    _accept_squad(
        store,
        clock,
        suffix="Azir",
        actor_id=AZIR_ID,
        runtime_id=AZIR_RUNTIME,
    )


def _multisquad_state(parent: Path, name: str):
    state, store, clock = create_context(parent, name)
    store.register_runtime(
        RuntimeRegistrationCommand(
            runtime_instance_id=GAREN_RUNTIME_TWO,
            actor_agent_id=SHOTCALLER_ID,
            harness_kind="codex-thread",
            backend_kind="herdr",
            session_ref=f"session:{GAREN_RUNTIME_TWO}",
            endpoint="synthetic:garen:two",
            runtime_generation="generation:garen:two",
            status="closed",
            verified=False,
            at=clock.now(),
        )
    )
    _accept_squad(
        store,
        clock,
        suffix="Jarvan",
        actor_id=JARVAN_ID,
        runtime_id=JARVAN_RUNTIME,
    )
    _add_azir(store, clock)
    return state, store


def _start(runtime: PersistentSupervisor):
    errors: list[BaseException] = []

    def run() -> None:
        try:
            runtime.run(emit_ready=False)
        except BaseException as exc:  # pragma: no cover - surfaced in caller
            errors.append(exc)

    thread = threading.Thread(target=run, name="synthetic-multisquad-supervisor")
    thread.start()
    assert runtime.ready.wait(timeout=5), errors
    return thread, errors


def _finish_startup_recovery(store, delivery: FakeDeliveryAdapter) -> None:
    deadline = time.monotonic() + 2
    while (
        store.pending_backlog(
            "2026-12-31T23:59:59Z", limit=100, per_recipient=20
        )
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert store.pending_backlog(
        "2026-12-31T23:59:59Z", limit=100, per_recipient=20
    ) == []
    delivery.sent.clear()


def test_one_service_registers_three_isolated_shotcallers(root: Path) -> None:
    state, store = _multisquad_state(root, "registration")
    store.close()
    runtime = PersistentSupervisor(
        state,
        lease_seconds=0.8,
        renew_seconds=0.2,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=FakeDeliveryAdapter(),
    )
    thread, errors = _start(runtime)
    try:
        statuses = {
            callsign: supervisor_status(state, callsign)
            for callsign in ("Garen", "Jarvan", "Azir")
        }
        service = supervisor_status(state)
        assert service["schema"] == "league.supervisor-service-status.v1"
        assert service["live"] and service["binding_count"] == 3
        assert [binding["callsign"] for binding in service["bindings"]] == [
            "Azir",
            "Garen",
            "Jarvan",
        ]
        assert all(status["live"] for status in statuses.values()), statuses
        assert {status["callsign"] for status in statuses.values()} == {
            "Garen", "Jarvan", "Azir"
        }
        with runtime.store_factory(state) as observer:
            registrations = [
                observer.watcher_registration(actor_id)
                for actor_id in (SHOTCALLER_ID, JARVAN_ID, AZIR_ID)
            ]
        assert all(registration is not None for registration in registrations)
        assert len({registration["watcher_id"] for registration in registrations}) == 3
        assert {registration["wake_locator"] for registration in registrations} == {
            f"unix:{runtime.socket_path}"
        }
    finally:
        if runtime.ready.is_set():
            send_supervisor_message(f"unix:{runtime.socket_path}", {"kind": "stop"})
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def test_global_stop_hook_resolves_session_and_blocks_only_owning_shotcaller(root: Path) -> None:
    """A global Codex Stop hook must not bleed Azir obligations into Ashe."""
    _, store = _multisquad_state(root, "global-stop-session-routing")
    store.intake_prompt(
        "prompt:azir:untriaged",
        AZIR_ID,
        AZIR_RUNTIME,
        "codex",
        "session:runtime:azir:one",
        "source:azir:untriaged",
        "Azir's real owner steer remains untriaged.",
        "2026-01-01T00:00:00+00:00",
    )
    ashe = handle_brokered_hook(
        store,
        {
            "command": "codex-stop-hook",
            "payload": {
                "session_id": "session:runtime:garen:one",
                "turn_id": "turn:ashe-stop",
                "hook_event_name": "Stop",
                "stop_hook_active": False,
            },
        },
    )
    azir = handle_brokered_hook(
        store,
        {
            "command": "codex-stop-hook",
            "payload": {
                "session_id": "session:runtime:azir:one",
                "turn_id": "turn:azir-stop",
                "hook_event_name": "Stop",
                "stop_hook_active": False,
            },
        },
    )
    assert ashe["actor_agent_id"] == SHOTCALLER_ID
    # The fixture may retain Garen's own seeded obligations, but Azir's
    # untriaged prompt must never appear in Ashe's decision or wake target.
    assert "Azir" not in str(ashe["hook_output"])
    assert azir["actor_agent_id"] == AZIR_ID
    assert azir["hook_output"]["decision"] == "block"
    assert "Azir" in azir["hook_output"]["reason"]
    assert azir["supervision_handoff"] is False


def test_service_manager_starts_one_multiplex_runtime() -> None:
    launchd = (ROOT / "config/league-supervisor.launchd.plist.in").read_text(
        encoding="utf-8"
    )
    assert launchd.count("<string>service-run</string>") == 1
    assert "--shotcaller" not in launchd and "@@SHOTCALLER@@" not in launchd
    assert "<key>RunAtLoad</key>\n  <true/>" in launchd
    assert "<key>KeepAlive</key>" in launchd
    assert "<key>SuccessfulExit</key>\n    <false/>" in launchd
    assert "<key>ThrottleInterval</key>\n  <integer>5</integer>" in launchd


def test_user_priority_generation_is_isolated_per_shotcaller(root: Path) -> None:
    state, store = _multisquad_state(root, "user-priority")
    runtime = PersistentSupervisor(
        state,
        lease_seconds=0.8,
        renew_seconds=0.2,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=FakeDeliveryAdapter(),
    )
    thread, errors = _start(runtime)
    try:
        barrier = threading.Barrier(3)
        notify_errors: list[BaseException] = []

        def notify(actor_id: str, prompt_id: str) -> None:
            try:
                barrier.wait(timeout=2)
                with runtime.store_factory(state) as writer:
                    assert notify_user_message(writer, actor_id, prompt_id)
            except BaseException as exc:  # pragma: no cover - surfaced below
                notify_errors.append(exc)

        workers = [
            threading.Thread(target=notify, args=(actor_id, prompt_id))
            for actor_id, prompt_id in (
                (SHOTCALLER_ID, "prompt:garen"),
                (JARVAN_ID, "prompt:jarvan"),
                (AZIR_ID, "prompt:azir"),
            )
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        assert all(not worker.is_alive() for worker in workers) and not notify_errors
        statuses = {
            callsign: supervisor_status(state, callsign)
            for callsign in ("Garen", "Jarvan", "Azir")
        }
        assert {
            callsign: status["user_priority_generation"]
            for callsign, status in statuses.items()
        } == {"Garen": 1, "Jarvan": 1, "Azir": 1}
    finally:
        store.close()
        send_supervisor_message(f"unix:{runtime.socket_path}", {"kind": "stop"})
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def test_attention_uses_exact_direct_fallback_without_service(root: Path) -> None:
    _, store = _multisquad_state(root, "direct-fallback")
    attention = store.record_supervision_fault(
        JARVAN_ID,
        "runtime_reconciliation_refused",
        "synthetic-direct-fallback",
        "2026-01-01T00:01:00Z",
    )
    delivery = FakeDeliveryAdapter()
    receipt = dispatch_event(
        store,
        outbox_id=attention["outbox_id"],
        event_id=attention["event_id"],
        recipient_agent_id=JARVAN_ID,
        at="2026-01-01T00:01:01Z",
        adapter=delivery,
    )
    assert receipt["state"] == "delivered"
    assert len(delivery.sent) == 1
    assert delivery.sent[0].channel == "direct"
    assert delivery.sent[0].target["runtime_instance_id"] == JARVAN_RUNTIME
    assert delivery.sent[0].envelope["recipient_agent_id"] == JARVAN_ID
    store.close()


def test_restart_recovers_one_lost_notification_for_exact_squad(root: Path) -> None:
    state, store = _multisquad_state(root, "restart-recovery")
    first_delivery = FakeDeliveryAdapter()
    first = PersistentSupervisor(
        state,
        lease_seconds=0.8,
        renew_seconds=0.2,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=first_delivery,
    )
    first_thread, first_errors = _start(first)
    _finish_startup_recovery(store, first_delivery)
    first_fences = {
        actor_id: store.watcher_registration(actor_id)["fence"]
        for actor_id in (SHOTCALLER_ID, JARVAN_ID, AZIR_ID)
    }
    send_supervisor_message(f"unix:{first.socket_path}", {"kind": "stop"})
    first_thread.join(timeout=5)
    assert not first_thread.is_alive() and not first_errors

    lost = store.record_supervision_fault(
        AZIR_ID,
        "runtime_reconciliation_refused",
        "synthetic-lost-notification",
        "2026-01-01T00:04:00Z",
    )
    restarted_delivery = FakeDeliveryAdapter()
    restarted = PersistentSupervisor(
        state,
        lease_seconds=0.8,
        renew_seconds=0.2,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=restarted_delivery,
    )
    restarted_thread, restarted_errors = _start(restarted)
    try:
        deadline = time.monotonic() + 2
        while not restarted_delivery.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [item.envelope["event_id"] for item in restarted_delivery.sent] == [
            lost["event_id"]
        ]
        second_fences = {
            actor_id: store.watcher_registration(actor_id)["fence"]
            for actor_id in (SHOTCALLER_ID, JARVAN_ID, AZIR_ID)
        }
        assert all(second_fences[actor] > first_fences[actor] for actor in first_fences)
    finally:
        store.close()
        send_supervisor_message(f"unix:{restarted.socket_path}", {"kind": "stop"})
        restarted_thread.join(timeout=5)
    assert not restarted_thread.is_alive() and not restarted_errors


def test_brokered_prompt_resolves_and_wakes_only_its_shotcaller(root: Path) -> None:
    state, store = _multisquad_state(root, "brokered-prompt")
    runtime = PersistentSupervisor(
        state,
        lease_seconds=0.8,
        renew_seconds=0.2,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=FakeDeliveryAdapter(),
    )
    thread, errors = _start(runtime)
    try:
        result = send_supervisor_message(
            f"unix:{runtime.socket_path}",
            {
                "kind": "hook",
                "hook": {
                    "command": "codex-user-prompt-hook",
                    "shotcaller": "Jarvan",
                    "session_id": None,
                    "capture_event_id": "codex-user-prompt:" + "c" * 32,
                    "payload": {
                        "session_id": f"session:{JARVAN_RUNTIME}",
                        "turn_id": "turn:jarvan-owner-prompt",
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Synthetic Jarvan-equivalent owner prompt",
                    },
                },
            },
        )
        assert result["priority"] == "user"
        intake = store.untriaged_intake(JARVAN_ID)
        assert [prompt["body"] for prompt in intake["prompts"]] == [
            "Synthetic Jarvan-equivalent owner prompt"
        ]
        statuses = {
            callsign: supervisor_status(state, callsign)
            for callsign in ("Garen", "Jarvan", "Azir")
        }
        assert statuses["Jarvan"]["user_priority_generation"] == 1
        assert statuses["Garen"]["user_priority_generation"] == 0
        assert statuses["Azir"]["user_priority_generation"] == 0
    finally:
        store.close()
        send_supervisor_message(f"unix:{runtime.socket_path}", {"kind": "stop"})
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def test_calm_pause_and_delivery_targets_are_isolated_per_squad(root: Path) -> None:
    state, store = _multisquad_state(root, "calm-isolation")
    startup_delivery = FakeDeliveryAdapter()
    runtime = PersistentSupervisor(
        state,
        lease_seconds=0.8,
        renew_seconds=0.2,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=startup_delivery,
    )
    thread, errors = _start(runtime)
    try:
        _finish_startup_recovery(store, startup_delivery)
        jarvan = store.supervisor_binding("Jarvan")
        store.configure_supervision_policy(
            jarvan["scope_id"], JARVAN_ID, "calm", 60, "2026-01-01T00:01:00Z"
        )
        paused = pause_supervisor(state, "Jarvan")
        assert paused["runtime_state"] == "paused"
        assert supervisor_status(state, "Garen")["runtime_state"] == "supervising"
        assert supervisor_status(state, "Azir")["runtime_state"] == "supervising"

        targets = FakeDeliveryAdapter()
        jarvan_fault = store.record_supervision_fault(
            JARVAN_ID,
            "runtime_reconciliation_refused",
            "synthetic-jarvan-paused",
            "2026-01-01T00:01:01Z",
        )
        garen_fault = store.record_supervision_fault(
            SHOTCALLER_ID,
            "runtime_reconciliation_refused",
            "synthetic-garen-supervising",
            "2026-01-01T00:01:02Z",
        )
        for fault, recipient, at in (
            (jarvan_fault, JARVAN_ID, "2026-01-01T00:01:03Z"),
            (garen_fault, SHOTCALLER_ID, "2026-01-01T00:01:04Z"),
        ):
            dispatch_event(
                store,
                outbox_id=fault["outbox_id"],
                event_id=fault["event_id"],
                recipient_agent_id=recipient,
                at=at,
                adapter=targets,
            )
        assert [item.channel for item in targets.sent] == ["direct", "watcher"]
        assert targets.sent[0].target["runtime_instance_id"] == JARVAN_RUNTIME
        assert targets.sent[1].envelope["recipient_agent_id"] == SHOTCALLER_ID
        resumed = resume_supervisor(state, "Jarvan")
        assert resumed["runtime_state"] == "supervising"
    finally:
        store.close()
        send_supervisor_message(f"unix:{runtime.socket_path}", {"kind": "stop"})
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def test_active_turn_persists_attention_without_duplicate_wake(root: Path) -> None:
    state, store = _multisquad_state(root, "active-turn")
    wake = FakeWakeAdapter()
    delivery = FakeDeliveryAdapter()
    runtime = PersistentSupervisor(
        state,
        lease_seconds=0.8,
        renew_seconds=0.2,
        wake_adapter=wake,
        delivery_adapter=delivery,
    )
    thread, errors = _start(runtime)
    try:
        _finish_startup_recovery(store, delivery)
        store.begin_shotcaller_turn(
            SHOTCALLER_ID, "turn-token:garen", "2026-01-01T00:02:00Z"
        )
        fault = store.record_supervision_fault(
            SHOTCALLER_ID,
            "runtime_reconciliation_refused",
            "synthetic-active-turn",
            "2026-01-01T00:02:01Z",
        )
        receipt = dispatch_event(
            store,
            outbox_id=fault["outbox_id"],
            event_id=fault["event_id"],
            recipient_agent_id=SHOTCALLER_ID,
            at="2026-01-01T00:02:02Z",
            adapter=delivery,
        )
        assert receipt["state"] == "pending"
        assert receipt["reason"] == "owner_turn_active"
        assert delivery.sent == [] and wake.calls == []
    finally:
        store.close()
        send_supervisor_message(f"unix:{runtime.socket_path}", {"kind": "stop"})
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def test_turn_reuses_existing_shotcaller_scope(root: Path) -> None:
    _, store = _multisquad_state(root, "existing-scope")
    store.stop_decision(
        "Garen-existing-scope",
        SHOTCALLER_ID,
        "terminal:existing-scope",
        "2026-01-01T00:02:00Z",
    )
    begun = store.begin_shotcaller_turn(
        SHOTCALLER_ID, "turn-token:existing-scope", "2026-01-01T00:02:01Z"
    )
    assert begun["scope_id"] == "Garen-existing-scope"
    store.abort_shotcaller_turn(
        SHOTCALLER_ID, "turn-token:existing-scope", "2026-01-01T00:02:02Z"
    )
    store.close()


def test_priority_publish_refuses_removed_binding_without_global_signal(
    root: Path,
) -> None:
    state, store = _multisquad_state(root, "priority-renewal-race")
    store.close()
    runtime = PersistentSupervisor(state, wake_adapter=FakeWakeAdapter())
    runtime._bindings = {
        SHOTCALLER_ID: {
            "actor_agent_id": SHOTCALLER_ID,
            "user_priority_generation": 0,
        }
    }
    started = threading.Event()
    errors: list[BaseException] = []

    def publish() -> None:
        started.set()
        try:
            runtime._publish_user_priority(SHOTCALLER_ID)
        except BaseException as exc:
            errors.append(exc)

    with runtime._fence_lock:
        worker = threading.Thread(target=publish)
        worker.start()
        assert started.wait(timeout=2)
        runtime._bindings.pop(SHOTCALLER_ID)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], SupervisorUnavailable)
    assert runtime.user_priority_generation == 0
    assert not runtime.user_priority.is_set()


def test_discovery_observation_and_aggregate_status_are_batched(root: Path) -> None:
    state, store = _multisquad_state(root, "batched-service")
    assert len(store.supervisor_bindings()) == 3
    store.close()

    observer = CountingRuntimeObserver()
    runtime = PersistentSupervisor(
        state,
        lease_seconds=0.8,
        renew_seconds=0.2,
        recovery_seconds=10,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=FakeDeliveryAdapter(),
        runtime_observer=observer,
    )
    thread, errors = _start(runtime)
    try:
        deadline = time.monotonic() + 2
        while not observer.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(observer.calls) == 1
        real_send = send_supervisor_message
        status_messages: list[dict[str, object]] = []

        def counted_send(locator, message, **kwargs):
            status_messages.append(dict(message))
            return real_send(locator, message, **kwargs)

        with patch(
            "league.persistent_supervisor.send_supervisor_message",
            side_effect=counted_send,
        ):
            status = supervisor_status(state)
        assert status["live"] and status["binding_count"] == 3
        assert status_messages == [{"kind": "service-ping"}]
    finally:
        send_supervisor_message(f"unix:{runtime.socket_path}", {"kind": "stop"})
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def test_committed_turn_stop_hands_pending_attention_to_supervisor(root: Path) -> None:
    state, store = _multisquad_state(root, "stop-handoff")
    delivery = FakeDeliveryAdapter()
    runtime = PersistentSupervisor(
        state,
        lease_seconds=0.8,
        renew_seconds=0.2,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=delivery,
    )
    thread, errors = _start(runtime)
    try:
        _finish_startup_recovery(store, delivery)
        store.begin_shotcaller_turn(
            SHOTCALLER_ID, "turn-token:handoff", "2026-01-01T00:03:00Z"
        )
        fault = store.record_supervision_fault(
            SHOTCALLER_ID,
            "runtime_reconciliation_refused",
            "synthetic-stop-handoff",
            "2026-01-01T00:03:01Z",
        )
        deferred = dispatch_event(
            store,
            outbox_id=fault["outbox_id"],
            event_id=fault["event_id"],
            recipient_agent_id=SHOTCALLER_ID,
            at="2026-01-01T00:03:02Z",
            adapter=delivery,
        )
        assert deferred["reason"] == "owner_turn_active"
        store.commit_shotcaller_turn(
            SHOTCALLER_ID, "turn-token:handoff", "2026-01-01T00:03:03Z"
        )
        stopped = send_supervisor_message(
            f"unix:{runtime.socket_path}",
            {
                "kind": "hook",
                "hook": {
                    "command": "codex-stop-hook",
                    "shotcaller": "Garen",
                    "session_id": None,
                    "capture_event_id": None,
                    "payload": {
                        "session_id": "session:runtime:garen:one",
                        "turn_id": "turn:garen-stop-handoff",
                        "hook_event_name": "Stop",
                        "stop_hook_active": True,
                    },
                },
            },
        )
        assert stopped["hook_output"] == {}
        deadline = time.monotonic() + 2
        while not delivery.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        delivered_event_ids = [item.envelope["event_id"] for item in delivery.sent]
        assert delivered_event_ids == [fault["event_id"]], delivered_event_ids
    finally:
        store.close()
        send_supervisor_message(f"unix:{runtime.socket_path}", {"kind": "stop"})
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-multisquad-supervisor-") as temporary:
        test_service_manager_starts_one_multiplex_runtime()
        test_one_service_registers_three_isolated_shotcallers(Path(temporary))
        test_user_priority_generation_is_isolated_per_shotcaller(Path(temporary))
        test_attention_uses_exact_direct_fallback_without_service(Path(temporary))
        test_restart_recovers_one_lost_notification_for_exact_squad(Path(temporary))
        test_brokered_prompt_resolves_and_wakes_only_its_shotcaller(Path(temporary))
        test_calm_pause_and_delivery_targets_are_isolated_per_squad(Path(temporary))
        test_active_turn_persists_attention_without_duplicate_wake(Path(temporary))
        test_turn_reuses_existing_shotcaller_scope(Path(temporary))
        test_priority_publish_refuses_removed_binding_without_global_signal(
            Path(temporary)
        )
        test_discovery_observation_and_aggregate_status_are_batched(Path(temporary))
        test_committed_turn_stop_hands_pending_attention_to_supervisor(Path(temporary))
    print("PASS: one persistent service multiplexes isolated Shotcaller bindings")


if __name__ == "__main__":
    main()
