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


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise StorageRefusal(
            "hook_native_input_invalid", f"Cursor hook field is missing: {name}"
        )
    return value


def translate_input(operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate and canonicalize one real Cursor CLI hook envelope."""

    if not isinstance(payload, Mapping):
        raise StorageRefusal("hook_native_input_invalid", "Cursor hook input must be an object")
    expected = {
        "prompt_intake": "beforeSubmitPrompt",
        "pre_tool_authorization": "preToolUse",
        "stop_supervision": "stop",
    }.get(operation)
    if expected is None or payload.get("hook_event_name") != expected:
        raise StorageRefusal("hook_native_input_invalid", "Cursor hook event is invalid")
    _required_text(payload, "conversation_id")
    _required_text(payload, "generation_id")
    if operation == "prompt_intake":
        _required_text(payload, "prompt")
    elif operation == "pre_tool_authorization":
        _required_text(payload, "tool_name")
        _required_text(payload, "tool_use_id")
        _required_text(payload, "cwd")
        if not isinstance(payload.get("tool_input"), Mapping):
            raise StorageRefusal(
                "hook_native_input_invalid", "Cursor preToolUse tool_input must be an object"
            )
    elif (
        payload.get("status") not in {"completed", "aborted", "error"}
        or not isinstance(payload.get("loop_count"), int)
        or isinstance(payload.get("loop_count"), bool)
    ):
        raise StorageRefusal("hook_native_input_invalid", "Cursor stop state is invalid")
    return {**dict(payload), "league_adapter_kind": "cursor"}


def translate_output(operation: str, output: Mapping[str, Any]) -> Mapping[str, Any]:
    """Render the exact Cursor CLI hook response schema."""

    if operation == "prompt_intake":
        return {"continue": True}
    if operation == "pre_tool_authorization":
        decision = output.get("decision", "accept")
        if decision not in {"accept", "refuse"}:
            raise StorageRefusal("hook_translation_invalid", "Cursor policy decision is invalid")
        native: dict[str, Any] = {
            "permission": "allow" if decision == "accept" else "deny"
        }
        if decision == "refuse":
            native["user_message"] = str(
                output.get("reason_code", "league_policy_refused")
            )
        return native
    if operation == "stop_supervision":
        followup = output.get("followup_message")
        return {"followup_message": followup} if isinstance(followup, str) and followup else {}
    raise StorageRefusal("hook_translation_invalid", "Cursor hook operation is invalid")


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
    document = (
        {"version": 1, "hooks": {}}
        if not target.exists()
        else load_hook_document(target)
    )
    if document.get("version") != 1 or not isinstance(document.get("hooks"), dict):
        raise StorageRefusal("hook_bootstrap_invalid", "Cursor hook configuration is malformed")
    hooks = document["hooks"]
    added: list[str] = []
    pre_tool_command = shlex.join(
        (str(stable_watcher), str(hook_profile["pre_tool_authorization"]["command"]))
    )
    legacy_handlers = hooks.get("beforeShellExecution")
    if isinstance(legacy_handlers, list):
        retained = [
            item
            for item in legacy_handlers
            if not (isinstance(item, dict) and item.get("command") == pre_tool_command)
        ]
        if len(retained) != len(legacy_handlers):
            hooks["beforeShellExecution"] = retained
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
        required_handler = (
            {"command": command, "failClosed": True}
            if profile["native_event"] == "preToolUse"
            else {"command": command}
        )
        if not matches:
            handlers.append(required_handler)
            added.append(event)
        elif matches[0] != required_handler:
            matches[0].clear()
            matches[0].update(required_handler)
            added.append(event)
    if added:
        atomic_write(target, stable_json(document))
    return {"adapter_kind": adapter_kind, "target": str(target), "added": added}
