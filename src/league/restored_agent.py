"""Exact restored-agent lifecycle reconciliation.

The public command composes one agent adapter and one multiplexer adapter over
canonical League state.  It never starts, resumes, prompts, or closes a process.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .agent_adapters import builtin_agent_adapter_registry
from .display_replay import canonical_presentations
from .multiplexer_adapters import builtin_multiplexer_adapter_registry
from .persistent_supervisor import (
    SupervisorUnavailable,
    send_supervisor_message,
    supervisor_wake_locator,
)
from .storage_types import StorageRefusal


class RestoredWatcherAdapter(Protocol):
    def preflight(self, presentation: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def bind(
        self,
        presentation: Mapping[str, Any],
        runtime_receipt: Mapping[str, Any],
        preflight: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def verify(
        self,
        presentation: Mapping[str, Any],
        runtime_receipt: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise StorageRefusal(
            "restored_agent_time_invalid", "reconciliation time must be RFC3339"
        ) from exc
    if parsed.tzinfo is None:
        raise StorageRefusal(
            "restored_agent_time_invalid", "reconciliation time needs a UTC offset"
        )
    return parsed


def restored_runtime_generation(
    multiplexer_kind: str, terminal_id: str, session_ref: str
) -> str:
    if not all((multiplexer_kind, terminal_id, session_ref)):
        raise StorageRefusal(
            "runtime_reconcile_identity_mismatch",
            "restored runtime generation identity is incomplete",
        )
    digest = hashlib.sha256(f"{terminal_id}\0{session_ref}".encode("utf-8")).hexdigest()
    return f"{multiplexer_kind}:{digest[:24]}"


class SupervisorWatcherAdapter:
    """Rebind an already-running canonical supervisor to a restored runtime."""

    def __init__(self, store: Any, at: str) -> None:
        self.store = store
        self.at = _timestamp(at)

    @staticmethod
    def _expected(
        presentation: Mapping[str, Any], runtime: Mapping[str, Any]
    ) -> dict[str, str]:
        return {
            "actor_agent_id": str(presentation["agent_id"]),
            "runtime_instance_id": str(presentation["runtime_instance_id"]),
            "runtime_generation": str(runtime["runtime_generation"]),
            "endpoint": str(runtime["endpoint"]),
            "session_ref": str(presentation["session_ref"]),
        }

    def preflight(self, presentation: Mapping[str, Any]) -> Mapping[str, Any]:
        registration = self.store.watcher_registration(str(presentation["agent_id"]))
        locator = (
            str(registration["wake_locator"])
            if isinstance(registration, Mapping)
            else supervisor_wake_locator(Path(self.store.state_root))
        )
        try:
            response = send_supervisor_message(
                locator,
                {
                    "kind": "ping",
                    "actor_agent_id": str(presentation["agent_id"]),
                },
                timeout_seconds=0.5,
            )
        except (SupervisorUnavailable, StorageRefusal) as exc:
            raise StorageRefusal(
                "restored_agent_watcher_unavailable",
                "Shotcaller wake locator is not a verified live supervisor",
                retryable=True,
            ) from exc
        exact = {
            "actor_agent_id": str(presentation["agent_id"]),
            "runtime_instance_id": str(presentation["runtime_instance_id"]),
            "session_ref": str(presentation["session_ref"]),
        }
        if (
            response.get("callsign") != presentation["tokens"]["sidebar_name"]
            or any(response.get(key) != value for key, value in exact.items())
            or type(response.get("fence")) is not int
            or int(response["fence"]) < 1
        ):
            raise StorageRefusal(
                "restored_agent_watcher_mismatch",
                "Shotcaller wake locator belongs to another runtime",
            )
        return {
            "locator": locator,
            "fence": int(response["fence"]),
            "runtime_generation": response.get("runtime_generation"),
            "endpoint": response.get("endpoint"),
            "registration": None if registration is None else dict(registration),
        }

    def bind(
        self,
        presentation: Mapping[str, Any],
        runtime_receipt: Mapping[str, Any],
        preflight: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        expected = self._expected(presentation, runtime_receipt)
        registration = self.store.watcher_registration(str(presentation["agent_id"]))
        lease_live = False
        if isinstance(registration, Mapping):
            try:
                lease_live = _timestamp(str(registration["leased_until"])) > self.at
            except StorageRefusal:
                lease_live = False
        already_exact = bool(
            preflight.get("runtime_generation") == expected["runtime_generation"]
            and preflight.get("endpoint") == expected["endpoint"]
            and isinstance(registration, Mapping)
            and registration.get("runtime_instance_id") == expected["runtime_instance_id"]
            and registration.get("wake_locator") == preflight["locator"]
            and int(registration.get("fence", 0)) == int(preflight["fence"])
            and lease_live
        )
        if already_exact:
            return {
                "schema": "league.restored-watcher.v1",
                "watcher_id": str(registration["watcher_id"]),
                "wake_locator_verified": True,
                "fence": int(registration["fence"]),
                "idempotent": True,
            }
        try:
            response = send_supervisor_message(
                str(preflight["locator"]),
                {
                    "kind": "reconcile-restored-runtime",
                    "fence": int(preflight["fence"]),
                    **expected,
                },
                timeout_seconds=1.0,
            )
        except (SupervisorUnavailable, StorageRefusal) as exc:
            raise StorageRefusal(
                "restored_agent_watcher_unavailable",
                "Shotcaller supervisor could not bind the restored runtime",
                retryable=True,
            ) from exc
        current = self.store.watcher_registration(str(presentation["agent_id"]))
        try:
            verified = send_supervisor_message(
                str(preflight["locator"]),
                {
                    "kind": "ping",
                    "actor_agent_id": str(presentation["agent_id"]),
                },
                timeout_seconds=0.5,
            )
        except (SupervisorUnavailable, StorageRefusal) as exc:
            raise StorageRefusal(
                "restored_agent_watcher_unavailable",
                "Shotcaller supervisor did not acknowledge its restored binding",
                retryable=True,
            ) from exc
        if (
            not isinstance(current, Mapping)
            or any(response.get(key) != value for key, value in expected.items())
            or any(verified.get(key) != value for key, value in expected.items())
            or current.get("runtime_instance_id") != expected["runtime_instance_id"]
            or current.get("wake_locator") != preflight["locator"]
            or int(current.get("fence", 0)) != response.get("fence")
            or verified.get("fence") != response.get("fence")
            or _timestamp(str(current["leased_until"])) <= self.at
        ):
            raise StorageRefusal(
                "restored_agent_watcher_unverified",
                "Shotcaller watcher did not settle on the exact restored runtime",
            )
        return {
            "schema": "league.restored-watcher.v1",
            "watcher_id": str(current["watcher_id"]),
            "wake_locator_verified": True,
            "fence": int(current["fence"]),
            "idempotent": False,
        }

    def verify(
        self,
        presentation: Mapping[str, Any],
        runtime_receipt: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        expected = self._expected(presentation, runtime_receipt)
        registration = self.store.watcher_registration(str(presentation["agent_id"]))
        if not isinstance(registration, Mapping):
            raise StorageRefusal(
                "restored_agent_watcher_unverified",
                "Shotcaller watcher registration disappeared before final acceptance",
            )
        try:
            observed = send_supervisor_message(
                str(registration["wake_locator"]),
                {
                    "kind": "ping",
                    "actor_agent_id": str(presentation["agent_id"]),
                },
                timeout_seconds=0.5,
            )
        except (SupervisorUnavailable, StorageRefusal) as exc:
            raise StorageRefusal(
                "restored_agent_watcher_unavailable",
                "Shotcaller watcher was unavailable at final acceptance",
                retryable=True,
            ) from exc
        if (
            any(observed.get(key) != value for key, value in expected.items())
            or observed.get("fence") != registration.get("fence")
            or registration.get("runtime_instance_id") != expected["runtime_instance_id"]
            or _timestamp(str(registration["leased_until"])) <= self.at
        ):
            raise StorageRefusal(
                "restored_agent_watcher_unverified",
                "Shotcaller watcher does not bind the final restored runtime",
            )
        return {"watcher_live": True, "fence": int(registration["fence"])}


def _bind_inventory(
    presentations: list[dict[str, Any]], inventory: list[Mapping[str, Any]]
) -> tuple[list[tuple[dict[str, Any], Mapping[str, Any]]], list[str]]:
    bound: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    pending: list[str] = []
    used_endpoints: set[tuple[Any, Any]] = set()
    sessions = [str(item["session_ref"]) for item in presentations]
    if len(sessions) != len(set(sessions)):
        raise StorageRefusal(
            "restored_agent_canonical_ambiguous",
            "one immutable session is bound to more than one canonical runtime",
        )
    for presentation in presentations:
        matches = [
            item
            for item in inventory
            if isinstance(item.get("agent_session"), Mapping)
            and item["agent_session"].get("value") == presentation["session_ref"]
        ]
        if not matches:
            occupied = [
                item for item in inventory
                if item.get("name") == presentation["routing_name"]
            ]
            if occupied:
                raise StorageRefusal(
                    "restored_agent_session_replaced",
                    "canonical routing name is occupied by another native session",
                )
            pending.append(str(presentation["runtime_instance_id"]))
            continue
        if len(matches) != 1:
            raise StorageRefusal(
                "restored_agent_session_ambiguous",
                "immutable native session appears on multiple restored endpoints",
            )
        match = matches[0]
        endpoint_key = (match.get("pane_id"), match.get("terminal_id"))
        if endpoint_key in used_endpoints:
            raise StorageRefusal(
                "restored_agent_session_ambiguous",
                "one restored endpoint matched multiple canonical sessions",
            )
        used_endpoints.add(endpoint_key)
        conflicting_route = [
            item
            for item in inventory
            if item.get("name") == presentation["routing_name"]
            and item.get("pane_id") != match.get("pane_id")
        ]
        if conflicting_route:
            raise StorageRefusal(
                "restored_agent_route_occupied",
                "canonical routing name belongs to another live endpoint",
            )
        bound.append((presentation, match))
    return bound, pending


def reconcile_restored_agents(
    store: Any,
    *,
    multiplexer_kind: str,
    at: str,
    timeout_ms: int = 30_000,
    poll_ms: int = 100,
    herdr_runner: Any = None,
    watcher_adapter: RestoredWatcherAdapter | None = None,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    """Reconcile every exact restored canonical agent without process effects."""

    _timestamp(at)
    if not 0 <= timeout_ms <= 300_000 or not 10 <= poll_ms <= 5_000:
        raise StorageRefusal(
            "restored_agent_timeout_invalid", "restored-agent timeout is invalid"
        )
    agents = builtin_agent_adapter_registry()
    multiplexers = builtin_multiplexer_adapter_registry(herdr_runner=herdr_runner)
    multiplexer = multiplexers.adapter(multiplexer_kind)
    required = {"discover", "routing", "metadata"}
    if not required <= multiplexer.capabilities:
        raise StorageRefusal(
            "multiplexer_restore_unsupported",
            "selected multiplexer cannot reconcile a restored agent",
        )
    presentations = canonical_presentations(
        store, multiplexer_kind=multiplexer_kind
    )
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        inventory = multiplexer.discover()
        bound, pending = _bind_inventory(presentations, inventory)
        if not pending:
            break
        if time.monotonic() >= deadline:
            raise StorageRefusal(
                "restored_agent_not_ready",
                "canonical sessions were not discoverable before timeout",
                retryable=True,
            )
        sleeper(poll_ms / 1000)

    prepared: list[dict[str, Any]] = []
    watchers = watcher_adapter or SupervisorWatcherAdapter(store, at)
    for presentation, item in bound:
        endpoint = multiplexer.endpoint(str(presentation["descriptor_id"]), item)
        observation = multiplexer.inspect_restored(presentation, endpoint)
        translated = agents.adapter(
            str(presentation["agent_adapter_kind"])
        ).restored_presentation(presentation, observation)
        watcher_preflight = (
            watchers.preflight(presentation)
            if presentation["role"] == "shotcaller"
            else None
        )
        prepared.append(
            {
                "presentation": presentation,
                "endpoint": endpoint,
                "observation": observation,
                "translated": translated,
                "watcher_preflight": watcher_preflight,
            }
        )

    receipts: list[dict[str, Any]] = []
    for item in prepared:
        presentation = item["presentation"]
        endpoint = item["endpoint"]
        before = item["observation"]
        try:
            route = multiplexer.routing(presentation, endpoint)
            if route["process_fingerprint"] != before["process_fingerprint"]:
                raise StorageRefusal(
                    "restored_agent_process_changed",
                    "routing changed the restored foreground process",
                )
            generation = multiplexer.runtime_generation(
                before["agent"], str(presentation["session_ref"])
            )
            runtime = store.reconcile_restored_runtime(
                str(presentation["runtime_instance_id"]),
                str(presentation["agent_id"]),
                str(presentation["thread_id"]),
                str(presentation["session_ref"]),
                multiplexer_kind,
                str(presentation["endpoint"]),
                str(presentation["runtime_generation"]),
                endpoint.pane_id,
                generation,
                at,
            )
            watcher = (
                watchers.bind(
                    presentation,
                    runtime,
                    item["watcher_preflight"],
                )
                if presentation["role"] == "shotcaller"
                else {"idempotent": True}
            )
            after_route = multiplexer.inspect_restored(presentation, endpoint)
            if (
                after_route["process_fingerprint"] != before["process_fingerprint"]
                or after_route["agent"].get("name") != presentation["routing_name"]
            ):
                raise StorageRefusal(
                    "restored_agent_route_unverified",
                    "restored process or routing identity changed before presentation",
                )
            metadata = multiplexer.metadata(
                item["translated"], endpoint, int(after_route["state_change_seq"]) + 1
            )
        except StorageRefusal as exc:
            store.record_restored_runtime_recovery(
                str(presentation["runtime_instance_id"]),
                str(presentation["agent_id"]),
                exc.code,
                at,
            )
            raise StorageRefusal(
                "restored_agent_recovery_pending",
                "restored-agent reconciliation crossed its guarded effect boundary; exact retry is required",
                retryable=True,
            ) from exc
        idempotent = bool(
            route["idempotent"]
            and runtime["idempotent"]
            and watcher["idempotent"]
            and metadata["idempotent"]
        )
        receipts.append(
            {
                "runtime_instance_id": presentation["runtime_instance_id"],
                "agent_id": presentation["agent_id"],
                "role": presentation["role"],
                "session_ref": presentation["session_ref"],
                "matched": True,
                "runtime_reconciled": True,
                "route_bound": True,
                "watcher_live": (
                    True if presentation["role"] == "shotcaller" else "not_applicable"
                ),
                "presentation_replayed": True,
                "pane_id": endpoint.pane_id,
                "terminal_id": endpoint.terminal_id,
                "runtime_generation": generation,
                "stable_readbacks": metadata["stable_readbacks"],
                "idempotent": idempotent,
            }
        )

    receipt_by_runtime = {
        str(receipt["runtime_instance_id"]): receipt for receipt in receipts
    }
    try:
        final_inventory = multiplexer.discover()
        final_bound, final_pending = _bind_inventory(presentations, final_inventory)
        if final_pending or len(final_bound) != len(prepared):
            raise StorageRefusal(
                "restored_agent_final_identity_unverified",
                "restored inventory changed before reconciliation acceptance",
            )
        for presentation, inventory_item in final_bound:
            receipt = receipt_by_runtime[str(presentation["runtime_instance_id"])]
            endpoint = multiplexer.endpoint(
                str(presentation["descriptor_id"]), inventory_item
            )
            if (
                endpoint.pane_id != receipt["pane_id"]
                or endpoint.terminal_id != receipt["terminal_id"]
            ):
                raise StorageRefusal(
                    "restored_agent_final_identity_unverified",
                    "restored endpoint changed before final acceptance",
                )
            observed = multiplexer.inspect_restored(presentation, endpoint)
            translated = agents.adapter(
                str(presentation["agent_adapter_kind"])
            ).restored_presentation(presentation, observed)
            route = multiplexer.routing(presentation, endpoint)
            metadata = multiplexer.metadata(
                translated, endpoint, int(observed["state_change_seq"]) + 1
            )
            runtime = store.reconcile_restored_runtime(
                str(presentation["runtime_instance_id"]),
                str(presentation["agent_id"]),
                str(presentation["thread_id"]),
                str(presentation["session_ref"]),
                multiplexer_kind,
                endpoint.pane_id,
                str(receipt["runtime_generation"]),
                endpoint.pane_id,
                str(receipt["runtime_generation"]),
                at,
            )
            if not route["idempotent"] or not metadata["idempotent"] or not runtime["idempotent"]:
                raise StorageRefusal(
                    "restored_agent_final_identity_unverified",
                    "final restored-agent readback required an additional effect",
                )
            if presentation["role"] == "shotcaller":
                watchers.verify(presentation, runtime)
            store.satisfy_restored_runtime_recovery(
                str(presentation["runtime_instance_id"]), at
            )
    except StorageRefusal as exc:
        for prepared_item in prepared:
            affected = prepared_item["presentation"]
            store.record_restored_runtime_recovery(
                str(affected["runtime_instance_id"]),
                str(affected["agent_id"]),
                exc.code,
                at,
            )
        raise StorageRefusal(
            "restored_agent_recovery_pending",
            "final restored-agent acceptance failed; exact retry is required",
            retryable=True,
        ) from exc
    return {
        "schema": "league.restored-agent-reconciliation.v1",
        "multiplexer_kind": multiplexer_kind,
        "candidate_count": len(presentations),
        "reconciled_count": sum(not receipt["idempotent"] for receipt in receipts),
        "idempotent_count": sum(receipt["idempotent"] for receipt in receipts),
        "receipts": receipts,
        "created_processes": 0,
        "resumed_sessions": 0,
        "prompted_sessions": 0,
        "closed_processes": 0,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


__all__ = [
    "RestoredWatcherAdapter",
    "SupervisorWatcherAdapter",
    "reconcile_restored_agents",
    "restored_runtime_generation",
    "utc_now",
]
