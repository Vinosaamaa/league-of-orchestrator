"""Herdr restored-endpoint and presentation transport adapter."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...storage_types import StorageRefusal
from ..contract import CommandRunner, RestoredEndpoint


MAX_TOKENS_PER_REPORT = 16


class SubprocessRunner:
    def run(
        self, arguments: Sequence[str], timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            tuple(arguments), text=True, capture_output=True,
            timeout=timeout_seconds, check=False,
        )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _result(completed: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    try:
        envelope = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal(
            "display_replay_adapter_failed", f"{label} returned malformed JSON"
        ) from exc
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if completed.returncode != 0 or not isinstance(result, dict):
        raise StorageRefusal("display_replay_adapter_failed", f"{label} refused or failed")
    return result


class HerdrMultiplexerAdapter:
    kind = "herdr"
    capabilities = frozenset(
        {
            "calling_context", "discover", "routing", "placement", "metadata", "title",
            "delivery", "steering_delivery", "close", "visible_launch", "shotcaller_bootstrap",
            "rollover_reconciliation", "production_cleanup",
            "runtime_replacement",
        }
    )

    def __init__(
        self, runner: CommandRunner | None = None, *, binary: str | None = None
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.binary = binary or os.environ.get("HERDR_BIN_PATH") or "herdr"

    def calling_context(self) -> Mapping[str, str]:
        context = {
            "workspace_id": os.environ.get("HERDR_WORKSPACE_ID", ""),
            "tab_id": os.environ.get("HERDR_TAB_ID", ""),
            "pane_id": os.environ.get("HERDR_PANE_ID", ""),
        }
        if os.environ.get("HERDR_ENV") != "1" or any(not value for value in context.values()):
            raise StorageRefusal(
                "multiplexer_context_unavailable",
                "Herdr calling pane identity is unavailable",
            )
        return context

    def visible_launch_driver(self, agent_kind: str, **inputs: Any) -> Any:
        """Return the Herdr compatibility driver selected by agent kind.

        Provider adapters own provider validation and descriptor construction;
        this multiplexer adapter is the sole place that chooses a native
        Herdr process/layout driver.
        """

        if agent_kind == "pi":
            from ...pi_launch import HerdrPiLaunchAdapter

            return HerdrPiLaunchAdapter(
                inputs["store"],
                inputs["descriptor"],
                at=inputs["at"],
                runner=self.runner,
                startup_timeout_ms=inputs["startup_timeout_ms"],
            )
        if agent_kind in {"codex", "cursor"}:
            from ...visible_launch import HerdrCodexLaunchAdapter

            return HerdrCodexLaunchAdapter(
                inputs["options"],
                self.runner,
                environment=inputs.get("environment"),
                resume_thread_id=inputs.get("resume_session_id"),
                harness_kind=agent_kind,
            )
        raise StorageRefusal(
            "launch_harness_unsupported",
            "selected multiplexer cannot launch the requested agent adapter",
        )

    def shotcaller_bootstrap_driver(self, options: Any) -> Any:
        from ...shotcaller_bootstrap import HerdrShotcallerBootstrapAdapter

        return HerdrShotcallerBootstrapAdapter(options, runner=self.runner)

    def rollover_snapshot_driver(self) -> Any:
        from ...rollover_snapshot import HerdrRolloverSnapshotAdapter

        return HerdrRolloverSnapshotAdapter(runner=self.runner)

    def rollover_descendant_driver(self) -> Any:
        from ...rollover_descendant import HerdrDescendantRuntimeAdapter

        return HerdrDescendantRuntimeAdapter(runner=self.runner)

    def cleanup_drivers(self, **inputs: Any) -> tuple[Any, Any]:
        from ...production_cleanup import HerdrBackendAdapter, HerdrHarnessAdapter

        identity = dict(inputs["identity"])
        pane_id = str(identity.get("pane_id", ""))
        workspace_id, separator, _ = pane_id.partition(":")
        if not separator or not workspace_id:
            raise StorageRefusal(
                "cleanup_identity_mismatch",
                "Herdr workspace identity is incomplete",
            )
        identity["workspace_id"] = workspace_id
        return (
            HerdrHarnessAdapter(identity, inputs["runner"]),
            HerdrBackendAdapter(
                inputs["store"], identity, inputs["runner"], inputs["at"]
            ),
        )

    def _command(self, arguments: Sequence[str], label: str) -> dict[str, Any]:
        return _result(self.runner.run(arguments, timeout_seconds=30), label)

    def _effect(self, arguments: Sequence[str], label: str) -> None:
        completed = self.runner.run(arguments, timeout_seconds=30)
        if completed.returncode != 0:
            raise StorageRefusal("display_replay_adapter_failed", f"{label} refused or failed")

    def discover(self) -> list[Mapping[str, Any]]:
        result = self._command(
            (self.binary, "agent", "list"), "Herdr restored agent discovery"
        )
        agents = result.get("agents")
        if not isinstance(agents, list) or any(
            not isinstance(agent, Mapping) for agent in agents
        ):
            raise StorageRefusal(
                "display_replay_adapter_failed",
                "Herdr restored agent discovery returned no exact inventory",
            )
        return [dict(agent) for agent in agents]

    def endpoint(self, descriptor_id: str, item: Mapping[str, Any]) -> RestoredEndpoint:
        values = [item.get(key) for key in ("workspace_id", "tab_id", "pane_id", "terminal_id")]
        if any(not isinstance(value, str) or not value for value in values):
            raise StorageRefusal(
                "display_replay_binding_invalid", "restored endpoint identity is incomplete"
            )
        return RestoredEndpoint(descriptor_id, *values)

    def runtime_generation(
        self, item: Mapping[str, Any], session_ref: str
    ) -> str:
        terminal_id = item.get("terminal_id")
        if (
            not isinstance(terminal_id, str)
            or not terminal_id
            or not isinstance(session_ref, str)
            or not session_ref
        ):
            raise StorageRefusal(
                "runtime_observation_refused",
                "Herdr runtime generation identity is incomplete",
                retryable=True,
            )
        return "herdr:" + hashlib.sha256(
            f"{terminal_id}\0{session_ref}".encode("utf-8")
        ).hexdigest()[:24]

    def inspect_restored(
        self, descriptor: Mapping[str, Any], endpoint: RestoredEndpoint
    ) -> dict[str, Any]:
        agent = dict(
            self._command(
                (self.binary, "agent", "get", endpoint.pane_id),
                "Herdr restored agent inspection",
            ).get("agent", {})
        )
        pane = dict(
            self._command(
                (self.binary, "pane", "get", endpoint.pane_id),
                "Herdr restored pane inspection",
            ).get("pane", {})
        )
        info = self._command(
            (self.binary, "pane", "process-info", "--pane", endpoint.pane_id),
            "Herdr restored process inspection",
        ).get("process_info")
        processes = info.get("foreground_processes") if isinstance(info, Mapping) else None
        if not isinstance(processes, list) or len(processes) != 1 or not isinstance(processes[0], Mapping):
            raise StorageRefusal(
                "display_replay_process_ambiguous",
                "restored pane does not have one exact foreground process",
            )
        process = dict(processes[0])
        if (
            agent.get("workspace_id") != endpoint.workspace_id
            or agent.get("tab_id") != endpoint.tab_id
            or agent.get("pane_id") != endpoint.pane_id
            or agent.get("terminal_id") != endpoint.terminal_id
            or pane.get("workspace_id") != endpoint.workspace_id
            or pane.get("tab_id") != endpoint.tab_id
            or pane.get("pane_id") != endpoint.pane_id
            or pane.get("terminal_id") != endpoint.terminal_id
            or agent.get("cwd") != descriptor["cwd"]
            or agent.get("foreground_cwd") != descriptor["cwd"]
            or process.get("cwd") != descriptor["cwd"]
            or not isinstance(process.get("pid"), int)
            or not isinstance(process.get("process_start"), str)
            or not process["process_start"]
            or type(agent.get("state_change_seq")) is not int
        ):
            raise StorageRefusal(
                "display_replay_binding_mismatch",
                "restored endpoint does not bind the canonical cwd and process",
            )
        session = agent.get("agent_session")
        return {
            "agent": agent,
            "pane": pane,
            "process": process,
            "workspace_id": endpoint.workspace_id,
            "tab_id": endpoint.tab_id,
            "pane_id": endpoint.pane_id,
            "terminal_id": endpoint.terminal_id,
            "session_ref": session.get("value") if isinstance(session, Mapping) else None,
            "session_source": session.get("source") if isinstance(session, Mapping) else None,
            "cwd": str(descriptor["cwd"]),
            "process_fingerprint": _digest(process),
            "state_change_seq": int(agent["state_change_seq"]),
        }

    def routing(
        self,
        descriptor: Mapping[str, Any],
        endpoint: RestoredEndpoint,
    ) -> dict[str, Any]:
        """Restore one canonical routing name on an exact proven session."""

        before = self.inspect_restored(descriptor, endpoint)
        expected = descriptor.get("routing_name")
        if not isinstance(expected, str) or not expected:
            raise StorageRefusal(
                "multiplexer_route_invalid", "canonical routing name is unavailable"
            )
        inventory = self.discover()
        occupied = [
            item for item in inventory
            if item.get("name") == expected and item.get("pane_id") != endpoint.pane_id
        ]
        if occupied:
            raise StorageRefusal(
                "multiplexer_route_occupied",
                "canonical routing name belongs to another live endpoint",
            )
        previous = before["agent"].get("name")
        idempotent = previous == expected
        if not idempotent:
            self._effect(
                (self.binary, "agent", "rename", endpoint.pane_id, expected),
                "Herdr restored routing name",
            )
        after = self.inspect_restored(descriptor, endpoint)
        if after["agent"].get("name") != expected:
            raise StorageRefusal(
                "multiplexer_route_unverified",
                "restored routing name did not bind to the exact endpoint",
            )
        return {
            "schema": "league.multiplexer-route.v1",
            "pane_id": endpoint.pane_id,
            "terminal_id": endpoint.terminal_id,
            "session_ref": after["session_ref"],
            "routing_name": expected,
            "previous_routing_name": previous,
            "process_fingerprint": after["process_fingerprint"],
            "idempotent": idempotent,
        }

    def placement(self, specification: Mapping[str, Any]) -> RestoredEndpoint:
        descriptor_id = specification.get("descriptor_id")
        workspace_id = specification.get("workspace_id")
        role = specification.get("role")
        cwd = Path(str(specification.get("cwd", "")))
        if (
            not isinstance(descriptor_id, str)
            or not descriptor_id
            or not isinstance(workspace_id, str)
            or not workspace_id
            or role not in {"shotcaller", "champion"}
            or not cwd.is_absolute()
            or cwd == Path("/")
            or not cwd.is_dir()
            or cwd.is_symlink()
        ):
            raise StorageRefusal(
                "multiplexer_placement_invalid",
                "placement requires an exact role, workspace, and existing cwd",
            )
        environment_arguments = specification.get("environment_arguments", ())
        if (
            not isinstance(environment_arguments, (tuple, list))
            or len(environment_arguments) % 2
            or any(
                environment_arguments[index] != "--env"
                or not isinstance(environment_arguments[index + 1], str)
                or "=" not in environment_arguments[index + 1]
                for index in range(0, len(environment_arguments), 2)
            )
        ):
            raise StorageRefusal(
                "multiplexer_placement_invalid",
                "placement environment must contain exact --env key-value pairs",
            )
        if role == "champion":
            label = specification.get("label")
            label_arguments = (
                ("--label", label)
                if isinstance(label, str) and label and not any(c in label for c in "\r\n\0")
                else ()
            )
            result = self._command(
                (
                    self.binary,
                    "tab",
                    "create",
                    "--workspace",
                    workspace_id,
                    "--cwd",
                    str(cwd.resolve()),
                    *label_arguments,
                    *environment_arguments,
                    "--no-focus",
                ),
                "Herdr Champion placement",
            )
            tab, pane = result.get("tab"), result.get("root_pane")
        else:
            creator = specification.get("creator_pane_id")
            if not isinstance(creator, str) or not creator:
                raise StorageRefusal(
                    "multiplexer_placement_invalid",
                    "Shotcaller placement requires the exact creator pane",
                )
            result = self._command(
                (
                    self.binary,
                    "pane",
                    "split",
                    creator,
                    "--direction",
                    "right",
                    "--cwd",
                    str(cwd.resolve()),
                    *environment_arguments,
                    "--no-focus",
                ),
                "Herdr Shotcaller placement",
            )
            pane = result.get("pane")
            tab = {"tab_id": result.get("tab_id")}
        if not isinstance(tab, Mapping) or not isinstance(pane, Mapping):
            raise StorageRefusal(
                "multiplexer_placement_unverified", "Herdr placement receipt is incomplete"
            )
        values = [tab.get("tab_id"), pane.get("pane_id"), pane.get("terminal_id")]
        if any(not isinstance(value, str) or not value for value in values):
            raise StorageRefusal(
                "multiplexer_placement_unverified", "Herdr placement identity is incomplete"
            )
        return RestoredEndpoint(
            descriptor_id,
            workspace_id,
            str(values[0]),
            str(values[1]),
            str(values[2]),
        )

    def title(self, endpoint: RestoredEndpoint, title: str) -> dict[str, Any]:
        if not isinstance(title, str) or not title or any(c in title for c in "\r\n\0"):
            raise StorageRefusal("multiplexer_title_invalid", "title is not exact")
        self._effect(
            (self.binary, "pane", "rename", endpoint.pane_id, title),
            "Herdr pane title",
        )
        return {
            "schema": "league.multiplexer-title.v1",
            "pane_id": endpoint.pane_id,
            "terminal_id": endpoint.terminal_id,
            "title": title,
        }

    def delivery(self, target: str, body: str, *, wait: bool = False) -> dict[str, Any]:
        if (
            not isinstance(target, str)
            or not target
            or not isinstance(body, str)
            or not body
            or len(body.encode("utf-8")) > 64 * 1024
        ):
            raise StorageRefusal(
                "multiplexer_delivery_invalid", "delivery target or body is invalid"
            )
        arguments = [self.binary, "agent", "prompt", target, body]
        if wait:
            arguments.extend(("--wait", "--timeout", "30000"))
        self._effect(tuple(arguments), "Herdr agent delivery")
        return {
            "schema": "league.multiplexer-delivery.v1",
            "target": target,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "waited": wait,
        }

    def steering_delivery(self, **inputs: Any) -> Any:
        from ...cursor_steering import HerdrCursorSteeringAdapter

        def invoke(arguments: Sequence[str], **options: Any) -> subprocess.CompletedProcess[str]:
            return self.runner.run(
                arguments, timeout_seconds=int(options.get("timeout", 15))
            )

        return HerdrCursorSteeringAdapter(
            inputs["store"], at=inputs["at"], runner=invoke
        ).send(inputs["target"], inputs["envelope"])

    def replacement_verify(self, **inputs: Any) -> Mapping[str, Any]:
        """Bind one replacement participant to an exact native process/session."""

        target = inputs.get("target")
        process_names = inputs.get("process_names")
        adapter_kind = inputs.get("adapter_kind")
        provider_kind = inputs.get("provider_kind")
        if (
            not isinstance(target, Mapping)
            or not isinstance(process_names, frozenset)
            or not process_names
            or not isinstance(adapter_kind, str)
            or not adapter_kind
            or not isinstance(provider_kind, str)
            or not provider_kind
        ):
            raise StorageRefusal(
                "runtime_replacement_identity_invalid",
                "replacement adapter identity is incomplete",
            )
        endpoint_id = target.get("endpoint")
        session_ref = target.get("session_ref")
        cwd = target.get("cwd")
        routing_name = target.get("routing_name")
        runtime_generation = target.get("runtime_generation")
        if any(
            not isinstance(value, str) or not value
            for value in (
                endpoint_id,
                session_ref,
                cwd,
                routing_name,
                runtime_generation,
            )
        ):
            raise StorageRefusal(
                "runtime_replacement_identity_invalid",
                "replacement runtime identity is incomplete",
            )
        matches = [
            item for item in self.discover() if item.get("pane_id") == endpoint_id
        ]
        if len(matches) != 1:
            raise StorageRefusal(
                "runtime_replacement_identity_ambiguous",
                "replacement endpoint is missing or ambiguous",
            )
        item = matches[0]
        endpoint = self.endpoint(str(target.get("agent_id", "replacement")), item)
        agent = dict(
            self._command(
                (self.binary, "agent", "get", endpoint.pane_id),
                "Herdr replacement agent inspection",
            ).get("agent", {})
        )
        pane = dict(
            self._command(
                (self.binary, "pane", "get", endpoint.pane_id),
                "Herdr replacement pane inspection",
            ).get("pane", {})
        )
        info = self._command(
            (self.binary, "pane", "process-info", "--pane", endpoint.pane_id),
            "Herdr replacement process inspection",
        ).get("process_info")
        processes = info.get("foreground_processes") if isinstance(info, Mapping) else None
        if (
            not isinstance(processes, list)
            or len(processes) != 1
            or not isinstance(processes[0], Mapping)
        ):
            raise StorageRefusal(
                "runtime_replacement_process_ambiguous",
                "replacement endpoint has no single exact foreground process",
            )
        process = dict(processes[0])
        observed_process = Path(str(process.get("argv0") or process.get("name") or "")).name
        session = agent.get("agent_session")
        observed_session = session.get("value") if isinstance(session, Mapping) else None
        observed_generation = self.runtime_generation(item, str(observed_session or ""))
        exact = bool(
            item.get("pane_id") == endpoint.pane_id
            and item.get("terminal_id") == endpoint.terminal_id
            and agent.get("pane_id") == endpoint.pane_id
            and agent.get("terminal_id") == endpoint.terminal_id
            and pane.get("pane_id") == endpoint.pane_id
            and pane.get("terminal_id") == endpoint.terminal_id
            and agent.get("name") == routing_name
            and agent.get("agent") == adapter_kind
            and agent.get("display_agent") == provider_kind
            and observed_session == session_ref
            and agent.get("cwd") == cwd
            and agent.get("foreground_cwd") == cwd
            and process.get("cwd") == cwd
            and observed_process in process_names
            and observed_generation == runtime_generation
            and isinstance(process.get("pid"), int)
            and isinstance(process.get("process_start"), str)
            and bool(process["process_start"])
        )
        if not exact:
            raise StorageRefusal(
                "runtime_replacement_identity_mismatch",
                "replacement endpoint does not match the exact adapter, provider, cwd, process, and session",
            )
        return {
            "schema": "league.runtime-replacement-verification.v1",
            "verified": True,
            "agent_id": target.get("agent_id"),
            "runtime_instance_id": target.get("runtime_instance_id"),
            "adapter_kind": adapter_kind,
            "provider_kind": provider_kind,
            "multiplexer_kind": self.kind,
            "workspace_id": endpoint.workspace_id,
            "tab_id": endpoint.tab_id,
            "pane_id": endpoint.pane_id,
            "terminal_id": endpoint.terminal_id,
            "session_ref": session_ref,
            "endpoint": endpoint_id,
            "runtime_generation": runtime_generation,
            "cwd": cwd,
            "routing_name": routing_name,
            "process_fingerprint": _digest(process),
        }

    def replacement_route_swap(self, **inputs: Any) -> Mapping[str, Any]:
        """Move A aside and promote proven B; undo A if B promotion fails."""

        operation_id = inputs.get("operation_id")
        predecessor = dict(inputs.get("predecessor") or {})
        successor = dict(inputs.get("successor") or {})
        predecessor_verification = dict(
            inputs.get("predecessor_verification") or {}
        )
        successor_verification = dict(inputs.get("successor_verification") or {})
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or predecessor_verification.get("verified") is not True
            or successor_verification.get("verified") is not True
        ):
            raise StorageRefusal(
                "runtime_replacement_route_invalid",
                "route swap requires two exact verified participants",
            )
        canonical = predecessor.get("routing_name")
        staging = successor.get("routing_name")
        predecessor_staging = "retired_" + hashlib.sha256(
            operation_id.encode("utf-8")
        ).hexdigest()[:16]
        if any(
            not isinstance(value, str) or not value
            for value in (canonical, staging)
        ):
            raise StorageRefusal(
                "runtime_replacement_route_invalid",
                "route swap names are incomplete",
            )
        occupied = {
            str(item["name"]): str(item.get("pane_id", ""))
            for item in self.discover()
            if isinstance(item.get("name"), str) and item["name"]
        }
        if (
            occupied.get(str(canonical)) != predecessor.get("endpoint")
            or occupied.get(str(staging)) != successor.get("endpoint")
            or (
                predecessor_staging in occupied
                and occupied[predecessor_staging] != predecessor.get("endpoint")
            )
        ):
            raise StorageRefusal(
                "runtime_replacement_route_occupied",
                "replacement route ownership changed before the swap",
            )
        self._effect(
            (
                self.binary,
                "agent",
                "rename",
                str(predecessor["endpoint"]),
                predecessor_staging,
            ),
            "Herdr predecessor staging route",
        )
        try:
            self._effect(
                (
                    self.binary,
                    "agent",
                    "rename",
                    str(successor["endpoint"]),
                    str(canonical),
                ),
                "Herdr successor canonical route",
            )
        except Exception:
            self._effect(
                (
                    self.binary,
                    "agent",
                    "rename",
                    str(predecessor["endpoint"]),
                    str(canonical),
                ),
                "Herdr predecessor route rollback",
            )
            raise
        after = {
            str(item.get("pane_id")): str(item.get("name"))
            for item in self.discover()
        }
        if (
            after.get(str(predecessor["endpoint"])) != predecessor_staging
            or after.get(str(successor["endpoint"])) != canonical
        ):
            raise StorageRefusal(
                "runtime_replacement_route_unverified",
                "replacement routes did not verify after promotion",
            )
        return {
            "schema": "league.runtime-replacement-route.v1",
            "verified": True,
            "operation_id": operation_id,
            "predecessor_agent_id": predecessor.get("agent_id"),
            "successor_agent_id": successor.get("agent_id"),
            "canonical_routing_name": canonical,
            "predecessor_staging_routing_name": predecessor_staging,
            "successor_previous_routing_name": staging,
            "predecessor_endpoint": predecessor.get("endpoint"),
            "successor_endpoint": successor.get("endpoint"),
        }

    def replacement_route_rollback(self, **inputs: Any) -> Mapping[str, Any]:
        route = dict(inputs.get("route_receipt") or {})
        if route.get("verified") is not True:
            raise StorageRefusal(
                "runtime_replacement_route_rollback_unverified",
                "route rollback has no exact swap receipt",
            )
        inventory = {
            str(item.get("pane_id")): str(item.get("name"))
            for item in self.discover()
        }
        predecessor_endpoint = str(route.get("predecessor_endpoint", ""))
        successor_endpoint = str(route.get("successor_endpoint", ""))
        canonical = str(route.get("canonical_routing_name", ""))
        predecessor_staging = str(route.get("predecessor_staging_routing_name", ""))
        successor_staging = str(route.get("successor_previous_routing_name", ""))
        if inventory.get(successor_endpoint) == canonical:
            self._effect(
                (self.binary, "agent", "rename", successor_endpoint, successor_staging),
                "Herdr successor route rollback",
            )
        if inventory.get(predecessor_endpoint) == predecessor_staging:
            self._effect(
                (self.binary, "agent", "rename", predecessor_endpoint, canonical),
                "Herdr predecessor route restoration",
            )
        after = {
            str(item.get("pane_id")): str(item.get("name"))
            for item in self.discover()
        }
        if (
            after.get(predecessor_endpoint) != canonical
            or after.get(successor_endpoint) != successor_staging
        ):
            raise StorageRefusal(
                "runtime_replacement_route_rollback_unverified",
                "replacement route rollback did not verify",
            )
        return {
            "schema": "league.runtime-replacement-route-rollback.v1",
            "verified": True,
            "operation_id": route.get("operation_id"),
            "predecessor_authoritative": True,
            "successor_staged": True,
        }

    def replacement_retire(self, **inputs: Any) -> Mapping[str, Any]:
        target = dict(inputs.get("target") or {})
        verification = dict(inputs.get("verification") or {})
        operation_id = inputs.get("operation_id")
        exit_prompt = inputs.get("exit_prompt")
        if (
            verification.get("verified") is not True
            or not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(exit_prompt, str)
            or not exit_prompt
        ):
            raise StorageRefusal(
                "runtime_replacement_retirement_unverified",
                "predecessor retirement lacks exact verification",
            )
        self.delivery(str(target["routing_name"]), exit_prompt, wait=True)
        endpoint = RestoredEndpoint(
            str(operation_id),
            str(verification["workspace_id"]),
            str(verification["tab_id"]),
            str(verification["pane_id"]),
            str(verification["terminal_id"]),
        )
        self.close(endpoint, placement="tab")
        return {
            "schema": "league.runtime-replacement-retirement.v1",
            "verified": True,
            "operation_id": operation_id,
            "agent_id": target.get("agent_id"),
            "runtime_instance_id": target.get("runtime_instance_id"),
            "session_ref": target.get("session_ref"),
            "endpoint": target.get("endpoint"),
            "runtime_generation": target.get("runtime_generation"),
            "state": "retired",
        }

    def close(
        self, endpoint: RestoredEndpoint, *, placement: str = "pane"
    ) -> dict[str, Any]:
        if placement not in {"pane", "tab"}:
            raise StorageRefusal(
                "multiplexer_close_invalid", "close placement is unsupported"
            )
        arguments = (
            (self.binary, "tab", "close", endpoint.tab_id)
            if placement == "tab"
            else (self.binary, "pane", "close", endpoint.pane_id)
        )
        self._effect(
            arguments,
            "Herdr placement close",
        )
        if any(item.get("pane_id") == endpoint.pane_id for item in self.discover()):
            raise StorageRefusal(
                "multiplexer_close_unverified", "Herdr pane remained in agent inventory"
            )
        return {
            "schema": "league.multiplexer-close.v1",
            "pane_id": endpoint.pane_id,
            "terminal_id": endpoint.terminal_id,
            "placement": placement,
            "closed": True,
        }

    @staticmethod
    def _exact_presentation(
        presentation: Mapping[str, Any], observation: Mapping[str, Any], *, minimum_sequence: int
    ) -> bool:
        agent = observation["agent"]
        tokens = agent.get("tokens")
        titles = (agent.get("terminal_title"), agent.get("terminal_title_stripped"))
        return bool(
            agent.get("metadata_source") == presentation["metadata_source"]
            and agent.get("display_agent") == presentation["provider_kind"]
            and isinstance(tokens, Mapping)
            and all(tokens.get(key) == value for key, value in presentation["tokens"].items())
            and all(
                title
                in {
                    presentation["title"],
                    f"{presentation['title']} | {presentation['agent_adapter_kind']}",
                }
                for title in titles
            )
            and observation["state_change_seq"] >= minimum_sequence
        )

    def metadata(
        self,
        presentation: Mapping[str, Any],
        endpoint: RestoredEndpoint,
        first_sequence: int,
    ) -> dict[str, Any]:
        before = self.inspect_restored(presentation, endpoint)
        if (
            before.get("session_ref") != presentation.get("session_ref")
            or before.get("session_source") != presentation.get("applies_to_source")
        ):
            raise StorageRefusal(
                "multiplexer_metadata_session_mismatch",
                "display metadata target is not the exact native agent session",
            )
        token_items = sorted(presentation["tokens"].items())
        batches = [
            token_items[index:index + MAX_TOKENS_PER_REPORT]
            for index in range(0, len(token_items), MAX_TOKENS_PER_REPORT)
        ]
        final_sequence = first_sequence + len(batches) - 1
        already_exact = self._exact_presentation(
            presentation, before, minimum_sequence=0
        )
        if not already_exact and first_sequence <= int(before["state_change_seq"]):
            raise StorageRefusal(
                "multiplexer_metadata_sequence_stale",
                "display metadata sequence does not advance the current source",
            )
        if not already_exact:
            for offset, batch in enumerate(batches):
                arguments = [
                    self.binary, "pane", "report-metadata", endpoint.pane_id,
                    "--source", str(presentation["metadata_source"]),
                    "--applies-to-source", str(presentation["applies_to_source"]),
                    "--agent", str(presentation["agent_adapter_kind"]),
                    "--display-agent", str(presentation["provider_kind"]),
                    "--title", str(presentation["title"]),
                    "--seq", str(first_sequence + offset),
                ]
                for key, value in batch:
                    arguments.extend(("--token", f"{key}={value}"))
                self._effect(tuple(arguments), "Herdr asynchronous display metadata replay")
        verification_sequence = (
            int(before["state_change_seq"]) if already_exact else final_sequence
        )
        stable: list[dict[str, Any]] = []
        for _ in range(2):
            observed = self.inspect_restored(presentation, endpoint)
            if not self._exact_presentation(
                presentation, observed, minimum_sequence=verification_sequence
            ):
                raise StorageRefusal(
                    "multiplexer_metadata_unverified",
                    "durable display metadata did not verify",
                )
            stable.append(observed)
        fingerprints = [
            _digest(
                {
                    "agent": item["agent"],
                    "pane": item["pane"],
                    "process": item["process_fingerprint"],
                }
            )
            for item in stable
        ]
        if fingerprints[0] != fingerprints[1]:
            raise StorageRefusal(
                "multiplexer_metadata_unstable",
                "display metadata changed between stable readbacks",
            )
        last = stable[-1]
        return {
            "schema": "league.multiplexer-metadata-publication.v1",
            "session_ref": last["session_ref"],
            "session_source": last["session_source"],
            "workspace_id": endpoint.workspace_id,
            "tab_id": endpoint.tab_id,
            "pane_id": endpoint.pane_id,
            "terminal_id": endpoint.terminal_id,
            "process_fingerprint": last["process_fingerprint"],
            "metadata_source": presentation["metadata_source"],
            "final_sequence": int(last["state_change_seq"]),
            "stable_readbacks": 2,
            "observation_digest": fingerprints[-1],
            "replay_owner": "league_async_startup_plugin",
            "idempotent": already_exact,
        }


__all__ = ["CommandRunner", "HerdrMultiplexerAdapter", "MAX_TOKENS_PER_REPORT"]
