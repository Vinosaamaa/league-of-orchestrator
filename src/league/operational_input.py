"""Typed League input that wakes an agent without becoming owner prompt intake."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping


OPERATIONAL_INPUT_PREFIX = "LEAGUE_OP: "
OPERATIONAL_INPUT_SCHEMA = "league.operational-input.v1"
OPERATIONAL_INPUT_KINDS = frozenset(
    {"delivery", "owner-control", "routed-delivery"}
)
_HEADER_KEYS = frozenset(
    {
        "content_sha256",
        "event_id",
        "kind",
        "outbox_id",
        "recipient_agent_id",
        "schema",
    }
)
MAX_OPERATIONAL_INPUT_BYTES = 64 * 1024


def render_operational_input(
    kind: str, envelope: Mapping[str, Any], content: str
) -> str:
    """Wrap one canonical outbox delivery in a strict provider-neutral header."""

    identities = {
        name: envelope.get(name)
        for name in ("outbox_id", "event_id", "recipient_agent_id")
    }
    if (
        kind not in OPERATIONAL_INPUT_KINDS
        or not isinstance(content, str)
        or not content
        or any(not isinstance(value, str) or not value for value in identities.values())
    ):
        raise ValueError("operational input identity or content is invalid")
    header = {
        "schema": OPERATIONAL_INPUT_SCHEMA,
        "kind": kind,
        **identities,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    rendered = (
        OPERATIONAL_INPUT_PREFIX
        + json.dumps(header, sort_keys=True, separators=(",", ":"))
        + "\n"
        + content
    )
    if len(rendered.encode("utf-8")) > MAX_OPERATIONAL_INPUT_BYTES:
        raise ValueError("operational input exceeds its byte bound")
    return rendered


def parse_operational_input(body: str) -> dict[str, str] | None:
    """Return a strict typed header only when its visible content is intact."""

    if (
        not isinstance(body, str)
        or not body.startswith(OPERATIONAL_INPUT_PREFIX)
        or len(body.encode("utf-8")) > MAX_OPERATIONAL_INPUT_BYTES
    ):
        return None
    first_line, separator, content = body.partition("\n")
    if not separator or not content:
        return None
    try:
        header = json.loads(first_line.removeprefix(OPERATIONAL_INPUT_PREFIX))
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(header, dict)
        or frozenset(header) != _HEADER_KEYS
        or header.get("schema") != OPERATIONAL_INPUT_SCHEMA
        or header.get("kind") not in OPERATIONAL_INPUT_KINDS
        or any(
            not isinstance(header.get(name), str) or not header[name]
            for name in _HEADER_KEYS - {"schema"}
        )
    ):
        return None
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(str(header["content_sha256"]), digest):
        return None
    return {str(key): str(value) for key, value in header.items()}


def transition_content(envelope: Mapping[str, Any]) -> str:
    """Render the human/model-facing content of one material transition."""

    summary = " ".join(str(envelope.get("summary", "")).split())
    return (
        f"CHAMPION TRANSITION [{envelope['event_id']}] "
        f"{envelope.get('status')}: {summary}"
    )


def owner_control_content(envelope: Mapping[str, Any]) -> str:
    """Render the exact delegated pause instruction for a Champion."""

    return (
        f"LEAGUE OWNER CONTROL [{envelope['event_id']}] Pause delegated work now, "
        "preserve durable progress, and await a new explicit owner instruction."
    )


__all__ = [
    "OPERATIONAL_INPUT_PREFIX",
    "owner_control_content",
    "parse_operational_input",
    "render_operational_input",
    "transition_content",
]
