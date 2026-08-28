#!/usr/bin/env python3
"""Bounded SQLite decision prototype for issues #18 and #6.

This module is deliberately not imported by the production watcher.  It proves
the storage policy and narrow domain operations without installing a database,
changing hooks, or migrating live state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


WAL_MINIMUM = (3, 51, 3)
MIGRATION_NAME = "initial-storage-decision-prototype"


class StoreRefusal(RuntimeError):
    """A stable interface refusal, never a raw SQL contract for callers."""


@dataclass(frozen=True)
class ConnectionPolicy:
    loaded_runtime: tuple[int, int, int]
    journal_mode: str
    busy_timeout_ms: int
    foreign_keys: bool


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
      version INTEGER PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      checksum TEXT NOT NULL,
      applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE projects (
      project_id TEXT PRIMARY KEY,
      repository_url TEXT NOT NULL UNIQUE,
      state TEXT NOT NULL CHECK (state IN ('active', 'retired')),
      version INTEGER NOT NULL CHECK (version > 0),
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE tasks (
      task_id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(project_id),
      summary TEXT NOT NULL,
      state TEXT NOT NULL,
      version INTEGER NOT NULL CHECK (version > 0),
      current_owner_agent_id TEXT REFERENCES agent_instances(agent_id)
        DEFERRABLE INITIALLY DEFERRED,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE callsigns (
      callsign TEXT PRIMARY KEY,
      pool_role TEXT NOT NULL
        CHECK (pool_role IN ('shotcaller', 'champion', 'hidden-worker')),
      enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
      last_released_at TEXT
    )
    """,
    """
    CREATE TABLE agent_instances (
      agent_id TEXT PRIMARY KEY,
      callsign TEXT NOT NULL REFERENCES callsigns(callsign),
      role TEXT NOT NULL CHECK (role IN ('shotcaller', 'champion', 'hidden-worker')),
      task_id TEXT REFERENCES tasks(task_id),
      status TEXT NOT NULL,
      version INTEGER NOT NULL CHECK (version > 0),
      updated_at TEXT NOT NULL,
      update_text TEXT NOT NULL,
      retired_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX ux_live_callsign
      ON agent_instances(callsign) WHERE retired_at IS NULL
    """,
    """
    CREATE TABLE callsign_leases (
      callsign TEXT PRIMARY KEY REFERENCES callsigns(callsign),
      agent_id TEXT NOT NULL UNIQUE REFERENCES agent_instances(agent_id),
      reserved_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE events (
      event_id TEXT PRIMARY KEY,
      agent_id TEXT REFERENCES agent_instances(agent_id),
      task_id TEXT REFERENCES tasks(task_id),
      entity_version INTEGER NOT NULL CHECK (entity_version > 0),
      event_type TEXT NOT NULL,
      status TEXT,
      update_text TEXT NOT NULL,
      occurred_at TEXT NOT NULL,
      CHECK ((agent_id IS NOT NULL) + (task_id IS NOT NULL) = 1)
    )
    """,
    """
    CREATE UNIQUE INDEX ux_agent_event_version
      ON events(agent_id, entity_version) WHERE agent_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX ux_task_event_version
      ON events(task_id, entity_version) WHERE task_id IS NOT NULL
    """,
    """
    CREATE TABLE deliveries (
      event_id TEXT NOT NULL REFERENCES events(event_id),
      recipient_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      state TEXT NOT NULL
        CHECK (state IN ('claimed', 'accepted', 'acknowledged', 'failed')),
      attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
      claim_token TEXT NOT NULL,
      acknowledged_at TEXT,
      PRIMARY KEY (event_id, recipient_agent_id)
    )
    """,
    """
    CREATE TABLE assignment_receipts (
      task_id TEXT NOT NULL REFERENCES tasks(task_id),
      owner_agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id),
      task_version INTEGER NOT NULL CHECK (task_version > 0),
      received_at TEXT NOT NULL,
      PRIMARY KEY (task_id, task_version)
    )
    """,
    """
    CREATE INDEX ix_projects_repository ON projects(repository_url)
    """,
    """
    CREATE INDEX ix_deliveries_state ON deliveries(recipient_agent_id, state)
    """,
)


def _schema_checksum() -> str:
    source = "\n".join(" ".join(statement.split()) for statement in SCHEMA_STATEMENTS)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _version_tuple(value: Iterable[int]) -> tuple[int, int, int]:
    parts = tuple(int(part) for part in value)
    if len(parts) < 3:
        raise StoreRefusal("runtime version must contain major, minor, and patch")
    return parts[:3]


def choose_journal_mode(
    loaded_runtime: Iterable[int], *, request_wal: bool = True
) -> str:
    """Select WAL only from the application's loaded-library version."""
    loaded = _version_tuple(loaded_runtime)
    return "WAL" if request_wal and loaded >= WAL_MINIMUM else "DELETE"


class SQLiteStorePrototype:
    """Internal prototype behind the future stable ``league`` commands."""

    def __init__(
        self,
        database: Path,
        *,
        busy_timeout_ms: int = 250,
        request_wal: bool = True,
    ) -> None:
        if busy_timeout_ms < 1 or busy_timeout_ms > 10_000:
            raise StoreRefusal("busy timeout must be between 1 and 10000 milliseconds")
        self.database = Path(database)
        self.connection = sqlite3.connect(
            self.database,
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        loaded = _version_tuple(sqlite3.sqlite_version_info)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        requested_mode = choose_journal_mode(loaded, request_wal=request_wal)
        actual_mode = str(
            self.connection.execute(f"PRAGMA journal_mode={requested_mode}").fetchone()[0]
        ).upper()
        self.connection.execute("PRAGMA synchronous=FULL")
        foreign_keys = bool(self.connection.execute("PRAGMA foreign_keys").fetchone()[0])
        if not foreign_keys:
            self.connection.close()
            raise StoreRefusal("foreign-key enforcement could not be enabled")
        if actual_mode != requested_mode:
            self.connection.close()
            raise StoreRefusal(
                f"journal mode {requested_mode} was requested but {actual_mode} was loaded"
            )
        self.policy = ConnectionPolicy(
            loaded_runtime=loaded,
            journal_mode=actual_mode,
            busy_timeout_ms=busy_timeout_ms,
            foreign_keys=foreign_keys,
        )
        try:
            self.apply_migrations()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SQLiteStorePrototype":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def apply_migrations(self) -> None:
        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current > 1:
            raise StoreRefusal(f"database schema version {current} is newer than supported version 1")
        if current == 1:
            try:
                row = self.connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version=1"
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise StoreRefusal("schema version marker exists without a migration ledger") from exc
            if row is None or row["checksum"] != _schema_checksum():
                raise StoreRefusal("migration checksum mismatch")
            return
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA_STATEMENTS:
                self.connection.execute(statement)
            self.connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(1,?,?,?)",
                (MIGRATION_NAME, _schema_checksum(), "2026-08-28T00:00:00Z"),
            )
            self.connection.execute("PRAGMA user_version=1")
            self.connection.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise StoreRefusal(f"migration failed: {exc}") from exc

    def create_project(self, project_id: str, repository_url: str, at: str) -> None:
        try:
            self.connection.execute(
                "INSERT INTO projects VALUES(?,?,\'active\',1,?)",
                (project_id, repository_url, at),
            )
        except sqlite3.IntegrityError as exc:
            raise StoreRefusal("project identity already exists or is invalid") from exc

    def create_task(self, task_id: str, project_id: str, summary: str, at: str) -> None:
        try:
            self.connection.execute(
                "INSERT INTO tasks VALUES(?,?,?,\'active\',1,NULL,?)",
                (task_id, project_id, summary, at),
            )
        except sqlite3.IntegrityError as exc:
            raise StoreRefusal("task identity is invalid or its project does not exist") from exc

    def add_callsign(self, callsign: str, role: str = "champion") -> None:
        try:
            self.connection.execute(
                "INSERT INTO callsigns VALUES(?,?,1,NULL)", (callsign, role)
            )
        except sqlite3.IntegrityError as exc:
            raise StoreRefusal("callsign already exists or has an unsupported role") from exc

    def reserve_callsign(
        self,
        callsign: str,
        agent_id: str,
        task_id: str,
        at: str,
    ) -> str:
        """Reserve a callsign, create its incarnation, and append event 1 atomically."""
        event_id = f"agent:{agent_id}:1"
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                INSERT INTO agent_instances
                  (agent_id,callsign,role,task_id,status,version,updated_at,update_text,retired_at)
                VALUES(?,?,\'champion\',?,\'working\',1,?,\'reserved\',NULL)
                """,
                (agent_id, callsign, task_id, at),
            )
            self.connection.execute(
                "INSERT INTO callsign_leases VALUES(?,?,?)", (callsign, agent_id, at)
            )
            self.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at)
                VALUES(?,?,NULL,1,\'callsign_reserved\',\'working\',\'reserved\',?)
                """,
                (event_id, agent_id, at),
            )
            self.connection.execute("COMMIT")
            return event_id
        except sqlite3.DatabaseError as exc:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise StoreRefusal("callsign reservation conflicted or referenced invalid state") from exc

    def transition(
        self,
        agent_id: str,
        expected_version: int,
        status: str,
        update_text: str,
        at: str,
    ) -> str:
        """Append an ordered event and compare-and-swap current state atomically."""
        next_version = expected_version + 1
        event_id = f"agent:{agent_id}:{next_version}"
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute(
                """
                UPDATE agent_instances
                   SET status=?, version=?, updated_at=?, update_text=?
                 WHERE agent_id=? AND version=? AND retired_at IS NULL
                """,
                (status, next_version, at, update_text, agent_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise StoreRefusal("transition precondition failed")
            self.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at)
                VALUES(?,?,NULL,?,\'agent_transition\',?,?,?)
                """,
                (event_id, agent_id, next_version, status, update_text, at),
            )
            self.connection.execute("COMMIT")
            return event_id
        except StoreRefusal:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        except sqlite3.DatabaseError as exc:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise StoreRefusal(f"transition failed: {exc}") from exc

    def claim_delivery(
        self, event_id: str, recipient_agent_id: str, claim_token: str
    ) -> None:
        try:
            self.connection.execute(
                "INSERT INTO deliveries VALUES(?,?,\'claimed\',1,?,NULL)",
                (event_id, recipient_agent_id, claim_token),
            )
        except sqlite3.IntegrityError as exc:
            raise StoreRefusal("delivery claim conflicted or referenced unknown identity") from exc

    def acknowledge_delivery(
        self, event_id: str, recipient_agent_id: str, claim_token: str, at: str
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE deliveries SET state='acknowledged', acknowledged_at=?
             WHERE event_id=? AND recipient_agent_id=? AND claim_token=? AND state='claimed'
            """,
            (at, event_id, recipient_agent_id, claim_token),
        )
        if cursor.rowcount != 1:
            raise StoreRefusal("delivery acknowledgement precondition failed")

    def transfer_task_owner(
        self,
        task_id: str,
        expected_version: int,
        new_owner_agent_id: str,
        at: str,
    ) -> str:
        next_version = expected_version + 1
        event_id = f"task:{task_id}:{next_version}"
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            owner = self.connection.execute(
                """
                SELECT task_id FROM agent_instances
                 WHERE agent_id=? AND task_id=? AND retired_at IS NULL
                """,
                (new_owner_agent_id, task_id),
            ).fetchone()
            if owner is None:
                raise StoreRefusal("new owner is not an active agent for this task")
            cursor = self.connection.execute(
                """
                UPDATE tasks SET current_owner_agent_id=?, version=?, updated_at=?
                 WHERE task_id=? AND version=?
                """,
                (new_owner_agent_id, next_version, at, task_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise StoreRefusal("owner-transfer precondition failed")
            self.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,entity_version,event_type,status,update_text,occurred_at)
                VALUES(?,NULL,?,?,\'task_owner_transferred\',\'active\',\'owner transferred\',?)
                """,
                (event_id, task_id, next_version, at),
            )
            self.connection.execute(
                "INSERT INTO assignment_receipts VALUES(?,?,?,?)",
                (task_id, new_owner_agent_id, next_version, at),
            )
            self.connection.execute("COMMIT")
            return event_id
        except StoreRefusal:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        except sqlite3.DatabaseError as exc:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise StoreRefusal(f"owner transfer failed: {exc}") from exc

    def project_lookup(self, repository_url: str) -> Optional[dict[str, Any]]:
        row = self.connection.execute(
            "SELECT project_id,repository_url,state,version,updated_at FROM projects WHERE repository_url=?",
            (repository_url,),
        ).fetchone()
        return dict(row) if row is not None else None

    def agent_snapshot(self, agent_id: str) -> Optional[dict[str, Any]]:
        row = self.connection.execute(
            """
            SELECT agent_id,callsign,role,task_id,status,version,updated_at,update_text
              FROM agent_instances WHERE agent_id=?
            """,
            (agent_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def integrity(self) -> dict[str, Any]:
        integrity_rows = [row[0] for row in self.connection.execute("PRAGMA integrity_check")]
        foreign_key_rows = [tuple(row) for row in self.connection.execute("PRAGMA foreign_key_check")]
        return {
            "integrity": integrity_rows,
            "foreign_key_violations": foreign_key_rows,
            "ok": integrity_rows == ["ok"] and not foreign_key_rows,
        }

    def backup(self, destination: Path) -> dict[str, Any]:
        target = sqlite3.connect(destination)
        try:
            self.connection.backup(target)
        finally:
            target.close()
        check = sqlite3.connect(destination)
        try:
            integrity = [row[0] for row in check.execute("PRAGMA integrity_check")]
            foreign_keys = [tuple(row) for row in check.execute("PRAGMA foreign_key_check")]
        finally:
            check.close()
        if integrity != ["ok"] or foreign_keys:
            raise StoreRefusal("backup integrity validation failed")
        return {
            "kind": "sqlite-backup",
            "schema": 1,
            "integrity": "ok",
            "foreign_key_violations": 0,
        }

    def export(self) -> dict[str, Any]:
        """Return a deterministic, bounded export without the database path."""
        tables = (
            ("projects", "project_id"),
            ("tasks", "task_id"),
            ("callsigns", "callsign"),
            ("agent_instances", "agent_id"),
            ("callsign_leases", "callsign"),
            ("events", "event_id"),
            ("deliveries", "event_id,recipient_agent_id"),
            ("assignment_receipts", "task_id,task_version"),
        )
        exported: dict[str, Any] = {"schema": 1, "canonical": False, "tables": {}}
        for table, order_by in tables:
            columns = [row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")]
            rows = [
                dict(zip(columns, row))
                for row in self.connection.execute(
                    f"SELECT * FROM {table} ORDER BY {order_by}"
                )
            ]
            exported["tables"][table] = rows
        return exported


def stable_export_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
