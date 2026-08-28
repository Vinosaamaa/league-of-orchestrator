#!/usr/bin/env python3
"""Multi-window prompt and request-claim concurrency coverage."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    GAREN_RUNTIME_TWO,
    capture_p100,
    create_context,
)
from storage_fixture import SHOTCALLER_ID  # noqa: E402


def race(functions):
    barrier = threading.Barrier(len(functions))
    outcomes = []
    lock = threading.Lock()

    def run(function):
        barrier.wait()
        try:
            result = ("ok", function())
        except StorageRefusal as exc:
            result = (exc.code, None)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=run, args=(function,)) for function in functions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    return outcomes


def test_prompt_source_idempotency_under_two_writers(root: Path) -> None:
    state, setup, clock = create_context(root, "prompt-race")
    setup.close()
    first = SQLiteStorage(state, busy_timeout_ms=1000)
    second = SQLiteStorage(state, busy_timeout_ms=1000)
    args = (
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        "session:race",
        "source:race",
        "Identical retry payload",
        clock.now(),
    )
    outcomes = race(
        [
            lambda: first.intake_prompt("prompt-race-1", *args),
            lambda: second.intake_prompt("prompt-race-2", *args),
        ]
    )
    assert [name for name, _ in outcomes].count("ok") == 2
    prompt_ids = {result["prompt_id"] for name, result in outcomes if name == "ok"}
    assert len(prompt_ids) == 1
    assert first.connection.execute(
        "SELECT COUNT(*) FROM prompts WHERE source_event_key='source:race'"
    ).fetchone()[0] == 1
    first.close()
    second.close()


def test_same_request_race_and_different_request_concurrency(root: Path) -> None:
    state, setup, clock = create_context(root, "claim-race")
    capture_p100(setup, clock)
    setup.close()
    first = SQLiteStorage(state, busy_timeout_ms=1000)
    second = SQLiteStorage(state, busy_timeout_ms=1000)
    outcomes = race(
        [
            lambda: first.claim_request(
                "R2", GAREN_RUNTIME, "claim-window-one", clock.after(60), clock.now()
            ),
            lambda: second.claim_request(
                "R2", GAREN_RUNTIME_TWO, "claim-window-two", clock.after(60), clock.now()
            ),
        ]
    )
    assert sorted(name for name, _ in outcomes) == ["ok", "request_claimed"]
    first.close()
    second.close()

    state, setup, clock = create_context(root, "different-requests")
    capture_p100(setup, clock)
    setup.close()
    first = SQLiteStorage(state, busy_timeout_ms=1000)
    second = SQLiteStorage(state, busy_timeout_ms=1000)
    outcomes = race(
        [
            lambda: first.claim_request(
                "R2", GAREN_RUNTIME, "claim-r2", clock.after(60), clock.now()
            ),
            lambda: second.claim_request(
                "R3", GAREN_RUNTIME_TWO, "claim-r3", clock.after(60), clock.now()
            ),
        ]
    )
    assert [name for name, _ in outcomes] == ["ok", "ok"] or sorted(
        name for name, _ in outcomes
    ) == ["ok", "ok"]
    assert first.connection.execute(
        "SELECT COUNT(*) FROM request_claims WHERE released_at IS NULL"
    ).fetchone()[0] == 2
    first.close()
    second.close()


def test_expired_claim_recovery_fences_old_window(root: Path) -> None:
    _, store, clock = create_context(root, "claim-recovery")
    capture_p100(store, clock)
    first = store.claim_request(
        "R3", GAREN_RUNTIME, "old-proof", clock.after(10), clock.now()
    )
    assert first["claim_version"] == 1
    clock.advance(11)
    recovered = store.claim_request(
        "R3", GAREN_RUNTIME_TWO, "new-proof", clock.after(60), clock.now()
    )
    assert recovered["recovered"] and recovered["claim_version"] == 2
    try:
        store.dispatch_request(
            "R3",
            "old-proof",
            "stale-dispatch",
            "question",
            "direct",
            False,
            None,
            None,
            None,
            clock.now(),
        )
    except StorageRefusal as exc:
        assert exc.code == "claim_mismatch"
    else:
        raise AssertionError("expired request holder wrote after recovery")
    current = store.dispatch_request(
        "R3",
        "new-proof",
        "current-dispatch",
        "question",
        "direct",
        False,
        None,
        None,
        None,
        clock.now(),
    )
    assert current["execution_mode"] == "direct"
    store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-request-concurrency-") as temporary:
        root = Path(temporary)
        test_prompt_source_idempotency_under_two_writers(root)
        test_same_request_race_and_different_request_concurrency(root)
        test_expired_claim_recovery_fences_old_window(root)
    print("PASS: two-window prompt idempotency, same-request exclusion, different-request concurrency, and stale-claim fencing")


if __name__ == "__main__":
    main()
