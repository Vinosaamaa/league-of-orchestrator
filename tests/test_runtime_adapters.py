#!/usr/bin/env python3
"""Capability matrix and shared non-Codex runtime lifecycle regressions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.adapter_types import (  # noqa: E402
    BACKEND_CAPABILITIES,
    HARNESS_CAPABILITIES,
    AdapterContract,
    AdapterInstruction,
    OpaqueIdentity,
)
from league.adapters import (
    AdapterRegistry,
    builtin_contract_registry,
    builtin_harness_contracts,
    builtin_registry,
)  # noqa: E402
from league.runtime import RuntimeCreateSpec, RuntimeLifecycle  # noqa: E402
from league.persistent_supervisor import HerdrRuntimeObservationAdapter  # noqa: E402
from league.agent_adapters import (  # noqa: E402
    OPERATION_METHODS,
    SharedLifecyclePolicy,
    builtin_agent_adapter_registry,
)
from league.multiplexer_adapters import (  # noqa: E402
    CommandRunner,
    MULTIPLEXER_OPERATIONS,
    MultiplexerAdapter,
    RestoredEndpoint,
    builtin_multiplexer_adapter_registry,
)
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
from league.visible_launch import VisibleLaunchOptions  # noqa: E402
from runtime_doubles import DeterministicBackend  # noqa: E402
from storage_fixture import CHAMPION_ID, TASK_ID  # noqa: E402
from storage_test_support import seeded_state  # noqa: E402
from storage_test_support import invoke_cli  # noqa: E402


AT3 = "2026-01-01T00:02:00Z"
AT4 = "2026-01-01T00:03:00Z"
AT5 = "2026-01-01T00:04:00Z"


class TitleFailureBackend(DeterministicBackend):
    """Fail after session creation to exercise exact partial-runtime rollback."""

    def input(self, endpoint: OpaqueIdentity, instruction: AdapterInstruction):
        if instruction.operation == "title":
            self.operations.append(("title_failed", endpoint.encoded))
            raise StorageRefusal("synthetic_title_failure", "isolated title failure")
        return super().input(endpoint, instruction)


class HerdrOperationRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments, timeout_seconds=30):
        del timeout_seconds
        command = tuple(arguments)
        self.calls.append(command)
        result = None
        if command[1:3] == ("tab", "create"):
            result = {
                "tab": {"tab_id": "w1:t2"},
                "root_pane": {"pane_id": "w1:p2", "terminal_id": "term:2"},
            }
        elif command[1:3] == ("pane", "split"):
            result = {
                "tab_id": "w1:t1",
                "pane": {"pane_id": "w1:p3", "terminal_id": "term:3"},
            }
        elif command[1:3] == ("agent", "list"):
            result = {"agents": []}
        elif command[1:3] not in {
            ("agent", "prompt"), ("pane", "close"), ("pane", "rename")
        }:
            raise AssertionError(command)
        stdout = "" if result is None else json.dumps({"result": result})
        return subprocess.CompletedProcess(command, 0, stdout, "")


def pi_registry(backend: DeterministicBackend) -> AdapterRegistry:
    registry = AdapterRegistry()
    pi = next(adapter for adapter in builtin_harness_contracts() if adapter.contract.kind == "pi")
    registry.register_harness(pi)
    registry.register_backend(backend)
    return registry


def test_named_compatibility_matrix_and_opaque_identity() -> None:
    matrix = builtin_contract_registry().capability_matrix()
    pairs = {(item["harness"], item["backend"]): item for item in matrix["pairs"]}
    assert pairs[("codex", "herdr")]["operations"]["create"] == "driver_unavailable"
    assert pairs[("codex", "herdr")]["operations"]["resume"] == "driver_unavailable"
    assert pairs[("codex", "tmux")]["operations"]["backend.allocate"] == "unsupported"
    assert pairs[("cursor", "herdr")]["operations"]["resume"] == "driver_unavailable"
    assert pairs[("pi", "herdr")]["evidence"] == "inherited-contract"
    assert pairs[("pi", "herdr")]["operations"]["resume"] == "driver_unavailable"
    assert OpaqueIdentity.decode("codex:not-a-uuid").value == "not-a-uuid"
    try:
        OpaqueIdentity.decode("not-namespaced")
    except StorageRefusal as exc:
        assert exc.code == "invalid_identity"
    else:
        raise AssertionError("unnamespaced core identity was accepted")


def test_runtime_matrix_cli_and_unsupported_create(root: Path) -> None:
    _, state, _ = seeded_state(root, "matrix")
    matrix_command = invoke_cli(state, "runtime", "matrix")
    assert matrix_command["ok"] is True and matrix_command["result"]["pairs"]
    production_pairs = {
        (item["harness"], item["backend"]): item
        for item in matrix_command["result"]["pairs"]
    }
    for provider in ("codex", "cursor", "pi"):
        pair = production_pairs[(provider, "herdr")]
        assert pair["availability"] == "contract-only"
        assert pair["operations"]["create"] == "driver_unavailable"
        assert pair["semantic_availability"] == "operational"
        assert pair["lifecycle_operations"]["launch"] == "supported"
    assert production_pairs[("cursor", "tmux")]["semantic_availability"] == "contract-only"
    assert production_pairs[("cursor", "tmux")]["lifecycle_operations"]["launch"] == "driver_unavailable"
    with SQLiteStorage(state) as store:
        unavailable = RuntimeLifecycle(store, builtin_contract_registry())
        try:
            unavailable.create(
                RuntimeCreateSpec(
                    "binding:unsupported",
                    TASK_ID,
                    "codex",
                    "tmux",
                    "Unsupported fixture",
                    AT3,
                    {},
                    {},
                )
            )
        except StorageRefusal as exc:
            assert exc.code == "unsupported_capability"
        else:
            raise AssertionError("unsupported Codex/tmux allocation was attempted")
        try:
            unavailable.create(
                RuntimeCreateSpec(
                    "binding:driver-unavailable",
                    TASK_ID,
                    "codex",
                    "herdr",
                    "Contract-only fixture",
                    AT3,
                    {},
                    {},
                )
            )
        except StorageRefusal as exc:
            assert exc.code == "runtime_driver_unavailable"
        else:
            raise AssertionError("contract-only Herdr adapter was treated as an operational driver")


def test_adapter_contract_validation() -> None:
    for contract, code in (
        (
            lambda: AdapterContract(
                "bad-evidence",
                "harness",
                HARNESS_CAPABILITIES,
                "claimed-real",
                "available",
                "Invalid fixture.",
            ),
            "adapter_contract_invalid",
        ),
        (
            lambda: AdapterContract(
                "bad-capability",
                "backend",
                frozenset({"resume"}),
                "isolated-double",
                "available",
                "Invalid fixture.",
            ),
            "adapter_contract_invalid",
        ),
    ):
        try:
            contract()
        except StorageRefusal as exc:
            assert exc.code == code
        else:
            raise AssertionError("invalid adapter contract was accepted")


def test_parameterized_provider_lifecycle_event_parity() -> None:
    registry = builtin_agent_adapter_registry()
    policy = SharedLifecyclePolicy()
    cases = (
        ("codex", "UserPromptSubmit", {"session_id": "session:codex", "turn_id": "turn:1"}),
        ("pi", "input", {"session_path": "/tmp/session.jsonl", "input_id": "input:1"}),
        ("cursor", "beforeSubmitPrompt", {"conversation_id": "conversation:1", "generation_id": "generation:1"}),
    )
    for kind, native_event, payload in cases:
        adapter = registry.adapter(kind)
        expected_operations = frozenset({
            "prompt_intake", "pre_tool_authorization", "stop_supervision", "launch",
            "resume", "steer", "title", "delivery", "retirement", "cleanup",
            "replacement",
        })
        assert adapter.lifecycle_operations == expected_operations
        for operation in adapter.lifecycle_operations:
            for method in OPERATION_METHODS[operation]:
                assert callable(getattr(adapter, method, None)), (kind, operation, method)
        event = adapter.translate_event(native_event, payload)
        decision = policy.decide(event)
        assert event.operation == "prompt_intake"
        assert decision == type(decision)("prompt_intake", "accept", "policy_accepted")
    pre_tool_cases = (
        ("codex", "PreToolUse", {"session_id": "session:codex", "turn_id": "turn:2"}),
        ("pi", "tool_call", {"session_path": "/tmp/session.jsonl", "input_id": "input:2"}),
        ("cursor", "beforeShellExecution", {"conversation_id": "conversation:1", "generation_id": "generation:2"}),
    )
    for kind, native_event, payload in pre_tool_cases:
        event = registry.adapter(kind).translate_event(native_event, payload)
        role_refused = policy.decide(event, authorized=True)
        assert role_refused.reason_code == "shotcaller_delegation_unverified"
        refused = policy.decide(
            event, authorized=False, actor_role="shotcaller"
        )
        assert refused.outcome == "refuse" and refused.reason_code == "tool_not_authorized"
        accepted = policy.decide(
            event,
            authorized=True,
            actor_role="champion",
            delegated_by_shotcaller=True,
        )
        assert accepted.outcome == "accept"

    restore_cases = (
        ("codex", "codex", "codex"),
        ("pi", "cursor", "pi"),
        ("pi", "codex", "pi"),
        ("cursor", "cursor", "/opt/provider/cursor-agent"),
    )
    for kind, provider, process in restore_cases:
        descriptor = {
            "agent_adapter_kind": kind,
            "runtime_kind": kind,
            "provider_kind": provider,
            "session_ref": f"session:{kind}:{provider}",
            "cwd": "/tmp/synthetic-worktree",
            "routing_name": f"{kind}-{provider}",
            "metadata_source": f"league:{kind}:{provider}",
            "applies_to_source": f"provider:{kind}",
            "title": f"{kind} fixture",
            "tokens": {"display_provider": provider},
        }
        observation = {
            "agent": {"agent": kind},
            "process": {"argv0": process},
            "session_ref": descriptor["session_ref"],
            "session_source": descriptor["applies_to_source"],
        }
        packet = registry.adapter(kind).restored_presentation(descriptor, observation)
        assert packet["provider_kind"] == provider
        assert set(packet) == {
            "agent_adapter_kind", "provider_kind", "session_ref", "cwd",
            "routing_name", "metadata_source", "applies_to_source", "title", "tokens",
        }


def test_registered_visible_launch_factories_own_provider_selection(root: Path) -> None:
    class SecondMultiplexer:
        kind = "fixture"
        capabilities = frozenset({"visible_launch"})

        def __init__(self) -> None:
            self.calls = []

        def visible_launch_driver(self, agent_kind, **inputs):
            self.calls.append((agent_kind, inputs))
            return SimpleNamespace(
                profile=SimpleNamespace(kind=agent_kind),
                descriptor=inputs.get("descriptor", {}),
            )

    state_root = root / "state"
    state_root.mkdir(parents=True)
    _, state, _ = seeded_state(root, "visible-factory")
    options = VisibleLaunchOptions(
        workspace_id="wsynthetic",
        task_label="Adapter Launch",
        model="gpt-5.6-sol",
        effort="xhigh",
        league_command=str(ROOT / "bin" / "league"),
        state_root=str(state_root),
        routing={"decision_id": "route:synthetic"},
    )
    common = {
        "assignment_id": "assignment:synthetic",
        "task_id": TASK_ID,
        "champion_agent_id": CHAMPION_ID,
        "repository": "https://example.invalid/league.git",
        "issue": 84,
        "branch": "agent/test/synthetic",
        "worktree": str(root),
        "project_code": None,
        "release_root": None,
        "resolved_release_root": str(ROOT),
        "session_path": None,
        "parent_session_id": None,
        "parent_session_path": None,
        "session_id": None,
        "session_mode": "create",
        "workspace_id": "wsynthetic",
        "state_root": str(state_root),
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "routing": {"decision_id": "route:synthetic"},
        "at": AT3,
    }
    multiplexer = SecondMultiplexer()
    with SQLiteStorage(state) as store:
        registry = builtin_agent_adapter_registry()
        codex = registry.adapter("codex").visible_launch(
            store=store,
            options=options,
            multiplexer=multiplexer,
            startup_timeout_ms=120_000,
            launch={**common, "provider_kind": "codex"},
        )
        cursor = registry.adapter("cursor").visible_launch(
            store=store,
            options=options,
            multiplexer=multiplexer,
            startup_timeout_ms=120_000,
            launch={**common, "provider_kind": "cursor"},
        )
        pi = registry.adapter("pi").visible_launch(
            store=store,
            options=options,
            multiplexer=multiplexer,
            startup_timeout_ms=120_000,
            launch={
                **common,
                "provider_kind": "cursor",
                "project_code": "synthetic",
            },
        )
    assert codex.profile.kind == "codex"
    assert cursor.profile.kind == "cursor"
    assert pi.descriptor["runtime_kind"] == "pi"
    assert pi.descriptor["provider_kind"] == "cursor"
    assert pi.descriptor["model"] == "gpt-5.6-sol"
    assert pi.descriptor["effort"] == "xhigh"
    assert [kind for kind, _ in multiplexer.calls] == ["codex", "cursor", "pi"]


def test_multiplexer_registry_and_fail_closed_tmux_restore() -> None:
    assert "metadata" in MultiplexerAdapter.__dict__
    assert "metadata" not in CommandRunner.__dict__
    assert "run" in CommandRunner.__dict__
    registry = builtin_multiplexer_adapter_registry()
    assert registry.adapter("herdr").capabilities == frozenset(
        {
            "calling_context", "discover", "routing", "placement", "metadata", "title",
            "delivery", "steering_delivery", "close", "visible_launch",
            "shotcaller_bootstrap", "rollover_reconciliation",
            "production_cleanup", "runtime_replacement",
        }
    )
    assert registry.adapter("tmux").capabilities == frozenset()
    for adapter in registry.adapters():
        assert adapter.capabilities <= MULTIPLEXER_OPERATIONS
        from league.multiplexer_adapters import MULTIPLEXER_OPERATION_METHODS
        for capability in adapter.capabilities:
            for method in MULTIPLEXER_OPERATION_METHODS[capability]:
                assert callable(getattr(adapter, method, None)), (
                    adapter.kind, capability, method
                )
    try:
        registry.adapter("tmux").metadata(
            {}, RestoredEndpoint("synthetic", "w", "t", "p", "term"), 1
        )
    except StorageRefusal as exc:
        assert exc.code == "multiplexer_restore_unsupported"
    else:
        raise AssertionError("tmux restore was fabricated without its native integration")


def test_registered_herdr_placement_delivery_and_close_are_concrete(root: Path) -> None:
    cwd = root / "mux-cwd"
    cwd.mkdir(parents=True)
    runner = HerdrOperationRunner()
    herdr = builtin_multiplexer_adapter_registry(herdr_runner=runner).adapter("herdr")
    champion = herdr.placement(
        {
            "descriptor_id": "descriptor:champion",
            "workspace_id": "w1",
            "role": "champion",
            "cwd": str(cwd),
        }
    )
    shotcaller = herdr.placement(
        {
            "descriptor_id": "descriptor:shotcaller",
            "workspace_id": "w1",
            "role": "shotcaller",
            "cwd": str(cwd),
            "creator_pane_id": "w1:p1",
        }
    )
    assert (champion.tab_id, champion.pane_id) == ("w1:t2", "w1:p2")
    assert (shotcaller.tab_id, shotcaller.pane_id) == ("w1:t1", "w1:p3")
    delivered = herdr.delivery("lux", "Synthetic exact delivery.")
    assert delivered["target"] == "lux"
    titled = herdr.title(champion, "Synthetic Champion")
    assert titled["title"] == "Synthetic Champion"
    assert herdr.close(champion)["closed"] is True
    assert any(call[1:3] == ("tab", "create") for call in runner.calls)
    assert any(call[1:3] == ("pane", "split") for call in runner.calls)
    assert any(call[1:3] == ("agent", "prompt") for call in runner.calls)
    assert any(call[1:3] == ("pane", "rename") for call in runner.calls)
    assert any(call[1:3] == ("pane", "close") for call in runner.calls)


def test_registered_runtime_observer_matches_all_agent_kinds_once() -> None:
    sessions = {
        "codex": "11111111-1111-4111-8111-111111111111",
        "cursor": "22222222-2222-4222-8222-222222222222",
        "pi": "/synthetic/pi/sessions/observer.jsonl",
    }
    agents = []
    candidates = []
    for index, (kind, session_ref) in enumerate(sessions.items()):
        terminal_id = f"terminal:{kind}"
        agents.append(
            {
                "agent": kind,
                "agent_session": {"value": session_ref},
                "agent_status": "working",
                "name": f"route-{kind}",
                "pane_id": f"pane:{kind}",
                "terminal_id": terminal_id,
            }
        )
        candidates.append(
            {
                "assignment_id": f"assignment:{kind}",
                "harness_kind": f"{kind}-thread",
                "backend_kind": "herdr",
                "session_ref": session_ref,
                "routing_name": f"route-{kind}",
                "endpoint": f"pane:{kind}",
                "runtime_generation": "herdr:"
                + hashlib.sha256(
                    f"{terminal_id}\0{session_ref}".encode("utf-8")
                ).hexdigest()[:24],
            }
        )

    calls = []

    def runner(arguments, *, check, capture_output, text, timeout):
        assert not check and capture_output and text and timeout == 30
        calls.append(tuple(arguments))
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"result": {"agents": agents}}), ""
        )

    observed = HerdrRuntimeObservationAdapter(runner).observe(tuple(candidates))
    assert set(observed) == {f"assignment:{kind}" for kind in sessions}
    assert all(item["state"] == "live" for item in observed.values())
    assert len(calls) == 1 and calls[0][-2:] == ("agent", "list")


def assert_runtime_export_redaction(store: SQLiteStorage) -> None:
    exported = store.export_bytes(format_name="json", purpose="inspection", max_records=1000)
    value = json.loads(exported)
    binding = value["tables"]["runtime_bindings"][0]
    assert binding["session_identity"] == "[redacted]"
    assert binding["endpoint_generation"] == "[redacted]"


def test_non_codex_shared_lifecycle(root: Path) -> None:
    _, state, _ = seeded_state(root, "runtime")
    backend = DeterministicBackend()
    with SQLiteStorage(state) as store:
        lifecycle = RuntimeLifecycle(store, pi_registry(backend))
        created = lifecycle.create(
            RuntimeCreateSpec(
                binding_id="binding:pi-fixture",
                task_id=TASK_ID,
                harness_kind="pi",
                backend_kind="fixture",
                title="Udyr runtime test",
                at=AT3,
                harness={"workload": "synthetic"},
                backend={"endpoint": "isolated"},
            )
        )
        assert created["session_identity"] == "pi:session-1"
        endpoint_state = backend.endpoints[created["endpoint_identity"]]
        endpoint_state["generation"] = "reused-generation"
        prompts_before = [operation for operation, _ in backend.operations].count("prompt")
        try:
            lifecycle.prompt("binding:pi-fixture", "Must not reach a reused endpoint.")
        except StorageRefusal as exc:
            assert exc.code == "identity_mismatch"
        else:
            raise AssertionError("reused endpoint generation accepted runtime input")
        assert [operation for operation, _ in backend.operations].count("prompt") == prompts_before
        endpoint_state["generation"] = "generation-1"
        lifecycle.prompt("binding:pi-fixture", "Route the synthetic task.")
        transition = store.transition(
            CHAMPION_ID, 2, "progress", "Synthetic routed transition.", AT3
        )
        assert transition["version"] == 3
        lifecycle.wake("binding:pi-fixture", "transition:agent-progress")
        assert lifecycle.status("binding:pi-fixture") == "active"
        lifecycle.resume("binding:pi-fixture")
        assert lifecycle.status("binding:pi-fixture") == "active"
        closed = lifecycle.guarded_exit(
            "binding:pi-fixture",
            expected_version=1,
            expected_fence=0,
            executor_id="executor:pi",
            leased_until=AT5,
            at=AT4,
        )
        assert closed == {
            "binding_id": "binding:pi-fixture",
            "state": "closed",
            "version": 3,
            "fence": 1,
            "idempotent": False,
        }
        assert lifecycle.status("binding:pi-fixture") == "missing"
        assert_runtime_export_redaction(store)
    operations = [operation for operation, _ in backend.operations]
    assert all(name in operations for name in ("allocate", "create", "title", "prompt", "hook", "resume", "exit", "close"))


def test_cursor_shared_contract_supports_exact_resume(root: Path) -> None:
    _, state, _ = seeded_state(root, "runtime-cursor")
    backend = DeterministicBackend()
    registry = AdapterRegistry()
    cursor = next(
        adapter
        for adapter in builtin_harness_contracts()
        if adapter.contract.kind == "cursor"
    )
    registry.register_harness(cursor)
    registry.register_backend(backend)
    with SQLiteStorage(state) as store:
        lifecycle = RuntimeLifecycle(store, registry)
        created = lifecycle.create(
            RuntimeCreateSpec(
                "binding:cursor-fixture",
                TASK_ID,
                "cursor",
                "fixture",
                "Cursor runtime test",
                AT3,
                {},
                {},
            )
        )
        lifecycle.prompt(created["binding_id"], "Synthetic Cursor prompt.")
        lifecycle.interrupt(created["binding_id"])
        lifecycle.resume(created["binding_id"])
        assert lifecycle.status(created["binding_id"]) == "active"


def test_create_rolls_back_allocated_endpoint_before_persistence(root: Path) -> None:
    _, state, _ = seeded_state(root, "runtime-rollback")
    backend = TitleFailureBackend()
    with SQLiteStorage(state) as store:
        lifecycle = RuntimeLifecycle(store, pi_registry(backend))
        try:
            lifecycle.create(
                RuntimeCreateSpec(
                    binding_id="binding:rolled-back",
                    task_id=TASK_ID,
                    harness_kind="pi",
                    backend_kind="fixture",
                    title="Synthetic failure",
                    at=AT3,
                    harness={},
                    backend={},
                )
            )
        except StorageRefusal as exc:
            assert exc.code == "synthetic_title_failure"
        else:
            raise AssertionError("synthetic post-create failure unexpectedly succeeded")
        assert store.runtime_binding("binding:rolled-back") is None
    endpoint = OpaqueIdentity("fixture", "endpoint-1")
    assert backend.inspect(endpoint).state == "missing"
    assert [operation for operation, _ in backend.operations].count("exit") == 1
    assert [operation for operation, _ in backend.operations].count("close") == 1


def test_declared_codex_resume_reaches_backend_once(root: Path) -> None:
    _, state, _ = seeded_state(root, "unsupported-resume")
    backend = DeterministicBackend()
    with SQLiteStorage(state) as store:
        lifecycle = RuntimeLifecycle(store, builtin_registry((backend,)))
        created = lifecycle.create(
            RuntimeCreateSpec(
                binding_id="binding:codex-no-resume",
                task_id=TASK_ID,
                harness_kind="codex",
                backend_kind="fixture",
                title="Codex compatibility fixture",
                at=AT3,
                harness={},
                backend={},
            )
        )
        before = len(backend.operations)
        resumed = lifecycle.resume(created["binding_id"])
        assert resumed.observed_state == "active"
        assert len(backend.operations) == before + 2


def test_named_codex_herdr_and_tmux_contract_behavior(root: Path) -> None:
    _, state, _ = seeded_state(root, "codex-contracts")
    herdr = DeterministicBackend("herdr")
    with SQLiteStorage(state) as store:
        lifecycle = RuntimeLifecycle(store, builtin_registry((herdr,)))
        lifecycle.create(
            RuntimeCreateSpec(
                binding_id="binding:codex-herdr",
                task_id=TASK_ID,
                harness_kind="codex",
                backend_kind="herdr",
                title="Codex Herdr contract",
                at=AT3,
                harness={},
                backend={},
            )
        )
        lifecycle.prompt("binding:codex-herdr", "Synthetic Codex prompt.")
        assert lifecycle.status("binding:codex-herdr") == "active"
        lifecycle.guarded_exit(
            "binding:codex-herdr",
            expected_version=1,
            expected_fence=0,
            executor_id="executor:herdr",
            leased_until=AT5,
            at=AT4,
        )
    herdr_operations = [operation for operation, _ in herdr.operations]
    assert all(name in herdr_operations for name in ("allocate", "create", "prompt", "exit", "close"))

    tmux = DeterministicBackend("tmux", BACKEND_CAPABILITIES - {"allocate"})
    endpoint = OpaqueIdentity("tmux", "attached-endpoint")
    tmux.endpoints[endpoint.encoded] = {
        "state": "active",
        "generation": "attached-generation",
        "session_identity": "codex:attached-session",
        "title": "Existing tmux endpoint",
    }
    with SQLiteStorage(state) as store:
        store.register_runtime_binding(
            "binding:codex-tmux",
            TASK_ID,
            "codex",
            "tmux",
            "codex:attached-session",
            endpoint.encoded,
            "attached-generation",
            {
                "harness": ["create", "exit", "identify", "prompt", "status", "title"],
                "backend": ["close", "input", "inspect"],
            },
            AT3,
        )
        lifecycle = RuntimeLifecycle(store, builtin_registry((tmux,)))
        lifecycle.prompt("binding:codex-tmux", "Synthetic attached prompt.")
        assert lifecycle.status("binding:codex-tmux") == "active"
        lifecycle.guarded_exit(
            "binding:codex-tmux",
            expected_version=1,
            expected_fence=0,
            executor_id="executor:tmux",
            leased_until=AT5,
            at=AT4,
        )
    tmux_operations = [operation for operation, _ in tmux.operations]
    assert "allocate" not in tmux_operations
    assert all(name in tmux_operations for name in ("prompt", "exit", "close"))


def test_runtime_exit_fence_recovers_without_duplicate_effects(root: Path) -> None:
    for index, boundary in enumerate(("after_runtime_exit", "after_endpoint_close")):
        _, state, _ = seeded_state(root, f"runtime-exit-{index}")
        backend = DeterministicBackend()
        with SQLiteStorage(state) as store:
            lifecycle = RuntimeLifecycle(store, pi_registry(backend))
            lifecycle.create(
                RuntimeCreateSpec(
                    binding_id=f"binding:exit-{index}",
                    task_id=TASK_ID,
                    harness_kind="pi",
                    backend_kind="fixture",
                    title="Recoverable exit fixture",
                    at=AT3,
                    harness={},
                    backend={},
                )
            )

            def crash(point: str) -> None:
                if point == boundary:
                    raise RuntimeError(point)

            try:
                lifecycle.guarded_exit(
                    f"binding:exit-{index}",
                    expected_version=1,
                    expected_fence=0,
                    executor_id="executor:first",
                    leased_until=AT4,
                    at=AT3,
                    fault=crash,
                )
            except RuntimeError as exc:
                assert str(exc) == boundary
            else:
                raise AssertionError(f"runtime exit crash boundary did not fire: {boundary}")
            try:
                lifecycle.guarded_exit(
                    f"binding:exit-{index}",
                    expected_version=2,
                    expected_fence=1,
                    executor_id="executor:early",
                    leased_until=AT5,
                    at=AT3,
                )
            except StorageRefusal as exc:
                assert exc.code == "runtime_exit_busy" and exc.retryable is True
            else:
                raise AssertionError("runtime exit lease was stolen before expiry")
            resumed = lifecycle.guarded_exit(
                f"binding:exit-{index}",
                expected_version=2,
                expected_fence=1,
                executor_id="executor:resume",
                leased_until=AT5,
                at=AT4,
            )
            assert resumed["state"] == "closed" and resumed["version"] == 4
            duplicate = lifecycle.guarded_exit(
                f"binding:exit-{index}",
                expected_version=4,
                expected_fence=2,
                executor_id="executor:duplicate",
                leased_until=AT5,
                at=AT4,
            )
            assert duplicate["idempotent"] is True
        operations = [operation for operation, _ in backend.operations]
        assert operations.count("exit") == 1
        assert operations.count("close") == 1


def main() -> None:
    test_named_compatibility_matrix_and_opaque_identity()
    test_adapter_contract_validation()
    test_parameterized_provider_lifecycle_event_parity()
    test_multiplexer_registry_and_fail_closed_tmux_restore()
    test_registered_runtime_observer_matches_all_agent_kinds_once()
    with tempfile.TemporaryDirectory(prefix="league-runtime-adapter-") as temporary:
        root = Path(temporary)
        test_runtime_matrix_cli_and_unsupported_create(root / "matrix")
        test_registered_herdr_placement_delivery_and_close_are_concrete(root / "mux")
        test_registered_visible_launch_factories_own_provider_selection(root / "factory")
        test_non_codex_shared_lifecycle(root / "lifecycle")
        test_cursor_shared_contract_supports_exact_resume(root / "cursor-lifecycle")
        test_create_rolls_back_allocated_endpoint_before_persistence(root / "rollback")
        test_declared_codex_resume_reaches_backend_once(root / "codex-resume")
        test_named_codex_herdr_and_tmux_contract_behavior(root / "codex-contracts")
        test_runtime_exit_fence_recovers_without_duplicate_effects(root / "exit-fence")
    print("PASS: named Codex/Herdr/tmux contracts, opaque identity, and isolated non-Codex create-to-cleanup lifecycle")


if __name__ == "__main__":
    main()
