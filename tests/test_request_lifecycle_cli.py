#!/usr/bin/env python3
"""Machine-readable lifecycle help, schema, and command facade coverage."""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league import cli  # noqa: E402
from league.sqlite_store import MAX_EXPORT_PAYLOAD_BYTES  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from league.storage_request import MAX_TRIAGE_JSON_BYTES  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    SyntheticLifecycleSeeder,
    create_context,
)
from storage_fixture import SHOTCALLER_ID  # noqa: E402
from storage_test_support import invoke_cli  # noqa: E402


def success(payload, command):
    assert payload["schema"] == "league.command.v1"
    assert payload["ok"] is True and payload["command"] == command
    return payload["result"]


def test_help_inventory_and_schemas() -> None:
    output = io.BytesIO()
    assert cli.main(["help", "inventory"], output=output) == 0
    inventory = success(json.loads(output.getvalue()), "help.inventory")
    required = {
        "request.intake",
        "request.triage",
        "request.claim",
        "request.release",
        "request.dispatch",
        "request.route",
        "request.accept",
        "request.awaiting-user",
        "request.block",
        "request.defer",
        "request.cancel",
        "request.result",
        "request.answer",
        "request.unresolved",
        "assign.prepare",
        "assign.launching",
        "assign.activate",
        "assign.block",
        "task.transition",
        "delivery.claim-outbox",
        "delivery.ack-outbox",
        "hook.stop",
        "skill.validate",
        "skill.audit",
        "skill.matrix",
    }
    assert required <= set(inventory["commands"])
    assert {f"request.{name}" for name in cli.REQUEST_STATE_COMMANDS} <= set(
        inventory["commands"]
    )
    assert inventory["lease_kinds"] == [
        "request_claim",
        "outbox_dispatch",
        "watcher_registration",
    ]
    assert {
        "league-skill-contracts.schema.json",
        "league-skill-runtime-profile.schema.json",
        "league-skill-validation.schema.json",
        "league-skill-audit.schema.json",
        "league-skill-matrix.schema.json",
    } <= set(inventory["schemas"])
    for name in inventory["schemas"]:
        schema = json.loads((ROOT / "schema" / name).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema.get("additionalProperties") is False or schema["type"] == "array"


def capture_cli_request(state: Path, clock) -> None:
    intake = success(
        invoke_cli(
            state,
            "request",
            "intake",
            "--prompt-id",
            "prompt-cli",
            "--intake-actor-id",
            SHOTCALLER_ID,
            "--runtime-instance-id",
            GAREN_RUNTIME,
            "--adapter-kind",
            "codex",
            "--session-ref",
            "session:cli",
            "--source-event-key",
            "source:cli",
            "--body",
            "Implement one synthetic CLI request",
            "--at",
            clock.now(),
        ),
        "request.intake",
    )
    assert intake["prompt_id"] == "prompt-cli"
    items = json.dumps(
        [
            {
                "prompt_item_id": "item-cli",
                "ordinal": 1,
                "summary": "Implement synthetic CLI request",
                "disposition": "new_request",
                "request_id": "request-cli",
            }
        ],
        separators=(",", ":"),
    )
    triage = success(
        invoke_cli(
            state,
            "request",
            "triage",
            "--prompt-id",
            "prompt-cli",
            "--items-json",
            items,
            "--at",
            clock.now(),
        ),
        "request.triage",
    )
    assert triage["request_count"] == 1


def dispatch_cli_request(state: Path, clock) -> None:
    success(
        invoke_cli(
            state,
            "request",
            "claim",
            "--request-id",
            "request-cli",
            "--runtime-instance-id",
            GAREN_RUNTIME,
            "--claim-token",
            "claim-cli",
            "--leased-until",
            clock.after(120),
            "--at",
            clock.now(),
        ),
        "request.claim",
    )
    dispatch = success(
        invoke_cli(
            state,
            "request",
            "dispatch",
            "--request-id",
            "request-cli",
            "--claim-token",
            "claim-cli",
            "--dispatch-id",
            "dispatch-cli",
            "--work-kind",
            "repository-write",
            "--requested-mode",
            "champion",
            "--at",
            clock.now(),
        ),
        "request.dispatch",
    )
    assert dispatch["execution_mode"] == "champion"


def dispatched_cli_state(root: Path, name: str):
    state, store, clock = create_context(root, name)
    store.close()
    capture_cli_request(state, clock)
    dispatch_cli_request(state, clock)
    return state, clock


def test_request_commands(root: Path) -> None:
    state, _ = dispatched_cli_state(root, "cli-request")
    unresolved = success(
        invoke_cli(
            state,
            "request",
            "unresolved",
            "--owner-agent-id",
            SHOTCALLER_ID,
            "--before-action",
            "handoff",
        ),
        "request.unresolved",
    )
    assert not unresolved["safe_to_finish"]


def test_stop_command(root: Path) -> None:
    state, clock = dispatched_cli_state(root, "cli-stop")
    stop = success(
        invoke_cli(
            state,
            "hook",
            "stop",
            "--scope-id",
            "Garen-cli",
            "--actor-agent-id",
            SHOTCALLER_ID,
            "--terminal-generation",
            "terminal:cli",
            "--at",
            clock.now(),
        ),
        "hook.stop",
    )
    assert stop["status"] == "blocked_once" and stop["decision"] == "block"


def test_triage_refuses_oversized_json_before_decode(root: Path) -> None:
    state, store, clock = create_context(root, "cli-triage-bound")
    store.intake_prompt(
        "prompt-cli-bound",
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        "session:cli-bound",
        "source:cli-bound",
        "Synthetic bounded triage prompt",
        clock.now(),
    )
    store.close()
    payload = invoke_cli(
        state,
        "request",
        "triage",
        "--prompt-id",
        "prompt-cli-bound",
        "--items-json",
        "[" + (" " * MAX_TRIAGE_JSON_BYTES) + "not-json",
        "--at",
        clock.now(),
        expected=2,
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_json"
    assert "bounded encoded size" in payload["error"]["message"]


def test_export_refuses_prompt_payloads_over_byte_budget(root: Path) -> None:
    _, store, clock = create_context(root, "export-payload-bound")
    store.intake_prompt(
        "prompt-export-bound",
        SHOTCALLER_ID,
        GAREN_RUNTIME,
        "codex",
        "session:export-bound",
        "source:export-bound",
        "Synthetic export payload",
        clock.now(),
    )
    SyntheticLifecycleSeeder(store, clock).set_prompt_payload_body(
        "prompt-export-bound", "x" * (MAX_EXPORT_PAYLOAD_BYTES + 1)
    )
    try:
        store.export_bytes(format_name="json", purpose="rollback", max_records=10_000)
    except StorageRefusal as exc:
        assert exc.code == "export_payload_too_large"
    else:
        raise AssertionError("rollback export materialized an oversized prompt payload")
    store.close()


def main() -> None:
    test_help_inventory_and_schemas()
    with tempfile.TemporaryDirectory(prefix="league-request-cli-") as temporary:
        root = Path(temporary)
        test_request_commands(root)
        test_stop_command(root)
        test_triage_refuses_oversized_json_before_decode(root)
        test_export_refuses_prompt_payloads_over_byte_budget(root)
    print("PASS: machine-readable help/schemas and request/Stop CLI facade")


if __name__ == "__main__":
    main()
