"""Stable storage boundary for duplicate-preflight issue selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class BeginIssueSelectionCommand:
    selection_key: str
    task_id: str
    task_summary: str
    coordinator_agent_id: str
    repository: str
    repository_key: str
    normalized_title: str
    semantic_scope_digest: str
    owner_attempt_id: str
    lease_expires_at: str
    at: str


@dataclass(frozen=True)
class CompleteIssueSelectionCommand:
    selection_key: str
    expected_version: int
    owner_attempt_id: str
    task_id: str
    task_summary: str
    coordinator_agent_id: str
    repository: str
    repository_key: str
    normalized_title: str
    semantic_scope_digest: str
    decision: str
    issue: int
    issue_url: str
    issue_title: str
    issue_body_digest: str
    duplicate_matches: int
    reopen_action_receipt_digest: str | None
    at: str


class IssueStorage(Protocol):
    def begin_issue_selection(
        self, command: BeginIssueSelectionCommand
    ) -> dict[str, Any]: ...

    def complete_issue_selection(
        self, command: CompleteIssueSelectionCommand
    ) -> dict[str, Any]: ...

    def release_issue_selection(
        self,
        selection_key: str,
        owner_attempt_id: str,
        expected_version: int,
        at: str,
    ) -> dict[str, Any]: ...

    def verify_issue_reopen_authority(
        self,
        receipt_digest: str,
        coordinator_agent_id: str,
        repository: str,
        issue: int,
    ) -> dict[str, Any]: ...
