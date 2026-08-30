"""Provider-owned lifecycle details for visible Herdr agents.

League persists the returned session value as an opaque identity.  Only this
module interprets provider command lines and validates provider session values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

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
        if self.kind == "codex":
            return (
                "--model",
                model,
                "--config",
                f'model_reasoning_effort="{effort}"',
                "--add-dir",
                str(state_root),
            )
        if self.kind == "cursor":
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
        integration = release_root / "integrations" / "pi" / "league-runtime.ts"
        profile = release_root / "integrations" / "pi" / "league-bash.sb"
        watcher = release_root / "bin" / "agent-watcher"
        if not integration.is_file() or integration.is_symlink():
            raise StorageRefusal(
                "launch_integration_unavailable",
                "Pi lifecycle integration is missing from the exact League release",
            )
        if not profile.is_file() or profile.is_symlink():
            raise StorageRefusal(
                "launch_integration_unavailable",
                "Pi shell sandbox profile is missing from the exact League release",
            )
        if not watcher.is_file() or watcher.is_symlink():
            raise StorageRefusal(
                "launch_integration_unavailable",
                "Pi canonical watcher is missing from the exact League release",
            )
        arguments = [
            "--model",
            model,
            "--thinking",
            effort,
            "--extension",
            str(integration),
            "--session-dir",
            str(state_root / "provider-sessions" / "pi"),
        ]
        if resume_session is not None:
            arguments.extend(("--session", resume_session))
        return tuple(arguments)


PROVIDERS = {
    "codex": ProviderLifecycle("codex", "codex-thread", "codex", "/exit", False),
    "cursor": ProviderLifecycle("cursor", "cursor-thread", "cursor", "/exit", True),
    "pi": ProviderLifecycle("pi", "pi-thread", "pi", "/quit", True),
}


def provider_lifecycle(kind: str) -> ProviderLifecycle:
    try:
        return PROVIDERS[kind]
    except KeyError as exc:
        raise StorageRefusal(
            "launch_harness_unsupported", f"visible harness is unsupported: {kind}"
        ) from exc


def supported_runtime_kind(kind: str) -> bool:
    return any(profile.runtime_kind == kind for profile in PROVIDERS.values())


def profile_for_runtime_kind(kind: str) -> ProviderLifecycle:
    for profile in PROVIDERS.values():
        if profile.runtime_kind == kind or profile.kind == kind:
            return profile
    raise StorageRefusal(
        "cleanup_adapter_unsupported", f"runtime harness is unsupported: {kind}"
    )
