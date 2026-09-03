"""Cursor CLI native hook configuration installation."""

from __future__ import annotations

from pathlib import Path
import shlex
from typing import Any, Mapping

from ...provider_hooks import atomic_write, load_hook_document, stable_json
from ...storage_types import StorageRefusal


BOOTSTRAP_PROFILE = {
    "schema": "league.provider-hook-bootstrap.v1",
    "profile_loaded": True,
    "activation": "native_hook_payload",
    "target_relative": ".cursor/hooks.json",
    "source_relative": None,
    "launch_enforcement": "native",
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
    del source_root
    if bootstrap_profile != BOOTSTRAP_PROFILE or adapter_kind != "cursor":
        raise StorageRefusal("hook_bootstrap_invalid", "Cursor bootstrap declaration changed")
    document = load_hook_document(target)
    if document.get("version") != 1 or not isinstance(document.get("hooks"), dict):
        raise StorageRefusal("hook_bootstrap_invalid", "Cursor hook configuration is malformed")
    hooks = document["hooks"]
    added: list[str] = []
    for profile in hook_profile.values():
        event = str(profile["native_event"])
        command = shlex.join((str(stable_watcher), str(profile["command"])))
        handlers = hooks.setdefault(event, [])
        if not isinstance(handlers, list):
            raise StorageRefusal("hook_bootstrap_invalid", "Cursor hook event is malformed")
        matches = [
            item
            for item in handlers
            if isinstance(item, dict) and item.get("command") == command
        ]
        if len(matches) > 1:
            raise StorageRefusal("hook_bootstrap_ambiguous", "Cursor League hook is duplicated")
        if not matches:
            handlers.append({"command": command})
            added.append(event)
    if added:
        atomic_write(target, stable_json(document))
    return {"adapter_kind": adapter_kind, "target": str(target), "added": added}
