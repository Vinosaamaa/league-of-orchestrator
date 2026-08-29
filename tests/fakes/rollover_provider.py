#!/usr/bin/env python3
"""Stateless configured-provider double for focused rollover CLI tests."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def main() -> None:
    provider = sys.argv[-2]
    action = sys.argv[-1]
    request = json.load(sys.stdin)
    operation = request["operation"]
    assert request["schema"] == "league.rollover-provider-request.v1"
    marker_arguments = [item for item in sys.argv[1:-2] if item.startswith("--fail-once=")]
    if action == "acknowledge" and marker_arguments:
        marker = Path(marker_arguments[0].split("=", 1)[1])
        if not marker.exists():
            marker.write_text(request["idempotency_key"], encoding="utf-8")
            raise SystemExit(23)
        assert marker.read_text(encoding="utf-8") == request["idempotency_key"]
    if action == "acknowledge":
        startup = request["startup_context"]
        assert startup["runtime"]["harness_kind"] == provider
        response = {
            "schema": "league.rollover-provider-receipt.v1",
            "action": action,
            "verified": True,
            "operation_id": operation["operation_id"],
            "successor_agent_id": operation["successor_agent_id"],
            "runtime_instance_id": startup["runtime"]["runtime_instance_id"],
            "handoff_digest": operation["handoff_digest"],
            "snapshot_version": operation["snapshot"]["version"],
            "snapshot_count": operation["snapshot"]["count"],
            "snapshot_digest": operation["snapshot"]["digest"],
            "pages_digest": digest(request["bindings"]),
        }
    elif action == "abort":
        runtime = request["runtime"]
        assert runtime["harness_kind"] == provider
        response = {
            "schema": "league.rollover-provider-receipt.v1",
            "action": action,
            "verified": True,
            "operation_id": operation["operation_id"],
            "predecessor_agent_id": operation["predecessor_agent_id"],
            "successor_agent_id": operation["successor_agent_id"],
            "runtime": runtime,
            "runtime_cleanup_receipt_digest": digest(
                {"runtime": runtime["runtime_instance_id"]}
            ),
            "cleanup_digest": digest({"cleanup": operation["operation_id"], "action": action}),
        }
    elif action == "drain":
        runtime = request["runtime"]
        assert runtime["harness_kind"] == provider
        response = {
            "schema": "league.rollover-provider-receipt.v1",
            "action": action,
            "verified": True,
            "operation_id": operation["operation_id"],
            "predecessor_agent_id": operation["predecessor_agent_id"],
            "successor_agent_id": operation["successor_agent_id"],
            "owner_event_id": operation["owner_event_id"],
            "runtime": runtime,
            "archive_digest": digest({"archive": operation["operation_id"]}),
            "resource_receipt_digest": digest({"resources": operation["operation_id"]}),
            "callsign_release_receipt_digest": digest({"callsign": operation["operation_id"]}),
        }
    else:
        raise SystemExit(64)
    response["idempotency_key"] = request["idempotency_key"]
    json.dump(response, sys.stdout, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    main()
