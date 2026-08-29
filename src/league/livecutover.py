"""One-shot, authority-bound live cutover executor for issue #23."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .acceptance import _atomic_write, _stage_release_bytes, _switch_symlink
from .precutover import _integrated_lifecycle, _read_only_shadow, _snapshot, _validate_plan
from .sqlite_store import SQLiteStorage
from .storage import StorageRefusal


def _stable(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_stable(value)).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageRefusal("cutover_authority_invalid", "cutover authority is unreadable") from exc
    if not isinstance(value, dict):
        raise StorageRefusal("cutover_authority_invalid", "cutover authority must be an object")
    return value


def _copy_node(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def _remove_node(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _backup(plan: dict[str, Any], backup_root: Path) -> list[dict[str, Any]]:
    if backup_root.exists() or backup_root.is_symlink():
        raise StorageRefusal("cutover_backup_exists", "cutover backup root must be absent")
    backup_root.mkdir(parents=True, mode=0o700)
    receipts = []
    for target in plan["current_targets"]:
        if Path(target["path"]) == backup_root:
            continue
        source = Path(target["path"])
        before = _snapshot(source)
        backup = backup_root / "targets" / target["target_id"]
        if before["exists"]:
            _copy_node(source, backup)
            if _snapshot(backup)["sha256"] != before["sha256"]:
                raise StorageRefusal("cutover_backup_mismatch", "cutover backup hash differs")
        receipts.append({"target_id": target["target_id"], "path": str(source), "before": before})
    _atomic_write(backup_root / "backup-receipt.json", _stable(receipts))
    return receipts


def _restore(receipts: list[dict[str, Any]], backup_root: Path) -> None:
    for item in reversed(receipts):
        target = Path(item["path"])
        _remove_node(target)
        if item["before"]["exists"]:
            _copy_node(backup_root / "targets" / item["target_id"], target)
            if _snapshot(target)["sha256"] != item["before"]["sha256"]:
                raise StorageRefusal("cutover_rollback_failed", "restored target hash differs")


def run_live_cutover(
    temporary_root: Path,
    namespace: str,
    *,
    plan_path: Path,
    authority_receipt: Path,
    authority_digest: str,
    source_root: Path,
) -> dict[str, Any]:
    plan = _validate_plan(plan_path)
    authority = _load(authority_receipt)
    operation = authority.get("operation", {})
    mutation = authority.get("mutation_manifest", {})
    if (
        operation.get("state") != "awaiting_authority"
        or mutation.get("manifest_sha256") != authority_digest
        or operation.get("mutation_manifest_sha256") != authority_digest
        or mutation.get("applied") is not False
    ):
        raise StorageRefusal("cutover_authority_invalid", "cutover authority digest or state differs")
    root = temporary_root.resolve(strict=True) / f"league-{namespace}-cutover"
    root.mkdir(mode=0o700)
    proposed = plan["proposed"]
    lock_path = Path(proposed["writer_pointer"]).with_name("league-cutover.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_root = Path(proposed["backup_root"])
    receipts: list[dict[str, Any]] = []
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StorageRefusal("cutover_locked", "another live cutover holds the global lock") from exc
        try:
            receipts = _backup(plan, backup_root)
            shadow = _read_only_shadow(root, plan)
            release = Path(proposed["release_prefix"]) / "releases" / __version__
            release_bundle = root / "release-bundle" / __version__
            release_bundle.mkdir(parents=True, mode=0o700)
            release.mkdir(parents=True, mode=0o700)
            source_hashes, release_hashes, installed_hashes = _stage_release_bytes(
                source_root, release_bundle, release, (str(source_root).encode(), str(root).encode())
            )
            if not (source_hashes == release_hashes == installed_hashes):
                raise StorageRefusal("cutover_install_mismatch", "source and installed release differ")
            state_root = Path(proposed["state_root"])
            if state_root.exists():
                raise StorageRefusal("cutover_target_exists", "canonical SQLite state already exists")
            shutil.copytree(root / "live-shadow" / "state", state_root)
            with SQLiteStorage(state_root, request_wal=False) as store:
                integrity = store.integrity()
            if not integrity["ok"]:
                raise StorageRefusal("cutover_integrity_failed", "installed SQLite integrity failed")
            _switch_symlink(Path(proposed["stable_launcher"]), str(release / "bin/league"))
            _switch_symlink(Path(proposed["watcher_launcher"]), str(release / "bin/agent-watcher"))
            pointer_operation = next(
                item
                for item in mutation["operations"]
                if item["operation"] == "switch_canonical_writer_pointer"
            )
            pointer = pointer_operation["after"]["pointer"]
            _atomic_write(Path(proposed["writer_pointer"]), _stable(pointer))
            smoke = _integrated_lifecycle(root / "smoke", source_root)
            receipt = {
                "schema": "league.live-cutover-receipt.v1",
                "state": "completed",
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "authority_digest": authority_digest,
                "writer_generation": pointer["generation"],
                "source_manifest_sha256": _digest(source_hashes),
                "shadow": shadow,
                "integrity": integrity,
                "smoke": smoke,
                "rollback_backup_verified": True,
            }
            _atomic_write(backup_root / "cutover-receipt.json", _stable(receipt))
            return receipt
        except BaseException:
            if receipts:
                _restore(receipts, backup_root)
                _atomic_write(
                    backup_root / "rollback-receipt.json",
                    _stable({"schema": "league.live-cutover-rollback.v1", "state": "completed"}),
                )
            raise
