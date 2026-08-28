"""Administrative portion of the stable storage contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Protocol

from .storage_types import ConnectionPolicy, FaultInjector


class AdministrativeStorage(Protocol):
    policy: ConnectionPolicy

    def close(self) -> None: ...

    def migrate(
        self, *, backup_name: Optional[str] = None, fault: Optional[FaultInjector] = None
    ) -> dict[str, Any]: ...

    def integrity(self) -> dict[str, Any]: ...

    def backup(
        self, name: str, *, fault: Optional[FaultInjector] = None
    ) -> dict[str, Any]: ...

    def write_restricted(self, name: str, payload: bytes) -> Path: ...
