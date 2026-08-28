"""Stable advisory project-catalog storage boundary."""

from __future__ import annotations

from typing import Optional, Protocol, Sequence


class ProjectStorage(Protocol):
    def put_project(
        self,
        project_id: str,
        *,
        expected_version: int,
        summary: str,
        repository: str,
        root: str,
        code: Optional[str],
        aliases: Sequence[str],
        state: str,
        repository_visibility: str,
        export_policy: str,
        at: str,
    ) -> dict[str, object]: ...

    def set_project_suggestions(
        self,
        project_id: str,
        expected_version: int,
        squad_ids: Sequence[str],
        at: str,
    ) -> dict[str, object]: ...

    def resolve_project(
        self,
        repository: Optional[str] = None,
        *,
        project_id: Optional[str] = None,
        root: Optional[str] = None,
        code: Optional[str] = None,
        alias: Optional[str] = None,
        visibility: str = "local",
    ) -> Optional[dict[str, object]]: ...

    def list_projects(
        self, *, visibility: str = "local", limit: int = 200
    ) -> dict[str, object]: ...

    def project_advice(
        self,
        project_id: str,
        *,
        explicit_squad_id: Optional[str] = None,
        visibility: str = "local",
    ) -> dict[str, object]: ...
