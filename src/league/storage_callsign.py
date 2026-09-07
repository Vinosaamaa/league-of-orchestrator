"""Persistent callsign queue portion of the stable storage contract."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, Sequence

from .storage_types import FaultInjector


class CallsignQueueStorage(Protocol):
    def callsign_assignment_status(self, assignment_id: str) -> Optional[dict[str, Any]]: ...

    def record_shotcaller_bootstrap(
        self,
        assignment_id: str,
        expected_version: int,
        receipt: Mapping[str, Any],
        at: str,
        *,
        fault: Optional[FaultInjector] = None,
    ) -> dict[str, Any]: ...

    def shotcaller_bootstrap_status(self, assignment_id: str) -> Optional[dict[str, Any]]: ...

    def record_shotcaller_bootstrap_baseline(
        self,
        assignment_id: str,
        expected_version: int,
        baseline: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def shotcaller_bootstrap_baseline(self, assignment_id: str) -> Optional[dict[str, Any]]: ...

    def record_shotcaller_bootstrap_publication(
        self,
        assignment_id: str,
        expected_version: int,
        publication: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def shotcaller_bootstrap_publication(
        self, assignment_id: str
    ) -> Optional[dict[str, Any]]: ...

    def bind_shotcaller_bootstrap_runtime(
        self, assignment_id: str, expected_version: int, runtime_instance_id: str
    ) -> dict[str, Any]: ...

    def reconcile_callsign_pool(
        self,
        role: str,
        expected_queue_version: int,
        seed: str,
        shuffle_version: int,
        entries: Sequence[Mapping[str, Any]],
        at: str,
    ) -> dict[str, Any]: ...

    def allocate_callsign(
        self,
        assignment_id: str,
        agent_id: str,
        role: str,
        scope_kind: str,
        scope_id: str,
        required_capabilities: Sequence[str],
        at: str,
        *,
        fault: Optional[FaultInjector] = None,
        recovery_baseline: Optional[Mapping[str, Any]] = None,
        recovery_thread_id: Optional[str] = None,
        expected_callsign: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def activate_callsign(
        self,
        assignment_id: str,
        expected_version: int,
        receipt: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]: ...

    def rollback_callsign(
        self,
        assignment_id: str,
        expected_version: int,
        failure_receipt_digest: str,
        at: str,
    ) -> dict[str, Any]: ...

    def release_callsign(
        self,
        assignment_id: str,
        expected_version: int,
        release_receipt_digest: str,
        at: str,
    ) -> dict[str, Any]: ...

    def callsign_status(self, role: str) -> dict[str, Any]: ...
