#!/usr/bin/env python3
"""Dependency-free checks for the public schemas, config, and examples."""

from __future__ import annotations

import json
import hashlib
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
    assert routing["schema"] == 2 and routing["tiers"]
    assert routing["policy"] == {
        "quality_baseline": "WORKER_STRONG",
        "safe_boundary_escalations": 1,
    }
    assert routing["evaluations"]["WORKER_FAST"]["approved"] is False
    for tier in routing["tiers"].values():
        assert set(tier) == {"model", "effort"}
        assert all(isinstance(value, str) and value for value in tier.values())

    skill_contract = load_json(ROOT / "config" / "custom-skills.json")
    skill_profile = load_json(ROOT / "config" / "skill-runtime.example.json")
    skill_audit = load_json(ROOT / "docs" / "research" / "custom-skill-audit.json")
    canonical_skill_contract = json.dumps(
        skill_contract, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert skill_contract["schema"] == "league.skill-contracts.v1"
    assert skill_profile["schema"] == "league.skill-runtime-profile.v1"
    assert skill_audit["schema"] == "league.skill-audit.v1"
    assert skill_audit["contract_sha256"] == hashlib.sha256(canonical_skill_contract).hexdigest()
    assert skill_audit["local_paths_included"] is False
    assert skill_audit["skill_bodies_included"] is False
    assert skill_audit["summary"] == {
        "roots": 2,
        "skills": 23,
        "copies": 25,
        "shared": 10,
        "specialist": 13,
        "recorded": 10,
        "unrecorded": 13,
    }
    assert [(item["skill"], item["root"], item["content_sha256"]) for item in skill_audit["copies"]] == [
        (item["skill"], item["root"], item["content_sha256"])
        for item in skill_contract["installations"]
    ]
    skill_definitions = {item["identity"]: item for item in skill_contract["skills"]}
    assert skill_definitions["research"]["fallback"] == {
        "mode": "inline",
        "when_missing": ["delegation.background-visible-agents"],
        "when_available": "delegate",
    }

    for name in (
        "agent-status.schema.json",
        "agent-update.schema.json",
        "agent-routing.schema.json",
        "league-skill-contracts.schema.json",
        "league-skill-runtime-profile.schema.json",
        "league-skill-validation.schema.json",
        "league-skill-audit.schema.json",
        "league-skill-matrix.schema.json",
        "league-activity-evidence.schema.json",
        "league-report.schema.json",
        "league-outbound-receipt.schema.json",
        "league-project-catalog.schema.json",
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
