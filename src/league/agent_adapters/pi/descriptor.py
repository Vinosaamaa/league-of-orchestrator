"""Pi-owned atomic descriptor transitions for runtime replacement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ...storage_types import StorageRefusal


@dataclass(frozen=True)
class PiReplacementDescriptorTransaction:
    schema: str
    source_adapter: str
    phase: str
    participant: str
    operation_id: str
    assignment_id: str
    descriptor_id: str | None
    session_ref: str | None
    provider_kind: str
    from_state: str
    to_state: str
    required: bool

    def apply(self, store: Any, *, at: str) -> None:
        """Verify or transition one exact Pi descriptor on the caller's transaction."""

        if (
            self.schema != "league.runtime-replacement-descriptor-transaction.v1"
            or self.source_adapter != "pi"
            or self.provider_kind not in {"codex", "cursor"}
            or self.from_state not in {"active", "blocked"}
            or self.to_state not in {"active", "blocked"}
            or (self.descriptor_id is None and self.session_ref is None)
        ):
            raise StorageRefusal(
                "runtime_replacement_descriptor_invalid",
                "Pi replacement descriptor transaction is malformed",
            )
        rows = store.connection.execute(
            """
            SELECT descriptor_id FROM provider_launch_descriptors
             WHERE runtime_kind='pi' AND provider_kind=?
               AND assignment_id=? AND state=?
               AND (? IS NULL OR descriptor_id=?)
               AND (? IS NULL OR session_path=?)
             ORDER BY descriptor_id
            """,
            (
                self.provider_kind,
                self.assignment_id,
                self.from_state,
                self.descriptor_id,
                self.descriptor_id,
                self.session_ref,
                self.session_ref,
            ),
        ).fetchall()
        if not rows and self.required is False:
            return
        if len(rows) != 1:
            raise StorageRefusal(
                "runtime_replacement_descriptor_ambiguous",
                "Pi replacement descriptor did not bind exactly once",
            )
        if self.from_state == self.to_state:
            return
        updated = store.connection.execute(
            """
            UPDATE provider_launch_descriptors
               SET state=?,version=version+1,updated_at=?
             WHERE descriptor_id=? AND runtime_kind='pi' AND provider_kind=?
               AND assignment_id=? AND state=?
            """,
            (
                self.to_state,
                at,
                rows[0]["descriptor_id"],
                self.provider_kind,
                self.assignment_id,
                self.from_state,
            ),
        )
        if updated.rowcount != 1:
            raise StorageRefusal(
                "runtime_replacement_descriptor_conflict",
                "Pi replacement descriptor transition lost its CAS",
            )


def replacement_descriptor_transactions(
    *,
    phase: str,
    participant: str,
    operation_id: str,
    assignment_id: str,
    target: Mapping[str, Any],
    activated: bool,
) -> tuple[PiReplacementDescriptorTransaction, ...]:
    session_ref = target.get("session_ref")
    provider_kind = target.get("provider_kind")
    if not isinstance(provider_kind, str) or provider_kind not in {"codex", "cursor"}:
        raise StorageRefusal(
            "runtime_replacement_descriptor_invalid",
            "Pi replacement descriptor provider is invalid",
        )
    common = {
        "schema": "league.runtime-replacement-descriptor-transaction.v1",
        "source_adapter": "pi",
        "operation_id": operation_id,
        "assignment_id": assignment_id,
        "session_ref": session_ref if isinstance(session_ref, str) and session_ref else None,
        "provider_kind": provider_kind,
        "required": True,
    }
    if phase == "activation" and participant == "predecessor":
        states = ("active", "blocked")
        descriptor_id = None
    elif phase == "activation" and participant == "successor":
        states = ("active", "active")
        descriptor_id = f"runtime-replacement:{operation_id}"
    elif phase == "rollback" and participant == "successor":
        states = ("active", "blocked")
        descriptor_id = f"runtime-replacement:{operation_id}"
        common["required"] = False
    elif phase == "rollback" and participant == "predecessor" and activated:
        states = ("blocked", "active")
        descriptor_id = None
    else:
        return ()
    return (
        PiReplacementDescriptorTransaction(
            **common,
            phase=phase,
            participant=participant,
            descriptor_id=descriptor_id,
            from_state=states[0],
            to_state=states[1],
        ),
    )


__all__ = [
    "PiReplacementDescriptorTransaction",
    "replacement_descriptor_transactions",
]
