"""Exact post-cutover delivery through a watcher or verified Herdr fallback."""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from .request_services import (
    DeliveryAdapter,
    DeliveryAmbiguous,
    DeliveryReceipt,
    DeliveryService,
    DeliveryUnavailable,
)
from .agent_adapters import adapter_kind_from_runtime, builtin_agent_adapter_registry
from .multiplexer_adapters import builtin_multiplexer_adapter_registry
from .persistent_supervisor import (
    CallableMultiplexerRunner,
    SupervisorUnavailable,
    send_supervisor_message,
)


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
        *,
        store: Any | None = None,
        at: str | None = None,
    ) -> None:
        self.runner = runner
        self.store = store
        self.at = at

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
            try:
                agent_kind = adapter_kind_from_runtime(str(target.get("harness_kind", "")))
                agent = builtin_agent_adapter_registry().adapter(agent_kind)
            except DeliveryUnavailable:
                raise
            except Exception as exc:
                raise DeliveryUnavailable("receiver_unavailable") from exc
            owner_control = envelope.get("event_type") == "owner_stop_control"
            if "delivery" not in agent.lifecycle_operations or (
                owner_control and "steer" not in agent.lifecycle_operations
            ):
                raise DeliveryUnavailable("receiver_unavailable")
            if owner_control and (
                target.get("runtime_instance_id")
                != envelope.get("target_runtime_instance_id")
                or target.get("generation")
                != envelope.get("target_runtime_generation")
            ):
                raise DeliveryUnavailable("owner_stop_target_changed")
            try:
                multiplexer = builtin_multiplexer_adapter_registry(
                    herdr_runner=CallableMultiplexerRunner(self.runner),
                    herdr_binary="herdr",
                ).adapter(str(target.get("backend_kind", "")))
                operation = agent.control_delegated if owner_control else agent.deliver
                delivered = operation(
                    target=target,
                    envelope=envelope,
                    multiplexer=multiplexer,
                    store=self.store,
                    at=self.at,
                    runner=self.runner,
                )
            except (DeliveryAmbiguous, DeliveryUnavailable):
                raise
            except Exception as exc:
                raise DeliveryAmbiguous("receiver_outcome_ambiguous") from exc
            if isinstance(delivered, DeliveryReceipt):
                return delivered
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
    if policy["action"] == "defer":
        return {
            "outbox_id": outbox_id,
            "event_id": event_id,
            "recipient_agent_id": recipient_agent_id,
            "state": policy["state"],
            "reason": policy["reason"],
            "idempotent": policy["idempotent"],
        }
    service = DeliveryService(
        store,
        adapter or InstalledDeliveryAdapter(store=store, at=at),
        _Clock(at),
        _Ids(),
        dispatcher_id="dispatcher:installed-agent-transition",
    )
    try:
        return service.dispatch_source(outbox_id, event_id, recipient_agent_id)
    except DeliveryAmbiguous as exc:
        return {
            "outbox_id": outbox_id,
            "event_id": event_id,
            "recipient_agent_id": recipient_agent_id,
            "state": "awaiting_receipt",
            "reason": str(exc) or "receiver_outcome_ambiguous",
        }
    except DeliveryUnavailable as exc:
        return {
            "outbox_id": outbox_id,
            "event_id": event_id,
            "recipient_agent_id": recipient_agent_id,
            "state": "pending",
            "reason": str(exc) or "receiver_unavailable",
        }
