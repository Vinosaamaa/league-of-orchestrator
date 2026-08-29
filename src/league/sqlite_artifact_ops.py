"""Transactional repository-owned artifact operations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from .storage_types import StorageRefusal


def declare(store: Any, value: Mapping[str, Any], at: str) -> dict[str, Any]:
    try:
        with store._transaction():
            task = store.connection.execute(
                "SELECT state FROM tasks WHERE task_id=?", (value["task_id"],)
            ).fetchone()
            if task is None:
                raise StorageRefusal("task_unknown", "artifact task does not exist")
            if task["state"] in {"completed", "complete", "cancelled", "canceled", "failed", "rejected"}:
                raise StorageRefusal(
                    "artifact_declaration_late", "expected artifacts must be declared before terminal completion"
                )
            store.connection.execute(
                """
                INSERT INTO repository_artifacts
                  (artifact_id,task_id,name,classification,repository,issue,worktree,branch,
                   repository_path,state,version,declared_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'pending',1,?,?)
                """,
                (
                    value["artifact_id"], value["task_id"], value["name"], value["classification"],
                    value["repository"], value["issue"], value["worktree"], value["branch"],
                    value["repository_path"], at, at,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "artifact declaration conflicted with canonical state") from exc
    return {
        "artifact_id": value["artifact_id"], "task_id": value["task_id"],
        "classification": "repository_owned", "state": "pending", "version": 1,
    }


def publish(
    store: Any, artifact_id: str, expected_version: int, receipt: Mapping[str, Any], at: str
) -> dict[str, Any]:
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT * FROM repository_artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise StorageRefusal("artifact_unknown", "repository artifact does not exist")
            if int(row["version"]) != expected_version or row["state"] != "pending":
                raise StorageRefusal("artifact_conflict", "artifact is not pending at the expected version")
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE repository_artifacts
                   SET pull_request_number=?,pull_request_url=?,tested_head=?,merge_commit=?,
                       merge_url=?,merge_receipt_json=?,state='published',version=?,updated_at=?
                 WHERE artifact_id=?
                """,
                (
                    receipt["pull_request_number"], receipt["pull_request_url"], receipt["tested_head"],
                    receipt["merge_receipt"]["commit"], receipt["merge_receipt"]["url"],
                    json.dumps(receipt["merge_receipt"], sort_keys=True, separators=(",", ":")),
                    next_version, at, artifact_id,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "publication receipt conflicted with canonical state") from exc
    return {
        "artifact_id": artifact_id, "task_id": row["task_id"],
        "classification": "repository_owned", "state": "published", "version": next_version,
        "tested_head": receipt["tested_head"], "merge_commit": receipt["merge_receipt"]["commit"],
    }


def status(store: Any, task_id: str) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        "SELECT * FROM repository_artifacts WHERE task_id=? ORDER BY artifact_id", (task_id,)
    ).fetchall()
    return [
        {
            "artifact_id": row["artifact_id"], "task_id": row["task_id"], "name": row["name"],
            "classification": row["classification"], "repository": row["repository"],
            "issue": row["issue"], "worktree": row["worktree"], "branch": row["branch"],
            "repository_path": row["repository_path"], "state": row["state"],
            "version": int(row["version"]), "pull_request_number": row["pull_request_number"],
            "pull_request_url": row["pull_request_url"], "tested_head": row["tested_head"],
            "merge_commit": row["merge_commit"], "merge_url": row["merge_url"],
        }
        for row in rows
    ]


def unresolved(store: Any, task_id: str) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        "SELECT artifact_id,state FROM repository_artifacts WHERE task_id=? AND state!='published' ORDER BY artifact_id",
        (task_id,),
    ).fetchall()
    return [{"artifact_id": row["artifact_id"], "state": row["state"]} for row in rows]
