"""Provider-owned lifecycle details for visible Herdr agents.

League persists the returned session value as an opaque identity.  Only this
module interprets provider command lines and validates provider session values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .storage_types import StorageRefusal


THREAD_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
SAFE_OPAQUE_SESSION = re.compile(r"^[^\x00-\x1f\x7f]{1,1024}$")


@dataclass(frozen=True)
class ProviderLifecycle:
    kind: str
    runtime_kind: str
    display_kind: str
    exit_prompt: str
    supports_resume: bool
    argument_builder: Callable[..., tuple[str, ...]]

    def validate_session(self, value: str | None) -> bool:
        if not isinstance(value, str) or not SAFE_OPAQUE_SESSION.fullmatch(value):
            return False
        return THREAD_UUID.fullmatch(value) is not None

    def start_arguments(
        self,
        *,
        model: str,
        effort: str,
        state_root: Path,
        release_root: Path,
        resume_session: str | None,
        provider_kind: str | None = None,
    ) -> tuple[str, ...]:
        if resume_session is not None:
            if not self.supports_resume:
                raise StorageRefusal(
                    "launch_resume_unsupported",
                    f"{self.display_kind} does not declare exact-session resume",
                )
            if not self.validate_session(resume_session):
                raise StorageRefusal(
                    "launch_resume_identity_invalid",
                    f"{self.display_kind} resume identity is invalid",
                )
        return self.argument_builder(
            model=model,
            effort=effort,
            state_root=state_root,
            release_root=release_root,
            resume_session=resume_session,
            provider_kind=provider_kind,
        )


def provider_lifecycle(kind: str) -> ProviderLifecycle:
    from .agent_adapters import builtin_agent_adapter_registry

    adapter = builtin_agent_adapter_registry().adapter(kind)
    profile = getattr(adapter, "launch_profile", None)
    if not isinstance(profile, ProviderLifecycle):
        raise StorageRefusal(
            "launch_harness_unsupported", f"visible harness is unsupported: {kind}"
        )
    return profile


def supported_runtime_kind(kind: str) -> bool:
    try:
        provider_lifecycle(kind.removesuffix("-thread"))
    except StorageRefusal:
        return False
    return True


def profile_for_runtime_kind(kind: str) -> ProviderLifecycle:
    try:
        return provider_lifecycle(kind.removesuffix("-thread"))
    except StorageRefusal as exc:
        raise StorageRefusal(
            "cleanup_adapter_unsupported", f"runtime harness is unsupported: {kind}"
        ) from exc
