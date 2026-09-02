"""Provider-neutral agent lifecycle adapters."""

from .core import (
    ADAPTER_OPERATIONS,
    AgentLifecycleAdapter,
    LifecycleDecision,
    LifecycleEvent,
    OPERATION_METHODS,
    SharedLifecyclePolicy,
)
from .registry import (
    AgentAdapterRegistry,
    adapter_kind_from_runtime,
    builtin_agent_adapter_registry,
)

__all__ = [
    "ADAPTER_OPERATIONS",
    "AgentAdapterRegistry",
    "adapter_kind_from_runtime",
    "AgentLifecycleAdapter",
    "LifecycleDecision",
    "LifecycleEvent",
    "OPERATION_METHODS",
    "SharedLifecyclePolicy",
    "builtin_agent_adapter_registry",
]
