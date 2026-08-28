#!/usr/bin/env python3
"""Pending Squad registration and exact-runtime acceptance coverage."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from league.storage import StorageRefusal  # noqa: E402
from request_lifecycle_fixture import (  # noqa: E402
    GAREN_RUNTIME,
    JARVAN_ID,
    JARVAN_RUNTIME,
    create_context,
)
from storage_fixture import SHOTCALLER_ID  # noqa: E402


def _register(store, clock, *, suffix: str = "one", expires_at: str | None = None):
    return store.register_squad(
        registration_id=f"registration:{suffix}",
        squad_id=f"squad:Jarvan-{suffix}",
        requester_agent_id=SHOTCALLER_ID,
        shotcaller_agent_id=JARVAN_ID,
        runtime_instance_id=JARVAN_RUNTIME,
        project_ids=(),
        capabilities=("request.route",),
        expires_at=expires_at or clock.after(600),
        event_id=f"event:registration:{suffix}",
        outbox_id=f"outbox:registration:{suffix}",
        at=clock.now(),
    )


def test_idempotent_pending_offer_does_not_activate(root: Path) -> None:
    _, store, clock = create_context(root, "squad-idempotent")
    try:
        offered = _register(store, clock)
        assert offered["state"] == "pending"
        assert _register(store, clock)["idempotent"] is True
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE squad_id='squad:Jarvan-one'"
        ).fetchone()[0] == 0
        assert store.squad_status(
            registration_id="registration:one", at=clock.now()
        )["registration"]["state"] == "pending"
    finally:
        store.close()


def test_wrong_runtime_and_expired_offer_leave_no_squad(root: Path) -> None:
    _, store, clock = create_context(root, "squad-expiry")
    try:
        _register(store, clock, expires_at=clock.after(60))
        try:
            store.accept_squad(
                registration_id="registration:one",
                shotcaller_agent_id=JARVAN_ID,
                runtime_instance_id=GAREN_RUNTIME,
                decision="accept",
                event_id="event:accept:wrong",
                outbox_id="outbox:accept:wrong",
                at=clock.now(),
            )
        except StorageRefusal as exc:
            assert exc.code == "squad_runtime_mismatch"
        else:
            raise AssertionError("a different Shotcaller runtime accepted the offer")
        clock.advance(61)
        try:
            store.accept_squad(
                registration_id="registration:one",
                shotcaller_agent_id=JARVAN_ID,
                runtime_instance_id=JARVAN_RUNTIME,
                decision="accept",
                event_id="event:accept:expired",
                outbox_id="outbox:accept:expired",
                at=clock.now(),
            )
        except StorageRefusal as exc:
            assert exc.code == "squad_registration_expired"
        else:
            raise AssertionError("an expired Squad registration activated")
        assert store.connection.execute(
            "SELECT state FROM squad_registration_offers WHERE registration_id='registration:one'"
        ).fetchone()[0] == "expired"
        assert store.connection.execute("SELECT COUNT(*) FROM squads").fetchone()[0] == 1
    finally:
        store.close()


def test_acceptance_and_requester_outbox_are_atomic(root: Path) -> None:
    _, store, clock = create_context(root, "squad-atomic")
    try:
        offered = _register(store, clock)
        try:
            store.accept_squad(
                registration_id="registration:one",
                shotcaller_agent_id=JARVAN_ID,
                runtime_instance_id=JARVAN_RUNTIME,
                decision="accept",
                event_id="event:accept:collision",
                outbox_id=offered["outbox_id"],
                at=clock.now(),
            )
        except StorageRefusal:
            pass
        else:
            raise AssertionError("acceptance survived a colliding requester outbox")
        assert store.connection.execute(
            "SELECT COUNT(*) FROM squads WHERE squad_id='squad:Jarvan-one'"
        ).fetchone()[0] == 0
        accepted = store.accept_squad(
            registration_id="registration:one",
            shotcaller_agent_id=JARVAN_ID,
            runtime_instance_id=JARVAN_RUNTIME,
            decision="accept",
            event_id="event:accept:one",
            outbox_id="outbox:accept:one",
            at=clock.now(),
        )
        assert accepted["state"] == "accepted"
        committed = store.connection.execute(
            """
            SELECT s.squad_id,i.state,o.recipient_agent_id
              FROM squads s JOIN shotcaller_intake i ON i.squad_id=s.squad_id
              JOIN squad_registration_offers r ON r.squad_id=s.squad_id
              JOIN delivery_outbox o ON o.outbox_id=r.response_outbox_id
             WHERE s.squad_id='squad:Jarvan-one'
            """
        ).fetchone()
        assert tuple(committed) == ("squad:Jarvan-one", "accepting", SHOTCALLER_ID)
    finally:
        store.close()


def test_active_binding_never_overwrites_rollover_boundary(root: Path) -> None:
    _, store, clock = create_context(root, "squad-binding")
    try:
        _register(store, clock)
        store.accept_squad(
            registration_id="registration:one",
            shotcaller_agent_id=JARVAN_ID,
            runtime_instance_id=JARVAN_RUNTIME,
            decision="accept",
            event_id="event:accept:one",
            outbox_id="outbox:accept:one",
            at=clock.now(),
        )
        try:
            _register(store, clock, suffix="replacement")
        except StorageRefusal as exc:
            assert exc.code == "squad_active_conflict"
        else:
            raise AssertionError("registration overwrote an active Shotcaller binding")
        row = store.connection.execute(
            "SELECT squad_id,shotcaller_agent_id,owner_fence FROM squads WHERE shotcaller_agent_id=?",
            (JARVAN_ID,),
        ).fetchone()
        assert tuple(row) == ("squad:Jarvan-one", JARVAN_ID, 1)
    finally:
        store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-squad-registration-") as temporary:
        root = Path(temporary)
        test_idempotent_pending_offer_does_not_activate(root)
        test_wrong_runtime_and_expired_offer_leave_no_squad(root)
        test_acceptance_and_requester_outbox_are_atomic(root)
        test_active_binding_never_overwrites_rollover_boundary(root)
    print("PASS: pending Squad registration, exact acceptance, atomic outbox, and rollover boundary")


if __name__ == "__main__":
    main()
