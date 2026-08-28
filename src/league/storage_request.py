"""Prompt and request portion of the stable storage contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol


MAX_TRIAGE_JSON_BYTES = 65_536
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
    pre_bounded: bool = False
    read_only: bool = False
    answer_or_routing_only: bool = False
    expected_minutes: int = 0
    expected_task_action_calls: int = 0
    creates_artifact: bool = False
    mutates_state: bool = False
    reproduces_issue: bool = False
    runs_tests: bool = False
    runs_benchmark: bool = False
    uses_browser_or_computer: bool = False
    project_implementation: bool = False
    continuation_role: Optional[str] = None
    continuation_target: Optional[str] = None
    project_suggested_shotcaller: Optional[str] = None
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
    ) -> dict[str, Any]: ...
    def triage_prompt(self, prompt_id: str, items: list[dict[str, Any]], at: str) -> dict[str, Any]: ...

    def claim_request(
        self,
        request_id: str,
        runtime_instance_id: str,
        claim_token: str,
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
