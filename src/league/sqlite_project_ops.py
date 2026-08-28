"""Advisory project catalog operations over the canonical SQLite store."""

from __future__ import annotations

import posixpath
import re
import sqlite3
import unicodedata
from datetime import datetime
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

from .storage_types import StorageRefusal


PROJECT_ID = re.compile(r"^project:[a-z0-9][a-z0-9._-]{0,95}$")
SCP_REPOSITORY = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(.+)$")
TERMINAL_AGENT_STATES = {"completed", "complete", "cancelled", "canceled", "failed"}
MAX_PROJECTS = 500


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal("invalid_project", "project timestamp must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StorageRefusal("invalid_project", "project timestamp must include an offset")
    return value


def _text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise StorageRefusal("invalid_project", f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise StorageRefusal("invalid_project", f"{label} is empty or exceeds its bound")
    return normalized


def canonical_token(value: str, label: str, maximum: int = 64) -> tuple[str, str]:
    exact = _text(value, label, maximum)
    return exact, exact.casefold()


def canonical_root(value: str) -> tuple[str, str]:
    exact = _text(value, "project root", 2048)
    if not exact.startswith("/"):
        raise StorageRefusal("invalid_project", "project root must be an exact absolute path")
    key = posixpath.normpath(exact)
    if key == "/" or not key.startswith("/"):
        raise StorageRefusal("invalid_project", "filesystem root cannot be a project root")
    return key, key


def canonical_repository(value: str) -> tuple[str, str]:
    exact = _text(value, "project repository", 2048)
    host: Optional[str] = None
    path: Optional[str] = None
    if "://" in exact:
        try:
            parsed = urlsplit(exact)
            if parsed.scheme not in {"https", "http", "ssh", "git"}:
                raise StorageRefusal("invalid_project", "repository scheme is unsupported")
            if parsed.query or parsed.fragment or parsed.password:
                raise StorageRefusal("invalid_project", "repository identity cannot contain secrets, query, or fragment")
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise StorageRefusal("invalid_project", "repository URL is invalid") from exc
        if host is not None and port is not None:
            host = f"{host}:{port}"
        path = parsed.path
    else:
        scp = SCP_REPOSITORY.fullmatch(exact)
        if scp:
            host, path = scp.groups()
        elif "/" in exact:
            host, path = exact.split("/", 1)
    if host is None or path is None:
        raise StorageRefusal("invalid_project", "repository must identify an exact remote host and path")
    host = host.casefold().strip()
    path = unicodedata.normalize("NFC", path.strip("/"))
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if not host or not parts or any(part in {"", ".", ".."} for part in parts):
        raise StorageRefusal("invalid_project", "repository must identify an exact remote path")
    return exact, f"{host}/{'/'.join(parts)}"


def _visibility(value: str) -> str:
    if value not in {"local", "outbound"}:
        raise StorageRefusal("invalid_visibility", "visibility must be local or outbound")
    return value


def _alias_rows(store: Any, project_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.connection.execute(
            "SELECT alias,alias_key,position FROM project_aliases WHERE project_id=? ORDER BY position",
            (project_id,),
        )
    ]


def _alias_map(store: Any, project_ids: Sequence[str]) -> dict[str, list[str]]:
    result = {project_id: [] for project_id in project_ids}
    if not project_ids:
        return result
    placeholders = ",".join("?" for _ in project_ids)
    rows = store.connection.execute(
        f"""
        SELECT project_id,alias FROM project_aliases
         WHERE project_id IN ({placeholders})
         ORDER BY project_id,position,alias_key
        """,
        tuple(project_ids),
    ).fetchall()
    for row in rows:
        result[str(row["project_id"])].append(str(row["alias"]))
    return result


def _squad_unavailable_reason(row: Any) -> Optional[str]:
    if row["squad_state"] != "active":
        return "squad_retired"
    if row["retired_at"] is not None or row["shotcaller_status"] in TERMINAL_AGENT_STATES:
        return "shotcaller_unavailable"
    return None


def _squad_map(store: Any, project_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    result = {project_id: [] for project_id in project_ids}
    if not project_ids:
        return result
    placeholders = ",".join("?" for _ in project_ids)
    rows = store.connection.execute(
        f"""
        SELECT p.project_id,p.squad_id,p.position,s.state squad_state,a.callsign,
               a.status shotcaller_status,a.retired_at
          FROM project_squad_suggestions p
          JOIN squads s ON s.squad_id=p.squad_id
          JOIN agent_instances a ON a.agent_id=s.shotcaller_agent_id
         WHERE p.project_id IN ({placeholders})
         ORDER BY p.project_id,p.position,p.squad_id
        """,
        tuple(project_ids),
    ).fetchall()
    for row in rows:
        reason = _squad_unavailable_reason(row)
        result[str(row["project_id"])].append(
            {
                "squad_id": row["squad_id"],
                "shotcaller": row["callsign"],
                "position": int(row["position"]),
                "available": reason is None,
                "unavailable_reason": reason,
            }
        )
    return result


def _squad_rows(store: Any, project_id: str) -> list[dict[str, Any]]:
    return _squad_map(store, [project_id])[project_id]


def _project_value(
    store: Any,
    row: Any,
    visibility: str,
    *,
    aliases: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    mode = _visibility(visibility)
    alias_values = (
        list(aliases)
        if aliases is not None
        else [item["alias"] for item in _alias_rows(store, row["project_id"])]
    )
    repository = row["repository"]
    root = row["root_path"]
    if mode == "outbound":
        repository = "[redacted]" if repository is not None else None
        root = "[redacted]" if root is not None else None
    return {
        "project_id": row["project_id"],
        "summary": row["summary"],
        "aliases": alias_values,
        "code": row["code"],
        "root": root,
        "repository": repository,
        "state": row["state"],
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


def _row_by_id(store: Any, project_id: str) -> Any:
    return store.connection.execute(
        """
        SELECT project_id,repository,summary,root_path,code,state,version,updated_at
          FROM projects WHERE project_id=?
        """,
        (project_id,),
    ).fetchone()


def resolve_project_routing_identity(
    store: Any, repository: str
) -> Optional[tuple[str, str]]:
    """Resolve only the project identity needed by assignment reservation."""
    _, key = canonical_repository(repository)
    rows = store.connection.execute(
        """
        SELECT project_id,state FROM projects
        WHERE repository_key=?
         ORDER BY project_id LIMIT 2
        """,
        (key,),
    ).fetchall()
    if len(rows) > 1:
        raise StorageRefusal(
            "ambiguous_project", "project identity matches more than one catalog entry"
        )
    if not rows:
        return None
    return str(rows[0]["project_id"]), str(rows[0]["state"])


def resolve_project(
    store: Any,
    repository: Optional[str] = None,
    *,
    project_id: Optional[str] = None,
    root: Optional[str] = None,
    code: Optional[str] = None,
    alias: Optional[str] = None,
    visibility: str = "local",
) -> Optional[dict[str, Any]]:
    selectors = [repository is not None, project_id is not None, root is not None, code is not None, alias is not None]
    if sum(selectors) != 1:
        raise StorageRefusal("invalid_project_selector", "exactly one project identity selector is required")
    parameters: tuple[Any, ...]
    if project_id is not None:
        query = "SELECT * FROM projects WHERE project_id=?"
        parameters = (project_id,)
    elif repository is not None:
        _, key = canonical_repository(repository)
        query = "SELECT * FROM projects WHERE repository_key=?"
        parameters = (key,)
    elif root is not None:
        exact, key = canonical_root(root)
        query = "SELECT * FROM projects WHERE root_key=? OR (root_key IS NULL AND root_path=?)"
        parameters = (key, exact)
    elif code is not None:
        _, key = canonical_token(code, "project code", 24)
        query = "SELECT * FROM projects WHERE code_key=?"
        parameters = (key,)
    else:
        _, key = canonical_token(alias or "", "project alias")
        query = """
            SELECT p.* FROM projects p JOIN project_aliases a ON a.project_id=p.project_id
             WHERE a.alias_key=? ORDER BY p.project_id
        """
        parameters = (key,)
    rows = list(store.connection.execute(query, parameters).fetchall())
    if len(rows) > 1:
        raise StorageRefusal("ambiguous_project", "project identity matches more than one catalog entry")
    return _project_value(store, rows[0], visibility) if rows else None


def _normalized_aliases(values: Sequence[str]) -> list[tuple[str, str]]:
    if len(values) > 16:
        raise StorageRefusal("invalid_project", "a project supports at most 16 aliases")
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in values:
        exact, key = canonical_token(value, "project alias")
        if key in seen:
            raise StorageRefusal("invalid_project", "project aliases must be unique after normalization")
        seen.add(key)
        result.append((exact, key))
    return result


def put_project(
    store: Any,
    project_id: str,
    *,
    expected_version: int,
    summary: str,
    repository: str,
    root: str,
    code: Optional[str],
    aliases: Sequence[str],
    state: str,
    at: str,
) -> dict[str, Any]:
    if not PROJECT_ID.fullmatch(project_id) or expected_version < 0 or state not in {"active", "retired"}:
        raise StorageRefusal("invalid_project", "project identity, version, or state is invalid")
    summary_value = _text(summary, "project summary", 240)
    repository_value, repository_key = canonical_repository(repository)
    root_value, root_key = canonical_root(root)
    code_value: Optional[str] = None
    code_key: Optional[str] = None
    if code is not None:
        code_value, code_key = canonical_token(code, "project code", 24)
    alias_values = _normalized_aliases(aliases)
    _timestamp(at)
    try:
        with store._transaction():
            existing = _row_by_id(store, project_id)
            existing_aliases = [
                (row["alias"], row["alias_key"]) for row in _alias_rows(store, project_id)
            ]
            if existing is None:
                if expected_version != 0:
                    raise StorageRefusal("version_conflict", "new project requires expected version zero")
            elif int(existing["version"]) != expected_version:
                raise StorageRefusal("version_conflict", "project expected-version precondition failed")
            desired = {
                "summary": summary_value,
                "repository": repository_value,
                "root_path": root_value,
                "code": code_value,
                "state": state,
                "updated_at": at,
            }
            if existing is not None and all(existing[key] == value for key, value in desired.items()) and existing_aliases == alias_values:
                value = _project_value(store, existing, "local")
                value["idempotent"] = True
                return value
            for column, key, label in (
                ("repository_key", repository_key, "repository"),
                ("root_key", root_key, "root"),
                ("code_key", code_key, "code"),
            ):
                if key is None:
                    continue
                collision = store.connection.execute(
                    f"SELECT project_id FROM projects WHERE {column}=? AND project_id<>? LIMIT 1",
                    (key, project_id),
                ).fetchone()
                if collision is not None:
                    raise StorageRefusal("project_identity_conflict", f"canonical project {label} already belongs to another project")
            if existing is None:
                version = 1
                store.connection.execute(
                    """
                    INSERT INTO projects
                      (project_id,repository,state,version,updated_at,summary,root_path,
                       repository_key,root_key,code,code_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (project_id, repository_value, state, version, at, summary_value, root_value,
                     repository_key, root_key, code_value, code_key),
                )
            else:
                version = expected_version + 1
                store.connection.execute(
                    """
                    UPDATE projects SET repository=?,state=?,version=?,updated_at=?,summary=?,
                           root_path=?,repository_key=?,root_key=?,code=?,code_key=?
                     WHERE project_id=? AND version=?
                    """,
                    (repository_value, state, version, at, summary_value, root_value,
                     repository_key, root_key, code_value, code_key, project_id, expected_version),
                )
                store.connection.execute("DELETE FROM project_aliases WHERE project_id=?", (project_id,))
            for position, (alias_value, alias_key) in enumerate(alias_values):
                store.connection.execute(
                    "INSERT INTO project_aliases(project_id,alias,alias_key,position) VALUES(?,?,?,?)",
                    (project_id, alias_value, alias_key, position),
                )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "project catalog write conflicted with canonical state") from exc
    value = resolve_project(store, project_id=project_id)
    assert value is not None
    value["idempotent"] = False
    return value


def set_project_suggestions(
    store: Any,
    project_id: str,
    expected_version: int,
    squad_ids: Sequence[str],
    at: str,
) -> dict[str, Any]:
    if expected_version < 1 or len(squad_ids) > 32 or len(set(squad_ids)) != len(squad_ids):
        raise StorageRefusal("invalid_project_suggestions", "suggested Squads are invalid or duplicated")
    _timestamp(at)
    try:
        with store._transaction():
            project = _row_by_id(store, project_id)
            if project is None:
                raise StorageRefusal("project_unknown", "project does not exist")
            if int(project["version"]) != expected_version:
                raise StorageRefusal("version_conflict", "project expected-version precondition failed")
            current = [row["squad_id"] for row in _squad_rows(store, project_id)]
            if current == list(squad_ids):
                return {
                    "project_id": project_id,
                    "version": int(project["version"]),
                    "idempotent": True,
                    "suggestions": _squad_rows(store, project_id),
                }
            if squad_ids:
                placeholders = ",".join("?" for _ in squad_ids)
                known = {
                    str(row["squad_id"])
                    for row in store.connection.execute(
                        f"SELECT squad_id FROM squads WHERE squad_id IN ({placeholders})",
                        tuple(squad_ids),
                    )
                }
                if known != set(squad_ids):
                    raise StorageRefusal("squad_unknown", "suggested Squad does not exist")
            store.connection.execute("DELETE FROM project_squad_suggestions WHERE project_id=?", (project_id,))
            for position, squad_id in enumerate(squad_ids):
                store.connection.execute(
                    """
                    INSERT INTO project_squad_suggestions
                      (project_id,squad_id,position,created_at,updated_at)
                    VALUES(?,?,?,?,?)
                    """,
                    (project_id, squad_id, position, at, at),
                )
            version = expected_version + 1
            store.connection.execute(
                "UPDATE projects SET version=?,updated_at=? WHERE project_id=? AND version=?",
                (version, at, project_id, expected_version),
            )
    except StorageRefusal:
        raise
    except sqlite3.DatabaseError as exc:
        raise store._translate_database_error(exc, "project suggestion write conflicted with canonical state") from exc
    return {
        "project_id": project_id,
        "version": version,
        "idempotent": False,
        "suggestions": _squad_rows(store, project_id),
    }


def list_projects(store: Any, *, visibility: str = "local", limit: int = 200) -> dict[str, Any]:
    _visibility(visibility)
    if not 1 <= limit <= MAX_PROJECTS:
        raise StorageRefusal("invalid_limit", f"project limit must be between 1 and {MAX_PROJECTS}")
    rows = store.connection.execute(
        "SELECT * FROM projects ORDER BY project_id LIMIT ?", (limit + 1,)
    ).fetchall()
    sample = list(rows[:limit])
    aliases = _alias_map(store, [str(row["project_id"]) for row in sample])
    return {
        "schema": "league.project-catalog.v1",
        "canonical": False,
        "visibility": visibility,
        "projects": [
            _project_value(
                store,
                row,
                visibility,
                aliases=aliases[str(row["project_id"])],
            )
            for row in sample
        ],
        "truncated": len(rows) > limit,
        "limit": limit,
    }


def project_advice(
    store: Any,
    project_id: str,
    *,
    explicit_squad_id: Optional[str] = None,
    visibility: str = "local",
) -> dict[str, Any]:
    project = resolve_project(store, project_id=project_id, visibility=visibility)
    if project is None:
        raise StorageRefusal("project_unknown", "project does not exist")
    suggestions = _squad_rows(store, project_id)
    explicit = None
    if explicit_squad_id is not None:
        squad = store.connection.execute(
            """
            SELECT s.state squad_state,a.status shotcaller_status,a.retired_at
              FROM squads s JOIN agent_instances a ON a.agent_id=s.shotcaller_agent_id
             WHERE s.squad_id=?
            """,
            (explicit_squad_id,),
        ).fetchone()
        available = squad is not None and _squad_unavailable_reason(squad) is None
        explicit = {
            "squad_id": explicit_squad_id,
            "known": squad is not None,
            "available": available,
            "source": "explicit",
        }
    return {
        "schema": "league.project-advice.v1",
        "canonical": False,
        "project": project,
        "explicit_route": explicit,
        "suggestions": suggestions,
        "available_suggestions": [item for item in suggestions if item["available"]],
        "binding_changed": False,
    }
