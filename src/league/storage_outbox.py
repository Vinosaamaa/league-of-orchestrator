"""Event outbox portion of the stable storage contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class OutboxDispatchIdentity:
    outbox_id: str
    event_id: str
    recipient_agent_id: str
    dispatcher_id: str
    attempt_id: str


class OutboxStorage(Protocol):
    def claim_outbox(
        self, identity: OutboxDispatchIdentity, lease_expires_at: str, at: str
    ) -> dict[str, Any]: ...
    def acknowledge_outbox(
        self,
        identity: OutboxDispatchIdentity,
        fence: int,
        adapter_kind: str,
        effect_kind: str,
        effect_id: str,
        at: str,
    ) -> dict[str, Any]: ...

    def fail_outbox(
        self,
        identity: OutboxDispatchIdentity,
        fence: int,
        adapter_kind: str,
        reason: str,
        retry_at: str,
        at: str,
    ) -> dict[str, Any]: ...

    def pending_backlog(
        self,
        at: str,
        *,
        limit: int = 100,
        per_recipient: int = 2,
        exclude_outbox_id: Optional[str] = None,
    ) -> list[dict[str, Any]]: ...

    def delivery_target(self, recipient_agent_id: str, at: str) -> Optional[dict[str, Any]]: ...

    def outbox_envelope(
        self, outbox_id: str, event_id: str, recipient_agent_id: str
    ) -> dict[str, Any]: ...
