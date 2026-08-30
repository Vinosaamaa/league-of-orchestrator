"""Exact provider-thread continuation portion of the stable storage contract."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol


class ContinuationStorage(Protocol):
    def thread_archive(self, archive_id: str) -> Optional[dict[str, Any]]: ...
    def prepare_continuation(self, spec: Mapping[str, Any]) -> dict[str, Any]: ...
    def continuation_status(self, operation_id: str) -> Optional[dict[str, Any]]: ...
    def continuation_for_assignment(
        self, assignment_id: str
    ) -> Optional[dict[str, Any]]: ...
    def claim_issue_reopen(
        self,
        operation_id: str,
        expected_version: int,
        expected_fence: int,
        executor_id: str,
        leased_until: str,
        at: str,
    ) -> dict[str, Any]: ...
    def record_issue_reopen(
        self,
        operation_id: str,
        expected_version: int,
        fence: int,
        outcome: str,
        receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...
    def mark_continuation_launching(
        self, operation_id: str, expected_version: int, at: str
    ) -> dict[str, Any]: ...
