"""Small adapter-facing services around the canonical request lifecycle store."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Protocol

from .storage import (
    DispatchRequestCommand,
    OutboxDispatchIdentity,
    PrepareAssignmentCommand,
    Storage,
    StorageRefusal,
)


class Clock(Protocol):
    def now(self) -> str: ...

    def after(self, seconds: int) -> str: ...


class IdFactory(Protocol):
    def new(self, kind: str) -> str: ...


@dataclass(frozen=True)
class AssignmentSpec:
    assignment_id: str
    request_id: str
    claim_token: str
    task_id: str
    task_summary: str
    coordinator_agent_id: str
    champion_agent_id: str
    repository: str
    issue: int
    branch: str
    worktree: str
    issue_receipt: Optional[dict[str, Any]]
    required_capabilities: tuple[str, ...] = ()
    callsign: Optional[str] = None


class LaunchAdapterError(RuntimeError):
    def __init__(
        self,
        failure_class: str,
        *,
        cleanup_required: bool = False,
        cleanup_proven: bool = False,
    ) -> None:
        super().__init__(failure_class)
        self.failure_class = failure_class
        self.cleanup_required = cleanup_required
        self.cleanup_proven = cleanup_proven


class LaunchAdapter(Protocol):
    def launch(self, spec: AssignmentSpec) -> dict[str, Any]: ...


class AssignmentService:
    """Bridge one recoverable DB assignment around one visible launch adapter."""

    def __init__(
        self,
        store: Storage,
        adapter: LaunchAdapter,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.clock = clock
        self.ids = ids

    def assign(self, spec: AssignmentSpec) -> dict[str, Any]:
        if spec.issue_receipt is None:
            raise StorageRefusal(
                "issue_verification_required",
                "visible Champion assignment requires exact owner-API issue evidence",
            )
        prepared = self.store.prepare_assignment(
            PrepareAssignmentCommand(
                assignment_id=spec.assignment_id,
                request_id=spec.request_id,
                claim_token=spec.claim_token,
                task_id=spec.task_id,
                task_summary=spec.task_summary,
                coordinator_agent_id=spec.coordinator_agent_id,
                champion_agent_id=spec.champion_agent_id,
                repository=spec.repository,
                issue=spec.issue,
                branch=spec.branch,
                worktree=spec.worktree,
                at=self.clock.now(),
                required_capabilities=spec.required_capabilities,
                issue_receipt=spec.issue_receipt,
            )
        )
        if prepared["state"] == "active":
            return prepared
        if prepared["state"] == "pending":
            launching = self.store.mark_assignment_launching(
                spec.assignment_id, prepared["version"], self.clock.now()
            )
        elif prepared["state"] == "launching":
            launching = prepared
        else:
            raise StorageRefusal(
                "assignment_conflict", "assignment cannot launch from its current recoverable state"
            )
        try:
            receipt = self.adapter.launch(replace(spec, callsign=prepared["callsign"]))
        except LaunchAdapterError as exc:
            return self.store.block_assignment(
                spec.assignment_id,
                launching["version"],
                exc.failure_class,
                exc.cleanup_required,
                exc.cleanup_proven,
                self.clock.now(),
            )
        except Exception as exc:
            return self.store.block_assignment(
                spec.assignment_id,
                launching["version"],
                f"launch_adapter_{type(exc).__name__.lower()}",
                True,
                False,
                self.clock.now(),
            )
        try:
            return self.store.activate_assignment(
                spec.assignment_id,
                launching["version"],
                receipt,
                self.ids.new("event"),
                self.ids.new("outbox"),
                self.clock.now(),
            )
        except StorageRefusal as exc:
            if exc.code not in {"receipt_unverified", "receipt_mismatch", "runtime_conflict"}:
                raise
            return self.store.block_assignment(
                spec.assignment_id,
                launching["version"],
                f"launch_{exc.code}",
                True,
                False,
                self.clock.now(),
            )


@dataclass(frozen=True)
class DeliveryReceipt:
    outbox_id: str
    event_id: str
    recipient_agent_id: str
    effect_kind: str
    effect_id: str


class DeliveryAdapter(Protocol):
    def send(
        self,
        channel: str,
        target: dict[str, Any],
        envelope: dict[str, Any],
    ) -> DeliveryReceipt: ...


class DeliveryUnavailable(RuntimeError):
    pass


class DeliveryService:
    """Dispatch exact source events, then fairly drain unrelated pending rows."""

    def __init__(
        self,
        store: Storage,
        adapter: DeliveryAdapter,
        clock: Clock,
        ids: IdFactory,
        *,
        dispatcher_id: str,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.clock = clock
        self.ids = ids
        self.dispatcher_id = dispatcher_id

    def dispatch_source(
        self, outbox_id: str, event_id: str, recipient_agent_id: str
    ) -> dict[str, Any]:
        at = self.clock.now()
        attempt_id = self.ids.new("attempt")
        identity = OutboxDispatchIdentity(
            outbox_id=outbox_id,
            event_id=event_id,
            recipient_agent_id=recipient_agent_id,
            dispatcher_id=self.dispatcher_id,
            attempt_id=attempt_id,
        )
        claim = self.store.claim_outbox(
            identity,
            self.clock.after(30),
            at,
        )
        if claim["state"] == "delivered":
            return claim
        target = self.store.delivery_target(recipient_agent_id, at)
        if target is None:
            self.store.fail_outbox(
                identity,
                claim["fence"],
                "none",
                "receiver_unavailable",
                self.clock.after(30),
                self.clock.now(),
            )
            raise DeliveryUnavailable("receiver_unavailable")
        envelope = self.store.outbox_envelope(outbox_id, event_id, recipient_agent_id)
        try:
            receipt = self.adapter.send(target["channel"], target, envelope)
        except DeliveryUnavailable:
            self.store.fail_outbox(
                identity,
                claim["fence"],
                target["channel"],
                "receiver_unavailable",
                self.clock.after(30),
                self.clock.now(),
            )
            raise
        exact = (
            receipt.outbox_id == outbox_id
            and receipt.event_id == event_id
            and receipt.recipient_agent_id == recipient_agent_id
        )
        if not exact:
            self.store.fail_outbox(
                identity,
                claim["fence"],
                target["channel"],
                "recipient_receipt_mismatch",
                self.clock.after(30),
                self.clock.now(),
            )
            raise StorageRefusal(
                "receipt_mismatch", "delivery receipt did not name the exact source event and recipient"
            )
        return self.store.acknowledge_outbox(
            identity,
            claim["fence"],
            target["channel"],
            receipt.effect_kind,
            receipt.effect_id,
            self.clock.now(),
        )

    def _dispatch_backlog(
        self,
        *,
        limit: int,
        per_recipient: int,
        exclude_outbox_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for item in self.store.pending_backlog(
            self.clock.now(),
            limit=limit,
            per_recipient=per_recipient,
            exclude_outbox_id=exclude_outbox_id,
        ):
            try:
                outcomes.append(
                    self.dispatch_source(
                        item["outbox_id"], item["event_id"], item["recipient_agent_id"]
                    )
                )
            except (DeliveryUnavailable, StorageRefusal) as exc:
                outcomes.append(
                    {
                        "outbox_id": item["outbox_id"],
                        "event_id": item["event_id"],
                        "recipient_agent_id": item["recipient_agent_id"],
                        "state": "pending",
                        "error": getattr(exc, "code", str(exc)),
                    }
                )
        return outcomes

    def drain(self, *, limit: int = 20, per_recipient: int = 2) -> list[dict[str, Any]]:
        return self._dispatch_backlog(limit=limit, per_recipient=per_recipient)

    def dispatch_source_then_drain(
        self,
        outbox_id: str,
        event_id: str,
        recipient_agent_id: str,
        *,
        backlog_limit: int = 20,
    ) -> dict[str, Any]:
        source = self.dispatch_source(outbox_id, event_id, recipient_agent_id)
        backlog = self._dispatch_backlog(
            limit=backlog_limit,
            per_recipient=2,
            exclude_outbox_id=outbox_id,
        )
        return {"source": source, "backlog": backlog}


class DispatchService:
    """Place explicit dispatch in front of a substantive direct action."""

    def __init__(self, store: Storage, clock: Clock, ids: IdFactory) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids

    def run_direct(
        self,
        request_id: str,
        claim_token: str,
        work_kind: str,
        action: Callable[[], Any],
        *,
        requested_mode: Optional[str] = "direct",
    ) -> Any:
        decision = self.store.dispatch_request(
            DispatchRequestCommand(
                request_id=request_id,
                claim_token=claim_token,
                dispatch_id=self.ids.new("dispatch"),
                work_kind=work_kind,
                requested_mode=requested_mode,
                hidden_supported=False,
                requested_model=None,
                requested_effort=None,
                explicit_route=None,
                at=self.clock.now(),
            )
        )
        if decision["execution_mode"] != "direct":
            raise StorageRefusal("direct_refused", "substantive direct action requires direct dispatch")
        return action()
