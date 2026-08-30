"""Durable scoped autonomous-delivery authorization and action operations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .sqlite_project_ops import canonical_repository
from .storage_mode import (
    PROTECTED_GATE_ACTIONS,
    BeginProtectedGateCommand,
    SettleModeActionCommand,
    SettleProtectedGateCommand,
    protected_gate_scope_digest,
)
from .storage_types import StorageRefusal


GRANT_SCHEMA = "league.autonomous-grant.v1"
STATUS_SCHEMA = "league.mode-status.v1"
GOAL_STATES = {
    "awaiting_authority",
    "implementing",
    "ready_to_land",
    "landing",
    "deploying",
    "verifying",
    "repair_pending",
    "delivered",
    "cleanup_pending",
    "cleaned",
}
LIMIT_KEYS = {
    "max_attempts",
    "max_concurrency",
    "max_cost_microunits",
    "max_changed_files",
    "max_duration_seconds",
    "max_repair_attempts",
}
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise StorageRefusal("mode_time_invalid", f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StorageRefusal("mode_time_invalid", f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("mode_time_invalid", f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _text(value: Any, label: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > maximum
        or any(character in value for character in "\x00\r")
    ):
        raise StorageRefusal("mode_grant_invalid", f"{label} is invalid")
    return value


def _token(value: Any, label: str) -> str:
    text = _text(value, label, maximum=256)
    if not SAFE_TOKEN.fullmatch(text):
        raise StorageRefusal("mode_grant_invalid", f"{label} is invalid")
    return text


def _tokens(value: Any, label: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise StorageRefusal("mode_grant_invalid", f"{label} must be an array")
    result = [_token(item, label) for item in value]
    if (required and not result) or len(result) != len(set(result)) or len(result) > 128:
        raise StorageRefusal("mode_grant_invalid", f"{label} is empty, duplicated, or too large")
    return sorted(result)


def _normalize_limits(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != LIMIT_KEYS:
        raise StorageRefusal(
            "mode_grant_invalid", "grant must set every autonomous-delivery limit"
        )
    result: dict[str, int] = {}
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise StorageRefusal("mode_grant_invalid", f"{key} must be a positive integer")
        result[key] = raw
    return dict(sorted(result.items()))


def _normalize_grant(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "grant_id",
        "goal_id",
        "issuer",
        "shotcaller_agent_id",
        "exact_goal",
        "scope",
        "allowed_actions",
        "exclusions",
        "sensitive_inclusions",
        "resource_boundary",
        "starts_at",
        "expires_at",
        "limits",
        "revision",
    }
    if set(value) != required or value.get("schema") != GRANT_SCHEMA:
        raise StorageRefusal("mode_grant_invalid", "grant fields or schema are invalid")
    issuer = value["issuer"]
    if not isinstance(issuer, dict) or set(issuer) != {"kind", "id"} or issuer.get("kind") != "summoner":
        raise StorageRefusal("mode_grant_invalid", "grant issuer must identify the Summoner")
    scope = value["scope"]
    scope_keys = {"project_ids", "repositories", "environments", "deployment_targets"}
    if not isinstance(scope, dict) or set(scope) != scope_keys:
        raise StorageRefusal("mode_grant_invalid", "grant scope is invalid")
    repositories = _tokens(scope["repositories"], "repository scope")
    normalized_repositories = sorted(canonical_repository(item)[0] for item in repositories)
    project_ids = _tokens(scope["project_ids"], "project scope")
    if not project_ids and not normalized_repositories:
        raise StorageRefusal("mode_grant_invalid", "grant needs a repository or project scope")
    starts = _time(str(value["starts_at"]), "grant start")
    expires_raw = value["expires_at"]
    expires = None if expires_raw is None else _time(str(expires_raw), "grant expiry")
    if expires is not None and expires <= starts:
        raise StorageRefusal("mode_grant_invalid", "grant expiry must follow its start")
    revision = value["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise StorageRefusal("mode_grant_invalid", "grant revision must be positive")
    boundary = value["resource_boundary"]
    if not isinstance(boundary, dict) or len(_json(boundary).encode("utf-8")) > 16_384:
        raise StorageRefusal("mode_grant_invalid", "resource boundary is invalid")
    allowed_actions = _tokens(value["allowed_actions"], "allowed actions", required=True)
    exclusions = _tokens(value["exclusions"], "grant exclusions")
    sensitive_inclusions = _tokens(value["sensitive_inclusions"], "sensitive inclusions")
    if not set(allowed_actions) <= SUPPORTED_ACTIONS:
        raise StorageRefusal("mode_grant_invalid", "grant contains an unsupported action")
    if set(exclusions) & set(sensitive_inclusions):
        raise StorageRefusal("mode_grant_invalid", "grant cannot include and exclude the same category")
    normalized = {
        "schema": GRANT_SCHEMA,
        "grant_id": _token(value["grant_id"], "grant id"),
        "goal_id": _token(value["goal_id"], "goal id"),
        "issuer": {"kind": "summoner", "id": _token(issuer["id"], "issuer id")},
        "shotcaller_agent_id": _token(value["shotcaller_agent_id"], "Shotcaller id"),
        "exact_goal": _text(value["exact_goal"], "exact goal"),
        "scope": {
            "project_ids": project_ids,
            "repositories": normalized_repositories,
            "environments": _tokens(scope["environments"], "environment scope"),
            "deployment_targets": _tokens(scope["deployment_targets"], "deployment target scope"),
        },
        "allowed_actions": allowed_actions,
        "exclusions": exclusions,
        "sensitive_inclusions": sensitive_inclusions,
        "resource_boundary": boundary,
        "starts_at": value["starts_at"],
        "expires_at": expires_raw,
        "limits": _normalize_limits(value["limits"]),
        "revision": revision,
    }
    normalized["canonical_digest"] = _digest(normalized)
    return normalized


def _grant_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "grant_id": row["grant_id"],
        "revision": int(row["revision"]),
        "issuer": {"kind": row["issuer_kind"], "id": row["issuer_id"]},
        "shotcaller_agent_id": row["shotcaller_agent_id"],
        "exact_goal": row["exact_goal"],
        "scope": json.loads(row["scope_json"]),
        "allowed_actions": json.loads(row["allowed_actions_json"]),
        "exclusions": json.loads(row["exclusions_json"]),
        "sensitive_inclusions": json.loads(row["sensitive_inclusions_json"]),
        "resource_boundary": json.loads(row["resource_boundary_json"]),
        "starts_at": row["starts_at"],
        "expires_at": row["expires_at"],
        "canonical_digest": row["canonical_digest"],
        "version": int(row["version"]),
    }


def _usage(store: Any, goal_id: str) -> dict[str, int]:
    row = store.connection.execute(
        """SELECT attempts_used,cost_microunits_used,changed_files_used,
                  duration_seconds_used,in_progress_actions
             FROM delivery_goals WHERE goal_id=?""",
        (goal_id,),
    ).fetchone()
    if row is None:
        return {
            "attempts": 0,
            "cost_microunits": 0,
            "changed_files": 0,
            "duration_seconds": 0,
            "concurrency": 0,
        }
    return {
        "attempts": int(row[0]),
        "cost_microunits": int(row[1]),
        "changed_files": int(row[2]),
        "duration_seconds": int(row[3]),
        "concurrency": int(row[4]),
    }


def _grant_status(store: Any, grant: sqlite3.Row, at: str) -> str:
    if store.connection.execute(
        "SELECT 1 FROM authorization_revocations WHERE grant_id=?", (grant["grant_id"],)
    ).fetchone():
        return "revoked"
    instant = _time(at, "status time")
    if instant < _time(grant["starts_at"], "grant start"):
        return "not_started"
    if grant["expires_at"] is not None and instant >= _time(grant["expires_at"], "grant expiry"):
        return "expired"
    return "active"


def mode_status(store: Any, goal_id: str, at: str) -> dict[str, Any]:
    _token(goal_id, "goal id")
    _time(at, "status time")
    goal = store.connection.execute(
        "SELECT * FROM delivery_goals WHERE goal_id=?", (goal_id,)
    ).fetchone()
    if goal is None:
        return {
            "schema": STATUS_SCHEMA,
            "mode": "manual",
            "goal_id": goal_id,
            "goal_state": "awaiting_authority",
            "grant": None,
            "limits": {},
            "usage": {},
            "next_irreversible_action": "authorize",
        }
    grant = store.connection.execute(
        "SELECT * FROM authorization_grants WHERE grant_id=?", (goal["active_grant_id"],)
    ).fetchone()
    assert grant is not None
    status = _grant_status(store, grant, at)
    record = _grant_record(grant)
    record["status"] = status
    return {
        "schema": STATUS_SCHEMA,
        "mode": "autonomous_delivery" if status == "active" else "manual",
        "goal_id": goal_id,
        "goal_state": goal["state"],
        "goal_version": int(goal["version"]),
        "grant": record,
        "limits": json.loads(grant["limits_json"]),
        "usage": _usage(store, goal_id),
        "next_irreversible_action": goal["next_irreversible_action"],
    }


def authorize_mode(
    store: Any, grant_value: Mapping[str, Any], expected_goal_version: int, at: str
) -> dict[str, Any]:
    _time(at, "authorization time")
    grant = _normalize_grant(grant_value)
    if _time(at, "authorization time") >= (
        _time(grant["expires_at"], "grant expiry")
        if grant["expires_at"] is not None
        else datetime.max.replace(tzinfo=timezone.utc)
    ):
        raise StorageRefusal("grant_expired", "an already-expired grant cannot be authorized")
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT canonical_digest,goal_id FROM authorization_grants WHERE grant_id=?",
                (grant["grant_id"],),
            ).fetchone()
            if existing is not None:
                if existing["canonical_digest"] != grant["canonical_digest"]:
                    raise StorageRefusal("grant_conflict", "grant id already names different authority")
                result = mode_status(store, grant["goal_id"], at)
                result["idempotent"] = True
                return result
            owner = store.connection.execute(
                "SELECT role,retired_at FROM agent_instances WHERE agent_id=?",
                (grant["shotcaller_agent_id"],),
            ).fetchone()
            if owner is None or owner["role"] != "shotcaller" or owner["retired_at"] is not None:
                raise StorageRefusal("grant_owner_invalid", "grant Shotcaller must be one live canonical owner")
            goal = store.connection.execute(
                "SELECT * FROM delivery_goals WHERE goal_id=?", (grant["goal_id"],)
            ).fetchone()
            if goal is None:
                if expected_goal_version != 0 or grant["revision"] != 1:
                    raise StorageRefusal("goal_version_conflict", "initial grant requires goal version zero and revision one")
            else:
                active = store.connection.execute(
                    "SELECT revision FROM authorization_grants WHERE grant_id=?",
                    (goal["active_grant_id"],),
                ).fetchone()
                if int(goal["version"]) != expected_goal_version:
                    raise StorageRefusal("goal_version_conflict", "goal changed before grant authorization")
                if active is None or grant["revision"] != int(active["revision"]) + 1:
                    raise StorageRefusal("grant_revision_conflict", "scope change requires the next immutable grant revision")
            store.connection.execute(
                """
                INSERT INTO authorization_grants
                  (grant_id,goal_id,revision,issuer_kind,issuer_id,shotcaller_agent_id,
                   exact_goal,scope_json,allowed_actions_json,exclusions_json,
                   sensitive_inclusions_json,resource_boundary_json,starts_at,expires_at,
                   limits_json,canonical_digest,version,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
                """,
                (
                    grant["grant_id"], grant["goal_id"], grant["revision"],
                    grant["issuer"]["kind"], grant["issuer"]["id"],
                    grant["shotcaller_agent_id"], grant["exact_goal"],
                    _json(grant["scope"]), _json(grant["allowed_actions"]),
                    _json(grant["exclusions"]), _json(grant["sensitive_inclusions"]),
                    _json(grant["resource_boundary"]), grant["starts_at"],
                    grant["expires_at"], _json(grant["limits"]),
                    grant["canonical_digest"], at,
                ),
            )
            active_now = _time(at, "authorization time") >= _time(grant["starts_at"], "grant start")
            if goal is None:
                store.connection.execute(
                    """
                    INSERT INTO delivery_goals
                      (goal_id,active_grant_id,state,next_irreversible_action,
                       attempts_used,cost_microunits_used,changed_files_used,
                       duration_seconds_used,in_progress_actions,version,created_at,updated_at)
                    VALUES(?,?,?, ?,0,0,0,0,0,1,?,?)
                    """,
                    (
                        grant["goal_id"], grant["grant_id"],
                        "implementing" if active_now else "awaiting_authority",
                        "transition_ready_to_land" if active_now else "wait_for_grant_start",
                        at, at,
                    ),
                )
            else:
                next_version = expected_goal_version + 1
                state = "implementing" if goal["state"] == "awaiting_authority" and active_now else goal["state"]
                updated = store.connection.execute(
                    """
                    UPDATE delivery_goals
                       SET active_grant_id=?,state=?,version=?,updated_at=?
                     WHERE goal_id=? AND version=?
                    """,
                    (grant["grant_id"], state, next_version, at, grant["goal_id"], expected_goal_version),
                )
                if updated.rowcount != 1:
                    raise StorageRefusal("goal_version_conflict", "goal changed before grant authorization")
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "autonomous grant authorization conflicted") from exc
    result = mode_status(store, grant["goal_id"], at)
    result["idempotent"] = False
    return result


ACTION_SCHEMA = "league.autonomous-action.v1"
SUPPORTED_ACTIONS = {
    "land",
    "merge",
    "release",
    "publish",
    "install",
    "deploy",
    "verify",
    "smoke",
    "repair",
    "cleanup",
    "issue_reopen",
    "live_reconcile",
    "retire",
    "shotcaller_create",
    "squad_register",
    "teardown",
}
ACTION_GOAL_STATES = {
    "land": "landing",
    "merge": "landing",
    "release": "landing",
    "publish": "landing",
    "install": "deploying",
    "deploy": "deploying",
    "verify": "verifying",
    "smoke": "verifying",
    "repair": "repair_pending",
    "cleanup": "cleanup_pending",
    "teardown": "cleanup_pending",
}
ACTION_ALLOWED_FROM = {
    "land": {"ready_to_land"},
    "merge": {"ready_to_land"},
    "release": {"ready_to_land", "landing"},
    "publish": {"ready_to_land", "landing"},
    "install": {"landing", "deploying"},
    "deploy": {"landing", "deploying"},
    "verify": {"landing", "deploying", "verifying"},
    "smoke": {"landing", "deploying", "verifying"},
    "repair": {"repair_pending"},
    "cleanup": {"delivered", "cleanup_pending"},
    "issue_reopen": GOAL_STATES - {"cleaned"},
    "live_reconcile": GOAL_STATES
    - {"awaiting_authority", "repair_pending", "cleaned"},
    "shotcaller_create": GOAL_STATES
    - {"awaiting_authority", "repair_pending", "cleaned"},
    "squad_register": GOAL_STATES
    - {"awaiting_authority", "repair_pending", "cleaned"},
    "retire": GOAL_STATES
    - {"awaiting_authority", "repair_pending", "cleaned"},
    "teardown": {"delivered", "cleanup_pending"},
}
ALWAYS_REFUSED_RISKS = {
    "ambiguous_target",
    "platform_safety_bypass",
    "provider_restriction",
    "unavailable_permission",
    "unsupported_cleanup",
}


def _normalize_action(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "action_use_id",
        "idempotency_key",
        "goal_id",
        "grant_id",
        "actor_agent_id",
        "action_kind",
        "scope",
        "risk_categories",
        "sensitive_categories",
        "resources",
        "usage",
    }
    if set(value) != required or value.get("schema") != ACTION_SCHEMA:
        raise StorageRefusal("mode_action_invalid", "action fields or schema are invalid")
    action_kind = _token(value["action_kind"], "action kind")
    if action_kind not in SUPPORTED_ACTIONS:
        raise StorageRefusal("mode_action_invalid", "action kind is unsupported")
    scope = value["scope"]
    scope_keys = {"project_id", "repository", "environment", "deployment_target"}
    if not isinstance(scope, dict) or set(scope) != scope_keys:
        raise StorageRefusal("mode_action_invalid", "action scope is invalid")
    normalized_scope: dict[str, str | None] = {}
    for key in scope_keys:
        raw = scope[key]
        normalized_scope[key] = None if raw is None else _token(raw, f"action {key}")
    if normalized_scope["repository"] is not None:
        normalized_scope["repository"] = canonical_repository(
            normalized_scope["repository"]
        )[0]
    if normalized_scope["project_id"] is None and normalized_scope["repository"] is None:
        raise StorageRefusal(
            "mode_action_invalid", "action must name its exact project or repository"
        )
    resources = value["resources"]
    if not isinstance(resources, dict) or len(_json(resources).encode("utf-8")) > 16_384:
        raise StorageRefusal("mode_action_invalid", "action resource use is invalid")
    usage = value["usage"]
    usage_keys = {"attempts", "cost_microunits", "changed_files", "duration_seconds"}
    if not isinstance(usage, dict) or set(usage) != usage_keys:
        raise StorageRefusal("mode_action_invalid", "action usage is invalid")
    normalized_usage: dict[str, int] = {}
    for key in usage_keys:
        raw = usage[key]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise StorageRefusal("mode_action_invalid", "action usage must be non-negative integers")
        normalized_usage[key] = raw
    if normalized_usage["attempts"] < 1:
        raise StorageRefusal("mode_action_invalid", "action attempt use must be positive")
    normalized = {
        "schema": ACTION_SCHEMA,
        "action_use_id": _token(value["action_use_id"], "action use id"),
        "idempotency_key": _token(value["idempotency_key"], "idempotency key"),
        "goal_id": _token(value["goal_id"], "goal id"),
        "grant_id": _token(value["grant_id"], "grant id"),
        "actor_agent_id": _token(value["actor_agent_id"], "action owner id"),
        "action_kind": action_kind,
        "scope": normalized_scope,
        "risk_categories": _tokens(value["risk_categories"], "risk categories"),
        "sensitive_categories": _tokens(value["sensitive_categories"], "sensitive categories"),
        "resources": resources,
        "usage": dict(sorted(normalized_usage.items())),
    }
    normalized["use_receipt_digest"] = _digest(normalized)
    return normalized


def _within_boundary(value: Any, boundary: Any) -> bool:
    if isinstance(value, dict) and isinstance(boundary, dict):
        return set(value) <= set(boundary) and all(
            _within_boundary(value[key], boundary[key]) for key in value
        )
    if isinstance(value, list) and isinstance(boundary, list):
        return all(item in boundary for item in value)
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isinstance(boundary, (int, float))
        and not isinstance(boundary, bool)
    ):
        return value <= boundary
    return value == boundary


def _goal_and_grant(store: Any, goal_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
    goal = store.connection.execute(
        "SELECT * FROM delivery_goals WHERE goal_id=?", (goal_id,)
    ).fetchone()
    if goal is None:
        raise StorageRefusal("authority_missing", "goal has no autonomous-delivery authority")
    grant = store.connection.execute(
        "SELECT * FROM authorization_grants WHERE grant_id=?", (goal["active_grant_id"],)
    ).fetchone()
    if grant is None:
        raise StorageRefusal("authority_missing", "goal grant is unavailable")
    return goal, grant


def _action_result(row: sqlite3.Row, goal: sqlite3.Row, *, idempotent: bool) -> dict[str, Any]:
    return {
        "schema": "league.mode-action-receipt.v1",
        "action_use_id": row["action_use_id"],
        "goal_id": row["goal_id"],
        "grant_id": row["grant_id"],
        "grant_revision": int(row["grant_revision"]),
        "action_kind": row["action_kind"],
        "external_action_owner": row["external_owner_agent_id"],
        "state": row["state"],
        "use_receipt_digest": row["use_receipt_digest"],
        "result_receipt_digest": row["result_receipt_digest"],
        "goal_version_at_use": (
            None
            if row["goal_version_at_use"] is None
            else int(row["goal_version_at_use"])
        ),
        "goal_state": goal["state"],
        "goal_version": int(goal["version"]),
        "idempotent": idempotent,
    }


def _assert_action_scope(action: Mapping[str, Any], grant: sqlite3.Row) -> None:
    scope = json.loads(grant["scope_json"])
    action_scope = action["scope"]
    checks = (
        ("project_id", "project_ids"),
        ("repository", "repositories"),
        ("environment", "environments"),
        ("deployment_target", "deployment_targets"),
    )
    for action_key, grant_key in checks:
        selected = action_scope[action_key]
        if selected is not None and selected not in scope[grant_key]:
            raise StorageRefusal("action_scope_refused", f"action {action_key} is outside the grant")
    risks = set(action["risk_categories"])
    sensitive = set(action["sensitive_categories"])
    exclusions = set(json.loads(grant["exclusions_json"]))
    inclusions = set(json.loads(grant["sensitive_inclusions_json"]))
    if risks & ALWAYS_REFUSED_RISKS:
        raise StorageRefusal("action_safety_refused", "action crosses an unconditional safety boundary")
    if risks & exclusions or sensitive & exclusions:
        raise StorageRefusal("action_excluded", "action crosses an explicit grant exclusion")
    if not sensitive <= inclusions:
        raise StorageRefusal("sensitive_scope_refused", "sensitive action category is not explicitly included")
    boundary = json.loads(grant["resource_boundary_json"])
    if not _within_boundary(action["resources"], boundary):
        raise StorageRefusal("resource_boundary_refused", "action resource use exceeds the grant boundary")
    bounded_usage = {
        key: action["usage"][key]
        for key in ("cost_microunits", "changed_files")
        if key in boundary
    }
    if not _within_boundary(bounded_usage, boundary):
        raise StorageRefusal("resource_boundary_refused", "action usage exceeds the grant resource boundary")


def _assert_limits(store: Any, goal_id: str, grant: sqlite3.Row, action: Mapping[str, Any]) -> None:
    limits = json.loads(grant["limits_json"])
    current = _usage(store, goal_id)
    proposed = action["usage"]
    values = {
        "max_attempts": current["attempts"] + proposed["attempts"],
        "max_concurrency": current["concurrency"] + 1,
        "max_cost_microunits": current["cost_microunits"] + proposed["cost_microunits"],
        "max_changed_files": current["changed_files"] + proposed["changed_files"],
        "max_duration_seconds": current["duration_seconds"] + proposed["duration_seconds"],
    }
    for key, used in values.items():
        if key in limits and used > int(limits[key]):
            raise StorageRefusal("mode_limit_exceeded", f"action exceeds {key}")


def use_mode_action(
    store: Any, action_value: Mapping[str, Any], expected_goal_version: int, at: str
) -> dict[str, Any]:
    _time(at, "action start time")
    action = _normalize_action(action_value)
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM autonomous_action_uses WHERE idempotency_key=? OR action_use_id=?",
                (action["idempotency_key"], action["action_use_id"]),
            ).fetchone()
            if existing is not None:
                if existing["use_receipt_digest"] != action["use_receipt_digest"]:
                    raise StorageRefusal("mode_action_conflict", "action retry has different exact identity")
                goal = store.connection.execute(
                    "SELECT * FROM delivery_goals WHERE goal_id=?", (existing["goal_id"],)
                ).fetchone()
                assert goal is not None
                return _action_result(existing, goal, idempotent=True)
            goal, grant = _goal_and_grant(store, action["goal_id"])
            if int(goal["version"]) != expected_goal_version:
                raise StorageRefusal("goal_version_conflict", "goal changed before action use")
            if goal["active_grant_id"] != action["grant_id"]:
                raise StorageRefusal("grant_stale", "action names a stale grant revision")
            status = _grant_status(store, grant, at)
            if status != "active":
                raise StorageRefusal(f"grant_{status}", "grant does not authorize new action use")
            owner = store.connection.execute(
                "SELECT role,retired_at FROM agent_instances WHERE agent_id=?",
                (action["actor_agent_id"],),
            ).fetchone()
            if (
                action["actor_agent_id"] != grant["shotcaller_agent_id"]
                or owner is None
                or owner["role"] != "shotcaller"
                or owner["retired_at"] is not None
            ):
                raise StorageRefusal("action_owner_refused", "only the granted live Shotcaller owns external action use")
            allowed = set(json.loads(grant["allowed_actions_json"]))
            if action["action_kind"] not in allowed:
                raise StorageRefusal("action_not_allowed", "action is absent from the exact grant")
            if goal["state"] not in ACTION_ALLOWED_FROM[action["action_kind"]]:
                raise StorageRefusal("goal_transition_refused", "goal state does not permit this external action")
            _assert_action_scope(action, grant)
            _assert_limits(store, action["goal_id"], grant, action)
            repair = None
            if action["action_kind"] == "repair":
                repair = _latest_repair(
                    store, action["goal_id"], ("pending", "blocked")
                )
                if repair is None or repair["state"] != "pending" or int(repair["attempts_used"]) >= int(repair["max_attempts"]):
                    raise StorageRefusal("repair_limit_exceeded", "no bounded repair attempt remains")
            next_state = ACTION_GOAL_STATES.get(action["action_kind"], goal["state"])
            next_version = expected_goal_version + 1
            updated = store.connection.execute(
                """
                UPDATE delivery_goals
                   SET state=?,next_irreversible_action=?,
                       attempts_used=attempts_used+?,
                       cost_microunits_used=cost_microunits_used+?,
                       changed_files_used=changed_files_used+?,
                       duration_seconds_used=duration_seconds_used+?,
                       in_progress_actions=in_progress_actions+1,
                       version=?,updated_at=?
                 WHERE goal_id=? AND version=?
                """,
                (
                    next_state,
                    f"settle:{action['action_use_id']}",
                    action["usage"]["attempts"],
                    action["usage"]["cost_microunits"],
                    action["usage"]["changed_files"],
                    action["usage"]["duration_seconds"],
                    next_version,
                    at,
                    action["goal_id"],
                    expected_goal_version,
                ),
            )
            if updated.rowcount != 1:
                raise StorageRefusal("goal_version_conflict", "goal changed before action use")
            store.connection.execute(
                """
                INSERT INTO autonomous_action_uses
                  (action_use_id,idempotency_key,goal_id,grant_id,grant_revision,
                   external_owner_agent_id,action_kind,action_scope_json,risk_categories_json,
                   sensitive_categories_json,resource_use_json,attempt_count,cost_microunits,
                   changed_files,duration_seconds,state,use_receipt_digest,started_at,
                   goal_version_at_use)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'in_progress',?,?,?)
                """,
                (
                    action["action_use_id"], action["idempotency_key"], action["goal_id"],
                    action["grant_id"], int(grant["revision"]), action["actor_agent_id"],
                    action["action_kind"], _json(action["scope"]),
                    _json(action["risk_categories"]), _json(action["sensitive_categories"]),
                    _json(action["resources"]), action["usage"]["attempts"],
                    action["usage"]["cost_microunits"], action["usage"]["changed_files"],
                    action["usage"]["duration_seconds"], action["use_receipt_digest"], at,
                    next_version,
                ),
            )
            if repair is not None:
                store.connection.execute(
                    """
                    UPDATE autonomous_repair_obligations
                       SET state='in_progress',attempts_used=attempts_used+1,
                           version=version+1,updated_at=? WHERE repair_id=?
                    """,
                    (at, repair["repair_id"]),
                )
            row = store.connection.execute(
                "SELECT * FROM autonomous_action_uses WHERE action_use_id=?",
                (action["action_use_id"],),
            ).fetchone()
            goal = store.connection.execute(
                "SELECT * FROM delivery_goals WHERE goal_id=?", (action["goal_id"],)
            ).fetchone()
            assert row is not None and goal is not None
            return _action_result(row, goal, idempotent=False)
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "autonomous action use conflicted") from exc


def _repair_record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "repair_id": row["repair_id"],
        "state": row["state"],
        "attempts_used": int(row["attempts_used"]),
        "max_attempts": int(row["max_attempts"]),
    }


def _latest_repair(
    store: Any, goal_id: str, states: Sequence[str] = ()
) -> sqlite3.Row | None:
    if states:
        placeholders = ",".join("?" for _ in states)
        return store.connection.execute(
            f"""SELECT * FROM autonomous_repair_obligations
                  WHERE goal_id=? AND state IN ({placeholders})
                  ORDER BY created_at DESC LIMIT 1""",
            (goal_id, *states),
        ).fetchone()
    return store.connection.execute(
        """SELECT * FROM autonomous_repair_obligations
              WHERE goal_id=? ORDER BY created_at DESC LIMIT 1""",
        (goal_id,),
    ).fetchone()


def settle_mode_action(store: Any, command: SettleModeActionCommand) -> dict[str, Any]:
    action_use_id = command.action_use_id
    goal_id = command.goal_id
    expected_goal_version = command.expected_goal_version
    use_receipt_digest = command.use_receipt_digest
    outcome = command.outcome
    result_receipt_digest = command.result_receipt_digest
    failure_class = command.failure_class
    at = command.at
    _token(action_use_id, "action use id")
    _token(goal_id, "goal id")
    _time(at, "action settlement time")
    if outcome not in {"succeeded", "failed"} or not re.fullmatch(r"[0-9a-f]{64}", result_receipt_digest):
        raise StorageRefusal("mode_settlement_invalid", "action outcome or result digest is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", use_receipt_digest):
        raise StorageRefusal("mode_settlement_invalid", "action use receipt digest is invalid")
    if outcome == "failed":
        failure_class = _token(failure_class, "failure class")
    elif failure_class is not None:
        raise StorageRefusal("mode_settlement_invalid", "successful settlement cannot carry failure class")
    try:
        with store._transaction():
            action = store.connection.execute(
                "SELECT * FROM autonomous_action_uses WHERE action_use_id=?", (action_use_id,)
            ).fetchone()
            if action is None:
                raise StorageRefusal("mode_action_unknown", "action use does not exist")
            if action["goal_id"] != goal_id or action["use_receipt_digest"] != use_receipt_digest:
                raise StorageRefusal("mode_action_conflict", "settlement does not match exact action use")
            goal = store.connection.execute(
                "SELECT * FROM delivery_goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
            assert goal is not None
            if action["state"] != "in_progress":
                exact = (
                    action["state"] == outcome
                    and action["result_receipt_digest"] == result_receipt_digest
                    and action["failure_class"] == failure_class
                )
                if not exact:
                    raise StorageRefusal("mode_settlement_conflict", "action is already settled differently")
                result = _action_result(action, goal, idempotent=True)
                repair = store.connection.execute(
                    "SELECT * FROM autonomous_repair_obligations WHERE failed_action_use_id=?",
                    (action_use_id,),
                ).fetchone()
                result["repair"] = _repair_record(repair)
                return result
            goal_version_at_use = action["goal_version_at_use"]
            if goal_version_at_use is None:
                raise StorageRefusal(
                    "mode_action_reconciliation_required",
                    "in-progress action predates concurrency-safe settlement binding",
                )
            if int(goal_version_at_use) != expected_goal_version:
                raise StorageRefusal(
                    "goal_version_conflict",
                    "settlement does not match the action use goal version",
                )
            current_goal_version = int(goal["version"])
            if current_goal_version < int(goal_version_at_use):
                raise StorageRefusal(
                    "goal_version_conflict", "goal predates the exact action use"
                )
            next_version = current_goal_version + 1
            repair = None
            if outcome == "failed":
                if action["action_kind"] == "repair":
                    repair = _latest_repair(store, goal_id, ("in_progress",))
                    if repair is None:
                        raise StorageRefusal("repair_state_conflict", "repair action has no in-progress obligation")
                    repair_state = (
                        "blocked"
                        if int(repair["attempts_used"]) >= int(repair["max_attempts"])
                        else "pending"
                    )
                    store.connection.execute(
                        """
                        UPDATE autonomous_repair_obligations
                           SET state=?,failure_class=?,version=version+1,updated_at=?
                         WHERE repair_id=?
                        """,
                        (repair_state, failure_class, at, repair["repair_id"]),
                    )
                else:
                    current = store.connection.execute(
                        """
                        SELECT 1 FROM autonomous_repair_obligations
                         WHERE goal_id=? AND state IN ('pending','in_progress','blocked')
                        """,
                        (goal_id,),
                    ).fetchone()
                    if current is not None:
                        raise StorageRefusal("repair_state_conflict", "goal already has an unresolved repair obligation")
                    limits = json.loads(
                        store.connection.execute(
                            "SELECT limits_json FROM authorization_grants WHERE grant_id=?",
                            (action["grant_id"],),
                        ).fetchone()[0]
                    )
                    store.connection.execute(
                        """
                        INSERT INTO autonomous_repair_obligations
                          (repair_id,goal_id,failed_action_use_id,state,attempts_used,
                           max_attempts,failure_class,version,created_at,updated_at)
                        VALUES(?,?,?,'pending',0,?,?,1,?,?)
                        """,
                        (
                            f"repair:{action_use_id}", goal_id, action_use_id,
                            int(limits.get("max_repair_attempts", 1)), failure_class, at, at,
                        ),
                    )
                next_state = "repair_pending"
                next_action = "repair"
            else:
                if action["action_kind"] in {"verify", "smoke"}:
                    next_state, next_action = "delivered", "cleanup"
                elif action["action_kind"] in {"cleanup", "teardown"}:
                    next_state, next_action = "cleaned", "none"
                elif action["action_kind"] == "repair":
                    repair = _latest_repair(store, goal_id, ("in_progress",))
                    if repair is None:
                        raise StorageRefusal("repair_state_conflict", "repair action has no in-progress obligation")
                    store.connection.execute(
                        """
                        UPDATE autonomous_repair_obligations
                           SET state='completed',version=version+1,updated_at=? WHERE repair_id=?
                        """,
                        (at, repair["repair_id"]),
                    )
                    next_state, next_action = "verifying", "verify"
                elif action["action_kind"] in {"install", "deploy"}:
                    next_state, next_action = "deploying", "transition_verifying"
                elif action["action_kind"] in {
                    "issue_reopen",
                    "live_reconcile",
                    "retire",
                    "shotcaller_create",
                    "squad_register",
                }:
                    next_state, next_action = goal["state"], goal["next_irreversible_action"]
                else:
                    next_state, next_action = "landing", "transition_deploying_or_verifying"
            store.connection.execute(
                """
                UPDATE autonomous_action_uses
                   SET state=?,result_receipt_digest=?,failure_class=?,settled_at=?
                 WHERE action_use_id=?
                """,
                (outcome, result_receipt_digest, failure_class, at, action_use_id),
            )
            updated = store.connection.execute(
                """
                UPDATE delivery_goals
                   SET state=?,next_irreversible_action=?,
                       in_progress_actions=in_progress_actions-1,
                       version=?,updated_at=?
                 WHERE goal_id=? AND version=?
                """,
                (next_state, next_action, next_version, at, goal_id, current_goal_version),
            )
            if updated.rowcount != 1:
                raise StorageRefusal("goal_version_conflict", "goal changed before action settlement")
            action = store.connection.execute(
                "SELECT * FROM autonomous_action_uses WHERE action_use_id=?", (action_use_id,)
            ).fetchone()
            goal = store.connection.execute(
                "SELECT * FROM delivery_goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
            repair = _latest_repair(store, goal_id)
            assert action is not None and goal is not None
            result = _action_result(action, goal, idempotent=False)
            result["repair"] = _repair_record(repair)
            return result
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "autonomous action settlement conflicted") from exc


def _protected_gate_receipt(
    use: sqlite3.Row,
    settlement: sqlite3.Row | None,
) -> dict[str, Any]:
    return {
        "schema": "league.protected-gate-receipt.v1",
        "action_use_id": use["action_use_id"],
        "gate_name": use["gate_name"],
        "action_kind": use["action_kind"],
        "gate_scope_digest": use["gate_scope_digest"],
        "use_receipt_digest": use["use_receipt_digest"],
        "binding_digest": use["binding_digest"],
        "outcome": None if settlement is None else settlement["outcome"],
        "result_receipt_digest": (
            None if settlement is None else settlement["result_receipt_digest"]
        ),
        "settlement_digest": (
            None if settlement is None else settlement["settlement_digest"]
        ),
    }


def begin_protected_gate(
    store: Any, command: BeginProtectedGateCommand
) -> dict[str, Any]:
    expected_kind = PROTECTED_GATE_ACTIONS.get(command.gate_name)
    if expected_kind is None:
        raise StorageRefusal(
            "protected_gate_unknown", "command is not an autonomous protected gate"
        )
    action = _normalize_action(command.action)
    if action["action_kind"] != expected_kind:
        raise StorageRefusal(
            "protected_gate_action_mismatch",
            "protected gate action category does not match the command",
        )
    scope_digest = protected_gate_scope_digest(command.gate_scope)
    declared_scope_digests = action["resources"].get(
        "protected_gate_scope_digests"
    )
    if declared_scope_digests != [scope_digest]:
        raise StorageRefusal(
            "protected_gate_scope_mismatch",
            "protected target is absent from the exact action resource scope",
        )
    binding_value = {
        "action_use_id": action["action_use_id"],
        "gate_name": command.gate_name,
        "action_kind": expected_kind,
        "gate_scope_digest": scope_digest,
        "use_receipt_digest": action["use_receipt_digest"],
    }
    binding_digest = _digest(binding_value)
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM protected_gate_uses WHERE action_use_id=?",
                (action["action_use_id"],),
            ).fetchone()
            used = use_mode_action(
                store,
                command.action,
                command.expected_goal_version,
                command.at,
            )
            if existing is None:
                if used["idempotent"]:
                    raise StorageRefusal(
                        "protected_gate_unbound_action",
                        "an existing autonomous action cannot be attached to a gate later",
                    )
                store.connection.execute(
                    """
                    INSERT INTO protected_gate_uses
                      (action_use_id,gate_name,action_kind,gate_scope_digest,
                       use_receipt_digest,binding_digest,started_at)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        action["action_use_id"],
                        command.gate_name,
                        expected_kind,
                        scope_digest,
                        action["use_receipt_digest"],
                        binding_digest,
                        command.at,
                    ),
                )
            else:
                exact = (
                    existing["gate_name"] == command.gate_name
                    and existing["action_kind"] == expected_kind
                    and existing["gate_scope_digest"] == scope_digest
                    and existing["use_receipt_digest"] == action["use_receipt_digest"]
                    and existing["binding_digest"] == binding_digest
                )
                if not exact:
                    raise StorageRefusal(
                        "protected_gate_conflict",
                        "protected gate retry changed its exact authority binding",
                    )
            gate_use = store.connection.execute(
                "SELECT * FROM protected_gate_uses WHERE action_use_id=?",
                (action["action_use_id"],),
            ).fetchone()
            settlement = store.connection.execute(
                "SELECT * FROM protected_gate_settlements WHERE action_use_id=?",
                (action["action_use_id"],),
            ).fetchone()
            assert gate_use is not None
            result = dict(used)
            result["protected_gate"] = _protected_gate_receipt(
                gate_use, settlement
            )
            return result
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "protected gate authority binding conflicted"
        ) from exc


def settle_protected_gate(
    store: Any, command: SettleProtectedGateCommand
) -> dict[str, Any]:
    expected_kind = PROTECTED_GATE_ACTIONS.get(command.gate_name)
    if expected_kind is None or not re.fullmatch(
        r"[0-9a-f]{64}", command.gate_scope_digest
    ):
        raise StorageRefusal(
            "protected_gate_invalid", "protected gate settlement identity is invalid"
        )
    try:
        with store._transaction():
            gate_use = store.connection.execute(
                "SELECT * FROM protected_gate_uses WHERE action_use_id=?",
                (command.action_use_id,),
            ).fetchone()
            if (
                gate_use is None
                or gate_use["gate_name"] != command.gate_name
                or gate_use["action_kind"] != expected_kind
                or gate_use["gate_scope_digest"] != command.gate_scope_digest
                or gate_use["use_receipt_digest"] != command.use_receipt_digest
            ):
                raise StorageRefusal(
                    "protected_gate_conflict",
                    "protected gate settlement does not match its exact use",
                )
            settled = settle_mode_action(
                store,
                SettleModeActionCommand(
                    action_use_id=command.action_use_id,
                    goal_id=store.connection.execute(
                        "SELECT goal_id FROM autonomous_action_uses WHERE action_use_id=?",
                        (command.action_use_id,),
                    ).fetchone()[0],
                    expected_goal_version=command.expected_goal_version,
                    use_receipt_digest=command.use_receipt_digest,
                    outcome=command.outcome,
                    result_receipt_digest=command.result_receipt_digest,
                    failure_class=command.failure_class,
                    at=command.at,
                ),
            )
            settlement_value = {
                "binding_digest": gate_use["binding_digest"],
                "outcome": command.outcome,
                "result_receipt_digest": command.result_receipt_digest,
                "failure_class": command.failure_class,
            }
            settlement_digest = _digest(settlement_value)
            existing = store.connection.execute(
                "SELECT * FROM protected_gate_settlements WHERE action_use_id=?",
                (command.action_use_id,),
            ).fetchone()
            if existing is None:
                store.connection.execute(
                    """
                    INSERT INTO protected_gate_settlements
                      (action_use_id,outcome,result_receipt_digest,failure_class,
                       settlement_digest,settled_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        command.action_use_id,
                        command.outcome,
                        command.result_receipt_digest,
                        command.failure_class,
                        settlement_digest,
                        command.at,
                    ),
                )
            elif (
                existing["outcome"] != command.outcome
                or existing["result_receipt_digest"]
                != command.result_receipt_digest
                or existing["failure_class"] != command.failure_class
                or existing["settlement_digest"] != settlement_digest
            ):
                raise StorageRefusal(
                    "protected_gate_settlement_conflict",
                    "protected gate was already settled differently",
                )
            settlement = store.connection.execute(
                "SELECT * FROM protected_gate_settlements WHERE action_use_id=?",
                (command.action_use_id,),
            ).fetchone()
            assert settlement is not None
            result = dict(settled)
            result["protected_gate"] = _protected_gate_receipt(
                gate_use, settlement
            )
            return result
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(
            exc, "protected gate settlement conflicted"
        ) from exc


TRANSITIONS = {
    "awaiting_authority": {"implementing"},
    "implementing": {"ready_to_land"},
    "landing": {"deploying", "verifying"},
    "deploying": {"verifying"},
    "delivered": {"cleanup_pending"},
}


def transition_mode_goal(
    store: Any, goal_id: str, expected_goal_version: int, state: str, at: str
) -> dict[str, Any]:
    _token(goal_id, "goal id")
    _time(at, "goal transition time")
    if state not in GOAL_STATES:
        raise StorageRefusal("goal_transition_refused", "goal state is invalid")
    try:
        with store._transaction():
            goal, grant = _goal_and_grant(store, goal_id)
            if int(goal["version"]) != expected_goal_version:
                raise StorageRefusal("goal_version_conflict", "goal changed before transition")
            if state not in TRANSITIONS.get(goal["state"], set()):
                raise StorageRefusal("goal_transition_refused", "non-external goal transition is not allowed")
            if _grant_status(store, grant, at) != "active":
                raise StorageRefusal("grant_inactive", "goal transition requires active authority")
            if store.connection.execute(
                "SELECT 1 FROM autonomous_action_uses WHERE goal_id=? AND state='in_progress'",
                (goal_id,),
            ).fetchone():
                raise StorageRefusal("action_in_progress", "goal cannot transition around an unsettled action")
            next_action = {
                "implementing": "transition_ready_to_land",
                "ready_to_land": "land",
                "deploying": "deploy",
                "verifying": "verify",
                "cleanup_pending": "cleanup",
            }[state]
            store.connection.execute(
                """
                UPDATE delivery_goals SET state=?,next_irreversible_action=?,version=?,updated_at=?
                 WHERE goal_id=? AND version=?
                """,
                (state, next_action, expected_goal_version + 1, at, goal_id, expected_goal_version),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "autonomous goal transition conflicted") from exc
    return mode_status(store, goal_id, at)


def revoke_mode_grant(
    store: Any,
    grant_id: str,
    revoked_by: str,
    reason: str,
    expected_goal_version: int,
    at: str,
) -> dict[str, Any]:
    _token(grant_id, "grant id")
    _token(revoked_by, "revoker id")
    reason = _text(reason, "revocation reason", maximum=1024)
    _time(at, "revocation time")
    receipt_digest = _digest(
        {"grant_id": grant_id, "revoked_by": revoked_by, "reason": reason}
    )
    try:
        with store._transaction():
            existing = store.connection.execute(
                "SELECT * FROM authorization_revocations WHERE grant_id=?", (grant_id,)
            ).fetchone()
            grant = store.connection.execute(
                "SELECT * FROM authorization_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if grant is None:
                raise StorageRefusal("grant_unknown", "grant does not exist")
            if revoked_by != grant["issuer_id"]:
                raise StorageRefusal(
                    "grant_revoker_refused",
                    "only the Summoner identity recorded by the grant may revoke it",
                )
            if existing is not None:
                if existing["receipt_digest"] != receipt_digest:
                    raise StorageRefusal("grant_revocation_conflict", "grant was revoked differently")
                result = mode_status(store, grant["goal_id"], at)
                result["revocation_receipt_digest"] = receipt_digest
                result["idempotent"] = True
                return result
            goal = store.connection.execute(
                "SELECT * FROM delivery_goals WHERE goal_id=?", (grant["goal_id"],)
            ).fetchone()
            if goal is None or goal["active_grant_id"] != grant_id:
                raise StorageRefusal("grant_stale", "only the active grant revision can be revoked")
            if int(goal["version"]) != expected_goal_version:
                raise StorageRefusal("goal_version_conflict", "goal changed before revocation")
            store.connection.execute(
                """
                INSERT INTO authorization_revocations
                  (grant_id,revoked_by,reason,revoked_at,receipt_digest) VALUES(?,?,?,?,?)
                """,
                (grant_id, revoked_by, reason, at, receipt_digest),
            )
            store.connection.execute(
                """
                UPDATE delivery_goals
                   SET next_irreversible_action='authorize',version=?,updated_at=?
                 WHERE goal_id=? AND version=?
                """,
                (expected_goal_version + 1, at, grant["goal_id"], expected_goal_version),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "grant revocation conflicted") from exc
    result = mode_status(store, grant["goal_id"], at)
    result["revocation_receipt_digest"] = receipt_digest
    result["idempotent"] = False
    return result
