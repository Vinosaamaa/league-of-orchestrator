"""Explicit built-in multiplexer adapter registry."""

from __future__ import annotations

from ..storage_types import StorageRefusal
from .contract import (
    MULTIPLEXER_OPERATIONS,
    MULTIPLEXER_OPERATION_METHODS,
    MultiplexerAdapter,
)
from .herdr import adapter as herdr_adapter
from .tmux import adapter as tmux_adapter


class MultiplexerAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, MultiplexerAdapter] = {}

    def register(self, adapter: MultiplexerAdapter) -> None:
        if adapter.kind in self._adapters:
            raise StorageRefusal(
                "adapter_conflict", f"multiplexer adapter is already registered: {adapter.kind}"
            )
        unknown = adapter.capabilities - MULTIPLEXER_OPERATIONS
        missing = sorted(
            capability
            for capability in adapter.capabilities
            if any(
                not callable(getattr(adapter, method, None))
                for method in MULTIPLEXER_OPERATION_METHODS[capability]
            )
        )
        if unknown or missing:
            raise StorageRefusal(
                "adapter_contract_invalid",
                "multiplexer adapter advertises an unknown or non-callable capability",
            )
        self._adapters[adapter.kind] = adapter

    def adapter(self, kind: str) -> MultiplexerAdapter:
        try:
            return self._adapters[kind]
        except KeyError as exc:
            raise StorageRefusal(
                "adapter_unknown", f"multiplexer adapter is not registered: {kind}"
            ) from exc

    def adapters(self) -> tuple[MultiplexerAdapter, ...]:
        return tuple(self._adapters[kind] for kind in sorted(self._adapters))


def builtin_multiplexer_adapter_registry(
    *, herdr_runner=None, herdr_binary=None
) -> MultiplexerAdapterRegistry:
    registry = MultiplexerAdapterRegistry()
    registry.register(herdr_adapter(runner=herdr_runner, binary=herdr_binary))
    registry.register(tmux_adapter())
    return registry


__all__ = ["MultiplexerAdapterRegistry", "builtin_multiplexer_adapter_registry"]
