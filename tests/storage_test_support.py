"""Focused setup helpers shared by SQLite storage tests."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from league.cli import main as league_main
from league.importer import build_import_plan
from league.sqlite_store import CURRENT_SCHEMA_VERSION, SQLiteStorage

from storage_fixture import write_complete_fixture


def migrated_state(
    parent: Path,
    name: str,
    *,
    target_version: int = CURRENT_SCHEMA_VERSION,
    request_wal: bool = True,
) -> tuple[Path, dict[str, Any]]:
    state = parent / name
    state.mkdir()
    with SQLiteStorage.for_migration(state, request_wal=request_wal) as store:
        receipt = store.migrate(target_version=target_version)
    return state, receipt


def seeded_state(parent: Path, name: str) -> tuple[Path, Path, dict[str, Any]]:
    root = parent / name
    source = root / "source"
    state = root / "state"
    source.mkdir(parents=True)
    state.mkdir()
    fixture = write_complete_fixture(source)
    with SQLiteStorage.for_migration(state) as store:
        store.migrate()
    with SQLiteStorage(state) as store:
        plan = build_import_plan(
            source, fixture["manifest"], target_counts=store.import_target_counts()
        )
        store.apply_import(plan, plan["report_digest"])
    return source, state, fixture


def invoke_cli(
    state: Path,
    *arguments: str,
    expected: int = 0,
    raw: bool = False,
) -> Any:
    output = io.BytesIO()
    code = league_main(
        ["--state-root", str(state), *arguments],
        output=output,
    )
    assert code == expected, (code, output.getvalue())
    payload = output.getvalue()
    return payload if raw else json.loads(payload)
