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
from league.storage import RequestResultCommand, StorageRefusal  # noqa: E402
from league.storage_request import MAX_TASK_RESULT_SOURCES  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    GAREN_RUNTIME_TWO,
    JARVAN_RUNTIME,
    capture_p100,
    create_context,
    dispatch_request,
    observe_runtime,
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


def two_writer_requests(root: Path, name: str):
    state, setup, clock = create_context(root, name)
    capture_p100(setup, clock)
    setup.close()
    return (
        SQLiteStorage(state, busy_timeout_ms=1000),
        SQLiteStorage(state, busy_timeout_ms=1000),
        clock,
    )


def test_same_request_claim_is_exclusive(root: Path) -> None:
    first, second, clock = two_writer_requests(root, "claim-race")
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


def test_different_requests_claim_concurrently(root: Path) -> None:
    first, second, clock = two_writer_requests(root, "different-requests")
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


def test_prompt_intake_requires_exact_verified_live_runtime(root: Path) -> None:
    _, store, clock = create_context(root, "prompt-runtime-provenance")
    rejected = (
        (JARVAN_RUNTIME, "source:wrong-actor"),
        (GAREN_RUNTIME, "source:unverified"),
        (GAREN_RUNTIME, "source:failed"),
    )
    for runtime_id, source in rejected:
        if source == "source:unverified":
            observe_runtime(
                store,
                clock,
                runtime_instance_id=GAREN_RUNTIME,
                actor_agent_id=SHOTCALLER_ID,
                endpoint="synthetic:garen:one",
                runtime_generation="generation:garen:one",
                status="active",
                verified=False,
            )
        elif source == "source:failed":
            observe_runtime(
                store,
                clock,
                runtime_instance_id=GAREN_RUNTIME,
                actor_agent_id=SHOTCALLER_ID,
                endpoint="synthetic:garen:one",
                runtime_generation="generation:garen:one",
                status="failed",
            )
        try:
            store.intake_prompt(
                f"prompt:{source}",
                SHOTCALLER_ID,
                runtime_id,
                "codex",
                "session:runtime-provenance",
                source,
                "Synthetic rejected prompt",
                clock.now(),
            )
        except StorageRefusal as exc:
            assert exc.code == "runtime_unverified"
        else:
            raise AssertionError(f"prompt intake accepted invalid runtime: {source}")
    assert store.connection.execute(
        "SELECT COUNT(*) FROM prompts WHERE session_ref='session:runtime-provenance'"
    ).fetchone()[0] == 0
    observe_runtime(
        store,
        clock,
        runtime_instance_id=GAREN_RUNTIME,
        actor_agent_id=SHOTCALLER_ID,
        endpoint="synthetic:garen:one",
        runtime_generation="generation:garen:one",
        status="idle",
    )
    accepted = store.intake_prompt(
        "prompt:idle-runtime",
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        "session:runtime-provenance",
        "source:idle-runtime",
        "Synthetic accepted idle runtime prompt",
        clock.now(),
    )
    assert not accepted["idempotent"]
    store.close()


def test_request_claim_requires_verified_active_or_idle_runtime(root: Path) -> None:
    _, store, clock = create_context(root, "claim-live-runtime")
    capture_p100(store, clock)
    observe_runtime(
        store,
        clock,
        runtime_instance_id=GAREN_RUNTIME,
        actor_agent_id=SHOTCALLER_ID,
        endpoint="synthetic:garen:one",
        runtime_generation="generation:garen:one",
        status="failed",
    )
    try:
        store.claim_request("R1", GAREN_RUNTIME, "failed-claim", clock.after(60), clock.now())
    except StorageRefusal as exc:
        assert exc.code == "runtime_unverified"
    else:
        raise AssertionError("failed runtime acquired a request claim")
    assert store.connection.execute(
        "SELECT COUNT(*) FROM request_claims WHERE request_id='R1'"
    ).fetchone()[0] == 0
    observe_runtime(
        store,
        clock,
        runtime_instance_id=GAREN_RUNTIME,
        actor_agent_id=SHOTCALLER_ID,
        endpoint="synthetic:garen:one",
        runtime_generation="generation:garen:one",
        status="idle",
    )
    assert store.claim_request(
        "R1", GAREN_RUNTIME, "idle-claim", clock.after(60), clock.now()
    )["claim_version"] == 1
    store.close()


def test_dispatch_retry_precedes_post_dispatch_state_gate(root: Path) -> None:
    _, store, clock = create_context(root, "dispatch-idempotency")
    capture_p100(store, clock)
    store.claim_request("R1", GAREN_RUNTIME, "claim-r1", clock.after(60), clock.now())
    first = dispatch_request(
        store, clock, "R1", "claim-r1", "dispatch-r1", "question", "direct"
    )
    assert first["request_version"] == 2 and not first["idempotent"]
    retry = dispatch_request(
        store, clock, "R1", "claim-r1", "dispatch-r1", "question", "direct"
    )
    assert retry["idempotent"] and retry["request_version"] == 2
    try:
        dispatch_request(
            store, clock, "R1", "claim-r1", "dispatch-r1-conflict", "question", "direct"
        )
    except StorageRefusal as exc:
        assert exc.code == "dispatch_conflict"
    else:
        raise AssertionError("dispatch retry accepted a changed idempotency identity")
    store.close()


def test_request_result_sources_are_bounded_before_validation_queries(root: Path) -> None:
    _, store, clock = create_context(root, "result-source-bound")
    capture_p100(store, clock)
    try:
        store.record_request_result(
            RequestResultCommand(
                request_id="R3",
                claim_token="unused-claim",
                expected_version=1,
                result_id="result:oversized-sources",
                idempotency_key="result-key:oversized-sources",
                outcome="success",
                summary="Synthetic result with too many sources",
                task_ids=tuple(
                    f"task:{index}" for index in range(MAX_TASK_RESULT_SOURCES + 1)
                ),
                at=clock.now(),
                return_to_requester=False,
                event_id=None,
                outbox_id=None,
            )
        )
    except StorageRefusal as exc:
        assert exc.code == "invalid_result"
    else:
        raise AssertionError("request result accepted an oversized task source list")
    assert store.connection.execute(
        "SELECT COUNT(*) FROM request_results WHERE result_id='result:oversized-sources'"
    ).fetchone()[0] == 0
    store.close()


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
        dispatch_request(
            store, clock, "R3", "old-proof", "stale-dispatch", "question", "direct"
        )
    except StorageRefusal as exc:
        assert exc.code == "claim_mismatch"
    else:
        raise AssertionError("expired request holder wrote after recovery")
    current = dispatch_request(
        store, clock, "R3", "new-proof", "current-dispatch", "question", "direct"
    )
    assert current["execution_mode"] == "direct"
    store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-request-concurrency-") as temporary:
        root = Path(temporary)
        test_prompt_source_idempotency_under_two_writers(root)
        test_same_request_claim_is_exclusive(root)
        test_different_requests_claim_concurrently(root)
        test_prompt_intake_requires_exact_verified_live_runtime(root)
        test_request_claim_requires_verified_active_or_idle_runtime(root)
        test_dispatch_retry_precedes_post_dispatch_state_gate(root)
        test_request_result_sources_are_bounded_before_validation_queries(root)
        test_expired_claim_recovery_fences_old_window(root)
    print("PASS: exact prompt runtime provenance, live request claims, dispatch idempotency, two-window exclusion, concurrency, and stale-claim fencing")


if __name__ == "__main__":
    main()
