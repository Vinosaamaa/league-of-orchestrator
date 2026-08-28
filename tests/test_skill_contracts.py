#!/usr/bin/env python3
"""Synthetic provenance, parity, fallback, CLI, and privacy regressions."""

from __future__ import annotations

import copy
import io
import json
import re
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from league import cli  # noqa: E402
from league.adapters import builtin_contract_registry  # noqa: E402
from league.skill_contracts import (  # noqa: E402
    _scan_root,
    audit_installations,
    capability_matrix,
    validate_contract,
)
from league.storage import StorageRefusal  # noqa: E402


DIMENSIONS = ("harness", "tool", "platform", "browser", "forge", "delegation", "multiplexer")
PUBLIC_SKILL_FILES = (
    "Makefile",
    "README.md",
    "config/custom-skills.json",
    "config/skill-runtime.example.json",
    "docs/ACCEPTANCE.md",
    "docs/ARCHITECTURE.md",
    "docs/PROVENANCE.md",
    "docs/ROADMAP.md",
    "docs/runtime-lifecycle.md",
    "docs/skill-capabilities.md",
    "docs/research/custom-skill-audit.json",
    "schema/league-skill-audit.schema.json",
    "schema/league-skill-contracts.schema.json",
    "schema/league-skill-matrix.schema.json",
    "schema/league-skill-runtime-profile.schema.json",
    "schema/league-skill-validation.schema.json",
    "src/league/cli.py",
    "src/league/skill_contracts.py",
    "tests/test_request_lifecycle_cli.py",
    "tests/test_schema_examples.py",
    "tests/test_skill_contracts.py",
    "tests/test_sqlite_storage_commands.py",
)


def capabilities(**values: list[str]) -> dict[str, list[str]]:
    return {dimension: sorted(values.get(dimension, [])) for dimension in DIMENSIONS}


def write_skill(root: Path, identity: str, body: str) -> None:
    skill = root / identity
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(body, encoding="utf-8")


def fixture(root: Path) -> tuple[dict[str, object], dict[str, Path], dict[str, object]]:
    agents = root / "agents-root"
    codex = root / "codex-root"
    agents.mkdir(parents=True)
    codex.mkdir()
    write_skill(agents, "research", "synthetic research body must never appear in audit output\n")
    write_skill(agents, "terminal-browser", "synthetic terminal browser copy one\n")
    write_skill(codex, "herdr", "synthetic herdr body\n")
    write_skill(codex, "terminal-browser", "synthetic terminal browser copy two\n")
    scanned = {
        (item["skill"], label): item
        for label, path in (("agents-custom", agents), ("codex-custom", codex))
        for item in _scan_root(path)
    }
    contract = {
        "schema": "league.skill-contracts.v1",
        "roots": [{"label": "agents-custom"}, {"label": "codex-custom"}],
        "skills": [
            {
                "identity": "herdr",
                "scope": "specialist",
                "provenance": {"classification": "recorded", "owner": "Synthetic/skills"},
                "version": {"classification": "declared", "value": "1.0.0"},
                "capabilities": {
                    "required": capabilities(multiplexer=["herdr"]),
                    "optional": capabilities(),
                },
                "fallback": {"mode": "refuse", "when_missing": [], "when_available": "declared"},
            },
            {
                "identity": "research",
                "scope": "shared",
                "provenance": {
                    "classification": "unrecorded",
                    "reason": "no-authoritative-source-record",
                },
                "version": {"classification": "unrecorded"},
                "capabilities": {
                    "required": capabilities(),
                    "optional": capabilities(
                        tool=["web-search"], delegation=["background-visible-agents"]
                    ),
                },
                "fallback": {
                    "mode": "inline",
                    "when_missing": ["delegation.background-visible-agents"],
                    "when_available": "delegate",
                },
            },
            {
                "identity": "terminal-browser",
                "scope": "specialist",
                "provenance": {
                    "classification": "unrecorded",
                    "reason": "no-authoritative-source-record",
                },
                "version": {"classification": "unrecorded"},
                "capabilities": {
                    "required": capabilities(browser=["terminal-browser"]),
                    "optional": capabilities(),
                },
                "fallback": {"mode": "refuse", "when_missing": [], "when_available": "declared"},
            },
        ],
        "installations": [
            {
                "root": "codex-custom",
                "skill": "herdr",
                "entry_kind": "directory",
                "content_sha256": scanned[("herdr", "codex-custom")]["content_sha256"],
                "source_parity": "matched",
            },
            {
                "root": "agents-custom",
                "skill": "research",
                "entry_kind": "directory",
                "content_sha256": scanned[("research", "agents-custom")]["content_sha256"],
                "source_parity": "unverified",
            },
            {
                "root": "agents-custom",
                "skill": "terminal-browser",
                "entry_kind": "directory",
                "content_sha256": scanned[("terminal-browser", "agents-custom")]["content_sha256"],
                "source_parity": "unverified",
            },
            {
                "root": "codex-custom",
                "skill": "terminal-browser",
                "entry_kind": "directory",
                "content_sha256": scanned[("terminal-browser", "codex-custom")]["content_sha256"],
                "source_parity": "unverified",
            },
        ],
    }
    profile = {
        "schema": "league.skill-runtime-profile.v1",
        "adapter": {"harness": "codex", "backend": "herdr"},
        "capabilities": capabilities(tool=["web-search"], multiplexer=["herdr"]),
    }
    return contract, {"agents-custom": agents, "codex-custom": codex}, profile


def refused(operation, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def cli_result(arguments: list[str], expected: int = 0) -> dict[str, object]:
    output = io.BytesIO()
    assert cli.main(arguments, output=output) == expected, output.getvalue()
    return json.loads(output.getvalue())


def test_contract_audit_and_privacy(root: Path) -> None:
    contract, bindings, _ = fixture(root)
    validation = validate_contract(contract)
    assert validation["summary"] == {
        "roots": 2,
        "skills": 3,
        "copies": 4,
        "shared": 1,
        "specialist": 2,
        "recorded": 1,
        "unrecorded": 2,
    }
    assert validation["duplicates"] == [
        {
            "skill": "terminal-browser",
            "copies": 2,
            "source_owner": "unrecorded",
            "install_parity": "mismatched",
        }
    ]
    audit = audit_installations(contract, bindings)
    encoded = json.dumps(audit, sort_keys=True)
    assert audit["local_paths_included"] is False
    assert audit["skill_bodies_included"] is False
    assert str(root) not in encoded
    assert "synthetic research body" not in encoded
    (bindings["agents-custom"] / "research" / "SKILL.md").write_text(
        "changed synthetic body\n", encoding="utf-8"
    )
    refused(lambda: audit_installations(contract, bindings), "skill_install_parity_mismatch")


def test_capability_matrix_inline_delegate_and_specialists(root: Path) -> None:
    contract, _, profile = fixture(root)
    matrix = capability_matrix(contract, profile, builtin_contract_registry())
    skills = {item["skill"]: item for item in matrix["skills"]}
    assert matrix["adapter"]["availability"] == "contract-only"
    assert skills["research"]["status"] == "available_inline"
    assert skills["research"]["execution"] == "inline"
    assert skills["herdr"]["status"] == "available"
    assert skills["terminal-browser"]["status"] == "unavailable"
    delegated = copy.deepcopy(profile)
    delegated["capabilities"]["delegation"] = ["background-visible-agents"]
    delegated_matrix = capability_matrix(contract, delegated, builtin_contract_registry())
    research = next(item for item in delegated_matrix["skills"] if item["skill"] == "research")
    assert research["status"] == "available" and research["execution"] == "delegate"


def test_contract_refusals(root: Path) -> None:
    contract, bindings, _ = fixture(root)
    invalid = copy.deepcopy(contract)
    research = next(item for item in invalid["skills"] if item["identity"] == "research")
    research["fallback"]["when_missing"] = []
    refused(lambda: validate_contract(invalid), "skill_contract_invalid")
    refused(
        lambda: audit_installations(contract, {"agents-custom": bindings["agents-custom"]}),
        "skill_root_mismatch",
    )
    nested_target = root / "nested-target"
    nested_target.mkdir()
    (nested_target / "data.txt").write_text("synthetic nested target\n", encoding="utf-8")
    (bindings["agents-custom"] / "research" / "nested").symlink_to(
        nested_target, target_is_directory=True
    )
    refused(lambda: _scan_root(bindings["agents-custom"]), "skill_tree_unsafe")


def test_cli_and_repository_contract(root: Path) -> None:
    contract, bindings, profile = fixture(root)
    contract_path = root / "contract.json"
    profile_path = root / "profile.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    validated = cli_result(["skill", "validate", "--config", str(contract_path)])
    assert validated["ok"] is True and validated["command"] == "skill.validate"
    audited = cli_result(
        [
            "skill",
            "audit",
            "--config",
            str(contract_path),
            "--root",
            f"agents-custom={bindings['agents-custom']}",
            "--root",
            f"codex-custom={bindings['codex-custom']}",
        ]
    )
    assert audited["result"]["local_paths_included"] is False
    matrix = cli_result(
        ["skill", "matrix", "--config", str(contract_path), "--profile", str(profile_path)]
    )
    assert matrix["result"]["schema"] == "league.skill-matrix.v1"
    state_refusal = cli_result(
        [
            "--state-root",
            str(root / "state"),
            "skill",
            "validate",
            "--config",
            str(contract_path),
        ],
        expected=2,
    )
    assert state_refusal["error"]["code"] == "invalid_skill_state_root"

    repository_contract = json.loads(
        (ROOT / "config" / "custom-skills.json").read_text(encoding="utf-8")
    )
    repository_validation = validate_contract(repository_contract)
    assert repository_validation["summary"] == {
        "roots": 2,
        "skills": 23,
        "copies": 25,
        "shared": 10,
        "specialist": 13,
        "recorded": 10,
        "unrecorded": 13,
    }
    duplicates = {item["skill"]: item["install_parity"] for item in repository_validation["duplicates"]}
    assert duplicates == {"frontend-design": "matched", "terminal-browser": "mismatched"}
    definitions = {item["identity"]: item for item in repository_contract["skills"]}
    assert definitions["research"]["fallback"] == {
        "mode": "inline",
        "when_missing": ["delegation.background-visible-agents"],
        "when_available": "delegate",
    }
    for identity in ("herdr", "lavish-transcript", "spring-boot-engineer", "terminal-browser"):
        assert definitions[identity]["scope"] == "specialist"


def test_repository_public_safety() -> None:
    payload = b"\n".join((ROOT / relative).read_bytes() for relative in PUBLIC_SKILL_FILES)
    for forbidden in (
        b"/" + b"Users" + b"/",
        b"/" + b"home" + b"/",
        b"file" + b"://",
        b"BEGIN " + b"PRIVATE KEY",
        b"github" + b"_pat_",
        b"gh" + b"p_",
    ):
        assert forbidden not in payload
    assert re.search(rb"\bsk-[A-Za-z0-9_-]{20,}\b", payload) is None
    inventory_payload = b"\n".join(
        (ROOT / relative).read_bytes()
        for relative in (
            "config/custom-skills.json",
            "config/skill-runtime.example.json",
            "docs/research/custom-skill-audit.json",
            "docs/skill-capabilities.md",
        )
    )
    assert re.search(
        rb"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        inventory_payload,
        re.IGNORECASE,
    ) is None


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-skill-contracts-") as temporary:
        root = Path(temporary)
        test_contract_audit_and_privacy(root / "audit")
        test_capability_matrix_inline_delegate_and_specialists(root / "matrix")
        test_contract_refusals(root / "refusals")
        test_cli_and_repository_contract(root / "cli")
        test_repository_public_safety()
    print(
        "PASS: synthetic skill provenance, sanitized roots, deterministic duplicate parity, "
        "runtime capability fallback, specialist boundaries, and public-safety bytes"
    )


if __name__ == "__main__":
    main()
