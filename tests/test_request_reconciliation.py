#!/usr/bin/env python3
"""Focused omission-only Stop and agent-authored duplicate reconciliation proof."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from league.storage_request import ReconcileDuplicateRequestCommand  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    create_context,
    dispatch_request,
)
from storage_fixture import SHOTCALLER_ID  # noqa: E402
from storage_test_support import invoke_cli  # noqa: E402


def _request(store, clock, suffix: str, summary: str) -> None:
    prompt_id = f"prompt:reconcile:{suffix}"
    request_id = f"request:reconcile:{suffix}"
    store.intake_prompt(
        prompt_id,
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        f"session:{GAREN_RUNTIME}",
        f"turn:{suffix}",
        summary,
        clock.now(),
    )
    store.triage_prompt(
        prompt_id,
        [
            {
                "prompt_item_id": f"item:reconcile:{suffix}",
                "ordinal": 1,
                "summary": summary,
                "disposition": "new_request",
                "request_id": request_id,
                "next_attention_at": None,
            }
        ],
        clock.now(),
    )


def _command(
    state: Path,
    clock,
    *,
    duplicate_request_id: str = "request:reconcile:B",
    canonical_request_id: str = "request:reconcile:A",
    expected_duplicate_version: int = 1,
    expected_canonical_version: int = 1,
    expected_exit: int = 0,
) -> tuple[int, dict]:
    result = invoke_cli(
        state,
            "request",
            "reconcile-duplicate",
            "--duplicate-request-id",
            duplicate_request_id,
            "--canonical-request-id",
            canonical_request_id,
            "--owner-agent-id",
            SHOTCALLER_ID,
            "--expected-duplicate-version",
            str(expected_duplicate_version),
            "--expected-canonical-version",
            str(expected_canonical_version),
            "--at",
            clock.now(),
        expected=expected_exit,
    )
    return expected_exit, result


def test_stop_is_read_only_and_reconciliation_is_exact(root: Path) -> None:
    state, store, clock = create_context(root, "reconcile")
    _request(store, clock, "A", "Canonical owner request")
    _request(store, clock, "B", "Paraphrased duplicate owner request")
    before = store.unresolved_requests(SHOTCALLER_ID)
    before_ids = [row["request_id"] for row in before["requests"]]
    stop = store.stop_decision(
        "watcher:Garen",
        SHOTCALLER_ID,
        "terminal:reconciliation-proof",
        clock.now(),
        block_on_fresh_terminal=True,
    )
    after_stop = store.unresolved_requests(SHOTCALLER_ID)
    assert stop["decision"] == "block"
    assert stop["unresolved_summaries"] == [
        "Canonical owner request",
        "Paraphrased duplicate owner request",
    ]
    assert [row["request_id"] for row in after_stop["requests"]] == before_ids
    store.close()

    code, first = _command(state, clock)
    assert code == 0 and first["result"]["idempotent"] is False, first
    assert first["result"]["duplicate_state"] == "cancelled"
    code, repeated = _command(state, clock)
    assert code == 0 and repeated["result"]["idempotent"] is True, repeated

    with SQLiteStorage(state) as observer:
        boundary = observer.request_turn_boundary(SHOTCALLER_ID)
        assert [row["request_id"] for row in boundary["requests"]] == [
            "request:reconcile:A"
        ]
        assert boundary["obligations"]["unresolved_requests"] == 1
        exported = json.loads(
            observer.export_bytes(
                format_name="json", purpose="inspection", max_records=10_000
            )
        )
    sources = exported["tables"]["request_sources"]
    assert {row["request_id"] for row in sources} == {
        "request:reconcile:A",
        "request:reconcile:B",
    }
    links = exported["tables"]["request_reconciliations"]
    assert len(links) == 1
    assert links[0]["duplicate_request_id"] == "request:reconcile:B"
    assert links[0]["canonical_request_id"] == "request:reconcile:A"
    assert exported["tables"]["request_results"] == []


def test_reconciliation_refuses_self_and_stale_versions(root: Path) -> None:
    state, store, clock = create_context(root, "refusals")
    _request(store, clock, "A", "Canonical owner request")
    _request(store, clock, "B", "Duplicate owner request")
    store.close()
    self_code, self_result = _command(
        state,
        clock,
        duplicate_request_id="request:reconcile:A",
        canonical_request_id="request:reconcile:A",
        expected_exit=2,
    )
    assert self_code == 2 and self_result["error"]["code"] == "invalid_reconciliation"
    stale_code, stale_result = _command(
        state,
        clock,
        expected_duplicate_version=2,
        expected_exit=3,
    )
    assert stale_code == 3 and stale_result["error"]["code"] == "version_conflict"


def test_reconciliation_refuses_direct_dispatch_evidence(root: Path) -> None:
    _state, store, clock = create_context(root, "direct-dispatch-refusal")
    _request(store, clock, "A", "Canonical owner request")
    _request(store, clock, "B", "Duplicate owner request")
    store.claim_request(
        "request:reconcile:B",
        GAREN_RUNTIME,
        "claim:reconcile:B",
        clock.after(120),
        clock.now(),
    )
    dispatch_request(
        store,
        clock,
        "request:reconcile:B",
        "claim:reconcile:B",
        "dispatch:reconcile:B",
        "question",
        "direct",
    )
    duplicate_version = int(
        store.connection.execute(
            "SELECT version FROM requests WHERE request_id='request:reconcile:B'"
        ).fetchone()[0]
    )
    try:
        store.reconcile_duplicate_request(
            ReconcileDuplicateRequestCommand(
                duplicate_request_id="request:reconcile:B",
                canonical_request_id="request:reconcile:A",
                owner_agent_id=SHOTCALLER_ID,
                expected_duplicate_version=duplicate_version,
                expected_canonical_version=1,
                at=clock.now(),
            )
        )
    except StorageRefusal as exc:
        assert exc.code == "irreversible_execution_started"
    else:
        raise AssertionError("direct dispatch evidence allowed semantic supersession")
    store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-request-reconciliation-") as temporary:
        root = Path(temporary)
        test_stop_is_read_only_and_reconciliation_is_exact(root / "success")
        test_reconciliation_refuses_self_and_stale_versions(root / "refusal")
        test_reconciliation_refuses_direct_dispatch_evidence(root / "direct")
    print(
        "PASS: Stop is omission-only; exact same-owner reconciliation supersedes only the duplicate, "
        "preserves sources, is idempotent, and refuses self/stale links"
    )


if __name__ == "__main__":
    main()
