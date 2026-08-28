"""SQLite import and deterministic export operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping, Optional

from .storage_types import FaultInjector, ImportPlan, StorageRefusal


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def canonical_counts(store: Any, table_order: Iterable[str]) -> dict[str, int]:
    return {
        table: int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in table_order
    }


def apply_import(
    store: Any,
    plan: ImportPlan,
    expected_digest: str,
    *,
    columns_by_table: Mapping[str, tuple[str, ...]],
    table_order: tuple[str, ...],
    fault: Optional[FaultInjector] = None,
) -> dict[str, Any]:
    observed_digest = str(plan.get("report_digest", ""))
    report_for_digest = dict(plan.get("report", {}))
    report_for_digest.pop("report_digest", None)
    recomputed_digest = hashlib.sha256(
        json.dumps(
            {"report": report_for_digest, "rows": plan.get("rows")},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if observed_digest != recomputed_digest:
        raise StorageRefusal("import_plan_invalid", "import plan digest does not match its validated rows")
    if not expected_digest or observed_digest != expected_digest:
        raise StorageRefusal("import_digest_mismatch", "apply requires the exact preceding dry-run digest")
    collisions = {
        table: count for table, count in canonical_counts(store, table_order).items() if count
    }
    if collisions:
        raise StorageRefusal("import_collision", "target canonical store is not empty")
    rows = plan.get("rows")
    if not isinstance(rows, dict) or set(rows) != set(columns_by_table):
        raise StorageRefusal("import_plan_invalid", "import plan table coverage is invalid")
    run_id = f"import:{observed_digest[:24]}"
    try:
        with store._transaction():
            store.connection.execute(
                "INSERT INTO import_runs(run_id,report_digest,source_digest,applied_at) VALUES(?,?,?,?)",
                (run_id, observed_digest, plan["source_digest"], plan["applied_at"]),
            )
            for table in table_order:
                columns = columns_by_table[table]
                statement = (
                    f"INSERT INTO {table} ({','.join(columns)}) "
                    f"VALUES({','.join('?' for _ in columns)})"
                )
                for row in rows[table]:
                    if set(row) != set(columns):
                        raise StorageRefusal(
                            "import_plan_invalid", f"import plan row for {table} is invalid"
                        )
                    store.connection.execute(
                        statement, tuple(row[column] for column in columns)
                    )
                if fault and table == "events":
                    fault("after_import_events")
            for artifact in plan["artifacts"]:
                store.connection.execute(
                    """
                    INSERT INTO imported_artifacts
                      (artifact_id,kind,digest,record_count,source_order,import_run_id)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        artifact["artifact_id"],
                        artifact["kind"],
                        artifact["digest"],
                        artifact["record_count"],
                        artifact["source_order"],
                        run_id,
                    ),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "import collided with canonical state"
        ) from exc
    result = dict(plan["report"])
    result.update({"dry_run": False, "applied": True, "report_digest": observed_digest})
    return result


def _redacted_row(
    columns: list[str],
    values: sqlite3.Row,
    *,
    table: str,
    purpose: str,
    redactions: Mapping[str, set[str]],
) -> dict[str, Any]:
    row = dict(zip(columns, values))
    if purpose == "inspection":
        for field in redactions.get(table, set()):
            if row.get(field) is not None:
                row[field] = "[redacted]"
    return row


def export_bytes(
    store: Any,
    *,
    format_name: str,
    purpose: str,
    max_records: int,
    maximum_records: int,
    current_schema_version: int,
    export_tables: tuple[str, ...],
    export_order: Mapping[str, str],
    redactions: Mapping[str, set[str]],
) -> bytes:
    if format_name not in {"json", "jsonl"} or purpose not in {"inspection", "rollback"}:
        raise StorageRefusal("invalid_export", "export format or purpose is unsupported")
    if not 1 <= max_records <= maximum_records:
        raise StorageRefusal(
            "invalid_export", f"max records must be between 1 and {maximum_records}"
        )
    if not store.integrity()["ok"]:
        raise StorageRefusal(
            "integrity_failed", "export requires clean integrity and foreign-key checks"
        )
    table_counts = {
        table: int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in export_tables
    }
    total = sum(table_counts.values())
    if total > max_records:
        raise StorageRefusal("export_too_large", "export exceeds the requested record bound")
    header = {
        "schema": "league.export.v1",
        "canonical": False,
        "purpose": purpose,
        "database_schema_version": current_schema_version,
        "record_count": total,
        "table_counts": table_counts,
    }

    def rows_for(table: str):
        columns = [
            row[1] for row in store.connection.execute(f"PRAGMA table_info({table})")
        ]
        for values in store.connection.execute(
            f"SELECT * FROM {table} ORDER BY {export_order[table]}"
        ):
            yield _redacted_row(
                columns, values, table=table, purpose=purpose, redactions=redactions
            )

    if format_name == "json":
        tables = {table: list(rows_for(table)) for table in export_tables}
        return _json_bytes({**header, "tables": tables})

    def jsonl_lines():
        yield _json_bytes({**header, "kind": "metadata"})
        for table in export_tables:
            for row in rows_for(table):
                yield _json_bytes(
                    {
                        "schema": "league.export.v1",
                        "canonical": False,
                        "purpose": purpose,
                        "kind": "row",
                        "table": table,
                        "record": row,
                    }
                )

    return b"".join(jsonl_lines())
