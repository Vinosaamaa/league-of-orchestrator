#!/usr/bin/env python3
"""Capability matrix and shared non-Codex runtime lifecycle regressions."""

from __future__ import annotations

import json
import sys
import tempfile
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
from league.sqlite_store import SQLiteStorage  # noqa: E402
from league.storage import StorageRefusal  # noqa: E402
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
    assert pairs[("codex", "herdr")]["operations"]["resume"] == "unsupported"
    assert pairs[("codex", "tmux")]["operations"]["backend.allocate"] == "unsupported"
    assert pairs[("pi", "herdr")]["evidence"] == "unverified"
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
        lifecycle.interrupt("binding:pi-fixture")
        assert lifecycle.status("binding:pi-fixture") == "idle"
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
    assert all(name in operations for name in ("allocate", "create", "title", "prompt", "hook", "interrupt", "resume", "exit", "close"))


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


def test_unsupported_resume_fails_before_backend_input(root: Path) -> None:
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
        try:
            lifecycle.resume(created["binding_id"])
        except StorageRefusal as exc:
            assert exc.code == "unsupported_capability"
        else:
            raise AssertionError("undeclared Codex resume capability was invoked")
        assert len(backend.operations) == before


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
        lifecycle.interrupt("binding:codex-herdr")
        assert lifecycle.status("binding:codex-herdr") == "idle"
        lifecycle.guarded_exit(
            "binding:codex-herdr",
            expected_version=1,
            expected_fence=0,
            executor_id="executor:herdr",
            leased_until=AT5,
            at=AT4,
        )
    herdr_operations = [operation for operation, _ in herdr.operations]
    assert all(name in herdr_operations for name in ("allocate", "create", "prompt", "interrupt", "exit", "close"))

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
                "harness": ["create", "exit", "identify", "interrupt", "prompt", "status", "title"],
                "backend": ["close", "input", "inspect"],
            },
            AT3,
        )
        lifecycle = RuntimeLifecycle(store, builtin_registry((tmux,)))
        lifecycle.prompt("binding:codex-tmux", "Synthetic attached prompt.")
        assert lifecycle.status("binding:codex-tmux") == "active"
        lifecycle.interrupt("binding:codex-tmux")
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
    assert all(name in tmux_operations for name in ("prompt", "interrupt", "exit", "close"))


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
    with tempfile.TemporaryDirectory(prefix="league-runtime-adapter-") as temporary:
        root = Path(temporary)
        test_runtime_matrix_cli_and_unsupported_create(root / "matrix")
        test_non_codex_shared_lifecycle(root / "lifecycle")
        test_create_rolls_back_allocated_endpoint_before_persistence(root / "rollback")
        test_unsupported_resume_fails_before_backend_input(root / "unsupported")
        test_named_codex_herdr_and_tmux_contract_behavior(root / "codex-contracts")
        test_runtime_exit_fence_recovers_without_duplicate_effects(root / "exit-fence")
    print("PASS: named Codex/Herdr/tmux contracts, opaque identity, and isolated non-Codex create-to-cleanup lifecycle")


if __name__ == "__main__":
    main()
