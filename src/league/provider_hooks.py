"""Provider-neutral facade for source-managed hook bootstrap installation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .storage_types import StorageRefusal


def load_hook_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageRefusal(
            "hook_bootstrap_invalid", "provider hook configuration is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise StorageRefusal(
            "hook_bootstrap_invalid", "provider hook configuration must be an object"
        )
    return value


def stable_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.league-hook.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
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
