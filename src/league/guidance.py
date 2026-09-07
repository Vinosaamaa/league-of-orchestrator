"""Explicit-root staging for League's orchestration-only guide supplement.

The adapter has no command or home-directory default. Tests and release
rehearsals provide an isolated explicit agent root.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Sequence

from .agent_adapters import builtin_agent_adapter_kinds
from .storage_types import StorageRefusal


SUPPORTED_HARNESSES = frozenset(builtin_agent_adapter_kinds())
UNIVERSAL_TARGET = "AGENTS.md"
LEAGUE_TARGET = "league/AGENTS.md"
TARGET_NAME = Path(LEAGUE_TARGET).name
MAX_GUIDANCE_BYTES = 16_384
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _root(value: Path) -> Path:
    root = Path(value)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise StorageRefusal(
            "invalid_guidance_root",
            "guidance staging requires an explicit existing non-symlink root",
        )
    return root.resolve()


def is_universal_guidance_target(value: str | Path) -> bool:
    """Return whether a manifest or absolute path names the universal guide."""

    normalized = os.fspath(value).replace("\\", "/")
    return normalized in {UNIVERSAL_TARGET, "~/.agents/AGENTS.md"} or normalized.endswith(
        "/.agents/AGENTS.md"
    )


def _target(value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise StorageRefusal("invalid_guidance_target", "guidance target is invalid")
    normalized = value.replace("\\", "/")
    if is_universal_guidance_target(normalized):
        raise StorageRefusal(
            "universal_guidance_forbidden",
            "League cannot target the universal agent guide",
        )
    if normalized != LEAGUE_TARGET:
        raise StorageRefusal(
            "unsupported_guidance_target",
            "League guidance must target only league/AGENTS.md",
        )
    return Path("league") / TARGET_NAME


def validate_guidance_manifest(targets: Sequence[str]) -> tuple[str, ...]:
    """Validate all guide targets before a package or installer mutates state."""

    if isinstance(targets, (str, bytes)) or not isinstance(targets, Sequence):
        raise StorageRefusal("invalid_guidance_manifest", "guidance manifest is invalid")
    values = tuple(targets)
    if not values:
        raise StorageRefusal("invalid_guidance_manifest", "guidance manifest is empty")
    normalized = tuple(_target(value).as_posix() for value in values)
    if normalized != (LEAGUE_TARGET,):
        raise StorageRefusal(
            "invalid_guidance_manifest",
            "guidance manifest must contain exactly the League supplement",
        )
    return normalized


def _source(value: Path) -> bytes:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise StorageRefusal("invalid_guidance_source", "League guidance source is invalid")
    if path.stat().st_size > MAX_GUIDANCE_BYTES:
        raise StorageRefusal("invalid_guidance_source", "League guidance source is too large")
    payload = path.read_bytes()
    if not payload or len(payload) > MAX_GUIDANCE_BYTES or b"\x00" in payload:
        raise StorageRefusal("invalid_guidance_source", "League guidance source is invalid")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageRefusal("invalid_guidance_source", "League guidance source is not UTF-8") from exc
    return payload


def _universal_hash(root: Path) -> str:
    universal = root / UNIVERSAL_TARGET
    if (
        not universal.is_file()
        or universal.is_symlink()
        or universal.stat().st_size > MAX_GUIDANCE_BYTES
    ):
        raise StorageRefusal(
            "universal_guidance_unproven",
            "the universal agent guide must be an exact bounded regular file",
        )
    payload = universal.read_bytes()
    if not payload or b"\x00" in payload:
        raise StorageRefusal(
            "universal_guidance_unproven",
            "the universal agent guide bytes are invalid",
        )
    return _hash(payload)


def stage_guidance(
    source: Path,
    harness: str,
    destination_root: Path,
    *,
    target: str = LEAGUE_TARGET,
) -> dict[str, Any]:
    """Stage exact League supplement bytes with a recoverable backup."""

    relative_target = _target(target)
    if harness not in SUPPORTED_HARNESSES:
        raise StorageRefusal("unsupported_harness", "guidance harness is unsupported")
    root = _root(destination_root)
    payload = _source(source)
    universal_before = _universal_hash(root)
    target_parent = root / relative_target.parent
    if target_parent.exists() and (
        not target_parent.is_dir() or target_parent.is_symlink()
    ):
        raise StorageRefusal("guidance_target_unsafe", "League guidance root is unsafe")
    target_parent.mkdir(mode=0o700, exist_ok=True)
    destination = root / relative_target
    if destination.is_symlink():
        raise StorageRefusal("guidance_target_unsafe", "guidance target cannot be a symlink")
    before: bytes | None = None
    if destination.exists():
        if not destination.is_file():
            raise StorageRefusal("guidance_target_unsafe", "guidance target must be a regular file")
        if destination.stat().st_size > MAX_GUIDANCE_BYTES:
            raise StorageRefusal(
                "guidance_target_unsafe", "existing guidance exceeds the backup byte bound"
            )
        before = destination.read_bytes()
    temporary = target_parent / f".{TARGET_NAME}.league-stage"
    if temporary.exists() or temporary.is_symlink():
        raise StorageRefusal("guidance_stage_collision", "guidance staging file already exists")
    backup = None
    if before is not None:
        backup = target_parent / f".{TARGET_NAME}.league-backup-{_hash(before)[:12]}"
        if backup.exists() or backup.is_symlink():
            raise StorageRefusal("guidance_backup_collision", "guidance backup already exists")
        try:
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(before)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        installed = destination.read_bytes()
        if installed != payload:
            raise StorageRefusal("guidance_parity_failed", "staged guidance bytes did not verify")
    except BaseException:
        temporary.unlink(missing_ok=True)
        if before is None:
            destination.unlink(missing_ok=True)
        elif backup is not None and backup.exists():
            os.replace(backup, destination)
        raise
    try:
        universal_after = _universal_hash(root)
    except BaseException:
        if before is None:
            destination.unlink(missing_ok=True)
        elif backup is not None and backup.exists():
            os.replace(backup, destination)
        raise
    if universal_after != universal_before:
        if before is None:
            destination.unlink(missing_ok=True)
        elif backup is not None and backup.exists():
            os.replace(backup, destination)
        raise StorageRefusal(
            "universal_guidance_changed",
            "the universal agent guide changed during League staging",
        )
    return {
        "schema": "league.guidance-stage.v2",
        "harness": harness,
        "target": LEAGUE_TARGET,
        "source_sha256": _hash(payload),
        "installed_sha256": _hash(installed),
        "prior_sha256": _hash(before) if before is not None else None,
        "rollback_available": before is not None,
        "universal_before_sha256": universal_before,
        "universal_after_sha256": universal_after,
        "universal_unchanged": True,
        "target_included": False,
    }


def rollback_guidance(
    destination_root: Path,
    harness: str,
    stage_receipt: dict[str, Any],
    *,
    target: str = LEAGUE_TARGET,
) -> dict[str, Any]:
    """Restore only the League supplement named by one exact stage receipt."""

    relative_target = _target(target)
    if harness not in SUPPORTED_HARNESSES:
        raise StorageRefusal("unsupported_harness", "guidance harness is unsupported")
    required = {
        "schema", "harness", "target", "source_sha256", "installed_sha256",
        "prior_sha256", "rollback_available", "universal_before_sha256",
        "universal_after_sha256", "universal_unchanged", "target_included",
    }
    if (
        not isinstance(stage_receipt, dict)
        or set(stage_receipt) != required
        or stage_receipt.get("schema") != "league.guidance-stage.v2"
        or stage_receipt.get("harness") != harness
        or stage_receipt.get("target") != LEAGUE_TARGET
        or stage_receipt.get("universal_unchanged") is not True
        or stage_receipt.get("target_included") is not False
        or not _digest(stage_receipt.get("source_sha256"))
        or stage_receipt.get("source_sha256") != stage_receipt.get("installed_sha256")
        or not _digest(stage_receipt.get("universal_before_sha256"))
        or stage_receipt.get("universal_before_sha256")
        != stage_receipt.get("universal_after_sha256")
    ):
        raise StorageRefusal("guidance_receipt_invalid", "guidance stage receipt is invalid")
    root = _root(destination_root)
    universal_before = _universal_hash(root)
    if universal_before != stage_receipt["universal_before_sha256"]:
        raise StorageRefusal(
            "universal_guidance_changed",
            "the universal agent guide changed before League rollback",
        )
    destination = root / relative_target
    if (
        not destination.is_file()
        or destination.is_symlink()
        or destination.stat().st_size > MAX_GUIDANCE_BYTES
        or _hash(destination.read_bytes()) != stage_receipt["installed_sha256"]
    ):
        raise StorageRefusal(
            "guidance_rollback_conflict",
            "installed League guidance does not match the stage receipt",
        )
    prior_sha256 = stage_receipt["prior_sha256"]
    if prior_sha256 is None:
        if stage_receipt["rollback_available"] is not False:
            raise StorageRefusal("guidance_receipt_invalid", "guidance rollback receipt conflicts")
        destination.unlink()
        restored = None
    else:
        if (
            not _digest(prior_sha256)
            or stage_receipt["rollback_available"] is not True
        ):
            raise StorageRefusal("guidance_receipt_invalid", "guidance rollback receipt conflicts")
        backup = destination.parent / f".{TARGET_NAME}.league-backup-{prior_sha256[:12]}"
        if (
            not backup.is_file()
            or backup.is_symlink()
            or backup.stat().st_size > MAX_GUIDANCE_BYTES
            or _hash(backup.read_bytes()) != prior_sha256
        ):
            raise StorageRefusal("guidance_backup_missing", "exact League guidance backup is missing")
        os.replace(backup, destination)
        restored = _hash(destination.read_bytes())
        if restored != prior_sha256:
            raise StorageRefusal("guidance_rollback_failed", "League guidance rollback did not verify")
    universal_after = _universal_hash(root)
    if universal_after != universal_before:
        raise StorageRefusal(
            "universal_guidance_changed",
            "the universal agent guide changed during League rollback",
        )
    return {
        "schema": "league.guidance-rollback.v1",
        "harness": harness,
        "target": LEAGUE_TARGET,
        "completed": True,
        "restored_sha256": restored,
        "universal_before_sha256": universal_before,
        "universal_after_sha256": universal_after,
        "universal_unchanged": True,
        "target_included": False,
    }


__all__ = [
    "LEAGUE_TARGET",
    "MAX_GUIDANCE_BYTES",
    "SUPPORTED_HARNESSES",
    "UNIVERSAL_TARGET",
    "rollback_guidance",
    "stage_guidance",
    "validate_guidance_manifest",
]
