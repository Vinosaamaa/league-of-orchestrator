#!/usr/bin/env python3
"""Run the bounded filesystem/SQLite decision benchmark with synthetic state."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import resource
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from sqlite_store import SQLiteStorePrototype


AT = "2026-08-28T00:00:00Z"
REPOSITORY = "https://example.invalid/league/storage-benchmark"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".next")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class FilesystemRosterFixture:
    """Synthetic form of the overlapping snapshot plus JSONL operations."""

    def __init__(self, root: Path, agents: int) -> None:
        self.root = root
        for number in range(agents):
            record = self.root / f"agent-{number:03d}"
            record.mkdir(parents=True)
            status = {
                "agent_id": f"agent-{number:03d}",
                "status": "working",
                "version": 1,
                "updated_at": AT,
                "update": "reserved",
            }
            _atomic_json(record / "status.json", status)
            with (record / "updates.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "event_id": f"agent:{number:03d}:1",
                            "version": 1,
                            "status": "working",
                            "update": "reserved",
                            "at": AT,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )

    def transition(self, number: int, expected_version: int) -> None:
        record = self.root / f"agent-{number:03d}"
        log_path = record / "updates.jsonl"
        with log_path.open("a+", encoding="utf-8") as log:
            fcntl.flock(log.fileno(), fcntl.LOCK_EX)
            snapshot = json.loads((record / "status.json").read_text(encoding="utf-8"))
            if snapshot["version"] != expected_version:
                raise RuntimeError("filesystem transition precondition failed")
            next_version = expected_version + 1
            event = {
                "event_id": f"agent:{number:03d}:{next_version}",
                "version": next_version,
                "status": "working",
                "update": f"synthetic-{next_version}",
                "at": AT,
            }
            log.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            log.flush()
            os.fsync(log.fileno())
            snapshot.update(
                version=next_version,
                updated_at=AT,
                update=event["update"],
            )
            _atomic_json(record / "status.json", snapshot)
            fcntl.flock(log.fileno(), fcntl.LOCK_UN)

    def snapshot(self, number: int) -> dict[str, Any]:
        return json.loads(
            (self.root / f"agent-{number:03d}" / "status.json").read_text(
                encoding="utf-8"
            )
        )


def _peak_rss_mib() -> float:
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return raw / (1024 * 1024) if platform.system() == "Darwin" else raw / 1024


def _measure(operation: Callable[[], int]) -> dict[str, float]:
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    operations = operation()
    wall_seconds = time.perf_counter() - wall_start
    cpu_seconds = time.process_time() - cpu_start
    return {
        "operations": float(operations),
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "operations_per_second": operations / wall_seconds,
        "process_peak_rss_mib": _peak_rss_mib(),
    }


def _filesystem_iteration(root: Path, agents: int, transitions: int) -> dict[str, float]:
    fixture = FilesystemRosterFixture(root, agents)

    def work() -> int:
        for version in range(1, transitions + 1):
            for number in range(agents):
                fixture.transition(number, version)
        for number in range(agents):
            snapshot = fixture.snapshot(number)
            if snapshot["version"] != transitions + 1:
                raise RuntimeError("filesystem snapshot/event parity failed")
        return agents * transitions + agents

    return _measure(work)


def _sqlite_iteration(root: Path, agents: int, transitions: int) -> tuple[dict[str, float], str]:
    database = root / "prototype.sqlite3"
    with SQLiteStorePrototype(database) as store:
        store.create_project("project-1", REPOSITORY, AT)
        for number in range(agents):
            task_id = f"task-{number:03d}"
            callsign = f"Callsign{number:03d}"
            agent_id = f"agent-{number:03d}"
            store.create_task(task_id, "project-1", "synthetic task", AT)
            store.add_callsign(callsign)
            store.reserve_callsign(callsign, agent_id, task_id, AT)

        def work() -> int:
            for version in range(1, transitions + 1):
                for number in range(agents):
                    store.transition(
                        f"agent-{number:03d}",
                        version,
                        "working",
                        f"synthetic-{version + 1}",
                        AT,
                    )
            for number in range(agents):
                snapshot = store.agent_snapshot(f"agent-{number:03d}")
                if snapshot is None or snapshot["version"] != transitions + 1:
                    raise RuntimeError("SQLite current/event parity failed")
            return agents * transitions + agents

        measurement = _measure(work)
        if not store.integrity()["ok"]:
            raise RuntimeError("SQLite integrity validation failed")
        return measurement, store.policy.journal_mode


def _summarize(samples: list[dict[str, float]]) -> dict[str, Any]:
    wall = sorted(sample["wall_seconds"] for sample in samples)
    cpu = sorted(sample["cpu_seconds"] for sample in samples)
    throughput = sorted(sample["operations_per_second"] for sample in samples)
    p95_index = min(len(wall) - 1, int(len(wall) * 0.95))
    return {
        "operations_per_repetition": int(samples[0]["operations"]),
        "wall_seconds_median": round(statistics.median(wall), 6),
        "wall_seconds_p95": round(wall[p95_index], 6),
        "cpu_seconds_median": round(statistics.median(cpu), 6),
        "operations_per_second_median": round(statistics.median(throughput), 2),
        "process_peak_rss_mib": round(max(sample["process_peak_rss_mib"] for sample in samples), 2),
    }


def run_benchmark(agents: int, transitions: int, repetitions: int) -> dict[str, Any]:
    if not (1 <= agents <= 256 and 1 <= transitions <= 64 and 1 <= repetitions <= 20):
        raise ValueError("benchmark bounds exceeded")
    filesystem_samples: list[dict[str, float]] = []
    sqlite_samples: list[dict[str, float]] = []
    journal_modes: set[str] = set()
    for _ in range(repetitions):
        with tempfile.TemporaryDirectory(prefix="league-json-benchmark-") as temporary:
            filesystem_samples.append(
                _filesystem_iteration(Path(temporary), agents, transitions)
            )
        with tempfile.TemporaryDirectory(prefix="league-sqlite-benchmark-") as temporary:
            sample, journal_mode = _sqlite_iteration(Path(temporary), agents, transitions)
            sqlite_samples.append(sample)
            journal_modes.add(journal_mode)
    return {
        "schema": 1,
        "scope": "bounded synthetic decision evidence; not a production SLA",
        "runtime": {
            "python": platform.python_version(),
            "sqlite_loaded": sqlite3.sqlite_version,
            "journal_mode": sorted(journal_modes)[0]
            if len(journal_modes) == 1
            else sorted(journal_modes),
        },
        "workload": {
            "agents": agents,
            "transitions_per_agent": transitions,
            "current_state_reads_per_agent": 1,
            "repetitions": repetitions,
        },
        "filesystem_json_jsonl": _summarize(filesystem_samples),
        "sqlite": _summarize(sqlite_samples),
        "correctness": {
            "synthetic_only": True,
            "snapshot_event_parity": True,
            "sqlite_integrity_check": "ok",
            "sqlite_foreign_key_violations": 0,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=32)
    parser.add_argument("--transitions", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    print(
        json.dumps(
            run_benchmark(args.agents, args.transitions, args.repetitions),
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
