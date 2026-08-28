"""League remote adapters behind one mandatory final-payload privacy gate."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .privacy import validate_final_rendered_payload
from .storage_types import StorageRefusal


REMOTE_ADAPTER_KINDS = frozenset(
    {
        "github_issue",
        "github_pull_request",
        "github_comment",
        "lavish_share",
        "deployment_note",
        "report_export",
        "future_remote",
    }
)
_ADAPTER_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_REMOTE_RECEIPT_ID = 512


class RemoteTransport(Protocol):
    def send(self, payload: bytes) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class RenderedPayload:
    body: bytes
    mode: str = "outbound"
    structured_fields: Any = None
    approved_urls: tuple[str, ...] = ()


class GuardedRemoteAdapter:
    """The only League wrapper permitted to invoke a remote transport."""

    def __init__(
        self,
        adapter_kind: str,
        destination_visibility: str,
        transport: RemoteTransport,
    ) -> None:
        if adapter_kind not in REMOTE_ADAPTER_KINDS and not _ADAPTER_KIND.fullmatch(
            adapter_kind
        ):
            raise StorageRefusal("invalid_remote_adapter", "remote adapter kind is invalid")
        self.adapter_kind = adapter_kind
        self.destination_visibility = destination_visibility
        self.transport = transport

    def send(self, payload: RenderedPayload) -> dict[str, Any]:
        validation = validate_final_rendered_payload(
            payload.body,
            destination_visibility=self.destination_visibility,
            mode=payload.mode,
            structured_fields=payload.structured_fields,
            approved_urls=payload.approved_urls,
        )
        transport_receipt = self.transport.send(payload.body)
        receipt_id = transport_receipt.get("receipt_id")
        if (
            not isinstance(receipt_id, str)
            or not receipt_id
            or len(receipt_id) > MAX_REMOTE_RECEIPT_ID
            or "\x00" in receipt_id
        ):
            raise StorageRefusal(
                "remote_receipt_invalid", "remote transport returned no bounded receipt identity"
            )
        return {
            "schema": "league.outbound-receipt.v1",
            "adapter": self.adapter_kind,
            "destination_visibility": self.destination_visibility,
            "payload_sha256": validation.payload_sha256,
            "bytes": validation.byte_count,
            "transport_receipt_sha256": hashlib.sha256(
                receipt_id.encode("utf-8")
            ).hexdigest(),
            "redacted": True,
        }


def remote_adapter(
    adapter_kind: str,
    destination_visibility: str,
    transport: RemoteTransport,
) -> GuardedRemoteAdapter:
    """Construct a current or future adapter with the same fail-closed guard."""

    return GuardedRemoteAdapter(adapter_kind, destination_visibility, transport)


__all__ = [
    "GuardedRemoteAdapter",
    "MAX_REMOTE_RECEIPT_ID",
    "REMOTE_ADAPTER_KINDS",
    "RenderedPayload",
    "RemoteTransport",
    "remote_adapter",
]
