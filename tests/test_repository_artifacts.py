#!/usr/bin/env python3
"""Focused repository-owned publication and teardown gate coverage."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from storage_fixture import CHAMPION_ID, TASK_ID  # noqa: E402
from storage_test_support import invoke_cli, seeded_state  # noqa: E402


AT3 = "2026-01-01T00:02:00Z"
HEAD = "a" * 40
MERGE = "b" * 40


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def declaration(branch: str = "agent/synthetic/report") -> dict:
    return {
        "artifact_id": "artifact:report",
        "task_id": TASK_ID,
        "name": "Synthetic research report",
        "classification": "repository_owned",
        "repository": "https://example.invalid/league.git",
        "issue": 40,
        "worktree": "/synthetic/worktrees/report",
        "branch": branch,
        "repository_path": "docs/research/synthetic-report.md",
    }


def cleanup_manifest() -> dict:
    return {
        "task_id": TASK_ID,
        "owner": {"id": CHAMPION_ID, "role": "champion", "persistent": False},
        "task_class": "analysis",
        "disposition": "completed",
        "pending_decisions_clear": True,
        "expected_cleanup_version": 0,
        "identity": {"task_id": TASK_ID, "owner_id": CHAMPION_ID, "generation": "exact"},
        "proof": {"identity": {"exact": True}, "endpoint": {"terminal_or_idle": True}},
        "resources": [],
        "final_actions": [
            {
                "action_kind": name,
                "adapter_kind": adapter,
                "expected_identity": {"action": name, "generation": "exact"},
                "intended_state": {"completed": True, "action": name},
            }
            for name, adapter in (
                ("session_exit", "harness"),
                ("endpoint_close", "backend"),
                ("callsign_release", "callsign"),
            )
        ],
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-repository-artifacts-") as temporary:
        root = Path(temporary)
        _, state, _ = seeded_state(root, "state")

        direct_main = root / "direct-main.json"
        write(direct_main, declaration("main"))
        refused = invoke_cli(
            state, "artifact", "declare", "--input", str(direct_main), "--at", AT3, expected=2
        )
        assert refused["error"]["code"] == "direct_main_refused"

        spec = root / "artifact.json"
        write(spec, declaration())
        declared = invoke_cli(state, "artifact", "declare", "--input", str(spec), "--at", AT3)
        assert declared["result"]["state"] == "pending"

        manifest = root / "cleanup.json"
        write(manifest, cleanup_manifest())
        refused = invoke_cli(
            state, "cleanup", "plan", "--manifest", str(manifest),
            "--operation-id", "operation:pending", "--at", AT3, expected=2,
        )
        assert refused["error"]["code"] == "repository_publication_unresolved"

        missing = root / "missing-merge.json"
        write(
            missing,
            {"pull_request_number": 41, "pull_request_url": "https://example.invalid/pull/41",
             "tested_head": HEAD, "merge_receipt": None},
        )
        refused = invoke_cli(
            state, "artifact", "publish", "--artifact-id", "artifact:report",
            "--expected-version", "1", "--receipt", str(missing), "--at", AT3, expected=2,
        )
        assert refused["error"]["code"] == "merge_receipt_missing"

        receipt = root / "receipt.json"
        write(
            receipt,
            {
                "pull_request_number": 41,
                "pull_request_url": "https://example.invalid/pull/41",
                "tested_head": HEAD,
                "merge_receipt": {
                    "commit": MERGE,
                    "url": "https://example.invalid/commit/" + MERGE,
                    "merged_at": AT3,
                },
            },
        )
        published = invoke_cli(
            state, "artifact", "publish", "--artifact-id", "artifact:report",
            "--expected-version", "1", "--receipt", str(receipt), "--at", AT3,
        )
        assert published["result"]["state"] == "published"
        status = invoke_cli(state, "artifact", "status", "--task-id", TASK_ID)
        assert status["result"]["artifacts"][0]["merge_commit"] == MERGE

        allowed = invoke_cli(
            state, "cleanup", "plan", "--manifest", str(manifest),
            "--operation-id", "operation:published", "--at", AT3,
        )
        assert allowed["result"]["state"] == "cleanup_pending"
    print("PASS: repository publication is required before cleanup")


if __name__ == "__main__":
    main()
