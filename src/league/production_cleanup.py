"""Canonical SQLite-backed cleanup execution through exact production adapters."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence

from .cleanup import (
    CleanupAdapterRegistry,
    CleanupExecutor,
    cleanup_action_digest,
)
from .continuation import GitHubIssueAdapter, IssueCommandRunner
from .real_cleanup import (
    CallsignAdapter,
    CommandRunner,
    GitAdapter,
    HerdrBackendAdapter,
    HerdrHarnessAdapter,
    SubprocessRunner,
)
from .provider_lifecycle import profile_for_runtime_kind
from .multiplexer_adapters import (
    MultiplexerAdapterRegistry,
    builtin_multiplexer_adapter_registry,
)
from .storage import Storage, StorageRefusal


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class ProcessPort(Protocol):
    def inspect(self, pid: int) -> Optional[Mapping[str, Any]]: ...
    def terminate(self, pid: int, process_start: str) -> Mapping[str, Any]: ...


class SystemProcessPort:
    """Exact PID/start-time process control; it never scans for candidates."""

    def __init__(self, runner: Optional[CommandRunner] = None) -> None:
        self.runner = runner or SubprocessRunner()

    def inspect(self, pid: int) -> Optional[Mapping[str, Any]]:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            raise StorageRefusal("cleanup_identity_mismatch", "process PID is unsafe")
        inspected = self.runner.run(
            ("ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="),
            allow_failure=True,
        )
        line = inspected.stdout.rstrip("\n")
        start_text = line[:24].strip()
        state_text = line[24:].strip()
        if inspected.returncode != 0 or not start_text or not state_text:
            return None
        if state_text.startswith("Z"):
            return None
        return {"pid": pid, "process_start": start_text, "state": state_text}

    def terminate(self, pid: int, process_start: str) -> Mapping[str, Any]:
        observed = self.inspect(pid)
        if observed is None or observed.get("process_start") != process_start:
            raise StorageRefusal("cleanup_identity_mismatch", "process identity changed before termination")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            raise StorageRefusal("cleanup_adapter_failed", "exact process graceful stop failed") from exc
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.inspect(pid) is None:
                return {"pid": pid, "exit_verified": True, "signal": "SIGTERM"}
            time.sleep(0.1)
        raise StorageRefusal("cleanup_adapter_failed", "exact process did not exit after SIGTERM")


class _BaseAdapter:
    def intended(self, action: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
        return dict(observation) == dict(action["intended_state"])


class CanonicalArchiveAdapter(_BaseAdapter):
    """The fixed SQLite action payload is the immutable pre-effect archive."""

    kind = "archive"

    def __init__(self, archived: Mapping[str, Any]) -> None:
        self.archived = dict(archived)

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        if action.get("intended_state") != self.archived:
            raise StorageRefusal("cleanup_identity_mismatch", "canonical cleanup archive changed")
        return dict(self.archived)

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        raise StorageRefusal("cleanup_archive_conflict", "canonical archive must exist before execution")


class ExactProcessAdapter(_BaseAdapter):
    kind = "process"

    def __init__(self, port: ProcessPort) -> None:
        self.port = port

    @staticmethod
    def _identity(action: Mapping[str, Any]) -> tuple[dict[str, Any], int, str]:
        expected = action.get("expected_identity")
        if not isinstance(expected, Mapping):
            raise StorageRefusal("cleanup_identity_mismatch", "process identity is missing")
        pid = expected.get("pid")
        process_start = expected.get("process_start")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 1
            or not isinstance(process_start, str)
            or not process_start
        ):
            raise StorageRefusal("cleanup_identity_mismatch", "process PID/start identity is incomplete")
        return dict(expected), pid, process_start

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        expected, pid, process_start = self._identity(action)
        observed = self.port.inspect(pid)
        if observed is None:
            return dict(action["intended_state"])
        if observed.get("process_start") != process_start:
            raise StorageRefusal("cleanup_identity_mismatch", "process PID was reused")
        return expected

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        _, pid, process_start = self._identity(action)
        return dict(self.port.terminate(pid, process_start))


class SharedLeaseAdapter(_BaseAdapter):
    kind = "lease"
    REQUIRED = frozenset(
        {"resource_id", "task_id", "owner_agent_id", "kind", "endpoint", "generation"}
    )

    def __init__(self, store: Storage) -> None:
        self.store = store

    def _expected(self, action: Mapping[str, Any]) -> dict[str, Any]:
        expected = action.get("expected_identity")
        if not isinstance(expected, Mapping) or set(expected) != self.REQUIRED:
            raise StorageRefusal("cleanup_identity_mismatch", "shared lease identity is incomplete")
        return dict(expected)

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        expected = self._expected(action)
        row = self.store.resource_lease_for_cleanup(expected["resource_id"])
        if row is None or any(row[key] != value for key, value in expected.items()):
            raise StorageRefusal("cleanup_identity_mismatch", "shared lease identity changed")
        if row["state"] == "released":
            return dict(action["intended_state"])
        if row["state"] != "active":
            raise StorageRefusal("cleanup_identity_mismatch", "shared lease is not exactly active")
        return expected

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.store.release_resource_lease_for_cleanup(self._expected(action))


PRODUCTION_CLEANUP_ADAPTER_KINDS = frozenset(
    {
        CanonicalArchiveAdapter.kind,
        HerdrHarnessAdapter.kind,
        HerdrBackendAdapter.kind,
        GitAdapter.kind,
        CallsignAdapter.kind,
        ExactProcessAdapter.kind,
        SharedLeaseAdapter.kind,
        GitHubIssueAdapter.kind,
    }
)


def _one(actions: Sequence[Mapping[str, Any]], action_kind: str) -> Mapping[str, Any]:
    matches = [action for action in actions if action.get("action_kind") == action_kind]
    if len(matches) != 1:
        raise StorageRefusal(
            "cleanup_operation_invalid", f"cleanup requires exactly one {action_kind} action"
        )
    return matches[0]


def _validate_runtime_context(
    context: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    multiplexers: Optional[MultiplexerAdapterRegistry] = None,
) -> dict[str, Any]:
    harness_action = _one(actions, "session_exit")
    backend_action = _one(actions, "endpoint_close")
    harness = harness_action.get("expected_identity")
    backend = backend_action.get("expected_identity")
    if not isinstance(harness, Mapping) or not isinstance(backend, Mapping):
        raise StorageRefusal("cleanup_identity_mismatch", "runtime cleanup identity is incomplete")
    required_harness = {"agent_name", "pane_id", "session_id"}
    required_backend = {
        "pane_id",
        "terminal_id",
        "runtime_instance_id",
        "runtime_generation",
    }
    if set(harness) != required_harness or set(backend) != required_backend:
        raise StorageRefusal("cleanup_identity_mismatch", "runtime cleanup identity shape is unsupported")
    if harness["pane_id"] != backend["pane_id"]:
        raise StorageRefusal("cleanup_identity_mismatch", "harness and backend endpoint disagree")
    matches = [
        runtime
        for runtime in context["runtime_instances"]
        if runtime["runtime_instance_id"] == backend["runtime_instance_id"]
    ]
    if len(matches) != 1:
        raise StorageRefusal("cleanup_identity_mismatch", "canonical runtime identity is missing or ambiguous")
    runtime = matches[0]
    try:
        profile = profile_for_runtime_kind(str(runtime["harness_kind"]))
    except StorageRefusal as exc:
        raise StorageRefusal(
            "cleanup_adapter_unsupported",
            "canonical runtime has no supported production harness cleanup policy",
        ) from exc
    selected_multiplexers = multiplexers or builtin_multiplexer_adapter_registry()
    try:
        multiplexer = selected_multiplexers.adapter(str(runtime["backend_kind"]))
    except StorageRefusal as exc:
        raise StorageRefusal(
            "cleanup_adapter_unsupported",
            "canonical runtime has no registered production multiplexer cleanup policy",
        ) from exc
    if (
        "production_cleanup" not in multiplexer.capabilities
        or runtime["session_ref"] != harness["session_id"]
        or runtime["endpoint"] != backend["pane_id"]
        or runtime["runtime_generation"] != backend["runtime_generation"]
        or runtime["status"] not in {"active", "idle", "closed"}
        or (runtime["status"] in {"active", "idle"} and not runtime["verified"])
    ):
        raise StorageRefusal(
            "cleanup_adapter_unsupported",
            "canonical runtime is not a supported verified provider+multiplexer cleanup policy",
        )
    return {
        "agent_name": harness["agent_name"],
        "pane_id": backend["pane_id"],
        "terminal_id": backend["terminal_id"],
        "session_id": harness["session_id"],
        "runtime_instance_id": backend["runtime_instance_id"],
        "runtime_generation": backend["runtime_generation"],
        "backend_kind": str(runtime["backend_kind"]),
        "provider_kind": profile.kind,
        "exit_prompt": profile.exit_prompt,
    }


def production_cleanup_registry(
    store: Storage,
    context: Mapping[str, Any],
    *,
    at: str,
    runner: Optional[CommandRunner] = None,
    process_port: Optional[ProcessPort] = None,
    issue_runner: Optional[IssueCommandRunner] = None,
    issue_executable: Optional[str] = None,
    multiplexer_registry: Optional[MultiplexerAdapterRegistry] = None,
) -> CleanupAdapterRegistry:
    """Validate every persisted adapter policy before returning executable drivers."""

    operation = context.get("operation")
    actions = operation.get("actions") if isinstance(operation, Mapping) else None
    if not isinstance(actions, list):
        raise StorageRefusal("cleanup_operation_invalid", "canonical action plan is malformed")
    declared = {str(action.get("adapter_kind")) for action in actions}
    unknown = declared - PRODUCTION_CLEANUP_ADAPTER_KINDS
    if unknown:
        raise StorageRefusal(
            "cleanup_adapter_unsupported",
            "canonical cleanup plan names an unsupported production adapter",
        )
    command_runner = runner or SubprocessRunner()
    multiplexers = multiplexer_registry or builtin_multiplexer_adapter_registry()
    registry = CleanupAdapterRegistry()
    archive = _one(actions, "archive_identity_evidence")
    registry.register(CanonicalArchiveAdapter(archive["intended_state"]))
    runtime_identity = _validate_runtime_context(context, actions, multiplexers)
    multiplexer = multiplexers.adapter(runtime_identity["backend_kind"])
    harness_driver, backend_driver = multiplexer.cleanup_drivers(
        store=store,
        identity=runtime_identity,
        runner=command_runner,
        at=at,
    )
    registry.register(harness_driver)
    registry.register(backend_driver)
    if "git" in declared:
        worktree = _one(actions, "worktree_remove")["expected_identity"]
        branch = _one(actions, "branch_delete")["expected_identity"]
        if not isinstance(worktree, Mapping) or not isinstance(branch, Mapping):
            raise StorageRefusal("cleanup_identity_mismatch", "Git cleanup identity is incomplete")
        git_identity = {**dict(worktree), **dict(branch)}
        required = {"repository", "worktree", "branch", "head", "base_ref", "merge_commit"}
        if set(git_identity) != required:
            raise StorageRefusal("cleanup_identity_mismatch", "Git cleanup identity shape is unsupported")
        registry.register(GitAdapter(git_identity, command_runner))
    callsign = _one(actions, "callsign_release")["expected_identity"]
    if not isinstance(callsign, Mapping):
        raise StorageRefusal("cleanup_identity_mismatch", "callsign cleanup identity is incomplete")
    registry.register(CallsignAdapter(store, callsign, at))
    if "process" in declared:
        registry.register(ExactProcessAdapter(process_port or SystemProcessPort(command_runner)))
    if "lease" in declared:
        registry.register(SharedLeaseAdapter(store))
    if "issue" in declared:
        registry.register(
            GitHubIssueAdapter(issue_runner, executable=issue_executable)
        )
    return registry


@dataclass(frozen=True)
class ProductionCleanupResult:
    context: Mapping[str, Any]
    execution: Mapping[str, Any]
    rollover: Optional[Mapping[str, Any]]

    def as_record(self) -> dict[str, Any]:
        return {
            "mode": (
                "rollover_predecessor" if self.context.get("rollover") is not None else "automatic_champion"
            ),
            "operation_id": self.context["operation"]["operation_id"],
            "cleanup_revision": int(self.context["operation"]["cleanup_revision"]),
            "task_identity": dict(self.context["task_identity"]),
            "execution": dict(self.execution),
            "rollover": None if self.rollover is None else dict(self.rollover),
        }


class ProductionCleanup:
    def __init__(
        self,
        store: Storage,
        *,
        runner: Optional[CommandRunner] = None,
        process_port: Optional[ProcessPort] = None,
        issue_runner: Optional[IssueCommandRunner] = None,
        issue_executable: Optional[str] = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.process_port = process_port
        self.issue_runner = issue_runner
        self.issue_executable = issue_executable

    def execute(
        self,
        operation_id: str,
        *,
        expected_fence: int,
        executor_id: str,
        leased_until: str,
        at: str,
        fault: Optional[Any] = None,
    ) -> dict[str, Any]:
        context = self.store.cleanup_execution_context(operation_id)
        registry = production_cleanup_registry(
            self.store,
            context,
            at=at,
            runner=self.runner,
            process_port=self.process_port,
            issue_runner=self.issue_runner,
            issue_executable=self.issue_executable,
        )
        execution = CleanupExecutor(self.store, registry).execute(
            operation_id,
            expected_fence=expected_fence,
            executor_id=executor_id,
            leased_until=leased_until,
            at=at,
            fault=fault,
        )
        rollover_result: Optional[Mapping[str, Any]] = None
        if context.get("rollover") is not None and execution["state"] == "cleanup_completed":
            rollover_result = self._complete_rollover(context, at)
        return ProductionCleanupResult(context, execution, rollover_result).as_record()

    def _complete_rollover(self, context: Mapping[str, Any], at: str) -> Mapping[str, Any]:
        rollover = dict(context["rollover"])
        operation = self.store.cleanup_operation(context["operation"]["operation_id"])
        if operation is None:
            raise StorageRefusal("cleanup_operation_unknown", "completed cleanup operation disappeared")
        actions = operation["actions"]
        archive = _one(actions, "archive_identity_evidence")
        callsign = _one(actions, "callsign_release")
        archive_hash = (archive.get("receipt") or {}).get("receipt_hash")
        resource_hashes = [
            (action.get("receipt") or {}).get("receipt_hash")
            for action in actions
            if action.get("resource_id") is not None
        ]
        if not isinstance(archive_hash, str) or any(
            not isinstance(value, str) for value in resource_hashes
        ):
            raise StorageRefusal("cleanup_receipt_conflict", "rollover cleanup receipts are incomplete")
        receipt = {
            "schema": "league.rollover-drain-receipt.v1",
            "verified": True,
            "operation_id": rollover["operation_id"],
            "predecessor_agent_id": rollover["predecessor_agent_id"],
            "successor_agent_id": rollover["successor_agent_id"],
            "owner_event_id": rollover["owner_event_id"],
            "archive_digest": archive_hash,
            "resource_receipt_digest": hashlib.sha256(
                _stable_json(resource_hashes).encode("utf-8")
            ).hexdigest(),
            "callsign_release_receipt_digest": cleanup_action_digest(callsign),
        }
        return self.store.complete_rollover_drain(
            rollover["operation_id"], int(rollover["version"]), receipt, at
        )


__all__ = [
    "ExactProcessAdapter",
    "ProcessPort",
    "ProductionCleanup",
    "SharedLeaseAdapter",
    "SystemProcessPort",
    "production_cleanup_registry",
    "PRODUCTION_CLEANUP_ADAPTER_KINDS",
]
