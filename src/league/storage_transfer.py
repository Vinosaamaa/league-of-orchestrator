"""Import and deterministic export portion of the storage contract."""

from __future__ import annotations

from typing import Any, Optional, Protocol

from .storage_types import FaultInjector, ImportPlan


class TransferStorage(Protocol):
    def export_bytes(
        self, *, format_name: str, purpose: str, max_records: int
    ) -> bytes: ...

    def apply_import(
        self,
        plan: ImportPlan,
        expected_digest: str,
        *,
        fault: Optional[FaultInjector] = None,
    ) -> dict[str, Any]: ...

    def import_target_counts(self) -> dict[str, int]: ...
