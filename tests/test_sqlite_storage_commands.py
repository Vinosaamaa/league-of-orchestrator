#!/usr/bin/env python3
"""Focused in-process command facade and one launcher-boundary smoke test."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LEAGUE = ROOT / "bin/league"
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import league.cli as cli  # noqa: E402
from storage_fixture import (  # noqa: E402
    CHAMPION_ID,
    REPOSITORY,
    SHOTCALLER_ID,
    TASK_ID,
    write_complete_fixture,
)
from storage_test_support import invoke_cli, seeded_state  # noqa: E402


AT3 = "2026-01-01T00:02:00Z"
AT4 = "2026-01-01T00:03:00Z"
AT5 = "2026-01-01T00:04:00Z"
NEW_AGENT_ID = "33333333-3333-4333-8333-333333333333"


def success(payload: dict[str, Any], command: str) -> Any:
    assert payload["schema"] == "league.command.v1"
    assert payload["ok"] is True and payload["command"] == command
    assert set(payload) == {"schema", "ok", "command", "result"}
    return payload["result"]


def refusal(payload: dict[str, Any], command: str, code: str) -> None:
    assert payload["schema"] == "league.command.v1"
    assert payload["ok"] is False and payload["command"] == command
    assert payload["error"]["code"] == code
    assert isinstance(payload["error"]["retryable"], bool)


def test_launcher_help_and_schemas() -> None:
    launcher = subprocess.run(
        [str(LEAGUE), "--help"], text=True, capture_output=True, check=True, timeout=10
    )
    assert "SQL is not exposed" in " ".join(launcher.stdout.split())
    assert (
        "{storage,agent,callsign,delivery,project,task,runtime,routing,resource,cleanup,acceptance}"
        in launcher.stdout
    )
    parser = cli._parser()
    groups = next(action for action in parser._actions if getattr(action, "choices", None))
    storage_help = groups.choices["storage"].format_help()
    assert all(name in storage_help for name in ("migrate", "integrity", "backup", "export", "import"))
    for name in (
        "league-command-output.schema.json",
        "league-import-report.schema.json",
        "league-export.schema.json",
        "league-acceptance-receipt.schema.json",
    ):
        schema = json.loads((ROOT / "schema" / name).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
    report_schema = json.loads(
        (ROOT / "schema/league-import-report.schema.json").read_text(encoding="utf-8")
    )
    retained = report_schema["properties"]["retained_files"]["items"]
    assert set(retained["required"]) == {"artifact_id", "class", "digest", "bytes"}
    assert retained["additionalProperties"] is False
    audit = report_schema["properties"]["audit_coverage"]
    assert len(audit["required"]) == 40 and audit["additionalProperties"] is False
    missing_state = io.BytesIO()
    code = cli.main(
        ["agent", "status", "--agent-id", "synthetic-missing-root"],
        output=missing_state,
    )
    assert code == 2
    assert json.loads(missing_state.getvalue())["error"]["code"] == "state_root_required"


def test_storage_migrate_and_import(root: Path) -> None:
    source, state = root / "source", root / "state"
    source.mkdir(parents=True)
    state.mkdir()
    fixture = write_complete_fixture(source)
    migration = success(invoke_cli(state, "storage", "migrate"), "storage.migrate")
    assert migration["to_version"] == 3 and migration["policy"]["foreign_keys"] is True
    assert os.stat(state / "league.sqlite3").st_mode & 0o777 == 0o600
    dry_run = success(
        invoke_cli(
            state,
            "storage",
            "import",
            "--source-root",
            str(source),
            "--manifest",
            str(fixture["manifest"]),
        ),
        "storage.import",
    )
    assert dry_run["dry_run"] and dry_run["eligible"]
    applied = success(
        invoke_cli(
            state,
            "storage",
            "import",
            "--source-root",
            str(source),
            "--manifest",
            str(fixture["manifest"]),
            "--apply",
            "--expected-digest",
            dry_run["report_digest"],
        ),
        "storage.import",
    )
    assert applied["applied"]


def test_agent_and_delivery_commands(root: Path) -> None:
    _, state, _ = seeded_state(root, "agent-delivery")
    status = success(
        invoke_cli(state, "agent", "status", "--agent-id", CHAMPION_ID), "agent.status"
    )
    assert status["found"] and status["agent"]["version"] == 2
    transition = success(
        invoke_cli(
            state,
            "agent",
            "transition",
            "--agent-id",
            CHAMPION_ID,
            "--expected-version",
            "2",
            "--status",
            "blocked",
            "--update",
            "Synthetic command transition.",
            "--at",
            AT3,
        ),
        "agent.transition",
    )
    stale = invoke_cli(
        state,
        "agent",
        "transition",
        "--agent-id",
        CHAMPION_ID,
        "--expected-version",
        "2",
        "--status",
        "failed",
        "--update",
        "Stale synthetic transition.",
        "--at",
        AT3,
        expected=2,
    )
    refusal(stale, "agent.transition", "version_conflict")
    claim_args = (
        "delivery",
        "claim",
        "--event-id",
        transition["event_id"],
        "--recipient-agent-id",
        SHOTCALLER_ID,
    )
    first = success(
        invoke_cli(
            state,
            *claim_args,
            "--claim-token",
            "claim-one",
            "--claim-expires-at",
            AT4,
            "--at",
            AT3,
        ),
        "delivery.claim",
    )
    assert first["attempt"] == 1 and not first["idempotent"]
    identical = success(
        invoke_cli(
            state,
            *claim_args,
            "--claim-token",
            "claim-one",
            "--claim-expires-at",
            AT4,
            "--at",
            AT3,
        ),
        "delivery.claim",
    )
    assert identical["attempt"] == 1 and identical["idempotent"]
    active = invoke_cli(
        state,
        *claim_args,
        "--claim-token",
        "claim-two",
        "--claim-expires-at",
        AT5,
        "--at",
        AT3,
        expected=2,
    )
    refusal(active, "delivery.claim", "delivery_conflict")
    reclaimed = success(
        invoke_cli(
            state,
            *claim_args,
            "--claim-token",
            "claim-two",
            "--claim-expires-at",
            AT5,
            "--at",
            AT4,
        ),
        "delivery.claim",
    )
    assert reclaimed["attempt"] == 2 and not reclaimed["idempotent"]
    acknowledged = success(
        invoke_cli(
            state,
            "delivery",
            "ack",
            "--event-id",
            transition["event_id"],
            "--recipient-agent-id",
            SHOTCALLER_ID,
            "--claim-token",
            "claim-two",
            "--at",
            AT5,
        ),
        "delivery.ack",
    )
    assert acknowledged["state"] == "acknowledged"


def reservation_args() -> tuple[str, ...]:
    return (
        "callsign",
        "reserve",
        "--callsign",
        "Lux",
        "--agent-id",
        NEW_AGENT_ID,
        "--task-id",
        TASK_ID,
        "--role",
        "champion",
        "--status",
        "working",
        "--update",
        "Synthetic reservation.",
        "--at",
        AT3,
    )


def test_callsign_project_and_task_commands(root: Path) -> None:
    _, state, _ = seeded_state(root, "identity-owner")
    project = success(
        invoke_cli(state, "project", "resolve", "--repository", REPOSITORY),
        "project.resolve",
    )
    assert project["found"] and project["project"]["repository"] == REPOSITORY
    owner = success(
        invoke_cli(
            state,
            "task",
            "transfer-owner",
            "--task-id",
            TASK_ID,
            "--expected-version",
            "1",
            "--owner-kind",
            "squad",
            "--owner-id",
            "squad:Garen",
            "--at",
            AT3,
        ),
        "task.transfer-owner",
    )
    assert owner["owner"] == {"kind": "squad", "id": "squad:Garen"}
    first = success(invoke_cli(state, *reservation_args()), "callsign.reserve")
    assert first["version"] == 1 and not first["idempotent"]
    assert success(invoke_cli(state, *reservation_args()), "callsign.reserve")["idempotent"]
    mismatched = list(reservation_args())
    mismatched[mismatched.index("Synthetic reservation.")] = "Different retry payload."
    refusal(
        invoke_cli(state, *mismatched, expected=2),
        "callsign.reserve",
        "reservation_mismatch",
    )
    released = success(
        invoke_cli(
            state,
            "callsign",
            "release",
            "--callsign",
            "Lux",
            "--agent-id",
            NEW_AGENT_ID,
            "--expected-version",
            "1",
            "--at",
            AT4,
        ),
        "callsign.release",
    )
    assert released["version"] == 2


def test_admin_export_and_operational_envelope(root: Path) -> None:
    _, state, _ = seeded_state(root, "admin")
    assert success(invoke_cli(state, "storage", "integrity"), "storage.integrity")["ok"]
    backup = success(
        invoke_cli(state, "storage", "backup", "--name", "backups/command.sqlite3"),
        "storage.backup",
    )
    assert backup["integrity"] == "ok" and "path" not in backup
    receipt = success(
        invoke_cli(
            state,
            "storage",
            "export",
            "--format",
            "jsonl",
            "--purpose",
            "rollback",
            "--output-name",
            "exports/rollback.jsonl",
        ),
        "storage.export",
    )
    assert receipt["purpose"] == "rollback" and "path" not in receipt
    assert os.stat(state / "exports/rollback.jsonl").st_mode & 0o777 == 0o600

    original = cli._run
    try:
        cli._run = lambda _: (_ for _ in ()).throw(OSError("synthetic"))
        output = io.BytesIO()
        code = cli.main(
            ["--state-root", str(state), "storage", "integrity"], output=output
        )
    finally:
        cli._run = original
    assert code == 2
    refusal(json.loads(output.getvalue()), "storage.integrity", "operation_failed")


def main() -> None:
    test_launcher_help_and_schemas()
    with tempfile.TemporaryDirectory(prefix="league-storage-command-") as temporary:
        root = Path(temporary)
        test_storage_migrate_and_import(root / "bootstrap")
        test_agent_and_delivery_commands(root)
        test_callsign_project_and_task_commands(root)
        test_admin_export_and_operational_envelope(root)
    print("PASS: focused CLI groups, schemas, envelopes, lease recovery, and launcher smoke")


if __name__ == "__main__":
    main()
