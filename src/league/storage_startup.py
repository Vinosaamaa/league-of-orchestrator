"""Bounded startup context portion of the stable storage contract."""

from __future__ import annotations

from typing import Any, Protocol


class StartupContextStorage(Protocol):
    def startup_context(
        self,
        agent_id: str,
        runtime_instance_id: str,
        at: str,
    ) -> dict[str, Any]: ...
