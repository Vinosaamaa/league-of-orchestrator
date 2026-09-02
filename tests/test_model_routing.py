#!/usr/bin/env python3
"""Provider config, semantic signals, overrides, eval gates, and escalation."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.cli import (  # noqa: E402
    _champion_launch_route,
    _parser,
    main as league_main,
)
from league.routing import (  # noqa: E402
    ModelRouter,
    load_routing_config,
    migrate_routing_config,
)
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from storage_fixture import TASK_ID  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402


AT = "2026-08-28T12:00:00-07:00"
AFTER = "2026-08-29T12:00:00-07:00"


def refused(operation, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def config(path: Path, *, evaluated: bool = False, override: bool = False) -> Path:
    value = {
        "schema": 3,
        "policy_version": "test-policy.v3",
        "default_provider": "alpha",
        "provider_order": ["alpha", "beta"],
        "providers": {
            "alpha": {
                "config_version": "alpha.v1",
                "capabilities": ["reasoning"],
                "tiers": {
                    "COORDINATOR": {"model": "alpha/coordinator", "effort": "high"},
                    "WORKER_FAST": {"model": "alpha/fast", "effort": "medium"},
                    "WORKER_STRONG": {"model": "alpha/strong", "effort": "xhigh"},
                },
            },
            "beta": {
                "config_version": "beta.v1",
                "capabilities": ["reasoning", "browser"],
                "tiers": {
                    "COORDINATOR": {"model": "beta/coordinator", "effort": "high"},
                    "WORKER_FAST": {"model": "beta/fast", "effort": "medium"},
                    "WORKER_STRONG": {"model": "beta/strong", "effort": "xhigh"},
                },
            },
        },
        "evaluations": {
            "alpha/WORKER_FAST": {
                "representative_tasks": 24 if evaluated else 2,
                "task_success_rate": 0.98 if evaluated else 0.5,
                "correction_rate": 0.02 if evaluated else 0.5,
                "minimum_representative_tasks": 20,
                "minimum_task_success_rate": 0.95,
                "maximum_correction_rate": 0.05,
            }
        },
        "policy": {"quality_baseline": "WORKER_STRONG", "safe_boundary_escalations": 1},
        "operator_overrides": (
            [
                {
                    "id": "today-sol-xhigh",
                    "provider": "alpha",
                    "model": "alpha/operator-strong",
                    "effort": "xhigh",
                    "roles": ["champion"],
                    "starts_at": "2026-08-28T00:00:00-07:00",
                    "expires_at": "2026-08-29T00:00:00-07:00",
                }
            ]
            if override
            else []
        ),
    }
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    return path


def test_reliability_eval_and_explicit_precedence(root: Path) -> None:
    _, state, _ = seeded_state(root, "model-quality")
    with SQLiteStorage(state) as store:
        baseline = ModelRouter(load_routing_config(config(root / "base.json")), store).choose(
            decision_id="route:baseline",
            subject_kind="task",
            subject_id=TASK_ID,
            role="champion",
            chosen_at=AFTER,
            signals={"bounded_checkable": True},
        )
        assert baseline["tier"] == "WORKER_STRONG"
        assert baseline["reason_code"] == "reliability_baseline"
        assert baseline["provider_config_version"] == "alpha.v1"
        evaluated = ModelRouter(
            load_routing_config(config(root / "evaluated.json", evaluated=True)), store
        ).choose(
            decision_id="route:evaluated",
            subject_kind="task",
            subject_id="bounded",
            role="champion",
            chosen_at=AFTER,
            signals={"bounded_checkable": True},
        )
        assert evaluated["tier"] == "WORKER_FAST"
        assert evaluated["reason_code"] == "evidence_downgrade"
        explicit = ModelRouter(
            load_routing_config(config(root / "explicit.json", evaluated=True)), store
        ).choose(
            decision_id="route:explicit",
            subject_kind="task",
            subject_id="explicit",
            role="champion",
            chosen_at=AT,
            signals={"bounded_checkable": True},
            explicit_provider="beta",
            explicit_model="user/exact",
            explicit_effort="max",
            required_capabilities=("browser",),
        )
        assert (explicit["provider"], explicit["model"], explicit["effort"]) == (
            "beta",
            "user/exact",
            "max",
        )
        assert explicit["reason_code"] == "explicit_override"


def test_model_decision_corpus(root: Path) -> None:
    corpus = json.loads(
        (ROOT / "tests" / "fixtures" / "routing_decision_corpus.json").read_text()
    )
    _, state, _ = seeded_state(root, "model-corpus")
    with SQLiteStorage(state) as store:
        for index, case in enumerate(corpus["model"]):
            routing = load_routing_config(
                config(
                    root / f"corpus-{index}.json",
                    evaluated=case.get("evaluated", False),
                    override=case.get("override", False),
                )
            )
            try:
                decision = ModelRouter(routing, store).choose(
                    decision_id=f"route:corpus:{index}",
                    subject_kind="task",
                    subject_id=f"corpus:{index}",
                    role="champion",
                    chosen_at=case["at"],
                    signals=case["signals"],
                    required_capabilities=tuple(case.get("required_capabilities", [])),
                    explicit_provider=case.get("explicit_provider"),
                    explicit_model=case.get("explicit_model"),
                    explicit_effort=case.get("explicit_effort"),
                )
            except StorageRefusal as exc:
                assert exc.code == case.get("refusal"), case["name"]
                continue
            assert "refusal" not in case, case["name"]
            assert all(decision[key] == value for key, value in case["expected"].items()), case[
                "name"
            ]
            if "escalate_failure" in case:
                escalated = ModelRouter(routing, store).escalate(
                    decision_id=f"route:corpus:{index}:escalated",
                    prior_decision_id=f"route:corpus:{index}",
                    failure_class=case["escalate_failure"],
                    chosen_at=case["at"],
                )
                assert all(
                    escalated[key] == value
                    for key, value in case["escalated_expected"].items()
                ), case["name"]


def test_operator_expiry_capability_fallback_and_risk(root: Path) -> None:
    _, state, _ = seeded_state(root, "model-override")
    routing = load_routing_config(config(root / "override.json", evaluated=True, override=True))
    with SQLiteStorage(state) as store:
        router = ModelRouter(routing, store)
        active = router.choose(
            decision_id="route:override",
            subject_kind="task",
            subject_id="today",
            role="champion",
            chosen_at=AT,
            signals={"bounded_checkable": True},
        )
        assert active["operator_override_id"] == "today-sol-xhigh"
        assert active["reason_code"] == "operator_override"
        expired = router.choose(
            decision_id="route:expired",
            subject_kind="task",
            subject_id="tomorrow",
            role="champion",
            chosen_at=AFTER,
            signals={"bounded_checkable": True},
        )
        assert expired["operator_override_id"] is None and expired["tier"] == "WORKER_FAST"
        fallback = router.choose(
            decision_id="route:fallback",
            subject_kind="task",
            subject_id="browser",
            role="hidden-worker",
            chosen_at=AFTER,
            signals={"bounded_checkable": True},
            required_capabilities=("browser",),
        )
        assert fallback["provider"] == "beta" and fallback["fallback_from_provider"] == "alpha"
        assert fallback["reason_code"] == "provider_capability_fallback"
        risk = router.choose(
            decision_id="route:risk",
            subject_kind="task",
            subject_id="risk",
            role="hidden-worker",
            chosen_at=AFTER,
            signals={"bounded_checkable": True, "high_impact": True},
        )
        assert risk["tier"] == "WORKER_STRONG"


def test_one_concrete_safe_boundary_escalation(root: Path) -> None:
    _, state, _ = seeded_state(root, "model-escalation")
    routing = load_routing_config(config(root / "escalation.json", evaluated=True))
    with SQLiteStorage(state) as store:
        router = ModelRouter(routing, store)
        router.choose(
            decision_id="route:prior",
            subject_kind="task",
            subject_id=TASK_ID,
            role="champion",
            chosen_at=AFTER,
            signals={"bounded_checkable": True},
        )
        escalated = router.escalate(
            decision_id="route:escalated",
            prior_decision_id="route:prior",
            failure_class="failed_acceptance",
            chosen_at=AFTER,
        )
        assert escalated["state"] == "escalated" and escalated["tier"] == "WORKER_STRONG"
        blocked = router.escalate(
            decision_id="route:blocked",
            prior_decision_id="route:escalated",
            failure_class="conflicting_results",
            chosen_at=AFTER,
        )
        assert blocked["state"] == "blocked" and blocked["escalation_count"] == 1
        outcome = router.record_outcome(
            outcome_id="outcome:one",
            decision_id="route:escalated",
            success=True,
            corrections=0,
            latency_ms=500,
            cost_microunits=None,
            recorded_at=AFTER,
        )
        assert outcome["cost_microunits"] is None


def test_explicit_target_pin_blocks_silent_stronger_replacement(root: Path) -> None:
    _, state, _ = seeded_state(root, "model-explicit-escalation")
    routing = load_routing_config(config(root / "explicit-escalation.json", evaluated=True))
    with SQLiteStorage(state) as store:
        router = ModelRouter(routing, store)
        router.choose(
            decision_id="route:explicit-prior",
            subject_kind="task",
            subject_id=TASK_ID,
            role="champion",
            chosen_at=AFTER,
            signals={"bounded_checkable": True},
            explicit_model="user/pinned",
            explicit_effort="medium",
        )
        blocked = router.escalate(
            decision_id="route:explicit-blocked",
            prior_decision_id="route:explicit-prior",
            failure_class="tool_failure",
            chosen_at=AFTER,
        )
        assert blocked["state"] == "blocked"
        assert (blocked["model"], blocked["effort"], blocked["escalation_count"]) == (
            "user/pinned",
            "medium",
            0,
        )


def test_atomic_single_escalation_child(root: Path) -> None:
    _, state, _ = seeded_state(root, "model-race")
    routing = load_routing_config(config(root / "race.json", evaluated=True))
    with SQLiteStorage(state) as store:
        ModelRouter(routing, store).choose(
            decision_id="route:race-prior",
            subject_kind="task",
            subject_id=TASK_ID,
            role="champion",
            chosen_at=AFTER,
            signals={"bounded_checkable": True},
        )
    barrier = threading.Barrier(2)

    def race(decision_id: str) -> str:
        with SQLiteStorage(state) as store:
            barrier.wait()
            try:
                ModelRouter(routing, store).escalate(
                    decision_id=decision_id,
                    prior_decision_id="route:race-prior",
                    failure_class="tool_failure",
                    chosen_at=AFTER,
                )
            except StorageRefusal as exc:
                return exc.code
            return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(race, ("route:race-a", "route:race-b"))) == [
            "created",
            "routing_escalation_conflict",
        ]


def test_cli_and_malformed_config(root: Path) -> None:
    _, state, _ = seeded_state(root, "model-cli")
    config_path = config(root / "cli.json", evaluated=True)
    signals = root / "signals.json"
    signals.write_text('{"bounded_checkable":true}\n', encoding="utf-8")
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
            "--signals",
            str(signals),
            "--at",
            AFTER,
        ],
        output=output,
    )
    assert code == 0 and json.loads(output.getvalue())["result"]["tier"] == "WORKER_FAST"
    malformed = json.loads(config_path.read_text())
    malformed["policy_version"] = ""
    bad = root / "bad.json"
    bad.write_text(json.dumps(malformed), encoding="utf-8")
    try:
        load_routing_config(bad)
    except StorageRefusal as exc:
        assert exc.code == "routing_config_invalid"
    else:
        raise AssertionError("unversioned provider config was accepted")


def _launch_parser_arguments() -> list[str]:
    return [
        "assign",
        "run",
        "--request-id",
        "request:launch-route",
        "--claim-token",
        "claim:launch-route",
        "--task-id",
        TASK_ID,
        "--task-summary",
        "Route one Champion",
        "--coordinator-agent-id",
        "agent:shotcaller",
        "--repository",
        "https://example.invalid/league.git",
        "--branch",
        "agent/test/route",
        "--worktree",
        "/example/worktree",
        "--issue",
        "84",
        "--issue-selection-receipt-digest",
        "a" * 64,
    ]


def test_champion_launch_help_and_pi_codex_defaults() -> None:
    parser = _parser()
    args = parser.parse_args(_launch_parser_arguments())
    assert args.runtime_kind == "pi"
    assert args.provider_kind == "codex"
    assert args.model is None and args.effort is None
    assert args.routing_decision_id is None

    help_output = io.StringIO()
    try:
        with redirect_stdout(help_output):
            parser.parse_args(["assign", "run", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("assign run --help did not exit")
    rendered = help_output.getvalue()
    assert "[--model MODEL]" in rendered
    assert "[--effort EFFORT]" in rendered
    assert "[--routing-decision-id ROUTING_DECISION_ID]" in rendered
    assert "[--runtime-kind RUNTIME_KIND]" in rendered
    assert "[--provider-kind PROVIDER_KIND]" in rendered
    assert "{codex,cursor,pi}" not in rendered


def test_champion_launch_consumes_one_exact_persisted_decision(root: Path) -> None:
    _, state, _ = seeded_state(root, "launch-route")
    routing = load_routing_config(ROOT / "config/league-model-routing.example.json")
    with SQLiteStorage(state) as store:
        decision = ModelRouter(routing, store).choose(
            decision_id="route:launch",
            subject_kind="task",
            subject_id=TASK_ID,
            role="champion",
            chosen_at=AFTER,
            signals={"bounded_checkable": True},
        )
        args = SimpleNamespace(
            model=None,
            effort=None,
            routing_decision_id=decision["decision_id"],
            request_id="request:launch-route",
            task_id=TASK_ID,
            provider_kind="codex",
            requires=[],
        )
        resolved = _champion_launch_route(
            store, args, assignment_id="assignment:launch-route"
        )
        assert (resolved["model"], resolved["effort"], resolved["provider"]) == (
            "gpt-5.6-sol",
            "xhigh",
            "codex",
        )
        assert resolved["decision_id"] == decision["decision_id"]

        args.task_id = "task:wrong"
        refused(
            lambda: _champion_launch_route(
                store, args, assignment_id="assignment:launch-route"
            ),
            "launch_routing_decision_mismatch",
        )
        args.task_id = TASK_ID
        args.provider_kind = "cursor"
        refused(
            lambda: _champion_launch_route(
                store, args, assignment_id="assignment:launch-route"
            ),
            "launch_routing_decision_mismatch",
        )

        args.routing_decision_id = None
        refused(
            lambda: _champion_launch_route(
                store, args, assignment_id="assignment:launch-route"
            ),
            "launch_routing_decision_required",
        )
        args.routing_decision_id = "route:missing"
        refused(
            lambda: _champion_launch_route(
                store, args, assignment_id="assignment:launch-route"
            ),
            "launch_routing_decision_unknown",
        )


def test_explicit_pi_cursor_route_is_exact_without_policy_fallback(root: Path) -> None:
    _, state, _ = seeded_state(root, "launch-cursor")
    parsed = _parser().parse_args(
        _launch_parser_arguments()
        + [
            "--runtime-kind",
            "pi",
            "--provider-kind",
            "cursor",
            "--model",
            "cursor/grok-4.6",
            "--effort",
            "xhigh",
        ]
    )
    assert (parsed.runtime_kind, parsed.provider_kind) == ("pi", "cursor")
    with SQLiteStorage(state) as store:
        resolved = _champion_launch_route(
            store, parsed, assignment_id="assignment:cursor"
        )
        assert (resolved["provider"], resolved["model"], resolved["effort"]) == (
            "cursor",
            "cursor/grok-4.6",
            "xhigh",
        )
        assert resolved["explicit"] == {
            "runtime": True,
            "provider": True,
            "model": True,
            "effort": True,
        }


def test_default_pi_codex_refuses_cursor_decision_without_explicit_provider(
    root: Path,
) -> None:
    _, state, _ = seeded_state(root, "launch-cursor-decision")
    policy = json.loads(
        (ROOT / "config/league-model-routing.example.json").read_text(
            encoding="utf-8"
        )
    )
    policy["default_provider"] = "cursor"
    policy["provider_order"] = ["cursor"]
    policy["providers"] = {"cursor": policy["providers"].pop("openai")}
    policy["evaluations"] = {
        key.replace("openai/", "cursor/"): value
        for key, value in policy["evaluations"].items()
    }
    policy["operator_overrides"] = []
    path = root / "cursor-policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with SQLiteStorage(state) as store:
        decision = ModelRouter(load_routing_config(path), store).choose(
            decision_id="route:cursor-default-refusal",
            subject_kind="task",
            subject_id=TASK_ID,
            role="champion",
            chosen_at=AFTER,
            signals={"bounded_checkable": True},
        )
        default_args = _parser().parse_args(
            _launch_parser_arguments()
            + ["--routing-decision-id", decision["decision_id"]]
        )
        assert default_args.runtime_kind == "pi"
        assert default_args.provider_kind == "codex"
        assert default_args.provider_kind_explicit is False
        refused(
            lambda: _champion_launch_route(
                store, default_args, assignment_id="assignment:cursor-default"
            ),
            "launch_routing_decision_mismatch",
        )
        explicit_args = _parser().parse_args(
            _launch_parser_arguments()
            + [
                "--routing-decision-id",
                decision["decision_id"],
                "--provider-kind",
                "cursor",
            ]
        )
        resolved = _champion_launch_route(
            store, explicit_args, assignment_id="assignment:cursor-explicit"
        )
        assert resolved["provider"] == "cursor"
        assert resolved["explicit"]["provider"] is True


def test_schema_one_migration_is_versioned_and_never_silently_luna(root: Path) -> None:
    legacy = {
        "schema": 1,
        "tiers": {
            "COORDINATOR": {"model": "gpt-5.6-sol", "effort": "high"},
            "WORKER_FAST": {"model": "gpt-5.6-luna", "effort": "medium"},
            "WORKER_STRONG": {"model": "gpt-5.6-sol", "effort": "xhigh"},
        },
        "evaluations": {"WORKER_FAST": {"approved": False}},
        "policy": {
            "quality_baseline": "WORKER_STRONG",
            "safe_boundary_escalations": 1,
        },
    }
    migrated = migrate_routing_config(legacy)
    assert migrated["schema"] == 3
    assert migrated["policy_version"] == "league.model-routing.migrated-v3.1"
    assert migrated["evaluations"]["openai/WORKER_FAST"]["representative_tasks"] == 0
    legacy_path = root / "legacy-routing.json"
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    output = io.BytesIO()
    assert league_main(
        ["routing", "migrate-config", "--config", str(legacy_path)],
        output=output,
    ) == 0
    assert json.loads(output.getvalue())["result"] == migrated

    installed = root / "installed-routing.json"
    installed.write_bytes(legacy_path.read_bytes())
    backup = root / "installed-routing.schema1.backup.json"
    install_output = io.BytesIO()
    assert league_main(
        [
            "routing",
            "migrate-config",
            "--config",
            str(installed),
            "--destination",
            str(installed),
            "--backup",
            str(backup),
        ],
        output=install_output,
    ) == 0
    install = json.loads(install_output.getvalue())["result"]
    assert install["state"] == "installed"
    assert install["from_schema"] == 1 and install["to_schema"] == 3
    assert install["rollback_ready"] and not install["idempotent"]
    assert backup.read_bytes() == legacy_path.read_bytes()
    assert load_routing_config(installed) == migrated

    retry_output = io.BytesIO()
    assert league_main(
        [
            "routing",
            "migrate-config",
            "--config",
            str(installed),
            "--destination",
            str(installed),
            "--backup",
            str(backup),
        ],
        output=retry_output,
    ) == 0
    retry = json.loads(retry_output.getvalue())["result"]
    assert retry["idempotent"] and retry["installed_sha256"] == install["installed_sha256"]

    rollback_output = io.BytesIO()
    assert league_main(
        [
            "routing",
            "rollback-config",
            "--destination",
            str(installed),
            "--backup",
            str(backup),
            "--expected-installed-sha256",
            install["installed_sha256"],
            "--expected-backup-sha256",
            install["backup_sha256"],
        ],
        output=rollback_output,
    ) == 0
    rollback = json.loads(rollback_output.getvalue())["result"]
    assert not rollback["idempotent"]
    assert installed.read_bytes() == legacy_path.read_bytes()
    second_rollback = io.BytesIO()
    assert league_main(
        [
            "routing",
            "rollback-config",
            "--destination",
            str(installed),
            "--backup",
            str(backup),
            "--expected-installed-sha256",
            install["installed_sha256"],
            "--expected-backup-sha256",
            install["backup_sha256"],
        ],
        output=second_rollback,
    ) == 0
    assert json.loads(second_rollback.getvalue())["result"]["idempotent"]

    _, state, _ = seeded_state(root, "migrated-route")
    with SQLiteStorage(state) as store:
        selected = ModelRouter(migrated, store).choose(
            decision_id="route:migrated",
            subject_kind="task",
            subject_id=TASK_ID,
            role="champion",
            chosen_at=AFTER,
            signals={"bounded_checkable": True},
        )
    assert (selected["tier"], selected["model"], selected["effort"]) == (
        "WORKER_STRONG",
        "gpt-5.6-sol",
        "xhigh",
    )

    unsafe = json.loads(json.dumps(legacy))
    unsafe["tiers"]["WORKER_STRONG"]["model"] = "gpt-5.6-luna"
    refused(lambda: migrate_routing_config(unsafe), "routing_migration_unsafe")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-model-routing-") as temporary:
        root = Path(temporary)
        test_model_decision_corpus(root)
        test_reliability_eval_and_explicit_precedence(root)
        test_operator_expiry_capability_fallback_and_risk(root)
        test_one_concrete_safe_boundary_escalation(root)
        test_explicit_target_pin_blocks_silent_stronger_replacement(root)
        test_atomic_single_escalation_child(root)
        test_cli_and_malformed_config(root)
        test_champion_launch_help_and_pi_codex_defaults()
        test_champion_launch_consumes_one_exact_persisted_decision(root)
        test_explicit_pi_cursor_route_is_exact_without_policy_fallback(root)
        test_default_pi_codex_refuses_cursor_decision_without_explicit_provider(root)
        test_schema_one_migration_is_versioned_and_never_silently_luna(root)
    print(
        "PASS: versioned provider policy, Pi/Codex launch defaults, exact decision consumption, "
        "explicit Pi/Cursor routing, safe schema migration, and one escalation"
    )


if __name__ == "__main__":
    main()
