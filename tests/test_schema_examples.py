#!/usr/bin/env python3
"""Dependency-free checks for the public schemas, config, and examples."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATUSES = {
    "active",
    "started",
    "working",
    "progress",
    "blocked",
    "completed",
    "complete",
    "cancelled",
    "canceled",
    "failed",
    "ready_to_land",
}


def strict_object(pairs):
    value = {}
    for key, item in pairs:
        assert key not in value, f"duplicate JSON key: {key}"
        value[key] = item
    return value


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=strict_object)


def main() -> None:
    status = load_json(ROOT / "examples" / "agent-status.example.json")
    update_lines = (ROOT / "examples" / "agent-updates.example.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(update_lines) == 1
    update = json.loads(update_lines[0], object_pairs_hook=strict_object)
    assert (status["status"], status["updated_at"], status["update"]) == (
        update["status"],
        update["at"],
        update["update"],
    )
    assert status["status"] in STATUSES
    assert status["routing_name"] == status["callsign"].lower()
    assert status["display_agent"]
    assert status["repository"].startswith("https://example.invalid/")
    assert status["worktree"].startswith("/example/")

    routing = load_json(ROOT / "config" / "agent-routing.example.json")
    assert routing["schema"] == 1 and routing["tiers"]
    for tier in routing["tiers"].values():
        assert set(tier) == {"model", "effort"}
        assert all(isinstance(value, str) and value for value in tier.values())

    for name in (
        "agent-status.schema.json",
        "agent-update.schema.json",
        "agent-routing.schema.json",
    ):
        schema = load_json(ROOT / "schema" / name)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"

    status_schema = load_json(ROOT / "schema" / "agent-status.schema.json")
    assert status_schema["dependentRequired"] == {
        "routing_name": ["display_agent"],
        "display_agent": ["routing_name"],
    }
    assert status_schema["properties"]["routing_name"]["pattern"] == (
        "^[a-z][a-z0-9_-]{0,31}$"
    )

    print("PASS: strict synthetic examples, latest-event parity, routing config, and JSON schemas")


if __name__ == "__main__":
    main()
