"""Synthetic explicit-root fixtures shared by focused SQLite storage tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


AT1 = "2026-01-01T00:00:00Z"
AT2 = "2026-01-01T00:01:00Z"
SHOTCALLER_ID = "11111111-1111-4111-8111-111111111111"
CHAMPION_ID = "22222222-2222-4222-8222-222222222222"
TASK_ID = "synthetic-task-19"
REPOSITORY = "https://example.invalid/league.git"


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json(value) + "\n", encoding="utf-8")


def event_digest(source: str, offset: int, line: str) -> str:
    return hashlib.sha256(f"{source}\0{offset}\0{line}".encode("utf-8")).hexdigest()


def _write_roster_records(
    root: Path, runtime_root: Path | None = None
) -> dict[str, Any]:
    runtime_root = runtime_root or root / "synthetic-runtime-observation"
    original_updates = str(runtime_root / "rosters/Garen/champions/Thresh/updates.jsonl")
    champion_record = str(runtime_root / "rosters/Garen/champions/Thresh")
    champion_status_path = str(runtime_root / "rosters/Garen/champions/Thresh/status.json")
    champion_worktree = str(runtime_root / "worktrees/issue-19")
    champion_status = {
        "callsign": "Thresh",
        "role": "champion",
        "shotcaller": "Garen",
        "kind": "codex-thread",
        "address": "w1:p2",
        "thread_id": CHAMPION_ID,
        "backend": "herdr",
        "routing_name": "thresh",
        "display_agent": "codex",
        "task_id": TASK_ID,
        "repository": REPOSITORY,
        "issue": 19,
        "branch": "agent/synthetic/19",
        "worktree": champion_worktree,
        "task": "Synthetic storage task",
        "status": "progress",
        "updated_at": AT2,
        "update": "Synthetic progress is durable.",
        "blocker": None,
        "next": "Run synthetic verification.",
    }
    write_json(
        root / "rosters/Garen/status.json",
        {
            "callsign": "Garen",
            "role": "shotcaller",
            "shotcaller": None,
            "kind": "codex-thread",
            "address": "w1:p1",
            "thread_id": SHOTCALLER_ID,
            "task": "Synthetic coordination",
            "status": "working",
            "updated_at": AT2,
            "update": "Coordinating synthetic work.",
            "blocker": None,
            "next": "Receive synthetic delivery.",
        },
    )
    write_json(root / "rosters/Garen/champions/Thresh/status.json", champion_status)
    update1 = stable_json(
        {"at": AT1, "status": "working", "update": "Synthetic assignment accepted."}
    )
    update2 = stable_json(
        {"at": AT2, "status": "progress", "update": "Synthetic progress is durable."}
    )
    updates_data = f"{update1}\n{update2}\n"
    updates_path = root / "rosters/Garen/champions/Thresh/updates.jsonl"
    updates_path.parent.mkdir(parents=True, exist_ok=True)
    updates_path.write_text(updates_data, encoding="utf-8")
    second_offset = len((update1 + "\n").encode("utf-8"))
    return {
        "runtime_root": runtime_root,
        "original_updates": original_updates,
        "champion_record": champion_record,
        "champion_status_path": champion_status_path,
        "champion_worktree": champion_worktree,
        "champion_status": champion_status,
        "updates_data": updates_data,
        "first_event_digest": event_digest(original_updates, 0, update1),
        "second_event_digest": event_digest(original_updates, second_offset, update2),
        "second_offset": second_offset,
    }


def _write_callsign_pools(root: Path) -> None:
    write_json(
        root / "league-champions.json",
        {
            "available": {"shotcaller": [], "champion": ["Lux"]},
            "in_use": {
                "Garen": {"role": "shotcaller", "task_id": "synthetic-coordination"},
                "Thresh": {"role": "champion", "task_id": TASK_ID},
                "Pyke": {
                    "role": "champion",
                    "task_id": "synthetic-pending",
                    "pending": True,
                },
            },
        },
    )
    write_json(
        root / "scientists.json",
        {
            "schema": 1,
            "available": ["Curie"],
            "active": {
                "Turing": {
                    "callsign": "Turing",
                    "role": "hidden-worker",
                    "owner": "Garen",
                    "worker_id": "synthetic-worker-1",
                    "model": "synthetic-model",
                    "effort": "high",
                    "routing_reason": "Synthetic bounded analysis.",
                    "status": "working",
                }
            },
        },
    )


def _write_pending_launch(root: Path, context: dict[str, Any]) -> None:
    runtime_root = context["runtime_root"]
    write_json(
        root / "pending-launches/synthetic-pending.json",
        {
            "schema": 1,
            "task_id": "synthetic-pending",
            "callsign": "Pyke",
            "routing_name": "pyke",
            "display_agent": "codex",
            "address": "w1:p3",
            "pool": "champion",
            "record": str(runtime_root / "rosters/Garen/champions/Pyke"),
            "herdr_session": "synthetic-session",
            "attempt_id": "attempt-synthetic-1",
            "phase": "started",
            "repository": REPOSITORY,
            "issue": 19,
            "branch": "agent/synthetic/pending",
            "worktree": str(runtime_root / "worktrees/pending"),
            "started_at": AT1,
            "runtime_generation": "generation-synthetic-1",
        },
    )


def _write_watcher_state(root: Path, context: dict[str, Any]) -> None:
    digest = context["second_event_digest"]
    candidate = {
        "event": "champion-update",
        "event_id": digest,
        "record": context["champion_record"],
        "source_path": context["original_updates"],
        "source_offset": context["second_offset"],
        "callsign": "Thresh",
        "shotcaller": "Garen",
        "status": "progress",
        "at": AT2,
        "update": "Synthetic progress is durable.",
    }
    write_json(
        root / "watcher/Garen/state.json",
        {
            "schema": 2,
            "enabled": True,
            "allow_stop_once": False,
            "stop_blocked": False,
            "generation": 2,
            "initialized": True,
            "last_active": [context["champion_status_path"]],
            "offsets": {
                context["original_updates"]: len(context["updates_data"].encode("utf-8"))
            },
            "seen": [context["first_event_digest"], digest],
            "user_message_generation": 1,
            "wait_active": False,
            "wait_generation": 3,
            "wait_pid": None,
            "wait_process_start": None,
            "pending_events": {digest: candidate},
            "delivered_events": {
                context["first_event_digest"]: {"channel": "watcher"}
            },
            "last_event_id": digest,
            "reconciliation": {
                context["champion_record"]: {
                    "condition": "missing",
                    "count": 2,
                    "record_updated_at": AT2,
                    "evidence": {"display": "codex"},
                }
            },
        },
    )


def _write_coordination_artifacts(root: Path) -> None:
    write_json(root / "relay/Garen.json", {"delivered": ["a" * 64]})
    write_json(
        root / "task-resources.json",
        {
            "schema": 1,
            "resources": {
                "synthetic-process": {
                    "kind": "process",
                    "task_id": TASK_ID,
                    "owner": "Thresh",
                    "endpoint": "synthetic:endpoint",
                    "generation": "synthetic-generation",
                    "pid": 4242,
                    "process_start": "Thu Jan  1 00:00:00 2026",
                }
            },
            "shared_agent_chrome": {
                "owners": [
                    {
                        "task_id": TASK_ID,
                        "owner": "Thresh",
                        "generation": "chrome-generation",
                    }
                ]
            },
        },
    )
    evidence = root / "archives/synthetic-evidence.txt"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("Synthetic immutable evidence.\n", encoding="utf-8")


def _write_manifest(root: Path) -> Path:
    manifest = {
        "schema": 1,
        "captured_at": AT2,
        "canonical_sources": {
            "rosters": [
                {
                    "artifact_id": "R1-shotcaller",
                    "status": "rosters/Garen/status.json",
                    "updates": None,
                },
                {
                    "artifact_id": "R2-R3-champion",
                    "status": "rosters/Garen/champions/Thresh/status.json",
                    "updates": "rosters/Garen/champions/Thresh/updates.jsonl",
                },
            ],
            "pending_launches": [
                {"artifact_id": "L1-pending", "path": "pending-launches/synthetic-pending.json"}
            ],
            "watcher_states": [
                {"artifact_id": "W1-Garen", "path": "watcher/Garen/state.json"}
            ],
            "visible_callsign_pools": [
                {"artifact_id": "C1-visible", "path": "league-champions.json"}
            ],
            "hidden_worker_pools": [
                {"artifact_id": "C2-hidden", "path": "scientists.json"}
            ],
            "lead_relay_states": [
                {"artifact_id": "D4-relay", "path": "relay/Garen.json"}
            ],
            "resource_registries": [
                {"artifact_id": "P5-resources", "path": "task-resources.json"}
            ],
        },
        "retained_files": [
            {
                "artifact_id": "T5-evidence",
                "class": "archive-evidence",
                "path": "archives/synthetic-evidence.txt",
            }
        ],
        "unknown_consumers": [],
    }
    path = root / "import-manifest.json"
    write_json(path, manifest)
    return path


def write_complete_fixture(
    root: Path, *, runtime_root: Path | None = None
) -> dict[str, Any]:
    """Compose every canonical family plus one retained evidence file."""
    context = _write_roster_records(root, runtime_root)
    _write_callsign_pools(root)
    _write_pending_launch(root, context)
    _write_watcher_state(root, context)
    _write_coordination_artifacts(root)
    context.update(
        {
            "manifest": _write_manifest(root),
            "private_marker": str(context["runtime_root"]),
        }
    )
    return context
