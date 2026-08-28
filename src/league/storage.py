"""Composite stable storage interface used by commands and domain services."""

from __future__ import annotations

from .storage_admin import AdministrativeStorage
from .storage_assignment import AssignmentStorage, PrepareAssignmentCommand
from .storage_callsign import CallsignQueueStorage
from .storage_delivery import DeliveryStorage
from .storage_lifecycle import LifecycleStorage
from .storage_outbox import OutboxDispatchIdentity, OutboxStorage
from .storage_project import ProjectStorage
from .storage_request import (
    AnswerRequestCommand,
    DispatchRequestCommand,
    RequestResultCommand,
    RequestStorage,
)
from .storage_runtime import RuntimeLifecycleStorage
from .storage_roster import RosterStorage
from .storage_rollover import RolloverStorage
from .storage_transfer import TransferStorage
from .storage_watcher import RuntimeRegistrationCommand, WatcherStorage
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
    RequestStorage,
    AssignmentStorage,
    OutboxStorage,
    WatcherStorage,
    RuntimeLifecycleStorage,
    ProjectStorage,
    RosterStorage,
    CallsignQueueStorage,
    RolloverStorage,
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
    "AnswerRequestCommand",
    "DispatchRequestCommand",
    "OutboxDispatchIdentity",
    "PrepareAssignmentCommand",
    "RequestResultCommand",
    "RuntimeRegistrationCommand",
    "Storage",
    "StorageRefusal",
]
