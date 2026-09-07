"""Native response and bounded broker latency regressions; synthetic state only."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from league.agent_adapters.codex.hooks import translate_output
from league.canonical_watcher import _broker_hook
from league.persistent_supervisor import PersistentSupervisor


def test_codex_accept_is_native_noop():
    assert translate_output("pre_tool_authorization", {"decision": "accept"}) == {}
    denied = translate_output("pre_tool_authorization", {
        "decision": "refuse", "reason_code": "synthetic_refusal",
    })
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert denied["hookSpecificOutput"]["permissionDecisionReason"] == "synthetic_refusal"


def test_broker_accepts_briefly_delayed_reply():
    with tempfile.TemporaryDirectory(prefix="hook-latency-") as tmp:
        path = Path(tmp) / ".league-supervisor.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        failures = []

        def respond():
            try:
                connection, _ = server.accept()
                with connection:
                    request = b""
                    while not request.endswith(b"\n"):
                        request += connection.recv(4096)
                    assert json.loads(request)["kind"] == "hook"
                    time.sleep(0.65)
                    connection.sendall(b'{"ok":true,"hook_output":{}}\n')
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            with patch("league.canonical_watcher._state_root", return_value=Path(tmp)), patch(
                "league.canonical_watcher.PersistentSupervisor",
                return_value=SimpleNamespace(socket_path=path),
            ):
                started = time.monotonic()
                response = _broker_hook(SimpleNamespace(
                    command="codex-pre-tool-hook", shotcaller=None, session_id=None,
                ), {})
                assert response == {"ok": True, "hook_output": {}}
                assert time.monotonic() - started < 2.5
        finally:
            worker.join(3)
            server.close()
        assert not worker.is_alive()
        assert not failures, failures


def test_control_requests_do_not_queue_behind_background_work():
    with tempfile.TemporaryDirectory(prefix="hook-capacity-") as tmp:
        runtime = PersistentSupervisor(Path(tmp), max_accepted_work=1)
        release = threading.Event()
        handled = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as background, ThreadPoolExecutor(max_workers=1) as control:
            runtime._executor = background
            runtime._request_executor = control
            try:
                assert runtime._submit(release.wait, 3)
                assert not runtime._submit(lambda: None)
                assert runtime._submit(handled.set, control=True)
                assert handled.wait(0.5), "control request was starved by background work"
                control.submit(lambda: None).result(timeout=0.5)
                # Control traffic has its own finite admission budget too.
                for _ in range(16):
                    assert runtime._submit(release.wait, 3, control=True)
                assert not runtime._submit(lambda: None, control=True)
            finally:
                release.set()
                runtime._executor = None
                runtime._request_executor = None


if __name__ == "__main__":
    test_codex_accept_is_native_noop()
    test_broker_accepts_briefly_delayed_reply()
    test_control_requests_do_not_queue_behind_background_work()
    print("PASS: native no-op, delayed broker, isolated control capacity")
