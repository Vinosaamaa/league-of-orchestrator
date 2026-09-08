"""One event-driven classifier lane, independent of Shotcaller provider support."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable, Protocol

from .sqlite_store import SQLiteStorage
from .storage import StorageRefusal
from . import sqlite_prompt_triage_ops as ops


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Classifier(Protocol):
    def classify(self, value: dict[str, Any], cancelled: Callable[[], bool]) -> dict[str, Any]: ...
    def close(self) -> None: ...


class PromptTriageWorker:
    """The supervisor's root lock owns exactly one of these dedicated lanes.

    The model has no SQLite connection and never executes the requested work.
    No database transaction is held during inference. The backend is created
    lazily, reused across prompts, and makes no model calls while idle.
    """

    def __init__(self, state_root: Path, factory: Callable[[], Classifier],
                 delivered: Callable[[dict[str, Any]], None], *,
                 store_factory: Callable[[Path], Any] = SQLiteStorage) -> None:
        self.state_root = state_root
        self.factory = factory
        self.delivered = delivered
        self.store_factory = store_factory
        self._backend: Classifier | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def _close_backend(self) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                backend.close()
            except (OSError, ValueError, StorageRefusal):
                self.last_error = 'triage_backend_close_failed'

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("triage worker already started")
        self._thread = threading.Thread(target=self._run, name="league-prompt-triage", daemon=True)
        self._thread.start()
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def _cancelled(self, frame: dict[str, Any]) -> bool:
        if self._stop.is_set():
            return True
        with self.store_factory(self.state_root) as store:
            current = ops.policy(store, frame["owner_agent_id"])
        return current["mode"] != "background" or current["version"] != frame["policy_version"]

    def process_one(self) -> bool:
        with self.store_factory(self.state_root) as store:
            frame = ops.next_input(store)
        if frame is None:
            return False
        try:
            if self._cancelled(frame):
                return True
            if self._backend is None:
                self._backend = self.factory()
            started = time.monotonic()
            output = self._backend.classify(ops.compact_input(frame), lambda: self._cancelled(frame))
            with self.store_factory(self.state_root) as store:
                receipt = ops.commit(store, frame, output, _now(), metrics={
                    'classification_seconds': time.monotonic() - started,
                    'usage': getattr(self._backend, 'last_usage', {}),
                })
        except (StorageRefusal, OSError, ValueError, TimeoutError) as exc:
            self._close_backend()
            code = exc.code if isinstance(exc, StorageRefusal) else "triage_backend_unavailable"
            self.last_error = code
            with self.store_factory(self.state_root) as store:
                ops.record_failure(store, frame, code, _now())
            return True
        if receipt.get("outbox_id"):
            # Failure to wake cannot undo committed requests or their outbox.
            try:
                self.delivered(receipt)
            except (StorageRefusal, OSError):
                pass
        return True

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait()
                self._wake.clear()
                try:
                    while not self._stop.is_set() and self.process_one():
                        pass
                except (StorageRefusal, OSError, ValueError) as exc:
                    # Retain work and wait for a real wake/recovery, not an idle
                    # inference retry loop or silent permanent thread death.
                    self.last_error = exc.code if isinstance(exc, StorageRefusal) else 'triage_storage_unavailable'
        finally:
            self._close_backend()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is None:
            self._close_backend()
        else:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise StorageRefusal("triage_shutdown_pending", "owned triage worker did not stop within its bound")
