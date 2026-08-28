"""Composite stable storage interface used by commands and domain services."""

from __future__ import annotations

from .storage_admin import AdministrativeStorage
from .storage_delivery import DeliveryStorage
from .storage_lifecycle import LifecycleStorage
from .storage_runtime import RuntimeLifecycleStorage
from .storage_transfer import TransferStorage
from .storage_types import (
    ConnectionPolicy,
    FaultInjector,
    ImportArtifact,
    ImportPlan,
    StorageRefusal,
)


class Storage(
    AdministrativeStorage,
    LifecycleStorage,
    DeliveryStorage,
    TransferStorage,
    RuntimeLifecycleStorage,
):
    """The only domain-facing persistence interface.

    Implementations hide paths, SQL, pragmas, transactions, and retry details.
    The subprotocols keep administration, lifecycle, delivery, and transfer
    dependencies cohesive while this remains the one application interface.
    """


__all__ = [
    "ConnectionPolicy",
    "FaultInjector",
    "ImportArtifact",
    "ImportPlan",
    "Storage",
    "StorageRefusal",
]
