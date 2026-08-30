"""Guarded disposable Shotcaller rollover over one stable Squad."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from .sqlite_callsign_ops import (
    _release_active_in_transaction,
    _rollback_reserved_in_transaction,
    capabilities,
    digest,
    stable_json,
    timestamp,
)
from .storage_types import StorageRefusal
from .storage_types import FaultInjector


HANDOFF_KEYS = {
    "schema",
    "scope",
    "authority",
    "non_goals",
    "unresolved",
    "pending_decisions",
    "next_actions",
    "obligations",
    "policy_digest",
    "instruction_digest",
    "expires_at",
    "page_bound",
}
ABORT_RECEIPT_KEYS = {
    "schema",
    "verified",
    "operation_id",
    "successor_agent_id",
    "runtime_instance_id",
    "runtime_cleanup_receipt_digest",
    "cleanup_digest",
}
DRAIN_RECEIPT_KEYS = {
    "schema",
    "verified",
    "operation_id",
    "predecessor_agent_id",
    "successor_agent_id",
    "owner_event_id",
    "archive_digest",
    "resource_receipt_digest",
    "callsign_release_receipt_digest",
}
TERMINAL_REQUESTS = {"answered", "cancelled"}
MAX_ACTIVE_CHAMPIONS = 10_000
PRIVATE_KEY = re.compile(
    r"(?:password|secret|token|credential|cookie|transcript|worktree|local_path|endpoint|address|session)",
    re.IGNORECASE,
)
PRIVATE_TEXT = re.compile(
    r"(?:^/|file://|localhost|127\.0\.0\.1|0\.0\.0\.0|/Users/|/home/|BEGIN [A-Z ]+ PRIVATE KEY)",
    re.IGNORECASE,
)


def _safe_plan_value(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise StorageRefusal("handoff_unsafe", "handoff nesting exceeds its public-safe bound")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        if len(value) > 4096 or PRIVATE_TEXT.search(value):
            raise StorageRefusal("handoff_unsafe", "handoff contains local or private material")
        return
    if isinstance(value, list):
        if len(value) > 500:
            raise StorageRefusal("handoff_unsafe", "handoff list exceeds its bound")
        for item in value:
            _safe_plan_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 100:
            raise StorageRefusal("handoff_unsafe", "handoff object exceeds its bound")
        for key, item in value.items():
            if not isinstance(key, str) or PRIVATE_KEY.search(key):
                raise StorageRefusal("handoff_unsafe", "handoff contains a private field class")
            _safe_plan_value(item, depth=depth + 1)
        return
    raise StorageRefusal("handoff_unsafe", "handoff contains an unsupported value")


def _handoff_plan(plan: Mapping[str, Any], squad_id: str) -> tuple[dict[str, Any], str]:
    if set(plan) != HANDOFF_KEYS or plan.get("schema") != "league.shotcaller-handoff-plan.v1":
        raise StorageRefusal("invalid_handoff", "handoff plan shape is invalid")
    if plan.get("scope") != {"kind": "squad", "id": squad_id}:
        raise StorageRefusal("invalid_handoff", "handoff scope does not match the stable Squad")
    if not isinstance(plan.get("authority"), str) or not plan["authority"]:
        raise StorageRefusal("invalid_handoff", "handoff authority is missing")
    for key in ("non_goals", "unresolved", "pending_decisions", "next_actions", "obligations"):
        if not isinstance(plan.get(key), list):
            raise StorageRefusal("invalid_handoff", f"handoff {key} must be a bounded list")
    for key in ("policy_digest", "instruction_digest"):
        if not isinstance(plan.get(key), str) or not plan[key]:
            raise StorageRefusal("invalid_handoff", f"handoff {key} is required")
    timestamp(str(plan.get("expires_at", "")), "handoff expiry")
    page_bound = plan.get("page_bound")
    if not isinstance(page_bound, int) or not 1 <= page_bound <= 500:
        raise StorageRefusal("invalid_handoff", "handoff page bound must be between 1 and 500")
    value = dict(plan)
    _safe_plan_value(value)
    encoded = stable_json(value).encode("utf-8")
    if len(encoded) > 65_536:
        raise StorageRefusal("handoff_too_large", "handoff exceeds 65536 bytes")
    return value, digest(value)


def _runtime_identity(store: Any, agent_id: str, runtime_instance_id: str) -> tuple[Any, tuple[str, ...]]:
    row = store.connection.execute(
        """
        SELECT r.*,a.thread_id,a.address,a.backend,a.routing_name,a.display_agent,a.retired_at
          FROM runtime_instances r JOIN agent_instances a ON a.agent_id=r.actor_agent_id
         WHERE r.runtime_instance_id=? AND r.actor_agent_id=?
        """,
        (runtime_instance_id, agent_id),
    ).fetchone()
    if (
        row is None
        or row["retired_at"] is not None
        or row["status"] != "active"
        or not row["verified"]
        or row["session_ref"] != row["thread_id"]
        or row["endpoint"] != row["address"]
        or (row["backend"] is not None and row["backend_kind"] != row["backend"])
    ):
        raise StorageRefusal(
            "successor_identity_mismatch", "successor runtime identity is not exact and active"
        )
    try:
        declared = capabilities(json.loads(row["capabilities_json"]))
    except (json.JSONDecodeError, TypeError, StorageRefusal) as exc:
        raise StorageRefusal(
            "successor_capability_mismatch", "successor capability declaration is invalid"
        ) from exc
    return row, declared


def _snapshot_rows(store: Any, squad_id: str) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        """
        SELECT sc.champion_agent_id,a.callsign,a.task_id,a.shotcaller_agent_id,
               a.kind,a.thread_id,a.backend,
               a.routing_name,a.display_agent,a.repository,a.issue,a.branch,a.worktree,
               r.runtime_instance_id,r.session_ref,r.endpoint,r.runtime_generation,
               r.capabilities_json
          FROM squad_champions sc
          JOIN agent_instances a ON a.agent_id=sc.champion_agent_id
          LEFT JOIN runtime_instances r
            ON r.actor_agent_id=a.agent_id AND r.status IN ('active','idle')
         WHERE sc.squad_id=? AND a.retired_at IS NULL
         ORDER BY sc.champion_agent_id LIMIT 10001
        """,
        (squad_id,),
    ).fetchall()
    if len(rows) > MAX_ACTIVE_CHAMPIONS:
        raise StorageRefusal(
            "active_champion_snapshot_too_large",
            f"active Champion snapshot exceeds {MAX_ACTIVE_CHAMPIONS} rows",
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        agent_id = str(row["champion_agent_id"])
        if agent_id in seen:
            raise StorageRefusal(
                "active_champion_snapshot_conflict",
                "Champion has more than one active runtime binding",
            )
        seen.add(agent_id)
        private_binding = {
            "agent_id": agent_id,
            "task_id": row["task_id"],
            "shotcaller_agent_id": row["shotcaller_agent_id"],
            "kind": row["kind"],
            "thread_id": row["thread_id"],
            "backend": row["backend"],
            "routing_name": row["routing_name"],
            "display_agent": row["display_agent"],
            "repository": row["repository"],
            "issue": row["issue"],
            "branch": row["branch"],
            "worktree": row["worktree"],
            "runtime_instance_id": row["runtime_instance_id"],
            "session_ref": row["session_ref"],
            "endpoint": row["endpoint"],
            "runtime_generation": row["runtime_generation"],
            "capabilities": row["capabilities_json"],
        }
        public_row = {
            "champion_agent_id": agent_id,
            "task_id": row["task_id"],
            "callsign": row["callsign"],
            "binding_digest": digest(private_binding),
        }
        public_row["row_digest"] = digest(public_row)
        result.append(public_row)
    return result


def _snapshot_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    return digest(list(rows))


def _cursor(snapshot_id: str, offset: int, snapshot_digest: str) -> str:
    value = {
        "offset": offset,
        "signature": digest(
            {"snapshot_id": snapshot_id, "offset": offset, "snapshot_digest": snapshot_digest}
        ),
    }
    return base64.urlsafe_b64encode(stable_json(value).encode("utf-8")).decode("ascii").rstrip("=")


def _cursor_offset(cursor: str, snapshot: Any) -> int:
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
        raise StorageRefusal("invalid_cursor", "snapshot cursor is invalid or belongs elsewhere")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(
            base64.b64decode(cursor + padding, altchars=b"-_", validate=True).decode("utf-8")
        )
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StorageRefusal(
            "invalid_cursor", "snapshot cursor is invalid or belongs elsewhere"
        ) from exc
    if not isinstance(value, dict) or set(value) != {"offset", "signature"}:
        raise StorageRefusal("invalid_cursor", "snapshot cursor is invalid or belongs elsewhere")
    offset = value["offset"]
    signature = value["signature"]
    total = int(snapshot["total_count"])
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 1 <= offset < total
        or not isinstance(signature, str)
    ):
        raise StorageRefusal("invalid_cursor", "snapshot cursor is invalid or belongs elsewhere")
    expected = digest(
        {
            "snapshot_id": snapshot["snapshot_id"],
            "offset": offset,
            "snapshot_digest": snapshot["digest"],
        }
    )
    if not hmac.compare_digest(signature, expected):
        raise StorageRefusal("invalid_cursor", "snapshot cursor is invalid or belongs elsewhere")
    return offset


def _operation(store: Any, operation_id: str) -> Any:
    row = store.connection.execute(
        "SELECT * FROM rollover_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        raise StorageRefusal("rollover_unknown", "rollover operation does not exist")
    return row


def rollover_status(store: Any, operation_id: str) -> Optional[dict[str, Any]]:
    operation = store.connection.execute(
        "SELECT * FROM rollover_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if operation is None:
        return None
    snapshot = store.connection.execute(
        "SELECT * FROM active_champion_snapshots WHERE snapshot_id=?",
        (operation["snapshot_id"],),
    ).fetchone()
    return {
        "schema": "league.rollover.v1",
        "operation_id": operation_id,
        "squad_id": operation["squad_id"],
        "predecessor_agent_id": operation["predecessor_agent_id"],
        "successor_agent_id": operation["successor_agent_id"],
        "state": operation["state"],
        "version": int(operation["version"]),
        "authority_kind": operation["authority_kind"],
        "plan_digest": operation["plan_digest"],
        "handoff_digest": operation["handoff_digest"],
        "snapshot": {
            "snapshot_id": snapshot["snapshot_id"],
            "version": int(snapshot["snapshot_version"]),
            "count": int(snapshot["total_count"]),
            "page_bound": int(snapshot["page_bound"]),
            "expires_at": snapshot["expires_at"],
            "digest": snapshot["digest"],
        },
        "acknowledgement_digest": operation["acknowledgement_digest"],
        "owner_event_id": operation["owner_event_id"],
        "owner_outbox_id": operation["owner_outbox_id"],
    }


def rollover_cleanup_target(store: Any, operation_id: str) -> Optional[dict[str, Any]]:
    """Return the exact switched predecessor binding eligible for drain cleanup."""

    row = store.connection.execute(
        """
        SELECT operation_id,predecessor_agent_id,successor_agent_id,state,version,
               owner_event_id,owner_outbox_id,squad_id,successor_runtime_instance_id
          FROM rollover_operations WHERE operation_id=?
        """,
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "operation_id": row["operation_id"],
        "predecessor_agent_id": row["predecessor_agent_id"],
        "successor_agent_id": row["successor_agent_id"],
        "state": row["state"],
        "version": int(row["version"]),
        "owner_event_id": row["owner_event_id"],
        "owner_outbox_id": row["owner_outbox_id"],
        "squad_id": row["squad_id"],
        "successor_runtime_instance_id": row["successor_runtime_instance_id"],
    }


def rollover_descendant_target(
    store: Any,
    operation_id: str,
    reconciliation_id: str,
    champion_agent_id: str,
    task_id: str,
    snapshot_digest: str,
    snapshot_row_digest: str,
    expected_rollover_version: int,
    expected_agent_version: int,
    expected_task_version: int,
    expected_assignment_version: int,
    expected_callsign_assignment_version: int,
) -> dict[str, Any]:
    """Read the exact frozen/live identity an adapter must verify before mutation."""

    retry = store.connection.execute(
        "SELECT event_type FROM events WHERE event_id=?", (reconciliation_id,)
    ).fetchone()
    if retry is not None:
        return {"reconciled": retry["event_type"] == "rollover_descendant_reconciled"}
    operation = _operation(store, operation_id)
    if (
        operation["state"] not in {"switched", "completed"}
        or int(operation["version"]) != expected_rollover_version
    ):
        raise StorageRefusal(
            "rollover_not_committed",
            "descendant verification requires the exact committed rollover version",
        )
    snapshot = store.connection.execute(
        "SELECT digest FROM active_champion_snapshots WHERE snapshot_id=?",
        (operation["snapshot_id"],),
    ).fetchone()
    row = store.connection.execute(
        """
        SELECT champion_agent_id,task_id,callsign,binding_digest,row_digest
          FROM active_champion_snapshot_rows
         WHERE snapshot_id=? AND champion_agent_id=?
        """,
        (operation["snapshot_id"], champion_agent_id),
    ).fetchone()
    if (
        snapshot is None
        or snapshot["digest"] != snapshot_digest
        or row is None
        or row["task_id"] != task_id
        or row["row_digest"] != snapshot_row_digest
    ):
        raise StorageRefusal(
            "descendant_snapshot_mismatch", "descendant verification snapshot changed"
        )
    champion = store.connection.execute(
        "SELECT * FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
        (champion_agent_id,),
    ).fetchone()
    task = store.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if (
        champion is None
        or champion["role"] != "champion"
        or champion["task_id"] != task_id
        or champion["callsign"] != row["callsign"]
        or champion["shotcaller_agent_id"] != operation["predecessor_agent_id"]
        or int(champion["version"]) != expected_agent_version
        or task is None
        or task["champion_agent_id"] != champion_agent_id
        or task["coordinator_agent_id"] != operation["predecessor_agent_id"]
        or int(task["version"]) != expected_task_version
        or task["state"]
        in {"completed", "complete", "failed", "cancelled", "canceled", "rejected"}
    ):
        raise StorageRefusal(
            "descendant_identity_stale",
            "descendant verification identity or canonical coordinator changed",
        )
    runtimes = store.connection.execute(
        "SELECT * FROM runtime_instances WHERE actor_agent_id=? ORDER BY runtime_instance_id",
        (champion_agent_id,),
    ).fetchall()
    if len(runtimes) > 1:
        raise StorageRefusal(
            "descendant_runtime_ambiguous", "descendant has multiple canonical runtime identities"
        )
    runtime = runtimes[0] if runtimes else None
    private_binding = {
        "agent_id": champion_agent_id,
        "task_id": champion["task_id"],
        "shotcaller_agent_id": champion["shotcaller_agent_id"],
        "kind": champion["kind"],
        "thread_id": champion["thread_id"],
        "backend": champion["backend"],
        "routing_name": champion["routing_name"],
        "display_agent": champion["display_agent"],
        "repository": champion["repository"],
        "issue": champion["issue"],
        "branch": champion["branch"],
        "worktree": champion["worktree"],
        "runtime_instance_id": None if runtime is None else runtime["runtime_instance_id"],
        "session_ref": None if runtime is None else runtime["session_ref"],
        "endpoint": None if runtime is None else runtime["endpoint"],
        "runtime_generation": None if runtime is None else runtime["runtime_generation"],
        "capabilities": None if runtime is None else runtime["capabilities_json"],
    }
    if digest(private_binding) != row["binding_digest"]:
        raise StorageRefusal(
            "descendant_snapshot_mismatch",
            "descendant canonical binding no longer matches the frozen row",
        )
    callsigns = store.connection.execute(
        """
        SELECT callsign_assignment_id,requirements_json,version FROM callsign_assignments
         WHERE agent_id=? AND callsign=? AND role='champion'
           AND scope_kind='task' AND scope_id=? AND state='active'
         ORDER BY callsign_assignment_id
        """,
        (champion_agent_id, row["callsign"], task_id),
    ).fetchall()
    if len(callsigns) != 1:
        raise StorageRefusal(
            "descendant_callsign_ambiguous", "descendant callsign binding is not exact"
        )
    assignment = store.connection.execute(
        "SELECT task_assignment_id,version FROM task_assignments WHERE task_id=?", (task_id,)
    ).fetchone()
    if (
        int(callsigns[0]["version"]) != expected_callsign_assignment_version
        or (assignment is None and expected_assignment_version != 0)
        or (assignment is not None and int(assignment["version"]) != expected_assignment_version)
    ):
        raise StorageRefusal(
            "version_conflict", "descendant assignment or callsign version changed"
        )
    return {
        "reconciled": False,
        "champion_agent_id": champion_agent_id,
        "task_id": task_id,
        "callsign": row["callsign"],
        "kind": champion["kind"],
        "thread_id": champion["thread_id"],
        "backend": champion["backend"],
        "routing_name": champion["routing_name"],
        "display_agent": champion["display_agent"],
        "address": champion["address"],
        "worktree": champion["worktree"],
        "snapshot_row_digest": row["row_digest"],
        "runtime_count": len(runtimes),
        "runtime": None if runtime is None else dict(runtime),
        "capabilities": json.loads(callsigns[0]["requirements_json"]),
        "task_assignment_id": None if assignment is None else assignment["task_assignment_id"],
        "callsign_assignment_id": callsigns[0]["callsign_assignment_id"],
    }


def prepare_rollover(
    store: Any,
    operation_id: str,
    squad_id: str,
    predecessor_agent_id: str,
    successor_agent_id: str,
    callsign_assignment_id: str,
    expected_owner_version: int,
    expected_owner_fence: int,
    authority_kind: str,
    authority_digest: str,
    required_capabilities: Sequence[str],
    plan: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    timestamp(at, "rollover preparation time")
    if (
        not all(
            (
                operation_id,
                squad_id,
                predecessor_agent_id,
                successor_agent_id,
                callsign_assignment_id,
                authority_digest,
            )
        )
        or predecessor_agent_id == successor_agent_id
        or authority_kind not in {"explicit", "automatic"}
        or expected_owner_version < 1
        or expected_owner_fence < 1
    ):
        raise StorageRefusal("invalid_rollover", "rollover identity or authority is invalid")
    required = capabilities(required_capabilities)
    plan_value, plan_digest = _handoff_plan(plan, squad_id)
    if datetime.fromisoformat(plan_value["expires_at"].replace("Z", "+00:00")) <= datetime.fromisoformat(
        at.replace("Z", "+00:00")
    ):
        raise StorageRefusal("invalid_handoff", "handoff expiry must be after preparation")
    snapshot_id = f"snapshot:{operation_id}"
    try:
        with store._transaction():
            retry = store.connection.execute(
                "SELECT * FROM rollover_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if retry is not None:
                exact = (
                    retry["squad_id"] == squad_id
                    and retry["predecessor_agent_id"] == predecessor_agent_id
                    and retry["successor_agent_id"] == successor_agent_id
                    and retry["callsign_assignment_id"] == callsign_assignment_id
                    and int(retry["expected_owner_version"]) == expected_owner_version
                    and int(retry["expected_owner_fence"]) == expected_owner_fence
                    and retry["authority_kind"] == authority_kind
                    and retry["authority_digest"] == authority_digest
                    and retry["plan_digest"] == plan_digest
                    and tuple(json.loads(retry["required_capabilities_json"])) == required
                )
                if not exact:
                    raise StorageRefusal("rollover_conflict", "rollover retry changed identity")
                result = rollover_status(store, operation_id)
                assert result is not None
                result["idempotent"] = True
                return result
            squad = store.connection.execute(
                "SELECT * FROM squads WHERE squad_id=? AND state='active'", (squad_id,)
            ).fetchone()
            if (
                squad is None
                or squad["shotcaller_agent_id"] != predecessor_agent_id
                or int(squad["version"]) != expected_owner_version
                or int(squad["owner_fence"]) != expected_owner_fence
            ):
                raise StorageRefusal(
                    "owner_conflict", "stable Squad owner version/fence precondition failed"
                )
            intake = store.connection.execute(
                "SELECT state,fence FROM shotcaller_intake WHERE agent_id=? AND squad_id=?",
                (predecessor_agent_id, squad_id),
            ).fetchone()
            if intake is None or intake["state"] != "accepting" or int(intake["fence"]) != expected_owner_fence:
                raise StorageRefusal("owner_not_accepting", "predecessor intake is not accepting")
            assignment = store.connection.execute(
                "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                (callsign_assignment_id,),
            ).fetchone()
            if (
                assignment is None
                or assignment["agent_id"] != successor_agent_id
                or assignment["role"] != "shotcaller"
                or assignment["scope_kind"] != "squad"
                or assignment["scope_id"] != squad_id
                or assignment["state"] not in {"reserved", "active"}
            ):
                raise StorageRefusal(
                    "successor_identity_mismatch", "successor callsign reservation is not exact"
                )
            successor_owner = store.connection.execute(
                "SELECT 1 FROM squads WHERE shotcaller_agent_id=?", (successor_agent_id,)
            ).fetchone()
            if successor_owner is not None:
                raise StorageRefusal("successor_conflict", "successor already owns another Squad")
            rows = _snapshot_rows(store, squad_id)
            snapshot_digest = _snapshot_digest(rows)
            handoff_digest = digest(
                {
                    "operation_id": operation_id,
                    "squad_id": squad_id,
                    "predecessor_agent_id": predecessor_agent_id,
                    "successor_agent_id": successor_agent_id,
                    "authority_kind": authority_kind,
                    "authority_digest": authority_digest,
                    "required_capabilities": list(required),
                    "plan_digest": plan_digest,
                    "snapshot_id": snapshot_id,
                    "snapshot_version": 1,
                    "snapshot_count": len(rows),
                    "snapshot_digest": snapshot_digest,
                }
            )
            store.connection.execute(
                """
                INSERT INTO rollover_operations
                  (operation_id,squad_id,predecessor_agent_id,successor_agent_id,
                   callsign_assignment_id,state,authority_kind,authority_digest,
                   required_capabilities_json,plan_json,plan_digest,handoff_digest,
                   expected_owner_version,expected_owner_fence,snapshot_id,
                   acknowledgement_digest,successor_runtime_instance_id,owner_event_id,
                   owner_outbox_id,switch_receipt_digest,cleanup_receipt_digest,
                   version,created_at,updated_at)
                VALUES(?,?,?,?,?,'prepared',?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,1,?,?)
                """,
                (
                    operation_id,
                    squad_id,
                    predecessor_agent_id,
                    successor_agent_id,
                    callsign_assignment_id,
                    authority_kind,
                    authority_digest,
                    stable_json(required),
                    stable_json(plan_value),
                    plan_digest,
                    handoff_digest,
                    expected_owner_version,
                    expected_owner_fence,
                    snapshot_id,
                    at,
                    at,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO active_champion_snapshots
                  (snapshot_id,operation_id,squad_id,snapshot_version,total_count,page_bound,
                   expires_at,digest,created_at)
                VALUES(?,?,?,1,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    operation_id,
                    squad_id,
                    len(rows),
                    plan_value["page_bound"],
                    plan_value["expires_at"],
                    snapshot_digest,
                    at,
                ),
            )
            store.connection.executemany(
                """
                INSERT INTO active_champion_snapshot_rows
                  (snapshot_id,ordinal,champion_agent_id,task_id,callsign,binding_digest,row_digest)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    (
                        snapshot_id,
                        ordinal,
                        row["champion_agent_id"],
                        row["task_id"],
                        row["callsign"],
                        row["binding_digest"],
                        row["row_digest"],
                    )
                    for ordinal, row in enumerate(rows)
                ),
            )
            store.connection.execute(
                """
                INSERT INTO shotcaller_intake(agent_id,squad_id,state,fence,version,updated_at)
                VALUES(?,?,'draining',?,1,?)
                """,
                (successor_agent_id, squad_id, expected_owner_fence, at),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,NULL,NULL,?,?,'rollover_prepared','prepared','rollover prepared',?,?,
                       'squad',?)
                """,
                (
                    f"rollover:{operation_id}:prepared",
                    squad_id,
                    expected_owner_version,
                    at,
                    stable_json(
                        {
                            "operation_id": operation_id,
                            "handoff_digest": handoff_digest,
                            "snapshot_id": snapshot_id,
                            "snapshot_version": 1,
                            "snapshot_count": len(rows),
                            "snapshot_digest": snapshot_digest,
                        }
                    ),
                    squad_id,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "rollover preparation conflicted with canonical state"
        ) from exc
    result = rollover_status(store, operation_id)
    assert result is not None
    result["idempotent"] = False
    return result


def rollover_bindings(
    store: Any,
    operation_id: str,
    at: str,
    *,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    timestamp(at, "snapshot read time")
    operation = _operation(store, operation_id)
    snapshot = store.connection.execute(
        "SELECT * FROM active_champion_snapshots WHERE snapshot_id=?",
        (operation["snapshot_id"],),
    ).fetchone()
    if datetime.fromisoformat(at.replace("Z", "+00:00")) > datetime.fromisoformat(
        str(snapshot["expires_at"]).replace("Z", "+00:00")
    ):
        raise StorageRefusal(
            "active_champion_snapshot_stale", "active Champion binding snapshot has expired"
        )
    page_limit = int(snapshot["page_bound"]) if limit is None else limit
    if not isinstance(page_limit, int) or isinstance(page_limit, bool) or not 1 <= page_limit <= int(snapshot["page_bound"]):
        raise StorageRefusal("invalid_limit", "snapshot limit exceeds its configured page bound")
    total = int(snapshot["total_count"])
    offset = 0
    if cursor is not None:
        offset = _cursor_offset(cursor, snapshot)
    rows = [
        dict(row)
        for row in store.connection.execute(
            """
            SELECT champion_agent_id,task_id,callsign,binding_digest,row_digest
              FROM active_champion_snapshot_rows
             WHERE snapshot_id=? AND ordinal>=? ORDER BY ordinal LIMIT ?
            """,
            (snapshot["snapshot_id"], offset, page_limit),
        )
    ]
    next_offset = offset + len(rows)
    next_cursor = (
        _cursor(snapshot["snapshot_id"], next_offset, snapshot["digest"])
        if next_offset < total
        else None
    )
    page = {
        "offset": offset,
        "count": len(rows),
        "digest": digest(rows),
        "rows": rows,
    }
    return {
        "schema": "league.rollover-bindings.v1",
        "operation_id": operation_id,
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_version": int(snapshot["snapshot_version"]),
        "snapshot_count": total,
        "snapshot_digest": snapshot["digest"],
        "page_bound": int(snapshot["page_bound"]),
        "expires_at": snapshot["expires_at"],
        "page": page,
        "next_cursor": next_cursor,
    }


def _verify_pages(
    store: Any, snapshot: Any, pages: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    total = int(snapshot["total_count"])
    if isinstance(pages, (str, bytes)) or not pages or len(pages) > max(total, 1):
        raise StorageRefusal(
            "active_champion_snapshot_incomplete", "snapshot page receipts exceed their bound"
        )
    offset = 0
    all_rows: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, Mapping) or set(page) != {"offset", "count", "digest", "rows"}:
            raise StorageRefusal(
                "active_champion_snapshot_incomplete", "snapshot page receipt is invalid"
            )
        rows = page["rows"]
        if (
            page["offset"] != offset
            or not isinstance(rows, list)
            or page["count"] != len(rows)
            or page["digest"] != digest(rows)
            or len(rows) > int(snapshot["page_bound"])
            or (not rows and (total != 0 or len(pages) != 1))
        ):
            raise StorageRefusal(
                "active_champion_snapshot_incomplete", "snapshot page coverage is missing or repeated"
            )
        all_rows.extend(rows)
        offset += len(rows)
    if offset != total or digest(all_rows) != snapshot["digest"]:
        raise StorageRefusal(
            "active_champion_snapshot_incomplete", "snapshot pages do not cover the frozen digest"
        )
    canonical = store.connection.execute(
        """
        SELECT champion_agent_id,task_id,callsign,binding_digest,row_digest
          FROM active_champion_snapshot_rows WHERE snapshot_id=? ORDER BY ordinal
        """,
        (snapshot["snapshot_id"],),
    )
    for expected in all_rows:
        observed = canonical.fetchone()
        if observed is None or dict(observed) != expected:
            raise StorageRefusal(
                "active_champion_snapshot_incomplete",
                "snapshot page rows differ from canonical rows",
            )
    if canonical.fetchone() is not None:
        raise StorageRefusal(
            "active_champion_snapshot_incomplete", "snapshot page rows differ from canonical rows"
        )
    return all_rows


def acknowledge_rollover(
    store: Any,
    operation_id: str,
    successor_agent_id: str,
    runtime_instance_id: str,
    handoff_digest: str,
    snapshot_version: int,
    snapshot_count: int,
    snapshot_digest: str,
    pages: Sequence[Mapping[str, Any]],
    at: str,
) -> dict[str, Any]:
    timestamp(at, "rollover acknowledgement time")
    try:
        with store._transaction():
            operation = _operation(store, operation_id)
            if operation["successor_agent_id"] != successor_agent_id:
                raise StorageRefusal("successor_identity_mismatch", "successor identity changed")
            snapshot = store.connection.execute(
                "SELECT * FROM active_champion_snapshots WHERE snapshot_id=?",
                (operation["snapshot_id"],),
            ).fetchone()
            if datetime.fromisoformat(at.replace("Z", "+00:00")) > datetime.fromisoformat(
                str(snapshot["expires_at"]).replace("Z", "+00:00")
            ):
                raise StorageRefusal(
                    "active_champion_snapshot_stale", "active Champion snapshot expired"
                )
            if (
                handoff_digest != operation["handoff_digest"]
                or snapshot_version != int(snapshot["snapshot_version"])
                or snapshot_count != int(snapshot["total_count"])
                or snapshot_digest != snapshot["digest"]
            ):
                raise StorageRefusal("handoff_ack_mismatch", "handoff or snapshot digest changed")
            canonical_pages = _verify_pages(store, snapshot, pages)
            runtime, declared = _runtime_identity(store, successor_agent_id, runtime_instance_id)
            required = tuple(json.loads(operation["required_capabilities_json"]))
            if any(item not in declared for item in required):
                raise StorageRefusal(
                    "successor_capability_mismatch", "successor lacks a required capability"
                )
            assignment = store.connection.execute(
                "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                (operation["callsign_assignment_id"],),
            ).fetchone()
            if (
                assignment["state"] != "active"
                or not assignment["acceptance_digest"]
                or assignment["runtime_instance_id"] != runtime_instance_id
            ):
                raise StorageRefusal(
                    "successor_identity_mismatch",
                    "successor callsign acceptance does not match the exact runtime",
                )
            squad = store.connection.execute(
                "SELECT * FROM squads WHERE squad_id=?", (operation["squad_id"],)
            ).fetchone()
            if (
                squad["shotcaller_agent_id"] != operation["predecessor_agent_id"]
                or int(squad["version"]) != int(operation["expected_owner_version"])
                or int(squad["owner_fence"]) != int(operation["expected_owner_fence"])
            ):
                raise StorageRefusal(
                    "active_champion_snapshot_stale", "Squad owner fence changed before acknowledgement"
                )
            acknowledgement = {
                "operation_id": operation_id,
                "successor_agent_id": successor_agent_id,
                "runtime_instance_id": runtime_instance_id,
                "runtime_session_digest": digest(
                    {
                        "harness_kind": runtime["harness_kind"],
                        "session_identity": runtime["session_ref"],
                        "endpoint_generation": runtime["runtime_generation"],
                    }
                ),
                "capabilities": list(declared),
                "handoff_digest": handoff_digest,
                "snapshot_version": snapshot_version,
                "snapshot_count": snapshot_count,
                "snapshot_digest": snapshot_digest,
                "page_receipts_digest": digest(canonical_pages),
            }
            acknowledgement_digest = digest(acknowledgement)
            if operation["state"] == "acknowledged":
                if (
                    operation["acknowledgement_digest"] != acknowledgement_digest
                    or operation["successor_runtime_instance_id"] != runtime_instance_id
                ):
                    raise StorageRefusal("handoff_ack_mismatch", "acknowledgement retry changed")
                result = rollover_status(store, operation_id)
                assert result is not None
                result["idempotent"] = True
                return result
            if operation["state"] != "prepared":
                raise StorageRefusal("rollover_conflict", "rollover can no longer be acknowledged")
            next_version = int(operation["version"]) + 1
            store.connection.execute(
                """
                UPDATE rollover_operations SET state='acknowledged',acknowledgement_digest=?,
                       successor_runtime_instance_id=?,version=?,updated_at=?
                 WHERE operation_id=? AND state='prepared'
                """,
                (acknowledgement_digest, runtime_instance_id, next_version, at, operation_id),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,NULL,NULL,?,?,'rollover_acknowledged','acknowledged',
                       'successor acknowledged exact handoff',?,?,'squad',?)
                """,
                (
                    f"rollover:{operation_id}:acknowledged",
                    operation["squad_id"],
                    operation["expected_owner_version"],
                    at,
                    stable_json(
                        {
                            "operation_id": operation_id,
                            "acknowledgement_digest": acknowledgement_digest,
                            "snapshot_version": snapshot_version,
                            "snapshot_count": snapshot_count,
                            "snapshot_digest": snapshot_digest,
                        }
                    ),
                    operation["squad_id"],
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "rollover acknowledgement conflicted with canonical state"
        ) from exc
    result = rollover_status(store, operation_id)
    assert result is not None
    result["idempotent"] = False
    return result


def _unsafe_inflight(store: Any, predecessor_agent_id: str, at: str) -> Optional[str]:
    claim = store.connection.execute(
        """
        SELECT c.request_id FROM request_claims c JOIN requests r ON r.request_id=c.request_id
         WHERE r.owner_agent_id=? AND c.released_at IS NULL AND c.leased_until>? LIMIT 1
        """,
        (predecessor_agent_id, at),
    ).fetchone()
    if claim is not None:
        return "active_request_claim"
    delivery = store.connection.execute(
        """
        SELECT outbox_id FROM delivery_outbox
         WHERE recipient_agent_id=? AND state IN ('in_flight','awaiting_receipt') LIMIT 1
        """,
        (predecessor_agent_id,),
    ).fetchone()
    return "inflight_delivery" if delivery is not None else None


def commit_rollover(
    store: Any,
    operation_id: str,
    expected_owner_version: int,
    expected_owner_fence: int,
    owner_event_id: str,
    owner_outbox_id: str,
    at: str,
    *,
    fault: Optional[FaultInjector] = None,
) -> dict[str, Any]:
    timestamp(at, "rollover commit time")
    if not owner_event_id or not owner_outbox_id:
        raise StorageRefusal("invalid_rollover", "owner event and outbox identity are required")
    try:
        with store._transaction():
            operation = _operation(store, operation_id)
            if operation["state"] in {"switched", "completed"}:
                if operation["owner_event_id"] != owner_event_id or operation["owner_outbox_id"] != owner_outbox_id:
                    raise StorageRefusal("rollover_conflict", "owner switch retry changed receipts")
                result = rollover_status(store, operation_id)
                assert result is not None
                result["idempotent"] = True
                return result
            if operation["state"] != "acknowledged":
                raise StorageRefusal("handoff_ack_mismatch", "owner switch requires acknowledgement")
            if (
                expected_owner_version != int(operation["expected_owner_version"])
                or expected_owner_fence != int(operation["expected_owner_fence"])
            ):
                raise StorageRefusal("owner_conflict", "owner switch fence differs from preparation")
            squad = store.connection.execute(
                "SELECT * FROM squads WHERE squad_id=?", (operation["squad_id"],)
            ).fetchone()
            if (
                squad["shotcaller_agent_id"] != operation["predecessor_agent_id"]
                or int(squad["version"]) != expected_owner_version
                or int(squad["owner_fence"]) != expected_owner_fence
            ):
                raise StorageRefusal("owner_conflict", "Squad owner CAS precondition failed")
            snapshot = store.connection.execute(
                "SELECT * FROM active_champion_snapshots WHERE snapshot_id=?",
                (operation["snapshot_id"],),
            ).fetchone()
            if datetime.fromisoformat(at.replace("Z", "+00:00")) > datetime.fromisoformat(
                str(snapshot["expires_at"]).replace("Z", "+00:00")
            ):
                raise StorageRefusal(
                    "active_champion_snapshot_stale",
                    "active Champion snapshot expired before owner switch",
                )
            current_rows = _snapshot_rows(store, operation["squad_id"])
            if (
                len(current_rows) != int(snapshot["total_count"])
                or _snapshot_digest(current_rows) != snapshot["digest"]
            ):
                raise StorageRefusal(
                    "active_champion_snapshot_stale",
                    "active Champion bindings changed before owner switch",
                )
            _, declared = _runtime_identity(
                store,
                operation["successor_agent_id"],
                operation["successor_runtime_instance_id"],
            )
            required = tuple(json.loads(operation["required_capabilities_json"]))
            if any(item not in declared for item in required):
                raise StorageRefusal(
                    "successor_capability_mismatch", "successor capability proof changed"
                )
            unsafe = _unsafe_inflight(store, operation["predecessor_agent_id"], at)
            if unsafe:
                raise StorageRefusal(
                    "unsafe_handoff_boundary", f"owner switch refused: {unsafe}"
                )
            next_owner_version = expected_owner_version + 1
            next_fence = expected_owner_fence + 1
            changed = store.connection.execute(
                """
                UPDATE squads SET shotcaller_agent_id=?,version=?,owner_fence=?,updated_at=?
                 WHERE squad_id=? AND shotcaller_agent_id=? AND version=? AND owner_fence=?
                """,
                (
                    operation["successor_agent_id"],
                    next_owner_version,
                    next_fence,
                    at,
                    operation["squad_id"],
                    operation["predecessor_agent_id"],
                    expected_owner_version,
                    expected_owner_fence,
                ),
            )
            if changed.rowcount != 1:
                raise StorageRefusal("owner_conflict", "Squad owner CAS precondition failed")
            if fault:
                fault("after_owner_cas")
            predecessor_fenced = store.connection.execute(
                """
                UPDATE shotcaller_intake SET state='draining',fence=?,version=version+1,updated_at=?
                 WHERE agent_id=? AND squad_id=? AND state='accepting'
                """,
                (
                    next_fence,
                    at,
                    operation["predecessor_agent_id"],
                    operation["squad_id"],
                ),
            )
            if predecessor_fenced.rowcount != 1:
                raise StorageRefusal(
                    "owner_conflict", "predecessor intake fence CAS failed"
                )
            successor_opened = store.connection.execute(
                """
                UPDATE shotcaller_intake SET state='accepting',fence=?,version=version+1,updated_at=?
                 WHERE agent_id=? AND squad_id=? AND state='draining'
                """,
                (next_fence, at, operation["successor_agent_id"], operation["squad_id"]),
            )
            if successor_opened.rowcount != 1:
                raise StorageRefusal(
                    "successor_identity_mismatch", "successor intake fence CAS failed"
                )
            store.connection.execute(
                """
                UPDATE requests
                   SET owner_agent_id=CASE
                         WHEN owner_squad_id=:squad_id THEN :successor
                         ELSE owner_agent_id
                       END,
                       owner_squad_id=CASE
                         WHEN owner_squad_id=:squad_id THEN :squad_id
                         ELSE owner_squad_id
                       END,
                       return_to_agent_id=CASE
                         WHEN return_to_agent_id=:predecessor THEN :successor
                         ELSE return_to_agent_id
                       END,
                       pending_owner_agent_id=CASE
                         WHEN state='routed' AND (
                           pending_owner_squad_id=:squad_id OR
                           pending_owner_agent_id=:predecessor
                         ) THEN :successor
                         ELSE pending_owner_agent_id
                       END,
                       pending_owner_squad_id=CASE
                         WHEN state='routed' AND (
                           pending_owner_squad_id=:squad_id OR
                           pending_owner_agent_id=:predecessor
                         ) THEN :squad_id
                         ELSE pending_owner_squad_id
                       END,
                       version=version+1,updated_at=:at
                 WHERE state NOT IN ('answered','cancelled')
                   AND (
                     owner_squad_id=:squad_id OR
                     (state='routed' AND (
                       pending_owner_squad_id=:squad_id OR
                       pending_owner_agent_id=:predecessor
                     ))
                   )
                """,
                {
                    "successor": operation["successor_agent_id"],
                    "predecessor": operation["predecessor_agent_id"],
                    "squad_id": operation["squad_id"],
                    "at": at,
                },
            )
            switch_receipt = digest(
                {
                    "operation_id": operation_id,
                    "squad_id": operation["squad_id"],
                    "from_agent_id": operation["predecessor_agent_id"],
                    "to_agent_id": operation["successor_agent_id"],
                    "from_version": expected_owner_version,
                    "to_version": next_owner_version,
                    "from_fence": expected_owner_fence,
                    "to_fence": next_fence,
                    "acknowledgement_digest": operation["acknowledgement_digest"],
                    "snapshot_digest": snapshot["digest"],
                }
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,NULL,NULL,?,?,'owner_changed','draining','Squad owner changed',?,?,
                       'squad',?)
                """,
                (
                    owner_event_id,
                    operation["squad_id"],
                    next_owner_version,
                    at,
                    stable_json(
                        {
                            "operation_id": operation_id,
                            "from_agent_id": operation["predecessor_agent_id"],
                            "to_agent_id": operation["successor_agent_id"],
                            "owner_fence": next_fence,
                            "switch_receipt_digest": switch_receipt,
                        }
                    ),
                    operation["squad_id"],
                ),
            )
            if fault:
                fault("after_owner_event")
            store.connection.execute(
                """
                INSERT INTO delivery_outbox
                  (outbox_id,event_id,recipient_agent_id,state,available_at,attempt_count)
                VALUES(?,?,?,'pending',?,0)
                """,
                (owner_outbox_id, owner_event_id, operation["successor_agent_id"], at),
            )
            if fault:
                fault("after_owner_outbox")
            next_version = int(operation["version"]) + 1
            store.connection.execute(
                """
                UPDATE rollover_operations SET state='switched',owner_event_id=?,owner_outbox_id=?,
                       switch_receipt_digest=?,version=?,updated_at=? WHERE operation_id=?
                """,
                (
                    owner_event_id,
                    owner_outbox_id,
                    switch_receipt,
                    next_version,
                    at,
                    operation_id,
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "atomic Squad owner switch conflicted with canonical state"
        ) from exc
    result = rollover_status(store, operation_id)
    assert result is not None
    result["idempotent"] = False
    return result


def reconcile_rollover_descendant(
    store: Any,
    operation_id: str,
    reconciliation_id: str,
    champion_agent_id: str,
    task_id: str,
    runtime_instance_id: str,
    snapshot_digest: str,
    snapshot_row_digest: str,
    expected_rollover_version: int,
    expected_agent_version: int,
    expected_task_version: int,
    expected_assignment_version: int,
    expected_callsign_assignment_version: int,
    runtime_receipt: Optional[Mapping[str, Any]],
    pending_outbox_ids: Sequence[str],
    at: str,
) -> dict[str, Any]:
    """Bind one exact frozen descendant to its committed successor.

    Runtime evidence must come from the exact live adapter inspection. A
    missing imported runtime may be registered in this same transaction; no
    request, dispatch, launch, or delivery history is invented.
    """

    timestamp(at, "descendant reconciliation time")
    if (
        not all(
            (
                operation_id,
                reconciliation_id,
                champion_agent_id,
                task_id,
                runtime_instance_id,
                snapshot_digest,
                snapshot_row_digest,
            )
        )
        or min(
            expected_rollover_version,
            expected_agent_version,
            expected_task_version,
            expected_callsign_assignment_version,
        )
        < 1
        or expected_assignment_version < 0
    ):
        raise StorageRefusal(
            "invalid_descendant_reconciliation",
            "descendant reconciliation identity and versions are required",
        )
    declared_outboxes = tuple(pending_outbox_ids)
    if tuple(sorted(set(declared_outboxes))) != declared_outboxes:
        raise StorageRefusal(
            "invalid_descendant_reconciliation",
            "pending descendant outbox identities must be sorted and duplicate-free",
        )

    try:
        with store._transaction():
            retry = store.connection.execute(
                "SELECT event_type,task_id,detail_json FROM events WHERE event_id=?",
                (reconciliation_id,),
            ).fetchone()
            if retry is not None:
                try:
                    detail = json.loads(retry["detail_json"])
                    receipt = detail["receipt"]
                    receipt_digest = detail["receipt_digest"]
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise StorageRefusal(
                        "descendant_reconciliation_conflict",
                        "stored descendant reconciliation receipt is malformed",
                    ) from exc
                expected_retry = {
                    "operation_id": operation_id,
                    "reconciliation_id": reconciliation_id,
                    "champion_agent_id": champion_agent_id,
                    "task_id": task_id,
                    "runtime_instance_id": runtime_instance_id,
                    "snapshot_digest": snapshot_digest,
                    "snapshot_row_digest": snapshot_row_digest,
                    "expected_rollover_version": expected_rollover_version,
                    "expected_agent_version": expected_agent_version,
                    "expected_task_version": expected_task_version,
                    "expected_assignment_version": expected_assignment_version,
                    "expected_callsign_assignment_version": expected_callsign_assignment_version,
                    "retargeted_outbox_ids": list(declared_outboxes),
                }
                if (
                    retry["event_type"] != "rollover_descendant_reconciled"
                    or retry["task_id"] != task_id
                    or any(receipt.get(key) != value for key, value in expected_retry.items())
                    or digest(receipt) != receipt_digest
                ):
                    raise StorageRefusal(
                        "descendant_reconciliation_conflict",
                        "descendant reconciliation retry changed immutable identity",
                    )
                return {
                    "operation_id": operation_id,
                    "reconciliation_id": reconciliation_id,
                    "champion_agent_id": champion_agent_id,
                    "task_id": task_id,
                    "runtime_instance_id": runtime_instance_id,
                    "successor_agent_id": receipt["successor_agent_id"],
                    "created_assignment": bool(receipt["created_assignment"]),
                    "created_runtime": bool(receipt["created_runtime"]),
                    "task_version": int(receipt["task_version"]),
                    "retargeted_outbox_ids": list(declared_outboxes),
                    "pending_delivery_count": int(receipt["pending_delivery_count"]),
                    "receipt_digest": receipt_digest,
                    "idempotent": True,
                }

            operation = _operation(store, operation_id)
            if operation["state"] not in {"switched", "completed"}:
                raise StorageRefusal(
                    "rollover_not_committed",
                    "descendant reconciliation requires the committed successor",
                )
            if int(operation["version"]) != expected_rollover_version:
                raise StorageRefusal(
                    "version_conflict",
                    "descendant reconciliation rollover version changed",
                )
            snapshot = store.connection.execute(
                "SELECT * FROM active_champion_snapshots WHERE snapshot_id=?",
                (operation["snapshot_id"],),
            ).fetchone()
            if snapshot is None or snapshot["digest"] != snapshot_digest:
                raise StorageRefusal(
                    "active_champion_snapshot_stale",
                    "descendant reconciliation snapshot digest changed",
                )
            snapshot_row = store.connection.execute(
                """
                SELECT champion_agent_id,task_id,callsign,binding_digest,row_digest
                  FROM active_champion_snapshot_rows
                 WHERE snapshot_id=? AND champion_agent_id=?
                """,
                (operation["snapshot_id"], champion_agent_id),
            ).fetchone()
            if (
                snapshot_row is None
                or snapshot_row["task_id"] != task_id
                or snapshot_row["row_digest"] != snapshot_row_digest
                or digest(
                    {
                        "champion_agent_id": snapshot_row["champion_agent_id"],
                        "task_id": snapshot_row["task_id"],
                        "callsign": snapshot_row["callsign"],
                        "binding_digest": snapshot_row["binding_digest"],
                    }
                )
                != snapshot_row["row_digest"]
            ):
                raise StorageRefusal(
                    "descendant_snapshot_mismatch",
                    "descendant identity is not the exact frozen snapshot row",
                )
            squad = store.connection.execute(
                "SELECT shotcaller_agent_id,state,version,owner_fence FROM squads WHERE squad_id=?",
                (operation["squad_id"],),
            ).fetchone()
            if (
                squad is None
                or squad["state"] != "active"
                or squad["shotcaller_agent_id"] != operation["successor_agent_id"]
                or int(squad["version"]) != int(operation["expected_owner_version"]) + 1
                or int(squad["owner_fence"]) != int(operation["expected_owner_fence"]) + 1
            ):
                raise StorageRefusal(
                    "owner_conflict",
                    "committed successor owner identity or fence changed",
                )
            membership = store.connection.execute(
                "SELECT joined_at FROM squad_champions WHERE squad_id=? AND champion_agent_id=?",
                (operation["squad_id"], champion_agent_id),
            ).fetchone()
            champion = store.connection.execute(
                "SELECT * FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                (champion_agent_id,),
            ).fetchone()
            if (
                membership is None
                or champion is None
                or champion["role"] != "champion"
                or champion["task_id"] != task_id
                or champion["callsign"] != snapshot_row["callsign"]
                or champion["shotcaller_agent_id"] != operation["predecessor_agent_id"]
                or int(champion["version"]) != expected_agent_version
            ):
                raise StorageRefusal(
                    "descendant_identity_stale",
                    "Champion identity no longer matches the committed rollover descendant",
                )
            task = store.connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if (
                task is None
                or task["champion_agent_id"] != champion_agent_id
                or task["coordinator_agent_id"] != operation["predecessor_agent_id"]
                or int(task["version"]) != expected_task_version
                or task["state"] in {"completed", "complete", "failed", "cancelled", "canceled", "rejected"}
            ):
                raise StorageRefusal(
                    "descendant_task_stale",
                    "descendant task identity, version, or lifecycle changed",
                )
            runtimes = store.connection.execute(
                """
                SELECT runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,
                       endpoint,runtime_generation,status,verified,capabilities_json
                  FROM runtime_instances
                 WHERE actor_agent_id=?
                 ORDER BY runtime_instance_id
                """,
                (champion_agent_id,),
            ).fetchall()
            if len(runtimes) > 1:
                raise StorageRefusal(
                    "descendant_runtime_ambiguous",
                    "descendant reconciliation refuses multiple canonical runtimes",
                )
            receipt_keys = {
                "schema",
                "verified",
                "champion_agent_id",
                "task_id",
                "runtime_instance_id",
                "harness_kind",
                "backend_kind",
                "session_ref",
                "endpoint",
                "runtime_generation",
                "status",
                "callsign",
                "routing_name",
                "display_agent",
                "worktree",
                "terminal_id",
                "state_change_seq",
                "snapshot_row_digest",
                "capabilities",
            }
            if (
                not isinstance(runtime_receipt, Mapping)
                or set(runtime_receipt) != receipt_keys
                or runtime_receipt.get("schema")
                != "league.rollover-descendant-runtime.v1"
                or runtime_receipt.get("verified") is not True
                or runtime_receipt.get("champion_agent_id") != champion_agent_id
                or runtime_receipt.get("task_id") != task_id
                or runtime_receipt.get("runtime_instance_id") != runtime_instance_id
                or runtime_receipt.get("harness_kind") != "codex-thread"
                or runtime_receipt.get("backend_kind") != "herdr"
                or runtime_receipt.get("session_ref") != champion["thread_id"]
                or runtime_receipt.get("endpoint") != champion["address"]
                or runtime_receipt.get("status") not in {"active", "idle"}
                or runtime_receipt.get("callsign") != champion["callsign"]
                or runtime_receipt.get("routing_name") != champion["routing_name"]
                or runtime_receipt.get("display_agent") != champion["display_agent"]
                or runtime_receipt.get("worktree") != champion["worktree"]
                or runtime_receipt.get("snapshot_row_digest") != snapshot_row_digest
                or not isinstance(runtime_receipt.get("terminal_id"), str)
                or not runtime_receipt["terminal_id"]
                or type(runtime_receipt.get("state_change_seq")) is not int
                or runtime_receipt["state_change_seq"] < 0
                or not isinstance(runtime_receipt.get("runtime_generation"), str)
                or not runtime_receipt["runtime_generation"]
                or not isinstance(runtime_receipt.get("capabilities"), list)
            ):
                raise StorageRefusal(
                    "descendant_runtime_mismatch",
                    "live adapter receipt does not match the exact frozen Champion identity",
                )
            callsign_rows = store.connection.execute(
                """
                SELECT * FROM callsign_assignments
                 WHERE agent_id=? AND callsign=? AND role='champion'
                   AND scope_kind='task' AND scope_id=? AND state='active'
                 ORDER BY callsign_assignment_id
                """,
                (champion_agent_id, snapshot_row["callsign"], task_id),
            ).fetchall()
            if len(callsign_rows) != 1 or callsign_rows[0]["runtime_instance_id"] not in {
                None,
                runtime_instance_id,
            }:
                raise StorageRefusal(
                    "descendant_callsign_ambiguous",
                    "descendant reconciliation requires one exact active callsign binding",
                )
            callsign_assignment = callsign_rows[0]
            if int(callsign_assignment["version"]) != expected_callsign_assignment_version:
                raise StorageRefusal(
                    "version_conflict", "descendant callsign assignment version changed"
                )
            required_capabilities = json.loads(callsign_assignment["requirements_json"])
            if runtime_receipt["capabilities"] != required_capabilities:
                raise StorageRefusal(
                    "descendant_runtime_mismatch",
                    "live runtime capabilities differ from the canonical callsign contract",
                )
            created_runtime = not runtimes
            if runtimes:
                runtime = runtimes[0]
                if runtime["status"] in {"closed", "failed"}:
                    raise StorageRefusal(
                        "descendant_runtime_closed",
                        "closed or failed imported runtime cannot be rebound",
                    )
                if (
                    runtime["runtime_instance_id"] != runtime_instance_id
                    or not bool(runtime["verified"])
                    or runtime["harness_kind"] != runtime_receipt["harness_kind"]
                    or runtime["backend_kind"] != runtime_receipt["backend_kind"]
                    or runtime["session_ref"] != runtime_receipt["session_ref"]
                    or runtime["endpoint"] != runtime_receipt["endpoint"]
                    or runtime["runtime_generation"] != runtime_receipt["runtime_generation"]
                    or json.loads(runtime["capabilities_json"]) != required_capabilities
                ):
                    raise StorageRefusal(
                        "descendant_runtime_mismatch",
                        "canonical runtime differs from exact live adapter evidence",
                    )
            else:
                store.connection.execute(
                    """
                    INSERT INTO runtime_instances
                      (runtime_instance_id,actor_agent_id,harness_kind,backend_kind,session_ref,
                       endpoint,runtime_generation,status,verified,last_seen_at,capabilities_json)
                    VALUES(?,?,?,?,?,?,?,?,1,?,?)
                    """,
                    (
                        runtime_instance_id,
                        champion_agent_id,
                        runtime_receipt["harness_kind"],
                        runtime_receipt["backend_kind"],
                        runtime_receipt["session_ref"],
                        runtime_receipt["endpoint"],
                        runtime_receipt["runtime_generation"],
                        runtime_receipt["status"],
                        at,
                        stable_json(required_capabilities),
                    ),
                )
            assignment = store.connection.execute(
                "SELECT * FROM task_assignments WHERE task_id=?", (task_id,)
            ).fetchone()
            created_assignment = assignment is None
            if (
                (created_assignment and expected_assignment_version != 0)
                or (
                    assignment is not None
                    and int(assignment["version"]) != expected_assignment_version
                )
            ):
                raise StorageRefusal(
                    "version_conflict", "descendant task assignment version changed"
                )
            if assignment is not None and (
                assignment["champion_agent_id"] != champion_agent_id
                or assignment["callsign"] != snapshot_row["callsign"]
                or assignment["assignment_role"] != "champion"
                or assignment["state"] != "active"
                or assignment["coordinator_agent_id"] != operation["predecessor_agent_id"]
                or assignment["runtime_instance_id"] not in {None, runtime_instance_id}
            ):
                raise StorageRefusal(
                    "descendant_assignment_conflict",
                    "existing descendant task assignment is not exactly reconcilable",
                )
            inflight = store.connection.execute(
                """
                SELECT o.outbox_id
                  FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
                 WHERE o.recipient_agent_id=? AND o.state IN ('in_flight','awaiting_receipt')
                   AND (e.agent_id=? OR e.task_id=?)
                 ORDER BY o.outbox_id
                """,
                (operation["predecessor_agent_id"], champion_agent_id, task_id),
            ).fetchall()
            if inflight:
                raise StorageRefusal(
                    "descendant_delivery_inflight",
                    "claimed descendant delivery cannot be retargeted",
                )
            eligible = tuple(
                row["outbox_id"]
                for row in store.connection.execute(
                    """
                    SELECT o.outbox_id
                      FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
                     WHERE o.recipient_agent_id=? AND o.state='pending'
                       AND (e.agent_id=? OR e.task_id=?)
                     ORDER BY o.outbox_id
                    """,
                    (operation["predecessor_agent_id"], champion_agent_id, task_id),
                )
            )
            if declared_outboxes != eligible:
                raise StorageRefusal(
                    "descendant_delivery_set_stale",
                    "declared pending descendant deliveries are missing, broad, or stale",
                )

            assignment_id = (
                f"assignment:rollover:{digest({'operation_id': operation_id, 'champion_agent_id': champion_agent_id, 'task_id': task_id})[:32]}"
                if created_assignment
                else str(assignment["task_assignment_id"])
            )
            receipt = {
                "schema": "league.rollover-descendant-reconciliation.v1",
                "operation_id": operation_id,
                "reconciliation_id": reconciliation_id,
                "snapshot_id": operation["snapshot_id"],
                "snapshot_digest": snapshot_digest,
                "snapshot_row_digest": snapshot_row_digest,
                "squad_id": operation["squad_id"],
                "predecessor_agent_id": operation["predecessor_agent_id"],
                "successor_agent_id": operation["successor_agent_id"],
                "champion_agent_id": champion_agent_id,
                "task_id": task_id,
                "runtime_instance_id": runtime_instance_id,
                "runtime_generation": runtime_receipt["runtime_generation"],
                "runtime_receipt_digest": digest(dict(runtime_receipt)),
                "created_runtime": created_runtime,
                "callsign_assignment_id": callsign_assignment["callsign_assignment_id"],
                "task_assignment_id": assignment_id,
                "created_assignment": created_assignment,
                "expected_rollover_version": expected_rollover_version,
                "expected_agent_version": expected_agent_version,
                "expected_task_version": expected_task_version,
                "expected_assignment_version": expected_assignment_version,
                "expected_callsign_assignment_version": expected_callsign_assignment_version,
                "task_version": expected_task_version + 1,
                "retargeted_outbox_ids": list(declared_outboxes),
                "pending_delivery_count": 0,
                "reason": "committed_rollover_descendant_binding",
                "result": "reconciled",
                "at": at,
            }
            if created_assignment:
                store.connection.execute(
                    """
                    INSERT INTO task_assignments
                      (task_assignment_id,task_id,request_id,coordinator_agent_id,
                       champion_agent_id,runtime_instance_id,callsign,assignment_role,state,
                       acceptance_receipt_json,cleanup_required,version,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,'champion','active',?,0,1,?,?)
                    """,
                    (
                        assignment_id,
                        task_id,
                        task["request_id"],
                        operation["successor_agent_id"],
                        champion_agent_id,
                        runtime_instance_id,
                        snapshot_row["callsign"],
                        stable_json(receipt),
                        at,
                        at,
                    ),
                )
            else:
                changed_assignment = store.connection.execute(
                    """
                    UPDATE task_assignments
                       SET coordinator_agent_id=?,runtime_instance_id=?,version=version+1,updated_at=?
                     WHERE task_assignment_id=? AND coordinator_agent_id=? AND version=?
                    """,
                    (
                        operation["successor_agent_id"],
                        runtime_instance_id,
                        at,
                        assignment_id,
                        operation["predecessor_agent_id"],
                        expected_assignment_version,
                    ),
                )
                if changed_assignment.rowcount != 1:
                    raise StorageRefusal(
                        "descendant_assignment_conflict",
                        "descendant assignment coordinator changed during reconciliation",
                    )
            changed_task = store.connection.execute(
                """
                UPDATE tasks SET coordinator_agent_id=?,version=version+1,updated_at=?
                 WHERE task_id=? AND coordinator_agent_id=? AND version=?
                """,
                (
                    operation["successor_agent_id"],
                    at,
                    task_id,
                    operation["predecessor_agent_id"],
                    expected_task_version,
                ),
            )
            if changed_task.rowcount != 1:
                raise StorageRefusal(
                    "descendant_task_stale",
                    "descendant task coordinator changed during reconciliation",
                )
            changed_callsign = store.connection.execute(
                """
                UPDATE callsign_assignments
                   SET runtime_instance_id=?,version=version+1
                 WHERE callsign_assignment_id=? AND version=?
                """,
                (
                    runtime_instance_id,
                    callsign_assignment["callsign_assignment_id"],
                    expected_callsign_assignment_version,
                ),
            )
            if changed_callsign.rowcount != 1:
                raise StorageRefusal(
                    "version_conflict", "descendant callsign assignment CAS failed"
                )
            changed_agent = store.connection.execute(
                """
                UPDATE agent_instances
                   SET shotcaller_agent_id=?,version=version+1,updated_at=?
                 WHERE agent_id=? AND version=?
                """,
                (
                    operation["successor_agent_id"],
                    at,
                    champion_agent_id,
                    expected_agent_version,
                ),
            )
            if changed_agent.rowcount != 1:
                raise StorageRefusal(
                    "descendant_identity_stale", "descendant agent CAS failed"
                )
            for outbox_id in declared_outboxes:
                changed_outbox = store.connection.execute(
                    """
                    UPDATE delivery_outbox SET recipient_agent_id=?
                     WHERE outbox_id=? AND recipient_agent_id=? AND state='pending'
                    """,
                    (
                        operation["successor_agent_id"],
                        outbox_id,
                        operation["predecessor_agent_id"],
                    ),
                )
                if changed_outbox.rowcount != 1:
                    raise StorageRefusal(
                        "descendant_delivery_set_stale",
                        "descendant pending delivery changed during reconciliation",
                    )
            pending_delivery_count = int(
                store.connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM delivery_outbox o JOIN events e ON e.event_id=o.event_id
                     WHERE o.recipient_agent_id=? AND o.state='pending'
                       AND (e.agent_id=? OR e.task_id=?)
                    """,
                    (operation["successor_agent_id"], champion_agent_id, task_id),
                ).fetchone()[0]
            )
            receipt["pending_delivery_count"] = pending_delivery_count
            receipt_digest = digest(receipt)
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
                   update_text,occurred_at,detail_json,request_id,aggregate_kind,aggregate_id,
                   source_event_id)
                VALUES(?,NULL,?,NULL,?,'rollover_descendant_reconciled','reconciled',
                       'Champion rebound to committed Squad successor',?,?,?,'task',?,?)
                """,
                (
                    reconciliation_id,
                    task_id,
                    expected_task_version + 1,
                    at,
                    stable_json({"receipt": receipt, "receipt_digest": receipt_digest}),
                    task["request_id"],
                    task_id,
                    operation["owner_event_id"],
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "descendant reconciliation conflicted with canonical state"
        ) from exc
    return {
        "operation_id": operation_id,
        "reconciliation_id": reconciliation_id,
        "champion_agent_id": champion_agent_id,
        "task_id": task_id,
        "runtime_instance_id": runtime_instance_id,
        "successor_agent_id": operation["successor_agent_id"],
        "created_assignment": created_assignment,
        "created_runtime": created_runtime,
        "task_version": expected_task_version + 1,
        "retargeted_outbox_ids": list(declared_outboxes),
        "pending_delivery_count": pending_delivery_count,
        "receipt_digest": receipt_digest,
        "idempotent": False,
    }


def _receipt(receipt: Mapping[str, Any], keys: set[str], schema: str) -> tuple[dict[str, Any], str]:
    if set(receipt) != keys or receipt.get("schema") != schema or receipt.get("verified") is not True:
        raise StorageRefusal("receipt_unverified", "cleanup receipt is not exact and verified")
    for key in keys - {"verified"}:
        if not isinstance(receipt.get(key), str) or not receipt[key]:
            raise StorageRefusal("receipt_unverified", "cleanup receipt identity is incomplete")
    value = dict(receipt)
    return value, digest(value)


def _verified_closed_runtime_cleanup(
    store: Any,
    agent_id: str,
    runtime_instance_id: str,
    *,
    expected_runtime_instance_id: Optional[str] = None,
) -> None:
    rows = store.connection.execute(
        """
        SELECT r.runtime_instance_id,r.status,r.verified,r.session_ref,a.thread_id
          FROM runtime_instances r JOIN agent_instances a ON a.agent_id=r.actor_agent_id
         WHERE r.actor_agent_id=? ORDER BY r.runtime_instance_id
        """,
        (agent_id,),
    ).fetchall()
    if runtime_instance_id == "not-created":
        if rows:
            raise StorageRefusal(
                "cleanup_incomplete", "successor runtime exists despite no-runtime receipt"
            )
        return
    matches = [row for row in rows if row["runtime_instance_id"] == runtime_instance_id]
    if (
        len(matches) != 1
        or matches[0]["status"] not in {"closed", "failed"}
        or not matches[0]["verified"]
        or matches[0]["session_ref"] != matches[0]["thread_id"]
        or any(row["status"] in {"active", "idle"} for row in rows)
        or (
            expected_runtime_instance_id is not None
            and runtime_instance_id != expected_runtime_instance_id
        )
    ):
        raise StorageRefusal(
            "cleanup_incomplete", "successor runtime cleanup identity is not exact"
        )


def abort_rollover(
    store: Any,
    operation_id: str,
    expected_version: int,
    cleanup_receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    timestamp(at, "rollover abort time")
    receipt, receipt_digest = _receipt(
        cleanup_receipt, ABORT_RECEIPT_KEYS, "league.rollover-abort-receipt.v1"
    )
    try:
        with store._transaction():
            operation = _operation(store, operation_id)
            if operation["state"] == "aborted":
                if operation["cleanup_receipt_digest"] != receipt_digest:
                    raise StorageRefusal("receipt_conflict", "abort retry changed cleanup receipt")
                result = rollover_status(store, operation_id)
                assert result is not None
                result["idempotent"] = True
                return result
            if operation["state"] not in {"prepared", "acknowledged"} or int(operation["version"]) != expected_version:
                raise StorageRefusal("rollover_conflict", "rollover cannot abort after owner switch")
            if (
                receipt["operation_id"] != operation_id
                or receipt["successor_agent_id"] != operation["successor_agent_id"]
            ):
                raise StorageRefusal("receipt_mismatch", "abort receipt changed successor identity")
            assignment = store.connection.execute(
                "SELECT * FROM callsign_assignments WHERE callsign_assignment_id=?",
                (operation["callsign_assignment_id"],),
            ).fetchone()
            if assignment["state"] == "reserved":
                _verified_closed_runtime_cleanup(
                    store,
                    operation["successor_agent_id"],
                    receipt["runtime_instance_id"],
                )
                _rollback_reserved_in_transaction(
                    store, assignment, int(assignment["version"]), receipt_digest, at
                )
            elif assignment["state"] == "active":
                _verified_closed_runtime_cleanup(
                    store,
                    operation["successor_agent_id"],
                    receipt["runtime_instance_id"],
                    expected_runtime_instance_id=operation[
                        "successor_runtime_instance_id"
                    ],
                )
                _release_active_in_transaction(
                    store, assignment, int(assignment["version"]), receipt_digest, at
                )
            elif assignment["state"] not in {"rolled_back", "released"}:
                raise StorageRefusal("cleanup_incomplete", "successor callsign is not safely releasable")
            store.connection.execute(
                """
                UPDATE shotcaller_intake SET state='closed',version=version+1,updated_at=?
                 WHERE agent_id=? AND squad_id=?
                """,
                (at, operation["successor_agent_id"], operation["squad_id"]),
            )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE rollover_operations SET state='aborted',cleanup_receipt_digest=?,
                       version=?,updated_at=? WHERE operation_id=?
                """,
                (receipt_digest, next_version, at, operation_id),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,NULL,NULL,?,?,'rollover_aborted','aborted','rollover aborted',?,?,
                       'squad',?)
                """,
                (
                    f"rollover:{operation_id}:aborted",
                    operation["squad_id"],
                    operation["expected_owner_version"],
                    at,
                    stable_json(
                        {"operation_id": operation_id, "cleanup_receipt_digest": receipt_digest}
                    ),
                    operation["squad_id"],
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "rollover abort conflicted") from exc
    result = rollover_status(store, operation_id)
    assert result is not None
    result["idempotent"] = False
    return result


def _intake_reconciliation_plan(
    plan: Mapping[str, Any], operation_id: str
) -> tuple[dict[str, Any], str]:
    required = {
        "schema",
        "operation_id",
        "predecessor_agent_id",
        "successor_agent_id",
        "successor_runtime_instance_id",
        "requests",
        "prompts",
        "obligations",
    }
    if set(plan) != required or plan.get("schema") != "league.rollover-intake-reconciliation.v1":
        raise StorageRefusal(
            "invalid_intake_reconciliation", "intake reconciliation plan shape is invalid"
        )
    if plan.get("operation_id") != operation_id:
        raise StorageRefusal(
            "invalid_intake_reconciliation", "intake reconciliation operation changed"
        )
    for key in (
        "predecessor_agent_id",
        "successor_agent_id",
        "successor_runtime_instance_id",
    ):
        if not isinstance(plan.get(key), str) or not plan[key]:
            raise StorageRefusal(
                "invalid_intake_reconciliation", "intake reconciliation identity is incomplete"
            )
    shapes = {
        "requests": {"request_id", "version"},
        "prompts": {"prompt_id", "runtime_instance_id", "body_hash", "byte_count"},
        "obligations": {"obligation_id", "updated_at"},
    }
    identity_keys = {
        "requests": "request_id",
        "prompts": "prompt_id",
        "obligations": "obligation_id",
    }
    for name, shape in shapes.items():
        rows = plan.get(name)
        identity_key = identity_keys[name]
        if (
            not isinstance(rows, list)
            or len(rows) > 500
            or any(not isinstance(row, dict) or set(row) != shape for row in rows)
            or any(not isinstance(row.get(identity_key), str) or not row[identity_key] for row in rows)
            or [row[identity_key] for row in rows]
            != sorted({row[identity_key] for row in rows})
        ):
            raise StorageRefusal(
                "invalid_intake_reconciliation",
                f"{name} must be bounded, ordered, duplicate-free exact records",
            )
    for row in plan["requests"]:
        if type(row["version"]) is not int or row["version"] < 1:
            raise StorageRefusal(
                "invalid_intake_reconciliation", "request version is invalid"
            )
    for row in plan["prompts"]:
        if (
            not isinstance(row["runtime_instance_id"], str)
            or not row["runtime_instance_id"]
            or not isinstance(row["body_hash"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["body_hash"])
            or type(row["byte_count"]) is not int
            or row["byte_count"] < 1
        ):
            raise StorageRefusal(
                "invalid_intake_reconciliation", "prompt receipt identity is invalid"
            )
    for row in plan["obligations"]:
        timestamp(row["updated_at"], "obligation reconciliation update time")
    normalized = dict(plan)
    encoded = stable_json(normalized).encode("utf-8")
    if len(encoded) > 262_144:
        raise StorageRefusal(
            "invalid_intake_reconciliation", "intake reconciliation plan is too large"
        )
    return normalized, digest(normalized)


def _canonical_intake_page(
    store: Any, predecessor_agent_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Return one deterministic exact page without pretending a larger drain is complete."""

    request_rows = store.connection.execute(
        """
        SELECT request_id,version FROM requests
         WHERE owner_agent_id=? AND state NOT IN ('answered','cancelled')
         ORDER BY request_id LIMIT 501
        """,
        (predecessor_agent_id,),
    ).fetchall()
    prompt_rows = store.connection.execute(
        """
        SELECT p.prompt_id,p.runtime_instance_id,pp.body_hash,pp.byte_count
          FROM prompts p JOIN prompt_payloads pp ON pp.prompt_id=p.prompt_id
         WHERE p.current_owner_agent_id=? AND p.triage_state='untriaged'
         ORDER BY p.prompt_id LIMIT 501
        """,
        (predecessor_agent_id,),
    ).fetchall()
    obligation_rows = store.connection.execute(
        """
        SELECT obligation_id,updated_at FROM obligations
         WHERE owner_agent_id=? AND state='open' ORDER BY obligation_id LIMIT 501
        """,
        (predecessor_agent_id,),
    ).fetchall()
    has_more = any(len(rows) > 500 for rows in (request_rows, prompt_rows, obligation_rows))
    requests = [
        {"request_id": row["request_id"], "version": int(row["version"])}
        for row in request_rows[:500]
    ]
    prompts = [
        {
            "prompt_id": row["prompt_id"],
            "runtime_instance_id": row["runtime_instance_id"],
            "body_hash": row["body_hash"],
            "byte_count": int(row["byte_count"]),
        }
        for row in prompt_rows[:500]
    ]
    obligations = [
        {"obligation_id": row["obligation_id"], "updated_at": row["updated_at"]}
        for row in obligation_rows[:500]
    ]
    return requests, prompts, obligations, has_more


def rollover_intake_plan(
    store: Any,
    operation_id: str,
    snapshot_digest: str,
    expected_rollover_version: int,
) -> dict[str, Any]:
    """Build the next exact bounded predecessor-intake reconciliation page."""

    operation = _operation(store, operation_id)
    if (
        operation["state"] != "switched"
        or int(operation["version"]) != expected_rollover_version
    ):
        raise StorageRefusal(
            "intake_reconciliation_stale",
            "rollover is not at the exact committed reconciliation boundary",
        )
    snapshot = store.connection.execute(
        "SELECT digest FROM active_champion_snapshots WHERE snapshot_id=?",
        (operation["snapshot_id"],),
    ).fetchone()
    if snapshot is None or snapshot["digest"] != snapshot_digest:
        raise StorageRefusal(
            "intake_reconciliation_stale", "rollover snapshot digest changed"
        )
    _runtime_identity(
        store,
        operation["successor_agent_id"],
        operation["successor_runtime_instance_id"],
    )
    requests, prompts, obligations, has_more = _canonical_intake_page(
        store, operation["predecessor_agent_id"]
    )
    plan = {
        "schema": "league.rollover-intake-reconciliation.v1",
        "operation_id": operation_id,
        "predecessor_agent_id": operation["predecessor_agent_id"],
        "successor_agent_id": operation["successor_agent_id"],
        "successor_runtime_instance_id": operation["successor_runtime_instance_id"],
        "requests": requests,
        "prompts": prompts,
        "obligations": obligations,
    }
    normalized, plan_digest = _intake_reconciliation_plan(plan, operation_id)
    return {
        "plan": normalized,
        "plan_digest": plan_digest,
        "has_more": has_more,
        "counts": {
            "requests": len(requests),
            "prompts": len(prompts),
            "obligations": len(obligations),
        },
    }


def reconcile_rollover_intake(
    store: Any,
    operation_id: str,
    reconciliation_id: str,
    snapshot_digest: str,
    expected_rollover_version: int,
    plan: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    """Rebind the exact predecessor-owned unresolved intake to its successor."""

    timestamp(at, "intake reconciliation time")
    if (
        not all((operation_id, reconciliation_id, snapshot_digest))
        or len(reconciliation_id) > 160
        or expected_rollover_version < 1
    ):
        raise StorageRefusal(
            "invalid_intake_reconciliation", "intake reconciliation identity is invalid"
        )
    normalized, plan_digest = _intake_reconciliation_plan(plan, operation_id)
    event_id = "rollover-intake:" + reconciliation_id
    try:
        with store._transaction():
            retry = store.connection.execute(
                "SELECT detail_json FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if retry is not None:
                detail = json.loads(retry["detail_json"])
                if detail.get("plan_digest") != plan_digest:
                    raise StorageRefusal(
                        "intake_reconciliation_conflict",
                        "intake reconciliation retry changed its exact plan",
                    )
                return {**detail["result"], "idempotent": True}
            operation = _operation(store, operation_id)
            if (
                operation["state"] != "switched"
                or int(operation["version"]) != expected_rollover_version
            ):
                raise StorageRefusal(
                    "intake_reconciliation_stale",
                    "rollover is not at the exact committed reconciliation boundary",
                )
            snapshot = store.connection.execute(
                "SELECT digest FROM active_champion_snapshots WHERE snapshot_id=?",
                (operation["snapshot_id"],),
            ).fetchone()
            if snapshot is None or snapshot["digest"] != snapshot_digest:
                raise StorageRefusal(
                    "intake_reconciliation_stale", "rollover snapshot digest changed"
                )
            exact_identity = (
                normalized["predecessor_agent_id"] == operation["predecessor_agent_id"]
                and normalized["successor_agent_id"] == operation["successor_agent_id"]
                and normalized["successor_runtime_instance_id"]
                == operation["successor_runtime_instance_id"]
            )
            if not exact_identity:
                raise StorageRefusal(
                    "intake_reconciliation_stale", "rollover owner identity changed"
                )
            _runtime_identity(
                store,
                operation["successor_agent_id"],
                operation["successor_runtime_instance_id"],
            )
            (
                canonical_requests,
                canonical_prompts,
                canonical_obligations,
                has_more,
            ) = _canonical_intake_page(store, operation["predecessor_agent_id"])
            if (
                canonical_requests != normalized["requests"]
                or canonical_prompts != normalized["prompts"]
                or canonical_obligations != normalized["obligations"]
            ):
                raise StorageRefusal(
                    "intake_reconciliation_stale",
                    "declared predecessor intake differs from canonical unresolved state",
                )
            for row in canonical_requests:
                changed = store.connection.execute(
                    """
                    UPDATE requests SET owner_agent_id=?,
                           return_to_agent_id=CASE WHEN return_to_agent_id=? THEN ? ELSE return_to_agent_id END,
                           pending_owner_agent_id=CASE WHEN pending_owner_agent_id=? THEN ? ELSE pending_owner_agent_id END,
                           version=version+1,updated_at=?
                     WHERE request_id=? AND owner_agent_id=? AND version=?
                    """,
                    (
                        operation["successor_agent_id"],
                        operation["predecessor_agent_id"],
                        operation["successor_agent_id"],
                        operation["predecessor_agent_id"],
                        operation["successor_agent_id"],
                        at,
                        row["request_id"],
                        operation["predecessor_agent_id"],
                        row["version"],
                    ),
                )
                if changed.rowcount != 1:
                    raise StorageRefusal(
                        "intake_reconciliation_stale", "request ownership changed during reconciliation"
                    )
            for row in canonical_prompts:
                changed = store.connection.execute(
                    """
                    UPDATE prompts SET current_owner_agent_id=?,current_owner_runtime_instance_id=?
                     WHERE prompt_id=? AND current_owner_agent_id=? AND runtime_instance_id=?
                       AND triage_state='untriaged'
                    """,
                    (
                        operation["successor_agent_id"],
                        operation["successor_runtime_instance_id"],
                        row["prompt_id"],
                        operation["predecessor_agent_id"],
                        row["runtime_instance_id"],
                    ),
                )
                if changed.rowcount != 1:
                    raise StorageRefusal(
                        "intake_reconciliation_stale", "prompt ownership changed during reconciliation"
                    )
            for row in canonical_obligations:
                changed = store.connection.execute(
                    """
                    UPDATE obligations SET owner_agent_id=?,updated_at=?
                     WHERE obligation_id=? AND owner_agent_id=? AND state='open' AND updated_at=?
                    """,
                    (
                        operation["successor_agent_id"],
                        at,
                        row["obligation_id"],
                        operation["predecessor_agent_id"],
                        row["updated_at"],
                    ),
                )
                if changed.rowcount != 1:
                    raise StorageRefusal(
                        "intake_reconciliation_stale",
                        "obligation ownership changed during reconciliation",
                    )
            if canonical_requests or canonical_prompts or canonical_obligations:
                from .sqlite_watcher_ops import ensure_watcher_scope

                successor = store.connection.execute(
                    "SELECT callsign FROM agent_instances WHERE agent_id=? AND retired_at IS NULL",
                    (operation["successor_agent_id"],),
                ).fetchone()
                if successor is None:
                    raise StorageRefusal(
                        "successor_identity_mismatch", "successor callsign is not live"
                    )
                scope_id = f"watcher:{successor['callsign']}"
                ensure_watcher_scope(
                    store, scope_id, operation["successor_agent_id"], block_on_obligations=None
                )
                store.connection.execute(
                    """
                    UPDATE watcher_scopes
                       SET wait_generation=wait_generation+1,stop_blocked=0,wait_active=0
                     WHERE scope_id=? AND actor_agent_id=?
                    """,
                    (scope_id, operation["successor_agent_id"]),
                )
            result = {
                "operation_id": operation_id,
                "reconciliation_id": reconciliation_id,
                "predecessor_agent_id": operation["predecessor_agent_id"],
                "successor_agent_id": operation["successor_agent_id"],
                "successor_runtime_instance_id": operation["successor_runtime_instance_id"],
                "request_count": len(canonical_requests),
                "prompt_count": len(canonical_prompts),
                "obligation_count": len(canonical_obligations),
                "unresolved_count": len(canonical_requests) + len(canonical_prompts),
                "has_more": has_more,
                "idempotent": False,
            }
            detail = {
                "operation_id": operation_id,
                "reconciliation_id": reconciliation_id,
                "plan_digest": plan_digest,
                "snapshot_digest": snapshot_digest,
                "original_request_digest": digest(canonical_requests),
                "original_prompt_runtime_digest": digest(
                    [
                        {
                            "prompt_id": row["prompt_id"],
                            "runtime_instance_id": row["runtime_instance_id"],
                        }
                        for row in canonical_prompts
                    ]
                ),
                "original_obligation_digest": digest(canonical_obligations),
                "result": result,
            }
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,
                   update_text,occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,NULL,NULL,?,?,'rollover_intake_reconciled','reconciled',
                       'exact predecessor intake rebound to committed successor',?,?,
                       'squad',?)
                """,
                (
                    event_id,
                    operation["squad_id"],
                    expected_rollover_version,
                    at,
                    stable_json(detail),
                    operation["squad_id"],
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "rollover intake reconciliation conflicted with canonical state"
        ) from exc
    return result


def complete_rollover_drain(
    store: Any,
    operation_id: str,
    expected_version: int,
    cleanup_receipt: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    timestamp(at, "rollover drain completion time")
    receipt, receipt_digest = _receipt(
        cleanup_receipt, DRAIN_RECEIPT_KEYS, "league.rollover-drain-receipt.v1"
    )
    try:
        with store._transaction():
            operation = _operation(store, operation_id)
            if operation["state"] == "completed":
                if operation["cleanup_receipt_digest"] != receipt_digest:
                    raise StorageRefusal("receipt_conflict", "drain retry changed cleanup receipt")
                result = rollover_status(store, operation_id)
                assert result is not None
                result["idempotent"] = True
                return result
            if operation["state"] != "switched" or int(operation["version"]) != expected_version:
                raise StorageRefusal("rollover_conflict", "only a switched rollover can drain")
            exact = (
                receipt["operation_id"] == operation_id
                and receipt["predecessor_agent_id"] == operation["predecessor_agent_id"]
                and receipt["successor_agent_id"] == operation["successor_agent_id"]
                and receipt["owner_event_id"] == operation["owner_event_id"]
            )
            if not exact:
                raise StorageRefusal("receipt_mismatch", "drain receipt changed owner identity")
            squad = store.connection.execute(
                "SELECT * FROM squads WHERE squad_id=?", (operation["squad_id"],)
            ).fetchone()
            if squad["shotcaller_agent_id"] != operation["successor_agent_id"]:
                raise StorageRefusal("owner_conflict", "successor no longer owns the stable Squad")
            _runtime_identity(
                store,
                operation["successor_agent_id"],
                operation["successor_runtime_instance_id"],
            )
            unresolved = store.connection.execute(
                """
                SELECT 1 FROM requests WHERE owner_agent_id=?
                 AND state NOT IN ('answered','cancelled') LIMIT 1
                """,
                (operation["predecessor_agent_id"],),
            ).fetchone()
            untriaged_prompt = store.connection.execute(
                """
                SELECT 1 FROM prompts WHERE current_owner_agent_id=?
                 AND triage_state='untriaged' LIMIT 1
                """,
                (operation["predecessor_agent_id"],),
            ).fetchone()
            open_obligation = store.connection.execute(
                """
                SELECT 1 FROM obligations WHERE owner_agent_id=? AND state='open' LIMIT 1
                """,
                (operation["predecessor_agent_id"],),
            ).fetchone()
            pending_delivery = store.connection.execute(
                """
                SELECT 1 FROM delivery_outbox WHERE recipient_agent_id=?
                 AND state<>'delivered' AND state<>'cancelled' LIMIT 1
                """,
                (operation["predecessor_agent_id"],),
            ).fetchone()
            if any(
                row is not None
                for row in (unresolved, untriaged_prompt, open_obligation, pending_delivery)
            ):
                raise StorageRefusal("drain_incomplete", "predecessor still owns intake or delivery")
            live_runtime = store.connection.execute(
                """
                SELECT 1 FROM runtime_instances WHERE actor_agent_id=?
                 AND status IN ('active','idle') LIMIT 1
                """,
                (operation["predecessor_agent_id"],),
            ).fetchone()
            if live_runtime is not None:
                raise StorageRefusal("runtime_active", "predecessor runtime is not closed")
            assignment = store.connection.execute(
                """
                SELECT * FROM callsign_assignments
                 WHERE agent_id=? AND state IN ('active','released')
                 ORDER BY CASE state WHEN 'active' THEN 0 ELSE 1 END,
                          reserved_at DESC
                 LIMIT 1
                """,
                (operation["predecessor_agent_id"],),
            ).fetchone()
            if assignment is None:
                raise StorageRefusal(
                    "callsign_assignment_missing", "predecessor callsign assignment is unknown"
                )
            if assignment["state"] == "active":
                _release_active_in_transaction(
                    store,
                    assignment,
                    int(assignment["version"]),
                    receipt["callsign_release_receipt_digest"],
                    at,
                )
            elif (
                assignment["release_receipt_digest"]
                != receipt["callsign_release_receipt_digest"]
            ):
                raise StorageRefusal(
                    "callsign_assignment_missing",
                    "predecessor callsign release belongs to another cleanup",
                )
            store.connection.execute(
                """
                UPDATE shotcaller_intake SET state='closed',version=version+1,updated_at=?
                 WHERE agent_id=? AND squad_id=? AND state='draining'
                """,
                (at, operation["predecessor_agent_id"], operation["squad_id"]),
            )
            next_version = expected_version + 1
            store.connection.execute(
                """
                UPDATE rollover_operations SET state='completed',cleanup_receipt_digest=?,
                       version=?,updated_at=? WHERE operation_id=?
                """,
                (receipt_digest, next_version, at, operation_id),
            )
            store.connection.execute(
                """
                INSERT INTO events
                  (event_id,agent_id,task_id,squad_id,entity_version,event_type,status,update_text,
                   occurred_at,detail_json,aggregate_kind,aggregate_id)
                VALUES(?,NULL,NULL,?,?,'rollover_drained','completed',
                       'predecessor drain completed',?,?,'squad',?)
                """,
                (
                    f"rollover:{operation_id}:drained",
                    operation["squad_id"],
                    squad["version"],
                    at,
                    stable_json(
                        {"operation_id": operation_id, "cleanup_receipt_digest": receipt_digest}
                    ),
                    operation["squad_id"],
                ),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "rollover drain completion conflicted with canonical state"
        ) from exc
    result = rollover_status(store, operation_id)
    assert result is not None
    result["idempotent"] = False
    return result
