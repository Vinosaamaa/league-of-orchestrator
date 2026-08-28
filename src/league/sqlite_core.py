"""Shared SQLite connection and transaction mechanics."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .storage_types import StorageRefusal


class SQLiteTransactionCore:
    """Low-level behavior shared by all SQLite operation modules."""

    connection: sqlite3.Connection

    @staticmethod
    def _translate_database_error(
        exc: sqlite3.DatabaseError, message: str
    ) -> StorageRefusal:
        code = getattr(exc, "sqlite_errorcode", None)
        if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return StorageRefusal(
                "busy", "SQLite contention exceeded the bounded timeout", retryable=True
            )
        if isinstance(exc, sqlite3.IntegrityError):
            return StorageRefusal("conflict", message)
        return StorageRefusal("database_error", message)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
