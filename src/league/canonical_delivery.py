"""Exact post-cutover delivery through a watcher or verified Herdr fallback."""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from .request_services import (
    DeliveryAdapter,
    DeliveryReceipt,
    DeliveryService,
    DeliveryUnavailable,
)
from .persistent_supervisor import SupervisorUnavailable, send_supervisor_message


class _Clock:
    def __init__(self, at: str) -> None:
        self.value = at

    def now(self) -> str:
        return self.value

    def after(self, seconds: int) -> str:
        value = datetime.fromisoformat(self.value.replace("Z", "+00:00"))
        return (value + timedelta(seconds=seconds)).isoformat()


class _Ids:
    def new(self, kind: str) -> str:
        return f"{kind}:canonical:{uuid.uuid4()}"


class InstalledDeliveryAdapter:
    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.runner = runner

    def send(
        self, channel: str, target: dict[str, Any], envelope: dict[str, Any]
    ) -> DeliveryReceipt:
        effect = hashlib.sha256(
            (
                f"{channel}\0{target['runtime_instance_id']}\0{target['generation']}\0"
                f"{envelope['event_id']}"
            ).encode()
        ).hexdigest()
        if channel == "direct":
            routing_target = target.get("routing_name") or target.get("locator")
            if target.get("backend_kind") != "herdr" or not routing_target:
                raise DeliveryUnavailable("receiver_unavailable")
            command = ["herdr"]
            if os.environ.get("HERDR_SESSION"):
                command.extend(("--session", os.environ["HERDR_SESSION"]))
            summary = " ".join(str(envelope.get("summary", "")).split())
            command.extend(
                (
                    "agent",
                    "prompt",
                    str(routing_target),
                    (
                        f"CHAMPION TRANSITION [{envelope['event_id']}] "
                        f"{envelope.get('status')}: {summary}"
                    ),
                )
            )
            try:
                completed = self.runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise DeliveryUnavailable("receiver_unavailable") from exc
            if completed.returncode != 0:
                raise DeliveryUnavailable("receiver_unavailable")
        elif channel == "watcher":
            locator = str(target.get("locator", ""))
            if locator.startswith("unix:"):
                try:
                    send_supervisor_message(
                        locator,
                        {
                            "kind": "champion-event",
                            "fence": target["fence"],
                            "runtime_generation": target["generation"],
                            "envelope": envelope,
                        },
                        timeout_seconds=15,
                    )
                except SupervisorUnavailable as exc:
                    raise DeliveryUnavailable("receiver_unavailable") from exc
            elif not locator.startswith("sqlite-supervise:"):
                raise DeliveryUnavailable("receiver_unavailable")
        return DeliveryReceipt(
            outbox_id=str(envelope["outbox_id"]),
            event_id=str(envelope["event_id"]),
            recipient_agent_id=str(envelope["recipient_agent_id"]),
            effect_kind="watcher_event" if channel == "watcher" else "direct_prompt",
            effect_id=effect,
        )


def dispatch_event(
    store: Any,
    *,
    outbox_id: str,
    event_id: str,
    recipient_agent_id: str,
    at: str,
    adapter: DeliveryAdapter | None = None,
) -> dict[str, Any]:
    policy = store.apply_supervision_delivery_policy(
        outbox_id, event_id, recipient_agent_id, at
    )
    if policy["action"] == "silent":
        return {
            "outbox_id": outbox_id,
            "event_id": event_id,
            "recipient_agent_id": recipient_agent_id,
            "state": "suppressed",
            "effect_kind": "calm_silent",
            "reason": policy["reason"],
            "idempotent": policy["idempotent"],
        }
    service = DeliveryService(
        store,
        adapter or InstalledDeliveryAdapter(),
        _Clock(at),
        _Ids(),
        dispatcher_id="dispatcher:installed-agent-transition",
    )
    try:
        return service.dispatch_source(outbox_id, event_id, recipient_agent_id)
    except DeliveryUnavailable:
        return {
            "outbox_id": outbox_id,
            "event_id": event_id,
            "recipient_agent_id": recipient_agent_id,
            "state": "pending",
            "reason": "receiver_unavailable",
        }
