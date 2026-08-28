"""Stable activity-evidence and bounded reporting storage boundary."""

from __future__ import annotations

from typing import Any, Optional, Protocol


class ReportingStorage(Protocol):
    def record_activity_evidence(self, evidence: dict[str, Any]) -> dict[str, Any]: ...

    def generate_report(
        self,
        *,
        from_at: str,
        to_at: str,
        timezone_name: str,
        from_inclusive: bool,
        scope_kind: str,
        scope_id: Optional[str],
        limit: int,
        cursor: Optional[str],
        local_diagnostic: bool,
        report_id: Optional[str] = None,
        event_watermark: Optional[int] = None,
        source_watermark: Optional[str] = None,
        persist: bool = True,
        expected_content_hash: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def report_spec(self, report_id: str) -> Optional[dict[str, Any]]: ...
