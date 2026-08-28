#!/usr/bin/env python3
"""Quality-baseline, override, escalation, and outcome routing tests."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.cli import main as league_main  # noqa: E402
from league.routing import ModelRouter, load_routing_config  # noqa: E402
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from storage_fixture import TASK_ID  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402


AT3 = "2026-01-01T00:02:00Z"
AT4 = "2026-01-01T00:03:00Z"


def config(path: Path, *, fast_approved: bool) -> Path:
    value = {
        "schema": 2,
        "tiers": {
            "COORDINATOR": {"model": "provider/coordinator", "effort": "high"},
            "WORKER_FAST": {"model": "provider/fast", "effort": "medium"},
            "WORKER_STRONG": {"model": "provider/strong", "effort": "xhigh"},
        },
        "evaluations": {
            "WORKER_FAST": {
                "approved": fast_approved,
                "representative_tasks": 12 if fast_approved else 0,
            }
        },
        "policy": {"quality_baseline": "WORKER_STRONG", "safe_boundary_escalations": 1},
    }
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    return path


def test_quality_baseline_overrides_escalation_and_outcome(root: Path) -> None:
    _, state, _ = seeded_state(root, "routing")
    baseline_config = load_routing_config(config(root / "baseline.json", fast_approved=False))
    evaluated_config = load_routing_config(config(root / "evaluated.json", fast_approved=True))
    with SQLiteStorage(state) as store:
        baseline = ModelRouter(baseline_config, store).choose(
            decision_id="route:baseline",
            subject_kind="task",
            subject_id=TASK_ID,
            role="champion",
            profile="bounded",
            chosen_at=AT3,
        )
        assert baseline["tier"] == "WORKER_STRONG"
        assert "No approved representative downgrade evidence" in baseline["reason"]

        router = ModelRouter(evaluated_config, store)
        explicit = router.choose(
            decision_id="route:explicit",
            subject_kind="task",
            subject_id="synthetic-explicit",
            role="champion",
            profile="bounded",
            chosen_at=AT3,
            explicit_model="user/model-exact",
            explicit_effort="ultra",
        )
        assert explicit["tier"] == "WORKER_FAST"
        assert explicit["model"] == "user/model-exact" and explicit["effort"] == "ultra"
        escalated = router.escalate(
            decision_id="route:escalated",
            prior_decision_id="route:explicit",
            failure_class="failed_acceptance",
            chosen_at=AT4,
        )
        assert escalated["state"] == "escalated" and escalated["tier"] == "WORKER_STRONG"
        assert escalated["model"] == "user/model-exact" and escalated["effort"] == "ultra"
        blocked = router.escalate(
            decision_id="route:blocked",
            prior_decision_id="route:escalated",
            failure_class="conflicting_results",
            chosen_at=AT4,
        )
        assert blocked["state"] == "blocked" and blocked["escalation_count"] == 1
        try:
            router.escalate(
                decision_id="route:unsupported",
                prior_decision_id="route:explicit",
                failure_class="preference",
                chosen_at=AT4,
            )
        except StorageRefusal as exc:
            assert exc.code == "escalation_not_evidenced"
        else:
            raise AssertionError("unevidenced escalation was accepted")
        outcome = router.record_outcome(
            outcome_id="outcome:explicit",
            decision_id="route:escalated",
            success=True,
            corrections=1,
            latency_ms=1250,
            cost_microunits=42,
            recorded_at=AT4,
        )
        assert outcome["success"] == 1 and outcome["corrections"] == 1


def test_stable_cli_and_provider_names_stay_in_config(root: Path) -> None:
    _, state, _ = seeded_state(root, "cli")
    config_path = config(root / "cli-routing.json", fast_approved=True)
    import io

    output = io.BytesIO()
    code = league_main(
        [
            "--state-root",
            str(state),
            "routing",
            "choose",
            "--config",
            str(config_path),
            "--decision-id",
            "route:cli",
            "--subject-kind",
            "task",
            "--subject-id",
            TASK_ID,
            "--role",
            "champion",
            "--profile",
            "bounded",
            "--at",
            AT3,
        ],
        output=output,
    )
    payload = json.loads(output.getvalue())
    assert code == 0 and payload["result"]["tier"] == "WORKER_FAST"
    for path in (ROOT / "src/league/routing.py", ROOT / "src/league/runtime.py", ROOT / "src/league/cleanup.py"):
        source = path.read_text(encoding="utf-8").lower()
        assert "gpt-" not in source and "openai" not in source and "anthropic" not in source


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-model-routing-") as temporary:
        root = Path(temporary)
        test_quality_baseline_overrides_escalation_and_outcome(root)
        test_stable_cli_and_provider_names_stay_in_config(root)
    print("PASS: quality baseline, exact overrides, one safe escalation, role outcomes, and assignment-neutral routing API")


if __name__ == "__main__":
    main()
