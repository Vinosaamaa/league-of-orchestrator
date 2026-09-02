"""Byte-preserving migration from a legacy Pi inventory into unified Pi sessions."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePath
from typing import Any, Mapping

from .storage_types import StorageRefusal
from .visible_launch import CommandRunner, SubprocessRunner


MAX_HEADER_BYTES = 1_048_576
MAX_INVENTORY_FILES = 10_000


def _json_result(completed: Any, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("pi_session_migration_runtime_unverified", f"{label} returned malformed JSON") from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    if completed.returncode != 0 or not isinstance(result, dict):
        raise StorageRefusal("pi_session_migration_runtime_unverified", f"{label} refused or failed")
    return result


def _regular_file(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise StorageRefusal("pi_session_migration_source_unavailable", f"{label} is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise StorageRefusal("pi_session_migration_source_unavailable", f"{label} must be a regular non-symlink file")
    return info


def _header(path: Path, label: str) -> dict[str, Any]:
    _regular_file(path, label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.readline(MAX_HEADER_BYTES + 1)
    finally:
        os.close(descriptor)
    if not raw or len(raw) > MAX_HEADER_BYTES or not raw.endswith(b"\n"):
        raise StorageRefusal("pi_session_header_invalid", f"{label} has no bounded first JSONL record")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("pi_session_header_invalid", f"{label} first record is not JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("type") != "session"
        or not isinstance(value.get("id"), str)
        or not isinstance(value.get("cwd"), str)
        or not Path(value["cwd"]).is_absolute()
        or (value.get("parentSession") is not None and (
            not isinstance(value.get("parentSession"), str)
            or not Path(value["parentSession"]).is_absolute()
        ))
    ):
        raise StorageRefusal("pi_session_header_invalid", f"{label} session identity or lineage is incomplete")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _contained(root: Path, relative: str) -> Path:
    pure = PurePath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts) or pure.suffix != ".jsonl":
        raise StorageRefusal("pi_session_migration_path_invalid", "relative Pi session path is invalid")
    if (
        not root.is_absolute()
        or root == Path("/")
        or root.is_symlink()
        or not root.is_dir()
        or root.resolve() != root
    ):
        raise StorageRefusal("pi_session_migration_path_invalid", "Pi inventory root is invalid")
    destination = root.joinpath(*pure.parts)
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise StorageRefusal("pi_session_migration_path_invalid", "Pi session path escapes its inventory") from exc
    return destination


def _verify_stopped_pane(runner: CommandRunner, pane_id: str, cwd: str) -> None:
    completed = runner.run(("herdr", "pane", "process-info", "--pane", pane_id), timeout_seconds=30)
    info = _json_result(completed, "Herdr controlled restart boundary").get("process_info")
    processes = info.get("foreground_processes") if isinstance(info, Mapping) else None
    shell_pid = info.get("shell_pid") if isinstance(info, Mapping) else None
    foreground_group = info.get("foreground_process_group_id") if isinstance(info, Mapping) else None
    shell_only = (
        isinstance(processes, list)
        and len(processes) == 1
        and isinstance(processes[0], Mapping)
        and processes[0].get("pid") == shell_pid
        and processes[0].get("argv0") in {"zsh", "bash", "fish", "sh"}
        and processes[0].get("cwd") == cwd
    )
    if (
        not isinstance(processes, list)
        or not isinstance(shell_pid, int)
        or foreground_group != shell_pid
        or (processes and not shell_only)
    ):
        raise StorageRefusal(
            "pi_session_migration_runtime_active",
            "Pi session migration requires the exact restored pane at its shell-only restart boundary",
        )


def _inventory_identity(root: Path, session_id: str) -> tuple[Path, str] | None:
    count = 0
    for path in root.rglob("*.jsonl") if root.exists() else ():
        count += 1
        if count > MAX_INVENTORY_FILES:
            raise StorageRefusal("pi_session_inventory_ambiguous", "unified Pi inventory exceeds the bounded scan")
        header = _header(path, "unified Pi session")
        if header["id"] == session_id:
            return path, _sha256(path)
    return None


def _copy_exact(source: Path, destination: Path, inventory_root: Path, expected_sha256: str) -> str:
    current = inventory_root
    for part in destination.parent.relative_to(inventory_root).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            info = current.lstat()
        except OSError as exc:
            raise StorageRefusal("pi_session_migration_path_invalid", "Pi destination directory is unavailable") from exc
        if current.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise StorageRefusal("pi_session_migration_path_invalid", "Pi destination contains a non-directory or symlink")
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    try:
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode):
            raise StorageRefusal("pi_session_migration_source_unavailable", "Pi source changed before copy")
        try:
            destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if _sha256(destination) == expected_sha256:
                return expected_sha256
            raise StorageRefusal("pi_session_identity_duplicate", "unified Pi destination already differs")
        digest = hashlib.sha256()
        try:
            with os.fdopen(source_fd, "rb", closefd=False) as source_handle, os.fdopen(destination_fd, "wb") as destination_handle:
                for block in iter(lambda: source_handle.read(1024 * 1024), b""):
                    digest.update(block)
                    destination_handle.write(block)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        actual = digest.hexdigest()
        if actual != expected_sha256:
            destination.unlink(missing_ok=True)
            raise StorageRefusal("pi_session_migration_source_changed", "Pi source bytes changed at the restart boundary")
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return actual
    finally:
        os.close(source_fd)


def migrate_pi_session(
    store: Any,
    manifest: Mapping[str, Any],
    *,
    at: str,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Migrate one stopped legacy Pi session and bind its exact resume descriptor."""
    required = {
        "schema", "migration_id", "source_inventory_root", "unified_inventory_root",
        "relative_session_path", "expected_sha256", "descriptor", "endpoint",
    }
    exact = dict(manifest)
    if set(exact) != required or exact.get("schema") != "league.pi-session-migration.v1":
        raise StorageRefusal("pi_session_migration_invalid", "Pi migration manifest fields are not exact")
    descriptor = exact.get("descriptor")
    endpoint = exact.get("endpoint")
    if not isinstance(descriptor, Mapping) or not isinstance(endpoint, Mapping) or set(endpoint) != {"workspace_id", "tab_id", "pane_id", "terminal_id"}:
        raise StorageRefusal("pi_session_migration_invalid", "Pi migration descriptor or endpoint is incomplete")
    source_root = Path(str(exact["source_inventory_root"]))
    unified_root = Path(str(exact["unified_inventory_root"]))
    source = _contained(source_root, str(exact["relative_session_path"]))
    destination = _contained(unified_root, str(exact["relative_session_path"]))
    source_header = _header(source, "legacy Pi session")
    expected_sha256 = str(exact["expected_sha256"])
    if _sha256(source) != expected_sha256:
        raise StorageRefusal("pi_session_migration_digest_mismatch", "legacy Pi session digest differs from the manifest")
    parent_path = source_header.get("parentSession")
    parent_id = None
    if parent_path is not None:
        parent_header = _header(Path(parent_path), "legacy Pi parent session")
        parent_id = parent_header["id"]
    descriptor = dict(descriptor)
    if (
        descriptor.get("launch_mode") != "resume"
        or descriptor.get("requested_session_id") != source_header["id"]
        or descriptor.get("requested_session_path") != str(destination)
        or descriptor.get("cwd") != source_header["cwd"]
        or descriptor.get("parent_session_id") != parent_id
        or descriptor.get("parent_session_path") != parent_path
        or descriptor.get("workspace_id") != endpoint.get("workspace_id")
    ):
        raise StorageRefusal("pi_session_migration_descriptor_mismatch", "Pi resume descriptor differs from the exact JSONL lineage")
    pane_id = str(endpoint.get("pane_id", ""))
    _verify_stopped_pane(runner or SubprocessRunner(), pane_id, str(source_header["cwd"]))

    prepared_descriptor = store.prepare_provider_launch(descriptor, at)
    intent = {
        "schema": "league.pi-session-migration-intent.v1",
        "migration_id": str(exact["migration_id"]),
        "descriptor_id": str(descriptor["descriptor_id"]),
        "session_id": str(source_header["id"]),
        "source_session_path": str(source),
        "destination_session_path": str(destination),
        "session_sha256": expected_sha256,
        "parent_session_id": parent_id,
        "parent_session_path": parent_path,
        "cwd": str(source_header["cwd"]),
        "pane_id": pane_id,
    }
    prepared = store.prepare_pi_session_migration(intent, at)
    if prepared["state"] == "bound":
        return {**prepared, "descriptor_id": descriptor["descriptor_id"]}
    existing = _inventory_identity(unified_root, str(source_header["id"]))
    if existing is not None and (existing[0] != destination or existing[1] != expected_sha256):
        raise StorageRefusal("pi_session_identity_duplicate", "session ID already exists at a different unified Pi path or digest")
    if prepared["state"] == "intent_recorded":
        copied_digest = _copy_exact(source, destination, unified_root, expected_sha256)
        copied_receipt = {
            "schema": "league.pi-session-copy-receipt.v1",
            "session_id": source_header["id"],
            "session_path": str(destination),
            "session_sha256": copied_digest,
            "parent_session_id": parent_id,
            "parent_session_path": parent_path,
            "cwd": source_header["cwd"],
        }
        prepared = store.advance_pi_session_migration(
            intent["migration_id"], prepared["intent_digest"], "intent_recorded", "copied", copied_receipt, at
        )
    observation = {
        "schema": "league.pi-launch-observation.v1",
        "runtime_kind": "pi",
        "provider_kind": descriptor["provider_kind"],
        "session_id": source_header["id"],
        "session_path": str(destination),
        "parent_session_path": parent_path,
        "cwd": source_header["cwd"],
        "role": descriptor["role"],
        "placement": descriptor["placement"],
        "callsign": descriptor["callsign"],
        "project_code": descriptor["project_code"],
        "task_label": descriptor["task_label"],
        "routing_name": descriptor["routing_name"],
        "workspace_id": endpoint["workspace_id"],
        "tab_id": endpoint["tab_id"],
        "pane_id": endpoint["pane_id"],
        "terminal_id": endpoint["terminal_id"],
    }
    bound = store.bind_provider_launch(
        str(descriptor["descriptor_id"]), prepared_descriptor["version"], observation, at
    )
    final_receipt = {
        "schema": "league.pi-session-migration-receipt.v1",
        "session_id": source_header["id"],
        "session_path": str(destination),
        "session_sha256": expected_sha256,
        "parent_session_id": parent_id,
        "parent_session_path": parent_path,
        "cwd": source_header["cwd"],
        "descriptor_digest": bound["descriptor_digest"],
    }
    completed = store.advance_pi_session_migration(
        intent["migration_id"], prepared["intent_digest"], "copied", "bound", final_receipt, at
    )
    return {**completed, "descriptor_id": descriptor["descriptor_id"], "descriptor_digest": bound["descriptor_digest"]}


__all__ = ["migrate_pi_session"]
