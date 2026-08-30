"""One-shot, authority-bound live cutover executor for issue #23."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .acceptance import (
    NAMESPACE_PATTERN,
    _atomic_write,
    _stage_release_bytes,
    _switch_symlink,
)
from .precutover import _integrated_lifecycle, _read_only_shadow, _snapshot, _validate_plan
from .sqlite_store import SQLiteStorage
from .storage import StorageRefusal

ARCHIVE_SCHEMA = "league.legacy-system-archive.v1"


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
        if _snapshot(target) == item["before"]:
            continue
        _remove_node(target)
        if item["before"]["exists"]:
            _copy_node(backup_root / "targets" / item["target_id"], target)
        if _snapshot(target) != item["before"]:
            raise StorageRefusal("cutover_rollback_failed", "restored target differs")


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _preflight_live_cutover(
    temporary_root: Path, namespace: str, plan: dict[str, Any]
) -> tuple[Path, Path, Path]:
    supplied_root = Path(temporary_root)
    if (
        not supplied_root.is_absolute()
        or not supplied_root.is_dir()
        or supplied_root.is_symlink()
    ):
        raise StorageRefusal(
            "invalid_temporary_root",
            "temporary root must be an explicit directory",
        )
    temporary = supplied_root.resolve(strict=True)
    if temporary == Path("/"):
        raise StorageRefusal(
            "invalid_temporary_root",
            "temporary root must be an explicit directory",
        )
    if not NAMESPACE_PATTERN.fullmatch(namespace):
        raise StorageRefusal("invalid_namespace", "live cutover namespace is invalid")

    proposed = plan["proposed"]
    root = temporary / f"league-{namespace}-cutover"
    release = Path(proposed["release_prefix"]) / "releases" / __version__
    release_bundle = root / "release-bundle" / __version__
    if _lexists(release) or _lexists(release_bundle):
        raise StorageRefusal(
            "cutover_release_identity_exists",
            "the candidate release identity is already allocated",
        )
    if _lexists(root):
        raise StorageRefusal(
            "cutover_attempt_exists",
            "the live cutover attempt namespace is already allocated",
        )
    if _lexists(Path(proposed["backup_root"])):
        raise StorageRefusal("cutover_backup_exists", "cutover backup root must be absent")
    if _lexists(Path(proposed["state_root"])):
        raise StorageRefusal(
            "cutover_target_exists", "canonical SQLite state already exists"
        )
    if _lexists(Path(proposed["archive_root"])):
        raise StorageRefusal("legacy_archive_exists", "legacy archive root must be absent")
    return root, release, release_bundle


def _reserve_release_identity(release: Path, release_bundle: Path) -> None:
    reserved: list[Path] = []
    try:
        release_bundle.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        release_bundle.mkdir(mode=0o700)
        reserved.append(release_bundle)
        release.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        release.mkdir(mode=0o700)
        reserved.append(release)
    except FileExistsError as exc:
        for candidate in reversed(reserved):
            try:
                candidate.rmdir()
            except OSError:
                pass
        raise StorageRefusal(
            "cutover_release_identity_exists",
            "the candidate release identity was allocated concurrently",
        ) from exc


def _install_hook_routes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    stable_watcher = str(plan["proposed"]["watcher_launcher"])
    receipts: list[dict[str, Any]] = []
    for hook in sorted(plan["proposed"]["hooks"], key=lambda item: item["harness"]):
        path = Path(hook["target"])
        before = _snapshot(path)
        document = _load(path)
        harness = hook["harness"]
        hooks = document.get("hooks")
        if not isinstance(hooks, dict):
            raise StorageRefusal("cutover_hook_invalid", "hook configuration is malformed")
        added: list[str] = []
        if harness == "codex":
            wanted = {
                "UserPromptSubmit": f"{stable_watcher} codex-user-prompt-hook",
                "Stop": f"{stable_watcher} codex-stop-hook",
            }
            for event, command in wanted.items():
                groups = hooks.setdefault(event, [])
                if not isinstance(groups, list):
                    raise StorageRefusal("cutover_hook_invalid", "Codex hook event is malformed")
                matches = [
                    handler
                    for group in groups
                    if isinstance(group, dict)
                    for handler in group.get("hooks", [])
                    if isinstance(handler, dict) and handler.get("command") == command
                ]
                if len(matches) > 1:
                    raise StorageRefusal("cutover_hook_ambiguous", "Codex League hook is duplicated")
                if not matches:
                    groups.append(
                        {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
                    )
                    added.append(event)
        elif harness == "cursor":
            if document.get("version") != 1:
                raise StorageRefusal("cutover_hook_invalid", "Cursor hook version is unsupported")
            wanted = {
                "beforeSubmitPrompt": f"{stable_watcher} cursor-before-submit-hook",
                "stop": f"{stable_watcher} cursor-stop-hook",
            }
            for event, command in wanted.items():
                handlers = hooks.setdefault(event, [])
                if not isinstance(handlers, list):
                    raise StorageRefusal("cutover_hook_invalid", "Cursor hook event is malformed")
                matches = [
                    item
                    for item in handlers
                    if isinstance(item, dict) and item.get("command") == command
                ]
                if len(matches) > 1:
                    raise StorageRefusal("cutover_hook_ambiguous", "Cursor League hook is duplicated")
                if not matches:
                    handlers.append({"command": command})
                    added.append(event)
        else:
            raise StorageRefusal("cutover_hook_invalid", "unsupported cutover hook harness")
        if added:
            _atomic_write(path, _stable(document))
        after = _snapshot(path)
        receipts.append(
            {
                "harness": harness,
                "path": str(path),
                "added": added,
                "before_sha256": before["sha256"],
                "after_sha256": after["sha256"],
                "stable_watcher": stable_watcher,
            }
        )
    return receipts


def _live_watcher_smoke(
    state_root: Path, watcher_launcher: Path, writer_pointer: Path
) -> dict[str, Any]:
    with SQLiteStorage(state_root, request_wal=False) as store:
        actor = store.connection.execute(
            "SELECT callsign,thread_id FROM agent_instances "
            "WHERE retired_at IS NULL AND role='shotcaller' ORDER BY callsign LIMIT 1"
        ).fetchone()
    if actor is None or not actor["thread_id"]:
        raise StorageRefusal("cutover_watcher_smoke_failed", "no live Shotcaller is available")
    environment = {
        **os.environ,
        "LEAGUE_STATE_ROOT": str(state_root),
        "LEAGUE_WRITER_POINTER": str(writer_pointer),
    }

    def run(arguments: list[str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        completed = subprocess.run(
            arguments,
            input=None if payload is None else json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
            env=environment,
        )
        if completed.returncode != 0:
            detail = " ".join(completed.stderr.split())[:256]
            raise StorageRefusal(
                "cutover_watcher_smoke_failed",
                f"installed watcher refused: {detail or 'no error detail'}",
            )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise StorageRefusal(
                "cutover_watcher_smoke_failed", "installed watcher output is malformed"
            ) from exc
        if not isinstance(value, dict):
            raise StorageRefusal("cutover_watcher_smoke_failed", "installed watcher output differs")
        return value

    callsign, thread_id = str(actor["callsign"]), str(actor["thread_id"])
    status = run([str(watcher_launcher), "--shotcaller", callsign, "status"])
    if status != {"shotcaller": callsign, "writer": "sqlite"}:
        raise StorageRefusal("cutover_watcher_smoke_failed", "installed watcher is not on SQLite")
    codex = run(
        [str(watcher_launcher), "codex-user-prompt-hook"],
        {
            "session_id": thread_id,
            "turn_id": "cutover-smoke-codex-turn",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Synthetic cutover hook capture.",
        },
    )
    cursor = run(
        [str(watcher_launcher), "cursor-before-submit-hook"],
        {
            "conversation_id": thread_id,
            "generation_id": "cutover-smoke-cursor-generation",
            "hook_event_name": "beforeSubmitPrompt",
            "prompt": "Synthetic cutover Cursor hook capture.",
        },
    )
    return {
        "status": "passed",
        "writer": "sqlite",
        "shotcaller": callsign,
        "codex_user_prompt": codex,
        "cursor_before_submit": cursor,
    }


def verify_legacy_archive(archive: Path) -> dict[str, Any]:
    """Verify every archived legacy node against its immutable manifest."""

    manifest = _load(archive / "archive-manifest.json")
    if manifest.get("schema") != ARCHIVE_SCHEMA:
        raise StorageRefusal("legacy_archive_invalid", "legacy archive schema differs")
    expected_digest = manifest.get("manifest_sha256")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if not isinstance(expected_digest, str) or _digest(unsigned) != expected_digest:
        raise StorageRefusal("legacy_archive_invalid", "legacy archive manifest digest differs")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise StorageRefusal("legacy_archive_invalid", "legacy archive entries are missing")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "archived_relative_path",
            "before",
            "original_path",
            "target_id",
        }:
            raise StorageRefusal("legacy_archive_invalid", "legacy archive entry is malformed")
        target_id = entry["target_id"]
        if not isinstance(target_id, str) or not target_id or target_id in seen:
            raise StorageRefusal("legacy_archive_invalid", "legacy archive target identity differs")
        seen.add(target_id)
        before = entry["before"]
        relative = entry["archived_relative_path"]
        if not isinstance(before, dict) or before.get("exists") not in {True, False}:
            raise StorageRefusal("legacy_archive_invalid", "legacy archive snapshot is malformed")
        if before["exists"]:
            if not isinstance(relative, str) or Path(relative).parts != ("legacy-system", target_id):
                raise StorageRefusal("legacy_archive_invalid", "legacy archive path differs")
            observed = _snapshot(archive / relative)
            if observed != before:
                raise StorageRefusal("legacy_archive_mismatch", "archived legacy bytes differ")
        elif relative is not None:
            raise StorageRefusal("legacy_archive_invalid", "absent legacy target has archive bytes")
    restore = archive / "RESTORE.md"
    if not restore.is_file() or restore.is_symlink():
        raise StorageRefusal("legacy_archive_invalid", "legacy restore runbook is missing")
    backup_receipt = archive / "backup-receipt.json"
    if not backup_receipt.is_file() or backup_receipt.is_symlink():
        raise StorageRefusal("legacy_archive_invalid", "legacy backup receipt is missing")
    if (
        hashlib.sha256(restore.read_bytes()).hexdigest() != manifest.get("restore_sha256")
        or hashlib.sha256(backup_receipt.read_bytes()).hexdigest()
        != manifest.get("backup_receipt_sha256")
    ):
        raise StorageRefusal("legacy_archive_mismatch", "legacy restore evidence differs")
    return {
        "schema": ARCHIVE_SCHEMA,
        "archive": str(archive),
        "entry_count": len(entries),
        "manifest_sha256": expected_digest,
        "verified": True,
    }


def _archive_legacy(
    plan: dict[str, Any],
    receipts: list[dict[str, Any]],
    backup_root: Path,
    pointer: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    archive_root = Path(plan["proposed"]["archive_root"])
    archive = archive_root / pointer["generation"]
    if archive_root.exists() or archive_root.is_symlink():
        raise StorageRefusal("legacy_archive_exists", "legacy archive root must be absent")
    archive.mkdir(parents=True, mode=0o700)
    entries: list[dict[str, Any]] = []
    for item in receipts:
        target_id = item["target_id"]
        before = item["before"]
        relative: str | None = None
        if before["exists"]:
            relative = f"legacy-system/{target_id}"
            target = archive / relative
            _copy_node(backup_root / "targets" / target_id, target)
            if _snapshot(target) != before:
                raise StorageRefusal("legacy_archive_mismatch", "archived legacy bytes differ")
        entries.append(
            {
                "target_id": target_id,
                "original_path": item["path"],
                "archived_relative_path": relative,
                "before": before,
            }
        )
    restore = f"""# League legacy-system restore record

This directory is the immutable inactive copy of the pre-SQLite League system.
It includes the old watcher bundle and launcher, Codex and Cursor hook configs,
legacy JSON/JSONL records, watcher state, callsign pools, routing, and resources.

Generation replaced by this archive: `{pointer['generation']}`

Before any restoration:

1. Obtain explicit owner authority for a live rollback.
2. Run `league acceptance archive-verify --archive {archive}` and require `verified: true`.
3. Acquire the global League cutover lock and quiesce every SQLite hook and writer.
4. Back up the current SQLite database, installed release, launchers, hooks, and writer pointer.
5. Restore only the exact `original_path` nodes from `archive-manifest.json`; never copy by hand or omit a hash check.
6. Switch launchers, hooks, and writer generation together, then run watcher, transition, delivery, Stop, and teardown smoke.
7. If any gate fails, restore the pre-rollback SQLite backup before reopening intake.

The archive is evidence and rollback input. It is never an active writer.
""".encode("utf-8")
    backup_receipt = (backup_root / "backup-receipt.json").read_bytes()
    manifest: dict[str, Any] = {
        "schema": ARCHIVE_SCHEMA,
        "created_at": created_at,
        "writer_generation": pointer["generation"],
        "source_version": pointer["version"],
        "active_writer_after_cutover": "sqlite",
        "restore_sha256": hashlib.sha256(restore).hexdigest(),
        "backup_receipt_sha256": hashlib.sha256(backup_receipt).hexdigest(),
        "entries": entries,
    }
    manifest["manifest_sha256"] = _digest(manifest)
    _atomic_write(archive / "archive-manifest.json", _stable(manifest))
    _atomic_write(archive / "RESTORE.md", restore)
    _atomic_write(archive / "backup-receipt.json", backup_receipt)
    return verify_legacy_archive(archive)


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
    root, release, release_bundle = _preflight_live_cutover(
        temporary_root, namespace, plan
    )
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
            # Recheck under the global lock before creating any cutover target,
            # backup, or attempt directory.
            locked_paths = _preflight_live_cutover(temporary_root, namespace, plan)
            if locked_paths != (root, release, release_bundle):
                raise StorageRefusal(
                    "cutover_preflight_changed", "live cutover candidate paths changed"
                )
            root.mkdir(mode=0o700)
            receipts = _backup(plan, backup_root)
            shadow = _read_only_shadow(root, plan)
            _reserve_release_identity(release, release_bundle)
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
            writer_pointer = Path(proposed["writer_pointer"])
            _atomic_write(writer_pointer, _stable(pointer))
            hooks = _install_hook_routes(plan)
            watcher_smoke = _live_watcher_smoke(
                state_root, Path(proposed["watcher_launcher"]), writer_pointer
            )
            smoke = _integrated_lifecycle(root / "smoke", source_root)
            completed_at = datetime.now().astimezone().isoformat(timespec="seconds")
            archive = _archive_legacy(plan, receipts, backup_root, pointer, completed_at)
            receipt = {
                "schema": "league.live-cutover-receipt.v1",
                "state": "completed",
                "at": completed_at,
                "authority_digest": authority_digest,
                "writer_generation": pointer["generation"],
                "source_manifest_sha256": _digest(source_hashes),
                "shadow": shadow,
                "integrity": integrity,
                "hooks": hooks,
                "watcher_smoke": watcher_smoke,
                "smoke": smoke,
                "legacy_archive": archive,
                "rollback_backup_verified": True,
            }
            _atomic_write(backup_root / "cutover-receipt.json", _stable(receipt))
            _atomic_write(
                Path(archive["archive"]) / "cutover-receipt.json", _stable(receipt)
            )
            return receipt
        except BaseException:
            if receipts:
                _restore(receipts, backup_root)
                _atomic_write(
                    backup_root / "rollback-receipt.json",
                    _stable({"schema": "league.live-cutover-rollback.v1", "state": "completed"}),
                )
            raise
