#!/usr/bin/env python3
"""Focused persistent event-driven supervisor lifecycle proof."""

from __future__ import annotations

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import sys
import subprocess
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    GAREN_RUNTIME_TWO,
    JARVAN_ID,
    JARVAN_RUNTIME,
    create_context,
)
from storage_fixture import SHOTCALLER_ID  # noqa: E402
from league.canonical_delivery import InstalledDeliveryAdapter  # noqa: E402
from league.canonical_watcher import _prompt_identity  # noqa: E402
from league.persistent_supervisor import (  # noqa: E402
    BoundedRuntimeCommandRunner,
    PersistentSupervisor,
    SupervisorUnavailable,
    notify_user_message,
    send_supervisor_message,
    stop_supervisor,
    supervisor_status,
)
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import RuntimeRegistrationCommand  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402


class FakeWakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def send(self, binding, envelope) -> None:
        self.calls.append((dict(binding), dict(envelope)))


class ExplodingWakeAdapter:
    def send(self, binding, envelope) -> None:
        del binding, envelope
        raise KeyError("synthetic malformed adapter result")


class FakeRecoveryAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def recover(self, state_root: Path, prompt_ids: tuple[str, ...]) -> None:
        self.calls.append((state_root, prompt_ids))
        self.started.set()
        self.release.wait(timeout=5)
        self.completed.set()


class FailOneRenewalFactory:
    def __init__(self) -> None:
        self.registration_calls = 0
        self.failed = False

    def __call__(self, state_root: Path):
        factory = self

        class StoreProxy:
            def __init__(self) -> None:
                self.store = SQLiteStorage(state_root)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                self.store.close()

            def __getattr__(self, name):
                return getattr(self.store, name)

            def register_watcher(self, *args, **kwargs):
                factory.registration_calls += 1
                if factory.registration_calls == 2:
                    factory.failed = True
                    raise StorageRefusal("busy", "synthetic renewal contention", retryable=True)
                return self.store.register_watcher(*args, **kwargs)

        return StoreProxy()


def _start(runtime: PersistentSupervisor):
    errors: list[BaseException] = []

    def run() -> None:
        try:
            runtime.run(emit_ready=False)
        except BaseException as exc:  # pragma: no cover - surfaced in caller
            errors.append(exc)

    thread = threading.Thread(target=run, name="synthetic-persistent-supervisor")
    thread.start()
    assert runtime.ready.wait(timeout=5), errors
    return thread, errors


def _future(seconds: float) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    )


def _close_secondary_runtime(store: SQLiteStorage, at: str) -> None:
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
            at=at,
        )
    )


def test_runtime_inventory_output_is_bounded() -> None:
    runner = BoundedRuntimeCommandRunner(max_output_bytes=32)
    try:
        runner(
            [sys.executable, "-c", "print('x' * 64)"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except StorageRefusal as exc:
        assert exc.code == "runtime_observation_refused" and exc.retryable
    else:
        raise AssertionError("oversized runtime inventory output was retained")


def main() -> None:
    test_runtime_inventory_output_is_bounded()
    with tempfile.TemporaryDirectory(prefix="l66-supervisor-") as temporary:
        root = Path(temporary)
        state, store, clock = create_context(root, "state")
        _close_secondary_runtime(store, clock.now())
        binding = store.supervisor_binding("Garen")
        store.close()

        fake = FakeWakeAdapter()
        recovery = FakeRecoveryAdapter()
        runtime = PersistentSupervisor(
            state,
            callsign="Garen",
            lease_seconds=0.8,
            renew_seconds=0.2,
            wake_adapter=fake,
            recovery_adapter=recovery,
        )
        thread, errors = _start(runtime)
        first = supervisor_status(state, "Garen")
        assert first["live"] and first["event_driven"] and first["lease_valid"], first
        runtime.wake_adapter = ExplodingWakeAdapter()
        try:
            send_supervisor_message(
                f"unix:{runtime.socket_path}",
                {
                    "kind": "champion-event",
                    "fence": first["fence"],
                    "runtime_generation": binding["runtime_generation"],
                    "envelope": {"event_id": "event:adapter-error"},
                },
            )
        except SupervisorUnavailable:
            pass
        else:
            raise AssertionError("unexpected adapter failure did not return a refusal")
        runtime.wake_adapter = fake

        hook_environment = {
            **os.environ,
            "LEAGUE_STATE_ROOT": str(state),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        prompt_payload = {
            "session_id": f"session:{GAREN_RUNTIME}",
            "turn_id": "turn:brokered-prompt",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Synthetic exact brokered owner prompt",
        }
        prompt_started = time.monotonic()
        prompt_wake_latency = None
        submitted = subprocess.run(
            [str(ROOT / "bin/agent-watcher"), "codex-user-prompt-hook"],
            input=json.dumps(prompt_payload),
            capture_output=True,
            text=True,
            env=hook_environment,
            timeout=10,
            check=False,
        )
        assert submitted.returncode == 0 and json.loads(submitted.stdout) == {}, submitted.stderr
        prompt_wake_latency = time.monotonic() - prompt_started
        assert runtime.user_priority.is_set() and prompt_wake_latency < 2
        assert runtime.user_priority.is_set() and runtime.user_priority_generation == 1
        try:
            send_supervisor_message(
                f"unix:{runtime.socket_path}",
                {"kind": "hook", "hook": {"command": "unsupported"}},
            )
        except StorageRefusal as exc:
            assert exc.code == "prompt_hook_invalid"
        else:
            raise AssertionError("brokered StorageRefusal was not preserved")
        with SQLiteStorage(state) as observer:
            intake = observer.untriaged_intake(SHOTCALLER_ID)
            assert intake["returned_count"] == 1
            assert intake["prompts"][0]["body"] == prompt_payload["prompt"]

        stop_payload = {
            "session_id": f"session:{GAREN_RUNTIME}",
            "turn_id": "turn:brokered-prompt",
            "hook_event_name": "Stop",
            "stop_hook_active": True,
        }
        stopped_hook = subprocess.run(
            [str(ROOT / "bin/agent-watcher"), "codex-stop-hook"],
            input=json.dumps(stop_payload),
            capture_output=True,
            text=True,
            env=hook_environment,
            timeout=10,
            check=False,
        )
        stop_output = json.loads(stopped_hook.stdout)
        assert stopped_hook.returncode == 0 and stop_output["decision"] == "block"
        feedback_payload = {
            **prompt_payload,
            "prompt": stop_output["reason"],
        }
        feedback = subprocess.run(
            [str(ROOT / "bin/agent-watcher"), "codex-user-prompt-hook"],
            input=json.dumps(feedback_payload),
            capture_output=True,
            text=True,
            env=hook_environment,
            timeout=10,
            check=False,
        )
        assert feedback.returncode == 0 and json.loads(feedback.stdout) == {}
        steer_payload = {**prompt_payload, "prompt": "Synthetic same-turn genuine steer"}
        steer = subprocess.run(
            [str(ROOT / "bin/agent-watcher"), "codex-user-prompt-hook"],
            input=json.dumps(steer_payload),
            capture_output=True,
            text=True,
            env=hook_environment,
            timeout=10,
            check=False,
        )
        assert steer.returncode == 0 and json.loads(steer.stdout) == {}
        assert runtime.user_priority_generation == 2
        with SQLiteStorage(state) as observer:
            intake = observer.untriaged_intake(SHOTCALLER_ID)
            assert [row["body"] for row in intake["prompts"]] == [
                prompt_payload["prompt"],
                steer_payload["prompt"],
            ]

        orphan_payload = {
            "session_id": "session:synthetic-orphan",
            "turn_id": "turn:synthetic-orphan",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Synthetic orphaned recovery prompt",
        }
        orphan = subprocess.run(
            [str(ROOT / "bin/agent-watcher"), "codex-user-prompt-hook"],
            input=json.dumps(orphan_payload),
            capture_output=True,
            text=True,
            env=hook_environment,
            timeout=10,
            check=False,
        )
        assert orphan.returncode == 0 and json.loads(orphan.stdout) == {}
        assert recovery.started.wait(timeout=2) and not recovery.completed.is_set()
        recovery.release.set()
        assert recovery.completed.wait(timeout=2)
        assert recovery.calls and len(recovery.calls[0][1]) == 1

        conflict_payload = {
            "session_id": f"session:{GAREN_RUNTIME}",
            "turn_id": "turn:brokered-owner-conflict",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Synthetic prompt with conflicting durable ownership",
        }
        capture_event_id = "codex-user-prompt:" + "b" * 32
        prompt_id, source_event_key = _prompt_identity(
            "codex",
            conflict_payload["session_id"],
            f"{conflict_payload['turn_id']}\0{capture_event_id}",
            conflict_payload["prompt"],
        )
        encoded = conflict_payload["prompt"].encode("utf-8")
        with SQLiteStorage(state) as observer:
            before_scope = tuple(observer.connection.execute(
                "SELECT user_message_generation,wait_generation FROM watcher_scopes "
                "WHERE actor_agent_id=?",
                (SHOTCALLER_ID,),
            ).fetchone())
            with observer._transaction():
                observer.connection.execute(
                    """
                    INSERT INTO prompts
                      (prompt_id,intake_actor_id,runtime_instance_id,adapter_kind,session_ref,
                       source_event_key,triage_state,triage_digest,created_at,
                       current_owner_agent_id,current_owner_runtime_instance_id)
                    VALUES(?,?,?,?,?,?,'untriaged',NULL,?,?,?)
                    """,
                    (
                        prompt_id, JARVAN_ID, JARVAN_RUNTIME, "codex",
                        conflict_payload["session_id"], source_event_key, clock.now(),
                        JARVAN_ID, JARVAN_RUNTIME,
                    ),
                )
                observer.connection.execute(
                    """
                    INSERT INTO prompt_payloads
                      (prompt_id,body,body_hash,byte_count,pruned_at)
                    VALUES(?,?,?,?,NULL)
                    """,
                    (
                        prompt_id,
                        conflict_payload["prompt"],
                        hashlib.sha256(encoded).hexdigest(),
                        len(encoded),
                    ),
                )
        runtime.user_priority.clear()
        before_priority = runtime.user_priority_generation
        conflict_result = send_supervisor_message(
            f"unix:{runtime.socket_path}",
            {
                "kind": "hook",
                "hook": {
                    "command": "codex-user-prompt-hook",
                    "shotcaller": "Garen",
                    "session_id": None,
                    "payload": conflict_payload,
                    "capture_event_id": capture_event_id,
                },
            },
        )
        assert conflict_result["priority"] is None
        assert conflict_result["capture"]["state"] == "quarantined"
        assert conflict_result["capture"]["wake_committed"] is False
        assert conflict_result["capture"]["priority_eligible"] is False
        assert runtime.user_priority_generation == before_priority
        assert not runtime.user_priority.is_set()
        with SQLiteStorage(state) as observer:
            quarantine = observer.connection.execute(
                """
                SELECT state,reason,wake_actor_id,wake_scope_id,wake_committed
                  FROM prompt_quarantine WHERE prompt_id=?
                """,
                (prompt_id,),
            ).fetchone()
            owner = observer.connection.execute(
                "SELECT intake_actor_id,runtime_instance_id FROM prompts WHERE prompt_id=?",
                (prompt_id,),
            ).fetchone()
            after_scope = tuple(observer.connection.execute(
                "SELECT user_message_generation,wait_generation FROM watcher_scopes "
                "WHERE actor_agent_id=?",
                (SHOTCALLER_ID,),
            ).fetchone())
        assert tuple(quarantine) == (
            "quarantined", "runtime_unverified", None, None, 0
        )
        assert tuple(owner) == (JARVAN_ID, JARVAN_RUNTIME)
        assert after_scope == before_scope

        with SQLiteStorage(state) as observer:
            target = observer.delivery_target(SHOTCALLER_ID, _future(0))
            assert target is not None and target["channel"] == "watcher"
            assert str(target["locator"]).startswith("unix:")
            assert notify_user_message(observer, SHOTCALLER_ID, "prompt:synthetic")
            with patch(
                "league.persistent_supervisor.send_supervisor_message",
                side_effect=StorageRefusal("watcher_fenced", "synthetic stale fence"),
            ):
                assert not notify_user_message(
                    observer, SHOTCALLER_ID, "prompt:synthetic-stale"
                )

        envelope = {
            "outbox_id": "outbox:synthetic-supervisor",
            "event_id": "event:synthetic-supervisor",
            "recipient_agent_id": SHOTCALLER_ID,
            "status": "working",
            "summary": "Synthetic persistent wake.",
        }
        receipt = InstalledDeliveryAdapter().send("watcher", target, envelope)
        assert receipt.effect_kind == "watcher_event"
        assert fake.calls and fake.calls[0][1]["event_id"] == envelope["event_id"]

        time.sleep(0.45)
        renewed = supervisor_status(state, "Garen")
        assert renewed["live"] and renewed["fence"] > first["fence"]
        stopped = stop_supervisor(state, "Garen")
        assert stopped["stopped"] and not stopped["live"]
        thread.join(timeout=5)
        assert not thread.is_alive() and not errors
        with SQLiteStorage(state) as observer:
            assert observer.watcher_registration(SHOTCALLER_ID) is None

        stale_socket = runtime.socket_path
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(os.fspath(stale_socket))
        stale.close()
        with SQLiteStorage(state) as observer:
            observer.register_watcher(
                binding["scope_id"],
                "watcher:stale-owner",
                binding["actor_agent_id"],
                binding["runtime_instance_id"],
                f"unix:{stale_socket}",
                _future(30),
                50,
                datetime.now().astimezone().isoformat(timespec="microseconds"),
            )
        recovered_fake = FakeWakeAdapter()
        recovered_recovery = FakeRecoveryAdapter()
        recovered_recovery.release.set()
        recovered_runtime = PersistentSupervisor(
            state,
            callsign="Garen",
            lease_seconds=0.8,
            renew_seconds=0.2,
            wake_adapter=recovered_fake,
            recovery_adapter=recovered_recovery,
        )
        recovered_thread, recovered_errors = _start(recovered_runtime)
        recovered = supervisor_status(state, "Garen")
        assert recovered["live"] and recovered["fence"] > 50
        assert recovered_recovery.started.wait(timeout=2)
        stop_supervisor(state, "Garen")
        recovered_thread.join(timeout=5)
        assert not recovered_thread.is_alive() and not recovered_errors
        assert not stale_socket.exists()

        capacity_runtime = PersistentSupervisor(
            state,
            callsign="Garen",
            max_accepted_work=1,
        )
        capacity_runtime._executor = ThreadPoolExecutor(max_workers=1)
        capacity_release = threading.Event()
        assert capacity_runtime._submit(capacity_release.wait, 2)
        assert not capacity_runtime._submit(lambda: None)
        capacity_release.set()
        capacity_runtime._executor.shutdown(wait=True)
        capacity_runtime._executor = None

    with tempfile.TemporaryDirectory(prefix="l66-supervisor-renewal-") as temporary:
        state, store, clock = create_context(Path(temporary), "state")
        _close_secondary_runtime(store, clock.now())
        store.close()
        factory = FailOneRenewalFactory()
        renewal_runtime = PersistentSupervisor(
            state,
            callsign="Garen",
            lease_seconds=0.8,
            renew_seconds=0.15,
            wake_adapter=FakeWakeAdapter(),
            store_factory=factory,
        )
        renewal_thread, renewal_errors = _start(renewal_runtime)
        time.sleep(0.4)
        renewed = supervisor_status(state, "Garen")
        assert factory.failed and renewed["live"] and renewed["fence"] >= 3
        stop_supervisor(state, "Garen")
        renewal_thread.join(timeout=5)
        assert not renewal_thread.is_alive() and not renewal_errors

    with tempfile.TemporaryDirectory(prefix="l66-supervisor-uncertain-") as temporary:
        state, store, clock = create_context(Path(temporary), "state")
        _close_secondary_runtime(store, clock.now())
        binding = store.supervisor_binding("Garen")
        socket_path = PersistentSupervisor(state, callsign="Garen").socket_path
        assert not socket_path.exists()
        store.register_watcher(
            binding["scope_id"],
            "watcher:synthetic-unreachable",
            binding["actor_agent_id"],
            binding["runtime_instance_id"],
            f"unix:{socket_path}",
            _future(30),
            1,
            clock.now(),
        )
        store.close()
        environment = {
            **os.environ,
            "LEAGUE_STATE_ROOT": str(state),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        refused = subprocess.run(
            [str(ROOT / "bin/agent-watcher"), "codex-user-prompt-hook"],
            input=json.dumps(
                {
                    "session_id": f"session:{GAREN_RUNTIME}",
                    "turn_id": "turn:uncertain-owner",
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Synthetic prompt must not fall back",
                }
            ),
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
            check=False,
        )
        assert refused.returncode == 2
        assert "supervisor_ownership_uncertain" in refused.stderr
        with SQLiteStorage(state) as observer:
            assert observer.untriaged_intake(SHOTCALLER_ID)["returned_count"] == 0

    with tempfile.TemporaryDirectory(prefix="l66-supervisor-start-race-") as temporary:
        state, store, clock = create_context(Path(temporary), "state")
        _close_secondary_runtime(store, clock.now())
        store.close()
        runtime = PersistentSupervisor(state, callsign="Garen")
        runtime.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = runtime.lock_path.open("a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            refused = subprocess.run(
                [str(ROOT / "bin/agent-watcher"), "codex-user-prompt-hook"],
                input=json.dumps(
                    {
                        "session_id": f"session:{GAREN_RUNTIME}",
                        "turn_id": "turn:starting-owner",
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Synthetic prompt fenced during supervisor startup",
                    }
                ),
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "LEAGUE_STATE_ROOT": str(state),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                timeout=10,
                check=False,
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        assert refused.returncode == 2
        assert "supervisor_ownership_uncertain" in refused.stderr
        with SQLiteStorage(state) as observer:
            assert observer.untriaged_intake(SHOTCALLER_ID)["returned_count"] == 0

    print(
        "PASS: persistent supervisor owns one event socket, renews/fences its lease, "
        "captures exact-once prompts, suppresses Stop feedback, rearms same-turn steers, "
        "schedules orphan/backlog recovery off-path, delivers Champion wake, "
        "bounds accepted work, recovers one transient renewal, preserves broker refusals, "
        "recovers stale ownership, and stops cleanly"
    )


if __name__ == "__main__":
    main()
