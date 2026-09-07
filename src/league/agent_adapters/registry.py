"""Explicit built-in agent adapter registry."""

from __future__ import annotations

from pathlib import PurePosixPath

from ..storage_types import StorageRefusal
from ..multiplexer_adapters.contract import MULTIPLEXER_OPERATIONS
from .core import (
    ADAPTER_OPERATIONS,
    OPERATION_CAPABILITIES,
    OPERATION_METHODS,
    AgentLifecycleAdapter,
)
from .codex import adapter as codex_adapter
from .cursor_cli import adapter as cursor_cli_adapter
from .pi import adapter as pi_adapter


class AgentAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AgentLifecycleAdapter] = {}

    def register(self, adapter: AgentLifecycleAdapter) -> None:
        kind = adapter.contract.kind
        if kind in self._adapters:
            raise StorageRefusal("adapter_conflict", f"agent adapter is already registered: {kind}")
        unknown = adapter.lifecycle_operations - ADAPTER_OPERATIONS
        missing_methods = sorted(
            operation
            for operation in adapter.lifecycle_operations
            if any(
                not callable(getattr(adapter, method, None))
                for method in OPERATION_METHODS[operation]
            )
        )
        unsupported = sorted(
            operation
            for operation in adapter.lifecycle_operations
            if not OPERATION_CAPABILITIES.get(operation, frozenset())
            <= adapter.contract.capabilities
        )
        hook_operations = {
            "prompt_intake", "pre_tool_authorization", "stop_supervision"
        }
        profiles = getattr(adapter, "hook_profile", None)
        invalid_profiles = not isinstance(profiles, dict) or set(profiles) != hook_operations
        if not invalid_profiles:
            commands: set[str] = set()
            for operation in hook_operations:
                profile = profiles[operation]
                required = {
                    "command", "native_event", "hook_event", "session_field", "source_field"
                }
                if (
                    not isinstance(profile, dict)
                    or not required <= set(profile)
                    or any(
                        not isinstance(profile[key], str) or not profile[key]
                        for key in required
                    )
                    or profile["native_event"] not in adapter.native_events
                    or adapter.native_events[profile["native_event"]] != operation
                    or profile["command"] in commands
                ):
                    invalid_profiles = True
                    break
                commands.add(profile["command"])
        invalid_launch = (
            "launch" in adapter.lifecycle_operations
            and not callable(getattr(adapter, "visible_launch_factory", None))
        )
        bootstrap = getattr(adapter, "hook_bootstrap_profile", None)
        required_bootstrap = {
            "schema",
            "profile_loaded",
            "activation",
            "target_relative",
            "source_relative",
            "launch_enforcement",
        }
        invalid_bootstrap = (
            not isinstance(bootstrap, dict)
            or set(bootstrap) != required_bootstrap
            or bootstrap.get("schema") != "league.provider-hook-bootstrap.v1"
            or bootstrap.get("profile_loaded") is not True
            or bootstrap.get("activation") not in {
                "exact_canonical_binding",
                "native_hook_payload",
            }
            or bootstrap.get("launch_enforcement") not in {"native", "separate"}
            or not callable(getattr(adapter, "hook_bootstrap_installer", None))
        )
        invalid_hook_translation = any(
            not callable(getattr(adapter, attribute, None))
            for attribute in ("hook_input_translator", "hook_output_translator")
        )
        if not invalid_bootstrap:
            target_relative = bootstrap["target_relative"]
            source_relative = bootstrap["source_relative"]
            for relative in (target_relative, source_relative):
                if relative is None:
                    continue
                if not isinstance(relative, str) or not relative:
                    invalid_bootstrap = True
                    break
                candidate = PurePosixPath(relative)
                if (
                    candidate.is_absolute()
                    or ".." in candidate.parts
                    or "." in candidate.parts
                ):
                    invalid_bootstrap = True
                    break
        invalid_delivery = (
            "delivery" in adapter.lifecycle_operations
            and not callable(getattr(adapter, "delivery_handler", None))
        )
        invalid_steering = (
            "steer" in adapter.lifecycle_operations
            and not callable(getattr(adapter, "steering_handler", None))
        )
        invalid_providers = (
            not isinstance(getattr(adapter, "provider_kinds", None), frozenset)
            or not adapter.provider_kinds
            or any(
                not isinstance(provider, str) or not provider
                for provider in adapter.provider_kinds
            )
        )
        aliases = getattr(adapter, "provider_aliases", None)
        invalid_aliases = (
            not isinstance(aliases, dict)
            or any(
                not isinstance(alias, str)
                or not alias
                or target not in adapter.provider_kinds
                for alias, target in aliases.items()
            )
            or not callable(getattr(adapter, "normalize_provider", None))
        )
        invalid_presentation = not callable(
            getattr(adapter, "presentation_factory", None)
        )
        invalid_assignment = not callable(
            getattr(adapter, "assignment_factory", None)
        )
        invalid_descriptor_factory = (
            "replacement" in adapter.lifecycle_operations
            and not callable(
                getattr(adapter, "replacement_descriptor_factory", None)
            )
        )
        multiplexer_requirements = getattr(adapter, "multiplexer_requirements", None)
        invalid_multiplexer_requirements = (
            not isinstance(multiplexer_requirements, dict)
            or not set(multiplexer_requirements) <= adapter.lifecycle_operations
            or any(
                not isinstance(required, frozenset)
                or not required <= MULTIPLEXER_OPERATIONS
                for required in multiplexer_requirements.values()
            )
        )
        if (
            unknown or missing_methods or unsupported or invalid_profiles
            or invalid_bootstrap
            or invalid_hook_translation
            or invalid_launch or invalid_delivery or invalid_steering
            or invalid_providers
            or invalid_presentation or invalid_assignment or invalid_aliases
            or invalid_descriptor_factory
            or invalid_multiplexer_requirements
        ):
            raise StorageRefusal(
                "adapter_contract_invalid",
                "agent adapter advertises an unknown, non-callable, or unsupported lifecycle operation",
            )
        self._adapters[kind] = adapter

    def adapter(self, kind: str) -> AgentLifecycleAdapter:
        try:
            return self._adapters[kind]
        except KeyError as exc:
            raise StorageRefusal("adapter_unknown", f"agent adapter is not registered: {kind}") from exc

    def adapters(self) -> tuple[AgentLifecycleAdapter, ...]:
        return tuple(self._adapters[kind] for kind in sorted(self._adapters))


def builtin_agent_adapter_registry() -> AgentAdapterRegistry:
    registry = AgentAdapterRegistry()
    for factory in (codex_adapter, pi_adapter, cursor_cli_adapter):
        registry.register(factory())
    return registry


def builtin_agent_adapter_kinds() -> tuple[str, ...]:
    """Return the built-in inventory from the registry bootstrap itself."""

    return tuple(
        adapter.contract.kind for adapter in builtin_agent_adapter_registry().adapters()
    )


def adapter_kind_from_runtime(value: str) -> str:
    """Normalize a persisted runtime envelope without interpreting its session."""

    kind = value.removesuffix("-thread")
    builtin_agent_adapter_registry().adapter(kind)
    return kind
