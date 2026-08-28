"""Explicit-root cross-harness adapter for the source-managed shared guide.

The adapter is deliberately not wired to a command or a home-directory
default.  Issue #23 may consume it only inside its separately authorized staged
installer.  Tests use disposable explicit roots.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from .storage_types import StorageRefusal


SUPPORTED_HARNESSES = frozenset({"codex", "cursor", "pi"})
TARGET_NAME = "AGENTS.md"
MAX_GUIDANCE_BYTES = 16_384


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _root(value: Path) -> Path:
    root = Path(value)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise StorageRefusal(
            "invalid_guidance_root",
            "guidance staging requires an explicit existing non-symlink root",
        )
    return root.resolve()


def _source(value: Path) -> bytes:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise StorageRefusal("invalid_guidance_source", "shared guidance source is invalid")
    if path.stat().st_size > MAX_GUIDANCE_BYTES:
        raise StorageRefusal("invalid_guidance_source", "shared guidance source is not bounded text")
    payload = path.read_bytes()
    if not payload or len(payload) > MAX_GUIDANCE_BYTES or b"\x00" in payload:
        raise StorageRefusal("invalid_guidance_source", "shared guidance source is not bounded text")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageRefusal("invalid_guidance_source", "shared guidance source is not UTF-8") from exc
    return payload


def stage_guidance(source: Path, harness: str, destination_root: Path) -> dict[str, Any]:
    """Stage exact shared bytes with a verified recoverable backup.

    ``destination_root`` is the already isolated root selected by the caller;
    this function never discovers or defaults to a global harness directory.
    """

    if harness not in SUPPORTED_HARNESSES:
        raise StorageRefusal("unsupported_harness", "guidance harness is unsupported")
    root = _root(destination_root)
    payload = _source(source)
    target = root / TARGET_NAME
    if target.is_symlink():
        raise StorageRefusal("guidance_target_unsafe", "guidance target cannot be a symlink")
    before: bytes | None = None
    if target.exists():
        if not target.is_file():
            raise StorageRefusal("guidance_target_unsafe", "guidance target must be a regular file")
        if target.stat().st_size > MAX_GUIDANCE_BYTES:
            raise StorageRefusal(
                "guidance_target_unsafe", "existing guidance exceeds the backup byte bound"
            )
        before = target.read_bytes()
    temporary = root / f".{TARGET_NAME}.league-stage"
    if temporary.exists() or temporary.is_symlink():
        raise StorageRefusal("guidance_stage_collision", "guidance staging file already exists")
    backup = None
    if before is not None:
        backup = root / f".{TARGET_NAME}.league-backup-{_hash(before)[:12]}"
        if backup.exists() or backup.is_symlink():
            raise StorageRefusal("guidance_backup_collision", "guidance backup already exists")
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(before)
            handle.flush()
            os.fsync(handle.fileno())
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        installed = target.read_bytes()
        if installed != payload:
            raise StorageRefusal("guidance_parity_failed", "staged guidance bytes did not verify")
    except BaseException:
        temporary.unlink(missing_ok=True)
        if before is None:
            target.unlink(missing_ok=True)
        elif backup is not None and backup.exists():
            os.replace(backup, target)
        raise
    return {
        "schema": "league.guidance-stage.v1",
        "harness": harness,
        "source_sha256": _hash(payload),
        "installed_sha256": _hash(installed),
        "backup_sha256": _hash(before) if before is not None else None,
        "rollback_available": before is not None,
        "target_included": False,
    }


__all__ = ["SUPPORTED_HARNESSES", "stage_guidance"]
