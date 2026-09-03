#!/usr/bin/env python3
"""Semantic owner-stop authorization and exact delegated-control regressions."""

from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.agent_adapters import builtin_agent_adapter_registry  # noqa: E402
from league.canonical_watcher import handle_brokered_hook  # noqa: E402
from league.cli import main as league_main  # noqa: E402
from league.owner_stop import execute_owner_stop_controls  # noqa: E402
from league.persistent_supervisor import (  # noqa: E402
    PersistentSupervisor,
    stop_supervisor,
    supervisor_status,
)
from league.request_services import DeliveryReceipt  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import RuntimeRegistrationCommand, StorageRefusal  # noqa: E402
from lifecycle_fakes import FakeDeliveryAdapter  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    GAREN_RUNTIME_TWO,
    JARVAN_ID,
    create_context,
)
from storage_fixture import CHAMPION_ID, SHOTCALLER_ID  # noqa: E402
from test_multisquad_supervisor import (  # noqa: E402
    FakeWakeAdapter,
    _finish_startup_recovery,
    _multisquad_state,
    _start,
)


PI_WORKER_ID = "99999999-9999-4999-8999-999999999991"
OTHER_WORKER_ID = "99999999-9999-4999-8999-999999999992"


class FakeControlMultiplexer:
    capabilities = frozenset({"delivery"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def delivery(self, target: str, body: str) -> None:
        self.calls.append((target, body))


class ExactControlAdapter:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object], dict[str, object]]] = []
        self.fail_first = fail_first

    def send(self, channel, target, envelope):
        assert channel == "direct"
        self.calls.append((channel, dict(target), dict(envelope)))
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("synthetic unexpected owner-stop steering failure")
        return DeliveryReceipt(
            outbox_id=str(envelope["outbox_id"]),
            event_id=str(envelope["event_id"]),
            recipient_agent_id=str(envelope["recipient_agent_id"]),
            effect_kind="synthetic_owner_stop",
            effect_id=f"effect:{envelope['event_id']}",
        )


def _configure_owner_provider(store: SQLiteStorage, kind: str) -> tuple[str, str]:
    if kind == "codex":
        with store._transaction():
            store.connection.execute(
                "UPDATE runtime_instances SET status='closed',verified=0 "
                "WHERE runtime_instance_id=?",
                (GAREN_RUNTIME_TWO,),
            )
        return "session:runtime:garen:one", "turn:owner-stop"
    session = "/synthetic/pi/owner-stop.jsonl"
    with store._transaction():
        store.connection.execute(
            "UPDATE runtime_instances SET harness_kind='pi-thread',session_ref=? "
            "WHERE runtime_instance_id=?",
            (session, GAREN_RUNTIME),
        )
        store.connection.execute(
            "UPDATE runtime_instances SET status='closed',verified=0 "
            "WHERE runtime_instance_id=?",
            (GAREN_RUNTIME_TWO,),
        )
    return session, "input:owner-stop"


def _prompt_hook(kind: str, session: str, generation: str, body: str) -> dict[str, object]:
    if kind == "codex":
        return {
            "command": "codex-user-prompt-hook",
            "payload": {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "turn_id": generation,
                "prompt": body,
            },
            "capture_event_id": "codex-user-prompt:" + "1" * 32,
        }
    return {
        "command": "pi-input-hook",
        "payload": {
            "hook_event_name": "PiInput",
            "session_path": session,
            "input_id": generation,
            "prompt": body,
        },
    }


def _stop_hook(kind: str, session: str, generation: str) -> dict[str, object]:
    if kind == "codex":
        return {
            "command": "codex-stop-hook",
            "payload": {
                "hook_event_name": "Stop",
                "session_id": session,
                "turn_id": generation,
                "stop_hook_active": True,
            },
        }
    return {
        "command": "pi-stop-hook",
        "payload": {
            "hook_event_name": "PiStop",
            "session_path": session,
            "input_id": generation,
        },
    }


def _run_semantic_turn(
    state: Path,
    store: SQLiteStorage,
    clock,
    *,
    owner_control: dict[str, object] | None,
) -> dict[str, object]:
    intake = store.untriaged_intake(SHOTCALLER_ID)
    assert intake["returned_count"] == 1
    decision: dict[str, object] = {
        "items": [
            {
                "summary": "Owner stop instruction accounted for semantically",
                "disposition": "acknowledgement",
            }
        ]
    }
    if owner_control is not None:
        decision["owner_control"] = owner_control
    source = BytesIO(
        (
            json.dumps(
                {
                    "candidate_inventory_digest": intake["candidate_inventory"]["digest"],
                    "decisions": [decision],
                    "plans": [],
                },
                separators=(",", ":"),
            )
            + "\n"
            + json.dumps({"actions": []}, separators=(",", ":"))
            + "\n"
        ).encode()
    )
    sink = BytesIO()
    assert (
        league_main(
            [
                "--state-root",
                str(state),
                "request",
                "turn",
                "--owner-agent-id",
                SHOTCALLER_ID,
                "--at",
                clock.now(),
            ],
            input_stream=source,
            output=sink,
        )
        == 0
    )
    messages = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [message["result"]["phase"] for message in messages] == [
        "intake",
        "begun",
        "committed",
    ]
    return messages[-1]["result"]


def test_codex_pi_owner_control_uses_declared_steering() -> None:
    registry = builtin_agent_adapter_registry()
    for kind in ("codex", "pi"):
        adapter = registry.adapter(kind)
        assert adapter.steering_handler is not adapter.delivery_handler
        multiplexer = FakeControlMultiplexer()
        adapter.control_delegated(
            target={"routing_name": f"synthetic-{kind}", "locator": "pane:synthetic"},
            envelope={
                "event_id": f"event:owner-stop:{kind}",
                "event_type": "owner_stop_control",
                "status": "pause_requested",
                "summary": "arbitrary owner language is never forwarded",
            },
            multiplexer=multiplexer,
        )
        assert len(multiplexer.calls) == 1
        target, body = multiplexer.calls[0]
        assert target == f"synthetic-{kind}"
        assert "LEAGUE OWNER CONTROL" in body
        assert "arbitrary owner language" not in body


def test_codex_and_pi_require_semantic_control_and_consume_by_generation(
    root: Path,
) -> None:
    registry = builtin_agent_adapter_registry()
    for kind in ("codex", "pi"):
        provider_adapter = registry.adapter(kind)
        assert "steer" in provider_adapter.lifecycle_operations
        assert callable(provider_adapter.control_delegated)
        assert "prompt" in provider_adapter.contract.capabilities
        state, store, clock = create_context(root, f"semantic-{kind}")
        session, generation = _configure_owner_provider(store, kind)
        captured = handle_brokered_hook(
            store,
            _prompt_hook(
                kind,
                session,
                generation,
                "Stop everything now; this natural language must not authorize the hook.",
            ),
        )
        assert captured["capture"]["prompt_id"]
        committed = _run_semantic_turn(
            state, store, clock, owner_control=None
        )
        assert committed["owner_stop_controls"] == []
        blocked = handle_brokered_hook(store, _stop_hook(kind, session, generation))
        assert blocked["hook_output"]

        next_generation = f"{generation}:next"
        handle_brokered_hook(
            store,
            _prompt_hook(kind, session, next_generation, "Record the exact owner control."),
        )
        committed = _run_semantic_turn(
            state,
            store,
            clock,
            owner_control={"action": "stop", "interrupt_delegates": False},
        )
        assert committed["owner_stop_controls"][0]["state"] == "authorized"
        first = handle_brokered_hook(
            store, _stop_hook(kind, session, next_generation)
        )
        repeated = handle_brokered_hook(
            store, _stop_hook(kind, session, next_generation)
        )
        assert first["hook_output"] == repeated["hook_output"] == {}

        third_generation = f"{generation}:third"
        handle_brokered_hook(
            store,
            _prompt_hook(kind, session, third_generation, "A new ordinary owner prompt."),
        )
        store.triage_prompt(
            store.untriaged_intake(SHOTCALLER_ID)["prompts"][0]["prompt_id"],
            [
                {
                    "prompt_item_id": f"item:{kind}:third",
                    "ordinal": 1,
                    "summary": "Ordinary prompt",
                    "disposition": "acknowledgement",
                    "request_id": None,
                }
            ],
            clock.now(),
        )
        reblocked = handle_brokered_hook(
            store, _stop_hook(kind, session, third_generation)
        )
        assert reblocked["hook_output"]
        store.close()


def _add_delegated_runtime(
    store: SQLiteStorage,
    clock,
    *,
    actor_id: str,
    callsign: str,
    owner_id: str,
    harness_kind: str,
) -> None:
    with store._transaction():
        existing = store.connection.execute(
            "SELECT agent_id FROM agent_instances WHERE agent_id=?", (actor_id,)
        ).fetchone()
        if existing is None:
            store.connection.execute(
                """
                INSERT INTO agent_instances
                  (agent_id,callsign,role,shotcaller_agent_id,task_id,kind,address,
                   thread_id,backend,routing_name,display_agent,repository,issue,
                   branch,worktree,status,version,updated_at,update_text,blocker,
                   next_action,metadata_json,retired_at)
                VALUES(?,?,'champion',?,NULL,?,'pane:'||?,?,'herdr',lower(?),?,
                       NULL,NULL,NULL,NULL,'working',1,?,'Synthetic delegated work.',
                       NULL,'Await owner control.','{}',NULL)
                """,
                (
                    actor_id,
                    callsign,
                    owner_id,
                    harness_kind,
                    callsign,
                    f"session:{callsign.lower()}",
                    callsign,
                    "Pi" if harness_kind == "pi-thread" else "Codex",
                    clock.now(),
                ),
            )
    store.register_runtime(
        RuntimeRegistrationCommand(
            runtime_instance_id=f"runtime:{callsign.lower()}",
            actor_agent_id=actor_id,
            harness_kind=harness_kind,
            backend_kind="herdr",
            session_ref=f"session:{callsign.lower()}",
            endpoint=f"pane:{callsign}",
            runtime_generation=f"generation:{callsign.lower()}",
            status="active",
            verified=True,
            at=clock.now(),
            capabilities=("prompt", "stop"),
        )
    )


def test_live_watcher_owner_stop_interrupts_exact_codex_pi_delegates_only(
    root: Path,
) -> None:
    state, store = _multisquad_state(root, "multisquad-owner-stop")
    from lifecycle_fakes import FakeClock

    clock = FakeClock()
    with store._transaction():
        store.connection.execute(
            "UPDATE agent_instances SET status='completed' "
            "WHERE shotcaller_agent_id=? AND role='hidden-worker'",
            (SHOTCALLER_ID,),
        )
    _add_delegated_runtime(
        store,
        clock,
        actor_id=CHAMPION_ID,
        callsign="Thresh",
        owner_id=SHOTCALLER_ID,
        harness_kind="codex-thread",
    )
    _add_delegated_runtime(
        store,
        clock,
        actor_id=PI_WORKER_ID,
        callsign="Curie",
        owner_id=SHOTCALLER_ID,
        harness_kind="pi-thread",
    )
    _add_delegated_runtime(
        store,
        clock,
        actor_id=OTHER_WORKER_ID,
        callsign="Ahri",
        owner_id=JARVAN_ID,
        harness_kind="codex-thread",
    )
    # Attached watcher routing is valid for ordinary delivery, but an exact
    # owner control must bypass it and target the captured delegated runtime.
    store.register_watcher(
        "watcher:Thresh",
        "watcher:persistent:thresh",
        CHAMPION_ID,
        "runtime:thresh",
        "unix:/tmp/synthetic-thresh-watcher.sock",
        clock.after(300),
        1,
        clock.now(),
    )
    startup_delivery = FakeDeliveryAdapter()
    store.close()
    runtime = PersistentSupervisor(
        state,
        lease_seconds=2,
        renew_seconds=0.2,
        wake_adapter=FakeWakeAdapter(),
        delivery_adapter=startup_delivery,
    )
    thread, errors = _start(runtime)
    try:
        with SQLiteStorage(state) as control_store:
            _finish_startup_recovery(control_store, startup_delivery)
            assert supervisor_status(state)["live"] is True
            prompt = control_store.intake_prompt(
                "prompt:owner-stop:multi",
                SHOTCALLER_ID,
                GAREN_RUNTIME,
                "codex",
                "session:runtime:garen:one",
                "source:owner-stop:multi",
                "Structured semantic owner stop.",
                clock.now(),
            )
            control_store.triage_prompt(
                prompt["prompt_id"],
                [
                    {
                        "prompt_item_id": "item:owner-stop:multi",
                        "ordinal": 1,
                        "summary": "Owner requested delegated interruption",
                        "disposition": "acknowledgement",
                        "request_id": None,
                    }
                ],
                clock.now(),
            )
            prepared = control_store.prepare_owner_stop_control(
                SHOTCALLER_ID,
                "owner-stop:multi",
                prompt["prompt_id"],
                True,
                clock.now(),
            )
            adapter = ExactControlAdapter()
            with patch(
                "league.sqlite_store.finalize_owner_stop_control_operation",
                side_effect=StorageRefusal(
                    "busy", "synthetic transient finalization", retryable=True
                ),
            ):
                pending = execute_owner_stop_controls(
                    control_store, (prepared,), clock.now(), adapter=adapter
                )
            assert pending[0]["state"] == "dispatch_pending"
            first_call_count = len(adapter.calls)
            completed = execute_owner_stop_controls(
                control_store, (prepared,), clock.now(), adapter=adapter
            )
            assert completed[0]["state"] == "authorized"
            assert len(adapter.calls) == first_call_count
            repeated = execute_owner_stop_controls(
                control_store, (prepared,), clock.now(), adapter=adapter
            )
            assert repeated[0]["state"] == "authorized"
            assert len(adapter.calls) == first_call_count
            recipients = {
                str(envelope["recipient_agent_id"])
                for _, _, envelope in adapter.calls
            }
            assert recipients == {CHAMPION_ID, PI_WORKER_ID}
            assert OTHER_WORKER_ID not in recipients
            assert {
                str(target["harness_kind"]) for _, target, _ in adapter.calls
            } == {"codex-thread", "pi-thread"}
            assert all(
                envelope["event_type"] == "owner_stop_control"
                for _, _, envelope in adapter.calls
            )
            stop = control_store.stop_decision(
                prepared["scope_id"],
                SHOTCALLER_ID,
                "terminal:owner-stop:multi",
                clock.now(),
            )
            assert stop["decision"] == "allow"
            assert stop["status"] == "semantic_owner_stop"
            assert supervisor_status(state)["live"] is True
    finally:
        stop_supervisor(state)
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors


def test_persistent_supervisor_recovers_failed_owner_control(root: Path) -> None:
    state, store = _multisquad_state(root, "owner-stop-service-recovery")
    from lifecycle_fakes import FakeClock

    clock = FakeClock("2020-01-01T00:00:00Z")
    with store._transaction():
        store.connection.execute(
            "UPDATE agent_instances SET status='completed' WHERE shotcaller_agent_id=?",
            (SHOTCALLER_ID,),
        )
    _add_delegated_runtime(
        store,
        clock,
        actor_id=PI_WORKER_ID,
        callsign="Curie",
        owner_id=SHOTCALLER_ID,
        harness_kind="pi-thread",
    )
    prompt = store.intake_prompt(
        "prompt:owner-stop:recovery",
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        "session:owner-stop:recovery",
        "source:owner-stop:recovery",
        "Recover this structured owner stop.",
        clock.now(),
    )
    store.triage_prompt(
        prompt["prompt_id"],
        [
            {
                "prompt_item_id": "item:owner-stop:recovery",
                "ordinal": 1,
                "summary": "Recover delegated pause",
                "disposition": "acknowledgement",
                "request_id": None,
            }
        ],
        clock.now(),
    )
    prepared = store.prepare_owner_stop_control(
        SHOTCALLER_ID,
        "owner-stop:recovery",
        prompt["prompt_id"],
        True,
        clock.now(),
    )
    store.close()

    adapter = ExactControlAdapter(fail_first=True)
    runtime = PersistentSupervisor(state, delivery_adapter=adapter)
    runtime._bindings = {
        SHOTCALLER_ID: {"scope_id": prepared["scope_id"]}
    }
    runtime._recover_owner_stops()
    with SQLiteStorage(state) as observer:
        failed = observer.pending_owner_stop_controls((str(prepared["scope_id"]),))
        assert failed[0]["state"] == "failed"
        with observer._transaction():
            observer.connection.execute(
                "UPDATE delivery_outbox SET available_at='2000-01-01T00:00:00Z' "
                "WHERE outbox_id=?",
                (prepared["targets"][0]["outbox_id"],),
            )
    runtime._recover_owner_stops()
    with SQLiteStorage(state) as observer:
        selected = observer.resolve_supervisor_scope(SHOTCALLER_ID)
        row = observer.connection.execute(
            "SELECT metadata_json FROM watcher_scopes WHERE scope_id=?",
            (selected["scope_id"],),
        ).fetchone()
        owner_stop = json.loads(row["metadata_json"])["owner_stop"]
        assert owner_stop["state"] == "authorized", owner_stop
    assert len(adapter.calls) == 2


def test_owner_stop_control_rolls_back_with_turn_commit(root: Path) -> None:
    state, store, clock = create_context(root, "owner-stop-atomic-rollback")
    session, generation = _configure_owner_provider(store, "codex")
    handle_brokered_hook(
        store,
        _prompt_hook("codex", session, generation, "Record a structured owner stop."),
    )
    intake = store.untriaged_intake(SHOTCALLER_ID)
    source = BytesIO(
        (
            json.dumps(
                {
                    "candidate_inventory_digest": intake["candidate_inventory"]["digest"],
                    "decisions": [
                        {
                            "items": [
                                {
                                    "summary": "Owner stop acknowledged",
                                    "disposition": "acknowledgement",
                                }
                            ],
                            "owner_control": {
                                "action": "stop",
                                "interrupt_delegates": True,
                            },
                        }
                    ],
                    "plans": [],
                },
                separators=(",", ":"),
            )
            + "\n"
            + json.dumps({"actions": []}, separators=(",", ":"))
            + "\n"
        ).encode()
    )
    sink = BytesIO()
    with patch(
        "league.sqlite_watcher_ops.commit_shotcaller_turn",
        side_effect=RuntimeError("synthetic commit boundary failure"),
    ):
        assert (
            league_main(
                [
                    "--state-root",
                    str(state),
                    "request",
                    "turn",
                    "--owner-agent-id",
                    SHOTCALLER_ID,
                    "--at",
                    clock.now(),
                ],
                input_stream=source,
                output=sink,
            )
            == 2
        )
    selected = store.resolve_supervisor_scope(SHOTCALLER_ID)
    scope = store.connection.execute(
        "SELECT metadata_json FROM watcher_scopes WHERE scope_id=?",
        (selected["scope_id"],),
    ).fetchone()
    assert "owner_stop" not in json.loads(scope["metadata_json"])
    assert store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='owner_stop_control'"
    ).fetchone()[0] == 0
    store.close()


def test_startup_scope_reconciliation_selects_one_or_refuses_actionably(
    root: Path,
) -> None:
    _, store, clock = create_context(root, "scope-reconciliation")
    _configure_owner_provider(store, "codex")
    with store._transaction():
        store.connection.execute(
            "UPDATE watcher_scopes SET actor_agent_id=?,metadata_json=? WHERE scope_id='W1-Garen'",
            (
                SHOTCALLER_ID,
                json.dumps(
                    {
                        "supervision": {
                            "mode": "calm",
                            "attachment_mode": "detached",
                            "detachment_receipt": {},
                            "service_owner": "persistent",
                        }
                    },
                    separators=(",", ":"),
                ),
            ),
        )
        store.connection.execute(
            """
            INSERT INTO watcher_scopes
              (scope_id,schema_version,enabled,allow_stop_once,stop_blocked,
               generation,initialized,user_message_generation,wait_active,
               wait_generation,wait_pid,wait_process_start,last_event_id,
               metadata_json,actor_agent_id,block_on_obligations,
               last_blocked_wait_generation,last_user_priority_generation,
               last_terminal_generation)
            VALUES('stale:Garen',3,1,0,0,1,1,0,0,1,NULL,NULL,NULL,'{}',?,1,-1,0,NULL)
            """,
            (SHOTCALLER_ID,),
        )
    selected = store.supervisor_bindings()
    assert selected[0]["scope_id"] == "W1-Garen"
    assert selected[0]["scope_reconciliation"]["candidate_count"] == 2
    assert selected[0]["scope_reconciliation"]["selected_by"] == "persistent_owner"
    with store._transaction():
        store.connection.execute(
            "UPDATE watcher_scopes SET metadata_json='{}' WHERE scope_id='W1-Garen'"
        )
    try:
        store.supervisor_bindings()
    except Exception as exc:
        assert getattr(exc, "code", None) == "supervisor_scope_ambiguous"
        assert "Garen" in str(exc) and "2 valid watcher scopes" in str(exc)
    else:
        raise AssertionError("ambiguous startup scopes did not fail actionably")
    store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-owner-stop-control-") as temporary:
        root = Path(temporary)
        test_codex_pi_owner_control_uses_declared_steering()
        test_codex_and_pi_require_semantic_control_and_consume_by_generation(root)
        test_live_watcher_owner_stop_interrupts_exact_codex_pi_delegates_only(root)
        test_persistent_supervisor_recovers_failed_owner_control(root)
        test_owner_stop_control_rolls_back_with_turn_commit(root)
        test_startup_scope_reconciliation_selects_one_or_refuses_actionably(root)
    print(
        "PASS: semantic Codex/Pi owner-stop authorization, exact delegated interruption, "
        "generation consumption, live watcher isolation, and actionable scope reconciliation"
    )


if __name__ == "__main__":
    main()
