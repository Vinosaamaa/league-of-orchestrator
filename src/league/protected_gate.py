"""Carry one accepted autonomous grant through an exact protected command gate."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from .storage_mode import BeginProtectedGateCommand, SettleProtectedGateCommand
from .storage_types import StorageRefusal


def _result_digest(gate_name: str, outcome: str, value: Any) -> str:
    payload = json.dumps(
        {"gate_name": gate_name, "outcome": outcome, "value": value},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ProtectedGateExecutor:
    """Authorize, execute, and settle one protected gate without new authority."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def execute(
        self,
        *,
        gate_name: str,
        gate_scope: dict[str, Any],
        action: dict[str, Any],
        expected_goal_version: int,
        at: str,
        operation: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any]:
        begun = self.store.begin_protected_gate(
            BeginProtectedGateCommand(
                gate_name=gate_name,
                gate_scope=gate_scope,
                action=action,
                expected_goal_version=expected_goal_version,
                at=at,
            )
        )
        gate = begun["protected_gate"]
        if gate["outcome"] == "failed":
            raise StorageRefusal(
                "protected_gate_previously_failed",
                "protected gate retry names an already-failed exact action",
            )
        if gate["outcome"] == "succeeded":
            protected_gate = begun.pop("protected_gate")
            return {
                "operation": None,
                "mode_action": begun,
                "protected_gate": protected_gate,
            }
        try:
            operation_result = operation(begun)
        except Exception as exc:
            failure_class = (
                exc.code if isinstance(exc, StorageRefusal) else type(exc).__name__.lower()
            )
            failure_digest = _result_digest(
                gate_name, "failed", {"failure_class": failure_class}
            )
            try:
                self.store.settle_protected_gate(
                    SettleProtectedGateCommand(
                        action_use_id=gate["action_use_id"],
                        gate_name=gate_name,
                        gate_scope_digest=gate["gate_scope_digest"],
                        expected_goal_version=begun["goal_version_at_use"],
                        use_receipt_digest=gate["use_receipt_digest"],
                        outcome="failed",
                        result_receipt_digest=failure_digest,
                        failure_class=failure_class,
                        at=at,
                    )
                )
            except StorageRefusal as settlement_exc:
                raise StorageRefusal(
                    "protected_gate_settlement_failed",
                    "protected gate failed and its exact mode use could not be settled",
                ) from settlement_exc
            raise
        result_digest = _result_digest(gate_name, "succeeded", operation_result)
        settled = self.store.settle_protected_gate(
            SettleProtectedGateCommand(
                action_use_id=gate["action_use_id"],
                gate_name=gate_name,
                gate_scope_digest=gate["gate_scope_digest"],
                expected_goal_version=begun["goal_version_at_use"],
                use_receipt_digest=gate["use_receipt_digest"],
                outcome="succeeded",
                result_receipt_digest=result_digest,
                failure_class=None,
                at=at,
            )
        )
        protected_gate = settled.pop("protected_gate")
        return {
            "operation": operation_result,
            "mode_action": settled,
            "protected_gate": protected_gate,
        }


__all__ = ["ProtectedGateExecutor"]
