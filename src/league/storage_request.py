"""Prompt and request portion of the stable storage contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .orchestration import OrchestrationSignals


MAX_TRIAGE_JSON_BYTES = 65_536
MAX_TRIAGE_TURN_BYTES = 1_000_000
MAX_TRIAGE_TURN_PROMPTS = 25
MAX_TASK_RESULT_SOURCES = 128


@dataclass(frozen=True)
class DispatchRequestCommand:
    request_id: str
    claim_token: str
    dispatch_id: str
    work_kind: str
    requested_mode: Optional[str]
    hidden_supported: bool
    requested_model: Optional[str]
    requested_effort: Optional[str]
    explicit_route: Optional[str]
    at: str
    orchestration: OrchestrationSignals = OrchestrationSignals(False, False, False, 0, 0)
    continuation_role: Optional[str] = None
    continuation_target: Optional[str] = None
    hidden_subtask: Optional[str] = None
    hidden_scope_budget: Optional[str] = None


@dataclass(frozen=True)
class RequestProgressCommand:
    progress_id: str
    request_id: str
    claim_token: str
    expected_version: int
    progress_generation: int
    reason_code: str
    settled_count: int
    total_count: int
    current_phase: str
    blocker_count: int
    blocker_severity: str
    user_action_required: bool
    deadline_change: Optional[str]
    next_action: str
    event_id: str
    outbox_id: str
    at: str
    minimum_interval_seconds: int = 900
    grace_seconds: int = 300
    promised_checkpoint_at: Optional[str] = None


@dataclass(frozen=True)
class RequestResultCommand:
    request_id: str
    claim_token: str
    expected_version: int
    result_id: str
    idempotency_key: str
    outcome: str
    summary: str
    task_ids: tuple[str, ...]
    at: str
    return_to_requester: bool
    event_id: Optional[str]
    outbox_id: Optional[str]


@dataclass(frozen=True)
class AnswerRequestCommand:
    request_id: str
    claim_token: str
    expected_version: int
    response_ref_id: str
    adapter_kind: str
    session_locator: str
    response_locator: str
    durability: str
    content_hash: str
    resolution_summary: str
    event_id: str
    at: str


@dataclass(frozen=True)
class TurnDispatchPlan:
    runtime_instance_id: str
    claim_token: str
    leased_until: str
    command: DispatchRequestCommand


@dataclass(frozen=True)
class ReconcileDuplicateRequestCommand:
    duplicate_request_id: str
    canonical_request_id: str
    owner_agent_id: str
    expected_duplicate_version: int
    expected_canonical_version: int
    at: str


class RequestStorage(Protocol):
    def intake_prompt(
        self,
        prompt_id: str,
        intake_actor_id: str,
        runtime_instance_id: str,
        adapter_kind: str,
        session_ref: str,
        source_event_key: str,
        body: str,
        at: str,
        *,
        wake_scope_id: Optional[str] = None,
        wake: bool = True,
    ) -> dict[str, Any]: ...
    def quarantine_prompt(
        self, prompt_id: str, adapter_kind: str, session_ref: str,
        source_event_key: str, body: str, at: str, *,
        wake_actor_id: Optional[str] = None, wake_scope_id: Optional[str] = None,
    ) -> dict[str, Any]: ...
    def bind_quarantined_prompt(
        self, prompt_id: str, intake_actor_id: str, runtime_instance_id: str,
        at: str, *, wake_scope_id: Optional[str] = None, wake: bool = True,
    ) -> dict[str, Any]: ...
    def triage_prompt(self, prompt_id: str, items: list[dict[str, Any]], at: str) -> dict[str, Any]: ...

    def triage_prompt_batch(
        self,
        owner_agent_id: str,
        expected_prompt_ids: tuple[str, ...],
        decisions: list[dict[str, Any]],
        at: str,
    ) -> dict[str, Any]: ...

    def begin_request_turn(
        self,
        owner_agent_id: str,
        expected_prompt_ids: tuple[str, ...],
        decisions: list[dict[str, Any]],
        plans: tuple[TurnDispatchPlan, ...],
        at: str,
        *,
        expected_candidate_digest: Optional[str] = None,
        candidate_limit: int = 12,
        candidate_max_bytes: int = 24_576,
    ) -> dict[str, Any]: ...

    def commit_request_turn(
        self,
        owner_agent_id: str,
        actions: tuple[AnswerRequestCommand | RequestResultCommand, ...],
        at: str,
    ) -> dict[str, Any]: ...

    def commit_interactive_request_turn(
        self,
        owner_agent_id: str,
        turn_token: str,
        actions: tuple[AnswerRequestCommand | RequestResultCommand, ...],
        at: str,
    ) -> dict[str, Any]: ...

    def request_turn_boundary(self, owner_agent_id: str) -> dict[str, Any]: ...

    def reconcile_duplicate_request(
        self, command: ReconcileDuplicateRequestCommand
    ) -> dict[str, Any]: ...

    def claim_request(
        self,
        request_id: str,
        runtime_instance_id: str,
        claim_token: str,
        leased_until: str,
        at: str,
    ) -> dict[str, Any]: ...

    def accept_routed_delivery(
        self,
        event_id: str,
        recipient_agent_id: str,
        runtime_instance_id: str,
        leased_until: str,
        at: str,
    ) -> dict[str, Any]: ...

    def release_request_claim(
        self, request_id: str, runtime_instance_id: str, claim_token: str, at: str
    ) -> dict[str, Any]: ...

    def dispatch_request(self, command: DispatchRequestCommand) -> dict[str, Any]: ...

    def emit_request_progress(self, command: RequestProgressCommand) -> dict[str, Any]: ...

    def reconcile_request_progress(
        self, owner_agent_id: str, at: str
    ) -> dict[str, Any]: ...

    def route_request(
        self,
        request_id: str,
        claim_token: str,
        expected_version: int,
        recipient_agent_id: str,
        event_id: str,
        outbox_id: str,
        at: str,
        *,
        recipient_squad_id: Optional[str] = None,
        route_reason_code: str = "explicit_squad",
        route_policy_version: str = "league.orchestration.v1",
        route_confidence: str = "explicit",
        required_capabilities: tuple[str, ...] = (),
    ) -> dict[str, Any]: ...

    def set_request_state(
        self,
        request_id: str,
        claim_token: str,
        expected_version: int,
        state: str,
        summary: str,
        event_id: str,
        at: str,
        *,
        next_attention_at: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def record_request_result(self, command: RequestResultCommand) -> dict[str, Any]: ...

    def answer_request(self, command: AnswerRequestCommand) -> dict[str, Any]: ...

    def unresolved_requests(
        self,
        owner_agent_id: str,
        *,
        limit: int = 100,
        before_action: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def untriaged_intake(
        self,
        owner_agent_id: str,
        *,
        limit: int = 20,
        max_bytes: int = 1_000_000,
        candidate_limit: int = 12,
        candidate_max_bytes: int = 24_576,
        candidate_after: Optional[str] = None,
        candidate_page: bool = False,
    ) -> dict[str, Any]: ...

    def semantic_recovery_backlog(self, *, limit: int = 20) -> dict[str, Any]: ...
