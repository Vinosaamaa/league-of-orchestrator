"""Delivery acknowledgement portion of the storage contract."""

from __future__ import annotations

from typing import Any, Protocol


class DeliveryStorage(Protocol):
    def claim_delivery(
        self,
        event_id: str,
        recipient_agent_id: str,
        claim_token: str,
        claim_expires_at: str,
        at: str,
    ) -> dict[str, Any]: ...

    def acknowledge_delivery(
        self, event_id: str, recipient_agent_id: str, claim_token: str, at: str
    ) -> dict[str, Any]: ...

    def fail_delivery(
        self,
        event_id: str,
        recipient_agent_id: str,
        claim_token: str,
        reason: str,
        at: str,
    ) -> dict[str, Any]: ...
