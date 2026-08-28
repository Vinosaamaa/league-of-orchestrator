#!/usr/bin/env python3
"""Focused indexed typical-day and large-history report latency budgets."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.sqlite_report_ops import (  # noqa: E402
    LARGE_HISTORY_LATENCY_BUDGET_MS,
    TYPICAL_DAY_LATENCY_BUDGET_MS,
)
from league.sqlite_store import SQLiteStorage  # noqa: E402
from storage_test_support import migrated_state  # noqa: E402


def rows(start: int, count: int):
    for index in range(start, start + count):
        yield (
            f"evidence:perf-{index:05d}", "check", "observe", None, None, None, None, None,
            "succeeded", "verified", "Synthetic bounded performance evidence", "outbound_safe",
            None, "a" * 64, None, None, "local_only", None, None, None, None, 0,
            f"2026-08-28T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}Z",
        )


def report(store: SQLiteStorage):
    return store.generate_report(
        from_at="2026-08-28T00:00:00Z", to_at="2026-08-28T23:59:59Z", timezone_name="UTC",
        from_inclusive=True, scope_kind="all", scope_id=None, limit=1000, cursor=None,
        local_diagnostic=False, persist=False,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-report-performance-") as temporary:
        state, _ = migrated_state(Path(temporary), "state")
        with SQLiteStorage(state) as store:
            with store._transaction():
                store.connection.executemany(
                    """
                    INSERT INTO activity_evidence
                      (evidence_id,evidence_kind,action,owner_agent_id,squad_id,project_id,request_id,
                       task_id,state,verification,summary,summary_classification,public_url,object_hash,
                       local_evidence_ref,local_evidence_hash,local_evidence_classification,
                       stable_repair_id,repair_phase,root_cause_tag,owning_issue_url,
                       required_for_completion,occurred_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    rows(0, 2_000),
                )
            started = time.perf_counter()
            typical = report(store)
            typical_ms = (time.perf_counter() - started) * 1000
            assert typical["totals"]["facts"] == 2_000 and typical["pagination"]["next_cursor"]
            assert typical_ms <= TYPICAL_DAY_LATENCY_BUDGET_MS, typical_ms
            with store._transaction():
                store.connection.executemany(
                    """
                    INSERT INTO activity_evidence
                      (evidence_id,evidence_kind,action,owner_agent_id,squad_id,project_id,request_id,
                       task_id,state,verification,summary,summary_classification,public_url,object_hash,
                       local_evidence_ref,local_evidence_hash,local_evidence_classification,
                       stable_repair_id,repair_phase,root_cause_tag,owning_issue_url,
                       required_for_completion,occurred_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    rows(2_000, 48_000),
                )
            started = time.perf_counter()
            large = report(store)
            large_ms = (time.perf_counter() - started) * 1000
            assert large["totals"]["facts"] == 50_000 and large["pagination"]["next_cursor"]
            assert large_ms <= LARGE_HISTORY_LATENCY_BUDGET_MS, large_ms
            indexes = {
                row[0]
                for row in store.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_report_%'"
                )
            }
            assert {
                "ix_report_prompts_created",
                "ix_report_requests_updated",
                "ix_report_tasks_updated",
                "ix_report_assignments_created",
                "ix_report_assignments_updated",
                "ix_report_callsign_reserved",
                "ix_report_callsign_activated",
                "ix_report_callsign_released",
                "ix_report_transitions_created",
                "ix_report_rollovers_updated",
                "ix_report_routing_chosen",
                "ix_report_runtime_created",
                "ix_report_runtime_updated",
                "ix_report_resources_registered",
                "ix_report_resources_updated",
                "ix_report_cleanup_updated",
                "ix_report_cleanup_receipts_recorded",
                "ix_report_teardown_completed",
                "ix_report_outbox_available",
                "ix_report_obligations_created",
            } <= indexes
    print("PASS: indexed typical-day and large-history report budgets")


if __name__ == "__main__":
    main()
