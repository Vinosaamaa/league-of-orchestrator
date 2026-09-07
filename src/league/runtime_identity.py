"""Explicit same-pane recovery of a malformed legacy Codex Shotcaller identity.

This is not rollover: a valid provider thread can never be replaced here.
Native inspection is read-only; normal hooks never invoke this recovery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .provider_lifecycle import provider_lifecycle
from .restored_agent import SupervisorWatcherAdapter, restored_runtime_generation
from .storage_types import StorageRefusal


def validate_registration(store: Any, command: Any) -> None:
    """Validate native CLI evidence before allowing a verified registration."""
    if not command.verified:
        return
    kind = command.harness_kind.removesuffix("-thread")
    profile = provider_lifecycle(kind)
    actor = store.agent_status(command.actor_agent_id)
    if not profile.validate_session(command.session_ref):
        raise StorageRefusal("runtime_session_invalid", "provider session identity is invalid")
    if actor is not None and actor["kind"] != "unbound" and (
        actor["kind"].removesuffix("-thread") != kind
        or actor["thread_id"] != command.session_ref
        or actor["backend"] != command.backend_kind
        or actor["address"] != command.endpoint
    ):
        raise StorageRefusal(
            "runtime_identity_mismatch",
            "verified registration differs from the canonical agent; use exact identity recovery",
        )


def repair_shotcaller_identity(
    store: Any, request: Mapping[str, Any], *, multiplexer: Any,
    cwd: str, at: str, owner_authorized: bool, watcher: Any = None,
) -> dict[str, Any]:
    if not owner_authorized:
        raise StorageRefusal("owner_authorization_required", "identity repair requires explicit owner authority")
    profile = provider_lifecycle("codex")
    if (
        request["thread_id"] != request["agent_id"]
        or not profile.validate_session(request["thread_id"])
        or profile.validate_session(request["expected_session_ref"])
    ):
        raise StorageRefusal(
            "runtime_identity_repair_refused",
            "only malformed legacy identities with an independently matching Codex actor ID can be repaired",
        )
    actor = store.agent_status(request["agent_id"])
    if actor is None or actor["role"] != "shotcaller" or actor["retired_at"] is not None:
        raise StorageRefusal("runtime_identity_repair_refused", "target is not an active Shotcaller")
    context = multiplexer.calling_context()
    if multiplexer.kind != "herdr" or context["pane_id"] != request["endpoint"]:
        raise StorageRefusal("runtime_identity_repair_refused", "recovery is restricted to the calling Herdr pane")
    binding = store.supervisor_binding(actor["callsign"])
    if binding["actor_agent_id"] != request["agent_id"] or binding["runtime_instance_id"] != request["runtime_instance_id"]:
        raise StorageRefusal("runtime_identity_repair_refused", "supervisor does not bind the exact target runtime")
    items = [item for item in multiplexer.discover() if item.get("pane_id") == request["endpoint"]]
    if len(items) != 1:
        raise StorageRefusal("runtime_identity_repair_refused", "native pane identity is ambiguous")
    endpoint = multiplexer.endpoint(request["runtime_instance_id"], items[0])
    if endpoint.workspace_id != context["workspace_id"] or endpoint.tab_id != context["tab_id"]:
        raise StorageRefusal("runtime_identity_repair_refused", "native calling context changed")
    presentation = {
        "agent_id": request["agent_id"], "runtime_instance_id": request["runtime_instance_id"],
        "session_ref": binding["session_ref"], "cwd": cwd,
        "tokens": {"sidebar_name": actor["callsign"]}, "verify_native_process": True,
    }
    watchers = watcher or SupervisorWatcherAdapter(store, at)
    before = multiplexer.inspect_restored(presentation, endpoint)
    after = multiplexer.inspect_restored(presentation, endpoint)
    for observation in (before, after):
        process = observation.get("process")
        argv = process.get("argv") if isinstance(process, Mapping) else None
        if (
            observation["session_ref"] != request["thread_id"]
            or observation["session_source"] != "herdr:codex"
            or observation["agent"].get("agent") != "codex"
            or observation["agent"].get("agent_status") not in {"working", "idle", "waiting"}
            or not isinstance(argv, list) or not argv
            or not isinstance(argv[0], str) or Path(argv[0]).name != "codex"
        ):
            raise StorageRefusal("runtime_identity_repair_refused", "native Codex session did not verify")
    if before["process_fingerprint"] != after["process_fingerprint"]:
        raise StorageRefusal("runtime_identity_repair_refused", "native process changed during verification")
    generation = restored_runtime_generation("herdr", endpoint.terminal_id, request["thread_id"])
    proof = {"terminal_id": endpoint.terminal_id, "process_fingerprint": after["process_fingerprint"],
             "session_source": after["session_source"], "cwd": cwd, "stable_readbacks": 2}
    pending = store.pending_shotcaller_identity_repair(dict(request), generation, proof)
    preflight = (watchers.preflight(presentation) if pending is None else
                 watchers.preflight(presentation, pending_repair=pending))
    if pending is None and binding["session_ref"] == request["expected_session_ref"] and (
        preflight.get("runtime_generation") != request["expected_generation"]
        or preflight.get("endpoint") != request["endpoint"]
        or preflight.get("session_ref") != request["expected_session_ref"]
        or type(preflight.get("fence")) is not int or preflight["fence"] < 1
        or not isinstance(preflight.get("locator"), str) or not preflight["locator"].startswith("unix:")
    ):
        raise StorageRefusal("runtime_identity_repair_refused", "prior watcher identity did not verify")
    proof["watcher"] = {key: preflight.get(key) for key in
                        ("locator", "fence", "runtime_generation", "endpoint", "session_ref")}
    runtime = store.repair_shotcaller_identity(dict(request), generation, proof, at)
    presentation["session_ref"] = request["thread_id"]
    try:
        watchers.bind(presentation, runtime, preflight)
        watchers.verify(presentation, runtime)
        final = multiplexer.inspect_restored(presentation, endpoint)
        if final["session_ref"] != request["thread_id"] or final["process_fingerprint"] != after["process_fingerprint"]:
            raise StorageRefusal("runtime_identity_repair_refused", "native process changed after repair")
    except StorageRefusal as exc:
        store.record_restored_runtime_recovery(request["runtime_instance_id"], request["agent_id"], exc.code, at,
            next_action="retry runtime repair-shotcaller-identity with the exact original arguments")
        raise StorageRefusal("runtime_identity_repair_pending", "identity committed; exact watcher recovery retry is required", retryable=True) from exc
    store.satisfy_restored_runtime_recovery(request["runtime_instance_id"], at)
    return {**runtime, "watcher_verified": True, "native_identity_verified": True}
