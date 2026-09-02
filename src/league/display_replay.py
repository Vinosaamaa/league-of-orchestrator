"""Provider- and multiplexer-neutral restored-display reconciliation.

The asynchronous Herdr startup plugin is only a caller.  Canonical League
state remains the source of presentation truth and adapters perform all native
agent and multiplexer translation.  Reconciliation never creates, resumes, or
prompts a process.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

from .agent_adapters import builtin_agent_adapter_registry
from .multiplexer_adapters import builtin_multiplexer_adapter_registry
from .storage_types import StorageRefusal


LIVE_RUNTIME_STATES = frozenset({"active", "idle"})
VISIBLE_ROLES = frozenset({"shotcaller", "champion"})


def _adapter_kind(value: str) -> str:
    normalized = value.removesuffix("-thread")
    try:
        builtin_agent_adapter_registry().adapter(normalized)
    except StorageRefusal as exc:
        raise StorageRefusal(
            "display_replay_adapter_unknown",
            "canonical runtime has no registered display adapter",
        ) from exc
    return normalized


def _one(rows: list[Any], code: str, message: str) -> Any:
    if len(rows) != 1:
        raise StorageRefusal(code, message)
    return rows[0]


def _project_code(store: Any, row: Mapping[str, Any]) -> str:
    if row["role"] == "champion":
        project_rows = store.connection.execute(
            """
            SELECT p.code FROM tasks t JOIN projects p ON p.project_id=t.project_id
             WHERE t.task_id=? AND p.state='active'
            """,
            (row["task_id"],),
        ).fetchall()
        project = _one(
            list(project_rows),
            "display_replay_project_unproven",
            "Champion runtime does not bind one active canonical project",
        )
        code = project["code"]
    else:
        repository = row.get("repository")
        project = (
            store.resolve_project(repository, visibility="local")
            if isinstance(repository, str) and repository
            else None
        )
        code = project.get("code") if isinstance(project, Mapping) else None
    if not isinstance(code, str) or not code or len(code.encode("utf-8")) > 80:
        raise StorageRefusal(
            "display_replay_project_unproven",
            "runtime project code is not exact canonical metadata",
        )
    return code


def canonical_presentations(
    store: Any, *, multiplexer_kind: str = "herdr"
) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        """
        SELECT r.runtime_instance_id,r.harness_kind,r.backend_kind,r.session_ref,
               r.endpoint,r.runtime_generation,
               r.status AS runtime_status,r.verified,a.agent_id,a.callsign,a.role,
               a.task_id,a.kind,a.thread_id,a.routing_name,a.display_agent,a.repository,a.worktree,
               a.status AS agent_status,a.retired_at
          FROM runtime_instances r JOIN agent_instances a
            ON a.agent_id=r.actor_agent_id
         WHERE r.backend_kind=? AND r.status IN ('active','idle')
           AND r.verified=1 AND a.retired_at IS NULL
           AND a.role IN ('shotcaller','champion')
         ORDER BY r.runtime_instance_id
        """,
        (multiplexer_kind,),
    ).fetchall()
    presentations: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        for key in (
            "runtime_instance_id", "session_ref", "endpoint", "runtime_generation",
            "agent_id", "callsign",
            "role", "thread_id", "routing_name", "display_agent", "worktree", "agent_status",
        ):
            if not isinstance(row.get(key), str) or not row[key]:
                raise StorageRefusal(
                    "display_replay_identity_unproven",
                    "canonical restored runtime identity is incomplete",
                )
        cwd = Path(str(row["worktree"]))
        if not cwd.is_absolute() or cwd == Path("/"):
            raise StorageRefusal(
                "display_replay_identity_unproven",
                "canonical restored runtime cwd is not exact",
            )
        adapter_kind = _adapter_kind(str(row["harness_kind"]))
        adapter = builtin_agent_adapter_registry().adapter(adapter_kind)
        assignment_id = adapter.canonical_assignment(store=store, row=row)
        project_code = _project_code(store, row)
        owned = adapter.canonical_presentation(
            store=store,
            row=row,
            assignment_id=assignment_id,
            project_code=project_code,
        )
        thread = str(owned.pop("thread"))
        if len(thread.encode("utf-8")) > 80:
            raise StorageRefusal(
                "display_replay_thread_unrepresentable",
                "canonical thread identity exceeds Herdr metadata bounds",
            )
        tokens = dict(owned.pop("tokens"))
        tokens.update(
            {
                "sidebar_name": str(row["callsign"]),
                "project_code": project_code,
                "task_label": str(owned["task_label"]),
                "routing_alias": str(row["routing_name"]),
                "thread": thread,
                "thread_title": str(owned["title"]),
                "orchestrator_role": str(row["role"]),
                "display_provider": str(owned["provider_kind"]),
                "status_token": str(row["agent_status"]),
            }
        )
        presentations.append(
            {
                "schema": "league.restored-presentation.v1",
                "descriptor_id": assignment_id,
                "assignment_id": assignment_id,
                "runtime_instance_id": str(row["runtime_instance_id"]),
                "agent_id": str(row["agent_id"]),
                "role": str(row["role"]),
                "runtime_kind": adapter_kind,
                "agent_adapter_kind": adapter_kind,
                "multiplexer_kind": multiplexer_kind,
                "provider_kind": str(owned["provider_kind"]),
                "session_ref": str(row["session_ref"]),
                "thread_id": thread,
                "endpoint": str(row["endpoint"]),
                "runtime_generation": str(row["runtime_generation"]),
                "cwd": str(cwd),
                "routing_name": str(row["routing_name"]),
                "metadata_source": str(owned["metadata_source"]),
                "applies_to_source": str(owned["applies_to_source"]),
                "title": str(owned["title"]),
                "tokens": tokens,
            }
        )
    return presentations


def replay_restored_display(
    store: Any,
    *,
    multiplexer_kind: str = "herdr",
    timeout_ms: int = 30_000,
    poll_ms: int = 100,
    herdr_runner: Any = None,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    if not 0 <= timeout_ms <= 300_000 or not 10 <= poll_ms <= 5_000:
        raise StorageRefusal(
            "display_replay_timeout_invalid", "display replay timeout is invalid"
        )
    agents = builtin_agent_adapter_registry()
    multiplexers = builtin_multiplexer_adapter_registry(herdr_runner=herdr_runner)
    multiplexer = multiplexers.adapter(multiplexer_kind)
    if "discover" not in multiplexer.capabilities or "metadata" not in multiplexer.capabilities:
        raise StorageRefusal(
            "multiplexer_restore_unsupported",
            "selected multiplexer has no restored-display replay capability",
        )
    presentations = canonical_presentations(store, multiplexer_kind=multiplexer_kind)
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        inventory = multiplexer.discover()
        pending: list[str] = []
        bound: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
        for presentation in presentations:
            matches = [
                item
                for item in inventory
                if isinstance(item.get("agent_session"), Mapping)
                and item["agent_session"].get("value") == presentation["session_ref"]
            ]
            if not matches:
                route_replacement = [
                    item
                    for item in inventory
                    if item.get("name") == presentation["routing_name"]
                ]
                if route_replacement:
                    raise StorageRefusal(
                        "display_replay_session_replaced",
                        "canonical routing name is occupied by a different native session",
                    )
                pending.append(str(presentation["runtime_instance_id"]))
                continue
            if len(matches) != 1:
                raise StorageRefusal(
                    "display_replay_session_ambiguous",
                    "native session appears on more than one restored endpoint",
                )
            bound.append((presentation, matches[0]))
        if not pending:
            receipts: list[dict[str, Any]] = []
            for presentation, item in bound:
                endpoint = multiplexer.endpoint(str(presentation["descriptor_id"]), item)
                observation = multiplexer.inspect_restored(presentation, endpoint)
                translated = agents.adapter(
                    str(presentation["agent_adapter_kind"])
                ).restored_presentation(presentation, observation)
                receipt = multiplexer.metadata(
                    translated, endpoint, int(observation["state_change_seq"]) + 1
                )
                receipts.append(
                    {
                        "runtime_instance_id": presentation["runtime_instance_id"],
                        "session_ref": receipt["session_ref"],
                        "pane_id": receipt["pane_id"],
                        "terminal_id": receipt["terminal_id"],
                        "idempotent": receipt["idempotent"],
                        "stable_readbacks": receipt["stable_readbacks"],
                    }
                )
            return {
                "schema": "league.restored-display-replay.v1",
                "multiplexer_kind": multiplexer_kind,
                "candidate_count": len(presentations),
                "replayed_count": sum(not receipt["idempotent"] for receipt in receipts),
                "idempotent_count": sum(receipt["idempotent"] for receipt in receipts),
                "receipts": receipts,
                "created_processes": 0,
                "resumed_sessions": 0,
            }
        if time.monotonic() >= deadline:
            raise StorageRefusal(
                "display_replay_not_ready",
                "canonical restored sessions were not discoverable before timeout",
                retryable=True,
            )
        sleeper(poll_ms / 1000)


__all__ = ["canonical_presentations", "replay_restored_display"]
