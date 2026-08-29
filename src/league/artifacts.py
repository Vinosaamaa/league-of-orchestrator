"""Minimal repository-owned artifact lifecycle validation."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping

from .storage_artifact import ArtifactStorage
from .storage_types import StorageRefusal


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _text(value: Any, field: str, maximum: int = 2048) -> str:
    if not isinstance(value, str):
        raise StorageRefusal("artifact_invalid", f"{field} must be text")
    item = value.strip()
    if not item or item != value or len(item) > maximum or "\x00" in item:
        raise StorageRefusal("artifact_invalid", f"{field} is empty or exceeds its bound")
    return item


def _id(value: Any, field: str) -> str:
    item = _text(value, field, 192)
    if not SAFE_ID.fullmatch(item):
        raise StorageRefusal("artifact_invalid", f"{field} is not a stable League identifier")
    return item


def _path(value: Any) -> str:
    path = PurePosixPath(_text(value, "repository_path", 512))
    if path.is_absolute() or any(part in {"", ".", "..", ".git"} for part in path.parts):
        raise StorageRefusal("repository_path_invalid", "repository artifact path must be relative")
    return path.as_posix()


def declaration(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "artifact_id", "task_id", "name", "classification", "repository",
        "issue", "worktree", "branch", "repository_path",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise StorageRefusal("artifact_invalid", "repository artifact declaration is incomplete")
    if value["classification"] != "repository_owned":
        raise StorageRefusal("artifact_invalid", "this command accepts repository_owned artifacts only")
    issue = value["issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
        raise StorageRefusal("artifact_invalid", "issue must be a positive integer")
    branch = _text(value["branch"], "branch", 255)
    if branch.casefold() in {"main", "master"}:
        raise StorageRefusal("direct_main_refused", "repository artifacts require an issue branch")
    worktree = _text(value["worktree"], "worktree", 4096)
    if not worktree.startswith("/"):
        raise StorageRefusal("worktree_invalid", "worktree must be an exact absolute path")
    repository = _text(value["repository"], "repository", 2048)
    if not repository.startswith("https://"):
        raise StorageRefusal("artifact_invalid", "repository must use HTTPS")
    return {
        "artifact_id": _id(value["artifact_id"], "artifact_id"),
        "task_id": _id(value["task_id"], "task_id"),
        "name": _text(value["name"], "name", 256),
        "classification": "repository_owned",
        "repository": repository,
        "issue": issue,
        "worktree": worktree,
        "branch": branch,
        "repository_path": _path(value["repository_path"]),
    }


def publication(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {"pull_request_number", "pull_request_url", "tested_head", "merge_receipt"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise StorageRefusal("merge_receipt_missing", "merged publication receipt is incomplete")
    merge = value["merge_receipt"]
    if not isinstance(merge, Mapping) or set(merge) != {"commit", "url", "merged_at"}:
        raise StorageRefusal("merge_receipt_missing", "exact merge receipt is required")
    number = value["pull_request_number"]
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise StorageRefusal("publication_receipt_invalid", "pull request number must be positive")
    tested_head = _text(value["tested_head"], "tested_head", 40)
    merge_commit = _text(merge["commit"], "merge_receipt.commit", 40)
    if not GIT_SHA.fullmatch(tested_head) or not GIT_SHA.fullmatch(merge_commit):
        raise StorageRefusal("publication_receipt_invalid", "publication Git identities must be full SHAs")
    pr_url = _text(value["pull_request_url"], "pull_request_url", 2048)
    merge_url = _text(merge["url"], "merge_receipt.url", 2048)
    if not pr_url.startswith("https://") or not merge_url.startswith("https://"):
        raise StorageRefusal("publication_receipt_invalid", "publication URLs must use HTTPS")
    return {
        "pull_request_number": number,
        "pull_request_url": pr_url,
        "tested_head": tested_head,
        "merge_receipt": {
            "commit": merge_commit,
            "url": merge_url,
            "merged_at": _text(merge["merged_at"], "merge_receipt.merged_at", 64),
        },
    }


class ArtifactLifecycle:
    def __init__(self, storage: ArtifactStorage) -> None:
        self.storage = storage

    def declare(self, value: Mapping[str, Any], at: str) -> dict[str, Any]:
        return self.storage.declare_repository_artifact(declaration(value), at)

    def publish(
        self, artifact_id: str, expected_version: int, value: Mapping[str, Any], at: str
    ) -> dict[str, Any]:
        return self.storage.record_repository_publication(
            _id(artifact_id, "artifact_id"), expected_version, publication(value), at
        )

    def status(self, task_id: str) -> dict[str, Any]:
        exact = _id(task_id, "task_id")
        return {"task_id": exact, "artifacts": self.storage.task_artifacts(exact)}
