"""Provider-neutral facade for source-managed hook bootstrap installation."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping, Optional

from .storage_types import FaultInjector, StorageRefusal


MAX_HOOK_FILE_BYTES = 1024 * 1024
UPGRADE_SCHEMA = "league.provider-hook-upgrade.v1"


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON constant")


def _decode_object(payload: bytes, code: str) -> dict[str, Any]:
    if not payload or len(payload) > MAX_HOOK_FILE_BYTES or b"\x00" in payload:
        raise StorageRefusal(code, "provider hook document exceeds its safe byte boundary")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise StorageRefusal(code, "provider hook document is malformed") from exc
    if not isinstance(value, dict):
        raise StorageRefusal(code, "provider hook document must be an object")
    return value


def load_hook_document(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise StorageRefusal(
            "hook_bootstrap_invalid", "provider hook configuration is not a regular file"
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise StorageRefusal(
            "hook_bootstrap_invalid", "provider hook configuration is unreadable"
        ) from exc
    return _decode_object(payload, "hook_bootstrap_invalid")


def stable_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise StorageRefusal(
            "hook_bootstrap_invalid", "provider hook target cannot be a symbolic link"
        )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.league-hook.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def install_provider_hook_bootstrap(
    registry: Any,
    adapter_kind: str,
    *,
    source_root: Path,
    target: Path,
    stable_watcher: Path,
) -> Mapping[str, Any]:
    """Dispatch one exact install through the registered provider adapter."""

    adapter = registry.adapter(adapter_kind)
    if not source_root.is_absolute() or not target.is_absolute() or not stable_watcher.is_absolute():
        raise StorageRefusal(
            "hook_bootstrap_invalid", "provider hook install paths must be absolute"
        )
    receipt = adapter.install_hook_bootstrap(
        source_root=source_root,
        target=target,
        stable_watcher=stable_watcher,
    )
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("adapter_kind") != adapter_kind
        or receipt.get("target") != str(target)
        or not isinstance(receipt.get("added"), list)
    ):
        raise StorageRefusal(
            "hook_bootstrap_invalid", "provider hook installer returned a malformed receipt"
        )
    return receipt


def release_hook_bootstrap_sources(registry: Any, source_root: Path) -> tuple[Path, ...]:
    """Return adapter-declared bootstrap assets for the immutable release manifest."""

    sources: list[Path] = []
    for adapter in registry.adapters():
        relative = adapter.hook_bootstrap_profile["source_relative"]
        if relative is not None:
            sources.append(source_root / str(relative))
    return tuple(sorted(sources, key=lambda item: item.as_posix()))


def _root(path: Path, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute() or not value.is_dir() or value.is_symlink():
        raise StorageRefusal("hook_upgrade_invalid", f"{label} must be an exact directory")
    resolved = value.resolve()
    if resolved == Path("/"):
        raise StorageRefusal("hook_upgrade_invalid", f"{label} cannot be filesystem root")
    return resolved


def _profile_path(profile_root: Path, relative: Path | str, label: str) -> Path:
    """Resolve one upgrade-owned descendant without traversing symlinks."""

    value = Path(relative)
    if value.is_absolute() or not value.parts or any(
        part in {"", ".", ".."} for part in value.parts
    ):
        raise StorageRefusal("hook_upgrade_invalid", f"{label} is not profile-relative")
    current = profile_root
    for index, part in enumerate(value.parts):
        current = current / part
        if current.is_symlink():
            raise StorageRefusal(
                "hook_upgrade_invalid", f"{label} traverses a symbolic link"
            )
        if index < len(value.parts) - 1 and current.exists() and not current.is_dir():
            raise StorageRefusal(
                "hook_upgrade_invalid", f"{label} parent is not a directory"
            )
    try:
        current.resolve(strict=False).relative_to(profile_root)
    except ValueError as exc:
        raise StorageRefusal(
            "hook_upgrade_invalid", f"{label} escapes the provider profile root"
        ) from exc
    return current


def _profile_absolute(profile_root: Path, path: Path | str, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise StorageRefusal("hook_upgrade_invalid", f"{label} must be absolute")
    try:
        relative = value.relative_to(profile_root)
    except ValueError as exc:
        raise StorageRefusal(
            "hook_upgrade_invalid", f"{label} escapes the provider profile root"
        ) from exc
    return _profile_path(profile_root, relative, label)


def _missing_parent_directories(profile_root: Path, target: Path) -> list[str]:
    missing: list[Path] = []
    current = target.parent
    while current != profile_root:
        _profile_absolute(profile_root, current, "provider hook parent")
        if current.exists():
            if not current.is_dir():
                raise StorageRefusal(
                    "hook_upgrade_invalid", "provider hook parent is not a directory"
                )
            break
        missing.append(current)
        current = current.parent
    if current != profile_root and profile_root not in current.parents:
        raise StorageRefusal(
            "hook_upgrade_invalid", "provider hook parent escapes the profile root"
        )
    return [str(item.relative_to(profile_root)) for item in reversed(missing)]


def _snapshot(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists() and not path.is_symlink():
        return {"exists": False, "sha256": None, "bytes": 0, "mode": None}, None
    if path.is_symlink():
        raise StorageRefusal("hook_upgrade_invalid", "provider hook target is a symbolic link")
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_HOOK_FILE_BYTES:
            raise StorageRefusal("hook_upgrade_invalid", "provider hook target is not a bounded file")
        payload = path.read_bytes()
    except OSError as exc:
        raise StorageRefusal("hook_upgrade_invalid", "provider hook target is unreadable") from exc
    if len(payload) != metadata.st_size:
        raise StorageRefusal("hook_upgrade_invalid", "provider hook target changed during inspection")
    return {
        "exists": True,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "mode": stat.S_IMODE(metadata.st_mode),
    }, payload


def _manifest(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise StorageRefusal("hook_upgrade_manifest_invalid", "hook upgrade manifest is unavailable")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise StorageRefusal("hook_upgrade_manifest_invalid", "hook upgrade manifest is unreadable") from exc
    value = _decode_object(payload, "hook_upgrade_manifest_invalid")
    required = {
        "schema", "state", "profile_root", "source_root", "stable_watcher",
        "backup_root", "created_directories", "targets",
    }
    if (
        set(value) != required
        or value.get("schema") != UPGRADE_SCHEMA
        or value.get("state") not in {"prepared", "active", "rolled_back"}
        or not isinstance(value.get("targets"), list)
        or not value["targets"]
        or not isinstance(value.get("created_directories"), list)
        or any(not isinstance(item, str) for item in value["created_directories"])
    ):
        raise StorageRefusal("hook_upgrade_manifest_invalid", "hook upgrade manifest is malformed")
    return value


def _receipt(manifest: Mapping[str, Any], *, idempotent: bool) -> dict[str, Any]:
    public = {
        "schema": UPGRADE_SCHEMA,
        "state": manifest["state"],
        "adapter_count": len(manifest["targets"]),
        "idempotent": idempotent,
    }
    public["manifest_sha256"] = hashlib.sha256(stable_json(manifest)).hexdigest()
    return public


def _validate_manifest_identity(
    manifest: Mapping[str, Any], *, profile_root: Path, source_root: Path,
    stable_watcher: Path,
) -> None:
    if (
        manifest.get("profile_root") != str(profile_root)
        or manifest.get("source_root") != str(source_root)
        or manifest.get("stable_watcher") != str(stable_watcher)
    ):
        raise StorageRefusal("hook_upgrade_manifest_mismatch", "hook upgrade identity changed")
    _profile_absolute(
        profile_root, Path(str(manifest.get("backup_root"))), "provider hook backup root"
    )
    for item in manifest["targets"]:
        if not isinstance(item, Mapping):
            raise StorageRefusal("hook_upgrade_manifest_invalid", "hook target is malformed")
        _profile_path(profile_root, str(item.get("relative", "")), "provider hook target")
        backup_root = Path(str(manifest["backup_root"]))
        _profile_absolute(
            profile_root,
            backup_root / str(item.get("backup", "")),
            "provider hook backup",
        )
    for relative in manifest["created_directories"]:
        _profile_path(profile_root, relative, "upgrade-created directory")


def _restore(manifest: Mapping[str, Any]) -> None:
    profile_root = Path(str(manifest["profile_root"]))
    backup_root = _profile_absolute(
        profile_root, Path(str(manifest["backup_root"])), "provider hook backup root"
    )
    for item in reversed(list(manifest["targets"])):
        target = _profile_path(profile_root, str(item["relative"]), "provider hook target")
        current, _ = _snapshot(target)
        if current != item["before"] and current != item["after"]:
            raise StorageRefusal(
                "hook_upgrade_rollback_ambiguous",
                "provider hook target changed outside the owned upgrade",
            )
        if current == item["before"]:
            continue
        if item["before"]["exists"]:
            backup = _profile_absolute(
                profile_root,
                backup_root / str(item["backup"]),
                "provider hook backup",
            )
            snapshot, payload = _snapshot(backup)
            if snapshot != item["before"] or payload is None:
                raise StorageRefusal("hook_upgrade_backup_mismatch", "provider hook backup is invalid")
            atomic_write(target, payload, mode=int(item["before"]["mode"]))
        else:
            target.unlink(missing_ok=True)
        restored, _ = _snapshot(target)
        if restored != item["before"]:
            raise StorageRefusal("hook_upgrade_rollback_failed", "provider hook rollback did not verify")
    for relative in sorted(
        manifest["created_directories"], key=lambda item: len(Path(item).parts), reverse=True
    ):
        directory = _profile_path(
            profile_root, relative, "upgrade-created directory"
        )
        try:
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            if not directory.is_dir() or directory.is_symlink():
                raise StorageRefusal(
                    "hook_upgrade_rollback_ambiguous",
                    "upgrade-created directory identity changed",
                )


def upgrade_provider_hooks(
    registry: Any,
    *,
    source_root: Path,
    profile_root: Path,
    stable_watcher: Path,
    manifest_path: Path,
    fault: Optional[FaultInjector] = None,
) -> Mapping[str, Any]:
    """Atomically upgrade every registered provider hook with exact rollback bytes."""

    source = _root(source_root, "release root")
    profile = _root(profile_root, "provider profile root")
    watcher = Path(stable_watcher)
    if not watcher.is_absolute() or not watcher.is_file():
        raise StorageRefusal("hook_upgrade_invalid", "stable watcher command is unavailable")
    watcher = watcher.resolve() if not watcher.is_symlink() else watcher.absolute()
    manifest_file = Path(manifest_path)
    if not manifest_file.is_absolute() or manifest_file.is_symlink():
        raise StorageRefusal("hook_upgrade_invalid", "upgrade manifest path must be absolute")
    if manifest_file.exists():
        existing = _manifest(manifest_file)
        _validate_manifest_identity(
            existing, profile_root=profile, source_root=source, stable_watcher=watcher
        )
        if existing["state"] == "active":
            if any(
                _snapshot(_profile_path(profile, str(item["relative"]), "provider hook target"))[0]
                != item["after"]
                for item in existing["targets"]
            ):
                raise StorageRefusal("hook_upgrade_drift", "installed provider hooks drifted")
            return _receipt(existing, idempotent=True)
        if existing["state"] == "prepared":
            _restore(existing)
            recovered = {**existing, "state": "rolled_back"}
            atomic_write(manifest_file, stable_json(recovered))
            return _receipt(recovered, idempotent=False)
        return _receipt(existing, idempotent=True)

    backup_relative = Path(".league-provider-hook-backups") / hashlib.sha256(
        str(manifest_file).encode("utf-8")
    ).hexdigest()[:16]
    backup_root = _profile_path(profile, backup_relative, "provider hook backup root")
    if backup_root.exists() or backup_root.is_symlink():
        raise StorageRefusal("hook_upgrade_invalid", "provider hook backup identity is occupied")
    created_directories: list[str] = _missing_parent_directories(profile, backup_root / "probe")
    adapter_targets: list[tuple[Any, Path, Path]] = []
    for adapter in registry.adapters():
        relative = Path(str(adapter.hook_bootstrap_profile["target_relative"]))
        target = _profile_path(profile, relative, "provider hook target")
        for directory in _missing_parent_directories(profile, target):
            if directory not in created_directories:
                created_directories.append(directory)
        adapter_targets.append((adapter, relative, target))
    targets: list[dict[str, Any]] = []
    backups: list[tuple[str, bytes, int]] = []
    with tempfile.TemporaryDirectory(prefix="league-hook-candidates-") as temporary:
        candidate_root = Path(temporary)
        for adapter, relative, target in adapter_targets:
            before, payload = _snapshot(target)
            backup_name = f"{adapter.contract.kind}.before"
            if payload is not None:
                backups.append((backup_name, payload, int(before["mode"])))
            candidate = candidate_root / relative
            if payload is not None:
                atomic_write(candidate, payload, mode=int(before["mode"]))
            adapter.install_hook_bootstrap(
                source_root=source, target=candidate, stable_watcher=watcher
            )
            after, _ = _snapshot(candidate)
            targets.append(
                {
                    "adapter_kind": adapter.contract.kind,
                    "relative": relative.as_posix(),
                    "backup": backup_name,
                    "before": before,
                    "after": after,
                }
            )
    backup_root.mkdir(parents=True, mode=0o700)
    _profile_absolute(profile, backup_root, "provider hook backup root")
    for backup_name, payload, mode in backups:
        backup = _profile_absolute(
            profile, backup_root / backup_name, "provider hook backup"
        )
        atomic_write(backup, payload, mode=mode)
    manifest: dict[str, Any] = {
        "schema": UPGRADE_SCHEMA,
        "state": "prepared",
        "profile_root": str(profile),
        "source_root": str(source),
        "stable_watcher": str(watcher),
        "backup_root": str(backup_root),
        "created_directories": created_directories,
        "targets": targets,
    }
    atomic_write(manifest_file, stable_json(manifest))
    try:
        for item in targets:
            adapter = registry.adapter(str(item["adapter_kind"]))
            target = _profile_path(profile, str(item["relative"]), "provider hook target")
            adapter.install_hook_bootstrap(
                source_root=source, target=target, stable_watcher=watcher
            )
            if _snapshot(target)[0] != item["after"]:
                raise StorageRefusal("hook_upgrade_verification_failed", "provider hook bytes differ")
            if fault is not None:
                fault(f"provider_hook_upgraded:{item['adapter_kind']}")
    except BaseException:
        _restore(manifest)
        rolled_back = {**manifest, "state": "rolled_back"}
        atomic_write(manifest_file, stable_json(rolled_back))
        raise
    active = {**manifest, "state": "active"}
    atomic_write(manifest_file, stable_json(active))
    return _receipt(active, idempotent=False)


def rollback_provider_hooks(
    *,
    profile_root: Path,
    source_root: Path,
    stable_watcher: Path,
    manifest_path: Path,
) -> Mapping[str, Any]:
    profile = _root(profile_root, "provider profile root")
    source = _root(source_root, "release root")
    watcher = Path(stable_watcher)
    watcher = watcher.resolve() if not watcher.is_symlink() else watcher.absolute()
    manifest = _manifest(manifest_path)
    _validate_manifest_identity(
        manifest, profile_root=profile, source_root=source, stable_watcher=watcher
    )
    if manifest["state"] == "rolled_back":
        if any(
            _snapshot(_profile_path(profile, str(item["relative"]), "provider hook target"))[0]
            != item["before"]
            for item in manifest["targets"]
        ):
            raise StorageRefusal("hook_upgrade_drift", "rolled-back provider hooks drifted")
        return _receipt(manifest, idempotent=True)
    _restore(manifest)
    rolled_back = {**manifest, "state": "rolled_back"}
    atomic_write(manifest_path, stable_json(rolled_back))
    return _receipt(rolled_back, idempotent=False)
