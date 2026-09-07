"""Multiplexer-neutral lifecycle adapters."""

from .contract import (
    CommandRunner,
    MULTIPLEXER_OPERATIONS,
    MULTIPLEXER_OPERATION_METHODS,
    MultiplexerAdapter,
    RestoredEndpoint,
)
from .registry import MultiplexerAdapterRegistry, builtin_multiplexer_adapter_registry

__all__ = [
    "MULTIPLEXER_OPERATIONS",
    "MULTIPLEXER_OPERATION_METHODS",
    "CommandRunner",
    "MultiplexerAdapter",
    "MultiplexerAdapterRegistry",
    "RestoredEndpoint",
    "builtin_multiplexer_adapter_registry",
]
