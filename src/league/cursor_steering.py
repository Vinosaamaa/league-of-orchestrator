"""Exact state-aware direct delivery to a verified Cursor CLI pane."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import PurePath
from typing import Any, Callable, Mapping

from .request_services import DeliveryAmbiguous, DeliveryReceipt, DeliveryUnavailable
from .storage_types import StorageRefusal


def _stable_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def structured_delivery_prompt(
    target: Mapping[str, Any], envelope: Mapping[str, Any], *, state_root: str
) -> str:
    """Render one bounded envelope with a direct routed-request acceptance command."""

    actions: list[dict[str, Any]] = []
    if envelope.get("event_type") == "request_routed" and envelope.get("request_id"):
        actions.append(
            {
                "schema": "league.routed-delivery-action.v1",
                "kind": "accept_routed_request",
                "argv": [
                    "league",
                    "--state-root",
                    state_root,
                    "request",
                    "accept-routed",
                    "--event-id",
                    str(envelope["event_id"]),
                    "--recipient-agent-id",
                    str(envelope["recipient_agent_id"]),
                    "--runtime-instance-id",
                    str(target["runtime_instance_id"]),
                ],
            }
        )
    routed = {
        "schema": "league.routed-delivery.v1",
        "delivery": dict(envelope),
        "actions": actions,
    }
    return "LEAGUE ROUTED DELIVERY " + _stable_json(routed)


class HerdrCursorSteeringAdapter:
    """Fence Cursor input behind exact live identity, status, and durable effects."""

    def __init__(
        self,
        store: Any,
        *,
        at: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.store = store
        self.at = at
        self.runner = runner

    @staticmethod
    def _unavailable(reason: str) -> DeliveryUnavailable:
        return DeliveryUnavailable(reason)

    @staticmethod
    def _ambiguous(reason: str) -> DeliveryAmbiguous:
        return DeliveryAmbiguous(reason)

    def _command(self, *arguments: str) -> list[str]:
        command = ["herdr"]
        if os.environ.get("HERDR_SESSION"):
            command.extend(("--session", os.environ["HERDR_SESSION"]))
        command.extend(arguments)
        return command

    def _run(self, command: list[str], reason: str) -> subprocess.CompletedProcess[str]:
        try:
            completed = self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            error = (
                self._ambiguous(reason)
                if reason == "cursor_steering_outcome_ambiguous"
                else self._unavailable(reason)
            )
            raise error from exc
        if completed.returncode != 0:
            if reason == "cursor_steering_outcome_ambiguous":
                raise self._ambiguous(reason)
            raise self._unavailable(reason)
        return completed

    @staticmethod
    def _payload(completed: subprocess.CompletedProcess[str], kind: str) -> dict[str, Any]:
        try:
            value = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DeliveryUnavailable(f"cursor_{kind}_malformed") from exc
        if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
            raise DeliveryUnavailable(f"cursor_{kind}_malformed")
        return value["result"]

    def _agent_observation(
        self, pane_id: str, target: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = self._payload(
            self._run(
                self._command("agent", "get", pane_id),
                "cursor_identity_unavailable",
            ),
            "identity",
        )
        agent = result.get("agent")
        if not isinstance(agent, dict):
            raise self._unavailable("cursor_identity_unavailable")
        session = agent.get("agent_session")
        if not isinstance(session, dict) or not isinstance(session.get("value"), str):
            raise self._unavailable("cursor_session_unavailable")
        if agent.get("pane_id") != pane_id:
            raise self._unavailable("cursor_wrong_pane")
        if session["value"] != target.get("session_ref"):
            raise self._unavailable("cursor_session_replaced")
        if agent.get("agent") != "cursor":
            raise self._unavailable("cursor_provider_mismatch")
        if target.get("routing_name") and agent.get("name") != target.get("routing_name"):
            raise self._unavailable("cursor_route_mismatch")
        if agent.get("interactive_ready") is not True:
            raise self._unavailable("cursor_input_unavailable")
        status = agent.get("agent_status")
        if status not in {"idle", "working", "blocked"}:
            raise self._unavailable("cursor_state_unavailable")
        revision = agent.get("revision")
        state_change_seq = agent.get("state_change_seq")
        if (
            type(revision) is not int
            or revision < 0
            or type(state_change_seq) is not int
            or state_change_seq < 0
        ):
            raise self._unavailable("cursor_identity_unavailable")
        return {
            "pane_id": pane_id,
            "session_ref": session["value"],
            "status": status,
            "revision": revision,
            "state_change_seq": state_change_seq,
        }

    def _process_observation(self, pane_id: str) -> dict[str, Any]:
        result = self._payload(
            self._run(
                self._command("pane", "process-info", "--pane", pane_id),
                "cursor_process_unavailable",
            ),
            "process",
        )
        info = result.get("process_info")
        if not isinstance(info, dict) or info.get("pane_id") != pane_id:
            raise self._unavailable("cursor_wrong_pane")
        processes = info.get("foreground_processes")
        if not isinstance(processes, list):
            raise self._unavailable("cursor_process_unavailable")
        cursor_processes: list[dict[str, Any]] = []
        for process in processes:
            if not isinstance(process, dict):
                raise self._unavailable("cursor_process_ambiguous")
            names = {
                str(process.get("name", "")),
                PurePath(str(process.get("argv0", ""))).name,
            }
            argv = process.get("argv")
            if isinstance(argv, list) and argv:
                names.add(PurePath(str(argv[0])).name)
            if "cursor-agent" in names:
                cursor_processes.append(process)
        if not cursor_processes:
            raise self._unavailable("cursor_process_unavailable")
        if len(cursor_processes) != 1:
            raise self._unavailable("cursor_process_ambiguous")
        pid = cursor_processes[0].get("pid")
        process_group = info.get("foreground_process_group_id")
        if type(pid) is not int or pid < 1 or type(process_group) is not int or process_group < 1:
            raise self._unavailable("cursor_process_ambiguous")
        return {"cursor_pid": pid, "foreground_process_group_id": process_group}

    def _observe(self, pane_id: str, target: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **self._agent_observation(pane_id, target),
            **self._process_observation(pane_id),
        }

    @staticmethod
    def _same_binding(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
        return all(
            before.get(key) == after.get(key)
            for key in (
                "pane_id",
                "session_ref",
                "status",
                "revision",
                "state_change_seq",
                "cursor_pid",
                "foreground_process_group_id",
            )
        )

    def _refuse(
        self, outbox_id: str, intent_digest: str, reason: str, phase: str
    ) -> None:
        self.store.record_cursor_steering_phase(
            outbox_id,
            intent_digest,
            "refused",
            {
                "schema": "league.cursor-steering-refusal.v1",
                "reason": reason,
                "phase": phase,
            },
            self.at,
        )
        if phase == "pre_effect":
            raise self._unavailable(reason)
        raise self._ambiguous(reason)

    def send(
        self, target: Mapping[str, Any], envelope: Mapping[str, Any]
    ) -> DeliveryReceipt:
        pane_id = target.get("locator")
        if (
            target.get("backend_kind") != "herdr"
            or target.get("harness_kind") not in {"cursor", "cursor-thread"}
            or not isinstance(pane_id, str)
            or not pane_id
            or not isinstance(target.get("session_ref"), str)
            or not target.get("session_ref")
            or not isinstance(target.get("generation"), str)
        ):
            raise self._unavailable("cursor_target_unverified")
        existing = self.store.cursor_steering_effect(str(envelope["outbox_id"]))
        if existing is not None:
            if existing["state"] in {"effect_applied", "acknowledged"} and existing["effect_id"]:
                return DeliveryReceipt(
                    outbox_id=str(envelope["outbox_id"]),
                    event_id=str(envelope["event_id"]),
                    recipient_agent_id=str(envelope["recipient_agent_id"]),
                    effect_kind="cursor_steering",
                    effect_id=str(existing["effect_id"]),
                )
            if existing["state"] == "refused" and isinstance(existing.get("receipt"), dict):
                reason = str(existing["receipt"].get("reason", "cursor_steering_refused"))
                if existing["receipt"].get("phase") == "pre_effect":
                    raise self._unavailable(reason)
                raise self._ambiguous(reason)
            raise self._ambiguous("cursor_steering_outcome_ambiguous")
        prompt = structured_delivery_prompt(
            target, envelope, state_root=str(self.store.state_root)
        )
        prompt_bytes = prompt.encode()
        initial = self._observe(pane_id, target)
        if initial["status"] not in {"idle", "working"}:
            raise self._unavailable("cursor_state_unavailable")
        action = "working_steer" if initial["status"] == "working" else "idle_submit"
        intent = {
            "schema": "league.cursor-steering-intent.v1",
            "outbox_id": str(envelope["outbox_id"]),
            "event_id": str(envelope["event_id"]),
            "recipient_agent_id": str(envelope["recipient_agent_id"]),
            "runtime_instance_id": str(target["runtime_instance_id"]),
            "runtime_generation": str(target["generation"]),
            "pane_id": pane_id,
            "session_ref": str(target["session_ref"]),
            "routing_name": str(target.get("routing_name") or pane_id),
            "action": action,
            "observed_status": initial["status"],
            "observed_revision": initial["revision"],
            "observed_state_change_seq": initial["state_change_seq"],
            "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
            "prompt_bytes": len(prompt_bytes),
        }
        begun = self.store.begin_cursor_steering(intent, self.at)
        if begun["idempotent"]:
            if begun["state"] in {"effect_applied", "acknowledged"} and begun["effect_id"]:
                return DeliveryReceipt(
                    outbox_id=str(envelope["outbox_id"]),
                    event_id=str(envelope["event_id"]),
                    recipient_agent_id=str(envelope["recipient_agent_id"]),
                    effect_kind="cursor_steering",
                    effect_id=str(begun["effect_id"]),
                )
            if begun["state"] == "refused" and isinstance(begun.get("receipt"), dict):
                reason = str(begun["receipt"].get("reason", "cursor_steering_refused"))
                if begun["receipt"].get("phase") == "pre_effect":
                    raise self._unavailable(reason)
                raise self._ambiguous(reason)
            raise self._ambiguous("cursor_steering_outcome_ambiguous")
        intent_digest = str(begun["intent_digest"])
        before_effect = self._observe(pane_id, target)
        if not self._same_binding(initial, before_effect):
            self._refuse(str(envelope["outbox_id"]), intent_digest, "cursor_state_changed", "pre_effect")

        command_receipts: list[dict[str, Any]] = []
        if action == "idle_submit":
            self._run(
                self._command("agent", "prompt", pane_id, prompt),
                "cursor_steering_outcome_ambiguous",
            )
            command_receipts.append({"command": "agent.prompt", "enter_count": 1})
        else:
            self._run(
                self._command("pane", "send-text", pane_id, prompt),
                "cursor_steering_outcome_ambiguous",
            )
            command_receipts.append({"command": "pane.send-text", "prompt_sha256": intent["prompt_sha256"]})
            self.store.record_cursor_steering_phase(
                str(envelope["outbox_id"]),
                intent_digest,
                "text_sent",
                {
                    "schema": "league.cursor-steering-text-receipt.v1",
                    "prompt_sha256": intent["prompt_sha256"],
                    "prompt_bytes": intent["prompt_bytes"],
                },
                self.at,
            )
            before_interrupt = self._observe(pane_id, target)
            if not self._same_binding(initial, before_interrupt):
                self._refuse(
                    str(envelope["outbox_id"]),
                    intent_digest,
                    "cursor_state_changed",
                    "pre_interrupt",
                )
            self._run(
                self._command("pane", "send-keys", pane_id, "enter", "enter"),
                "cursor_steering_outcome_ambiguous",
            )
            command_receipts.append({"command": "pane.send-keys", "keys": ["enter", "enter"]})

        post = self._observe(pane_id, target)
        if (
            post["pane_id"] != initial["pane_id"]
            or post["session_ref"] != initial["session_ref"]
            or post["cursor_pid"] != initial["cursor_pid"]
            or post["state_change_seq"] <= initial["state_change_seq"]
        ):
            self._refuse(
                str(envelope["outbox_id"]),
                intent_digest,
                "cursor_steering_ack_unverified",
                "post_effect",
            )
        receipt = {
            "schema": "league.cursor-steering-receipt.v1",
            "action": action,
            "commands": command_receipts,
            "post_status": post["status"],
            "post_revision": post["revision"],
            "post_state_change_seq": post["state_change_seq"],
            "intent_digest": intent_digest,
        }
        effect_id = hashlib.sha256(_stable_json(receipt).encode()).hexdigest()
        receipt["effect_id"] = effect_id
        self.store.record_cursor_steering_phase(
            str(envelope["outbox_id"]),
            intent_digest,
            "effect_applied",
            receipt,
            self.at,
        )
        return DeliveryReceipt(
            outbox_id=str(envelope["outbox_id"]),
            event_id=str(envelope["event_id"]),
            recipient_agent_id=str(envelope["recipient_agent_id"]),
            effect_kind="cursor_steering",
            effect_id=effect_id,
        )


__all__ = ["HerdrCursorSteeringAdapter", "structured_delivery_prompt"]
