from ...adapter_types import HARNESS_CAPABILITIES, AdapterContract
from ...provider_lifecycle import ProviderLifecycle
from ...storage_types import StorageRefusal
from ..base import (
    DeclaredAgentAdapter,
    native_assignment,
    native_presentation,
    no_replacement_descriptor_transactions,
)
from ..core import declared_lifecycle_operations


def _deliver(*, store, at, multiplexer, target, envelope, **_unused):
    if "steering_delivery" not in multiplexer.capabilities:
        raise StorageRefusal(
            "multiplexer_steering_unsupported",
            "selected multiplexer has no state-aware steering transport",
        )
    return multiplexer.steering_delivery(
        store=store, at=at, target=target, envelope=envelope
    )


def _launch_arguments(*, model, effort, state_root, resume_session, **_unused):
    arguments = [
        "--model",
        f"{model}[effort={effort}]",
        "--sandbox",
        "enabled",
        "--add-dir",
        str(state_root),
    ]
    if resume_session is not None:
        arguments.extend(("--resume", resume_session))
    return tuple(arguments)


def _visible_launch_factory(*, options, multiplexer, launch, **_unused):
    resume_session = launch.get("session_id")
    forbidden = (
        launch.get("project_code"), launch.get("release_root"),
        launch.get("session_path"), launch.get("parent_session_id"),
        launch.get("parent_session_path"),
    )
    valid_mode = (
        launch.get("session_mode") == "create" and resume_session is None
    ) or (
        launch.get("session_mode") == "resume" and isinstance(resume_session, str)
        and bool(resume_session)
    )
    if (
        launch.get("provider_kind") != "cursor"
        or any(value is not None for value in forbidden)
        or not valid_mode
    ):
        raise StorageRefusal(
            "launch_scope_invalid",
            "direct Cursor runtime inputs do not match the selected adapter",
        )
    if "visible_launch" not in multiplexer.capabilities:
        raise StorageRefusal(
            "multiplexer_launch_unsupported",
            "selected multiplexer has no visible launch driver",
        )
    return multiplexer.visible_launch_driver(
        "cursor", options=options, resume_session_id=resume_session
    )


def _presentation(**inputs):
    return native_presentation(**inputs)


def adapter() -> DeclaredAgentAdapter:
    contract = AdapterContract(
        "cursor", "harness", frozenset(HARNESS_CAPABILITIES),
        "inherited-contract", "available",
        "First-class local Cursor CLI adapter; distinct from Cursor configured as a Pi provider.",
    )
    native_events = {
        "beforeSubmitPrompt": "prompt_intake",
        "beforeShellExecution": "pre_tool_authorization",
        "stop": "stop_supervision",
    }
    return DeclaredAgentAdapter(
        contract,
        native_events,
        ("conversation_id",),
        ("source_event_key", "generation_id"),
        declared_lifecycle_operations(contract, native_events),
        frozenset({"cursor-agent"}),
        frozenset({"cursor"}),
        {},
        ProviderLifecycle(
            "cursor", "cursor-thread", "cursor", "/exit", True, _launch_arguments
        ),
        {
            "prompt_intake": {
                "command": "cursor-before-submit-hook", "native_event": "beforeSubmitPrompt",
                "hook_event": "beforeSubmitPrompt", "session_field": "conversation_id",
                "source_field": "generation_id",
            },
            "pre_tool_authorization": {
                "command": "cursor-pre-tool-hook", "native_event": "beforeShellExecution",
                "hook_event": "beforeShellExecution", "session_field": "conversation_id",
                "source_field": "generation_id",
            },
            "stop_supervision": {
                "command": "cursor-stop-hook", "native_event": "stop",
                "hook_event": "stop", "session_field": "conversation_id",
                "source_field": "generation_id", "output_mode": "followup",
            },
        },
        {
            "launch": frozenset({"visible_launch"}),
            "resume": frozenset({"visible_launch"}),
            "steer": frozenset({"steering_delivery"}),
            "title": frozenset({"title"}),
            "delivery": frozenset({"steering_delivery"}),
            "retirement": frozenset({"stopped_retirement"}),
            "cleanup": frozenset({"production_cleanup"}),
            "replacement": frozenset({"runtime_replacement"}),
        },
        _visible_launch_factory,
        _deliver,
        _presentation,
        native_assignment,
        no_replacement_descriptor_transactions,
    )
