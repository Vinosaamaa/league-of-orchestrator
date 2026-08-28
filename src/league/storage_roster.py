"""Stable bounded read-only Roster snapshot boundary."""

from __future__ import annotations

from typing import Protocol


class RosterStorage(Protocol):
    def roster_snapshot(
        self,
        *,
        as_of: str,
        recent_since: str,
        stale_before: str,
        limit: int = 500,
        visibility: str = "outbound",
    ) -> dict[str, object]: ...
