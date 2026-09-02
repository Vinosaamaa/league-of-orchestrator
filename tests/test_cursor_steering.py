#!/usr/bin/env python3
"""Provider-faithful installed Cursor direct-delivery steering regressions."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.canonical_delivery import dispatch_event  # noqa: E402
from league.cursor_steering import HerdrCursorSteeringAdapter  # noqa: E402
from league.storage import OutboxDispatchIdentity, RuntimeRegistrationCommand  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    JARVAN_ID,
    activate_jarvan_squad,
    capture_p100,
    create_context,
)


CURSOR_RUNTIME = "runtime:cursor:jarvan"
CURSOR_SESSION = "00000000-0000-4000-8000-000000000084"
CURSOR_PANE = "w1:p84"
CURSOR_GENERATION = "generation:cursor:jarvan:1"


class FakeCursorHerdr:
    def __init__(
        self,
        status: str,
        *,
        race_at_get: int | None = None,
        wrong_pane: bool = False,
        wrong_session: bool = False,
        interactive_ready: bool = True,
        ambiguous_process: bool = False,
        missing_process: bool = False,
        acknowledge: bool = True,
    ) -> None:
        self.status = status
        self.seq = 40
        self.revision = 7
        self.race_at_get = race_at_get
        self.wrong_pane = wrong_pane
        self.wrong_session = wrong_session
        self.interactive_ready = interactive_ready
        self.ambiguous_process = ambiguous_process
        self.missing_process = missing_process
        self.acknowledge = acknowledge
        self.agent_gets = 0
        self.commands: list[list[str]] = []
        self.effects: list[list[str]] = []

    @staticmethod
    def _completed(command: list[str], payload: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload) + "\n", stderr=""
        )

    def __call__(self, command, **_kwargs):
        command = list(command)
        self.commands.append(command)
        if "agent" in command and "get" in command:
            self.agent_gets += 1
            if self.race_at_get == self.agent_gets:
                self.status = "idle" if self.status == "working" else "working"
                self.seq += 1
            return self._completed(
                command,
                {
                    "result": {
                        "agent": {
                            "agent": "cursor",
                            "agent_session": {
                                "agent": "cursor",
                                "kind": "id",
                                "source": "herdr:cursor",
                                "value": (
                                    "00000000-0000-4000-8000-000000000999"
                                    if self.wrong_session
                                    else CURSOR_SESSION
                                ),
                            },
                            "agent_status": self.status,
                            "interactive_ready": self.interactive_ready,
                            "name": "jarvan",
                            "pane_id": "w1:p999" if self.wrong_pane else CURSOR_PANE,
                            "revision": self.revision,
                            "state_change_seq": self.seq,
                        }
                    }
                },
            )
        if "process-info" in command:
            processes = []
            if not self.missing_process:
                processes.append(
                    {
                        "argv": ["/usr/local/bin/cursor-agent"],
                        "argv0": "cursor-agent",
                        "name": "cursor-agent",
                        "pid": 8401,
                    }
                )
            if self.ambiguous_process:
                processes.append(
                    {
                        "argv": ["/opt/bin/cursor-agent"],
                        "argv0": "cursor-agent",
                        "name": "cursor-agent",
                        "pid": 8402,
                    }
                )
            return self._completed(
                command,
                {
                    "result": {
                        "process_info": {
                            "foreground_process_group_id": 8400,
                            "foreground_processes": processes,
                            "pane_id": CURSOR_PANE,
                            "shell_pid": 8399,
                        }
                    }
                },
            )
        if "prompt" in command:
            self.effects.append(command)
            if self.acknowledge:
                self.status = "working"
                self.seq += 1
            return self._completed(command, {"result": {"accepted": True}})
        if "send-text" in command:
            self.effects.append(command)
            return self._completed(command, {"result": {"accepted": True}})
        if "send-keys" in command:
            self.effects.append(command)
            if self.acknowledge:
                self.status = "working"
                self.seq += 1
            return self._completed(command, {"result": {"accepted": True}})
        raise AssertionError(f"unexpected Herdr command: {command}")


def routed_cursor_context(root: Path, label: str):
    _state, store, clock = create_context(root, label)
    capture_p100(store, clock)
    squad_id = activate_jarvan_squad(store, clock)
    store.claim_request("R2", GAREN_RUNTIME, "route-r2", clock.after(120), clock.now())
    routed = store.route_request(
        "R2",
        "route-r2",
        1,
        JARVAN_ID,
        f"event:{label}:routed",
        f"outbox:{label}:routed",
        clock.now(),
        recipient_squad_id=squad_id,
        required_capabilities=("request.route",),
    )
    clock.advance(1)
    store.register_runtime(
        RuntimeRegistrationCommand(
            CURSOR_RUNTIME,
            JARVAN_ID,
            "cursor-thread",
            "herdr",
            CURSOR_SESSION,
            CURSOR_PANE,
            CURSOR_GENERATION,
            "idle",
            True,
            clock.now(),
        )
    )
    return store, clock, routed


def _effect(store, outbox_id: str) -> dict:
    value = store.cursor_steering_effect(outbox_id)
    assert value is not None
    return value


def test_idle_submit_and_structured_route(root: Path) -> None:
    store, clock, routed = routed_cursor_context(root, "cursor-idle")
    herdr = FakeCursorHerdr("idle")
    # Inject only the command runner; production dispatch still selects the
    # installed Cursor adapter from the canonical runtime target.
    from league.canonical_delivery import InstalledDeliveryAdapter

    result = dispatch_event(
        store,
        outbox_id=routed["outbox_id"],
        event_id=routed["event_id"],
        recipient_agent_id=JARVAN_ID,
        at=clock.now(),
        adapter=InstalledDeliveryAdapter(store=store, at=clock.now(), runner=herdr),
    )
    assert result["state"] == "delivered" and result["effect_kind"] == "cursor_steering", result
    prompt_effects = [command for command in herdr.effects if "prompt" in command]
    assert len(prompt_effects) == 1
    assert not any("send-text" in command or "send-keys" in command for command in herdr.effects)
    prompt = prompt_effects[0][-1]
    assert prompt.startswith("LEAGUE ROUTED DELIVERY ")
    routed_payload = json.loads(prompt.removeprefix("LEAGUE ROUTED DELIVERY "))
    assert routed_payload["schema"] == "league.routed-delivery.v1"
    assert routed_payload["delivery"]["event_id"] == routed["event_id"]
    assert routed_payload["actions"][0]["argv"] == [
        "league",
        "--state-root",
        str(store.state_root),
        "request",
        "accept-routed",
        "--event-id",
        routed["event_id"],
        "--recipient-agent-id",
        JARVAN_ID,
        "--runtime-instance-id",
        CURSOR_RUNTIME,
    ]
    durable = _effect(store, routed["outbox_id"])
    assert durable["state"] == "acknowledged"
    assert durable["receipt"]["action"] == "idle_submit"
    assert durable["receipt"]["commands"] == [
        {"command": "agent.prompt", "enter_count": 1}
    ]
    accept_command = [
        str(ROOT / "bin" / "league"),
        *routed_payload["actions"][0]["argv"][1:],
    ]
    accepted = subprocess.run(accept_command, capture_output=True, text=True, check=False)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    accepted_payload = json.loads(accepted.stdout)["result"]
    assert accepted_payload["accepted"] is True
    assert accepted_payload["request_id"] == "R2"
    assert accepted_payload["claim_token"].startswith("routed-")
    duplicate_accept = subprocess.run(
        accept_command, capture_output=True, text=True, check=False
    )
    assert duplicate_accept.returncode == 0
    assert json.loads(duplicate_accept.stdout)["result"]["idempotent"] is True
    store.close()


def test_working_double_enter_and_post_steer_ack(root: Path) -> None:
    store, clock, routed = routed_cursor_context(root, "cursor-working")
    herdr = FakeCursorHerdr("working")
    from league.canonical_delivery import InstalledDeliveryAdapter

    result = dispatch_event(
        store,
        outbox_id=routed["outbox_id"],
        event_id=routed["event_id"],
        recipient_agent_id=JARVAN_ID,
        at=clock.now(),
        adapter=InstalledDeliveryAdapter(store=store, at=clock.now(), runner=herdr),
    )
    assert result["state"] == "delivered"
    assert len(herdr.effects) == 2
    assert "send-text" in herdr.effects[0]
    assert herdr.effects[1][-3:] == [CURSOR_PANE, "enter", "enter"]
    assert "send-keys" in herdr.effects[1]
    durable = _effect(store, routed["outbox_id"])
    assert durable["state"] == "acknowledged"
    assert durable["receipt"]["action"] == "working_steer"
    assert durable["receipt"]["post_state_change_seq"] > 40
    assert durable["receipt"]["commands"][-1] == {
        "command": "pane.send-keys",
        "keys": ["enter", "enter"],
    }
    store.close()


def test_working_state_race_never_double_enters(root: Path) -> None:
    store, clock, routed = routed_cursor_context(root, "cursor-race")
    herdr = FakeCursorHerdr("working", race_at_get=3)
    from league.canonical_delivery import InstalledDeliveryAdapter

    result = dispatch_event(
        store,
        outbox_id=routed["outbox_id"],
        event_id=routed["event_id"],
        recipient_agent_id=JARVAN_ID,
        at=clock.now(),
        adapter=InstalledDeliveryAdapter(store=store, at=clock.now(), runner=herdr),
    )
    assert result["state"] == "pending" and result["reason"] == "cursor_state_changed"
    assert len([command for command in herdr.effects if "send-text" in command]) == 1
    assert not any("send-keys" in command for command in herdr.effects)
    durable = _effect(store, routed["outbox_id"])
    assert durable["state"] == "refused"
    assert durable["receipt"]["phase"] == "pre_interrupt"
    store.close()


def test_duplicate_adapter_retry_never_replays_input(root: Path) -> None:
    store, clock, routed = routed_cursor_context(root, "cursor-duplicate")
    identity = OutboxDispatchIdentity(
        routed["outbox_id"],
        routed["event_id"],
        JARVAN_ID,
        "dispatcher:cursor-duplicate",
        "attempt:cursor-duplicate",
    )
    store.claim_outbox(identity, clock.after(30), clock.now())
    target = store.delivery_target(JARVAN_ID, clock.now())
    assert target is not None
    envelope = store.outbox_envelope(routed["outbox_id"], routed["event_id"], JARVAN_ID)
    herdr = FakeCursorHerdr("working")
    adapter = HerdrCursorSteeringAdapter(store, at=clock.now(), runner=herdr)
    first = adapter.send(target, envelope)
    effects_after_first = list(herdr.effects)
    second = adapter.send(target, envelope)
    assert second == first
    assert herdr.effects == effects_after_first
    assert len(herdr.effects) == 2
    assert _effect(store, routed["outbox_id"])["state"] == "effect_applied"
    store.close()


def test_wrong_pane_and_replaced_session_refuse(root: Path) -> None:
    from league.canonical_delivery import InstalledDeliveryAdapter

    for suffix, runner, reason in (
        ("pane", FakeCursorHerdr("idle", wrong_pane=True), "cursor_wrong_pane"),
        (
            "session",
            FakeCursorHerdr("idle", wrong_session=True),
            "cursor_session_replaced",
        ),
    ):
        store, clock, routed = routed_cursor_context(root, f"cursor-wrong-{suffix}")
        result = dispatch_event(
            store,
            outbox_id=routed["outbox_id"],
            event_id=routed["event_id"],
            recipient_agent_id=JARVAN_ID,
            at=clock.now(),
            adapter=InstalledDeliveryAdapter(store=store, at=clock.now(), runner=runner),
        )
        assert result["state"] == "pending" and result["reason"] == reason
        assert runner.effects == []
        assert store.cursor_steering_effect(routed["outbox_id"]) is None
        store.close()


def test_ambiguous_process_unavailable_input_and_missing_ack_refuse(root: Path) -> None:
    from league.canonical_delivery import InstalledDeliveryAdapter

    cases = (
        ("ambiguous", FakeCursorHerdr("idle", ambiguous_process=True), "cursor_process_ambiguous"),
        ("input", FakeCursorHerdr("idle", interactive_ready=False), "cursor_input_unavailable"),
        ("done", FakeCursorHerdr("done"), "cursor_state_unavailable"),
        ("ack", FakeCursorHerdr("working", acknowledge=False), "cursor_steering_ack_unverified"),
    )
    for suffix, runner, reason in cases:
        store, clock, routed = routed_cursor_context(root, f"cursor-refusal-{suffix}")
        result = dispatch_event(
            store,
            outbox_id=routed["outbox_id"],
            event_id=routed["event_id"],
            recipient_agent_id=JARVAN_ID,
            at=clock.now(),
            adapter=InstalledDeliveryAdapter(store=store, at=clock.now(), runner=runner),
        )
        assert result["state"] == "pending" and result["reason"] == reason
        if suffix == "ack":
            assert len(runner.effects) == 2
            assert _effect(store, routed["outbox_id"])["state"] == "refused"
        else:
            assert runner.effects == []
        store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-cursor-steering-") as temporary:
        root = Path(temporary)
        test_idle_submit_and_structured_route(root)
        test_working_double_enter_and_post_steer_ack(root)
        test_working_state_race_never_double_enters(root)
        test_duplicate_adapter_retry_never_replays_input(root)
        test_wrong_pane_and_replaced_session_refuse(root)
        test_ambiguous_process_unavailable_input_and_missing_ack_refuse(root)
    print(
        "PASS: installed Cursor idle submit, working steer, race fence, duplicate retry, "
        "identity refusal, and post-steer acknowledgement"
    )


if __name__ == "__main__":
    main()
