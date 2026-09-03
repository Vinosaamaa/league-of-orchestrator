"""Provider-neutral execution of durable semantic owner-stop controls."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import uuid

from .canonical_delivery import InstalledDeliveryAdapter
from .request_services import DeliveryAdapter, DeliveryService, DeliveryUnavailable
from .storage_types import StorageRefusal


class _Clock:
    def __init__(self, at: str) -> None:
        self.at = at

    def now(self) -> str:
        return self.at

    def after(self, seconds: int) -> str:
        value = datetime.fromisoformat(self.at.replace("Z", "+00:00"))
        return (value + timedelta(seconds=seconds)).isoformat()


class _Ids:
    def new(self, kind: str) -> str:
        return f"{kind}:owner-stop:{uuid.uuid4()}"


class _ExactTargetAdapter:
    """Fence delivery to the runtimes captured by the owner decision."""

    def __init__(self, adapter: DeliveryAdapter, targets: list[dict[str, Any]]) -> None:
        self.adapter = adapter
        self.targets = {
            str(target["recipient_agent_id"]): target for target in targets
        }

    def send(
        self, channel: str, target: dict[str, Any], envelope: dict[str, Any]
    ) -> Any:
        expected = self.targets.get(str(envelope.get("recipient_agent_id")))
        exact = bool(
            expected is not None
            and channel == "direct"
            and target.get("runtime_instance_id") == expected.get("runtime_instance_id")
            and target.get("generation") == expected.get("runtime_generation")
            and target.get("harness_kind") == expected.get("harness_kind")
            and target.get("backend_kind") == expected.get("backend_kind")
            and target.get("session_ref") == expected.get("session_ref")
            and target.get("locator") == expected.get("endpoint")
        )
        if not exact:
            raise DeliveryUnavailable("owner_stop_target_changed")
        return self.adapter.send(channel, target, envelope)


def execute_owner_stop_controls(
    store: Any,
    controls: tuple[dict[str, Any], ...],
    at: str,
    *,
    adapter: DeliveryAdapter | None = None,
) -> list[dict[str, Any]]:
    """Deliver exact pause controls and authorize Stop only after all receipts."""

    results: list[dict[str, Any]] = []
    for control in controls:
        actor_agent_id = control.get("actor_agent_id")
        control_id = control.get("control_id")
        targets = control.get("targets")
        if (
            not isinstance(actor_agent_id, str)
            or not actor_agent_id
            or not isinstance(control_id, str)
            or not control_id
            or not isinstance(targets, list)
            or any(not isinstance(target, dict) for target in targets)
        ):
            raise StorageRefusal(
                "owner_stop_invalid", "prepared semantic owner-stop control is malformed"
            )
        installed = adapter or InstalledDeliveryAdapter(store=store, at=at)
        exact_adapter = _ExactTargetAdapter(installed, targets)
        service = DeliveryService(
            store,
            exact_adapter,
            _Clock(at),
            _Ids(),
            dispatcher_id=f"dispatcher:owner-stop:{control_id}",
        )
        try:
            for target in targets:
                service.dispatch_source(
                    str(target["outbox_id"]),
                    str(target["event_id"]),
                    str(target["recipient_agent_id"]),
                )
            results.append(
                store.finalize_owner_stop_control(actor_agent_id, control_id, at)
            )
        except (DeliveryUnavailable, StorageRefusal) as exc:
            results.append(
                store.fail_owner_stop_control(
                    actor_agent_id,
                    control_id,
                    getattr(exc, "code", None) or str(exc) or "receiver_unavailable",
                    at,
                )
            )
    return results
