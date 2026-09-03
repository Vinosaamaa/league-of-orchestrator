import hashlib
from typing import Any, Mapping

from ...adapter_types import HARNESS_CAPABILITIES, AdapterContract
from ...provider_lifecycle import ProviderLifecycle
from ...storage_types import StorageRefusal
from ..base import (
    DeclaredAgentAdapter,
    deliver_via_multiplexer,
    steer_via_multiplexer,
)
from ..core import declared_lifecycle_operations
from .descriptor import replacement_descriptor_transactions
from .hooks import (
    BOOTSTRAP_PROFILE,
    install as install_hook_bootstrap,
    translate_input,
    translate_output,
)


def _launch_arguments(
    *, model, effort, release_root, resume_session, provider_kind, **_unused
):
    integration = release_root / "integrations" / "pi" / "league-runtime.ts"
    profile = release_root / "integrations" / "pi" / "league-bash.sb"
    watcher = release_root / "bin" / "agent-watcher"
    if any(
        not path.is_file() or path.is_symlink()
        for path in (integration, profile, watcher)
    ):
        raise StorageRefusal(
            "launch_integration_unavailable",
            "Pi lifecycle files are missing from the exact League release",
        )
    if provider_kind not in {"cursor", "codex"}:
        raise StorageRefusal(
            "launch_provider_invalid",
            "Pi launch requires an explicit Cursor or Codex provider",
        )
    arguments = [
        "--approve",
        "--provider",
        "cursor" if provider_kind == "cursor" else "openai-codex",
        "--model",
        model,
        "--thinking",
        effort,
        "--extension",
        str(integration),
    ]
    if resume_session is not None:
        arguments.extend(("--session", resume_session))
    return tuple(arguments)


def _visible_launch_factory(
    *, store, options, multiplexer, launch, startup_timeout_ms, **_unused
):
    from pathlib import Path

    from ...pi_launch import deterministic_pi_session_id

    project_code = launch.get("project_code")
    if not isinstance(project_code, str) or not project_code:
        raise StorageRefusal(
            "launch_scope_invalid", "Pi launch requires an explicit project code"
        )
    descriptor_id = str(
        launch.get("launch_descriptor_id")
        or f"pi-launch:{launch['assignment_id']}"
    )
    requested_session_id = launch.get("session_id")
    if launch.get("session_mode") == "create" and requested_session_id is None:
        requested_session_id = deterministic_pi_session_id(descriptor_id)
    release_root = Path(str(launch["resolved_release_root"])).resolve()
    descriptor = {
        "schema": "league.pi-launch-descriptor.v1",
        "descriptor_id": descriptor_id,
        "assignment_id": launch["assignment_id"],
        "runtime_kind": "pi",
        "provider_kind": launch["provider_kind"],
        "model": launch["model"],
        "effort": launch["effort"],
        "cwd": str(Path(str(launch["worktree"])).resolve()),
        "role": "champion",
        "placement": "new_tab",
        "callsign": "pending",
        "project_code": project_code,
        "task_label": options.task_label,
        "routing_name": "pending",
        "workspace_id": launch["workspace_id"],
        "creator_pane_id": None,
        "state_root": str(Path(str(launch["state_root"])).resolve()),
        "release_root": str(release_root),
        "launch_mode": launch["session_mode"],
        "requested_session_id": requested_session_id,
        "requested_session_path": (
            str(Path(str(launch["session_path"])).resolve())
            if launch.get("session_path") else None
        ),
        "parent_session_id": launch.get("parent_session_id"),
        "parent_session_path": (
            str(Path(str(launch["parent_session_path"])).resolve())
            if launch.get("parent_session_path") else None
        ),
        "routing": dict(launch["routing"]),
    }
    if "visible_launch" not in multiplexer.capabilities:
        raise StorageRefusal(
            "multiplexer_launch_unsupported",
            "selected multiplexer has no visible launch driver",
        )
    return multiplexer.visible_launch_driver(
        "pi",
        store=store,
        descriptor=descriptor,
        at=launch["at"],
        startup_timeout_ms=startup_timeout_ms,
    )


def _presentation(**inputs):
    return _pi_presentation(**inputs)


def _one(rows: list[Any], code: str, message: str) -> Any:
    if len(rows) != 1:
        raise StorageRefusal(code, message)
    return rows[0]


def _assignment(store: Any, row: Mapping[str, Any]) -> str:
    rows = store.connection.execute(
        """
        SELECT assignment_id FROM provider_launch_descriptors
         WHERE session_path=? AND state='active'
        """,
        (row["session_ref"],),
    ).fetchall()
    return str(
        _one(
            list(rows),
            "display_replay_assignment_unproven",
            "Pi runtime does not bind one active canonical assignment",
        )["assignment_id"]
    )


def _pi_presentation(
    store: Any,
    row: Mapping[str, Any],
    assignment_id: str,
    project_code: str,
) -> dict[str, Any]:
    records = store.connection.execute(
        """
        SELECT descriptor_id FROM provider_launch_descriptors
         WHERE assignment_id=? AND state='active'
        """,
        (assignment_id,),
    ).fetchall()
    descriptor_id = str(
        _one(
            list(records),
            "display_replay_descriptor_unproven",
            "Pi runtime does not bind one active canonical launch descriptor",
        )["descriptor_id"]
    )
    stored = store.provider_launch_descriptor(descriptor_id)
    if not isinstance(stored, Mapping):
        raise StorageRefusal(
            "display_replay_descriptor_unproven",
            "Pi launch descriptor is unavailable",
        )
    descriptor = stored.get("descriptor")
    receipt = stored.get("launch_receipt")
    if not isinstance(descriptor, Mapping) or not isinstance(receipt, Mapping):
        raise StorageRefusal(
            "display_replay_descriptor_unproven",
            "Pi launch descriptor lacks an exact active receipt",
        )
    session_ref = str(row["session_ref"])
    if (
        descriptor.get("runtime_kind") != "pi"
        or descriptor.get("assignment_id") != assignment_id
        or descriptor.get("callsign") != row["callsign"]
        or descriptor.get("routing_name") != row["routing_name"]
        or descriptor.get("cwd") != row["worktree"]
        or descriptor.get("project_code") != project_code
        or stored.get("session_path") != session_ref
        or receipt.get("session_path") != session_ref
    ):
        raise StorageRefusal(
            "display_replay_descriptor_mismatch",
            "Pi descriptor does not bind the canonical runtime and session",
        )
    session_id = stored.get("session_id")
    digest = stored.get("descriptor_digest")
    if (
        not isinstance(session_id, str)
        or session_id != row.get("thread_id")
        or not isinstance(digest, str)
    ):
        raise StorageRefusal(
            "display_replay_descriptor_unproven",
            "Pi session identity or descriptor digest is missing",
        )
    metadata_source = f"league:pi-launch:{digest[:16]}"
    task_label = str(descriptor["task_label"])
    title = (
        f"{row['callsign']} · {project_code}"
        if row["role"] == "shotcaller"
        else f"{row['callsign']} · {project_code}|{task_label}"
    )
    tokens = {
        "launch_runtime_kind": "pi",
        "launch_provider_kind": str(descriptor["provider_kind"]),
        "launch_role": str(row["role"]),
        "launch_placement": str(descriptor["placement"]),
        "launch_callsign": str(row["callsign"]),
        "launch_project_code": project_code,
        "launch_task_label": task_label,
        "launch_routing_alias": str(row["routing_name"]),
        "launch_session_id": session_id,
        "launch_session_path_digest": hashlib.sha256(session_ref.encode()).hexdigest(),
        "launch_descriptor_sha256": digest,
        "launch_descriptor_id": descriptor_id,
        "launch_metadata_source": metadata_source,
        "launch_activation_phase": "session_started",
    }
    parent = descriptor.get("parent_session_path")
    if isinstance(parent, str):
        tokens["launch_parent_digest"] = hashlib.sha256(parent.encode()).hexdigest()
    return {
        "provider_kind": str(descriptor["provider_kind"]),
        "task_label": task_label,
        "thread": session_id,
        "metadata_source": metadata_source,
        "applies_to_source": "herdr:pi",
        "title": title,
        "tokens": tokens,
    }


def adapter() -> DeclaredAgentAdapter:
    contract = AdapterContract(
        "pi", "harness", frozenset(HARNESS_CAPABILITIES - {"interrupt"}),
        "inherited-contract", "available",
        "Pi runtime events translated independently of its configured Cursor or Codex provider.",
    )
    native_events = {
        "input": "prompt_intake",
        "tool_call": "pre_tool_authorization",
        "agent_settled": "stop_supervision",
    }
    return DeclaredAgentAdapter(
        contract,
        native_events,
        ("session_path", "session_id"),
        ("source_event_key", "input_id"),
        declared_lifecycle_operations(contract, native_events),
        frozenset({"pi"}),
        frozenset({"codex", "cursor"}),
        {"openai": "codex", "openai-codex": "codex"},
        ProviderLifecycle("pi", "pi-thread", "pi", "/quit", True, _launch_arguments),
        {
            "prompt_intake": {
                "command": "pi-input-hook", "native_event": "input",
                "hook_event": "PiInput", "session_field": "session_path",
                "source_field": "input_id", "stop_feedback_suppression": True,
            },
            "pre_tool_authorization": {
                "command": "pi-pre-tool-hook", "native_event": "tool_call",
                "hook_event": "PiToolCall", "session_field": "session_path",
                "source_field": "input_id",
            },
            "stop_supervision": {
                "command": "pi-stop-hook", "native_event": "agent_settled",
                "hook_event": "PiStop", "session_field": "session_path",
                "source_field": "input_id", "output_mode": "followup",
            },
        },
        BOOTSTRAP_PROFILE,
        install_hook_bootstrap,
        translate_input,
        translate_output,
        {
            "launch": frozenset({"visible_launch"}),
            "resume": frozenset({"provider_session_lifecycle"}),
            "steer": frozenset({"delivery"}),
            "title": frozenset({"title"}),
            "delivery": frozenset({"delivery"}),
            "retirement": frozenset({"stopped_retirement"}),
            "cleanup": frozenset({"production_cleanup"}),
            "replacement": frozenset({"runtime_replacement"}),
        },
        _visible_launch_factory,
        deliver_via_multiplexer,
        steer_via_multiplexer,
        _presentation,
        _assignment,
        replacement_descriptor_transactions,
    )
