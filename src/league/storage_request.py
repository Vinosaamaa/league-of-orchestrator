"""Prompt and request portion of the stable storage contract."""

from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol


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

    def dispatch_request(
        self,
        request_id: str,
        claim_token: str,
        dispatch_id: str,
        work_kind: str,
        requested_mode: Optional[str],
        hidden_supported: bool,
        requested_model: Optional[str],
        requested_effort: Optional[str],
        explicit_route: Optional[str],
        at: str,
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

    def record_request_result(
        self,
        request_id: str,
        claim_token: str,
        expected_version: int,
        result_id: str,
        idempotency_key: str,
        outcome: str,
        summary: str,
        task_ids: Iterable[str],
        at: str,
        *,
        return_to_requester: bool,
        event_id: Optional[str] = None,
        outbox_id: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def answer_request(
        self,
        request_id: str,
        claim_token: str,
        expected_version: int,
        response_ref_id: str,
        adapter_kind: str,
        session_locator: str,
        response_locator: str,
        durability: str,
        content_hash: str,
        resolution_summary: str,
        event_id: str,
        at: str,
    ) -> dict[str, Any]: ...

    def unresolved_requests(
        self,
        owner_agent_id: str,
        *,
        limit: int = 100,
        before_action: Optional[str] = None,
    ) -> dict[str, Any]: ...
