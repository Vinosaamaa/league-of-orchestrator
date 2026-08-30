#!/usr/bin/env python3
"""Focused one-process exact-intake and atomic model-authored triage coverage."""

from __future__ import annotations

import json
import hashlib
from io import BytesIO
import subprocess
import tempfile
from unittest.mock import patch
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from request_lifecycle_fixture import GAREN_RUNTIME, create_context  # noqa: E402
from storage_fixture import SHOTCALLER_ID  # noqa: E402
from league.cli import main as league_main  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402


def _capture(store, clock, prompt_id: str, body: str) -> None:
    store.intake_prompt(
        prompt_id,
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        "session:turn-batch",
        f"source:{prompt_id}",
        body,
        clock.now(),
    )


def _decision(prompt_id: str, request_id: str, summary: str) -> dict[str, object]:
    return {
        "prompt_id": prompt_id,
        "items": [
            {
                "prompt_item_id": f"item:{prompt_id}:1",
                "ordinal": 1,
                "summary": summary,
                "disposition": "new_request",
                "request_id": request_id,
            }
        ],
    }


def _semantic_decision(summary: str) -> dict[str, object]:
    return {"items": [{"summary": summary, "disposition": "new_request"}]}


def _semantic_plan() -> dict[str, object]:
    return {
        "work_kind": "short-check",
        "requested_mode": "direct",
        "signals": {
            "pre_bounded": True,
            "read_only": True,
            "answer_or_routing_only": True,
            "expected_minutes": 2,
            "expected_task_action_calls": 1,
        },
    }


def _external_semantic_plan() -> dict[str, object]:
    return {
        "work_kind": "repository-write",
        "requested_mode": "champion",
        "signals": {
            "pre_bounded": False,
            "read_only": False,
            "answer_or_routing_only": False,
            "expected_minutes": 20,
            "expected_task_action_calls": 5,
        },
    }


def _seed_request(store, clock, ordinal: int, summary: str | None = None) -> None:
    prompt_id = f"prompt:seed:{ordinal:03d}"
    request_id = f"request:seed:{ordinal:03d}"
    _capture(store, clock, prompt_id, summary or f"Seed request {ordinal}")
    store.triage_prompt(
        prompt_id,
        [_decision(prompt_id, request_id, summary or f"Seed request {ordinal}")["items"][0]],
        clock.now(),
    )


def _start_turn(
    state: Path, at: str, *, limit: int | None = None
) -> subprocess.Popen[str]:
    command = [
            sys.executable,
            str(ROOT / "bin/league"),
            "--state-root",
            str(state),
            "request",
            "turn",
            "--owner-agent-id",
            SHOTCALLER_ID,
            "--at",
            at,
        ]
    if limit is not None:
        command.extend(("--limit", str(limit)))
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_interactive_turn_uses_one_process_and_one_ordered_batch(root: Path) -> None:
    state, store, clock = create_context(root, "turn-success")
    _capture(store, clock, "prompt:A", "First exact owner prompt")
    _capture(store, clock, "prompt:B", "Second distinct exact owner prompt")
    store.close()

    process = _start_turn(state, clock.now())
    turn_pid = process.pid
    assert process.stdout is not None and process.stdin is not None
    intake = json.loads(process.stdout.readline())
    assert intake["ok"] is True and intake["result"]["phase"] == "intake"
    prompts = intake["result"]["prompts"]
    assert [item["prompt_id"] for item in prompts] == ["prompt:A", "prompt:B"]
    assert [item["body"] for item in prompts] == [
        "First exact owner prompt",
        "Second distinct exact owner prompt",
    ]
    decisions = {
        "candidate_inventory_digest": intake["result"]["candidate_inventory"]["digest"],
        "decisions": [
            _semantic_decision("Handle the first request"),
            _semantic_decision("Handle the second request"),
        ],
        "plans": [_semantic_plan(), _semantic_plan()],
    }
    process.stdin.write(json.dumps(decisions, separators=(",", ":")) + "\n")
    process.stdin.flush()
    begun = json.loads(process.stdout.readline())
    assert process.pid == turn_pid and process.poll() is None
    assert begun["result"]["phase"] == "begun"
    assert begun["result"]["batch"]["prompt_count"] == 2
    request_ids = [
        item["request_id"] for item in begun["result"]["batch"]["dispatch_readiness"]
    ]
    assert len(request_ids) == 2 and request_ids[0] != request_ids[1]
    assert all(value.startswith("request:") for value in request_ids)
    assert all(
        route["mechanical"]["claim_token"].startswith("claim:")
        for route in begun["result"]["routing"]
    )
    assert all(
        item["dispatch"]["execution_mode"] == "direct"
        for item in begun["result"]["routing"]
    )
    assert begun["result"]["unresolved"]["untriaged_prompt_count"] == 0
    assert begun["result"]["unresolved"]["unresolved_count"] == 2
    actions = [
        {
            "kind": "answer",
            "request_index": 1,
            "content": "Bounded response A",
            "resolution_summary": "Answered bounded request A",
        },
        {
            "kind": "result",
            "request_index": 2,
            "outcome": "complete",
            "summary": "Returned bounded request B outcome",
            "task_ids": [],
            "return_to_requester": True,
        },
    ]
    process.stdin.write(
        json.dumps({"actions": actions}, separators=(",", ":"))
        + "\n"
    )
    process.stdin.flush()
    completed = json.loads(process.stdout.readline())
    assert process.wait(timeout=10) == 0
    assert process.pid == turn_pid
    assert completed["result"]["phase"] == "committed"
    assert len(completed["result"]["actions"]) == 2
    boundary = completed["result"]["unresolved"]
    assert boundary["safe_to_finish"] is False
    assert boundary["obligations"] == {
        "active_champions": 1,
        "cleanup_obligations": 0,
        "pending_assignments": 0,
        "pending_deliveries": 1,
        "unresolved_requests": 1,
    }, boundary


def test_turn_handler_never_spawns_a_second_process(root: Path) -> None:
    state, store, clock = create_context(root, "turn-no-child")
    _capture(store, clock, "prompt:G", "Seventh exact owner prompt")
    candidate_digest = store.untriaged_intake(SHOTCALLER_ID)["candidate_inventory"]["digest"]
    store.close()
    response = "One direct response"
    begin = {
        "candidate_inventory_digest": candidate_digest,
        "decisions": [_semantic_decision("Handle the seventh request")],
        "plans": [_semantic_plan()],
    }
    commit = {
        "actions": [
            {
                "kind": "answer",
                "request_index": 1,
                "content": response,
                "resolution_summary": "Answered bounded request G",
            }
        ]
    }
    source = BytesIO(
        (json.dumps(begin, separators=(",", ":")) + "\n").encode()
        + (json.dumps(commit, separators=(",", ":")) + "\n").encode()
    )
    sink = BytesIO()
    with patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
        code = league_main(
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
    assert code == 0
    popen.assert_not_called()
    run.assert_not_called()
    phases = [json.loads(line)["result"]["phase"] for line in sink.getvalue().splitlines()]
    assert phases == ["intake", "begun", "committed"]


def test_batch_failure_is_atomic_and_exact_retry_is_idempotent(root: Path) -> None:
    state, store, clock = create_context(root, "turn-atomic")
    _capture(store, clock, "prompt:C", "Third exact prompt")
    _capture(store, clock, "prompt:D", "Fourth exact prompt")
    decisions = [
        _decision("prompt:C", "request:collision", "Handle the third request"),
        _decision("prompt:D", "request:collision", "Handle the fourth request"),
    ]
    try:
        store.triage_prompt_batch(
            SHOTCALLER_ID,
            ("prompt:C", "prompt:D"),
            decisions,
            clock.now(),
        )
    except Exception as exc:
        assert getattr(exc, "code", None) == "conflict"
    else:
        raise AssertionError("colliding batch unexpectedly committed")
    assert store.untriaged_intake(SHOTCALLER_ID)["returned_count"] == 2
    fixed = [
        _decision("prompt:C", "request:C", "Handle the third request"),
        _decision("prompt:D", "request:D", "Handle the fourth request"),
    ]
    first = store.triage_prompt_batch(
        SHOTCALLER_ID,
        ("prompt:C", "prompt:D"),
        fixed,
        clock.now(),
    )
    retry = store.triage_prompt_batch(
        SHOTCALLER_ID,
        ("prompt:C", "prompt:D"),
        fixed,
        clock.now(),
    )
    assert first["idempotent"] is False and retry["idempotent"] is True
    assert store.untriaged_intake(SHOTCALLER_ID)["returned_count"] == 0
    store.close()


def test_partial_duplicate_or_reordered_decisions_refuse(root: Path) -> None:
    _, store, clock = create_context(root, "turn-refusals")
    _capture(store, clock, "prompt:E", "Fifth exact prompt")
    _capture(store, clock, "prompt:F", "Sixth exact prompt")
    cases = (
        [_decision("prompt:E", "request:E", "Handle the fifth request")],
        [
            _decision("prompt:E", "request:E", "Handle the fifth request"),
            _decision("prompt:E", "request:F", "Handle the sixth request"),
        ],
        [
            _decision("prompt:F", "request:F", "Handle the sixth request"),
            _decision("prompt:E", "request:E", "Handle the fifth request"),
        ],
    )
    for decisions in cases:
        try:
            store.triage_prompt_batch(
                SHOTCALLER_ID,
                ("prompt:E", "prompt:F"),
                decisions,
                clock.now(),
            )
        except Exception as exc:
            assert getattr(exc, "code", None) == "incomplete_triage_batch"
        else:
            raise AssertionError("incomplete or ambiguous batch unexpectedly committed")
    assert store.untriaged_intake(SHOTCALLER_ID)["returned_count"] == 2
    store.close()


def test_one_process_persists_all_dispositions_without_minting_deferred_requests(
    root: Path,
) -> None:
    state, store, clock = create_context(root, "turn-all-dispositions")
    _capture(store, clock, "prompt:seed", "Seed one existing request")
    store.triage_prompt(
        "prompt:seed",
        [_decision("prompt:seed", "request:existing", "Existing work")["items"][0]],
        clock.now(),
    )
    _capture(store, clock, "prompt:all", "Exercise every semantic disposition")
    store.close()

    process = _start_turn(state, clock.now())
    turn_pid = process.pid
    assert process.stdin is not None and process.stdout is not None
    intake = json.loads(process.stdout.readline())
    assert [row["prompt_id"] for row in intake["result"]["prompts"]] == ["prompt:all"]
    process.stdin.write(
        json.dumps(
            {
                "candidate_inventory_digest": intake["result"]["candidate_inventory"]["digest"],
                "decisions": [
                    {
                        "items": [
                            {"summary": "New work", "disposition": "new_request"},
                            {
                                "summary": "Follow existing work",
                                "disposition": "follow_up",
                                "related_request_id": "request:existing",
                                "related_request_version": 1,
                            },
                            {"summary": "Context only", "disposition": "context"},
                            {"summary": "Acknowledged only", "disposition": "acknowledgement"},
                            {
                                "summary": "Duplicate of existing work",
                                "disposition": "duplicate",
                                "related_request_id": "request:existing",
                                "related_request_version": 1,
                            },
                            {
                                "summary": "Defer existing work",
                                "disposition": "deferred",
                                "related_request_id": "request:existing",
                                "related_request_version": 1,
                                "defer_seconds": 60,
                            },
                        ]
                    }
                ],
                "plans": [_semantic_plan()],
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    process.stdin.flush()
    begun = json.loads(process.stdout.readline())
    assert process.pid == turn_pid and process.poll() is None
    assert begun["result"]["phase"] == "begun"
    assert begun["result"]["batch"]["triage"][0]["request_count"] == 1
    process.stdin.write(
        json.dumps(
            {
                "actions": [
                    {
                        "kind": "answer",
                        "request_index": 1,
                        "content": "Synthetic answer.",
                        "resolution_summary": "Answered the only newly minted request.",
                    }
                ]
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    process.stdin.flush()
    committed = json.loads(process.stdout.readline())
    assert committed["result"]["phase"] == "committed"
    assert process.wait(timeout=10) == 0 and process.pid == turn_pid

    with SQLiteStorage(state) as observer:
        rows = observer.connection.execute(
            "SELECT request_id,state FROM requests ORDER BY request_id"
        ).fetchall()
        assert len(rows) == 2
        assert {row[0]: row[1] for row in rows}["request:existing"] == "deferred"
        dispositions = observer.connection.execute(
            "SELECT disposition FROM prompt_items WHERE prompt_id='prompt:all' ORDER BY ordinal"
        ).fetchall()
        assert [row[0] for row in dispositions] == [
            "new_request",
            "follow_up",
            "context",
            "acknowledgement",
            "duplicate",
            "deferred",
        ]


def test_small_shortlist_does_not_block_direct_and_pages_off_path(root: Path) -> None:
    state, store, clock = create_context(root, "turn-shortlist-direct")
    for ordinal in range(80):
        _seed_request(store, clock, ordinal, f"Historical request {ordinal}")
    _capture(store, clock, "prompt:shortlist", "Answer this new bounded owner question")
    ranked = store.untriaged_intake(SHOTCALLER_ID)
    inventory = ranked["candidate_inventory"]
    assert inventory["total_count"] == 80
    assert inventory["returned_count"] == 12 and inventory["truncated"]
    assert inventory["ranking"] == "routing-lexical-recency"
    page_one = store.untriaged_intake(
        SHOTCALLER_ID, candidate_limit=12, candidate_page=True
    )["candidate_inventory"]
    assert page_one["ranking"] == "request-id-page" and page_one["next_cursor"]
    page_two = store.untriaged_intake(
        SHOTCALLER_ID,
        candidate_limit=12,
        candidate_after=page_one["next_cursor"],
        candidate_page=True,
    )["candidate_inventory"]
    assert page_two["returned_count"] == 12 and page_two["truncated"]
    store.close()

    process = _start_turn(state, clock.now())
    assert process.stdin is not None and process.stdout is not None
    intake = json.loads(process.stdout.readline())
    assert intake["result"]["candidate_inventory"]["truncated"]
    process.stdin.write(
        json.dumps(
            {
                "candidate_inventory_digest": intake["result"]["candidate_inventory"]["digest"],
                "decisions": [_semantic_decision("Answer the bounded owner question")],
                "plans": [_semantic_plan()],
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    process.stdin.flush()
    begun = json.loads(process.stdout.readline())
    assert begun["result"]["phase"] == "begun", begun
    process.stdin.write(
        json.dumps(
            {
                "actions": [
                    {
                        "kind": "answer",
                        "request_index": 1,
                        "content": "Direct synthetic answer",
                        "resolution_summary": "Answered without external work",
                    }
                ]
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    process.stdin.flush()
    committed = json.loads(process.stdout.readline())
    assert process.wait(timeout=10) == 0 and committed["result"]["phase"] == "committed"


def test_turn_limit_refuses_before_intake(root: Path) -> None:
    state, store, clock = create_context(root, "turn-limit-refusal")
    _capture(store, clock, "prompt:limit", "Prompt must remain unconsumed")
    store.close()
    process = _start_turn(state, clock.now(), limit=26)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 2 and stdout == ""
    assert "turn prompt limit must be between 1 and 25" in stderr
    with SQLiteStorage(state) as observer:
        assert observer.untriaged_intake(SHOTCALLER_ID)["returned_count"] == 1


def test_truncated_or_changed_candidates_fence_only_external_dispatch(root: Path) -> None:
    state, store, clock = create_context(root, "turn-shortlist-external")
    for ordinal in range(13):
        _seed_request(store, clock, ordinal)
    _capture(store, clock, "prompt:external", "Delegate a repository change")
    store.close()
    process = _start_turn(state, clock.now())
    assert process.stdin is not None and process.stdout is not None
    intake = json.loads(process.stdout.readline())
    process.stdin.write(
        json.dumps(
            {
                "candidate_inventory_digest": intake["result"]["candidate_inventory"]["digest"],
                "decisions": [_semantic_decision("Delegate repository work")],
                "plans": [_external_semantic_plan()],
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    process.stdin.flush()
    refused = json.loads(process.stdout.readline())
    assert process.wait(timeout=10) == 3
    assert refused["error"]["code"] == "candidate_inventory_truncated", refused

    changed_state, changed_store, changed_clock = create_context(root, "turn-candidate-cas")
    _seed_request(changed_store, changed_clock, 1, "Existing candidate")
    _capture(changed_store, changed_clock, "prompt:cas", "Delegate changed candidate work")
    changed_store.close()
    changed = _start_turn(changed_state, changed_clock.now())
    assert changed.stdin is not None and changed.stdout is not None
    changed_intake = json.loads(changed.stdout.readline())
    with SQLiteStorage(changed_state) as writer:
        _seed_request(writer, changed_clock, 2, "Concurrent candidate")
    changed.stdin.write(
        json.dumps(
            {
                "candidate_inventory_digest": changed_intake["result"]["candidate_inventory"]["digest"],
                "decisions": [_semantic_decision("Delegate changed candidate work")],
                "plans": [_external_semantic_plan()],
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    changed.stdin.flush()
    changed_refused = json.loads(changed.stdout.readline())
    assert changed.wait(timeout=10) == 3
    assert changed_refused["error"]["code"] == "version_conflict", changed_refused


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-request-turn-") as temporary:
        root = Path(temporary)
        test_interactive_turn_uses_one_process_and_one_ordered_batch(root)
        test_turn_handler_never_spawns_a_second_process(root)
        test_batch_failure_is_atomic_and_exact_retry_is_idempotent(root)
        test_partial_duplicate_or_reordered_decisions_refuse(root)
        test_one_process_persists_all_dispositions_without_minting_deferred_requests(root)
        test_small_shortlist_does_not_block_direct_and_pages_off_path(root)
        test_truncated_or_changed_candidates_fence_only_external_dispatch(root)
        test_turn_limit_refuses_before_intake(root)
    print(
        "PASS: one request-turn process emits exact intake, atomically begins ordered model "
        "triage/routing, commits answers, and returns the final unresolved boundary"
    )


if __name__ == "__main__":
    main()
