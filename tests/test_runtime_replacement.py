#!/usr/bin/env python3
"""Adapter-neutral active Champion replacement and compensation tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.agent_adapters.registry import AgentAdapterRegistry  # noqa: E402
from league.multiplexer_adapters.registry import MultiplexerAdapterRegistry  # noqa: E402
from league.request_services import AssignmentService, AssignmentSpec  # noqa: E402
from league.runtime_replacement import (  # noqa: E402
    RuntimeReplacementService,
    RuntimeReplacementSpec,
)
from league.storage import StorageRefusal  # noqa: E402
from lifecycle_fakes import FakeIds, issue_bound_spec  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    LUX_ID,
    capture_p100,
    create_context,
    dispatch_request,
)
from storage_fixture import SHOTCALLER_ID  # noqa: E402


class InitialLaunch:
    def __init__(self, runtime_kind: str, provider_kind: str) -> None:
        self.runtime_kind = runtime_kind
        self.provider_kind = provider_kind

    def launch(self, spec: AssignmentSpec) -> dict[str, Any]:
        return {
            "verified": True,
            "assignment_id": spec.assignment_id,
            "task_id": spec.task_id,
            "champion_agent_id": spec.champion_agent_id,
            "callsign": spec.callsign,
            "runtime_instance_id": f"runtime:{spec.champion_agent_id}",
            "thread_id": f"session:{self.runtime_kind}:{spec.champion_agent_id}",
            "endpoint": f"terminal:{self.runtime_kind}:{spec.champion_agent_id}",
            "runtime_generation": f"generation:{self.runtime_kind}:1",
            "harness_kind": f"{self.runtime_kind}-thread",
            "backend_kind": "herdr",
            "routing_name": str(spec.callsign).lower(),
            "display_agent": self.provider_kind,
            "repository": spec.repository,
            "issue": spec.issue,
            "branch": spec.branch,
            "worktree": spec.worktree,
            "capabilities": list(spec.required_capabilities),
        }


class ReplacementDriver:
    def __init__(self, owner: "FakeAgentAdapter", launch: Mapping[str, Any]) -> None:
        self.owner = owner
        self.launch_inputs = dict(launch)
        self.created_endpoint = False
        self.launch_receipt: dict[str, Any] | None = None

    def launch(self, spec: AssignmentSpec) -> dict[str, Any]:
        self.owner.launches += 1
        if self.owner.launch_fails:
            raise RuntimeError("synthetic successor launch failure")
        self.created_endpoint = True
        target = {
            "agent_id": spec.champion_agent_id,
            "runtime_instance_id": self.launch_inputs["runtime_instance_id"],
            "session_ref": f"session:{self.owner.kind}:{spec.champion_agent_id}",
            "endpoint": f"terminal:{self.owner.kind}:{spec.champion_agent_id}",
            "runtime_generation": f"generation:{self.owner.kind}:2",
            "cwd": spec.worktree,
            "routing_name": str(spec.routing_name),
            "provider_kind": self.launch_inputs["provider_kind"],
            "adapter_kind": self.owner.kind,
        }
        self.owner.multiplexer.native[spec.champion_agent_id] = target
        self.launch_receipt = {
            "verified": True,
            "assignment_id": spec.assignment_id,
            "task_id": spec.task_id,
            "champion_agent_id": spec.champion_agent_id,
            "callsign": spec.callsign,
            "runtime_instance_id": target["runtime_instance_id"],
            "thread_id": target["session_ref"],
            "endpoint": target["endpoint"],
            "runtime_generation": target["runtime_generation"],
            "harness_kind": f"{self.owner.kind}-thread",
            "backend_kind": "herdr",
            "routing_name": target["routing_name"],
            "display_agent": target["provider_kind"],
            "provider_kind": target["provider_kind"],
            "repository": spec.repository,
            "issue": spec.issue,
            "branch": spec.branch,
            "worktree": spec.worktree,
            "capabilities": list(spec.required_capabilities),
        }
        return dict(self.launch_receipt)

    def cleanup(self, _receipt: Mapping[str, Any] | None) -> bool:
        agent_id = self.launch_inputs["champion_agent_id"]
        self.owner.multiplexer.native.pop(agent_id, None)
        return True


class FakeAgentAdapter:
    def __init__(self, kind: str, providers: frozenset[str]) -> None:
        self.kind = kind
        self.contract = SimpleNamespace(kind=kind, capabilities=frozenset({"create", "exit"}))
        self.lifecycle_operations = frozenset({"replacement"})
        self.provider_kinds = providers
        self.hook_profile = {}
        self.visible_launch_factory = self.visible_launch
        self.delivery_handler = lambda **_inputs: None
        self.presentation_factory = lambda **inputs: inputs
        self.native_events = {}
        self.multiplexer: FakeMultiplexer
        self.launches = 0
        self.launch_fails = False

    def accepts_provider(self, provider_kind: str) -> bool:
        return provider_kind in self.provider_kinds

    def visible_launch(self, **inputs: Any) -> ReplacementDriver:
        return ReplacementDriver(self, inputs["launch"])

    def verify_replacement(
        self, *, target: Mapping[str, Any], multiplexer: "FakeMultiplexer"
    ) -> Mapping[str, Any]:
        return multiplexer.replacement_verify(
            adapter_kind=self.kind,
            provider_kind=target["provider_kind"],
            process_names=frozenset({self.kind}),
            target=target,
        )

    def retire_replacement(
        self,
        *,
        operation_id: str,
        target: Mapping[str, Any],
        multiplexer: "FakeMultiplexer",
    ) -> Mapping[str, Any]:
        verification = self.verify_replacement(target=target, multiplexer=multiplexer)
        return multiplexer.replacement_retire(
            operation_id=operation_id,
            adapter_kind=self.kind,
            provider_kind=target["provider_kind"],
            process_names=frozenset({self.kind}),
            exit_prompt="exit",
            target=target,
            verification=verification,
        )

    # Registry validation only requires advertised methods.  These hooks keep
    # this fixture deliberately narrower than a production lifecycle adapter.
    def canonical_presentation(self, **inputs: Any) -> Mapping[str, Any]:
        return inputs


class FakeMultiplexer:
    kind = "herdr"
    capabilities = frozenset({"runtime_replacement"})

    def __init__(self) -> None:
        self.native: dict[str, dict[str, Any]] = {}
        self.route_swaps = 0
        self.route_rollbacks = 0
        self.retirements = 0
        self.fail_retirement = False

    def replacement_verify(self, **inputs: Any) -> Mapping[str, Any]:
        target = dict(inputs["target"])
        native = self.native.get(str(target["agent_id"]))
        exact = native is not None and all(
            native.get(key) == target.get(key)
            for key in (
                "agent_id",
                "runtime_instance_id",
                "session_ref",
                "endpoint",
                "runtime_generation",
                "cwd",
                "routing_name",
                "provider_kind",
                "adapter_kind",
            )
        )
        if not exact:
            raise StorageRefusal(
                "runtime_replacement_identity_mismatch",
                "synthetic native identity changed",
            )
        return {"verified": True, **native}

    def replacement_route_swap(self, **inputs: Any) -> Mapping[str, Any]:
        predecessor = self.native[str(inputs["predecessor"]["agent_id"])]
        successor = self.native[str(inputs["successor"]["agent_id"])]
        predecessor_staging = f"retired_{inputs['operation_id'].split(':')[-1]}"
        predecessor["routing_name"] = predecessor_staging
        successor["routing_name"] = inputs["predecessor"]["routing_name"]
        self.route_swaps += 1
        return {
            "verified": True,
            "operation_id": inputs["operation_id"],
            "predecessor_agent_id": predecessor["agent_id"],
            "successor_agent_id": successor["agent_id"],
            "canonical_routing_name": successor["routing_name"],
            "successor_previous_routing_name": inputs["successor"]["routing_name"],
            "predecessor_staging_routing_name": predecessor_staging,
        }

    def replacement_route_rollback(self, **inputs: Any) -> Mapping[str, Any]:
        receipt = inputs["route_receipt"]
        predecessor = self.native[receipt["predecessor_agent_id"]]
        successor = self.native[receipt["successor_agent_id"]]
        predecessor["routing_name"] = receipt["canonical_routing_name"]
        successor["routing_name"] = receipt["successor_previous_routing_name"]
        self.route_rollbacks += 1
        return {"verified": True}

    def replacement_retire(self, **inputs: Any) -> Mapping[str, Any]:
        target = dict(inputs["target"])
        if self.fail_retirement and self.retirements == 0:
            self.retirements += 1
            raise RuntimeError("synthetic predecessor retirement failure")
        self.native.pop(str(target["agent_id"]), None)
        self.retirements += 1
        return {
            "verified": True,
            "operation_id": inputs["operation_id"],
            "agent_id": target["agent_id"],
            "runtime_instance_id": target["runtime_instance_id"],
            "session_ref": target["session_ref"],
            "endpoint": target["endpoint"],
            "runtime_generation": target["runtime_generation"],
            "state": "retired",
        }


def refused(operation: Any, code: str) -> None:
    try:
        operation()
    except StorageRefusal as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected refusal {code}")


def active_fixture(root: Path, runtime_kind: str, provider_kind: str):
    _, store, clock = create_context(root, f"replacement-{runtime_kind}-{provider_kind}")
    capture_p100(store, clock)
    store.claim_request("R3", GAREN_RUNTIME, "claim-r3", clock.after(120), clock.now())
    dispatch_request(store, clock, "R3", "claim-r3", "dispatch-r3", "repository-write", "champion")
    worktree = root / "synthetic-worktree"
    worktree.mkdir(exist_ok=True)
    spec = AssignmentSpec(
        assignment_id="assignment:replacement",
        request_id="R3",
        claim_token="claim-r3",
        task_id="task:replacement",
        task_summary="Replace synthetic runtime",
        coordinator_agent_id=SHOTCALLER_ID,
        champion_agent_id=LUX_ID,
        repository="https://example.invalid/league.git",
        issue=84,
        branch="agent/synthetic/replacement",
        worktree=str(worktree),
        issue_receipt=None,
    )
    active = AssignmentService(
        store, InitialLaunch(runtime_kind, provider_kind), clock, FakeIds()
    ).assign(issue_bound_spec(store, spec, clock.now()))
    assert active["state"] == "active", active
    assignment = store.connection.execute(
        "SELECT * FROM task_assignments WHERE task_assignment_id=?", (spec.assignment_id,)
    ).fetchone()
    agent = store.connection.execute(
        "SELECT * FROM agent_instances WHERE agent_id=?", (LUX_ID,)
    ).fetchone()
    task = store.connection.execute(
        "SELECT * FROM tasks WHERE task_id=?", (spec.task_id,)
    ).fetchone()
    assert assignment is not None and agent is not None and task is not None
    return store, clock, dict(assignment), dict(agent), dict(task), worktree


def replacement_spec(
    assignment: Mapping[str, Any],
    agent: Mapping[str, Any],
    task: Mapping[str, Any],
    worktree: Path,
    successor_kind: str,
    successor_provider: str,
    suffix: str,
) -> RuntimeReplacementSpec:
    successor_agent = f"successor-{suffix}"
    request = {
        "schema": "league.runtime-replacement-request.v1",
        "operation_id": f"replacement:{suffix}",
        "assignment_id": assignment["task_assignment_id"],
        "predecessor_agent_id": agent["agent_id"],
        "predecessor_runtime_instance_id": assignment["runtime_instance_id"],
        "successor_agent_id": successor_agent,
        "successor_runtime_instance_id": f"runtime:{successor_agent}",
        "successor_adapter_kind": successor_kind,
        "successor_harness_kind": f"{successor_kind}-thread",
        "successor_provider_kind": successor_provider,
        "multiplexer_kind": "herdr",
        "canonical_routing_name": agent["routing_name"],
        "staging_routing_name": f"staging_{suffix}",
        "routing_decision_id": "routing:synthetic",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "expected_assignment_version": assignment["version"],
        "expected_agent_version": agent["version"],
        "expected_task_version": task["version"],
    }
    launch_inputs = {
        "runtime_instance_id": request["successor_runtime_instance_id"],
        "champion_agent_id": successor_agent,
    }
    return RuntimeReplacementSpec(
        request=request,
        launch_options=SimpleNamespace(
            workspace_id="w1",
            task_label="Runtime Replace",
            model="gpt-5.6-sol",
            effort="high",
        ),
        launch_inputs=launch_inputs,
    )


def service_fixture(store: Any, clock: Any, mux: FakeMultiplexer):
    agents = AgentAdapterRegistry()
    codex = FakeAgentAdapter("codex", frozenset({"codex"}))
    pi = FakeAgentAdapter("pi", frozenset({"codex", "cursor"}))
    for adapter in (codex, pi):
        adapter.multiplexer = mux
        agents._adapters[adapter.kind] = adapter
    multiplexers = MultiplexerAdapterRegistry()
    multiplexers.register(mux)
    return RuntimeReplacementService(store, agents, multiplexers, clock), codex, pi


def test_adapter_neutral_replacement_matrix_and_exact_retry(root: Path) -> None:
    pairs = (
        ("codex", "codex", "pi", "codex", "codex-to-pi"),
        ("pi", "codex", "codex", "codex", "pi-to-codex"),
        ("pi", "cursor", "pi", "codex", "cursor-to-codex"),
        ("pi", "codex", "pi", "cursor", "codex-to-cursor"),
    )
    for old_kind, old_provider, new_kind, new_provider, suffix in pairs:
        case_root = root / suffix
        case_root.mkdir(parents=True)
        store, clock, assignment, agent, task, worktree = active_fixture(
            case_root, old_kind, old_provider
        )
        mux = FakeMultiplexer()
        mux.native[LUX_ID] = {
            "agent_id": LUX_ID,
            "runtime_instance_id": assignment["runtime_instance_id"],
            "session_ref": agent["thread_id"],
            "endpoint": agent["address"],
            "runtime_generation": f"generation:{old_kind}:1",
            "cwd": agent["worktree"],
            "routing_name": agent["routing_name"],
            "provider_kind": old_provider,
            "adapter_kind": old_kind,
        }
        service, codex, pi = service_fixture(store, clock, mux)
        spec = replacement_spec(
            assignment, agent, task, worktree, new_kind, new_provider, suffix
        )
        result = service.replace(spec)
        assert result["state"] == "completed", (suffix, result)
        successor = spec.request["successor_agent_id"]
        canonical = store.connection.execute(
            "SELECT champion_agent_id,runtime_instance_id FROM task_assignments WHERE task_assignment_id=?",
            (assignment["task_assignment_id"],),
        ).fetchone()
        assert tuple(canonical) == (
            successor,
            spec.request["successor_runtime_instance_id"],
        )
        assert LUX_ID not in mux.native
        assert mux.native[successor]["routing_name"] == agent["routing_name"]
        outbox_count = store.connection.execute(
            "SELECT COUNT(*) FROM delivery_outbox WHERE outbox_id=?",
            (result["outbox_id"],),
        ).fetchone()[0]
        assert outbox_count == 1
        retry = service.replace(spec)
        assert retry["state"] == "completed"
        assert (codex.launches + pi.launches) == 1
        assert mux.route_swaps == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM delivery_outbox WHERE outbox_id=?",
            (result["outbox_id"],),
        ).fetchone()[0] == 1
        store.close()


def test_post_switch_retirement_failure_compensates_to_predecessor(root: Path) -> None:
    store, clock, assignment, agent, task, worktree = active_fixture(
        root, "codex", "codex"
    )
    mux = FakeMultiplexer()
    mux.native[LUX_ID] = {
        "agent_id": LUX_ID,
        "runtime_instance_id": assignment["runtime_instance_id"],
        "session_ref": agent["thread_id"],
        "endpoint": agent["address"],
        "runtime_generation": "generation:codex:1",
        "cwd": agent["worktree"],
        "routing_name": agent["routing_name"],
        "provider_kind": "codex",
        "adapter_kind": "codex",
    }
    mux.fail_retirement = True
    service, _, _ = service_fixture(store, clock, mux)
    spec = replacement_spec(assignment, agent, task, worktree, "pi", "cursor", "retire-fail")
    result = service.replace(spec)
    assert result["state"] == "rolled_back", result
    canonical = store.connection.execute(
        "SELECT champion_agent_id,runtime_instance_id FROM task_assignments WHERE task_assignment_id=?",
        (assignment["task_assignment_id"],),
    ).fetchone()
    assert tuple(canonical) == (LUX_ID, assignment["runtime_instance_id"])
    assert mux.native[LUX_ID]["routing_name"] == agent["routing_name"]
    assert spec.request["successor_agent_id"] not in mux.native
    assert mux.route_rollbacks == 1
    assert store.connection.execute(
        "SELECT 1 FROM agent_instances WHERE agent_id=?",
        (spec.request["successor_agent_id"],),
    ).fetchone() is None
    store.close()


def test_unsupported_successor_pair_refuses_before_canonical_write(root: Path) -> None:
    store, clock, assignment, agent, task, worktree = active_fixture(
        root, "codex", "codex"
    )
    mux = FakeMultiplexer()
    service, _, _ = service_fixture(store, clock, mux)
    spec = replacement_spec(assignment, agent, task, worktree, "codex", "cursor", "unsupported")
    refused(lambda: service.replace(spec), "runtime_replacement_adapter_unsupported")
    assert store.connection.execute("SELECT COUNT(*) FROM runtime_replacements").fetchone()[0] == 0
    store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-runtime-replacement-") as temporary:
        root = Path(temporary)
        test_adapter_neutral_replacement_matrix_and_exact_retry(root / "matrix")
        rollback = root / "rollback"
        rollback.mkdir()
        test_post_switch_retirement_failure_compensates_to_predecessor(rollback)
        unsupported = root / "unsupported"
        unsupported.mkdir()
        test_unsupported_successor_pair_refuses_before_canonical_write(unsupported)
    print("PASS: adapter-neutral Champion runtime replacement, retry, and compensation")


if __name__ == "__main__":
    main()
