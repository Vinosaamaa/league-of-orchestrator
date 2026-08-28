"""Deterministic clocks, IDs, and adapters for request-lifecycle tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from league.request_services import (
    AssignmentSpec,
    DeliveryReceipt,
    DeliveryUnavailable,
    LaunchAdapterError,
)


class FakeClock:
    def __init__(self, value: str = "2026-01-01T01:00:00Z") -> None:
        self.value = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    def now(self) -> str:
        return self.value.isoformat(timespec="seconds").replace("+00:00", "Z")

    def after(self, seconds: int) -> str:
        return (self.value + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )

    def advance(self, seconds: int) -> str:
        self.value += timedelta(seconds=seconds)
        return self.now()


class FakeIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def new(self, kind: str) -> str:
        value = self.counts.get(kind, 0) + 1
        self.counts[kind] = value
        return f"{kind}-fake-{value}"


class FakeLaunchAdapter:
    def __init__(self, *, failure: Optional[LaunchAdapterError] = None) -> None:
        self.failure = failure
        self.calls: list[AssignmentSpec] = []

    def launch(self, spec: AssignmentSpec) -> dict[str, Any]:
        self.calls.append(spec)
        if self.failure is not None:
            raise self.failure
        return {
            "verified": True,
            "assignment_id": spec.assignment_id,
            "task_id": spec.task_id,
            "champion_agent_id": spec.champion_agent_id,
            "callsign": spec.callsign,
            "runtime_instance_id": f"runtime:{spec.champion_agent_id}",
            "thread_id": f"thread:{spec.champion_agent_id}",
            "endpoint": f"synthetic:{spec.callsign.lower()}",
            "runtime_generation": f"generation:{spec.champion_agent_id}",
            "harness_kind": "codex-thread",
            "backend_kind": "herdr",
            "routing_name": spec.callsign.lower(),
            "display_agent": "codex",
            "repository": spec.repository,
            "issue": spec.issue,
            "branch": spec.branch,
            "worktree": spec.worktree,
        }


@dataclass
class SentEnvelope:
    channel: str
    target: dict[str, Any]
    envelope: dict[str, Any]


class FakeDeliveryAdapter:
    def __init__(
        self,
        *,
        unavailable: bool = False,
        mismatch_event_id: Optional[str] = None,
    ) -> None:
        self.unavailable = unavailable
        self.mismatch_event_id = mismatch_event_id
        self.sent: list[SentEnvelope] = []

    def send(
        self,
        channel: str,
        target: dict[str, Any],
        envelope: dict[str, Any],
    ) -> DeliveryReceipt:
        self.sent.append(SentEnvelope(channel, dict(target), dict(envelope)))
        if self.unavailable:
            raise DeliveryUnavailable("synthetic closed endpoint")
        return DeliveryReceipt(
            outbox_id=envelope["outbox_id"],
            event_id=self.mismatch_event_id or envelope["event_id"],
            recipient_agent_id=envelope["recipient_agent_id"],
            effect_kind="inbox_event",
            effect_id=f"effect:{envelope['event_id']}",
        )
