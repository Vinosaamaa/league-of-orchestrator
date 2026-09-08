"""Explicit same-pane recovery of a malformed legacy Codex Shotcaller identity.

This is not rollover: a valid provider thread can never be replaced here.
Native inspection is read-only; normal hooks never invoke this recovery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .provider_lifecycle import provider_lifecycle
from .restored_agent import SupervisorWatcherAdapter, restored_runtime_generation, _timestamp
from .storage_types import StorageRefusal


def reconcile_champion_identity(
    store: Any, request: Mapping[str, Any], *, multiplexer: Any, at: str,
    owner_authorized: bool, check_only: bool = False,
) -> dict[str, Any]:
    """Recover one retained session, never infer identity from its pane or title."""
    _timestamp(at)
    if not owner_authorized:
        raise StorageRefusal("owner_authorization_required", "Champion recovery requires explicit owner authority")
    refusal = StorageRefusal("champion_identity_unproven", "exact retained Champion identity did not verify")
    owner = store.agent_status(request["owner_agent_id"])
    actor_row = store.connection.execute(
        "SELECT * FROM agent_instances WHERE agent_id=?", (request["agent_id"],)
    ).fetchone()
    actor = dict(actor_row) if actor_row is not None else None
    row = store.connection.execute(
        "SELECT * FROM runtime_instances WHERE runtime_instance_id=?", (request["runtime_instance_id"],)
    ).fetchone()
    if (owner is None or owner["role"] != "shotcaller" or owner["retired_at"] is not None
        or actor is None or actor["role"] != "champion" or actor["retired_at"] is not None
        or actor["shotcaller_agent_id"] != owner["agent_id"] or row is None
        or row["actor_agent_id"] != actor["agent_id"] or row["backend_kind"] != "herdr"
        or row["session_ref"] != request["session_ref"] or actor["thread_id"] != request["session_ref"]
        or row["endpoint"] != request["endpoint"] or actor["address"] != request["endpoint"]
        or row["status"] not in {"active", "idle", "failed"}
        or not actor.get("worktree") or not actor.get("routing_name") or multiplexer.kind != "herdr"):
        raise refusal
    kind = row["harness_kind"].removesuffix("-thread")
    if actor["kind"].removesuffix("-thread") != kind or not provider_lifecycle(kind).validate_session(request["session_ref"]):
        raise refusal
    inventory = multiplexer.discover()
    context = multiplexer.calling_context()
    owners = [item for item in inventory if item.get("pane_id") == context["pane_id"]
              and item.get("pane_id") == owner["address"]
              and (item.get("agent_session") or {}).get("value") == owner["thread_id"]]
    matches = [item for item in inventory if (item.get("agent_session") or {}).get("value") == request["session_ref"]]
    if len(owners) != 1 or len(matches) != 1:
        raise refusal
    item = matches[0]
    if (item.get("pane_id") != request["endpoint"] or item.get("name") != actor["routing_name"]
        or item.get("agent") != kind or item.get("foreground_cwd") != actor["worktree"]):
        raise refusal
    endpoint = multiplexer.endpoint(request["runtime_instance_id"], item)
    generation = restored_runtime_generation("herdr", endpoint.terminal_id, request["session_ref"])
    if row["runtime_generation"] not in {request["expected_generation"], generation}:
        raise StorageRefusal("runtime_reconcile_version_conflict", "previous Champion generation changed")
    descriptor = {**dict(row), "cwd": actor["worktree"], "verify_native_process": True,
                  "tokens": {"sidebar_name": actor["callsign"]}}
    first = multiplexer.inspect_restored(descriptor, endpoint)
    second = multiplexer.inspect_restored(descriptor, endpoint)
    keys = ("pane_id", "name", "agent", "agent_session", "workspace_id", "tab_id", "terminal_id", "foreground_cwd")
    if (not first.get("process_fingerprint") or first["process_fingerprint"] != second.get("process_fingerprint")
        or any(obs.get("session_ref") != request["session_ref"]
               or any(obs.get("agent", {}).get(key) != item.get(key) for key in keys)
               for obs in (first, second))):
        raise refusal
    if check_only:
        return {"runtime_instance_id": request["runtime_instance_id"], "verified": True,
                "check_only": True, "process_effects": False, "task_completion_effects": False}
    result = store.reconcile_restored_runtime(
        request["runtime_instance_id"], actor["agent_id"], actor["thread_id"], request["session_ref"],
        "herdr", request["endpoint"], request["expected_generation"], request["endpoint"], generation, at,
        recover_observed_failure=True, expected_owner_agent_id=request["owner_agent_id"],
        expected_agent_version=actor["version"],
    )
    return {**result, "process_effects": False, "task_completion_effects": False}


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
