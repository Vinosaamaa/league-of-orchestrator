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


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise StorageRefusal(
            "hook_native_input_invalid", f"Codex hook field is missing: {name}"
        )
    return value


def translate_input(operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate and canonicalize one real Codex hook envelope."""

    if not isinstance(payload, Mapping):
        raise StorageRefusal("hook_native_input_invalid", "Codex hook input must be an object")
    expected = {
        "prompt_intake": "UserPromptSubmit",
        "pre_tool_authorization": "PreToolUse",
        "stop_supervision": "Stop",
    }.get(operation)
    if expected is None or payload.get("hook_event_name") != expected:
        raise StorageRefusal("hook_native_input_invalid", "Codex hook event is invalid")
    _required_text(payload, "session_id")
    _required_text(payload, "turn_id")
    if operation == "prompt_intake":
        _required_text(payload, "prompt")
    elif operation == "pre_tool_authorization":
        _required_text(payload, "tool_name")
        _required_text(payload, "tool_use_id")
        if not isinstance(payload.get("tool_input"), Mapping):
            raise StorageRefusal(
                "hook_native_input_invalid", "Codex PreToolUse tool_input must be an object"
            )
    elif not isinstance(payload.get("stop_hook_active"), bool):
        raise StorageRefusal(
            "hook_native_input_invalid", "Codex Stop activity flag is missing"
        )
    return {**dict(payload), "league_adapter_kind": "codex"}


def translate_output(operation: str, output: Mapping[str, Any]) -> Mapping[str, Any]:
    """Render the exact Codex hook response schema."""

    if operation == "pre_tool_authorization":
        decision = output.get("decision", "accept")
        if decision not in {"accept", "refuse"}:
            raise StorageRefusal("hook_translation_invalid", "Codex policy decision is invalid")
        native: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if decision == "accept" else "deny",
        }
        if decision == "refuse":
            native["permissionDecisionReason"] = str(
                output.get("reason_code", "league_policy_refused")
            )
        return {"hookSpecificOutput": native}
    if operation in {"prompt_intake", "stop_supervision"}:
        return {
            key: value
            for key, value in output.items()
            if key in {"decision", "reason"}
        }
    raise StorageRefusal("hook_translation_invalid", "Codex hook operation is invalid")


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
    document = {"hooks": {}} if not target.exists() else load_hook_document(target)
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
