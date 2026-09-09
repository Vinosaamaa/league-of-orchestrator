#!/usr/bin/env python3
"""An unchanged native Stop continuation never creates another model turn."""
from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league import canonical_watcher as watcher
from league.sqlite_store import SQLiteStorage
from storage_fixture import SHOTCALLER_ID
from storage_test_support import seeded_state
from test_canonical_watcher import _environment, _register_garen_runtime, _watcher


def test_bounded_stop(root: Path) -> None:
    _, state, _ = seeded_state(root, "bounded-native-stop")
    env = _environment(root / "bounded-native-stop", state)
    _register_garen_runtime(state, "bounded-native-stop", session_ref=SHOTCALLER_ID)
    first = {"session_id": SHOTCALLER_ID, "turn_id": "bounded-turn",
             "hook_event_name": "Stop", "stop_hook_active": False}
    assert _watcher(env, "codex-stop-hook", payload=first)["decision"] == "block"
    # A retry of the original hook must still return its feedback, not lose it.
    assert _watcher(env, "codex-stop-hook", payload=first)["decision"] == "block"
    with SQLiteStorage(state, request_wal=False) as store:
        before = "\n".join(store.connection.iterdump())
    continuation = {**first, "stop_hook_active": True}
    # Even a dead broker cannot turn an already-issued reminder into a loop.
    with patch.dict("os.environ", env), patch.object(
        watcher, "_broker_hook", side_effect=AssertionError("must not contact broker")
    ):
        for _ in range(5):
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps(continuation))), redirect_stdout(output):
                assert watcher.main(["codex-stop-hook"]) == 0
            assert json.loads(output.getvalue()) == {}
    with SQLiteStorage(state, request_wal=False) as store:
        assert "\n".join(store.connection.iterdump()) == before
    # Human steering can reuse turn_id and must still rearm exactly once.
    prompt = {"session_id": SHOTCALLER_ID, "turn_id": "bounded-turn",
              "hook_event_name": "UserPromptSubmit", "prompt": "New synthetic request"}
    assert _watcher(env, "codex-user-prompt-hook", payload=prompt) == {}
    assert _watcher(env, "codex-stop-hook", payload=continuation)["decision"] == "block"
    assert _watcher(env, "codex-stop-hook", payload=continuation) == {}
    with SQLiteStorage(state, request_wal=False) as store:
        row = store.connection.execute(
            'SELECT metadata_json FROM watcher_scopes WHERE actor_agent_id=?', (SHOTCALLER_ID,),
        ).fetchone()
        assert json.loads(row['metadata_json'])['receiver_activity']['state'] == 'idle'
    # Unbound and malformed native input do not acquire this bound exemption.
    args = type("Args", (), {"command": "codex-stop-hook", "shotcaller": None,
                             "session_id": None})()
    with patch.dict("os.environ", env):
        assert not watcher._stop_continuation_already_notified(
            args, {**continuation, "session_id": "unbound-session"}
        )
        assert not watcher._stop_continuation_already_notified(
            args, {**continuation, "stop_hook_active": "true"}
        )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="league-stop-continuation-") as temporary:
        test_bounded_stop(Path(temporary))
    print("PASS: bounded native Stop, unchanged durable work, unavailable broker, fresh steering")
