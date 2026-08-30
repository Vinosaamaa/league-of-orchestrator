"""Composite stable storage interface used by commands and domain services."""

from __future__ import annotations

from .storage_admin import AdministrativeStorage
from .storage_artifact import ArtifactStorage
from .storage_assignment import (
    AssignmentStorage,
    LegacyDisplayReconciliationCommand,
    PrepareAssignmentCommand,
)
from .storage_callsign import CallsignQueueStorage
from .storage_continuation import ContinuationStorage
from .storage_delivery import DeliveryStorage
from .storage_lifecycle import LifecycleStorage
from .storage_issue import IssueStorage
from .storage_mode import ModeStorage, SettleModeActionCommand
from .storage_outbox import OutboxDispatchIdentity, OutboxStorage
from .storage_project import ProjectStorage
from .storage_reporting import ReportingStorage
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
    ArtifactStorage,
    LifecycleStorage,
    DeliveryStorage,
    TransferStorage,
    RequestStorage,
    AssignmentStorage,
    OutboxStorage,
    WatcherStorage,
    RuntimeLifecycleStorage,
    ReportingStorage,
    ProjectStorage,
    RosterStorage,
    CallsignQueueStorage,
    RolloverStorage,
    ModeStorage,
    IssueStorage,
    ContinuationStorage,
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
    "LegacyDisplayReconciliationCommand",
    "AnswerRequestCommand",
    "DispatchRequestCommand",
    "OutboxDispatchIdentity",
    "PrepareAssignmentCommand",
    "RequestResultCommand",
    "RuntimeRegistrationCommand",
    "SettleModeActionCommand",
    "Storage",
    "StorageRefusal",
]
