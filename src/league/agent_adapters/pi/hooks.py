"""Pi profile extension installation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ...provider_hooks import atomic_write
from ...storage_types import StorageRefusal


BOOTSTRAP_PROFILE = {
    "schema": "league.provider-hook-bootstrap.v1",
    "profile_loaded": True,
    "activation": "exact_canonical_binding",
    "target_relative": ".pi/agent/extensions/league-hooks.mjs",
    "source_relative": "integrations/pi/league-hooks.mjs",
    "launch_enforcement": "separate",
}


def install(
    *,
    adapter_kind: str,
    hook_profile: Mapping[str, Mapping[str, Any]],
    bootstrap_profile: Mapping[str, Any],
    source_root: Path,
    target: Path,
    stable_watcher: Path,
) -> Mapping[str, Any]:
    del hook_profile, stable_watcher
    if bootstrap_profile != BOOTSTRAP_PROFILE or adapter_kind != "pi":
        raise StorageRefusal("hook_bootstrap_invalid", "Pi bootstrap declaration changed")
    source = source_root / str(bootstrap_profile["source_relative"])
    if not source.is_file() or source.is_symlink() or target.is_symlink():
        raise StorageRefusal("hook_bootstrap_invalid", "Pi bootstrap asset is unavailable")
    payload = source.read_bytes()
    if not payload or len(payload) > 1024 * 1024:
        raise StorageRefusal("hook_bootstrap_invalid", "Pi bootstrap asset size is invalid")
    if target.exists() and not target.is_file():
        raise StorageRefusal("hook_bootstrap_invalid", "Pi bootstrap target is not a file")
    changed = True
    if target.is_file():
        with target.open("rb") as handle:
            changed = handle.read(len(payload) + 1) != payload
    if changed:
        atomic_write(target, payload)
    return {
        "adapter_kind": adapter_kind,
        "target": str(target),
        "added": ["profile_extension"] if changed else [],
    }
