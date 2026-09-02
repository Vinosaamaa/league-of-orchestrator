"""Canonical League-owned presentation metadata shared across adapters."""

from __future__ import annotations

import re
from typing import Any

from .storage_types import StorageRefusal


ORCHESTRATOR_ROLE_TOKEN = "orchestrator_role"
ORCHESTRATOR_ROLES = frozenset({"shotcaller", "champion"})
_CALLSIGN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_PROJECT_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,23}$")


def orchestrator_role_tokens(role: Any) -> dict[str, str]:
    """Return one exact canonical role token, or no token for unknown roles."""

    return (
        {ORCHESTRATOR_ROLE_TOKEN: role}
        if isinstance(role, str) and role in ORCHESTRATOR_ROLES
        else {}
    )


def canonical_display_metadata(metadata: Any) -> dict[str, str]:
    """Render owner-visible names from explicit canonical fields only."""

    if not isinstance(metadata, dict):
        return {}
    role = metadata.get(ORCHESTRATOR_ROLE_TOKEN)
    if role not in ORCHESTRATOR_ROLES:
        return {}
    callsign = metadata.get("callsign")
    if not isinstance(callsign, str) or _CALLSIGN.fullmatch(callsign) is None:
        raise StorageRefusal(
            "presentation_metadata_invalid",
            "canonical presentation callsign is invalid",
        )
    if role == "shotcaller":
        return {
            "title": callsign,
            "terminal_title": callsign,
            "sidebar_name": callsign,
            "thread_title": callsign,
            ORCHESTRATOR_ROLE_TOKEN: role,
        }

    task_label = metadata.get("task_label")
    if (
        not isinstance(task_label, str)
        or len(task_label.split()) != 2
        or " ".join(task_label.split()) != task_label
        or len(task_label) > 48
        or any(character in task_label for character in "\r\n\0")
    ):
        raise StorageRefusal(
            "presentation_metadata_invalid",
            "canonical Champion task label must contain exactly two words",
        )
    project_code = metadata.get("project_code")
    if project_code is not None and (
        not isinstance(project_code, str)
        or _PROJECT_CODE.fullmatch(project_code) is None
    ):
        raise StorageRefusal(
            "presentation_metadata_invalid",
            "canonical Champion project code is invalid",
        )
    suffix = project_code or task_label
    title = f"{callsign} · {suffix}"
    result = {
        "title": title,
        "terminal_title": title,
        "sidebar_name": callsign,
        "thread_title": title,
        "task_label": task_label,
        ORCHESTRATOR_ROLE_TOKEN: role,
    }
    if project_code is not None:
        result["project_code"] = project_code
    return result
