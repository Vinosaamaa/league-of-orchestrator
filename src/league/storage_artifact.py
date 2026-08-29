"""Repository-owned artifact storage boundary."""

from __future__ import annotations

from typing import Any, Mapping, Protocol


class ArtifactStorage(Protocol):
    def declare_repository_artifact(
        self, declaration: Mapping[str, Any], at: str
    ) -> dict[str, Any]: ...

    def record_repository_publication(
        self, artifact_id: str, expected_version: int, receipt: Mapping[str, Any], at: str
    ) -> dict[str, Any]: ...

    def task_artifacts(self, task_id: str) -> list[dict[str, Any]]: ...

    def unresolved_repository_publications(
        self, task_id: str
    ) -> list[dict[str, Any]]: ...
