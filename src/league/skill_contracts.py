"""Bounded provenance, installation-parity, and runtime-capability contracts.

The contract is intentionally repository-local.  Exact custom-root paths are
runtime inputs and are never returned in validation or audit output.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from .adapters import AdapterRegistry
from .storage_types import StorageRefusal


CONTRACT_SCHEMA = "league.skill-contracts.v1"
VALIDATION_SCHEMA = "league.skill-validation.v1"
AUDIT_SCHEMA = "league.skill-audit.v1"
PROFILE_SCHEMA = "league.skill-runtime-profile.v1"
MATRIX_SCHEMA = "league.skill-matrix.v1"
CAPABILITY_DIMENSIONS = (
    "harness",
    "tool",
    "platform",
    "browser",
    "forge",
    "delegation",
    "multiplexer",
)
MAX_CONTRACT_BYTES = 1_000_000
MAX_SKILL_FILES = 2_048
MAX_SKILL_FILE_BYTES = 16 * 1024 * 1024
MAX_SKILL_TREE_BYTES = 64 * 1024 * 1024
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
CAPABILITY = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
SOURCE_OWNER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
IGNORED_TREE_NAMES = frozenset({".DS_Store", ".git", "__pycache__"})


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise StorageRefusal("skill_contract_invalid", "skill contract contains a duplicate JSON key")
        value[key] = item
    return value


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_CONTRACT_BYTES + 1)
        if len(payload) > MAX_CONTRACT_BYTES:
            raise StorageRefusal("input_too_large", f"{label} exceeds the bounded JSON input size")
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
    except StorageRefusal:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("skill_contract_invalid", f"{label} could not be read") from exc
    if not isinstance(value, dict):
        raise StorageRefusal("skill_contract_invalid", f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise StorageRefusal("skill_contract_invalid", f"{label} fields are not exact")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StorageRefusal("skill_contract_invalid", f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise StorageRefusal("skill_contract_invalid", f"{label} must be an array")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise StorageRefusal("skill_contract_invalid", f"{label} is invalid")
    return value


def _capability_map(value: Any, label: str) -> dict[str, list[str]]:
    result = _object(value, label)
    _exact_keys(result, set(CAPABILITY_DIMENSIONS), label)
    for dimension in CAPABILITY_DIMENSIONS:
        capabilities = _array(result[dimension], f"{label}.{dimension}")
        if capabilities != sorted(capabilities) or len(capabilities) != len(set(capabilities)):
            raise StorageRefusal(
                "skill_contract_invalid",
                f"{label}.{dimension} must be sorted and unique",
            )
        if any(not isinstance(item, str) or not CAPABILITY.fullmatch(item) for item in capabilities):
            raise StorageRefusal("skill_contract_invalid", f"{label}.{dimension} is invalid")
    return result


def _capability_refs(value: Mapping[str, list[str]]) -> set[str]:
    return {
        f"{dimension}.{capability}"
        for dimension in CAPABILITY_DIMENSIONS
        for capability in value[dimension]
    }


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    contract = _object(contract, "skill contract")
    _exact_keys(contract, {"schema", "roots", "skills", "installations"}, "skill contract")
    if contract["schema"] != CONTRACT_SCHEMA:
        raise StorageRefusal("skill_contract_invalid", "skill contract schema is unsupported")

    root_values = _array(contract["roots"], "roots")
    roots: list[str] = []
    for item in root_values:
        root = _object(item, "root")
        _exact_keys(root, {"label"}, "root")
        roots.append(_identifier(root["label"], "root label"))
    if roots != sorted(roots) or len(roots) != len(set(roots)) or not roots:
        raise StorageRefusal("skill_contract_invalid", "root labels must be non-empty, sorted, and unique")

    skill_values = _array(contract["skills"], "skills")
    skills: dict[str, dict[str, Any]] = {}
    for item in skill_values:
        skill = _object(item, "skill")
        _exact_keys(
            skill,
            {"identity", "scope", "provenance", "version", "capabilities", "fallback"},
            "skill",
        )
        identity = _identifier(skill["identity"], "skill identity")
        if identity in skills:
            raise StorageRefusal("skill_contract_invalid", "skill identities must be unique")
        if skill["scope"] not in {"shared", "specialist"}:
            raise StorageRefusal("skill_contract_invalid", "skill scope is unsupported")

        provenance = _object(skill["provenance"], "skill provenance")
        classification = provenance.get("classification")
        if classification == "recorded":
            _exact_keys(provenance, {"classification", "owner"}, "recorded provenance")
            if not isinstance(provenance["owner"], str) or not SOURCE_OWNER.fullmatch(
                provenance["owner"]
            ):
                raise StorageRefusal("skill_contract_invalid", "recorded source owner is invalid")
        elif classification == "unrecorded":
            _exact_keys(provenance, {"classification", "reason"}, "unrecorded provenance")
            if provenance["reason"] != "no-authoritative-source-record":
                raise StorageRefusal("skill_contract_invalid", "unrecorded provenance reason is invalid")
        else:
            raise StorageRefusal("skill_contract_invalid", "skill provenance classification is required")

        version = _object(skill["version"], "skill version")
        version_classification = version.get("classification")
        if version_classification == "declared":
            _exact_keys(version, {"classification", "value"}, "declared version")
            if not isinstance(version["value"], str) or not VERSION_VALUE.fullmatch(version["value"]):
                raise StorageRefusal("skill_contract_invalid", "declared skill version is invalid")
        elif version_classification == "unrecorded":
            _exact_keys(version, {"classification"}, "unrecorded version")
        else:
            raise StorageRefusal("skill_contract_invalid", "skill version classification is required")

        capability_contract = _object(skill["capabilities"], "skill capabilities")
        _exact_keys(capability_contract, {"required", "optional"}, "skill capabilities")
        required = _capability_map(capability_contract["required"], "required capabilities")
        optional = _capability_map(capability_contract["optional"], "optional capabilities")
        if _capability_refs(required) & _capability_refs(optional):
            raise StorageRefusal("skill_contract_invalid", "required and optional capabilities overlap")

        fallback = _object(skill["fallback"], "skill fallback")
        _exact_keys(fallback, {"mode", "when_missing", "when_available"}, "skill fallback")
        if fallback["mode"] not in {"inline", "refuse"}:
            raise StorageRefusal("skill_contract_invalid", "skill fallback mode is unsupported")
        if fallback["when_available"] not in {"declared", "delegate"}:
            raise StorageRefusal("skill_contract_invalid", "available skill execution is unsupported")
        when_missing = _array(fallback["when_missing"], "fallback when_missing")
        if when_missing != sorted(when_missing) or len(when_missing) != len(set(when_missing)):
            raise StorageRefusal("skill_contract_invalid", "fallback capabilities must be sorted and unique")
        if any(not isinstance(item, str) or "." not in item for item in when_missing):
            raise StorageRefusal("skill_contract_invalid", "fallback capability reference is invalid")
        if not set(when_missing) <= _capability_refs(optional):
            raise StorageRefusal(
                "skill_contract_invalid",
                "inline fallback may reference only declared optional capabilities",
            )
        background_delegation = "delegation.background-visible-agents"
        if fallback["when_available"] == "delegate" and (
            background_delegation not in when_missing
            or background_delegation not in _capability_refs(optional)
        ):
            raise StorageRefusal(
                "skill_contract_invalid",
                "delegated execution requires optional background-visible-agent capability and inline fallback",
            )
        if skill["scope"] == "shared" and fallback["mode"] != "inline":
            raise StorageRefusal("skill_contract_invalid", "shared skills require an inline fallback")
        if skill["scope"] == "specialist":
            if fallback["mode"] != "refuse" or when_missing or not _capability_refs(required):
                raise StorageRefusal(
                    "skill_contract_invalid",
                    "specialist skills require capabilities and must refuse when unavailable",
                )
        skills[identity] = skill

    if list(skills) != sorted(skills) or not skills:
        raise StorageRefusal("skill_contract_invalid", "skills must be non-empty and identity-sorted")

    installation_values = _array(contract["installations"], "installations")
    installations: dict[tuple[str, str], dict[str, Any]] = {}
    for item in installation_values:
        installation = _object(item, "installation")
        _exact_keys(
            installation,
            {"root", "skill", "entry_kind", "content_sha256", "source_parity"},
            "installation",
        )
        root = _identifier(installation["root"], "installation root")
        identity = _identifier(installation["skill"], "installation skill")
        key = (identity, root)
        if root not in roots or identity not in skills or key in installations:
            raise StorageRefusal("skill_contract_invalid", "installation ownership is invalid or duplicated")
        if installation["entry_kind"] not in {"directory", "symlink"}:
            raise StorageRefusal("skill_contract_invalid", "installation entry kind is unsupported")
        if not isinstance(installation["content_sha256"], str) or not SHA256.fullmatch(
            installation["content_sha256"]
        ):
            raise StorageRefusal("skill_contract_invalid", "installation content hash is invalid")
        if installation["source_parity"] not in {"matched", "mismatched", "unverified"}:
            raise StorageRefusal("skill_contract_invalid", "installation source parity is unsupported")
        installations[key] = installation
    if list(installations) != sorted(installations) or not installations:
        raise StorageRefusal(
            "skill_contract_invalid",
            "installations must be non-empty and sorted by skill then root",
        )
    installed_skills = {identity for identity, _ in installations}
    if installed_skills != set(skills):
        raise StorageRefusal("skill_contract_invalid", "every declared skill must have an installation")

    duplicates: list[dict[str, Any]] = []
    for identity, skill in skills.items():
        copies = [
            installation
            for (installed_identity, _), installation in installations.items()
            if installed_identity == identity
        ]
        if len(copies) < 2:
            continue
        hashes = {copy["content_sha256"] for copy in copies}
        provenance = skill["provenance"]
        duplicates.append(
            {
                "skill": identity,
                "copies": len(copies),
                "source_owner": (
                    provenance["owner"]
                    if provenance["classification"] == "recorded"
                    else "unrecorded"
                ),
                "install_parity": "matched" if len(hashes) == 1 else "mismatched",
            }
        )

    return {
        "schema": VALIDATION_SCHEMA,
        "contract_sha256": _canonical_digest(contract),
        "summary": {
            "roots": len(roots),
            "skills": len(skills),
            "copies": len(installations),
            "shared": sum(skill["scope"] == "shared" for skill in skills.values()),
            "specialist": sum(skill["scope"] == "specialist" for skill in skills.values()),
            "recorded": sum(
                skill["provenance"]["classification"] == "recorded"
                for skill in skills.values()
            ),
            "unrecorded": sum(
                skill["provenance"]["classification"] == "unrecorded"
                for skill in skills.values()
            ),
        },
        "duplicates": duplicates,
    }


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256(b"league.skill-tree.v1\0")
    file_count = 0
    total_bytes = 0
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        retained_directories = sorted(
            name for name in directories if name not in IGNORED_TREE_NAMES
        )
        if any((current_path / name).is_symlink() for name in retained_directories):
            raise StorageRefusal(
                "skill_tree_unsafe",
                "skill content contains a nested link or non-regular file",
            )
        directories[:] = retained_directories
        files = sorted(name for name in files if name not in IGNORED_TREE_NAMES)
        for name in files:
            path = current_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise StorageRefusal(
                    "skill_tree_unsafe",
                    "skill content contains a nested link or non-regular file",
                )
            size = path.stat().st_size
            file_count += 1
            total_bytes += size
            if (
                file_count > MAX_SKILL_FILES
                or size > MAX_SKILL_FILE_BYTES
                or total_bytes > MAX_SKILL_TREE_BYTES
            ):
                raise StorageRefusal("skill_tree_too_large", "skill content exceeds audit bounds")
            content = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    content.update(block)
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            digest.update(content.digest())
            digest.update(b"\0")
    if file_count == 0 or not (root / "SKILL.md").is_file():
        raise StorageRefusal("skill_tree_invalid", "skill entry has no regular SKILL.md")
    return digest.hexdigest()


def _scan_root(root: Path) -> list[dict[str, str]]:
    if not root.is_absolute() or not root.is_dir():
        raise StorageRefusal("skill_root_invalid", "skill root must be an explicit absolute directory")
    found: list[dict[str, str]] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise StorageRefusal("skill_root_invalid", "skill root could not be read") from exc
    for entry in entries:
        if not entry.is_symlink() and not entry.is_dir():
            continue
        entry_kind = "symlink" if entry.is_symlink() else "directory"
        try:
            content_root = entry.resolve(strict=True) if entry.is_symlink() else entry
        except OSError as exc:
            raise StorageRefusal("skill_tree_invalid", "skill symlink target is unavailable") from exc
        if not content_root.is_dir() or not (content_root / "SKILL.md").is_file():
            continue
        identity = _identifier(entry.name, "installed skill identity")
        found.append(
            {
                "skill": identity,
                "entry_kind": entry_kind,
                "content_sha256": _tree_digest(content_root),
            }
        )
    return found


def audit_installations(
    contract: Mapping[str, Any], root_bindings: Mapping[str, Path]
) -> dict[str, Any]:
    validation = validate_contract(contract)
    expected_roots = [item["label"] for item in contract["roots"]]
    if sorted(root_bindings) != expected_roots:
        raise StorageRefusal("skill_root_mismatch", "explicit skill-root labels do not match the contract")

    actual: dict[tuple[str, str], dict[str, str]] = {}
    root_results: list[dict[str, Any]] = []
    for label in expected_roots:
        scanned = _scan_root(root_bindings[label])
        root_results.append({"label": label, "copies": len(scanned)})
        for item in scanned:
            key = (item["skill"], label)
            if key in actual:
                raise StorageRefusal("skill_install_duplicate", "skill root contains a duplicate identity")
            actual[key] = item

    expected = {
        (item["skill"], item["root"]): item for item in contract["installations"]
    }
    if set(actual) != set(expected):
        raise StorageRefusal(
            "skill_inventory_mismatch",
            "installed skill identities do not match the sanitized contract",
        )
    copies: list[dict[str, Any]] = []
    for identity, label in sorted(expected):
        observed = actual[(identity, label)]
        declared = expected[(identity, label)]
        if observed["entry_kind"] != declared["entry_kind"]:
            raise StorageRefusal("skill_install_parity_mismatch", "skill entry kind changed")
        if observed["content_sha256"] != declared["content_sha256"]:
            raise StorageRefusal("skill_install_parity_mismatch", "skill content hash changed")
        copies.append(
            {
                "root": label,
                "skill": identity,
                "entry_kind": observed["entry_kind"],
                "content_sha256": observed["content_sha256"],
                "source_parity": declared["source_parity"],
                "install_parity": "matched",
            }
        )
    return {
        "schema": AUDIT_SCHEMA,
        "contract_sha256": validation["contract_sha256"],
        "summary": validation["summary"],
        "roots": root_results,
        "copies": copies,
        "duplicates": validation["duplicates"],
        "local_paths_included": False,
        "skill_bodies_included": False,
    }


def validate_profile(profile: Mapping[str, Any], registry: AdapterRegistry) -> dict[str, Any]:
    profile = _object(profile, "skill runtime profile")
    _exact_keys(profile, {"schema", "adapter", "capabilities"}, "skill runtime profile")
    if profile["schema"] != PROFILE_SCHEMA:
        raise StorageRefusal("skill_profile_invalid", "skill runtime profile schema is unsupported")
    adapter = _object(profile["adapter"], "skill runtime adapter")
    _exact_keys(adapter, {"harness", "backend"}, "skill runtime adapter")
    harness = _identifier(adapter["harness"], "skill runtime harness")
    backend = _identifier(adapter["backend"], "skill runtime backend")
    capabilities = _capability_map(profile["capabilities"], "runtime capabilities")
    pair = next(
        (
            item
            for item in registry.capability_matrix()["pairs"]
            if item["harness"] == harness and item["backend"] == backend
        ),
        None,
    )
    if pair is None:
        raise StorageRefusal("skill_profile_adapter_unknown", "runtime adapter pair is not registered")
    return {"adapter": pair, "capabilities": capabilities}


def capability_matrix(
    contract: Mapping[str, Any], profile: Mapping[str, Any], registry: AdapterRegistry
) -> dict[str, Any]:
    validation = validate_contract(contract)
    runtime = validate_profile(profile, registry)
    available = _capability_refs(runtime["capabilities"])
    skills: list[dict[str, Any]] = []
    for skill in contract["skills"]:
        required = _capability_refs(skill["capabilities"]["required"])
        optional = _capability_refs(skill["capabilities"]["optional"])
        required_missing = sorted(required - available)
        optional_missing = sorted(optional - available)
        fallback_missing = sorted(set(skill["fallback"]["when_missing"]) & set(optional_missing))
        if required_missing:
            status = "unavailable"
            execution = "refuse"
        elif skill["fallback"]["mode"] == "inline" and fallback_missing:
            status = "available_inline"
            execution = "inline"
        else:
            status = "available"
            execution = skill["fallback"]["when_available"]
        skills.append(
            {
                "skill": skill["identity"],
                "scope": skill["scope"],
                "status": status,
                "execution": execution,
                "required_missing": required_missing,
                "optional_missing": optional_missing,
            }
        )
    return {
        "schema": MATRIX_SCHEMA,
        "contract_sha256": validation["contract_sha256"],
        "adapter": runtime["adapter"],
        "skills": skills,
    }
