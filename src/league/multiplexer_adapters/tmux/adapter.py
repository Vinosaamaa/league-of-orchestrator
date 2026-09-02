"""Compatibility declaration for future tmux restore integration."""

from __future__ import annotations

from typing import Any, Mapping

from ...storage_types import StorageRefusal
from ..contract import RestoredEndpoint


class TmuxMultiplexerAdapter:
    kind = "tmux"
    capabilities: frozenset[str] = frozenset()

    @staticmethod
    def _unsupported() -> None:
        raise StorageRefusal(
            "multiplexer_restore_unsupported",
            "tmux restore replay requires its separately owned native startup integration",
        )

    def restored_snapshot(self) -> list[dict[str, Any]]:
        self._unsupported()

    def endpoint(self, descriptor_id: str, item: Mapping[str, Any]) -> RestoredEndpoint:
        self._unsupported()

    def inspect_restored(
        self, descriptor: Mapping[str, Any], endpoint: RestoredEndpoint
    ) -> dict[str, Any]:
        self._unsupported()

    def metadata(
        self, presentation: Mapping[str, Any], endpoint: RestoredEndpoint, first_sequence: int
    ) -> dict[str, Any]:
        self._unsupported()


__all__ = ["TmuxMultiplexerAdapter"]
