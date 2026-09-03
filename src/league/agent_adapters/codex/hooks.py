"""Codex-native hook configuration installation."""

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
    "target_relative": ".codex/hooks.json",
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
    if bootstrap_profile != BOOTSTRAP_PROFILE or adapter_kind != "codex":
        raise StorageRefusal("hook_bootstrap_invalid", "Codex bootstrap declaration changed")
    document = load_hook_document(target)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        raise StorageRefusal("hook_bootstrap_invalid", "Codex hook configuration is malformed")
    added: list[str] = []
    for profile in hook_profile.values():
        event = str(profile["native_event"])
        command = shlex.join((str(stable_watcher), str(profile["command"])))
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise StorageRefusal("hook_bootstrap_invalid", "Codex hook event is malformed")
        if any(
            not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
            for group in groups
        ):
            raise StorageRefusal("hook_bootstrap_invalid", "Codex hook group is malformed")
        matches = [
            handler
            for group in groups
            for handler in group.get("hooks", [])
            if isinstance(handler, dict) and handler.get("command") == command
        ]
        if len(matches) > 1:
            raise StorageRefusal("hook_bootstrap_ambiguous", "Codex League hook is duplicated")
        if not matches:
            groups.append(
                {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
            )
            added.append(event)
    if added:
        atomic_write(target, stable_json(document))
    return {"adapter_kind": adapter_kind, "target": str(target), "added": added}
