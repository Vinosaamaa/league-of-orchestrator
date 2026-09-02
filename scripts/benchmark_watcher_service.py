#!/usr/bin/env python3
"""Measure the synthetic multi-Squad watcher IPC fast path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import tempfile
import threading
import time
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.persistent_supervisor import (  # noqa: E402
    PersistentSupervisor,
    send_supervisor_message,
)
from lifecycle_fakes import FakeDeliveryAdapter  # noqa: E402
from test_multisquad_supervisor import (  # noqa: E402
    CountingRuntimeObserver,
    FakeWakeAdapter,
    _multisquad_state,
)


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(values: Sequence[float]) -> dict[str, float]:
    return {
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "minimum_ms": round(min(values), 3),
        "maximum_ms": round(max(values), 3),
    }


def _measure(locator: str, message: dict[str, Any], samples: int) -> list[float]:
    values: list[float] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        response = send_supervisor_message(locator, message)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        if response.get("ok") is not True:
            raise RuntimeError("watcher benchmark received a refused response")
        values.append(elapsed)
    return values


def run(samples: int) -> dict[str, Any]:
    if not 10 <= samples <= 5_000:
        raise ValueError("samples must be between 10 and 5000")
    with tempfile.TemporaryDirectory(prefix="league-watcher-benchmark-") as temporary:
        state, store = _multisquad_state(Path(temporary), "state")
        store.close()
        runtime = PersistentSupervisor(
            state,
            lease_seconds=10,
            renew_seconds=3,
            recovery_seconds=30,
            wake_adapter=FakeWakeAdapter(),
            delivery_adapter=FakeDeliveryAdapter(),
            runtime_observer=CountingRuntimeObserver(),
        )
        errors: list[BaseException] = []

        def supervise() -> None:
            try:
                runtime.run(emit_ready=False)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        thread = threading.Thread(target=supervise, name="synthetic-watcher-benchmark")
        thread.start()
        if not runtime.ready.wait(timeout=5):
            raise RuntimeError(f"watcher benchmark service failed to start: {errors!r}")
        locator = f"unix:{runtime.socket_path}"
        try:
            service_message = {"kind": "service-ping"}
            targeted_message = {
                "kind": "ping",
                "actor_agent_id": "11111111-1111-4111-8111-111111111111",
            }
            # Warm both request shapes before collecting samples.
            send_supervisor_message(locator, service_message)
            send_supervisor_message(locator, targeted_message)
            service = _measure(locator, service_message, samples)
            targeted = _measure(locator, targeted_message, samples)
        finally:
            send_supervisor_message(locator, {"kind": "stop"})
            thread.join(timeout=5)
        if thread.is_alive() or errors:
            raise RuntimeError(f"watcher benchmark service did not stop cleanly: {errors!r}")
    return {
        "schema": "league.watcher-service-benchmark.v1",
        "samples_per_operation": samples,
        "active_squad_count": 3,
        "service_ping": _summary(service),
        "targeted_ping": _summary(targeted),
        "service_processes": 1,
        "model_processes": 0,
        "synthetic": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=200)
    args = parser.parse_args()
    print(json.dumps(run(args.samples), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
