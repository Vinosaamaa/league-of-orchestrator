"""Shared types for League's storage boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, TypedDict


class StorageRefusal(RuntimeError):
    """A bounded public refusal that does not expose implementation details."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ConnectionPolicy:
    loaded_runtime: tuple[int, int, int]
    journal_mode: str
    wal_allowed: bool
    wal_refusal: Optional[str]
    busy_timeout_ms: int
    foreign_keys: bool
    synchronous: str


FaultInjector = Callable[[str], None]

LIFECYCLE_STATES = (
    "active",
    "started",
    "working",
    "progress",
    "blocked",
    "completed",
    "complete",
    "cancelled",
    "canceled",
    "failed",
    "ready_to_land",
)


class ImportArtifact(TypedDict):
    artifact_id: str
    kind: str
    digest: str
    record_count: int
    source_order: int


class ImportPlan(TypedDict):
    """Validated, digest-bound plan accepted by the storage implementation."""

    report: dict[str, Any]
    target_schema_version: int
    report_digest: str
    source_digest: str
    applied_at: str
    artifacts: list[ImportArtifact]
    rows: dict[str, list[dict[str, Any]]]
