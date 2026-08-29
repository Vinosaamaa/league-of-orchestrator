"""Recoverable high-level Shotcaller rollover over the existing durable stages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from .sqlite_callsign_ops import digest, stable_json
from .storage import Storage, StorageRefusal


RUN_SCHEMA = "league.shotcaller-rollover-run.v1"
ADAPTER_CONFIG_SCHEMA = "league.rollover-provider-adapters.v1"
ADAPTER_REQUEST_SCHEMA = "league.rollover-provider-request.v1"
ADAPTER_RECEIPT_SCHEMA = "league.rollover-provider-receipt.v1"
MAX_ADAPTERS = 16
MAX_COMMAND_ARGUMENTS = 16
MAX_ADAPTER_INPUT_BYTES = 16 * 1024 * 1024
MAX_ADAPTER_OUTPUT_BYTES = 1024 * 1024
MAX_BINDING_PAGES = 10_000
SAFE_KIND = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")


class AdapterCommandRunner(Protocol):
    def run(self, command: Sequence[str], action: str, payload: bytes) -> bytes: ...


class SubprocessAdapterCommandRunner:
    """Bounded local command transport; adapter stderr and raw output stay private."""

    def run(self, command: Sequence[str], action: str, payload: bytes) -> bytes:
        if len(payload) > MAX_ADAPTER_INPUT_BYTES:
            raise StorageRefusal(
                "rollover_adapter_input_too_large",
                "rollover provider adapter input exceeds its bound",
            )
        try:
            with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                source.write(payload)
                source.seek(0)
                completed = subprocess.run(
                    [*command, action],
                    stdin=source,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=120,
                )
                if stdout.tell() > MAX_ADAPTER_OUTPUT_BYTES or stderr.tell() > MAX_ADAPTER_OUTPUT_BYTES:
                    raise StorageRefusal(
                        "rollover_adapter_output_too_large",
                        "rollover provider adapter output exceeds its bound",
                    )
                stdout.seek(0)
                body = stdout.read()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StorageRefusal(
                "rollover_adapter_outcome_unknown",
                "rollover provider adapter outcome is unknown; retry with the same operation",
                retryable=True,
            ) from exc
        if completed.returncode != 0:
            raise StorageRefusal(
                "rollover_adapter_outcome_unknown",
                "rollover provider adapter outcome is unknown; retry with the same operation",
                retryable=True,
            )
        return body


@dataclass(frozen=True)
class ConfiguredProviderAdapter:
    harness_kind: str
    command: tuple[str, ...]
    runner: AdapterCommandRunner

    def invoke(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        encoded = (stable_json(payload) + "\n").encode("utf-8")
        raw = self.runner.run(self.command, action, encoded)
        try:
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StorageRefusal(
                "rollover_adapter_invalid",
                "rollover provider adapter returned malformed private output",
            ) from exc
        if not isinstance(value, dict):
            raise StorageRefusal(
                "rollover_adapter_invalid",
                "rollover provider adapter returned a non-object",
            )
        return value


class ConfiguredProviderAdapters:
    def __init__(
        self,
        value: Mapping[str, Any],
        *,
        runner: Optional[AdapterCommandRunner] = None,
    ) -> None:
        if set(value) != {"schema", "adapters"} or value.get("schema") != ADAPTER_CONFIG_SCHEMA:
            raise StorageRefusal(
                "rollover_adapter_config_invalid", "rollover provider adapter config is invalid"
            )
        entries = value.get("adapters")
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ADAPTERS:
            raise StorageRefusal(
                "rollover_adapter_config_invalid", "rollover provider adapter list is empty or unbounded"
            )
        selected_runner = runner or SubprocessAdapterCommandRunner()
        self._adapters: dict[str, ConfiguredProviderAdapter] = {}
        normalized_entries: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {"harness_kind", "command"}:
                raise StorageRefusal(
                    "rollover_adapter_config_invalid", "rollover provider adapter entry is invalid"
                )
            kind = entry.get("harness_kind")
            command = entry.get("command")
            if not isinstance(kind, str) or not SAFE_KIND.fullmatch(kind) or kind in self._adapters:
                raise StorageRefusal(
                    "rollover_adapter_config_invalid", "rollover harness adapter identity is invalid"
                )
            if (
                not isinstance(command, list)
                or not 1 <= len(command) <= MAX_COMMAND_ARGUMENTS
                or any(
                    not isinstance(item, str)
                    or not item
                    or item.strip() != item
                    or len(item.encode("utf-8")) > 4096
                    or any(ord(character) < 32 for character in item)
                    for item in command
                )
            ):
                raise StorageRefusal(
                    "rollover_adapter_config_invalid", "rollover adapter command is invalid"
                )
            executable = Path(command[0])
            try:
                resolved_executable = executable.resolve(strict=True)
            except OSError as exc:
                raise StorageRefusal(
                    "rollover_adapter_config_invalid",
                    "rollover adapter executable cannot be resolved",
                ) from exc
            if not executable.is_absolute() or not resolved_executable.is_file() or not os.access(executable, os.X_OK):
                raise StorageRefusal(
                    "rollover_adapter_config_invalid",
                    "rollover adapter executable must be one exact absolute regular executable",
                )
            self._adapters[kind] = ConfiguredProviderAdapter(
                kind, tuple(command), selected_runner
            )
            normalized_entries.append({"harness_kind": kind, "command": list(command)})
        self.digest = digest(
            {
                "schema": ADAPTER_CONFIG_SCHEMA,
                "adapters": sorted(
                    normalized_entries, key=lambda item: item["harness_kind"]
                ),
            }
        )

    def require(self, harness_kind: str) -> ConfiguredProviderAdapter:
        try:
            return self._adapters[harness_kind]
        except KeyError as exc:
            raise StorageRefusal(
                "rollover_adapter_unconfigured",
                f"no configured provider adapter matches harness {harness_kind}",
            ) from exc


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise StorageRefusal("rollover_run_invalid", f"{label} is required")
    if len(value.encode("utf-8")) > 4096 or any(ord(character) < 32 for character in value):
        raise StorageRefusal("rollover_run_invalid", f"{label} is unsafe or too large")
    return value


def validate_run_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema",
        "operation_id",
        "squad_id",
        "predecessor_agent_id",
        "successor_agent_id",
        "predecessor_runtime_instance_id",
        "successor_runtime_instance_id",
        "callsign_assignment_id",
        "expected_owner_version",
        "expected_owner_fence",
        "authority_kind",
        "authority_digest",
        "required_capabilities",
        "plan",
        "owner_event_id",
        "owner_outbox_id",
    }
    if set(value) != keys or value.get("schema") != RUN_SCHEMA:
        raise StorageRefusal("rollover_run_invalid", "rollover run manifest shape is invalid")
    result = dict(value)
    for key in keys - {
        "schema",
        "expected_owner_version",
        "expected_owner_fence",
        "required_capabilities",
        "plan",
    }:
        result[key] = _text(result[key], key)
    if result["predecessor_agent_id"] == result["successor_agent_id"]:
        raise StorageRefusal("rollover_run_invalid", "rollover predecessor and successor must differ")
    if result["authority_kind"] not in {"explicit", "automatic"}:
        raise StorageRefusal("rollover_run_invalid", "rollover authority kind is invalid")
    for key in ("expected_owner_version", "expected_owner_fence"):
        if isinstance(result[key], bool) or not isinstance(result[key], int) or result[key] < 1:
            raise StorageRefusal("rollover_run_invalid", f"{key} must be a positive integer")
    required = result["required_capabilities"]
    if not isinstance(required, list):
        raise StorageRefusal("rollover_run_invalid", "required_capabilities must be a list")
    result["required_capabilities"] = list(required)
    if not isinstance(result["plan"], Mapping):
        raise StorageRefusal("rollover_run_invalid", "rollover plan must be an object")
    result["plan"] = dict(result["plan"])
    return result


def _receipt_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256((stable_json(value) + "\n").encode("utf-8")).hexdigest()


def _request(
    action: str,
    operation: Mapping[str, Any],
    body: Mapping[str, Any],
) -> dict[str, Any]:
    public_operation = {
        key: value for key, value in operation.items() if key != "idempotent"
    }
    idempotency_key = digest(
        {
            "operation_id": operation["operation_id"],
            "action": action,
            "version": operation["version"],
            "handoff_digest": operation["handoff_digest"],
            "snapshot_digest": operation["snapshot"]["digest"],
        }
    )
    return {
        "schema": ADAPTER_REQUEST_SCHEMA,
        "action": action,
        "idempotency_key": idempotency_key,
        "operation": public_operation,
        **body,
    }


def _exact_receipt(value: Mapping[str, Any], action: str, keys: set[str]) -> dict[str, Any]:
    expected = {"schema", "action", "verified", "idempotency_key", *keys}
    if (
        set(value) != expected
        or value.get("schema") != ADAPTER_RECEIPT_SCHEMA
        or value.get("action") != action
        or value.get("verified") is not True
    ):
        raise StorageRefusal(
            "rollover_adapter_invalid", f"rollover {action} adapter receipt is not exact and verified"
        )
    return dict(value)


class ShotcallerRolloverRunner:
    """Advance only the existing rollover state, resuming safely after every stage."""

    def __init__(self, store: Storage, adapters: ConfiguredProviderAdapters) -> None:
        self.store = store
        self.adapters = adapters

    def _context(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        return self.store.rollover_execution_context(
            manifest["operation_id"],
            manifest["predecessor_runtime_instance_id"],
            manifest["successor_runtime_instance_id"],
        )

    @staticmethod
    def _assert_identity(operation: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
        if any(
            operation[key] != manifest[key]
            for key in ("operation_id", "squad_id", "predecessor_agent_id", "successor_agent_id")
        ):
            raise StorageRefusal("rollover_run_conflict", "rollover run identity changed")

    def _acknowledge(
        self,
        manifest: Mapping[str, Any],
        operation: Mapping[str, Any],
        execution: Mapping[str, Any],
        at: str,
    ) -> tuple[dict[str, Any], str]:
        successor = execution["successor_runtime"]
        if successor is None:
            raise StorageRefusal(
                "successor_runtime_pending", "successor exact runtime acceptance is still required", retryable=True
            )
        startup = self.store.startup_context(
            manifest["successor_agent_id"], manifest["successor_runtime_instance_id"], at
        )
        pages: list[dict[str, Any]] = []
        page_bytes = 0
        snapshot = operation["snapshot"]
        expected_pages = max(
            1,
            min(
                MAX_BINDING_PAGES,
                (int(snapshot["count"]) + int(snapshot["page_bound"]) - 1)
                // int(snapshot["page_bound"]),
            ),
        )
        cursor: Optional[str] = None
        while True:
            value = self.store.rollover_bindings(
                manifest["operation_id"], at, cursor=cursor
            )
            if len(pages) >= expected_pages:
                raise StorageRefusal(
                    "rollover_bindings_unbounded",
                    "rollover bindings returned more pages than the immutable snapshot permits",
                )
            page_bytes += len(stable_json(value["page"]).encode("utf-8"))
            if page_bytes > MAX_ADAPTER_INPUT_BYTES:
                raise StorageRefusal(
                    "rollover_adapter_input_too_large",
                    "rollover binding pages exceed the provider input bound",
                )
            pages.append(value["page"])
            cursor = value["next_cursor"]
            if cursor is None:
                break
        bindings = {"pages": pages}
        pages_digest = digest(bindings)
        adapter = self.adapters.require(str(successor["harness_kind"]))
        provider_request = _request(
            "acknowledge",
            operation,
            {"startup_context": startup, "bindings": bindings},
        )
        receipt = _exact_receipt(
            adapter.invoke("acknowledge", provider_request),
            "acknowledge",
            {
                "operation_id",
                "successor_agent_id",
                "runtime_instance_id",
                "handoff_digest",
                "snapshot_version",
                "snapshot_count",
                "snapshot_digest",
                "pages_digest",
            },
        )
        expected = {
            "idempotency_key": provider_request["idempotency_key"],
            "operation_id": manifest["operation_id"],
            "successor_agent_id": manifest["successor_agent_id"],
            "runtime_instance_id": manifest["successor_runtime_instance_id"],
            "handoff_digest": operation["handoff_digest"],
            "snapshot_version": operation["snapshot"]["version"],
            "snapshot_count": operation["snapshot"]["count"],
            "snapshot_digest": operation["snapshot"]["digest"],
            "pages_digest": pages_digest,
        }
        if any(receipt[key] != expected[key] for key in expected):
            raise StorageRefusal(
                "rollover_adapter_mismatch", "successor acknowledgement changed exact handoff identity"
            )
        result = self.store.acknowledge_rollover(
            manifest["operation_id"],
            manifest["successor_agent_id"],
            manifest["successor_runtime_instance_id"],
            operation["handoff_digest"],
            operation["snapshot"]["version"],
            operation["snapshot"]["count"],
            operation["snapshot"]["digest"],
            pages,
            at,
        )
        return result, _receipt_digest(receipt)

    def _cleanup(
        self,
        action: str,
        participant: str,
        manifest: Mapping[str, Any],
        operation: Mapping[str, Any],
        execution: Mapping[str, Any],
        at: str,
    ) -> tuple[dict[str, Any], str]:
        runtime = execution[f"{participant}_runtime"]
        if runtime is None:
            if action != "abort":
                raise StorageRefusal("rollover_runtime_mismatch", "drain runtime is missing")
            return {
                "schema": "league.rollover-abort-receipt.v1",
                "verified": True,
                "operation_id": manifest["operation_id"],
                "successor_agent_id": manifest["successor_agent_id"],
                "runtime_instance_id": "not-created",
                "runtime_cleanup_receipt_digest": "not-created",
                "cleanup_digest": digest({"operation_id": manifest["operation_id"], "runtime": None}),
            }, digest({"operation_id": manifest["operation_id"], "runtime": None})
        runtime_identity = {
            key: runtime[key]
            for key in (
                "runtime_instance_id",
                "harness_kind",
                "backend_kind",
                "session_identity",
                "endpoint_identity",
                "runtime_generation",
            )
        }
        adapter = self.adapters.require(str(runtime["harness_kind"]))
        provider_request = _request(
            action,
            operation,
            {
                "participant": participant,
                "runtime": runtime_identity,
                "pending_obligations": execution["pending_obligations"],
            },
        )
        receipt = _exact_receipt(
            adapter.invoke(action, provider_request),
            action,
            (
                {
                    "operation_id",
                    "predecessor_agent_id",
                    "successor_agent_id",
                    "runtime",
                    "runtime_cleanup_receipt_digest",
                    "cleanup_digest",
                }
                if action == "abort"
                else {
                    "operation_id",
                    "predecessor_agent_id",
                    "successor_agent_id",
                    "owner_event_id",
                    "runtime",
                    "archive_digest",
                    "resource_receipt_digest",
                    "callsign_release_receipt_digest",
                }
            ),
        )
        exact = {
            "idempotency_key": provider_request["idempotency_key"],
            "operation_id": manifest["operation_id"],
            "predecessor_agent_id": manifest["predecessor_agent_id"],
            "successor_agent_id": manifest["successor_agent_id"],
            "runtime": runtime_identity,
        }
        if action == "drain":
            exact["owner_event_id"] = operation["owner_event_id"]
        if any(receipt[key] != expected for key, expected in exact.items()):
            raise StorageRefusal(
                "rollover_adapter_mismatch", f"{action} adapter changed exact rollover identity"
            )
        self.store.record_rollover_runtime_closed(
            manifest["operation_id"],
            participant,
            runtime["runtime_instance_id"],
            runtime["session_identity"],
            runtime["endpoint_identity"],
            runtime["runtime_generation"],
            at,
        )
        if action == "abort":
            cleanup = {
                "schema": "league.rollover-abort-receipt.v1",
                "verified": True,
                "operation_id": manifest["operation_id"],
                "successor_agent_id": manifest["successor_agent_id"],
                "runtime_instance_id": runtime["runtime_instance_id"],
                "runtime_cleanup_receipt_digest": receipt["runtime_cleanup_receipt_digest"],
                "cleanup_digest": receipt["cleanup_digest"],
            }
        else:
            cleanup = {
                "schema": "league.rollover-drain-receipt.v1",
                "verified": True,
                "operation_id": manifest["operation_id"],
                "predecessor_agent_id": manifest["predecessor_agent_id"],
                "successor_agent_id": manifest["successor_agent_id"],
                "owner_event_id": operation["owner_event_id"],
                "archive_digest": receipt["archive_digest"],
                "resource_receipt_digest": receipt["resource_receipt_digest"],
                "callsign_release_receipt_digest": receipt["callsign_release_receipt_digest"],
            }
        return cleanup, _receipt_digest(receipt)

    def _abort_stage(
        self,
        manifest: Mapping[str, Any],
        operation: Mapping[str, Any],
        execution: Mapping[str, Any],
        at: str,
        stages: list[dict[str, Any]],
        adapter_receipts: list[dict[str, str]],
    ) -> dict[str, Any]:
        if operation["state"] == "switched":
            raise StorageRefusal("rollover_conflict", "rollover cannot abort after owner switch")
        cleanup, receipt_digest = self._cleanup(
            "abort", "successor", manifest, operation, execution, at
        )
        adapter_receipts.append({"action": "abort", "digest": receipt_digest})
        result = self.store.abort_rollover(
            manifest["operation_id"], operation["version"], cleanup, at
        )
        stages.append({"stage": "abort", "outcome": "completed"})
        return result

    def _acknowledgement_stage(
        self,
        manifest: Mapping[str, Any],
        operation: Mapping[str, Any],
        execution: Mapping[str, Any],
        at: str,
        stages: list[dict[str, Any]],
        adapter_receipts: list[dict[str, str]],
    ) -> tuple[dict[str, Any], Optional[dict[str, int]]]:
        if execution["successor_runtime"] is None:
            stages.append({"stage": "bindings", "outcome": "successor_runtime_pending"})
            return dict(operation), {"successor_runtime": 1}
        result, receipt_digest = self._acknowledge(manifest, operation, execution, at)
        adapter_receipts.append({"action": "acknowledge", "digest": receipt_digest})
        stages.extend(
            [
                {"stage": "bindings", "outcome": "completed"},
                {"stage": "acknowledge", "outcome": "completed"},
            ]
        )
        return result, None

    def _commit_stage(
        self,
        manifest: Mapping[str, Any],
        operation: Mapping[str, Any],
        at: str,
        stages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self.store.commit_rollover(
            manifest["operation_id"],
            manifest["expected_owner_version"],
            manifest["expected_owner_fence"],
            manifest["owner_event_id"],
            manifest["owner_outbox_id"],
            at,
        )
        stages.append(
            {
                "stage": "commit",
                "outcome": "idempotent" if result.get("idempotent") else "completed",
            }
        )
        return result

    def _drain_stage(
        self,
        manifest: Mapping[str, Any],
        operation: Mapping[str, Any],
        at: str,
        stages: list[dict[str, Any]],
        adapter_receipts: list[dict[str, str]],
    ) -> tuple[dict[str, Any], Optional[dict[str, int]]]:
        execution = self._context(manifest)
        pending = dict(execution["pending_obligations"])
        if any(pending.values()):
            stages.append({"stage": "drain", "outcome": "obligations_pending"})
            return dict(operation), pending
        cleanup, receipt_digest = self._cleanup(
            "drain", "predecessor", manifest, operation, execution, at
        )
        adapter_receipts.append({"action": "drain", "digest": receipt_digest})
        result = self.store.complete_rollover_drain(
            manifest["operation_id"], operation["version"], cleanup, at
        )
        stages.append({"stage": "drain", "outcome": "completed"})
        return result, None

    def run(
        self,
        manifest_value: Mapping[str, Any],
        *,
        at: str,
        abort: bool = False,
    ) -> dict[str, Any]:
        manifest = validate_run_manifest(manifest_value)
        supplied_adapter_digest = manifest["plan"].get("provider_adapter_digest")
        if supplied_adapter_digest is not None and supplied_adapter_digest != self.adapters.digest:
            raise StorageRefusal(
                "rollover_adapter_config_mismatch",
                "configured provider adapters do not match the durable handoff plan",
            )
        manifest["plan"]["provider_adapter_digest"] = self.adapters.digest
        for participant in ("predecessor", "successor"):
            key = f"{participant}_runtime_instance_id"
            supplied_runtime_id = manifest["plan"].get(key)
            if supplied_runtime_id is not None and supplied_runtime_id != manifest[key]:
                raise StorageRefusal(
                    "rollover_runtime_mismatch",
                    f"{participant} runtime differs from the durable handoff plan",
                )
            manifest["plan"][key] = manifest[key]
        existing = self.store.rollover_status(manifest["operation_id"])
        stages: list[dict[str, Any]] = [
            {"stage": "status", "outcome": "found" if existing is not None else "not_found"}
        ]
        adapter_receipts: list[dict[str, str]] = []
        operation = self.store.prepare_rollover(
            manifest["operation_id"],
            manifest["squad_id"],
            manifest["predecessor_agent_id"],
            manifest["successor_agent_id"],
            manifest["callsign_assignment_id"],
            manifest["expected_owner_version"],
            manifest["expected_owner_fence"],
            manifest["authority_kind"],
            manifest["authority_digest"],
            manifest["required_capabilities"],
            manifest["plan"],
            at,
        )
        self._assert_identity(operation, manifest)
        stages.append(
            {
                "stage": "prepare",
                "outcome": "idempotent" if operation.get("idempotent") else "completed",
            }
        )
        if operation["state"] in {"completed", "aborted"}:
            return self._result(operation, stages, adapter_receipts, None)
        execution = self._context(manifest)
        predecessor_runtime = execution["predecessor_runtime"]
        if not predecessor_runtime["verified"] or (
            operation["state"] in {"prepared", "acknowledged"}
            and predecessor_runtime["status"] != "active"
        ):
            raise StorageRefusal(
                "rollover_runtime_mismatch",
                "predecessor is not the exact active verified Shotcaller runtime",
            )
        self.adapters.require(str(predecessor_runtime["harness_kind"]))
        successor_runtime = execution["successor_runtime"]
        if successor_runtime is not None:
            self.adapters.require(str(successor_runtime["harness_kind"]))
        if abort:
            operation = self._abort_stage(
                manifest, operation, execution, at, stages, adapter_receipts
            )
            return self._result(operation, stages, adapter_receipts, None)
        if operation["state"] == "prepared":
            operation, pending = self._acknowledgement_stage(
                manifest, operation, execution, at, stages, adapter_receipts
            )
            if pending is not None:
                return self._result(operation, stages, adapter_receipts, pending)
        if operation["state"] == "acknowledged":
            operation = self._commit_stage(manifest, operation, at, stages)
        if operation["state"] == "switched":
            operation, pending = self._drain_stage(
                manifest, operation, at, stages, adapter_receipts
            )
            if pending is not None:
                return self._result(operation, stages, adapter_receipts, pending)
        return self._result(operation, stages, adapter_receipts, None)

    def _result(
        self,
        operation: Mapping[str, Any],
        stages: list[dict[str, Any]],
        adapter_receipts: list[dict[str, str]],
        pending: Optional[Mapping[str, int]],
    ) -> dict[str, Any]:
        next_action = {
            "prepared": "launch or reconcile the exact successor runtime, then retry",
            "acknowledged": "retry the atomic owner switch",
            "switched": "satisfy predecessor obligations, then retry drain",
            "completed": "preserve rollover history and continue with the successor",
            "aborted": "preserve abort evidence; predecessor remains owner",
        }[str(operation["state"])]
        return {
            "schema": "league.rollover-run.v1",
            "operation": dict(operation),
            "stages": stages,
            "adapter_receipts": adapter_receipts,
            "provider_adapter_digest": self.adapters.digest,
            "pending_obligations": dict(pending or {}),
            "next_action": next_action,
        }


__all__ = [
    "ADAPTER_CONFIG_SCHEMA",
    "ConfiguredProviderAdapters",
    "ShotcallerRolloverRunner",
    "SubprocessAdapterCommandRunner",
    "validate_run_manifest",
]
