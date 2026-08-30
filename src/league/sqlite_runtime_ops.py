"""SQLite operations for adapter bindings, routing evidence, and cleanup."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Optional

from .cleanup import require_cleanup_task_disposition, select_cleanup_policy
from .storage_types import StorageRefusal


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def _time(value: str, label: str, code: str = "cleanup_lease_invalid") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal(code, f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal(code, f"{label} must include a UTC offset")
    return parsed


def runtime_cleanup_identity(
    store: Any,
    runtime_instance_id: str,
    endpoint_identity: str,
    runtime_generation: str,
) -> dict[str, Any]:
    """Return one exact runtime row or refuse a stale cleanup identity."""

    if not all((runtime_instance_id, endpoint_identity, runtime_generation)):
        raise StorageRefusal("cleanup_identity_mismatch", "runtime cleanup identity is incomplete")
    runtime = store.connection.execute(
        "SELECT endpoint,runtime_generation,status,verified FROM runtime_instances WHERE runtime_instance_id=?",
        (runtime_instance_id,),
    ).fetchone()
    if runtime is None:
        raise StorageRefusal("cleanup_identity_mismatch", "runtime cleanup identity is missing")
    if (
        runtime["endpoint"] != endpoint_identity
        or runtime["runtime_generation"] != runtime_generation
    ):
        raise StorageRefusal("cleanup_identity_mismatch", "runtime cleanup identity changed")
    return dict(runtime)


def register_runtime_binding(
    store: Any,
    binding_id: str,
    task_id: str,
    harness_kind: str,
    backend_kind: str,
    session_identity: str,
    endpoint_identity: str,
    endpoint_generation: str,
    capabilities: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    values = (
        binding_id,
        task_id,
        harness_kind,
        backend_kind,
        session_identity,
        endpoint_identity,
        endpoint_generation,
        _json(capabilities),
        at,
    )
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM runtime_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
            if existing is not None:
                expected = values[:-1]
                observed = tuple(existing[key] for key in (
                    "binding_id", "task_id", "harness_kind", "backend_kind",
                    "session_identity", "endpoint_identity", "endpoint_generation", "capabilities_json"
                ))
                if observed != expected:
                    raise StorageRefusal("binding_conflict", "runtime binding identity conflicts")
                return {"binding_id": binding_id, "version": int(existing["version"]), "idempotent": True}
            store.connection.execute(
                """
                INSERT INTO runtime_bindings
                  (binding_id,task_id,harness_kind,backend_kind,session_identity,endpoint_identity,
                   endpoint_generation,capabilities_json,state,version,created_at,updated_at,last_receipt_json)
                VALUES(?,?,?,?,?,?,?,?,'active',1,?,?, '{}')
                """,
                (*values, at),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "runtime binding conflicted with canonical state") from exc
    return {"binding_id": binding_id, "version": 1, "idempotent": False}


def close_runtime_for_cleanup(
    store: Any,
    runtime_instance_id: str,
    endpoint_identity: str,
    runtime_generation: str,
    at: str,
) -> dict[str, Any]:
    _time(at, "runtime cleanup close time")
    try:
        with store._transaction():
            runtime = runtime_cleanup_identity(
                store, runtime_instance_id, endpoint_identity, runtime_generation
            )
            if runtime["status"] == "closed" and not bool(runtime["verified"]):
                return {
                    "runtime_instance_id": runtime_instance_id,
                    "status": "closed",
                    "idempotent": True,
                }
            if runtime["status"] not in {"active", "idle", "closed"}:
                raise StorageRefusal("cleanup_identity_mismatch", "runtime is not cleanup-eligible")
            store.connection.execute(
                "UPDATE runtime_instances SET status='closed',verified=0,last_seen_at=? WHERE runtime_instance_id=?",
                (at, runtime_instance_id),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "runtime cleanup close conflicted with canonical state"
        ) from exc
    return {
        "runtime_instance_id": runtime_instance_id,
        "status": "closed",
        "idempotent": False,
    }


def runtime_binding(store: Any, binding_id: str) -> Optional[dict[str, Any]]:
    value = _row(store.connection.execute("SELECT * FROM runtime_bindings WHERE binding_id=?", (binding_id,)).fetchone())
    if value is not None:
        value["capabilities"] = json.loads(value.pop("capabilities_json"))
        value["last_receipt"] = json.loads(value.pop("last_receipt_json"))
    return value


def update_runtime_binding(
    store: Any,
    binding_id: str,
    expected_version: int,
    state: str,
    at: str,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    if state not in {"active", "idle", "interrupted", "closing", "closed", "failed"}:
        raise StorageRefusal("binding_state_invalid", "runtime binding state is unsupported")
    next_version = expected_version + 1
    try:
        with store._transaction():
            changed = store.connection.execute(
                """
                UPDATE runtime_bindings SET state=?,version=?,updated_at=?,last_receipt_json=?
                 WHERE binding_id=? AND version=?
                """,
                (state, next_version, at, _json(receipt), binding_id, expected_version),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("version_conflict", "runtime binding expected-version precondition failed")
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "runtime binding update failed") from exc
    return {"binding_id": binding_id, "state": state, "version": next_version}


def claim_runtime_exit(
    store: Any,
    binding_id: str,
    expected_version: int,
    expected_fence: int,
    executor_id: str,
    leased_until: str,
    at: str,
) -> dict[str, Any]:
    if not executor_id:
        raise StorageRefusal("runtime_exit_lease_invalid", "runtime exit executor is required")
    claim_time = _time(at, "runtime exit claim time", "runtime_exit_lease_invalid")
    try:
        with store._transaction():
            current = store.connection.execute(
                "SELECT state,version,exit_fence,exit_leased_until FROM runtime_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if current is None:
                raise StorageRefusal("binding_unknown", "runtime binding does not exist")
            if int(current["version"]) != expected_version or int(current["exit_fence"]) != expected_fence:
                raise StorageRefusal("version_conflict", "runtime exit claim precondition changed")
            if current["state"] == "closed":
                return {
                    "binding_id": binding_id,
                    "state": "closed",
                    "version": expected_version,
                    "fence": expected_fence,
                    "idempotent": True,
                }
            lease_expiry = _time(
                leased_until, "runtime exit lease expiry", "runtime_exit_lease_invalid"
            )
            if lease_expiry <= claim_time:
                raise StorageRefusal(
                    "runtime_exit_lease_invalid",
                    "runtime exit lease expiry must be after claim time",
                )
            if (
                current["state"] == "closing"
                and current["exit_leased_until"] is not None
                and _time(
                    current["exit_leased_until"],
                    "stored runtime exit lease expiry",
                    "runtime_exit_lease_invalid",
                )
                > claim_time
            ):
                raise StorageRefusal(
                    "runtime_exit_busy",
                    "runtime exit has an unexpired executor lease",
                    retryable=True,
                )
            next_version = expected_version + 1
            next_fence = expected_fence + 1
            changed = store.connection.execute(
                """
                UPDATE runtime_bindings
                   SET state='closing',version=?,exit_fence=?,exit_executor_id=?,exit_leased_until=?,updated_at=?
                 WHERE binding_id=? AND version=? AND exit_fence=?
                """,
                (
                    next_version,
                    next_fence,
                    executor_id,
                    leased_until,
                    at,
                    binding_id,
                    expected_version,
                    expected_fence,
                ),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("version_conflict", "runtime exit claim lost its fence")
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "runtime exit claim failed") from exc
    return {
        "binding_id": binding_id,
        "state": "closing",
        "version": next_version,
        "fence": next_fence,
        "idempotent": False,
    }


def finalize_runtime_exit(
    store: Any,
    binding_id: str,
    expected_version: int,
    fence: int,
    at: str,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    next_version = expected_version + 1
    try:
        with store._transaction():
            changed = store.connection.execute(
                """
                UPDATE runtime_bindings
                   SET state='closed',version=?,updated_at=?,last_receipt_json=?,
                       exit_executor_id=NULL,exit_leased_until=NULL
                 WHERE binding_id=? AND state='closing' AND version=? AND exit_fence=?
                """,
                (next_version, at, _json(receipt), binding_id, expected_version, fence),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("runtime_exit_fence_conflict", "runtime exit finalization fence changed")
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "runtime exit finalization failed") from exc
    return {
        "binding_id": binding_id,
        "state": "closed",
        "version": next_version,
        "fence": fence,
        "idempotent": False,
    }


def record_routing_decision(store: Any, decision: Mapping[str, Any]) -> dict[str, Any]:
    columns = (
        "decision_id", "subject_kind", "subject_id", "role", "tier", "model", "effort", "reason",
        "explicit_model", "explicit_effort", "state", "escalation_count", "prior_decision_id",
        "failure_class", "chosen_at", "provider", "provider_config_version", "policy_version", "reason_code",
        "explicit_provider", "operator_override_id", "fallback_from_provider",
        "required_capabilities_json", "signals_json",
    )
    normalized = tuple(
        int(bool(decision.get(column)))
        if column in {"explicit_model", "explicit_effort", "explicit_provider"}
        else decision.get(column)
        for column in columns
    )
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM model_routing_decisions WHERE decision_id=?", (decision["decision_id"],)
            ).fetchone()
            if existing is not None:
                if tuple(existing[column] for column in columns) != normalized:
                    raise StorageRefusal("routing_decision_conflict", "routing decision id has different evidence")
                result = dict(existing)
                result["idempotent"] = True
                return result
            if decision.get("prior_decision_id") is not None:
                child = store.connection.execute(
                    "SELECT decision_id FROM model_routing_decisions WHERE prior_decision_id=?",
                    (decision["prior_decision_id"],),
                ).fetchone()
                if child is not None:
                    raise StorageRefusal(
                        "routing_escalation_conflict",
                        "prior routing decision already has an escalation child",
                    )
            store.connection.execute(
                f"INSERT INTO model_routing_decisions({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                normalized,
            )
    except StorageRefusal:
        raise
    except sqlite3.IntegrityError as exc:
        prior_decision_id = decision.get("prior_decision_id")
        child = None
        if prior_decision_id is not None:
            child = store.connection.execute(
                "SELECT decision_id FROM model_routing_decisions WHERE prior_decision_id=?",
                (prior_decision_id,),
            ).fetchone()
        if child is not None:
            raise StorageRefusal(
                "routing_escalation_conflict",
                "prior routing decision already has an escalation child",
            ) from exc
        raise store._translate_database_error(exc, "routing decision conflicted with canonical state") from exc
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "routing decision conflicted with canonical state") from exc
    result = dict(zip(columns, normalized))
    result["idempotent"] = False
    return result


def routing_decision(store: Any, decision_id: str) -> Optional[dict[str, Any]]:
    return _row(store.connection.execute(
        "SELECT * FROM model_routing_decisions WHERE decision_id=?", (decision_id,)
    ).fetchone())


def record_routing_outcome(store: Any, outcome: Mapping[str, Any]) -> dict[str, Any]:
    columns = ("outcome_id", "decision_id", "success", "corrections", "latency_ms", "cost_microunits", "recorded_at")
    values = tuple(outcome.get(column) for column in columns)
    normalized = values[:2] + (int(bool(values[2])),) + values[3:]
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM model_routing_outcomes WHERE outcome_id=?",
                (outcome["outcome_id"],),
            ).fetchone()
            if existing is not None:
                if tuple(existing[column] for column in columns) != normalized:
                    raise StorageRefusal(
                        "routing_outcome_conflict",
                        "routing outcome id has different evidence",
                    )
                result = dict(existing)
                result["idempotent"] = True
                return result
            store.connection.execute(
                f"INSERT INTO model_routing_outcomes({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                normalized,
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "routing outcome conflicted with canonical state") from exc
    result = dict(zip(columns, normalized))
    result["idempotent"] = False
    return result


RESOURCE_COMPARE_KEYS = (
    "task_id",
    "owner_id",
    "owner_role",
    "resource_type",
    "lifetime",
    "expected_identity_json",
    "cleanup_action",
    "adapter_kind",
    "applicable",
    "applicability_reason",
)


def _register_task_resource_row(
    store: Any, resource: Mapping[str, Any], at: str
) -> dict[str, Any]:
    encoded_identity = _json(resource["expected_identity"])
    existing = store.connection.execute(
        "SELECT * FROM task_resources WHERE resource_id=?", (resource["resource_id"],)
    ).fetchone()
    comparable = (
        resource["task_id"],
        resource["owner_id"],
        resource["owner_role"],
        resource["resource_type"],
        resource["lifetime"],
        encoded_identity,
        resource["cleanup_action"],
        resource["adapter_kind"],
        int(resource["applicable"]),
        resource["applicability_reason"],
    )
    if existing is not None:
        if (
            tuple(existing[key] for key in RESOURCE_COMPARE_KEYS) != comparable
            or existing["state"] != "active"
        ):
            raise StorageRefusal("resource_conflict", "task resource identity conflicts")
        return {
            "resource_id": resource["resource_id"],
            "version": int(existing["version"]),
            "idempotent": True,
        }
    store.connection.execute(
        """
        INSERT INTO task_resources
          (resource_id,task_id,owner_id,owner_role,resource_type,lifetime,expected_identity_json,
           cleanup_action,adapter_kind,applicable,applicability_reason,state,version,registered_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',1,?,?)
        """,
        (resource["resource_id"], *comparable, at, at),
    )
    return {"resource_id": resource["resource_id"], "version": 1, "idempotent": False}


def register_task_resource(store: Any, resource: Mapping[str, Any], at: str) -> dict[str, Any]:
    try:
        with store._transaction():
            result = _register_task_resource_row(store, resource, at)
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "task resource conflicted with canonical state") from exc
    return result


def task_resources(store: Any, task_id: str) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for row in store.connection.execute(
        "SELECT * FROM task_resources WHERE task_id=? AND state='active' ORDER BY resource_id",
        (task_id,),
    ):
        resource = dict(row)
        resource["expected_identity"] = json.loads(resource.pop("expected_identity_json"))
        resource["applicable"] = bool(resource["applicable"])
        resources.append(resource)
    return resources


def plan_cleanup(store: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    try:
        with store._transaction():
            task = store.connection.execute(
                "SELECT state FROM tasks WHERE task_id=?", (plan["task_id"],)
            ).fetchone()
            if task is None:
                raise StorageRefusal(
                    "cleanup_owner_refused",
                    "cleanup owner is not the exact terminal task owner",
                )
            require_cleanup_task_disposition(
                str(task["state"]), str(plan["disposition"])
            )
            obligation = store.connection.execute(
                "SELECT * FROM cleanup_obligations WHERE task_id=?", (plan["task_id"],)
            ).fetchone()
            expected = int(plan["expected_cleanup_version"])
            if obligation is None:
                if expected != 0:
                    raise StorageRefusal("version_conflict", "cleanup obligation does not match expected version")
                obligation_id = f"cleanup:{plan['task_id']}"
                store.connection.execute(
                    """
                    INSERT INTO cleanup_obligations
                      (cleanup_obligation_id,task_id,owner_id,task_class,disposition,cleanup_state,
                       required_policy,next_action,version,updated_at)
                    VALUES(?,?,?,?,?,'cleanup_pending',?,'Execute the verified cleanup plan.',1,?)
                    """,
                    (obligation_id, plan["task_id"], plan["owner_id"], plan["task_class"],
                     plan["disposition"], plan["required_policy"], plan["at"]),
                )
                cleanup_revision = 1
            else:
                obligation_id = str(obligation["cleanup_obligation_id"])
                if int(obligation["version"]) != expected:
                    raise StorageRefusal("version_conflict", "cleanup obligation expected-version precondition failed")
                if obligation["cleanup_state"] in {"completed", "cleanup_completed"}:
                    raise StorageRefusal("cleanup_completed", "cleanup obligation is already complete")
                if obligation["owner_id"] is None:
                    cleanup_revision = expected + 1
                    store.connection.execute(
                        """
                        UPDATE cleanup_obligations
                           SET owner_id=?,task_class=?,disposition=?,cleanup_state='cleanup_pending',
                               required_policy=?,next_action='Execute the verified cleanup plan.',
                               version=?,updated_at=?
                         WHERE cleanup_obligation_id=? AND version=? AND owner_id IS NULL
                        """,
                        (
                            plan["owner_id"],
                            plan["task_class"],
                            plan["disposition"],
                            plan["required_policy"],
                            cleanup_revision,
                            plan["at"],
                            obligation_id,
                            expected,
                        ),
                    )
                else:
                    if obligation["required_policy"] != plan["required_policy"]:
                        raise StorageRefusal("cleanup_policy_conflict", "cleanup policy changed for an existing obligation")
                    if (
                        obligation["owner_id"] != plan["owner_id"]
                        or obligation["task_class"] != plan["task_class"]
                        or obligation["disposition"] != plan["disposition"]
                    ):
                        raise StorageRefusal(
                            "cleanup_identity_mismatch",
                            "cleanup obligation ownership or disposition changed",
                        )
                    cleanup_revision = expected
            existing = store.connection.execute(
                "SELECT * FROM cleanup_operations WHERE cleanup_obligation_id=? AND cleanup_revision=?",
                (obligation_id, cleanup_revision),
            ).fetchone()
            if existing is not None:
                if existing["operation_id"] != plan["operation_id"] or existing["plan_digest"] != plan["plan_digest"]:
                    raise StorageRefusal("cleanup_claim_conflict", "cleanup revision is already claimed")
                return {"operation_id": plan["operation_id"], "fence": int(existing["fence"]), "idempotent": True}
            planned_resources = {
                resource["resource_id"]: resource for resource in plan.get("resources", [])
            }
            active_resource_ids = {
                row["resource_id"]
                for row in store.connection.execute(
                    "SELECT resource_id FROM task_resources WHERE task_id=? AND state='active'",
                    (plan["task_id"],),
                )
            }
            omitted = active_resource_ids - set(planned_resources)
            if omitted:
                raise StorageRefusal(
                    "resource_proof_missing",
                    "active canonical task resource is absent from the cleanup plan",
                )
            for resource in planned_resources.values():
                _register_task_resource_row(store, resource, plan["at"])
            store.connection.execute(
                """
                INSERT INTO cleanup_operations
                  (operation_id,cleanup_obligation_id,cleanup_revision,plan_digest,state,fence,
                   executor_id,leased_until,created_at,updated_at)
                VALUES(?,?,?,?, 'planned',0,NULL,NULL,?,?)
                """,
                (plan["operation_id"], obligation_id, cleanup_revision, plan["plan_digest"], plan["at"], plan["at"]),
            )
            registered_resources = {
                row["resource_id"]: row["task_id"]
                for row in store.connection.execute(
                    "SELECT resource_id,task_id FROM task_resources WHERE task_id=? AND state='active'",
                    (plan["task_id"],),
                )
            }
            for action in plan["actions"]:
                if action["resource_id"] is not None:
                    if registered_resources.get(action["resource_id"]) != plan["task_id"]:
                        raise StorageRefusal("resource_identity_mismatch", "cleanup resource is not registered to the task")
                store.connection.execute(
                    """
                    INSERT INTO cleanup_actions
                      (action_id,operation_id,ordinal,action_kind,adapter_kind,resource_id,state,
                       expected_identity_json,intended_state_json)
                    VALUES(?,?,?,?,?,?, 'planned',?,?)
                    """,
                    (action["action_id"], plan["operation_id"], action["ordinal"], action["action_kind"],
                     action["adapter_kind"], action["resource_id"], _json(action["expected_identity"]),
                     _json(action["intended_state"])),
                )
            from .sqlite_continuation_ops import record_thread_archive_for_cleanup

            record_thread_archive_for_cleanup(store, plan)
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "cleanup plan conflicted with canonical state") from exc
    return {"operation_id": plan["operation_id"], "fence": 0, "idempotent": False, "state": "cleanup_pending"}


def cleanup_operation(store: Any, operation_id: str) -> Optional[dict[str, Any]]:
    operation = _row(store.connection.execute(
        "SELECT * FROM cleanup_operations WHERE operation_id=?", (operation_id,)
    ).fetchone())
    if operation is None:
        return None
    receipts = {
        row["action_id"]: {
            "outcome": row["outcome"],
            "receipt_hash": row["receipt_hash"],
            "recorded_at": row["recorded_at"],
        }
        for row in store.connection.execute(
            """
            SELECT action_id,outcome,receipt_hash,recorded_at
              FROM cleanup_action_receipts WHERE operation_id=?
            """,
            (operation_id,),
        )
    }
    actions = []
    for row in store.connection.execute(
        "SELECT * FROM cleanup_actions WHERE operation_id=? ORDER BY ordinal", (operation_id,)
    ):
        action = dict(row)
        action["expected_identity"] = json.loads(action.pop("expected_identity_json"))
        action["intended_state"] = json.loads(action.pop("intended_state_json"))
        action["receipt"] = receipts.get(action["action_id"])
        actions.append(action)
    operation["actions"] = actions
    final = store.connection.execute(
        "SELECT receipt_id,policy_version,receipt_hash,completed_at FROM teardown_receipts WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    operation["final_receipt"] = None if final is None else dict(final)
    return operation


def _validate_cleanup_resources(
    resources: list[dict[str, Any]], archived_resources: Any, actions: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Prove the registered-resource snapshot and its exact action bindings."""

    if not isinstance(archived_resources, list) or any(
        not isinstance(resource, dict) for resource in archived_resources
    ):
        raise StorageRefusal(
            "cleanup_operation_invalid",
            "cleanup plan has no immutable registered-resource snapshot",
        )
    resource_fields = {
        "resource_id",
        "task_id",
        "owner_id",
        "owner_role",
        "resource_type",
        "lifetime",
        "expected_identity",
        "cleanup_action",
        "adapter_kind",
        "applicable",
        "applicability_reason",
    }
    canonical_resources = {
        resource["resource_id"]: {key: resource[key] for key in resource_fields}
        for resource in resources
    }
    canonical_states = {
        resource["resource_id"]: resource["state"] for resource in resources
    }
    archived_by_id = {
        resource.get("resource_id"): resource for resource in archived_resources
    }
    if (
        len(archived_by_id) != len(archived_resources)
        or set(canonical_resources) != set(archived_by_id)
        or any(
            set(archived) != resource_fields
            or canonical_resources[resource_id] != archived
            for resource_id, archived in archived_by_id.items()
        )
    ):
        raise StorageRefusal(
            "cleanup_resource_changed",
            "registered cleanup resources changed after the immutable plan was created",
        )
    resource_actions: dict[str, list[dict[str, Any]]] = {
        resource_id: [] for resource_id in canonical_resources
    }
    for action in actions:
        resource_id = action.get("resource_id")
        if resource_id is not None:
            if resource_id not in resource_actions:
                raise StorageRefusal(
                    "cleanup_resource_changed",
                    "cleanup action refers to an unregistered task resource",
                )
            resource_actions[resource_id].append(action)
    for resource_id, resource in canonical_resources.items():
        expected_count = 0 if resource["lifetime"] == "persistent_retain" else 1
        matching = resource_actions[resource_id]
        if (
            len(matching) != expected_count
            or resource["applicable"] is not True
            or canonical_states[resource_id] == "stale"
            or any(
                action["action_kind"] != resource["cleanup_action"]
                or action["adapter_kind"] != resource["adapter_kind"]
                or action["expected_identity"] != resource["expected_identity"]
                for action in matching
            )
        ):
            raise StorageRefusal(
                "cleanup_resource_changed",
                "cleanup resource action, applicability, or identity changed",
            )
    return resource_actions


def cleanup_execution_context(store: Any, operation_id: str) -> dict[str, Any]:
    """Load one exact execution context exclusively from canonical SQLite rows."""

    operation = cleanup_operation(store, operation_id)
    if operation is None:
        raise StorageRefusal("cleanup_operation_unknown", "cleanup operation does not exist")
    row = store.connection.execute(
        """
        SELECT c.*,t.summary AS task_summary,t.state AS task_state,t.version AS task_version,
               t.current_owner_agent_id AS task_owner_agent_id,
               t.current_owner_squad_id AS task_owner_squad_id,
               a.role AS owner_role,a.task_id AS owner_task_id,a.status AS owner_status,
               a.retired_at AS owner_retired_at
          FROM cleanup_operations o
          JOIN cleanup_obligations c ON c.cleanup_obligation_id=o.cleanup_obligation_id
          JOIN tasks t ON t.task_id=c.task_id
          LEFT JOIN agent_instances a ON a.agent_id=c.owner_id
         WHERE o.operation_id=?
        """,
        (operation_id,),
    ).fetchone()
    if row is None:
        raise StorageRefusal("cleanup_operation_invalid", "cleanup operation lost its canonical task")
    actions = operation["actions"]
    if not actions or actions[0]["action_kind"] != "archive_identity_evidence":
        raise StorageRefusal("cleanup_archive_missing", "cleanup plan has no canonical archive action")
    archive = actions[0]["intended_state"]
    if not isinstance(archive, dict):
        raise StorageRefusal("cleanup_operation_invalid", "cleanup archive payload is malformed")
    expected_policy = select_cleanup_policy(str(row["task_class"]), str(row["disposition"]))
    owner = archive.get("owner")
    proof = archive.get("proof")
    if (
        archive.get("task_id") != row["task_id"]
        or archive.get("policy") != expected_policy
        or archive.get("pending_decisions_clear") is not True
        or not isinstance(owner, dict)
        or owner.get("id") != row["owner_id"]
        or not isinstance(proof, dict)
    ):
        raise StorageRefusal(
            "cleanup_operation_invalid",
            "cleanup task, policy, owner, decision, or proof no longer matches its canonical obligation",
        )
    missing = []
    for dotted in expected_policy["requirements"]:
        value: Any = proof
        for component in dotted.split("."):
            if not isinstance(value, dict) or component not in value:
                value = None
                break
            value = value[component]
        if value is not True:
            missing.append(dotted)
    if missing:
        raise StorageRefusal("cleanup_proof_missing", "canonical cleanup proof is incomplete")
    if owner.get("role") == "shotcaller":
        rollover = archive.get("rollover")
        if (
            not isinstance(rollover, dict)
            or row["owner_role"] != "shotcaller"
            or row["task_owner_agent_id"] != row["owner_id"]
        ):
            raise StorageRefusal("cleanup_owner_refused", "Shotcaller cleanup lost its rollover binding")
        observed_rollover = store.rollover_cleanup_target(str(rollover.get("operation_id", "")))
        if observed_rollover != rollover:
            raise StorageRefusal("cleanup_owner_refused", "rollover predecessor cleanup identity changed")
    else:
        if (
            row["owner_role"] != owner.get("role")
            or row["owner_task_id"] != row["task_id"]
            or (
                row["owner_retired_at"] is not None
                and operation["state"] != "completed"
            )
        ):
            raise StorageRefusal("cleanup_owner_refused", "cleanup owner is not the exact terminal task owner")
        require_cleanup_task_disposition(
            str(row["task_state"]), str(row["disposition"])
        )
    resources = []
    for resource in store.connection.execute(
        "SELECT * FROM task_resources WHERE task_id=? ORDER BY resource_id",
        (row["task_id"],),
    ):
        value = dict(resource)
        value["expected_identity"] = json.loads(value.pop("expected_identity_json"))
        value["applicable"] = bool(value["applicable"])
        resources.append(value)
    _validate_cleanup_resources(resources, archive.get("resources"), actions)
    immutable_actions = [
        {
            key: action[key]
            for key in (
                "action_id",
                "ordinal",
                "action_kind",
                "adapter_kind",
                "resource_id",
                "expected_identity",
                "intended_state",
            )
        }
        for action in actions
    ]
    expected_plan_digest = hashlib.sha256(
        _json({"policy": expected_policy, "actions": immutable_actions}).encode("utf-8")
    ).hexdigest()
    if operation["plan_digest"] != expected_plan_digest:
        raise StorageRefusal(
            "cleanup_operation_invalid",
            "cleanup action plan no longer matches its immutable digest",
        )
    runtime_bindings = [
        {
            **dict(runtime),
            "capabilities": json.loads(runtime["capabilities_json"]),
        }
        for runtime in store.connection.execute(
            "SELECT * FROM runtime_bindings WHERE task_id=? ORDER BY binding_id",
            (row["task_id"],),
        )
    ]
    runtime_instances = [
        dict(runtime)
        for runtime in store.connection.execute(
            "SELECT * FROM runtime_instances WHERE actor_agent_id=? ORDER BY runtime_instance_id",
            (row["owner_id"],),
        )
    ]
    return {
        "operation": operation,
        "obligation": dict(row),
        "task_identity": {
            "task_id": row["task_id"],
            "summary": row["task_summary"],
            "state": row["task_state"],
            "version": int(row["task_version"]),
            "owner_id": row["owner_id"],
            "owner_role": owner.get("role"),
            "owner_agent_id": row["task_owner_agent_id"],
            "owner_squad_id": row["task_owner_squad_id"],
        },
        "disposition": row["disposition"],
        "proof": proof,
        "resources": resources,
        "runtime_bindings": runtime_bindings,
        "runtime_instances": runtime_instances,
        "adapter_policy": [
            {
                "action_id": action["action_id"],
                "action_kind": action["action_kind"],
                "adapter_kind": action["adapter_kind"],
            }
            for action in actions
        ],
        "rollover": archive.get("rollover"),
    }


def claim_cleanup_operation(
    store: Any, operation_id: str, expected_fence: int, executor_id: str, leased_until: str, at: str
) -> dict[str, Any]:
    if not executor_id:
        raise StorageRefusal("cleanup_lease_invalid", "cleanup executor identity is required")
    claim_time = _time(at, "cleanup claim time")
    try:
        with store._transaction():
            current = store.connection.execute(
                "SELECT state,fence,leased_until FROM cleanup_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if current is None or int(current["fence"]) != expected_fence:
                raise StorageRefusal("cleanup_fence_conflict", "cleanup operation fence changed")
            if current["state"] == "completed":
                return {"operation_id": operation_id, "fence": expected_fence, "state": "completed", "idempotent": True}
            if current["state"] == "blocked":
                raise StorageRefusal("cleanup_blocked", "cleanup operation is durably blocked")
            lease_expiry = _time(leased_until, "cleanup lease expiry")
            if lease_expiry <= claim_time:
                raise StorageRefusal("cleanup_lease_invalid", "cleanup lease expiry must be after claim time")
            if (
                current["state"] == "executing"
                and current["leased_until"] is not None
                and _time(current["leased_until"], "stored cleanup lease expiry") > claim_time
            ):
                raise StorageRefusal(
                    "cleanup_busy",
                    "cleanup operation has an unexpired executor lease",
                    retryable=True,
                )
            next_fence = expected_fence + 1
            store.connection.execute(
                """
                UPDATE cleanup_operations SET state='executing',fence=?,executor_id=?,leased_until=?,updated_at=?
                 WHERE operation_id=? AND fence=?
                """,
                (next_fence, executor_id, leased_until, at, operation_id, expected_fence),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "cleanup operation claim failed") from exc
    return {"operation_id": operation_id, "fence": next_fence, "state": "executing", "idempotent": False}


def record_cleanup_action_receipt(
    store: Any,
    action_id: str,
    operation_id: str,
    fence: int,
    outcome: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    adapter_receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    payload = {"outcome": outcome, "before": before, "after": after, "adapter": adapter_receipt}
    digest = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    try:
        with store._transaction():
            operation = store.connection.execute(
                "SELECT state,fence FROM cleanup_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if operation is None or operation["state"] != "executing" or int(operation["fence"]) != fence:
                raise StorageRefusal("cleanup_fence_conflict", "cleanup action receipt has a stale fence")
            existing = store.connection.execute(
                "SELECT receipt_hash FROM cleanup_action_receipts WHERE action_id=?", (action_id,)
            ).fetchone()
            if existing is not None:
                if existing["receipt_hash"] != digest:
                    raise StorageRefusal("cleanup_receipt_conflict", "cleanup action already has another receipt")
                return {"action_id": action_id, "receipt_hash": digest, "idempotent": True}
            action = store.connection.execute(
                """
                SELECT state,resource_id,action_kind FROM cleanup_actions
                 WHERE action_id=? AND operation_id=?
                """,
                (action_id, operation_id),
            ).fetchone()
            if action is None or action["state"] not in {"planned", "executing"}:
                raise StorageRefusal("cleanup_action_conflict", "cleanup action is not receipt-eligible")
            store.connection.execute(
                """
                INSERT INTO cleanup_action_receipts
                  (action_id,operation_id,fence,outcome,before_json,after_json,adapter_receipt_json,receipt_hash,recorded_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (action_id, operation_id, fence, outcome, _json(before), _json(after), _json(adapter_receipt), digest, at),
            )
            store.connection.execute(
                "UPDATE cleanup_actions SET state='completed' WHERE action_id=?", (action_id,)
            )
            if action["resource_id"] is not None:
                state = "retained" if action["action_kind"] == "retain" else "released"
                store.connection.execute(
                    "UPDATE task_resources SET state=?,version=version+1,updated_at=? WHERE resource_id=?",
                    (state, at, action["resource_id"]),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "cleanup action receipt failed") from exc
    return {"action_id": action_id, "receipt_hash": digest, "idempotent": False}


def release_resource_lease_for_cleanup(
    store: Any, expected: Mapping[str, Any]
) -> dict[str, Any]:
    required = {
        "resource_id",
        "task_id",
        "owner_agent_id",
        "kind",
        "endpoint",
        "generation",
    }
    if set(expected) != required or any(
        not isinstance(expected.get(key), str) or not expected[key]
        for key in required
    ):
        raise StorageRefusal("cleanup_identity_mismatch", "shared lease identity is incomplete")
    try:
        with store._transaction():
            row = store.connection.execute(
                "SELECT * FROM resource_leases WHERE resource_id=?",
                (expected["resource_id"],),
            ).fetchone()
            if row is None or any(row[key] != expected[key] for key in required):
                raise StorageRefusal("cleanup_identity_mismatch", "shared lease identity changed")
            if row["state"] == "released":
                return {"resource_id": row["resource_id"], "state": "released", "idempotent": True}
            if row["state"] != "active":
                raise StorageRefusal("cleanup_identity_mismatch", "shared lease is not exactly active")
            store.connection.execute(
                "UPDATE resource_leases SET state='released' WHERE resource_id=? AND state='active'",
                (row["resource_id"],),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "shared lease release conflicted") from exc
    return {"resource_id": expected["resource_id"], "state": "released", "idempotent": False}


def resource_lease_for_cleanup(
    store: Any, resource_id: str
) -> Optional[dict[str, Any]]:
    row = store.connection.execute(
        "SELECT * FROM resource_leases WHERE resource_id=?", (resource_id,)
    ).fetchone()
    return None if row is None else dict(row)


def block_cleanup_operation(
    store: Any,
    operation_id: str,
    fence: int,
    action_id: Optional[str],
    refusal_code: str,
    receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    payload = {
        "operation_id": operation_id,
        "fence": fence,
        "action_id": action_id,
        "outcome": "blocked",
        "refusal_code": refusal_code,
        "receipt": dict(receipt),
    }
    digest = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    try:
        with store._transaction():
            operation = store.connection.execute(
                """
                SELECT o.*,c.task_id,c.version AS cleanup_version
                  FROM cleanup_operations o JOIN cleanup_obligations c
                    ON c.cleanup_obligation_id=o.cleanup_obligation_id
                 WHERE o.operation_id=?
                """,
                (operation_id,),
            ).fetchone()
            if operation is None or int(operation["fence"]) != fence:
                raise StorageRefusal("cleanup_fence_conflict", "cleanup block receipt has a stale fence")
            existing = store.connection.execute(
                "SELECT receipt_hash FROM teardown_receipts WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if operation["state"] == "blocked":
                if existing is None or existing["receipt_hash"] != digest:
                    raise StorageRefusal("cleanup_receipt_conflict", "cleanup block receipt changed")
                return {"operation_id": operation_id, "state": "blocked", "receipt_hash": digest, "idempotent": True}
            if operation["state"] != "executing":
                raise StorageRefusal("cleanup_fence_conflict", "cleanup operation is not blockable")
            if action_id is not None:
                action = store.connection.execute(
                    "SELECT state FROM cleanup_actions WHERE action_id=? AND operation_id=?",
                    (action_id, operation_id),
                ).fetchone()
                if action is None:
                    raise StorageRefusal("cleanup_action_conflict", "blocked cleanup action is unknown")
                if action["state"] in {"planned", "executing"}:
                    store.connection.execute(
                        "UPDATE cleanup_actions SET state='blocked' WHERE action_id=?",
                        (action_id,),
                    )
            store.connection.execute(
                "UPDATE cleanup_operations SET state='blocked',leased_until=NULL,updated_at=? WHERE operation_id=?",
                (at, operation_id),
            )
            store.connection.execute(
                """
                UPDATE cleanup_obligations
                   SET cleanup_state='blocked',next_action=?,version=version+1,updated_at=?
                 WHERE cleanup_obligation_id=?
                """,
                (f"Resolve cleanup refusal: {refusal_code}", at, operation["cleanup_obligation_id"]),
            )
            store.connection.execute(
                """
                INSERT INTO teardown_receipts
                  (receipt_id,operation_id,task_id,policy_version,receipt_hash,completed_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    f"cleanup-final:{operation_id}",
                    operation_id,
                    operation["task_id"],
                    f"blocked:{refusal_code}",
                    digest,
                    at,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "cleanup block receipt failed") from exc
    return {"operation_id": operation_id, "state": "blocked", "receipt_hash": digest, "idempotent": False}


def finalize_cleanup(store: Any, operation_id: str, fence: int, at: str) -> dict[str, Any]:
    try:
        with store._transaction():
            operation = store.connection.execute(
                """
                SELECT o.*,c.task_id,c.version AS cleanup_version,c.cleanup_state
                  FROM cleanup_operations o JOIN cleanup_obligations c
                    ON c.cleanup_obligation_id=o.cleanup_obligation_id
                 WHERE o.operation_id=?
                """,
                (operation_id,),
            ).fetchone()
            if operation is None or int(operation["fence"]) != fence:
                raise StorageRefusal("cleanup_fence_conflict", "cleanup finalization has a stale fence")
            if operation["state"] == "completed":
                receipt = store.connection.execute(
                    "SELECT receipt_hash FROM teardown_receipts WHERE operation_id=?", (operation_id,)
                ).fetchone()
                return {"operation_id": operation_id, "state": "cleanup_completed", "receipt_hash": receipt["receipt_hash"], "idempotent": True}
            pending = store.connection.execute(
                "SELECT COUNT(*) FROM cleanup_actions WHERE operation_id=? AND state!='completed'", (operation_id,)
            ).fetchone()[0]
            if pending:
                raise StorageRefusal("cleanup_incomplete", "cleanup action receipts are incomplete")
            first = store.connection.execute(
                "SELECT action_kind FROM cleanup_actions WHERE operation_id=? ORDER BY ordinal LIMIT 1", (operation_id,)
            ).fetchone()
            if first is None or first["action_kind"] != "archive_identity_evidence":
                raise StorageRefusal("cleanup_archive_missing", "identity archive receipt must precede release")
            receipts = [row[0] for row in store.connection.execute(
                "SELECT receipt_hash FROM cleanup_action_receipts WHERE operation_id=? ORDER BY action_id", (operation_id,)
            )]
            digest = hashlib.sha256(_json({"operation_id": operation_id, "receipts": receipts}).encode("utf-8")).hexdigest()
            receipt_id = f"teardown:{operation_id}"
            store.connection.execute(
                "INSERT INTO teardown_receipts(receipt_id,operation_id,task_id,policy_version,receipt_hash,completed_at) VALUES(?,?,?,?,?,?)",
                (receipt_id, operation_id, operation["task_id"], operation["plan_digest"], digest, at),
            )
            from .sqlite_continuation_ops import finalize_thread_archive_for_cleanup

            finalize_thread_archive_for_cleanup(store, operation_id, receipt_id, at)
            store.connection.execute(
                "UPDATE cleanup_operations SET state='completed',updated_at=? WHERE operation_id=?", (at, operation_id)
            )
            store.connection.execute(
                """
                UPDATE cleanup_obligations SET cleanup_state='cleanup_completed',next_action='None',
                       version=version+1,updated_at=? WHERE cleanup_obligation_id=?
                """,
                (at, operation["cleanup_obligation_id"]),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "cleanup finalization failed") from exc
    return {"operation_id": operation_id, "state": "cleanup_completed", "receipt_hash": digest, "idempotent": False}
