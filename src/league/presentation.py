"""Canonical League-owned presentation metadata shared across adapters."""

from __future__ import annotations

from typing import Any


ORCHESTRATOR_ROLE_TOKEN = "orchestrator_role"
ORCHESTRATOR_ROLES = frozenset({"shotcaller", "champion"})


def orchestrator_role_tokens(role: Any) -> dict[str, str]:
    """Return one exact canonical role token, or no token for unknown roles."""

    return (
        {ORCHESTRATOR_ROLE_TOKEN: role}
        if isinstance(role, str) and role in ORCHESTRATOR_ROLES
        else {}
    )
