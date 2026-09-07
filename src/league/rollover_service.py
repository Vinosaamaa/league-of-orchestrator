"""Thin, recoverable orchestration of the existing durable rollover stages.

Successor acknowledgement is explicit, never inferred from reading bindings.
Launch, descendant/intake reconciliation and cleanup retain their own gates.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .sqlite_callsign_ops import capabilities, timestamp
from .sqlite_rollover_ops import _handoff_plan
from .storage import Storage, StorageRefusal


RUN_SCHEMA = "league.shotcaller-rollover-run.v1"
RUN_KEYS = {
    "schema", "operation_id", "squad_id", "predecessor_agent_id",
    "successor_agent_id", "predecessor_runtime_instance_id",
    "successor_runtime_instance_id", "callsign_assignment_id",
    "expected_owner_version", "expected_owner_fence", "authority_kind",
    "authority_digest", "required_capabilities", "plan", "owner_event_id",
    "owner_outbox_id",
}


def validate_run_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != RUN_KEYS or value.get("schema") != RUN_SCHEMA:
        raise StorageRefusal("rollover_run_invalid", "rollover run manifest shape is invalid")
    for key in RUN_KEYS - {
        "schema", "expected_owner_version", "expected_owner_fence",
        "required_capabilities", "plan",
    }:
        item = value[key]
        if (
            not isinstance(item, str) or not item or item.strip() != item
            or len(item.encode("utf-8")) > 4096
            or any(ord(character) < 32 for character in item)
        ):
            raise StorageRefusal("rollover_run_invalid", "rollover identity is invalid")
    for key in ("expected_owner_version", "expected_owner_fence"):
        if type(value[key]) is not int or value[key] < 1:
            raise StorageRefusal("rollover_run_invalid", "owner version and fence must be positive")
    if value["authority_kind"] != "explicit":
        raise StorageRefusal(
            "rollover_authority_required",
            "run requires explicit authority; mode authority uses the protected staged command",
        )
    if value["predecessor_agent_id"] == value["successor_agent_id"]:
        raise StorageRefusal("rollover_run_invalid", "rollover participants must differ")
    if not isinstance(value["required_capabilities"], list) or not isinstance(value["plan"], Mapping):
        raise StorageRefusal("rollover_run_invalid", "rollover plan or capabilities are invalid")
    capabilities(value["required_capabilities"])
    _handoff_plan(value["plan"], value["squad_id"])
    return dict(value)


class ShotcallerRolloverRunner:
    def __init__(self, store: Storage) -> None:
        self.store = store

    def run(
        self,
        manifest_value: Mapping[str, Any],
        *,
        at: str,
        pages: Optional[Mapping[str, Any]] = None,
        abort_receipt: Optional[Mapping[str, Any]] = None,
        drain_receipt: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        manifest = validate_run_manifest(manifest_value)
        timestamp(at, "rollover run observation time")
        if sum(item is not None for item in (pages, abort_receipt, drain_receipt)) > 1:
            raise StorageRefusal("rollover_run_invalid", "supply only one staged receipt per invocation")
        if pages is not None and (set(pages) != {"pages"} or not isinstance(pages["pages"], list)):
            raise StorageRefusal("invalid_pages", "pages must contain the existing page receipt list")
        context = self.store.rollover_run_context(manifest)
        operation = context["operation"]
        stages = ["status"]
        if operation is None:
            if any(item is not None for item in (pages, abort_receipt, drain_receipt)):
                raise StorageRefusal("rollover_unknown", "prepare before submitting a staged receipt")
            operation = self.store.prepare_rollover(
                *(manifest[key] for key in (
                    "operation_id", "squad_id", "predecessor_agent_id", "successor_agent_id",
                    "callsign_assignment_id", "expected_owner_version", "expected_owner_fence",
                    "authority_kind", "authority_digest", "required_capabilities", "plan",
                )), at,
            )
            stages.append("prepare")
        operation_id = manifest["operation_id"]
        if drain_receipt is not None and operation["state"] not in {"switched", "completed"}:
            raise StorageRefusal("rollover_conflict", "drain requires an already switched operation")
        if abort_receipt is not None:
            operation = self.store.abort_rollover(
                operation_id, operation["version"], abort_receipt, at
            )
            stages.append("abort")
        else:
            if operation["state"] == "prepared" and pages is not None:
                self.store.startup_context(
                    manifest["successor_agent_id"], manifest["successor_runtime_instance_id"], at
                )
                snapshot = operation["snapshot"]
                operation = self.store.acknowledge_rollover(
                    operation_id, manifest["successor_agent_id"], manifest["successor_runtime_instance_id"],
                    operation["handoff_digest"], snapshot["version"], snapshot["count"],
                    snapshot["digest"], pages["pages"], at,
                )
                stages.append("acknowledge")
            if operation["state"] == "acknowledged":
                operation = self.store.commit_rollover(
                    operation_id, manifest["expected_owner_version"], manifest["expected_owner_fence"],
                    manifest["owner_event_id"], manifest["owner_outbox_id"], at,
                )
                stages.append("commit")
            if drain_receipt is not None:
                operation = self.store.complete_rollover_drain(
                    operation_id, operation["version"], drain_receipt, at
                )
                stages.append("drain")
        bindings = None
        if operation["state"] == "prepared":
            # One bounded immutable page, never an unbounded reconstructed handoff.
            bindings = self.store.rollover_bindings(operation_id, at)
            stages.append("bindings")
        next_actions = {
            "prepared": ["rollover.bindings", "agent.startup-context", "rollover.acknowledge"],
            "acknowledged": ["rollover.run"],
            "switched": [
                "rollover.reconcile-descendant", "rollover.intake-plan",
                "rollover.reconcile-intake", "cleanup.plan", "cleanup.execute",
            ],
            "aborted": [],
            "completed": [],
        }[operation["state"]]
        return {
            "schema": "league.rollover-run.v1", "operation": operation,
            "stages": stages, "bindings": bindings, "next_actions": next_actions,
        }
