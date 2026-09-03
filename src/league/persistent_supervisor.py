"""Persistent event-driven supervision outside model turns."""

from __future__ import annotations

import fcntl
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from .request_services import DeliveryAdapter
from .sqlite_store import SQLiteStorage
from .storage import StorageRefusal
from .storage_watcher import RuntimeRegistrationCommand
from .agent_adapters import adapter_kind_from_runtime
from .multiplexer_adapters import builtin_multiplexer_adapter_registry


MAX_MESSAGE_BYTES = 1_100_000
MAX_ACCEPTED_WORK = 16
MAX_RENEW_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 60
DEFAULT_RENEW_SECONDS = 20
DEFAULT_RECOVERY_SECONDS = 300
SOCKET_NAME = ".league-supervisor.sock"
LOCK_NAME = ".league-supervisor.lock"
MAX_RUNTIME_INVENTORY_OUTPUT_BYTES = 1_000_000


class BoundedRuntimeCommandRunner:
    """Run one runtime inventory command and retain only bounded output."""

    def __init__(self, max_output_bytes: int = MAX_RUNTIME_INVENTORY_OUTPUT_BYTES) -> None:
        if max_output_bytes < 1:
            raise ValueError("runtime command output bound must be positive")
        self.max_output_bytes = max_output_bytes

    def __call__(
        self,
        arguments: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if check or not capture_output or not text:
            raise ValueError("runtime command runner requires bounded text capture")
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.run(
                arguments,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
                check=False,
            )
            outputs: list[str] = []
            for stream in (stdout, stderr):
                if stream.tell() > self.max_output_bytes:
                    raise StorageRefusal(
                        "runtime_observation_refused",
                        "Herdr runtime inventory exceeded its output bound",
                        retryable=True,
                    )
                stream.seek(0)
                try:
                    outputs.append(stream.read().decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise StorageRefusal(
                        "runtime_observation_refused",
                        "Herdr runtime inventory was not UTF-8",
                        retryable=True,
                    ) from exc
        return subprocess.CompletedProcess(
            arguments, process.returncode, outputs[0], outputs[1]
        )


class SupervisorUnavailable(RuntimeError):
    """The exact persistent supervisor could not accept a wake."""


class WakeAdapter(Protocol):
    def send(self, binding: dict[str, Any], envelope: dict[str, Any]) -> None: ...


class SemanticRecoveryAdapter(Protocol):
    def recover(self, state_root: Path, prompt_ids: tuple[str, ...]) -> None: ...


class RuntimeObservationAdapter(Protocol):
    def observe(
        self, candidates: tuple[dict[str, Any], ...]
    ) -> dict[str, dict[str, str]]: ...


class HerdrRuntimeObservationAdapter:
    """Compatibility facade over registered read-only multiplexer inventories."""

    LIVE_STATUSES = frozenset({"active", "blocked", "done", "idle", "waiting", "working"})

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.runner = runner or BoundedRuntimeCommandRunner()
        self.multiplexers = builtin_multiplexer_adapter_registry(
            herdr_runner=CallableMultiplexerRunner(self.runner),
            herdr_binary="herdr",
        )

    @staticmethod
    def _session(agent: Mapping[str, Any]) -> str | None:
        session = agent.get("agent_session")
        value = session.get("value") if isinstance(session, Mapping) else None
        return value if isinstance(value, str) else None

    def observe(
        self, candidates: tuple[dict[str, Any], ...]
    ) -> dict[str, dict[str, str]]:
        if not candidates:
            return {}
        results: dict[str, dict[str, str]] = {}
        backends = sorted({str(item.get("backend_kind", "")) for item in candidates})
        for backend_kind in backends:
            try:
                multiplexer = self.multiplexers.adapter(backend_kind)
                if "discover" not in multiplexer.capabilities:
                    raise StorageRefusal(
                        "runtime_observation_unsupported",
                        "runtime monitor encountered an unsupported backend",
                    )
                agents = multiplexer.discover()
            except StorageRefusal:
                raise
            except (OSError, subprocess.SubprocessError) as exc:
                raise StorageRefusal(
                    "runtime_observation_refused",
                    "runtime inventory could not be observed exactly",
                    retryable=True,
                ) from exc
            indexes: dict[str, dict[str, set[int]]] = {
                "pane": {}, "route": {}, "session": {}
            }
            for index, agent in enumerate(agents):
                for kind, value in {
                    "pane": agent.get("pane_id"),
                    "route": agent.get("name"),
                    "session": self._session(agent),
                }.items():
                    if isinstance(value, str) and value:
                        indexes[kind].setdefault(value, set()).add(index)
            for candidate in (
                item for item in candidates if item.get("backend_kind") == backend_kind
            ):
                assignment_id = str(candidate["assignment_id"])
                related_indexes: set[int] = set()
                for kind, value in (
                    ("pane", candidate.get("endpoint")),
                    ("route", candidate.get("routing_name")),
                    ("session", candidate.get("session_ref")),
                ):
                    if isinstance(value, str) and value:
                        related_indexes.update(indexes[kind].get(value, ()))
                related = [agents[index] for index in sorted(related_indexes)]
                if not related:
                    results[assignment_id] = {"state": "missing", "fingerprint": "missing"}
                    continue
                if len(related) != 1:
                    results[assignment_id] = {"state": "mismatch", "fingerprint": "ambiguous"}
                    continue
                agent = related[0]
                session_ref = self._session(agent)
                try:
                    generation = multiplexer.runtime_generation(agent, str(session_ref or ""))
                    adapter_kind = adapter_kind_from_runtime(str(candidate["harness_kind"]))
                except StorageRefusal:
                    generation = ""
                    adapter_kind = ""
                exact = (
                    agent.get("agent") == adapter_kind
                    and agent.get("pane_id") == candidate.get("endpoint")
                    and agent.get("name") == candidate.get("routing_name")
                    and session_ref == candidate.get("session_ref")
                    and generation == candidate.get("runtime_generation")
                    and agent.get("agent_status") in self.LIVE_STATUSES
                )
                results[assignment_id] = {
                    "state": "live" if exact else "mismatch",
                    "fingerprint": hashlib.sha256(
                        json.dumps(
                            {
                                "pane": agent.get("pane_id"),
                                "route": agent.get("name"),
                                "session": session_ref,
                                "generation": generation,
                                "status": agent.get("agent_status"),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                }
        return results


class CallableMultiplexerRunner:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]]) -> None:
        self.runner = runner

    def run(
        self, arguments: Any, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        return self.runner(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )


class HerdrWakeAdapter:
    """Compatibility facade over the registered multiplexer delivery transport."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.multiplexers = builtin_multiplexer_adapter_registry(
            herdr_runner=CallableMultiplexerRunner(runner),
            herdr_binary="herdr",
        )

    def send(self, binding: dict[str, Any], envelope: dict[str, Any]) -> None:
        routing_target = binding.get("routing_name") or binding.get("endpoint")
        backend_kind = binding.get("backend_kind")
        if not isinstance(backend_kind, str) or not backend_kind or not routing_target:
            raise SupervisorUnavailable("verified Shotcaller wake endpoint is unavailable")
        summary = " ".join(str(envelope.get("summary", "")).split())
        try:
            multiplexer = self.multiplexers.adapter(backend_kind)
            if "delivery" not in multiplexer.capabilities:
                raise StorageRefusal(
                    "multiplexer_delivery_unsupported",
                    "selected multiplexer has no delivery transport",
                )
            multiplexer.delivery(
                str(routing_target),
                (
                    f"CHAMPION TRANSITION [{envelope['event_id']}] "
                    f"{envelope.get('status')}: {summary}"
                ),
            )
        except (OSError, subprocess.SubprocessError, StorageRefusal) as exc:
            raise SupervisorUnavailable("verified Shotcaller wake failed") from exc


def _json_line(value: dict[str, Any]) -> bytes:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise SupervisorUnavailable("supervisor message exceeds its byte bound")
    return encoded + b"\n"


def _now() -> datetime:
    return datetime.now().astimezone()


def _at(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="microseconds")


def _socket_path(state_root: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(state_root)).hexdigest()[:24]
    return Path("/tmp") / f"league-supervisor-{digest}.sock"


def _remove_owned_socket(path: Path) -> None:
    if not path.exists():
        return
    observed = path.lstat()
    if not stat.S_ISSOCK(observed.st_mode) or observed.st_uid != os.getuid():
        raise StorageRefusal(
            "supervisor_socket_unsafe",
            "supervisor socket path is not an owned local socket",
        )
    path.unlink()


def _locator(path: Path) -> str:
    return f"unix:{path}"


def supervisor_wake_locator(state_root: Path) -> str:
    """Return the one supported local wake locator for canonical state."""

    return _locator(_socket_path(state_root.resolve()))


def _stop_control_bindings(
    states: tuple[Mapping[str, Any], ...],
) -> list[dict[str, Any]]:
    return [
        {
            "actor_agent_id": str(state["actor_agent_id"]),
            "watcher_id": str(state["watcher_id"]),
            "fence": int(state["fence"]),
            "runtime_instance_id": str(state["runtime_instance_id"]),
            "runtime_generation": str(state["runtime_generation"]),
        }
        for state in sorted(states, key=lambda item: str(item["actor_agent_id"]))
    ]


def _path_from_locator(locator: str) -> Path:
    if not locator.startswith("unix:"):
        raise SupervisorUnavailable("watcher locator is not event-driven")
    path = Path(locator.removeprefix("unix:"))
    supported_name = path.name == SOCKET_NAME or (
        path.parent == Path("/tmp")
        and path.name.startswith("league-supervisor-")
        and path.name.endswith(".sock")
    )
    if not path.is_absolute() or not supported_name:
        raise SupervisorUnavailable("watcher locator is outside the supported service boundary")
    return path


def _peer_is_same_user(connection: socket.socket) -> bool:
    if hasattr(connection, "getpeereid"):
        uid, _ = connection.getpeereid()  # type: ignore[attr-defined]
        return uid == os.getuid()
    peer_credentials = getattr(socket, "SO_PEERCRED", None)
    if peer_credentials is not None:
        raw = connection.getsockopt(socket.SOL_SOCKET, peer_credentials, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", raw)
        return uid == os.getuid()
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        peer_uid = ctypes.c_uint()
        peer_gid = ctypes.c_uint()
        result = libc.getpeereid(
            connection.fileno(), ctypes.byref(peer_uid), ctypes.byref(peer_gid)
        )
    except (AttributeError, OSError):
        return False
    return result == 0 and peer_uid.value == os.getuid()


def send_supervisor_message(
    locator: str,
    message: dict[str, Any],
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    path = _path_from_locator(locator)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_seconds)
    try:
        client.connect(os.fspath(path))
        client.sendall(_json_line(message))
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            chunk = client.recv(min(4096, MAX_MESSAGE_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_MESSAGE_BYTES:
                raise SupervisorUnavailable("supervisor reply exceeds its byte bound")
    except (OSError, TimeoutError) as exc:
        raise SupervisorUnavailable("persistent supervisor is unavailable") from exc
    finally:
        client.close()
    try:
        response = json.loads(bytes(chunks))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SupervisorUnavailable("persistent supervisor returned a malformed reply") from exc
    if not isinstance(response, dict):
        raise SupervisorUnavailable("persistent supervisor refused the exact wake")
    if response.get("ok") is not True:
        if response.get("error") == "storage_refusal" and isinstance(
            response.get("code"), str
        ):
            raise StorageRefusal(
                str(response["code"]),
                "persistent supervisor refused the canonical operation",
                retryable=response.get("retryable") is True,
            )
        raise SupervisorUnavailable("persistent supervisor refused the exact wake")
    return response


def notify_user_message(store: Any, actor_agent_id: str, prompt_id: str) -> bool:
    target = store.delivery_target(actor_agent_id, _at())
    if target is None or target.get("channel") != "watcher":
        return False
    locator = str(target.get("locator", ""))
    if not locator.startswith("unix:"):
        return False
    try:
        response = send_supervisor_message(
            locator,
            {
                "kind": "user-message",
                "actor_agent_id": actor_agent_id,
                "prompt_id": prompt_id,
                "fence": target["fence"],
                "runtime_generation": target["generation"],
            },
            timeout_seconds=0.25,
        )
    except (SupervisorUnavailable, StorageRefusal):
        return False
    return response.get("priority") == "user"


def handoff_transition_delivery(
    store: Any,
    *,
    outbox_id: str,
    event_id: str,
    recipient_agent_id: str,
    at: str,
) -> dict[str, Any]:
    """Notify the exact supervisor; never deliver from the Champion command."""

    target = store.delivery_target(recipient_agent_id, at)
    pending = {
        "outbox_id": outbox_id,
        "event_id": event_id,
        "recipient_agent_id": recipient_agent_id,
        "state": "pending",
        "reason": "supervisor_unavailable",
    }
    if target is None or target.get("channel") != "watcher":
        return pending
    locator = str(target.get("locator", ""))
    if locator.startswith("sqlite-supervise:"):
        # Compatibility facade for the bounded legacy watcher. Delivery still
        # belongs to the watcher path, never to the provider-facing direct path.
        from .canonical_delivery import dispatch_event

        return dispatch_event(
            store,
            outbox_id=outbox_id,
            event_id=event_id,
            recipient_agent_id=recipient_agent_id,
            at=at,
        )
    if not locator.startswith("unix:"):
        return pending
    try:
        response = send_supervisor_message(
            locator,
            {
                "kind": "outbox-ready",
                "outbox_id": outbox_id,
                "event_id": event_id,
                "recipient_agent_id": recipient_agent_id,
                "fence": target["fence"],
                "runtime_generation": target["generation"],
            },
            timeout_seconds=0.5,
        )
    except (SupervisorUnavailable, StorageRefusal):
        return pending
    return {
        "outbox_id": outbox_id,
        "event_id": event_id,
        "recipient_agent_id": recipient_agent_id,
        "state": "scheduled",
        "reason": "supervisor_notified",
        "fence": response["fence"],
    }


class PersistentSupervisor:
    """One renewable, fenced service owner for one canonical state root."""

    def __init__(
        self,
        state_root: Path,
        *,
        callsign: str | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        renew_seconds: float = DEFAULT_RENEW_SECONDS,
        recovery_seconds: float = DEFAULT_RECOVERY_SECONDS,
        wake_adapter: WakeAdapter | None = None,
        delivery_adapter: DeliveryAdapter | None = None,
        recovery_adapter: SemanticRecoveryAdapter | None = None,
        runtime_observer: RuntimeObservationAdapter | None = None,
        store_factory: Callable[[Path], Any] = SQLiteStorage,
        max_accepted_work: int = MAX_ACCEPTED_WORK,
    ) -> None:
        if (
            lease_seconds <= 0
            or renew_seconds <= 0
            or renew_seconds >= lease_seconds
            or recovery_seconds <= 0
        ):
            raise StorageRefusal(
                "invalid_supervisor_lease",
                "lease, renewal, and recovery intervals must be positive and renewal must be shorter than the lease",
            )
        if not 1 <= max_accepted_work <= 1_024:
            raise StorageRefusal(
                "invalid_supervisor_capacity",
                "accepted supervisor work must have a positive bounded capacity",
            )
        self.state_root = state_root.resolve()
        self.callsign = callsign
        self.lease_seconds = lease_seconds
        self.renew_seconds = renew_seconds
        self.recovery_seconds = recovery_seconds
        self.wake_adapter = wake_adapter or HerdrWakeAdapter()
        self.delivery_adapter = delivery_adapter
        self.recovery_adapter = recovery_adapter
        self.runtime_observer = runtime_observer or HerdrRuntimeObservationAdapter()
        self.store_factory = store_factory
        self.socket_path = _socket_path(self.state_root)
        self.lock_path = self.state_root / LOCK_NAME
        self.stop_requested = threading.Event()
        self.ready = threading.Event()
        self._fence_lock = threading.Lock()
        self._fence = 0
        self._binding: dict[str, Any] = {}
        self._watcher_id = ""
        self._bindings: dict[str, dict[str, Any]] = {}
        self.registration_receipt: dict[str, Any] | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._work_slots = threading.BoundedSemaphore(max_accepted_work)
        self._priority_lock = threading.Lock()
        self._user_priority_generation = 0
        self.user_priority = threading.Event()
        self._monitor_lock = threading.Lock()
        self._runtime_suspicions: dict[str, tuple[str, float]] = {}
        self._next_runtime_observation = 0.0
        self._runtime_observation_running = False

    @property
    def user_priority_generation(self) -> int:
        with self._priority_lock:
            return self._user_priority_generation

    def _publish_user_priority(self, actor_agent_id: str) -> None:
        with self._fence_lock:
            state = self._bindings.get(actor_agent_id)
            if state is None:
                raise SupervisorUnavailable(
                    "user priority does not name an active Shotcaller binding"
                )
            with self._priority_lock:
                state["user_priority_generation"] = (
                    int(state.get("user_priority_generation", 0)) + 1
                )
                self._user_priority_generation += 1
                self.user_priority.set()

    def _submit(self, function: Callable[..., Any], *args: Any) -> bool:
        executor = self._executor
        if executor is None or not self._work_slots.acquire(blocking=False):
            return False

        def guarded() -> None:
            try:
                function(*args)
            finally:
                self._work_slots.release()

        try:
            executor.submit(guarded)
        except RuntimeError:
            self._work_slots.release()
            return False
        return True

    def _lease_expiry(self) -> str:
        return _at(_now() + timedelta(seconds=self.lease_seconds))

    def _register_binding(
        self, store: Any, binding: dict[str, Any]
    ) -> dict[str, Any]:
        actor_agent_id = str(binding["actor_agent_id"])
        existing = store.watcher_registration(actor_agent_id)
        previous = self._bindings.get(actor_agent_id)
        watcher_digest = hashlib.sha256(
            f"{actor_agent_id}\0{self.state_root}".encode("utf-8")
        ).hexdigest()[:24]
        watcher_id = f"watcher:persistent:{watcher_digest}"
        same_binding = bool(
            previous is not None
            and all(
                previous.get(key) == binding.get(key)
                for key in (
                    "scope_id",
                    "squad_id",
                    "runtime_instance_id",
                    "runtime_generation",
                    "endpoint",
                    "session_ref",
                )
            )
        )
        if previous is None:
            fence = max(
                0 if existing is None else int(existing["fence"]),
                int(binding.get("fence_floor", 0)),
            ) + 1
        elif same_binding:
            fence = int(previous["fence"])
        else:
            fence = max(
                int(previous["fence"]), int(binding.get("fence_floor", 0))
            ) + 1
        receipt = store.register_watcher(
            binding["scope_id"],
            watcher_id,
            actor_agent_id,
            binding["runtime_instance_id"],
            _locator(self.socket_path),
            self._lease_expiry(),
            fence,
            _at(),
            block_on_obligations=True,
            expected_watcher_id=(
                None if previous is None else str(previous["watcher_id"])
            ),
            expected_fence=(
                None if previous is None else int(previous["fence"])
            ),
        )
        return {
            **binding,
            "watcher_id": watcher_id,
            "fence": fence,
            "receipt": receipt,
            "user_priority_generation": (
                0
                if previous is None
                else int(previous.get("user_priority_generation", 0))
            ),
        }

    @staticmethod
    def _release_removed_bindings(
        store: Any, states: tuple[dict[str, Any], ...]
    ) -> None:
        for state in states:
            try:
                store.release_watcher(
                    str(state["watcher_id"]),
                    str(state["actor_agent_id"]),
                    int(state["fence"]),
                    _at(),
                )
            except StorageRefusal as exc:
                if exc.code != "watcher_fenced":
                    raise

    def _register(self) -> dict[str, Any]:
        with self.store_factory(self.state_root) as store:
            bindings = (
                (store.supervisor_binding(self.callsign),)
                if self.callsign is not None
                else store.supervisor_bindings()
            )
            if not bindings:
                raise StorageRefusal(
                    "supervisor_binding_invalid",
                    "persistent supervision requires at least one active Squad Shotcaller",
                )
            receipts: list[dict[str, Any]] = []
            next_states: dict[str, dict[str, Any]] = {}
            with self._fence_lock:
                for binding in bindings:
                    state = self._register_binding(store, binding)
                    actor_agent_id = str(state["actor_agent_id"])
                    next_states[actor_agent_id] = state
                    receipts.append(state["receipt"])
                removed = tuple(
                    state
                    for actor_id, state in self._bindings.items()
                    if actor_id not in next_states
                )
                self._bindings = next_states
                primary = sorted(
                    next_states.values(), key=lambda item: (item["callsign"], item["actor_agent_id"])
                )[0]
                self._binding = primary
                self._watcher_id = str(primary["watcher_id"])
                self._fence = int(primary["fence"])
            self._release_removed_bindings(store, removed)
            if len(receipts) == 1:
                return receipts[0]
            return {
                "schema": "league.supervisor-service-status.v1",
                "binding_count": len(receipts),
                "bindings": receipts,
                "idempotent": all(receipt["idempotent"] for receipt in receipts),
            }

    def _binding_state(self, actor_agent_id: str | None) -> dict[str, Any]:
        with self._fence_lock:
            if actor_agent_id is not None:
                state = self._bindings.get(actor_agent_id)
                if state is not None:
                    return dict(state)
            if len(self._bindings) == 1:
                return dict(next(iter(self._bindings.values())))
        raise SupervisorUnavailable("supervisor message does not name an active Shotcaller binding")

    def _assert_fenced_registration(
        self, store: Any, state: dict[str, Any], fence: int
    ) -> None:
        registrations = store.watcher_registrations(
            (str(state["actor_agent_id"]),)
        )
        registration = registrations.get(str(state["actor_agent_id"]))
        self._validate_fenced_registration(state, fence, registration)

    def _validate_fenced_registration(
        self,
        state: dict[str, Any],
        fence: int,
        registration: dict[str, Any] | None,
    ) -> None:
        if (
            registration is None
            or registration["watcher_id"] != state["watcher_id"]
            or int(registration["fence"]) != fence
            or registration["runtime_instance_id"]
            != state["runtime_instance_id"]
            or registration["runtime_generation"]
            != state["runtime_generation"]
            or registration["wake_locator"] != _locator(self.socket_path)
            or datetime.fromisoformat(str(registration["leased_until"])) <= _now()
        ):
            raise StorageRefusal(
                "watcher_fenced",
                "persistent supervisor no longer owns the exact live watcher lease",
            )

    def _assert_fenced_registrations(
        self, store: Any, states: tuple[dict[str, Any], ...]
    ) -> None:
        registrations = store.watcher_registrations(
            tuple(str(state["actor_agent_id"]) for state in states)
        )
        for state in states:
            self._validate_fenced_registration(
                state,
                int(state["fence"]),
                registrations.get(str(state["actor_agent_id"])),
            )

    def _release(self) -> None:
        with self._fence_lock:
            states = tuple(dict(state) for state in self._bindings.values())
        with self.store_factory(self.state_root) as store:
            for state in states:
                try:
                    store.release_watcher(
                        str(state["watcher_id"]),
                        str(state["actor_agent_id"]),
                        int(state["fence"]),
                        _at(),
                    )
                except StorageRefusal as exc:
                    if exc.code != "watcher_fenced":
                        raise

    def _response(self, connection: socket.socket, value: dict[str, Any]) -> None:
        try:
            connection.sendall(_json_line(value))
        finally:
            connection.close()

    def _dispatch_message(
        self, connection: socket.socket, message: dict[str, Any]
    ) -> None:
        kind = message.get("kind")
        if kind == "stop":
            with self._fence_lock:
                states = tuple(dict(state) for state in self._bindings.values())
            expected = _stop_control_bindings(states)
            if not states or message.get("bindings") != expected:
                raise SupervisorUnavailable(
                    "supervisor stop control identity is stale or incomplete"
                )
            with self.store_factory(self.state_root) as store:
                self._assert_fenced_registrations(store, states)
            with self._fence_lock:
                current = tuple(dict(state) for state in self._bindings.values())
                if _stop_control_bindings(current) != expected:
                    raise SupervisorUnavailable(
                        "supervisor stop control identity changed during validation"
                    )
                self.stop_requested.set()
            self._response(connection, {"ok": True, "stopping": True})
            return
        if kind == "service-ping":
            with self._fence_lock:
                states = tuple(
                    dict(state)
                    for state in sorted(
                        self._bindings.values(),
                        key=lambda item: (item["squad_id"], item["actor_agent_id"]),
                    )
                )
            with self.store_factory(self.state_root) as store:
                self._assert_fenced_registrations(store, states)
            self._response(
                connection,
                {
                    "ok": True,
                    "schema": "league.supervisor-service-status.v1",
                    "live": True,
                    "event_driven": True,
                    "binding_count": len(states),
                    "bindings": [
                        {
                            "callsign": state["callsign"],
                            "actor_agent_id": state["actor_agent_id"],
                            "squad_id": state["squad_id"],
                            "fence": state["fence"],
                            "notification_policy": state["receipt"].get(
                                "mode", "all_material"
                            ),
                            "attachment_mode": state["receipt"].get(
                                "attachment_mode", "attached"
                            ),
                            "live": True,
                            "monitor_live": True,
                        }
                        for state in states
                    ],
                    "monitor_live": True,
                },
            )
            return
        actor_agent_id = message.get("actor_agent_id")
        if kind == "champion-event" and isinstance(message.get("envelope"), dict):
            actor_agent_id = message["envelope"].get("recipient_agent_id")
        if kind == "hook":
            from .canonical_watcher import (
                brokered_hook_context,
                handle_brokered_hook,
            )

            hook = message.get("hook")
            if not isinstance(hook, dict):
                raise SupervisorUnavailable("supervisor hook request is malformed")
            with self.store_factory(self.state_root) as store:
                context = brokered_hook_context(store, hook)
                if context.actor_id is None:
                    result = handle_brokered_hook(store, hook, context=context)
                    self._response(
                        connection,
                        {
                            "ok": True,
                            "hook_output": result["hook_output"],
                            "capture": None,
                            "priority": None,
                        },
                    )
                    return
                owner_agent_id = (
                    None
                    if context.actor_id is None
                    else store.supervision_owner(context.actor_id)
                )
                state = self._binding_state(owner_agent_id)
                fence = int(state["fence"])
                self._assert_fenced_registration(store, state, fence)
                result = handle_brokered_hook(store, hook, context=context)
            capture = result.get("capture")
            published_user_priority = bool(
                isinstance(capture, dict)
                and capture.get("priority_eligible") is True
            )
            if published_user_priority:
                self._publish_user_priority(str(state["actor_agent_id"]))
            self._response(
                connection,
                {
                    "ok": True,
                    "hook_output": result["hook_output"],
                    "capture": capture,
                    "priority": "user" if published_user_priority else None,
                },
            )
            if result.get("supervision_handoff") is True:
                self._submit(
                    self._recover_pending, str(state["actor_agent_id"])
                )
            if (
                isinstance(capture, dict)
                and capture.get("state") == "quarantined"
                and isinstance(capture.get("prompt_id"), str)
            ):
                self._schedule_semantic_recovery((str(capture["prompt_id"]),))
            return
        state = self._binding_state(
            actor_agent_id if isinstance(actor_agent_id, str) else None
        )
        fence = int(state["fence"])
        if kind == "ping":
            self._response(
                connection,
                {
                    "ok": True,
                    "schema": "league.supervisor-status.v1",
                    "live": True,
                    "event_driven": True,
                    "callsign": state["callsign"],
                    "actor_agent_id": state["actor_agent_id"],
                    "squad_id": state["squad_id"],
                    "fence": fence,
                    "runtime_instance_id": state["runtime_instance_id"],
                    "runtime_generation": state["runtime_generation"],
                    "endpoint": state["endpoint"],
                    "session_ref": state["session_ref"],
                    "user_priority_generation": int(
                        state.get("user_priority_generation", 0)
                    ),
                },
            )
            return
        if kind == "reconcile-restored-runtime":
            if message.get("fence") != fence:
                raise SupervisorUnavailable("supervisor restore fence is stale")
            required = {
                "actor_agent_id",
                "runtime_instance_id",
                "runtime_generation",
                "endpoint",
                "session_ref",
            }
            if any(
                not isinstance(message.get(key), str) or not message[key]
                for key in required
            ):
                raise SupervisorUnavailable("supervisor restore identity is incomplete")
            if message["actor_agent_id"] != state["actor_agent_id"]:
                raise SupervisorUnavailable("supervisor restore owner is not exact")
            with self.store_factory(self.state_root) as store:
                binding = store.supervisor_binding(str(state["callsign"]))
                if any(binding[key] != message[key] for key in required):
                    raise SupervisorUnavailable(
                        "supervisor restore identity is not canonical"
                    )
                with self._fence_lock:
                    current = self._bindings.get(str(state["actor_agent_id"]))
                    if current is None or int(current["fence"]) != fence:
                        raise SupervisorUnavailable(
                            "supervisor restore binding changed concurrently"
                        )
                    restored = self._register_binding(store, binding)
                    self._bindings[str(restored["actor_agent_id"])] = restored
                    primary = sorted(
                        self._bindings.values(),
                        key=lambda item: (item["callsign"], item["actor_agent_id"]),
                    )[0]
                    self._binding = primary
                    self._watcher_id = str(primary["watcher_id"])
                    self._fence = int(primary["fence"])
            self._response(
                connection,
                {
                    "ok": True,
                    "schema": "league.supervisor-restore-binding.v1",
                    "callsign": restored["callsign"],
                    "actor_agent_id": restored["actor_agent_id"],
                    "runtime_instance_id": restored["runtime_instance_id"],
                    "runtime_generation": restored["runtime_generation"],
                    "endpoint": restored["endpoint"],
                    "session_ref": restored["session_ref"],
                    "fence": restored["fence"],
                    "watcher_id": restored["watcher_id"],
                    "idempotent": False,
                },
            )
            return
        if (
            message.get("fence") != fence
            or message.get("runtime_generation") != state["runtime_generation"]
        ):
            raise SupervisorUnavailable("supervisor wake identity is stale")
        if kind == "user-message":
            with self.store_factory(self.state_root) as store:
                self._assert_fenced_registration(store, state, fence)
            self._publish_user_priority(str(state["actor_agent_id"]))
            self._response(connection, {"ok": True, "priority": "user", "fence": fence})
            return
        if kind == "outbox-ready":
            if (
                message.get("recipient_agent_id") != state["actor_agent_id"]
                or not all(
                    isinstance(message.get(key), str) and message[key]
                    for key in ("outbox_id", "event_id")
                )
            ):
                raise SupervisorUnavailable("supervisor outbox identity is invalid")
            if not self._submit(
                self._recover_outbox,
                str(message["outbox_id"]),
                str(message["event_id"]),
                str(message["recipient_agent_id"]),
            ):
                raise SupervisorUnavailable("supervisor delivery capacity is unavailable")
            self._response(
                connection,
                {
                    "ok": True,
                    "scheduled": True,
                    "event_id": message["event_id"],
                    "fence": fence,
                },
            )
            return
        if kind == "runtime-observation":
            self._response(
                connection,
                {"ok": True, "observation_scheduled": True, "fence": fence},
            )
            self._schedule_runtime_observation(force=True)
            return
        if kind in {
            "attach-shotcaller",
            "detach-shotcaller",
            "calm-pause",
            "calm-resume",
        }:
            attachment_mode = (
                "detached"
                if kind in {"detach-shotcaller", "calm-pause"}
                else "attached"
            )
            with self.store_factory(self.state_root) as store:
                result = store.set_supervision_attachment(
                    str(state["scope_id"]),
                    str(state["actor_agent_id"]),
                    attachment_mode,
                    _at(),
                    expected_watcher_id=str(state["watcher_id"]),
                    expected_fence=fence,
                )
            with self._fence_lock:
                current = self._bindings.get(str(state["actor_agent_id"]))
                if current is not None and int(current["fence"]) == fence:
                    current["receipt"] = {
                        **dict(current.get("receipt", {})),
                        "mode": result["notification_policy"],
                        "runtime_state": "supervising",
                        "wake_policy": (
                            "normal"
                            if result["notification_policy"] == "all_material"
                            else "calm"
                        ),
                        "attachment_mode": result["attachment_mode"],
                        "silent_reconciliation": result["silent_reconciliation"],
                    }
            self._response(connection, {"ok": True, "fence": fence, **result})
            return
        if kind != "champion-event" or not isinstance(message.get("envelope"), dict):
            raise SupervisorUnavailable("supervisor message kind is unsupported")
        with self.store_factory(self.state_root) as store:
            self._assert_fenced_registration(store, state, fence)
        self.wake_adapter.send(state, message["envelope"])
        self._response(
            connection,
            {
                "ok": True,
                "delivered": True,
                "event_id": message["envelope"].get("event_id"),
                "fence": fence,
            },
        )

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(15)
        payload = bytearray()
        try:
            if not _peer_is_same_user(connection):
                raise SupervisorUnavailable("supervisor peer identity is invalid")
            while not payload.endswith(b"\n"):
                chunk = connection.recv(min(4096, MAX_MESSAGE_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > MAX_MESSAGE_BYTES:
                    raise SupervisorUnavailable("supervisor message exceeds its byte bound")
            message = json.loads(bytes(payload))
            if not isinstance(message, dict):
                raise SupervisorUnavailable("supervisor message is malformed")
            self._dispatch_message(connection, message)
        except StorageRefusal as exc:
            self._response(
                connection,
                {
                    "ok": False,
                    "error": "storage_refusal",
                    "code": exc.code,
                    "retryable": exc.retryable,
                },
            )
        except (SupervisorUnavailable, json.JSONDecodeError, UnicodeDecodeError, OSError):
            self._response(connection, {"ok": False, "error": "supervisor_wake_refused"})
        except Exception:
            # This is the worker thread boundary: never leave a broker client
            # hanging on an unexpected adapter or malformed-message failure.
            try:
                self._response(
                    connection, {"ok": False, "error": "supervisor_internal_error"}
                )
            except OSError:
                connection.close()

    def _recover_pending(self, actor_agent_id: str | None = None) -> None:
        from .canonical_delivery import dispatch_event

        try:
            with self._fence_lock:
                active_actor_ids = set(self._bindings)
            if actor_agent_id is not None:
                active_actor_ids.intersection_update({actor_agent_id})
            with self.store_factory(self.state_root) as store:
                rows = store.pending_backlog(_at(), limit=100, per_recipient=20)
                for row in rows:
                    if row["recipient_agent_id"] not in active_actor_ids:
                        continue
                    dispatch_event(
                        store,
                        outbox_id=str(row["outbox_id"]),
                        event_id=str(row["event_id"]),
                        recipient_agent_id=str(row["recipient_agent_id"]),
                        at=_at(),
                        adapter=self.delivery_adapter,
                    )
        except (StorageRefusal, SupervisorUnavailable):
            return

    def _recover_owner_stops(self) -> None:
        """Resume post-commit owner controls for exact currently bound scopes."""

        from .owner_stop import execute_owner_stop_controls

        try:
            with self._fence_lock:
                scope_ids = tuple(
                    str(state["scope_id"])
                    for state in self._bindings.values()
                )
            with self.store_factory(self.state_root) as store:
                controls = store.pending_owner_stop_controls(scope_ids)
                if controls:
                    execute_owner_stop_controls(
                        store, controls, _at(), adapter=self.delivery_adapter
                    )
        except (StorageRefusal, SupervisorUnavailable):
            return

    def _recover_outbox(
        self, outbox_id: str, event_id: str, recipient_agent_id: str
    ) -> None:
        from .canonical_delivery import dispatch_event

        try:
            state = self._binding_state(recipient_agent_id)
            with self.store_factory(self.state_root) as store:
                self._assert_fenced_registration(store, state, int(state["fence"]))
                dispatch_event(
                    store,
                    outbox_id=outbox_id,
                    event_id=event_id,
                    recipient_agent_id=recipient_agent_id,
                    at=_at(),
                    adapter=self.delivery_adapter,
                )
        except (StorageRefusal, SupervisorUnavailable):
            return

    def _schedule_semantic_recovery(self, prompt_ids: tuple[str, ...]) -> None:
        if self.recovery_adapter is None or self._executor is None or not prompt_ids:
            return
        self._submit(self.recovery_adapter.recover, self.state_root, prompt_ids)

    def _recover_semantic_backlog(self) -> None:
        if self.recovery_adapter is None:
            return
        try:
            with self.store_factory(self.state_root) as store:
                backlog = store.semantic_recovery_backlog(limit=20)
        except StorageRefusal:
            return
        self._schedule_semantic_recovery(tuple(backlog["prompt_ids"]))

    def _record_monitor_fault(
        self,
        fault_kind: str,
        fault_key: str,
        actor_agent_id: str | None = None,
    ) -> None:
        from .canonical_delivery import dispatch_event

        with self._fence_lock:
            recipients = (
                (actor_agent_id,)
                if actor_agent_id is not None
                else tuple(sorted(self._bindings))
            )
        for recipient_agent_id in recipients:
            try:
                with self.store_factory(self.state_root) as store:
                    fault = store.record_supervision_fault(
                        recipient_agent_id, fault_kind, fault_key, _at()
                    )
                    dispatch_event(
                        store,
                        outbox_id=fault["outbox_id"],
                        event_id=fault["event_id"],
                        recipient_agent_id=fault["recipient_agent_id"],
                        at=_at(),
                        adapter=self.delivery_adapter,
                    )
            except (StorageRefusal, SupervisorUnavailable):
                continue

    def _apply_runtime_observation(
        self,
        store: Any,
        candidate: dict[str, Any],
        observation: dict[str, str] | None,
        policy: dict[str, Any],
        now: float,
        owner_agent_id: str,
    ) -> None:
        from .canonical_delivery import dispatch_event

        assignment_id = str(candidate["assignment_id"])
        if observation is None:
            self._record_monitor_fault(
                "runtime_observation_refused",
                f"missing_result:{assignment_id}",
                owner_agent_id,
            )
            return
        state = observation.get("state")
        fingerprint = str(observation.get("fingerprint", state))
        if state == "live":
            store.register_runtime(
                RuntimeRegistrationCommand(
                    runtime_instance_id=str(candidate["runtime_instance_id"]),
                    actor_agent_id=str(candidate["champion_agent_id"]),
                    harness_kind=str(candidate["harness_kind"]),
                    backend_kind=str(candidate["backend_kind"]),
                    session_ref=str(candidate["session_ref"]),
                    endpoint=str(candidate["endpoint"]),
                    runtime_generation=str(candidate["runtime_generation"]),
                    status="active",
                    verified=True,
                    at=_at(),
                )
            )
            with self._monitor_lock:
                self._runtime_suspicions.pop(assignment_id, None)
            return
        if state not in {"missing", "mismatch"}:
            self._record_monitor_fault(
                "runtime_observation_refused",
                f"invalid_result:{assignment_id}",
                owner_agent_id,
            )
            return
        with self._monitor_lock:
            prior = self._runtime_suspicions.get(assignment_id)
            if prior is None or prior[0] != fingerprint:
                self._runtime_suspicions[assignment_id] = (
                    fingerprint,
                    now + float(policy["unreachable_grace_seconds"]),
                )
                return
            if now < prior[1]:
                return
        try:
            store.register_runtime(
                RuntimeRegistrationCommand(
                    runtime_instance_id=str(candidate["runtime_instance_id"]),
                    actor_agent_id=str(candidate["champion_agent_id"]),
                    harness_kind=str(candidate["harness_kind"]),
                    backend_kind=str(candidate["backend_kind"]),
                    session_ref=str(candidate["session_ref"]),
                    endpoint=str(candidate["endpoint"]),
                    runtime_generation=str(candidate["runtime_generation"]),
                    status="failed",
                    verified=False,
                    at=_at(),
                )
            )
            reconciled = store.reconcile_assignment_runtime(assignment_id, _at())
            dispatch_event(
                store,
                outbox_id=reconciled["outbox_id"],
                event_id=reconciled["event_id"],
                recipient_agent_id=reconciled["recipient_agent_id"],
                at=_at(),
                adapter=self.delivery_adapter,
            )
        except StorageRefusal as exc:
            self._record_monitor_fault(
                "runtime_reconciliation_refused",
                f"{assignment_id}:{exc.code}",
                owner_agent_id,
            )
        finally:
            with self._monitor_lock:
                self._runtime_suspicions.pop(assignment_id, None)

    def _observe_runtime_candidates(self) -> None:
        now = time.monotonic()
        with self._fence_lock:
            owners = tuple(sorted(self._bindings))
        candidate_ids: set[str] = set()
        owner_batches: dict[
            str, tuple[tuple[dict[str, Any], ...], dict[str, Any]]
        ] = {}
        all_candidates: list[dict[str, Any]] = []
        with self.store_factory(self.state_root) as store:
            for owner_agent_id in owners:
                try:
                    batch = store.runtime_monitor_candidates(owner_agent_id, limit=50)
                    policy = store.supervision_policy(owner_agent_id)
                except StorageRefusal as exc:
                    self._record_monitor_fault(
                        "runtime_observation_refused", exc.code, owner_agent_id
                    )
                    continue
                if batch["truncated"]:
                    self._record_monitor_fault(
                        "runtime_observation_refused",
                        "candidate_inventory_truncated",
                        owner_agent_id,
                    )
                    continue
                candidates = tuple(batch["candidates"])
                owner_batches[owner_agent_id] = (candidates, policy)
                all_candidates.extend(candidates)
                candidate_ids.update(
                    str(candidate["assignment_id"]) for candidate in candidates
                )
        try:
            observations = self.runtime_observer.observe(tuple(all_candidates))
        except StorageRefusal as exc:
            for owner_agent_id in owner_batches:
                self._record_monitor_fault(
                    "runtime_observation_refused", exc.code, owner_agent_id
                )
            observations = {}
            owner_batches = {}
        with self.store_factory(self.state_root) as store:
            for owner_agent_id, (candidates, policy) in owner_batches.items():
                for candidate in candidates:
                    assignment_id = str(candidate["assignment_id"])
                    self._apply_runtime_observation(
                        store,
                        candidate,
                        observations.get(assignment_id),
                        policy,
                        now,
                        owner_agent_id,
                    )

        with self._monitor_lock:
            for assignment_id in tuple(self._runtime_suspicions):
                if assignment_id not in candidate_ids:
                    self._runtime_suspicions.pop(assignment_id, None)
            deadlines = [deadline for _, deadline in self._runtime_suspicions.values()]
            self._next_runtime_observation = (
                min(deadlines) if deadlines else now + self.recovery_seconds
            )

    def _schedule_runtime_observation(self, *, force: bool = False) -> None:
        with self._monitor_lock:
            if self._runtime_observation_running or (
                not force and time.monotonic() < self._next_runtime_observation
            ):
                return
            self._runtime_observation_running = True

        def observe() -> None:
            try:
                self._observe_runtime_candidates()
            finally:
                with self._monitor_lock:
                    self._runtime_observation_running = False

        if not self._submit(observe):
            with self._monitor_lock:
                self._runtime_observation_running = False

    def _renew_with_recovery(self) -> dict[str, Any]:
        last: StorageRefusal | None = None
        for attempt in range(MAX_RENEW_ATTEMPTS):
            try:
                return self._register()
            except StorageRefusal as exc:
                if not exc.retryable and exc.code != "busy":
                    raise
                last = exc
                if attempt + 1 < MAX_RENEW_ATTEMPTS:
                    time.sleep(min(0.05, self.renew_seconds / 4))
        raise StorageRefusal(
            "supervisor_renewal_failed",
            "persistent supervisor could not renew its fenced lease within the retry bound",
            retryable=True,
        ) from last

    def run(self, *, emit_ready: bool = True) -> int:
        self.state_root.mkdir(parents=True, exist_ok=True)
        lock = self.lock_path.open("a+")
        server: socket.socket | None = None
        acquired = False
        executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="league-supervisor")
        self._executor = executor
        previous_handlers: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda _signum, _frame: self.stop_requested.set())
        try:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError as exc:
                raise StorageRefusal(
                    "supervision_active",
                    "one persistent supervisor already owns this canonical state root",
                ) from exc
            _remove_owned_socket(self.socket_path)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(os.fspath(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            server.listen(16)
            server.settimeout(min(0.25, self.renew_seconds))
            receipt = self._register()
            self.registration_receipt = receipt
            self.ready.set()
            if emit_ready:
                with self._fence_lock:
                    states = tuple(
                        dict(state)
                        for state in sorted(
                            self._bindings.values(),
                            key=lambda item: (item["callsign"], item["actor_agent_id"]),
                        )
                    )
                if len(states) == 1:
                    state = states[0]
                    ready_payload = {
                        "schema": "league.supervisor-status.v1",
                        "live": True,
                        "event_driven": True,
                        "callsign": state["callsign"],
                        "fence": state["fence"],
                        "lease_seconds": self.lease_seconds,
                        "recovery_seconds": self.recovery_seconds,
                        "mode": state["receipt"].get("mode", "all_material"),
                        "runtime_state": state["receipt"].get(
                            "runtime_state", "supervising"
                        ),
                        "wake_policy": state["receipt"].get(
                            "wake_policy", "normal"
                        ),
                        "attachment_mode": state["receipt"].get(
                            "attachment_mode", "attached"
                        ),
                        "monitor_live": True,
                        "silent_reconciliation": state["receipt"].get(
                            "silent_reconciliation"
                        ),
                    }
                else:
                    ready_payload = {
                        "schema": "league.supervisor-service-status.v1",
                        "live": True,
                        "event_driven": True,
                        "binding_count": len(states),
                        "bindings": [
                            {
                                "callsign": state["callsign"],
                                "actor_agent_id": state["actor_agent_id"],
                                "squad_id": state["squad_id"],
                                "fence": state["fence"],
                                "notification_policy": state["receipt"].get(
                                    "mode", "all_material"
                                ),
                                "attachment_mode": state["receipt"].get(
                                    "attachment_mode", "attached"
                                ),
                                "live": True,
                                "monitor_live": True,
                            }
                            for state in states
                        ],
                        "lease_seconds": self.lease_seconds,
                        "recovery_seconds": self.recovery_seconds,
                        "monitor_live": True,
                    }
                print(
                    json.dumps(
                        ready_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
            self._submit(self._recover_pending)
            self._submit(self._recover_owner_stops)
            self._submit(self._recover_semantic_backlog)
            self._schedule_runtime_observation(force=True)
            next_renewal = time.monotonic() + self.renew_seconds
            next_recovery = time.monotonic() + self.recovery_seconds
            while not self.stop_requested.is_set():
                if time.monotonic() >= next_renewal:
                    try:
                        self._renew_with_recovery()
                    except StorageRefusal as exc:
                        self._record_monitor_fault("supervisor_lease_loss", exc.code)
                        raise
                    next_renewal = time.monotonic() + self.renew_seconds
                self._schedule_runtime_observation()
                if time.monotonic() >= next_recovery:
                    self._submit(self._recover_pending)
                    self._submit(self._recover_owner_stops)
                    self._submit(self._recover_semantic_backlog)
                    next_recovery = time.monotonic() + self.recovery_seconds
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                if not self._submit(self._handle, connection):
                    self._response(
                        connection,
                        {"ok": False, "error": "supervisor_capacity_exceeded"},
                    )
            return 0
        finally:
            self.stop_requested.set()
            if server is not None:
                server.close()
            executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
            if acquired:
                self._release()
                _remove_owned_socket(self.socket_path)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def supervisor_status(state_root: Path, callsign: str | None = None) -> dict[str, Any]:
    with SQLiteStorage(state_root) as store:
        bindings = store.supervisor_bindings()
        registrations = store.watcher_registrations(
            tuple(str(binding["actor_agent_id"]) for binding in bindings)
        )
    if callsign is None:
        if not bindings:
            raise StorageRefusal(
                "supervisor_binding_invalid",
                "persistent supervision requires at least one active Squad Shotcaller",
            )
        missing = len(registrations) != len(bindings) or any(
            registration is None
            or not str(registration["wake_locator"]).startswith("unix:")
            for registration in registrations.values()
        )
        if not missing:
            first = registrations[bindings[0]["actor_agent_id"]]
            assert first is not None
            try:
                response = send_supervisor_message(
                    str(first["wake_locator"]),
                    {"kind": "service-ping"},
                    timeout_seconds=0.5,
                )
                return {key: value for key, value in response.items() if key != "ok"}
            except SupervisorUnavailable:
                pass
        reason = "registration_missing" if missing else "process_unreachable"
        return {
            "schema": "league.supervisor-service-status.v1",
            "live": False,
            "event_driven": True,
            "binding_count": len(bindings),
            "bindings": [
                {
                    "callsign": binding["callsign"],
                    "actor_agent_id": binding["actor_agent_id"],
                    "squad_id": binding["squad_id"],
                    "live": False,
                    "monitor_live": False,
                    "reason": reason,
                }
                for binding in bindings
            ],
            "monitor_live": False,
        }
    with SQLiteStorage(state_root) as store:
        binding = store.supervisor_binding(callsign)
        # Explicit callsigns may resolve through the restored-agent/source-canary
        # compatibility binding rather than the active-Squad aggregate above.
        # Read that exact owner's registration from the same canonical snapshot.
        registration = store.watcher_registration(str(binding["actor_agent_id"]))
        policy = store.supervision_policy(binding["actor_agent_id"])
    base = {
        "schema": "league.supervisor-status.v1",
        "callsign": binding["callsign"],
        "event_driven": True,
        "mode": policy["mode"],
        "runtime_state": policy["runtime_state"],
        "wake_policy": policy["wake_policy"],
        "attachment_mode": policy["attachment_mode"],
    }
    if registration is None or not str(registration["wake_locator"]).startswith("unix:"):
        return {
            **base,
            "live": False,
            "monitor_live": False,
            "reason": "registration_missing",
        }
    try:
        response = send_supervisor_message(
            str(registration["wake_locator"]),
            {"kind": "ping", "actor_agent_id": binding["actor_agent_id"]},
            timeout_seconds=0.5,
        )
    except SupervisorUnavailable:
        return {
            **base,
            "live": False,
            "monitor_live": False,
            "reason": "process_unreachable",
        }
    lease_valid = datetime.fromisoformat(str(registration["leased_until"])) > _now()
    identity_valid = (
        response.get("fence") == int(registration["fence"])
        and response.get("callsign") == binding["callsign"]
    )
    return {
        **base,
        "live": bool(lease_valid and identity_valid),
        "monitor_live": bool(lease_valid and identity_valid),
        "lease_valid": lease_valid,
        "identity_valid": identity_valid,
        "fence": int(registration["fence"]),
        "user_priority_generation": int(
            response.get("user_priority_generation", 0)
        ),
    }


def stop_supervisor(state_root: Path, callsign: str | None = None) -> dict[str, Any]:
    status = supervisor_status(state_root, callsign)
    if not status["live"]:
        raise StorageRefusal(
            "supervisor_not_live", "the exact persistent supervisor is not live"
        )
    with SQLiteStorage(state_root) as store:
        binding = (
            store.supervisor_bindings()[0]
            if status["schema"] == "league.supervisor-service-status.v1"
            else store.supervisor_binding(callsign)
        )
        registration = store.watcher_registration(str(binding["actor_agent_id"]))
    if registration is None:
        raise StorageRefusal(
            "supervisor_not_live",
            "the exact persistent supervisor binding changed before stop",
        )
    locator = str(registration["wake_locator"])
    service = send_supervisor_message(locator, {"kind": "service-ping"})
    reported = service.get("bindings")
    if not isinstance(reported, list) or not reported:
        raise StorageRefusal(
            "supervisor_not_live",
            "the persistent supervisor returned no exact stop-control bindings",
        )
    try:
        reported_callsigns = tuple(str(item["callsign"]) for item in reported)
        if len(set(reported_callsigns)) != len(reported_callsigns):
            raise TypeError
        with SQLiteStorage(state_root) as store:
            discovered = {
                str(item["callsign"]): item for item in store.supervisor_bindings()
            }
            missing = tuple(
                name for name in reported_callsigns if name not in discovered
            )
            if missing:
                if len(reported_callsigns) != 1 or callsign != reported_callsigns[0]:
                    raise TypeError
                discovered[reported_callsigns[0]] = store.supervisor_binding(callsign)
            bindings = tuple(discovered[name] for name in reported_callsigns)
            registrations = store.watcher_registrations(
                tuple(str(item["actor_agent_id"]) for item in bindings)
            )
            if len(registrations) != len(bindings):
                raise StorageRefusal(
                    "supervisor_not_live",
                    "the exact persistent supervisor binding set changed before stop",
                )
            control_states = tuple(
                {**item, **registrations[str(item["actor_agent_id"])]}
                for item in bindings
            )
    except (KeyError, TypeError) as exc:
        raise StorageRefusal(
            "supervisor_not_live",
            "the persistent supervisor stop-control identity is malformed",
        ) from exc
    send_supervisor_message(
        locator,
        {"kind": "stop", "bindings": _stop_control_bindings(control_states)},
        timeout_seconds=1,
    )
    deadline = time.monotonic() + 5
    retry_delay = 0.1
    while time.monotonic() < deadline:
        current = supervisor_status(state_root, callsign)
        if not current["live"]:
            if status["schema"] == "league.supervisor-service-status.v1":
                return {
                    "schema": "league.supervisor-service-status.v1",
                    "event_driven": True,
                    "live": False,
                    "stopped": True,
                    "binding_count": status["binding_count"],
                    "bindings": current["bindings"],
                    "monitor_live": False,
                }
            return {
                "schema": "league.supervisor-status.v1",
                "callsign": binding["callsign"],
                "event_driven": True,
                "live": False,
                "stopped": True,
            }
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(retry_delay, remaining))
            retry_delay = min(retry_delay * 2, 0.5)
    raise StorageRefusal(
        "supervisor_stop_timeout", "persistent supervisor did not stop within its bound"
    )


def set_shotcaller_attachment(
    state_root: Path,
    callsign: str | None,
    attachment_mode: str,
    *,
    deprecated_alias: str | None = None,
) -> dict[str, Any]:
    if not callsign:
        raise StorageRefusal(
            "shotcaller_required",
            "attachment changes require one exact Shotcaller callsign",
        )
    if attachment_mode not in {"attached", "detached"}:
        raise StorageRefusal(
            "supervision_attachment_invalid", "attachment mode is unsupported"
        )
    status = supervisor_status(state_root, callsign)
    if not status["live"]:
        raise StorageRefusal(
            "supervisor_not_live",
            "the OS-managed persistent watcher is not live; run the supported service-start operation before changing attachment",
        )
    with SQLiteStorage(state_root) as store:
        binding = store.supervisor_binding(callsign)
        registration = store.watcher_registration(binding["actor_agent_id"])
    assert registration is not None
    response = send_supervisor_message(
        str(registration["wake_locator"]),
        {
            "kind": (
                "detach-shotcaller"
                if attachment_mode == "detached"
                else "attach-shotcaller"
            ),
            "actor_agent_id": binding["actor_agent_id"],
            "fence": int(registration["fence"]),
            "runtime_generation": binding["runtime_generation"],
        },
        timeout_seconds=1,
    )
    result = {
        "schema": "league.supervisor-status.v1",
        "callsign": binding["callsign"],
        "event_driven": True,
        "live": True,
        "mode": response["notification_policy"],
        "runtime_state": "supervising",
        "wake_policy": (
            "normal"
            if response["notification_policy"] == "all_material"
            else "calm"
        ),
        "attachment_mode": response["attachment_mode"],
        "hooks_changed": False,
        "monitor_live": True,
        "fence": response["fence"],
        "in_flight_count": response["in_flight_count"],
        "silent_reconciliation": response["silent_reconciliation"],
    }
    if deprecated_alias is not None:
        result["deprecated_alias"] = deprecated_alias
    return result


def detach_shotcaller(
    state_root: Path, callsign: str | None = None
) -> dict[str, Any]:
    return set_shotcaller_attachment(state_root, callsign, "detached")


def attach_shotcaller(
    state_root: Path, callsign: str | None = None
) -> dict[str, Any]:
    return set_shotcaller_attachment(state_root, callsign, "attached")


def pause_supervisor(state_root: Path, callsign: str | None = None) -> dict[str, Any]:
    """Deprecated compatibility alias for ``detach-shotcaller``."""

    return set_shotcaller_attachment(
        state_root, callsign, "detached", deprecated_alias="service-pause"
    )


def resume_supervisor(state_root: Path, callsign: str | None = None) -> dict[str, Any]:
    """Deprecated compatibility alias for ``attach-shotcaller``."""

    return set_shotcaller_attachment(
        state_root, callsign, "attached", deprecated_alias="service-resume"
    )


__all__ = [
    "HerdrWakeAdapter",
    "HerdrRuntimeObservationAdapter",
    "PersistentSupervisor",
    "RuntimeObservationAdapter",
    "SemanticRecoveryAdapter",
    "SupervisorUnavailable",
    "handoff_transition_delivery",
    "notify_user_message",
    "send_supervisor_message",
    "attach_shotcaller",
    "detach_shotcaller",
    "pause_supervisor",
    "resume_supervisor",
    "set_shotcaller_attachment",
    "stop_supervisor",
    "supervisor_status",
]
