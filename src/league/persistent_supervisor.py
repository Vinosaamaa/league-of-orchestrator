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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from .sqlite_store import SQLiteStorage
from .storage import StorageRefusal


MAX_MESSAGE_BYTES = 1_100_000
MAX_ACCEPTED_WORK = 16
MAX_RENEW_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 60
DEFAULT_RENEW_SECONDS = 20
SOCKET_NAME = ".league-supervisor.sock"
LOCK_NAME = ".league-supervisor.lock"


class SupervisorUnavailable(RuntimeError):
    """The exact persistent supervisor could not accept a wake."""


class WakeAdapter(Protocol):
    def send(self, binding: dict[str, Any], envelope: dict[str, Any]) -> None: ...


class SemanticRecoveryAdapter(Protocol):
    def recover(self, state_root: Path, prompt_ids: tuple[str, ...]) -> None: ...


class HerdrWakeAdapter:
    """Wake the verified Shotcaller only after the service owns the event."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.runner = runner

    def send(self, binding: dict[str, Any], envelope: dict[str, Any]) -> None:
        routing_target = binding.get("routing_name") or binding.get("endpoint")
        if binding.get("backend_kind") != "herdr" or not routing_target:
            raise SupervisorUnavailable("verified Shotcaller wake endpoint is unavailable")
        command = ["herdr"]
        if os.environ.get("HERDR_SESSION"):
            command.extend(("--session", os.environ["HERDR_SESSION"]))
        summary = " ".join(str(envelope.get("summary", "")).split())
        command.extend(
            (
                "agent",
                "prompt",
                str(routing_target),
                (
                    f"CHAMPION TRANSITION [{envelope['event_id']}] "
                    f"{envelope.get('status')}: {summary}"
                ),
            )
        )
        try:
            completed = self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SupervisorUnavailable("verified Shotcaller wake failed") from exc
        if completed.returncode != 0 or completed.stdout or completed.stderr:
            raise SupervisorUnavailable("verified Shotcaller wake failed")


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
                "prompt_id": prompt_id,
                "fence": target["fence"],
                "runtime_generation": target["generation"],
            },
            timeout_seconds=0.25,
        )
    except SupervisorUnavailable:
        return False
    return response.get("priority") == "user"


class PersistentSupervisor:
    """One renewable, fenced service owner for one canonical state root."""

    def __init__(
        self,
        state_root: Path,
        *,
        callsign: str | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        renew_seconds: float = DEFAULT_RENEW_SECONDS,
        wake_adapter: WakeAdapter | None = None,
        recovery_adapter: SemanticRecoveryAdapter | None = None,
        store_factory: Callable[[Path], Any] = SQLiteStorage,
        max_accepted_work: int = MAX_ACCEPTED_WORK,
    ) -> None:
        if lease_seconds <= 0 or renew_seconds <= 0 or renew_seconds >= lease_seconds:
            raise StorageRefusal(
                "invalid_supervisor_lease",
                "renew interval must be positive and shorter than the watcher lease",
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
        self.wake_adapter = wake_adapter or HerdrWakeAdapter()
        self.recovery_adapter = recovery_adapter
        self.store_factory = store_factory
        self.socket_path = _socket_path(self.state_root)
        self.lock_path = self.state_root / LOCK_NAME
        self.stop_requested = threading.Event()
        self.ready = threading.Event()
        self._fence_lock = threading.Lock()
        self._fence = 0
        self._binding: dict[str, Any] = {}
        self._watcher_id = ""
        self._executor: ThreadPoolExecutor | None = None
        self._work_slots = threading.BoundedSemaphore(max_accepted_work)
        self._priority_lock = threading.Lock()
        self._user_priority_generation = 0
        self.user_priority = threading.Event()

    @property
    def user_priority_generation(self) -> int:
        with self._priority_lock:
            return self._user_priority_generation

    def _publish_user_priority(self) -> None:
        with self._priority_lock:
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

    def _register(self) -> dict[str, Any]:
        with self.store_factory(self.state_root) as store:
            binding = store.supervisor_binding(self.callsign)
            existing = store.watcher_registration(binding["actor_agent_id"])
            with self._fence_lock:
                self._fence = max(
                    self._fence,
                    0 if existing is None else int(existing["fence"]),
                ) + 1
                fence = self._fence
            watcher_digest = hashlib.sha256(
                f"{binding['actor_agent_id']}\0{self.state_root}".encode("utf-8")
            ).hexdigest()[:24]
            self._watcher_id = f"watcher:persistent:{watcher_digest}"
            receipt = store.register_watcher(
                binding["scope_id"],
                self._watcher_id,
                binding["actor_agent_id"],
                binding["runtime_instance_id"],
                _locator(self.socket_path),
                self._lease_expiry(),
                fence,
                _at(),
                block_on_obligations=True,
            )
            self._binding = binding
            return receipt

    def _release(self) -> None:
        if not self._binding or not self._watcher_id:
            return
        with self._fence_lock:
            fence = self._fence
        try:
            with self.store_factory(self.state_root) as store:
                store.release_watcher(
                    self._watcher_id,
                    self._binding["actor_agent_id"],
                    fence,
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
            with self._fence_lock:
                fence = self._fence
            kind = message.get("kind")
            if kind == "hook":
                from .canonical_watcher import handle_brokered_hook

                hook = message.get("hook")
                if not isinstance(hook, dict):
                    raise SupervisorUnavailable("supervisor hook request is malformed")
                with self.store_factory(self.state_root) as store:
                    result = handle_brokered_hook(store, hook)
                capture = result.get("capture")
                published_user_priority = bool(
                    isinstance(capture, dict)
                    and capture.get("owned_by_shotcaller") is True
                    and isinstance(capture.get("prompt_id"), str)
                    and capture.get("idempotent") is False
                    and capture.get("suppressed") is None
                )
                if published_user_priority:
                    self._publish_user_priority()
                self._response(
                    connection,
                    {
                        "ok": True,
                        "hook_output": result["hook_output"],
                        "capture": capture,
                        "priority": "user" if published_user_priority else None,
                    },
                )
                if (
                    isinstance(capture, dict)
                    and capture.get("state") == "quarantined"
                    and isinstance(capture.get("prompt_id"), str)
                ):
                    self._schedule_semantic_recovery((str(capture["prompt_id"]),))
                return
            if kind == "ping":
                self._response(
                    connection,
                    {
                        "ok": True,
                        "schema": "league.supervisor-status.v1",
                        "live": True,
                        "event_driven": True,
                        "callsign": self._binding["callsign"],
                        "fence": fence,
                    },
                )
                return
            if kind == "stop":
                self._response(
                    connection,
                    {"ok": True, "stopping": True, "fence": fence},
                )
                self.stop_requested.set()
                return
            if (
                message.get("fence") != fence
                or message.get("runtime_generation")
                != self._binding["runtime_generation"]
            ):
                raise SupervisorUnavailable("supervisor wake identity is stale")
            if kind == "user-message":
                self._publish_user_priority()
                self._response(
                    connection,
                    {"ok": True, "priority": "user", "fence": fence},
                )
                return
            if kind != "champion-event" or not isinstance(message.get("envelope"), dict):
                raise SupervisorUnavailable("supervisor message kind is unsupported")
            self.wake_adapter.send(self._binding, message["envelope"])
            self._response(
                connection,
                {
                    "ok": True,
                    "delivered": True,
                    "event_id": message["envelope"].get("event_id"),
                    "fence": fence,
                },
            )
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

    def _recover_pending(self) -> None:
        from .canonical_delivery import dispatch_event

        try:
            with self.store_factory(self.state_root) as store:
                rows = store.pending_backlog(_at(), limit=100, per_recipient=20)
            for row in rows:
                if row["recipient_agent_id"] != self._binding["actor_agent_id"]:
                    continue
                with self.store_factory(self.state_root) as store:
                    dispatch_event(
                        store,
                        outbox_id=str(row["outbox_id"]),
                        event_id=str(row["event_id"]),
                        recipient_agent_id=str(row["recipient_agent_id"]),
                        at=_at(),
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
            self.ready.set()
            if emit_ready:
                print(
                    json.dumps(
                        {
                            "schema": "league.supervisor-status.v1",
                            "live": True,
                            "event_driven": True,
                            "callsign": self._binding["callsign"],
                            "fence": receipt["fence"],
                            "lease_seconds": self.lease_seconds,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
            self._submit(self._recover_pending)
            self._submit(self._recover_semantic_backlog)
            next_renewal = time.monotonic() + self.renew_seconds
            while not self.stop_requested.is_set():
                if time.monotonic() >= next_renewal:
                    self._renew_with_recovery()
                    next_renewal = time.monotonic() + self.renew_seconds
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
        binding = store.supervisor_binding(callsign)
        registration = store.watcher_registration(binding["actor_agent_id"])
    base = {
        "schema": "league.supervisor-status.v1",
        "callsign": binding["callsign"],
        "event_driven": True,
    }
    if registration is None or not str(registration["wake_locator"]).startswith("unix:"):
        return {**base, "live": False, "reason": "registration_missing"}
    try:
        response = send_supervisor_message(
            str(registration["wake_locator"]), {"kind": "ping"}, timeout_seconds=0.5
        )
    except SupervisorUnavailable:
        return {**base, "live": False, "reason": "process_unreachable"}
    lease_valid = datetime.fromisoformat(str(registration["leased_until"])) > _now()
    identity_valid = (
        response.get("fence") == int(registration["fence"])
        and response.get("callsign") == binding["callsign"]
    )
    return {
        **base,
        "live": bool(lease_valid and identity_valid),
        "lease_valid": lease_valid,
        "identity_valid": identity_valid,
        "fence": int(registration["fence"]),
    }


def stop_supervisor(state_root: Path, callsign: str | None = None) -> dict[str, Any]:
    status = supervisor_status(state_root, callsign)
    if not status["live"]:
        raise StorageRefusal(
            "supervisor_not_live", "the exact persistent supervisor is not live"
        )
    with SQLiteStorage(state_root) as store:
        binding = store.supervisor_binding(callsign)
        registration = store.watcher_registration(binding["actor_agent_id"])
    assert registration is not None
    send_supervisor_message(
        str(registration["wake_locator"]), {"kind": "stop"}, timeout_seconds=1
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = supervisor_status(state_root, callsign)
        if not current["live"]:
            return {
                "schema": "league.supervisor-status.v1",
                "callsign": binding["callsign"],
                "live": False,
                "stopped": True,
            }
        time.sleep(0.02)
    raise StorageRefusal(
        "supervisor_stop_timeout", "persistent supervisor did not stop within its bound"
    )


__all__ = [
    "HerdrWakeAdapter",
    "PersistentSupervisor",
    "SemanticRecoveryAdapter",
    "SupervisorUnavailable",
    "notify_user_message",
    "send_supervisor_message",
    "stop_supervisor",
    "supervisor_status",
]
