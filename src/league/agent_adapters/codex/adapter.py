from ...adapter_types import HARNESS_CAPABILITIES, AdapterContract
from ...provider_lifecycle import ProviderLifecycle
from ...storage_types import StorageRefusal
from ..base import (
    DeclaredAgentAdapter,
    deliver_via_multiplexer,
    native_assignment,
    native_presentation,
    no_replacement_descriptor_transactions,
)
from ..core import declared_lifecycle_operations


def _launch_arguments(*, model, effort, state_root, **_unused):
    return (
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{effort}"',
        "--add-dir",
        str(state_root),
    )


def _visible_launch_factory(*, store, options, multiplexer, launch, **_unused):
    from ...continuation import continuation_resume_thread

    resume_thread_id = continuation_resume_thread(
        store,
        assignment_id=launch["assignment_id"],
        task_id=launch["task_id"],
        champion_agent_id=launch["champion_agent_id"],
        repository=launch["repository"],
        issue=launch["issue"],
        branch=launch["branch"],
        worktree=launch["worktree"],
        at=launch["at"],
    )
    forbidden = (
        launch.get("project_code"), launch.get("release_root"),
        launch.get("session_path"), launch.get("parent_session_id"),
        launch.get("parent_session_path"), launch.get("session_id"),
    )
    if (
        launch.get("provider_kind") != "codex"
        or any(value is not None for value in forbidden)
        or launch.get("session_mode") != "create"
    ):
        raise StorageRefusal(
            "launch_scope_invalid",
            "direct Codex runtime inputs do not match the selected adapter",
        )
    if "visible_launch" not in multiplexer.capabilities:
        raise StorageRefusal(
            "multiplexer_launch_unsupported",
            "selected multiplexer has no visible launch driver",
        )
    return multiplexer.visible_launch_driver(
        "codex", options=options, resume_session_id=resume_thread_id
    )


def _presentation(**inputs):
    return native_presentation(**inputs)


def adapter() -> DeclaredAgentAdapter:
    contract = AdapterContract(
        "codex", "harness", frozenset(HARNESS_CAPABILITIES - {"interrupt"}),
        "inherited-contract", "available",
        "Codex lifecycle translated through the shared agent adapter contract.",
    )
    native_events = {
        "UserPromptSubmit": "prompt_intake",
        "PreToolUse": "pre_tool_authorization",
        "Stop": "stop_supervision",
    }
    return DeclaredAgentAdapter(
        contract,
        native_events,
        ("session_id",),
        ("source_event_key", "turn_id"),
        declared_lifecycle_operations(contract, native_events),
        frozenset({"codex"}),
        frozenset({"codex"}),
        {"openai": "codex", "openai-codex": "codex"},
        ProviderLifecycle(
            "codex", "codex-thread", "codex", "/exit", True, _launch_arguments
        ),
        {
            "prompt_intake": {
                "command": "codex-user-prompt-hook", "native_event": "UserPromptSubmit",
                "hook_event": "UserPromptSubmit", "session_field": "session_id",
                "source_field": "turn_id", "invocation_identity": True,
                "stop_feedback_suppression": True,
            },
            "pre_tool_authorization": {
                "command": "codex-pre-tool-hook", "native_event": "PreToolUse",
                "hook_event": "PreToolUse", "session_field": "session_id",
                "source_field": "turn_id",
            },
            "stop_supervision": {
                "command": "codex-stop-hook", "native_event": "Stop",
                "hook_event": "Stop", "session_field": "session_id",
                "source_field": "turn_id", "active_field": "stop_hook_active",
                "output_mode": "decision",
            },
        },
        {
            "launch": frozenset({"visible_launch"}),
            "resume": frozenset({"visible_launch"}),
            "steer": frozenset({"delivery"}),
            "title": frozenset({"title"}),
            "delivery": frozenset({"delivery"}),
            "retirement": frozenset({"stopped_retirement"}),
            "cleanup": frozenset({"production_cleanup"}),
            "replacement": frozenset({"runtime_replacement"}),
        },
        _visible_launch_factory,
        deliver_via_multiplexer,
        _presentation,
        native_assignment,
        no_replacement_descriptor_transactions,
    )
