"""Agent, callsign, project, and ownership portion of the storage contract."""

from __future__ import annotations

from typing import Any, Optional, Protocol

from .storage_types import FaultInjector


class LifecycleStorage(Protocol):
    def agent_status(self, agent_id: str) -> Optional[dict[str, Any]]: ...

    def transition(
        self,
        agent_id: str,
        expected_version: int,
        status: str,
        update: str,
        at: str,
        *,
        fault: Optional[FaultInjector] = None,
    ) -> dict[str, Any]: ...

    def transfer_task_owner(
        self,
        task_id: str,
        expected_version: int,
        owner_kind: str,
        owner_id: str,
        at: str,
    ) -> dict[str, Any]: ...
