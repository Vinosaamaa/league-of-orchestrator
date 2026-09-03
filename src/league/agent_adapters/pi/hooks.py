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


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise StorageRefusal(
            "hook_native_input_invalid", f"Pi hook field is missing: {name}"
        )
    return value


def translate_input(operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the provider-neutral envelope emitted by the installed Pi extension."""

    if not isinstance(payload, Mapping):
        raise StorageRefusal("hook_native_input_invalid", "Pi hook input must be an object")
    expected = {
        "prompt_intake": "PiInput",
        "pre_tool_authorization": "PiToolCall",
        "stop_supervision": "PiStop",
    }.get(operation)
    if expected is None or payload.get("hook_event_name") != expected:
        raise StorageRefusal("hook_native_input_invalid", "Pi hook event is invalid")
    _required_text(payload, "session_id")
    session_path = Path(_required_text(payload, "session_path"))
    if not session_path.is_absolute():
        raise StorageRefusal("hook_native_input_invalid", "Pi session path must be absolute")
    _required_text(payload, "input_id")
    if operation == "prompt_intake":
        _required_text(payload, "prompt")
    elif operation == "pre_tool_authorization":
        _required_text(payload, "tool_name")
        if not isinstance(payload.get("tool_input"), Mapping):
            raise StorageRefusal(
                "hook_native_input_invalid", "Pi tool_call input must be an object"
            )
    return {**dict(payload), "league_adapter_kind": "pi"}


def translate_output(operation: str, output: Mapping[str, Any]) -> Mapping[str, Any]:
    if operation not in {
        "prompt_intake", "pre_tool_authorization", "stop_supervision"
    }:
        raise StorageRefusal("hook_translation_invalid", "Pi hook operation is invalid")
    return dict(output)


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
