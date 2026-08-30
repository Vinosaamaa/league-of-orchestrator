"""CAS-safe refresh of one expired switched-rollover descendant snapshot."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable, Mapping, Optional, Sequence

from .rollover_descendant import _herdr_runtime_generation
from .sqlite_callsign_ops import capabilities, digest, stable_json, timestamp
from .sqlite_rollover_ops import (
    _descendant_source_shape,
    _descendant_reconciliation_receipt_exact,
    _historical_imported_descendant_reconciliation_receipt_exact,
    _operation,
    _runtime_capability_contract,
    _runtime_identity,
    _snapshot_digest,
    _snapshot_row_digest,
    _snapshot_rows,
)
from .storage_types import FaultInjector, StorageRefusal


def _event_id(refresh_id: str) -> str:
    if (
        not isinstance(refresh_id, str)
        or not 1 <= len(refresh_id) <= 120
        or re.fullmatch(r"[A-Za-z0-9_.:-]+", refresh_id) is None
    ):
        raise StorageRefusal(
            "invalid_snapshot_refresh", "snapshot refresh identity is invalid"
        )
    return f"rollover-snapshot-refresh:{refresh_id}"


def _snapshot_value(snapshot: Any) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "version": int(snapshot["snapshot_version"]),
        "count": int(snapshot["total_count"]),
        "page_bound": int(snapshot["page_bound"]),
        "expires_at": snapshot["expires_at"],
        "digest": snapshot["digest"],
    }


def _successor_progress(
    store: Any,
    operation: Mapping[str, Any],
    champion: Mapping[str, Any],
    task_id: str,
    callsign_assignment: Mapping[str, Any],
    runtime: Mapping[str, Any],
    required_capabilities: Sequence[str],
    runtime_capabilities: Sequence[str],
) -> dict[str, Any]:
    """Prove one already-transferred descendant from its immutable receipt."""

    events = store.connection.execute(
        """
        SELECT event_id,task_id,entity_version,event_type,detail_json,aggregate_kind,
               aggregate_id,source_event_id
          FROM events
         WHERE event_type='rollover_descendant_reconciled' AND task_id=?
         ORDER BY event_id
        """,
        (task_id,),
    ).fetchall()
    proofs: list[tuple[Any, dict[str, Any], str]] = []
    for event in events:
        try:
            detail = json.loads(event["detail_json"])
            receipt = detail["receipt"]
            receipt_digest = detail["receipt_digest"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if (
            isinstance(receipt, dict)
            and receipt.get("operation_id") == operation["operation_id"]
            and receipt.get("champion_agent_id") == champion["agent_id"]
            and receipt.get("task_id") == task_id
        ):
            proofs.append((event, receipt, receipt_digest))
    if len(proofs) != 1:
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "successor-owned descendant lacks one exact reconciliation receipt",
        )
    event, receipt, receipt_digest = proofs[0]
    try:
        receipt_task_version = int(receipt["task_version"])
        expected_agent_version = int(receipt["expected_agent_version"])
        expected_assignment_version = int(receipt["expected_assignment_version"])
        expected_callsign_version = int(
            receipt["expected_callsign_assignment_version"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "successor-owned descendant reconciliation receipt is malformed",
        ) from exc
    required_receipt = {
        "schema": "league.rollover-descendant-reconciliation.v1",
        "operation_id": operation["operation_id"],
        "reconciliation_id": event["event_id"],
        "squad_id": operation["squad_id"],
        "predecessor_agent_id": operation["predecessor_agent_id"],
        "successor_agent_id": operation["successor_agent_id"],
        "champion_agent_id": champion["agent_id"],
        "task_id": task_id,
        "runtime_instance_id": runtime["runtime_instance_id"],
        "runtime_generation": runtime["runtime_generation"],
        "callsign_assignment_id": callsign_assignment["callsign_assignment_id"],
        "result": "reconciled",
    }
    current_receipt = _descendant_reconciliation_receipt_exact(receipt)
    historical_imported_receipt = (
        _historical_imported_descendant_reconciliation_receipt_exact(receipt)
    )
    if (
        not isinstance(receipt_digest, str)
        or not (current_receipt or historical_imported_receipt)
        or digest(receipt) != receipt_digest
        or any(receipt.get(key) != value for key, value in required_receipt.items())
        or event["event_type"] != "rollover_descendant_reconciled"
        or event["task_id"] != task_id
        or event["aggregate_kind"] != "task"
        or event["aggregate_id"] != task_id
        or event["source_event_id"] != operation["owner_event_id"]
        or int(event["entity_version"]) != receipt_task_version
        or (
            current_receipt
            and (
                receipt.get("required_capabilities")
                != list(required_capabilities)
                or receipt.get("runtime_capabilities")
                != list(runtime_capabilities)
            )
        )
    ):
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "successor-owned descendant reconciliation receipt is not exact",
        )
    source_snapshot = store.connection.execute(
        """
        SELECT snapshot_id,operation_id,snapshot_version,digest
          FROM active_champion_snapshots
         WHERE snapshot_id=?
        """,
        (receipt.get("snapshot_id"),),
    ).fetchone()
    source_row = store.connection.execute(
        """
        SELECT champion_agent_id,task_id,callsign,binding_digest,row_digest
          FROM active_champion_snapshot_rows
         WHERE snapshot_id=? AND champion_agent_id=?
        """,
        (receipt.get("snapshot_id"), champion["agent_id"]),
    ).fetchone()
    source_runtime = None if receipt.get("created_runtime") is True else runtime
    source_private_binding = {
        "agent_id": champion["agent_id"],
        "task_id": task_id,
        "shotcaller_agent_id": operation["predecessor_agent_id"],
        "kind": champion["kind"],
        "thread_id": champion["thread_id"],
        "backend": champion["backend"],
        "routing_name": champion["routing_name"],
        "display_agent": champion["display_agent"],
        "repository": champion["repository"],
        "issue": champion["issue"],
        "branch": champion["branch"],
        "worktree": champion["worktree"],
        "runtime_instance_id": (
            None if source_runtime is None else source_runtime["runtime_instance_id"]
        ),
        "session_ref": None if source_runtime is None else source_runtime["session_ref"],
        "endpoint": None if source_runtime is None else source_runtime["endpoint"],
        "runtime_generation": (
            None if source_runtime is None else source_runtime["runtime_generation"]
        ),
        "capabilities": (
            None if source_runtime is None else source_runtime["capabilities_json"]
        ),
    }
    if (
        source_snapshot is None
        or source_snapshot["operation_id"] != operation["operation_id"]
        or source_snapshot["digest"] != receipt.get("snapshot_digest")
        or source_row is None
        or source_row["task_id"] != task_id
        or source_row["callsign"] != champion["callsign"]
        or source_row["row_digest"] != receipt.get("snapshot_row_digest")
        or source_row["binding_digest"] != digest(source_private_binding)
        or _snapshot_row_digest(
            source_snapshot["snapshot_id"],
            int(source_snapshot["snapshot_version"]),
            {
                "champion_agent_id": source_row["champion_agent_id"],
                "task_id": source_row["task_id"],
                "callsign": source_row["callsign"],
                "binding_digest": source_row["binding_digest"],
            },
        )
        != source_row["row_digest"]
    ):
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "successor-owned descendant source snapshot proof is missing or changed",
        )
    task = store.connection.execute(
        "SELECT * FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    assignment = store.connection.execute(
        "SELECT * FROM task_assignments WHERE task_assignment_id=?",
        (receipt.get("task_assignment_id"),),
    ).fetchone()
    minimum_agent_version = expected_agent_version + 1
    minimum_task_version = receipt_task_version
    minimum_assignment_version = (
        1
        if receipt.get("created_assignment") is True
        else expected_assignment_version + 1
    )
    minimum_callsign_version = expected_callsign_version + 1
    if (
        int(champion["version"]) < minimum_agent_version
        or task is None
        or task["champion_agent_id"] != champion["agent_id"]
        or task["coordinator_agent_id"] != operation["successor_agent_id"]
        or int(task["version"]) < minimum_task_version
        or task["state"]
        in {"completed", "complete", "failed", "cancelled", "canceled", "rejected"}
        or assignment is None
        or assignment["task_id"] != task_id
        or assignment["champion_agent_id"] != champion["agent_id"]
        or assignment["coordinator_agent_id"] != operation["successor_agent_id"]
        or assignment["runtime_instance_id"] != runtime["runtime_instance_id"]
        or assignment["callsign"] != champion["callsign"]
        or assignment["assignment_role"] != "champion"
        or assignment["state"] != "active"
        or int(assignment["version"]) < minimum_assignment_version
        or callsign_assignment["runtime_instance_id"] != runtime["runtime_instance_id"]
        or int(callsign_assignment["version"]) < minimum_callsign_version
    ):
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "successor-owned descendant transfer no longer matches its receipt",
        )
    if receipt.get("created_assignment") is True:
        try:
            acceptance_receipt = json.loads(assignment["acceptance_receipt_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise StorageRefusal(
                "snapshot_refresh_identity_changed",
                "created descendant assignment receipt is malformed",
            ) from exc
        if acceptance_receipt != receipt:
            raise StorageRefusal(
                "snapshot_refresh_identity_changed",
                "created descendant assignment does not retain the exact reconciliation receipt",
            )
    declared_outboxes = receipt.get("retargeted_outbox_ids")
    if (
        not isinstance(declared_outboxes, list)
        or any(not isinstance(item, str) or not item for item in declared_outboxes)
        or declared_outboxes != sorted(set(declared_outboxes))
    ):
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "successor-owned descendant outbox receipt is malformed",
        )
    if (
        historical_imported_receipt
        and receipt["pending_delivery_count"] != len(declared_outboxes)
    ):
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "historical descendant receipt contains unenumerated pending deliveries",
        )
    for outbox_id in declared_outboxes:
        outbox = store.connection.execute(
            """
            SELECT o.recipient_agent_id,e.agent_id,e.task_id
              FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
             WHERE o.outbox_id=?
            """,
            (outbox_id,),
        ).fetchone()
        if (
            outbox is None
            or outbox["recipient_agent_id"] != operation["successor_agent_id"]
            or (
                outbox["agent_id"] != champion["agent_id"]
                and outbox["task_id"] != task_id
            )
        ):
            raise StorageRefusal(
                "snapshot_refresh_identity_changed",
                "successor-owned descendant outbox transfer is incomplete",
            )
    stale_outbox = store.connection.execute(
        """
        SELECT 1 FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
         WHERE o.recipient_agent_id=?
           AND o.state IN ('pending','in_flight','awaiting_receipt')
           AND (e.agent_id=? OR e.task_id=?) LIMIT 1
        """,
        (operation["predecessor_agent_id"], champion["agent_id"], task_id),
    ).fetchone()
    if stale_outbox is not None:
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "successor-owned descendant still has predecessor delivery ownership",
        )
    return {
        "champion_agent_id": champion["agent_id"],
        "task_id": task_id,
        "state": "successor_reconciled",
        "reconciliation_id": event["event_id"],
        "receipt_digest": receipt_digest,
    }


def _descendant_context(
    store: Any,
    operation: Mapping[str, Any],
    current_rows: Sequence[Mapping[str, Any]],
    source_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    squad_id = operation["squad_id"]
    predecessor_agent_id = operation["predecessor_agent_id"]
    successor_agent_id = operation["successor_agent_id"]
    identities = store.connection.execute(
        """
        SELECT a.*,t.state AS task_state
          FROM squad_champions sc
          JOIN agent_instances a ON a.agent_id=sc.champion_agent_id
          LEFT JOIN tasks t ON t.task_id=a.task_id
         WHERE sc.squad_id=? ORDER BY a.agent_id
        """,
        (squad_id,),
    ).fetchall()
    runtime_rows = store.connection.execute(
        """
        SELECT r.* FROM squad_champions sc
          JOIN runtime_instances r ON r.actor_agent_id=sc.champion_agent_id
         WHERE sc.squad_id=? ORDER BY r.actor_agent_id,r.runtime_instance_id
        """,
        (squad_id,),
    ).fetchall()
    callsign_rows = store.connection.execute(
        """
        SELECT c.* FROM squad_champions sc
          JOIN agent_instances a ON a.agent_id=sc.champion_agent_id
          JOIN callsign_assignments c
            ON c.agent_id=a.agent_id AND c.callsign=a.callsign
           AND c.role='champion' AND c.scope_kind='task' AND c.scope_id=a.task_id
           AND c.state='active'
         WHERE sc.squad_id=? ORDER BY c.agent_id,c.callsign_assignment_id
        """,
        (squad_id,),
    ).fetchall()
    identity_by_agent = {row["agent_id"]: row for row in identities}
    source_by_agent = {row["champion_agent_id"]: row for row in source_rows}
    runtimes_by_agent: dict[str, list[Any]] = {}
    callsigns_by_agent: dict[str, list[Any]] = {}
    for row in runtime_rows:
        runtimes_by_agent.setdefault(row["actor_agent_id"], []).append(row)
    for row in callsign_rows:
        callsigns_by_agent.setdefault(row["agent_id"], []).append(row)

    descendants: list[dict[str, Any]] = []
    for current_row in current_rows:
        champion_agent_id = current_row["champion_agent_id"]
        task_id = current_row["task_id"]
        champion = identity_by_agent.get(champion_agent_id)
        source_row = source_by_agent.get(champion_agent_id)
        runtimes = runtimes_by_agent.get(champion_agent_id, [])
        callsigns = callsigns_by_agent.get(champion_agent_id, [])
        if len(runtimes) > 1:
            raise StorageRefusal(
                "snapshot_refresh_ambiguous",
                "descendant has multiple canonical runtime identities",
            )
        if len(callsigns) != 1:
            raise StorageRefusal(
                "snapshot_refresh_ambiguous",
                "descendant callsign identity is missing or ambiguous",
            )
        if (
            champion is None
            or source_row is None
            or champion["retired_at"] is not None
            or champion["role"] != "champion"
            or champion["task_id"] != task_id
            or champion["callsign"] != current_row["callsign"]
            or champion["task_state"] is None
            or champion["task_state"]
            in {"completed", "complete", "failed", "cancelled", "canceled", "rejected"}
        ):
            raise StorageRefusal(
                "snapshot_refresh_identity_changed",
                "descendant identity no longer matches the switched predecessor boundary",
            )
        try:
            required_capabilities = capabilities(
                json.loads(callsigns[0]["requirements_json"])
            )
        except (json.JSONDecodeError, TypeError, StorageRefusal) as exc:
            raise StorageRefusal(
                "snapshot_refresh_runtime_mismatch",
                "descendant capability identity is malformed",
            ) from exc
        runtime = None
        runtime_capabilities: tuple[str, ...] | None = None
        if runtimes:
            runtime = runtimes[0]
            required_capabilities, runtime_capabilities = _runtime_capability_contract(
                callsigns[0]["requirements_json"],
                runtime["capabilities_json"],
                code="snapshot_refresh_runtime_mismatch",
                message="canonical descendant runtime is missing a required callsign capability",
            )
            if (
                not bool(runtime["verified"])
                or runtime["status"] not in {"active", "idle"}
                or runtime["harness_kind"] != champion["kind"]
                or runtime["backend_kind"] != champion["backend"]
                or runtime["session_ref"] != champion["thread_id"]
                or runtime["endpoint"] != champion["address"]
                or not isinstance(runtime["runtime_generation"], str)
                or not runtime["runtime_generation"]
            ):
                raise StorageRefusal(
                    "snapshot_refresh_runtime_mismatch",
                    "canonical descendant runtime differs from the exact agent binding",
                )
        route_adoption = None
        if champion["routing_name"] is None:
            if (
                champion["display_agent"] is not None
                or champion["shotcaller_agent_id"] != predecessor_agent_id
                or champion["kind"] != "codex-thread"
                or champion["backend"] != "herdr"
                or not isinstance(champion["address"], str)
                or not champion["address"]
                or not isinstance(champion["thread_id"], str)
                or not champion["thread_id"]
                or not isinstance(champion["worktree"], str)
                or not champion["worktree"]
            ):
                raise StorageRefusal(
                    "snapshot_refresh_identity_changed",
                    "null descendant route is not an exact predecessor Herdr binding",
                )
            task = store.connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            assignments = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_id=? ORDER BY task_assignment_id",
                (task_id,),
            ).fetchall()
            if task is None or len(assignments) > 1:
                raise StorageRefusal(
                    "snapshot_refresh_identity_changed",
                    "null-route descendant task binding is missing or ambiguous",
                )
            source_shape, import_provenance_digest = _descendant_source_shape(
                store,
                task,
                champion_agent_id,
                predecessor_agent_id,
                callsigns[0],
                None if not assignments else assignments[0],
            )
            if source_shape != "imported_legacy_partial":
                raise StorageRefusal(
                    "snapshot_refresh_identity_changed",
                    "only an exact imported legacy predecessor may adopt a null route",
                )
            def binding_digest(
                bound_runtime: Optional[Mapping[str, Any]],
                *,
                routing_name: Optional[str],
                display_agent: Optional[str],
            ) -> str:
                return digest(
                    {
                        "agent_id": champion_agent_id,
                        "task_id": task_id,
                        "shotcaller_agent_id": champion["shotcaller_agent_id"],
                        "kind": champion["kind"],
                        "thread_id": champion["thread_id"],
                        "backend": champion["backend"],
                        "routing_name": routing_name,
                        "display_agent": display_agent,
                        "repository": champion["repository"],
                        "issue": champion["issue"],
                        "branch": champion["branch"],
                        "worktree": champion["worktree"],
                        "runtime_instance_id": (
                            None
                            if bound_runtime is None
                            else bound_runtime["runtime_instance_id"]
                        ),
                        "session_ref": (
                            None
                            if bound_runtime is None
                            else bound_runtime["session_ref"]
                        ),
                        "endpoint": (
                            None if bound_runtime is None else bound_runtime["endpoint"]
                        ),
                        "runtime_generation": (
                            None
                            if bound_runtime is None
                            else bound_runtime["runtime_generation"]
                        ),
                        "capabilities": (
                            None
                            if bound_runtime is None
                            else bound_runtime["capabilities_json"]
                        ),
                    }
                )

            current_binding_digest = binding_digest(
                runtime,
                routing_name=champion["routing_name"],
                display_agent=champion["display_agent"],
            )
            source_without_runtime_digest = binding_digest(
                None,
                routing_name=champion["routing_name"],
                display_agent=champion["display_agent"],
            )
            source_binding_digest = source_row["binding_digest"]
            runtime_materialized_after_snapshot = False
            if (
                current_binding_digest != current_row["binding_digest"]
                or (
                    source_binding_digest != current_binding_digest
                    and not (
                        runtime is not None
                        and source_binding_digest == source_without_runtime_digest
                    )
                )
            ):
                raise StorageRefusal(
                    "snapshot_refresh_identity_changed",
                    "null-route descendant differs beyond one exact materialized runtime",
                )
            if source_binding_digest != current_binding_digest:
                runtime_materialized_after_snapshot = True
            resulting_binding_digest = binding_digest(
                runtime,
                routing_name=str(champion["callsign"]).lower(),
                display_agent="codex",
            )
            route_adoption = {
                "routing_name": str(champion["callsign"]).lower(),
                "display_agent": "codex",
                "expected_agent_version": int(champion["version"]),
                "expected_callsign_assignment_version": int(callsigns[0]["version"]),
                "callsign_assignment_id": callsigns[0]["callsign_assignment_id"],
                "expected_callsign_runtime_instance_id": callsigns[0][
                    "runtime_instance_id"
                ],
                "expected_callsign_requirements": list(required_capabilities),
                "source_shape": source_shape,
                "import_provenance_digest": import_provenance_digest,
                "snapshot_row_digest": source_row["row_digest"],
                "source_binding_digest": source_binding_digest,
                "pre_adoption_binding_digest": current_binding_digest,
                "resulting_binding_digest": resulting_binding_digest,
                "runtime_materialized_after_snapshot": (
                    runtime_materialized_after_snapshot
                ),
                "runtime_instance_id": (
                    None if runtime is None else runtime["runtime_instance_id"]
                ),
                "runtime_generation": (
                    None if runtime is None else runtime["runtime_generation"]
                ),
                "runtime_count": len(runtimes),
                "runtime_status": None if runtime is None else runtime["status"],
                "runtime_capabilities": (
                    None if runtime is None else list(runtime_capabilities)
                ),
            }
        elif not isinstance(champion["display_agent"], str) or not champion[
            "display_agent"
        ]:
            raise StorageRefusal(
                "snapshot_refresh_identity_changed",
                "canonical descendant route lacks its display identity",
            )
        if champion["shotcaller_agent_id"] == predecessor_agent_id:
            progress = {
                "champion_agent_id": champion_agent_id,
                "task_id": task_id,
                "state": "predecessor_pending",
                "reconciliation_id": None,
                "receipt_digest": None,
            }
        elif champion["shotcaller_agent_id"] == successor_agent_id and runtime is not None:
            progress = _successor_progress(
                store,
                operation,
                champion,
                task_id,
                callsigns[0],
                runtime,
                required_capabilities,
                runtime_capabilities,
            )
        else:
            raise StorageRefusal(
                "snapshot_refresh_identity_changed",
                "descendant owner is neither the switched predecessor nor a proved successor transfer",
            )
        descendants.append(
            {
                "champion_agent_id": champion_agent_id,
                "task_id": task_id,
                "callsign": current_row["callsign"],
                "kind": champion["kind"],
                "thread_id": champion["thread_id"],
                "backend": champion["backend"],
                "routing_name": champion["routing_name"],
                "agent_version": int(champion["version"]),
                "display_agent": champion["display_agent"],
                "address": champion["address"],
                "worktree": champion["worktree"],
                "canonical_row_digest": current_row["row_digest"],
                "required_capabilities": list(required_capabilities),
                "capabilities": list(
                    required_capabilities
                    if runtime_capabilities is None
                    else runtime_capabilities
                ),
                "runtime": (
                    None
                    if runtime is None
                    else {
                        "runtime_instance_id": runtime["runtime_instance_id"],
                        "runtime_generation": runtime["runtime_generation"],
                        "status": runtime["status"],
                        "capabilities": list(runtime_capabilities),
                    }
                ),
                "progress": progress,
                "route_adoption": route_adoption,
            }
        )
    return descendants


def _retry(
    store: Any,
    event_id: str,
    *,
    operation_id: str,
    refresh_id: str,
    squad_id: str,
    predecessor_agent_id: str,
    successor_agent_id: str,
    expected_rollover_version: int,
    expected_snapshot_version: int,
    expected_snapshot_digest: str,
    expires_at: str,
    at: str,
) -> Optional[dict[str, Any]]:
    event = store.connection.execute(
        "SELECT event_type,detail_json FROM events WHERE event_id=?", (event_id,)
    ).fetchone()
    if event is None:
        return None
    try:
        detail = json.loads(event["detail_json"])
        receipt = detail["receipt"]
        receipt_digest = detail["receipt_digest"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise StorageRefusal(
            "snapshot_refresh_conflict", "stored snapshot refresh receipt is malformed"
        ) from exc
    exact = {
        "operation_id": operation_id,
        "refresh_id": refresh_id,
        "squad_id": squad_id,
        "predecessor_agent_id": predecessor_agent_id,
        "successor_agent_id": successor_agent_id,
        "expected_rollover_version": expected_rollover_version,
        "expected_snapshot_version": expected_snapshot_version,
        "expected_snapshot_digest": expected_snapshot_digest,
        "expires_at": expires_at,
        "refreshed_at": at,
    }
    if (
        event["event_type"] != "rollover_snapshot_refreshed"
        or any(receipt.get(key) != value for key, value in exact.items())
        or digest(receipt) != receipt_digest
    ):
        raise StorageRefusal(
            "snapshot_refresh_conflict",
            "snapshot refresh retry changed immutable identity",
        )
    return {**receipt, "receipt_digest": receipt_digest, "idempotent": True}


def _context(
    store: Any,
    operation_id: str,
    squad_id: str,
    predecessor_agent_id: str,
    successor_agent_id: str,
    expected_rollover_version: int,
    expected_snapshot_version: int,
    expected_snapshot_digest: str,
    expires_at: str,
    at: str,
) -> dict[str, Any]:
    now = timestamp(at, "snapshot refresh time")
    expiry = timestamp(expires_at, "refreshed snapshot expiry")
    if expiry <= now:
        raise StorageRefusal(
            "invalid_snapshot_refresh", "refreshed snapshot expiry must be after refresh"
        )
    operation = _operation(store, operation_id)
    if (
        operation["state"] != "switched"
        or int(operation["version"]) != expected_rollover_version
    ):
        raise StorageRefusal(
            "snapshot_refresh_stale",
            "snapshot refresh requires the exact switched rollover version",
        )
    if (
        operation["squad_id"] != squad_id
        or operation["predecessor_agent_id"] != predecessor_agent_id
        or operation["successor_agent_id"] != successor_agent_id
    ):
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "rollover Squad or owner identity differs from the refresh request",
        )
    snapshot = store.connection.execute(
        "SELECT * FROM active_champion_snapshots WHERE snapshot_id=?",
        (operation["snapshot_id"],),
    ).fetchone()
    if (
        snapshot is None
        or snapshot["operation_id"] != operation_id
        or snapshot["squad_id"] != squad_id
        or int(snapshot["snapshot_version"]) != expected_snapshot_version
        or snapshot["digest"] != expected_snapshot_digest
    ):
        raise StorageRefusal(
            "snapshot_refresh_stale", "current rollover snapshot changed before refresh"
        )
    if now <= timestamp(str(snapshot["expires_at"]), "stored snapshot expiry"):
        raise StorageRefusal(
            "snapshot_refresh_not_expired",
            "only an expired switched-rollover snapshot may be refreshed",
        )
    squad = store.connection.execute(
        "SELECT * FROM squads WHERE squad_id=?", (squad_id,)
    ).fetchone()
    if (
        squad is None
        or squad["state"] != "active"
        or squad["shotcaller_agent_id"] != successor_agent_id
        or int(squad["version"]) != int(operation["expected_owner_version"]) + 1
        or int(squad["owner_fence"]) != int(operation["expected_owner_fence"]) + 1
    ):
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "committed Squad owner identity or fence changed",
        )
    owners = store.connection.execute(
        """
        SELECT agent_id,role,retired_at FROM agent_instances
         WHERE agent_id IN (?,?) ORDER BY agent_id
        """,
        (predecessor_agent_id, successor_agent_id),
    ).fetchall()
    if (
        len(owners) != 2
        or {row["agent_id"] for row in owners}
        != {predecessor_agent_id, successor_agent_id}
        or any(row["role"] != "shotcaller" or row["retired_at"] is not None for row in owners)
    ):
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "predecessor or successor Shotcaller identity changed",
        )
    predecessor_intake = store.connection.execute(
        "SELECT state,fence FROM shotcaller_intake WHERE agent_id=? AND squad_id=?",
        (predecessor_agent_id, squad_id),
    ).fetchone()
    successor_intake = store.connection.execute(
        "SELECT state,fence FROM shotcaller_intake WHERE agent_id=? AND squad_id=?",
        (successor_agent_id, squad_id),
    ).fetchone()
    if (
        predecessor_intake is None
        or predecessor_intake["state"] != "draining"
        or int(predecessor_intake["fence"]) != int(squad["owner_fence"])
        or successor_intake is None
        or successor_intake["state"] != "accepting"
        or int(successor_intake["fence"]) != int(squad["owner_fence"])
    ):
        raise StorageRefusal(
            "snapshot_refresh_identity_changed",
            "Shotcaller intake ownership is not the exact switched boundary",
        )
    _runtime_identity(
        store, successor_agent_id, operation["successor_runtime_instance_id"]
    )
    old_rows = [
        dict(row)
        for row in store.connection.execute(
            """
            SELECT champion_agent_id,task_id,callsign,binding_digest,row_digest
              FROM active_champion_snapshot_rows WHERE snapshot_id=?
             ORDER BY ordinal
            """,
            (snapshot["snapshot_id"],),
        )
    ]
    current_rows = _snapshot_rows(store, squad_id)
    old_identity = [
        (row["champion_agent_id"], row["task_id"], row["callsign"])
        for row in old_rows
    ]
    current_identity = [
        (row["champion_agent_id"], row["task_id"], row["callsign"])
        for row in current_rows
    ]
    if old_identity != current_identity or len(old_rows) != int(snapshot["total_count"]):
        raise StorageRefusal(
            "snapshot_refresh_set_changed",
            "current active descendants differ from the expired frozen set",
        )
    descendants = _descendant_context(store, operation, current_rows, old_rows)
    canonical_digest = digest(
        {
            "operation_id": operation_id,
            "rollover_version": expected_rollover_version,
            "source_snapshot": _snapshot_value(snapshot),
            "squad_version": int(squad["version"]),
            "owner_fence": int(squad["owner_fence"]),
            "rows": current_rows,
            "descendants": descendants,
        }
    )
    return {
        "operation": operation,
        "snapshot": snapshot,
        "source_snapshot": _snapshot_value(snapshot),
        "current_rows": current_rows,
        "descendants": descendants,
        "canonical_digest": canonical_digest,
        "squad_version": int(squad["version"]),
    }


def refresh_target(
    store: Any,
    operation_id: str,
    refresh_id: str,
    squad_id: str,
    predecessor_agent_id: str,
    successor_agent_id: str,
    expected_rollover_version: int,
    expected_snapshot_version: int,
    expected_snapshot_digest: str,
    expires_at: str,
    at: str,
) -> dict[str, Any]:
    event_id = _event_id(refresh_id)
    retry = _retry(
        store,
        event_id,
        operation_id=operation_id,
        refresh_id=refresh_id,
        squad_id=squad_id,
        predecessor_agent_id=predecessor_agent_id,
        successor_agent_id=successor_agent_id,
        expected_rollover_version=expected_rollover_version,
        expected_snapshot_version=expected_snapshot_version,
        expected_snapshot_digest=expected_snapshot_digest,
        expires_at=expires_at,
        at=at,
    )
    if retry is not None:
        return {"refreshed": True}
    context = _context(
        store,
        operation_id,
        squad_id,
        predecessor_agent_id,
        successor_agent_id,
        expected_rollover_version,
        expected_snapshot_version,
        expected_snapshot_digest,
        expires_at,
        at,
    )
    return {
        "refreshed": False,
        "descendants": context["descendants"],
        "canonical_digest": context["canonical_digest"],
    }


def _observations(
    descendants: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    keys = {
        "schema", "verified", "champion_agent_id", "task_id", "callsign",
        "thread_id", "endpoint", "routing_name", "worktree", "terminal_id",
        "state_change_seq", "runtime_generation", "status", "canonical_row_digest",
    }
    ordered = sorted(
        (dict(item) for item in observations), key=lambda item: item.get("champion_agent_id", "")
    )
    targets = sorted(
        (dict(item) for item in descendants), key=lambda item: item["champion_agent_id"]
    )
    if len(ordered) != len(targets):
        raise StorageRefusal(
            "snapshot_refresh_live_proof_missing",
            "live descendant observation count differs from the canonical snapshot",
        )
    for target, observation in zip(targets, ordered):
        terminal_id = observation.get("terminal_id")
        thread_id = observation.get("thread_id")
        runtime_generation = observation.get("runtime_generation")
        expected_route = target.get("routing_name")
        if not isinstance(expected_route, str) or not expected_route:
            adoption = target.get("route_adoption")
            expected_route = (
                adoption.get("routing_name")
                if isinstance(adoption, Mapping)
                else None
            )
        derived_generation = (
            _herdr_runtime_generation(terminal_id, thread_id)
            if isinstance(terminal_id, str)
            and bool(terminal_id)
            and isinstance(thread_id, str)
            and bool(thread_id)
            else None
        )
        if (
            set(observation) != keys
            or observation.get("schema") != "league.rollover-snapshot-observation.v1"
            or observation.get("verified") is not True
            or observation.get("champion_agent_id") != target["champion_agent_id"]
            or observation.get("task_id") != target["task_id"]
            or observation.get("callsign") != target["callsign"]
            or observation.get("thread_id") != target["thread_id"]
            or observation.get("endpoint") != target["address"]
            or observation.get("routing_name") != expected_route
            or observation.get("worktree") != target["worktree"]
            or observation.get("canonical_row_digest") != target["canonical_row_digest"]
            or not isinstance(runtime_generation, str)
            or not runtime_generation
            or derived_generation is None
            or runtime_generation != derived_generation
            or (
                target["runtime"] is not None
                and runtime_generation
                != target["runtime"]["runtime_generation"]
            )
            or observation.get("status") not in {"active", "idle"}
            or not isinstance(terminal_id, str)
            or not terminal_id
            or type(observation.get("state_change_seq")) is not int
            or observation["state_change_seq"] < 0
        ):
            raise StorageRefusal(
                "snapshot_refresh_live_proof_mismatch",
                "live descendant observation differs from canonical identity",
            )
    return ordered, digest(ordered)


def _adopt_legacy_null_routes(
    store: Any,
    context: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    refresh_id: str,
    at: str,
    fault: Optional[FaultInjector],
) -> list[dict[str, Any]]:
    """CAS-persist only exact live routes missing from imported predecessor rows."""

    operation = context["operation"]
    observed_by_agent = {
        item["champion_agent_id"]: item for item in observations
    }
    results: list[dict[str, Any]] = []
    for descendant in context["descendants"]:
        adoption = descendant.get("route_adoption")
        if not isinstance(adoption, Mapping):
            continue
        observation = observed_by_agent.get(descendant["champion_agent_id"])
        if observation is None:
            raise StorageRefusal(
                "snapshot_refresh_live_proof_missing",
                "legacy route adoption lacks its exact live observation",
            )
        callsign_assignment = store.connection.execute(
            """
            SELECT callsign_assignment_id,agent_id,callsign,role,scope_kind,scope_id,
                   state,runtime_instance_id,requirements_json,version
              FROM callsign_assignments WHERE callsign_assignment_id=?
            """,
            (adoption["callsign_assignment_id"],),
        ).fetchone()
        runtimes = store.connection.execute(
            "SELECT * FROM runtime_instances WHERE actor_agent_id=? ORDER BY runtime_instance_id",
            (descendant["champion_agent_id"],),
        ).fetchall()
        try:
            requirements = (
                None
                if callsign_assignment is None
                else json.loads(callsign_assignment["requirements_json"])
            )
            runtime_capabilities = (
                None
                if len(runtimes) != 1
                else json.loads(runtimes[0]["capabilities_json"])
            )
        except (json.JSONDecodeError, TypeError) as exc:
            raise StorageRefusal(
                "snapshot_refresh_concurrent_mutation",
                "legacy route adoption capability evidence changed",
            ) from exc
        if (
            callsign_assignment is None
            or callsign_assignment["agent_id"] != descendant["champion_agent_id"]
            or callsign_assignment["callsign"] != descendant["callsign"]
            or callsign_assignment["role"] != "champion"
            or callsign_assignment["scope_kind"] != "task"
            or callsign_assignment["scope_id"] != descendant["task_id"]
            or callsign_assignment["state"] != "active"
            or callsign_assignment["runtime_instance_id"]
            != adoption["expected_callsign_runtime_instance_id"]
            or int(callsign_assignment["version"])
            != adoption["expected_callsign_assignment_version"]
            or requirements != adoption["expected_callsign_requirements"]
            or len(runtimes) != adoption["runtime_count"]
            or (
                len(runtimes) == 1
                and (
                    not bool(runtimes[0]["verified"])
                    or runtimes[0]["harness_kind"] != descendant["kind"]
                    or runtimes[0]["backend_kind"] != descendant["backend"]
                    or runtimes[0]["session_ref"] != descendant["thread_id"]
                    or runtimes[0]["endpoint"] != descendant["address"]
                    or runtimes[0]["runtime_instance_id"]
                    != adoption["runtime_instance_id"]
                    or runtimes[0]["runtime_generation"]
                    != adoption["runtime_generation"]
                    or runtimes[0]["status"] != adoption["runtime_status"]
                    or runtime_capabilities != adoption["runtime_capabilities"]
                )
            )
        ):
            raise StorageRefusal(
                "snapshot_refresh_concurrent_mutation",
                "legacy route adoption callsign or runtime evidence changed",
            )
        expected_agent_version = adoption["expected_agent_version"]
        next_agent_version = expected_agent_version + 1
        event_id = "rollover-route-adoption:" + digest(
            {
                "operation_id": operation["operation_id"],
                "refresh_id": refresh_id,
                "champion_agent_id": descendant["champion_agent_id"],
                "task_id": descendant["task_id"],
            }
        )[:40]
        receipt = {
            "schema": "league.rollover-descendant-route-adoption.v1",
            "operation_id": operation["operation_id"],
            "refresh_id": refresh_id,
            "event_id": event_id,
            "source_snapshot_id": context["source_snapshot"]["snapshot_id"],
            "source_snapshot_version": context["source_snapshot"]["version"],
            "source_snapshot_digest": context["source_snapshot"]["digest"],
            "source_snapshot_row_digest": adoption["snapshot_row_digest"],
            "squad_id": operation["squad_id"],
            "predecessor_agent_id": operation["predecessor_agent_id"],
            "successor_agent_id": operation["successor_agent_id"],
            "champion_agent_id": descendant["champion_agent_id"],
            "task_id": descendant["task_id"],
            "callsign": descendant["callsign"],
            "routing_name": adoption["routing_name"],
            "display_agent": adoption["display_agent"],
            "expected_agent_version": expected_agent_version,
            "agent_version": next_agent_version,
            "callsign_assignment_id": adoption["callsign_assignment_id"],
            "expected_callsign_assignment_version": adoption[
                "expected_callsign_assignment_version"
            ],
            "expected_callsign_runtime_instance_id": adoption[
                "expected_callsign_runtime_instance_id"
            ],
            "expected_callsign_requirements": adoption[
                "expected_callsign_requirements"
            ],
            "runtime_count": adoption["runtime_count"],
            "runtime_instance_id": adoption["runtime_instance_id"],
            "runtime_generation": adoption["runtime_generation"],
            "runtime_status": adoption["runtime_status"],
            "runtime_capabilities": adoption["runtime_capabilities"],
            "runtime_materialized_after_snapshot": adoption[
                "runtime_materialized_after_snapshot"
            ],
            "source_binding_digest": adoption["source_binding_digest"],
            "pre_adoption_binding_digest": adoption[
                "pre_adoption_binding_digest"
            ],
            "resulting_binding_digest": adoption["resulting_binding_digest"],
            "endpoint_digest": digest({"endpoint": descendant["address"]}),
            "thread_digest": digest({"thread_id": descendant["thread_id"]}),
            "worktree_digest": digest({"worktree": descendant["worktree"]}),
            "terminal_digest": digest({"terminal_id": observation["terminal_id"]}),
            "state_change_seq": observation["state_change_seq"],
            "observed_status": observation["status"],
            "observation_digest": digest(dict(observation)),
            "source_shape": adoption["source_shape"],
            "import_provenance_digest": adoption["import_provenance_digest"],
            "reason": "switched_rollover_imported_legacy_null_route_adoption",
            "result": "adopted",
            "at": at,
        }
        changed = store.connection.execute(
            """
            UPDATE agent_instances
               SET routing_name=?,display_agent=?,version=version+1,updated_at=?
             WHERE agent_id=? AND role='champion' AND shotcaller_agent_id=?
               AND task_id=? AND retired_at IS NULL AND routing_name IS NULL
               AND display_agent IS NULL AND kind='codex-thread' AND backend='herdr'
               AND address=? AND thread_id=? AND worktree=? AND version=?
            """,
            (
                adoption["routing_name"],
                adoption["display_agent"],
                at,
                descendant["champion_agent_id"],
                operation["predecessor_agent_id"],
                descendant["task_id"],
                descendant["address"],
                descendant["thread_id"],
                descendant["worktree"],
                expected_agent_version,
            ),
        )
        if changed.rowcount != 1:
            raise StorageRefusal(
                "snapshot_refresh_concurrent_mutation",
                "legacy descendant route adoption CAS changed",
            )
        if fault:
            fault("after_refresh_route_adoption_agent")
        receipt_digest = digest(receipt)
        store.connection.execute(
            """
            INSERT INTO events
              (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
               update_text,occurred_at,detail_json,aggregate_kind,aggregate_id,
               source_event_id)
            VALUES(?,?,NULL,NULL,?,'rollover_descendant_route_adopted',?,
                   'imported legacy descendant route adopted',?,?,'agent',?,?)
            """,
            (
                event_id,
                descendant["champion_agent_id"],
                next_agent_version,
                observation["status"],
                at,
                stable_json(
                    {"receipt": receipt, "receipt_digest": receipt_digest}
                ),
                descendant["champion_agent_id"],
                operation["owner_event_id"],
            ),
        )
        if fault:
            fault("after_refresh_route_adoption_event")
        results.append(
            {
                "champion_agent_id": descendant["champion_agent_id"],
                "task_id": descendant["task_id"],
                "event_id": event_id,
                "receipt_digest": receipt_digest,
                "routing_name": adoption["routing_name"],
                "expected_agent_version": expected_agent_version,
                "agent_version": next_agent_version,
                "source_binding_digest": adoption["source_binding_digest"],
                "pre_adoption_binding_digest": adoption[
                    "pre_adoption_binding_digest"
                ],
                "resulting_binding_digest": adoption["resulting_binding_digest"],
                "runtime_materialized_after_snapshot": adoption[
                    "runtime_materialized_after_snapshot"
                ],
            }
        )
    if results and fault:
        fault("after_refresh_route_adoptions")
    return results


def refresh(
    store: Any,
    operation_id: str,
    refresh_id: str,
    squad_id: str,
    predecessor_agent_id: str,
    successor_agent_id: str,
    expected_rollover_version: int,
    expected_snapshot_version: int,
    expected_snapshot_digest: str,
    expires_at: str,
    at: str,
    canonical_digest: str,
    observations: Sequence[Mapping[str, Any]],
    final_observer: Callable[
        [list[dict[str, Any]]], Sequence[Mapping[str, Any]]
    ],
    *,
    fault: Optional[FaultInjector] = None,
) -> dict[str, Any]:
    event_id = _event_id(refresh_id)
    try:
        with store._transaction(immediate=False):
            retry = _retry(
                store,
                event_id,
                operation_id=operation_id,
                refresh_id=refresh_id,
                squad_id=squad_id,
                predecessor_agent_id=predecessor_agent_id,
                successor_agent_id=successor_agent_id,
                expected_rollover_version=expected_rollover_version,
                expected_snapshot_version=expected_snapshot_version,
                expected_snapshot_digest=expected_snapshot_digest,
                expires_at=expires_at,
                at=at,
            )
            if retry is not None:
                return retry
            context = _context(
                store,
                operation_id,
                squad_id,
                predecessor_agent_id,
                successor_agent_id,
                expected_rollover_version,
                expected_snapshot_version,
                expected_snapshot_digest,
                expires_at,
                at,
            )
            if context["canonical_digest"] != canonical_digest:
                raise StorageRefusal(
                    "snapshot_refresh_concurrent_mutation",
                    "canonical descendant state changed after live observation",
                )
            normalized, observation_digest = _observations(
                context["descendants"], observations
            )
            final_candidate = sorted(
                (
                    dict(item)
                    for item in final_observer(
                        [dict(item) for item in context["descendants"]]
                    )
                ),
                key=lambda item: item.get("champion_agent_id", ""),
            )
            if normalized != final_candidate:
                raise StorageRefusal(
                    "snapshot_refresh_live_changed",
                    "live descendant identity changed during snapshot refresh",
                )
            final_normalized, final_observation_digest = _observations(
                context["descendants"], final_candidate
            )
            route_adoptions = _adopt_legacy_null_routes(
                store,
                context,
                final_normalized,
                refresh_id=refresh_id,
                at=at,
                fault=fault,
            )
            post_context = _context(
                store,
                operation_id,
                squad_id,
                predecessor_agent_id,
                successor_agent_id,
                expected_rollover_version,
                expected_snapshot_version,
                expected_snapshot_digest,
                expires_at,
                at,
            )
            adopted_agents = {
                item["champion_agent_id"] for item in route_adoptions
            }
            adoption_by_agent = {
                item["champion_agent_id"]: item for item in route_adoptions
            }
            before_rows = {
                row["champion_agent_id"]: row for row in context["current_rows"]
            }
            after_rows = {
                row["champion_agent_id"]: row
                for row in post_context["current_rows"]
            }
            if set(before_rows) != set(after_rows) or any(
                (
                    before_rows[agent_id]["binding_digest"]
                    != adoption_by_agent[agent_id]["pre_adoption_binding_digest"]
                    or after_rows[agent_id]["binding_digest"]
                    != adoption_by_agent[agent_id]["resulting_binding_digest"]
                )
                if agent_id in adopted_agents
                else after_rows[agent_id] != before_rows[agent_id]
                for agent_id in before_rows
            ):
                raise StorageRefusal(
                    "snapshot_refresh_concurrent_mutation",
                    "canonical descendants changed beyond exact route adoption",
                )
            next_snapshot_version = expected_snapshot_version + 1
            next_rollover_version = expected_rollover_version + 1
            snapshot_id = f"snapshot:{operation_id}:v{next_snapshot_version}"
            refreshed_rows = []
            for current_row in post_context["current_rows"]:
                row = {
                    "champion_agent_id": current_row["champion_agent_id"],
                    "task_id": current_row["task_id"],
                    "callsign": current_row["callsign"],
                    "binding_digest": current_row["binding_digest"],
                }
                row["row_digest"] = _snapshot_row_digest(
                    snapshot_id, next_snapshot_version, row
                )
                refreshed_rows.append(row)
            snapshot_digest = _snapshot_digest(refreshed_rows)
            changed = store.connection.execute(
                """
                UPDATE rollover_operations SET snapshot_id=?,version=?,updated_at=?
                 WHERE operation_id=? AND state='switched' AND version=? AND snapshot_id=?
                """,
                (
                    snapshot_id, next_rollover_version, at, operation_id,
                    expected_rollover_version, context["snapshot"]["snapshot_id"],
                ),
            )
            if changed.rowcount != 1:
                raise StorageRefusal(
                    "snapshot_refresh_concurrent_mutation",
                    "rollover snapshot pointer changed during refresh",
                )
            if fault:
                fault("after_refresh_operation_cas")
            store.connection.execute(
                """
                INSERT INTO active_champion_snapshots
                  (snapshot_id,operation_id,squad_id,snapshot_version,total_count,page_bound,
                   expires_at,digest,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id, operation_id, squad_id, next_snapshot_version,
                    len(refreshed_rows), int(context["snapshot"]["page_bound"]),
                    expires_at, snapshot_digest, at,
                ),
            )
            if fault:
                fault("after_refresh_snapshot")
            store.connection.executemany(
                """
                INSERT INTO active_champion_snapshot_rows
                  (snapshot_id,ordinal,champion_agent_id,task_id,callsign,binding_digest,row_digest)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    (
                        snapshot_id, ordinal, row["champion_agent_id"], row["task_id"],
                        row["callsign"], row["binding_digest"], row["row_digest"],
                    )
                    for ordinal, row in enumerate(refreshed_rows)
                ),
            )
            if fault:
                fault("after_refresh_rows")
            snapshot_value = {
                "snapshot_id": snapshot_id,
                "version": next_snapshot_version,
                "count": len(refreshed_rows),
                "page_bound": int(context["snapshot"]["page_bound"]),
                "expires_at": expires_at,
                "digest": snapshot_digest,
            }
            receipt = {
                "schema": "league.rollover-snapshot-refresh.v1",
                "operation_id": operation_id,
                "refresh_id": refresh_id,
                "squad_id": squad_id,
                "predecessor_agent_id": predecessor_agent_id,
                "successor_agent_id": successor_agent_id,
                "expected_rollover_version": expected_rollover_version,
                "expected_snapshot_version": expected_snapshot_version,
                "expected_snapshot_digest": expected_snapshot_digest,
                "source_snapshot": context["source_snapshot"],
                "snapshot": snapshot_value,
                "rollover_version": next_rollover_version,
                "descendant_count": len(refreshed_rows),
                "capability_bindings": [
                    {
                        "champion_agent_id": descendant["champion_agent_id"],
                        "required_capabilities": descendant["required_capabilities"],
                        "runtime_capabilities": (
                            None
                            if descendant["runtime"] is None
                            else descendant["runtime"]["capabilities"]
                        ),
                    }
                    for descendant in post_context["descendants"]
                ],
                "progress_bindings": [
                    descendant["progress"]
                    for descendant in post_context["descendants"]
                ],
                "route_adoptions": route_adoptions,
                "canonical_digest": canonical_digest,
                "refreshed_canonical_digest": post_context["canonical_digest"],
                "observation_digest": observation_digest,
                "final_observation_digest": final_observation_digest,
                "expires_at": expires_at,
                "refreshed_at": at,
            }
            receipt_digest = digest(receipt)
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,NULL,NULL,?,?,'rollover_snapshot_refreshed','switched',
                       'expired rollover snapshot refreshed',?,?,'squad',?)
                """,
                (
                    event_id, squad_id, context["squad_version"], at,
                    stable_json(
                        {
                            "receipt": receipt,
                            "receipt_digest": receipt_digest,
                            "observation_count": len(final_normalized),
                        }
                    ),
                    squad_id,
                ),
            )
            if fault:
                fault("after_refresh_event")
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "rollover snapshot refresh conflicted with canonical state"
        ) from exc
    return {**receipt, "receipt_digest": receipt_digest, "idempotent": False}
