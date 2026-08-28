#!/usr/bin/env python3
"""Table-driven owner/execution routing policy coverage."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.orchestration import (  # noqa: E402
    OrchestrationSignals,
    SquadCandidate,
    decide_orchestration_route,
)
from league.storage import StorageRefusal  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    JARVAN_ID,
    JARVAN_RUNTIME,
    create_context,
)
from storage_fixture import SHOTCALLER_ID  # noqa: E402


def test_corpus() -> None:
    corpus = json.loads(
        (ROOT / "tests" / "fixtures" / "routing_decision_corpus.json").read_text()
    )
    assert corpus["schema"] == "league.routing-decision-corpus.v1"
    for case in corpus["orchestration"]:
        candidates = tuple(
            SquadCandidate(
                squad_id=value["squad_id"],
                strong_match=value["strong_match"],
                accepting=value["accepting"],
                owner_live=value["owner_live"],
                capabilities=frozenset(value.get("capabilities", [])),
            )
            for value in case.get("candidates", [])
        )
        try:
            decision = decide_orchestration_route(
                OrchestrationSignals(**case["signals"]),
                explicit_squad_id=case.get("explicit_squad_id"),
                continuation_squad_id=case.get("continuation_squad_id"),
                candidates=candidates,
                required_capabilities=tuple(case.get("required_capabilities", [])),
                cross_project=case.get("cross_project", False),
            )
        except StorageRefusal as exc:
            assert exc.code == case.get("refusal"), case["name"]
            continue
        assert "refusal" not in case, case["name"]
        assert [decision.route, decision.reason_code, decision.target] == case["expected"], case[
            "name"
        ]


def test_hidden_scientist_is_separate_from_direct_tiny() -> None:
    scientist = OrchestrationSignals(
        pre_bounded=True,
        read_only=True,
        answer_or_routing_only=False,
        expected_minutes=3,
        expected_task_action_calls=2,
        hidden_advisory=True,
    )
    assert scientist.hidden_scientist() is True
    assert scientist.direct_tiny() is False
    unsafe = OrchestrationSignals(
        pre_bounded=True,
        read_only=True,
        answer_or_routing_only=False,
        expected_minutes=3,
        expected_task_action_calls=2,
        runs_tests=True,
        hidden_advisory=True,
    )
    assert unsafe.hidden_scientist() is False


def test_registered_project_route_requires_squad_and_runtime_capability(root: Path) -> None:
    _, store, clock = create_context(root, "routing-project-capability")
    try:
        store.register_squad(
            registration_id="registration:routing",
            squad_id="squad:Routing",
            requester_agent_id=SHOTCALLER_ID,
            shotcaller_agent_id=JARVAN_ID,
            runtime_instance_id=JARVAN_RUNTIME,
            project_ids=(),
            capabilities=("request.route",),
            expires_at=clock.after(600),
            event_id="event:registration:routing",
            outbox_id="outbox:registration:routing",
            at=clock.now(),
        )
        store.accept_squad(
            registration_id="registration:routing",
            shotcaller_agent_id=JARVAN_ID,
            runtime_instance_id=JARVAN_RUNTIME,
            decision="accept",
            event_id="event:accept:routing",
            outbox_id="outbox:accept:routing",
            at=clock.now(),
        )
        project = store.put_project(
            "project:routing",
            expected_version=0,
            summary="Synthetic routing project",
            repository="https://example.invalid/synthetic/routing.git",
            root="/synthetic/routing",
            code="routing",
            aliases=(),
            state="active",
            repository_visibility="private",
            export_policy="deny",
            at=clock.now(),
        )
        store.set_project_suggestions(
            "project:routing", project["version"], ("squad:Routing",), clock.now()
        )
        signals = OrchestrationSignals(False, False, False, 30, 4)
        try:
            store.orchestration_decision(
                signals,
                project_ids=("project:routing",),
                explicit_squad_id="squad:Routing",
                required_capabilities=("request.route",),
            )
        except StorageRefusal as exc:
            assert exc.code == "owner_capability_mismatch"
        else:
            raise AssertionError("Squad declaration substituted for live runtime capability")
        with store._transaction():
            store.connection.execute(
                "UPDATE runtime_instances SET capabilities_json='[\"request.route\"]' WHERE runtime_instance_id=?",
                (JARVAN_RUNTIME,),
            )
        decision = store.orchestration_decision(
            signals,
            project_ids=("project:routing",),
            required_capabilities=("request.route",),
        )
        assert decision["route"] == "squad_route"
        assert decision["reason_code"] == "unique_strong_squad"
    finally:
        store.close()


def main() -> None:
    test_corpus()
    test_hidden_scientist_is_separate_from_direct_tiny()
    with tempfile.TemporaryDirectory(prefix="league-routing-policy-") as temporary:
        test_registered_project_route_requires_squad_and_runtime_capability(Path(temporary))
    print("PASS: deterministic owner routing, exact direct bounds, and hidden scientist separation")


if __name__ == "__main__":
    main()
