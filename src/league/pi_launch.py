"""Explicit Pi runtime plus Cursor/Codex provider launch through Herdr."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .request_services import AssignmentSpec, LaunchAdapterError
from .storage_types import StorageRefusal
from .visible_launch import MAX_CONTEXT_BYTES, CommandRunner, SubprocessRunner
from .worktree import verified_worktree_repository_root


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _result(completed: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal("launch_adapter_failed", f"{label} returned malformed JSON") from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    if completed.returncode != 0 or not isinstance(result, dict):
        raise StorageRefusal("launch_adapter_failed", f"{label} refused or failed")
    return result


def _pi_provider(provider_kind: str) -> str:
    if provider_kind == "cursor":
        return "cursor"
    if provider_kind == "codex":
        return "openai-codex"
    raise StorageRefusal("provider_launch_descriptor_invalid", "Pi provider must be cursor or codex")


def _session_arguments(descriptor: Mapping[str, Any], *, restart: bool) -> tuple[str, ...]:
    if restart:
        session_path = descriptor.get("session_path") or descriptor.get("requested_session_path")
        if not isinstance(session_path, str) or not Path(session_path).is_absolute():
            raise StorageRefusal("provider_restart_unavailable", "Pi restart requires the exact session path")
        return ("--session", session_path)
    mode = descriptor["launch_mode"]
    if mode == "create":
        return ("--session-id", str(descriptor["requested_session_id"]))
    if mode == "fork":
        return ("--fork", str(descriptor["parent_session_path"]))
    return ("--session", str(descriptor["requested_session_path"]))


def pi_start_arguments(
    descriptor: Mapping[str, Any], *, restart: bool = False
) -> tuple[str, ...]:
    release_root = Path(str(descriptor["release_root"]))
    integration = release_root / "integrations" / "pi" / "league-runtime.ts"
    profile = release_root / "integrations" / "pi" / "league-bash.sb"
    watcher = release_root / "bin" / "agent-watcher"
    if any(not path.is_file() or path.is_symlink() for path in (integration, profile, watcher)):
        raise StorageRefusal("launch_integration_unavailable", "Pi lifecycle files are missing from the exact League release")
    digest = descriptor.get("descriptor_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise StorageRefusal("provider_launch_descriptor_invalid", "Pi launch requires the durable descriptor digest")
    metadata = {
        "pane-id": str(descriptor.get("pane_id", "")),
        "state-root": str(descriptor["state_root"]),
        "watcher-command": str(watcher),
        "worktree": str(descriptor["cwd"]),
        "sandbox-profile": str(profile),
        "runtime-kind": "pi",
        "provider-kind": str(descriptor["provider_kind"]),
        "role": str(descriptor["role"]),
        "placement": str(descriptor["placement"]),
        "callsign": str(descriptor["callsign"]),
        "project-code": str(descriptor["project_code"]),
        "task-label": str(descriptor["task_label"]),
        "routing-alias": str(descriptor["routing_name"]),
        "descriptor-digest": digest,
    }
    metadata_arguments = tuple(
        item
        for key, value in metadata.items()
        for item in (f"--league-{key}", value)
    )
    if not metadata["pane-id"]:
        raise StorageRefusal("provider_launch_descriptor_invalid", "Pi launch requires the exact Herdr pane ID")
    return (
        "--approve",
        "--provider",
        _pi_provider(str(descriptor["provider_kind"])),
        "--model",
        str(descriptor["model"]),
        "--thinking",
        str(descriptor["effort"]),
        "--extension",
        str(integration),
        *metadata_arguments,
        *_session_arguments(descriptor, restart=restart),
    )


def pi_launch_environment(descriptor: Mapping[str, Any], digest: str) -> tuple[str, ...]:
    profile = Path(str(descriptor["release_root"])) / "integrations" / "pi" / "league-bash.sb"
    watcher = Path(str(descriptor["release_root"])) / "bin" / "agent-watcher"
    values = {
        "LEAGUE_STATE_ROOT": str(descriptor["state_root"]),
        "LEAGUE_WATCHER_COMMAND": str(watcher),
        "LEAGUE_WORKTREE": str(descriptor["cwd"]),
        "LEAGUE_PI_SANDBOX_PROFILE": str(profile),
        "LEAGUE_RUNTIME_KIND": "pi",
        "LEAGUE_PROVIDER_KIND": str(descriptor["provider_kind"]),
        "LEAGUE_LAUNCH_ROLE": str(descriptor["role"]),
        "LEAGUE_LAUNCH_PLACEMENT": str(descriptor["placement"]),
        "LEAGUE_CALLSIGN": str(descriptor["callsign"]),
        "LEAGUE_PROJECT_CODE": str(descriptor["project_code"]),
        "LEAGUE_TASK_LABEL": str(descriptor["task_label"]),
        "LEAGUE_ROUTING_ALIAS": str(descriptor["routing_name"]),
        "LEAGUE_LAUNCH_DESCRIPTOR_DIGEST": digest,
    }
    return tuple(item for key, value in values.items() for item in ("--env", f"{key}={value}"))


class HerdrPiLaunchAdapter:
    """Launch and verify one exact Pi runtime without a local wrapper script."""

    def __init__(
        self,
        store: Any,
        descriptor: Mapping[str, Any],
        *,
        at: str,
        runner: CommandRunner | None = None,
        startup_timeout_ms: int = 120_000,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.descriptor = dict(descriptor)
        self.at = at
        self.runner = runner or SubprocessRunner()
        self.startup_timeout_ms = startup_timeout_ms
        self.environment = dict(environment or os.environ)
        self._created: dict[str, str] | None = None
        self._receipt: dict[str, Any] | None = None
        if self.environment.get("HERDR_ENV") != "1":
            raise StorageRefusal("launch_scope_invalid", "visible Pi launch requires the current Herdr session")

    def _command(self, arguments: Sequence[str], label: str, timeout: int = 30) -> dict[str, Any]:
        return _result(self.runner.run(arguments, timeout_seconds=timeout), label)

    def _effect_command(self, arguments: Sequence[str], label: str, timeout: int = 30) -> None:
        completed = self.runner.run(arguments, timeout_seconds=timeout)
        if completed.returncode != 0:
            raise StorageRefusal("launch_adapter_failed", f"{label} refused or failed")

    def _agent(self) -> dict[str, Any]:
        return dict(
            self._command(
                ("herdr", "agent", "get", str(self.descriptor["routing_name"])),
                "Herdr Pi inspection",
            ).get("agent", {})
        )

    def _pane(self, pane_id: str) -> dict[str, Any]:
        return dict(
            self._command(("herdr", "pane", "get", pane_id), "Herdr Pi pane inspection").get("pane", {})
        )

    def _process_exact(self, pane_id: str, *, restart: bool) -> None:
        info = self._command(
            ("herdr", "pane", "process-info", "--pane", pane_id),
            "Herdr Pi process inspection",
        ).get("process_info")
        processes = info.get("foreground_processes") if isinstance(info, Mapping) else None
        if not isinstance(processes, list):
            raise StorageRefusal("launch_identity_unverified", "Pi foreground process inventory is unavailable")
        if len(processes) != 1 or not isinstance(processes[0], Mapping):
            raise StorageRefusal("launch_identity_unverified", "Pi foreground process is not the exact explicit launch")
        process = processes[0]
        if process.get("cwd") != self.descriptor["cwd"] or process.get("argv0") not in {None, "pi"}:
            raise StorageRefusal("launch_identity_unverified", "Pi foreground process is not bound to the exact worktree")
        argv = process.get("argv")
        if argv is not None:
            if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
                raise StorageRefusal("launch_identity_unverified", "Pi process arguments are ambiguous")
            expected = list(pi_start_arguments(self.descriptor, restart=restart))
            if not any(argv[index : index + len(expected)] == expected for index in range(len(argv) - len(expected) + 1)):
                raise StorageRefusal("launch_identity_unverified", "Pi process arguments differ from the durable launch descriptor")
        elif process.get("argv0") != "pi":
            raise StorageRefusal("launch_identity_unverified", "Pi process identity is ambiguous")

    def _report_resume_state(self, endpoint: Mapping[str, str]) -> None:
        session_id = str(self.descriptor["session_id"])
        session_path = str(self.descriptor["session_path"])
        digest = str(self.descriptor["descriptor_digest"])
        source = "league:pi-launch:" + digest[:16]
        sequence = str(time.time_ns() // 1000)
        values = {
            "launch_runtime_kind": "pi",
            "launch_provider_kind": str(self.descriptor["provider_kind"]),
            "launch_role": str(self.descriptor["role"]),
            "launch_placement": str(self.descriptor["placement"]),
            "launch_callsign": str(self.descriptor["callsign"]),
            "launch_project_code": str(self.descriptor["project_code"]),
            "launch_task_label": str(self.descriptor["task_label"]),
            "launch_routing_alias": str(self.descriptor["routing_name"]),
            "launch_session_id": session_id,
            "launch_session_path_digest": hashlib.sha256(session_path.encode()).hexdigest(),
            "launch_descriptor_sha256": digest,
            "launch_activation_phase": "session_started",
        }
        parent_path = self.descriptor.get("parent_session_path")
        if isinstance(parent_path, str):
            values["launch_parent_digest"] = hashlib.sha256(parent_path.encode()).hexdigest()
        title = (
            str(self.descriptor["callsign"])
            if self.descriptor["role"] == "shotcaller"
            else f"{self.descriptor['callsign']} · {self.descriptor['project_code']}|{self.descriptor['task_label']}"
        )
        metadata_arguments = [
            "herdr", "pane", "report-metadata", endpoint["pane_id"],
            "--source", source,
            "--agent", "pi", "--display-agent", str(self.descriptor["provider_kind"]),
            "--title", title, "--seq", sequence,
        ]
        for key, value in values.items():
            metadata_arguments.extend(("--token", f"{key}={value}"))
        self._effect_command(tuple(metadata_arguments), "Herdr Pi exact resume metadata report")
        self._effect_command(
            ("herdr", "pane", "rename", endpoint["pane_id"], title),
            "Herdr Pi exact resume canonical label",
        )

    def _observation(self, endpoint: Mapping[str, str], *, restart: bool) -> dict[str, Any]:
        deadline = time.monotonic() + min(self.startup_timeout_ms / 1000, 15.0)
        stable_fingerprint: str | None = None
        stable_observation: dict[str, Any] | None = None
        expected_thread_title = (
            str(self.descriptor["callsign"])
            if self.descriptor["role"] == "shotcaller"
            else f"{self.descriptor['callsign']} · {self.descriptor['project_code']}|{self.descriptor['task_label']}"
        )
        while time.monotonic() < deadline:
            agent = self._agent()
            pane = self._pane(endpoint["pane_id"])
            tokens = agent.get("tokens")
            if isinstance(tokens, Mapping):
                session = agent.get("agent_session")
                session_id = tokens.get("launch_session_id")
                session_path = (
                    session.get("value")
                    if isinstance(session, Mapping)
                    else self.descriptor.get("session_path") if restart else None
                )
                session_exact = (
                    isinstance(session, Mapping)
                    and session.get("agent") == "pi"
                    and session.get("kind") == "path"
                    and session.get("source") == "herdr:pi"
                ) or (restart and session is None and session_path == self.descriptor.get("session_path"))
                expected_parent_path = self.descriptor.get("parent_session_path")
                parent_digest = tokens.get("launch_parent_digest")
                parent_exact = (
                    expected_parent_path is None and parent_digest is None
                ) or (
                    isinstance(expected_parent_path, str)
                    and parent_digest == hashlib.sha256(expected_parent_path.encode()).hexdigest()
                )
                exact = (
                    agent.get("name") == self.descriptor["routing_name"]
                    and agent.get("agent") == "pi"
                    and agent.get("display_agent") == self.descriptor["provider_kind"]
                    and agent.get("workspace_id") == self.descriptor["workspace_id"]
                    and agent.get("tab_id") == endpoint["tab_id"]
                    and agent.get("pane_id") == endpoint["pane_id"]
                    and agent.get("terminal_id") == endpoint["terminal_id"]
                    and agent.get("cwd") == self.descriptor["cwd"]
                    and agent.get("foreground_cwd") == self.descriptor["cwd"]
                    and pane.get("label") == expected_thread_title
                    and tokens.get("launch_runtime_kind") == "pi"
                    and tokens.get("launch_provider_kind") == self.descriptor["provider_kind"]
                    and tokens.get("launch_role") == self.descriptor["role"]
                    and tokens.get("launch_placement") == self.descriptor["placement"]
                    and tokens.get("launch_callsign") == self.descriptor["callsign"]
                    and tokens.get("launch_project_code") == self.descriptor["project_code"]
                    and tokens.get("launch_task_label") == self.descriptor["task_label"]
                    and tokens.get("launch_routing_alias") == self.descriptor["routing_name"]
                    and tokens.get("launch_descriptor_sha256") == self.descriptor["descriptor_digest"]
                    and tokens.get("launch_activation_phase") == "session_started"
                    and isinstance(session_id, str)
                    and isinstance(session_path, str)
                    and Path(session_path).is_absolute()
                    and session_exact
                    and tokens.get("launch_session_path_digest") == hashlib.sha256(session_path.encode()).hexdigest()
                    and parent_exact
                )
                if exact:
                    self._process_exact(endpoint["pane_id"], restart=restart)
                    observation = {
                        "schema": "league.pi-launch-observation.v1",
                        "runtime_kind": "pi",
                        "provider_kind": self.descriptor["provider_kind"],
                        "session_id": session_id,
                        "session_path": session_path,
                        "parent_session_path": expected_parent_path,
                        "cwd": self.descriptor["cwd"],
                        "role": self.descriptor["role"],
                        "placement": self.descriptor["placement"],
                        "callsign": self.descriptor["callsign"],
                        "project_code": self.descriptor["project_code"],
                        "task_label": self.descriptor["task_label"],
                        "routing_name": self.descriptor["routing_name"],
                        "workspace_id": self.descriptor["workspace_id"],
                        "tab_id": endpoint["tab_id"],
                        "pane_id": endpoint["pane_id"],
                        "terminal_id": endpoint["terminal_id"],
                    }
                    fingerprint = _digest(
                        {
                            "observation": observation,
                            "terminal_title": agent.get("terminal_title"),
                            "pane_label": pane.get("label"),
                            "tokens": dict(tokens),
                        }
                    )
                    if fingerprint == stable_fingerprint and stable_observation == observation:
                        return observation
                    stable_fingerprint = fingerprint
                    stable_observation = observation
                    time.sleep(0.25)
                    continue
            stable_fingerprint = None
            stable_observation = None
            time.sleep(0.1)
        raise StorageRefusal("launch_identity_unverified", "Pi did not publish exact session and launch metadata")

    def _allocate(self) -> dict[str, str]:
        env = pi_launch_environment(self.descriptor, str(self.descriptor["descriptor_digest"]))
        if self.descriptor["role"] == "champion":
            result = self._command(
                (
                    "herdr", "tab", "create", "--workspace", str(self.descriptor["workspace_id"]),
                    "--cwd", str(self.descriptor["cwd"]), "--label",
                    f"{self.descriptor['callsign']} · {self.descriptor['project_code']}|{self.descriptor['task_label']}",
                    *env, "--no-focus",
                ),
                "Herdr Pi Champion tab creation",
            )
            tab, pane = result.get("tab"), result.get("root_pane")
        else:
            result = self._command(
                (
                    "herdr", "pane", "split", str(self.descriptor["creator_pane_id"]),
                    "--direction", "right", "--cwd", str(self.descriptor["cwd"]),
                    *env, "--no-focus",
                ),
                "Herdr Pi Shotcaller sibling creation",
            )
            pane = result.get("pane")
            tab = {"tab_id": result.get("tab_id")}
        if not isinstance(tab, Mapping) or not isinstance(pane, Mapping):
            raise StorageRefusal("launch_identity_unverified", "Herdr Pi placement receipt is incomplete")
        endpoint = {
            "tab_id": str(tab.get("tab_id", "")),
            "pane_id": str(pane.get("pane_id", "")),
            "terminal_id": str(pane.get("terminal_id", "")),
        }
        if not all(endpoint.values()):
            raise StorageRefusal("launch_identity_unverified", "Herdr Pi endpoint identity is incomplete")
        label = (
            str(self.descriptor["callsign"])
            if self.descriptor["role"] == "shotcaller"
            else f"{self.descriptor['callsign']} · {self.descriptor['project_code']}|{self.descriptor['task_label']}"
        )
        self._effect_command(("herdr", "pane", "rename", endpoint["pane_id"], label), "Herdr Pi canonical pane label")
        return endpoint

    def launch(self, spec: AssignmentSpec) -> dict[str, Any]:
        worktree = Path(spec.worktree)
        if not worktree.is_absolute() or not worktree.is_dir() or worktree.is_symlink() or spec.callsign is None:
            raise LaunchAdapterError("invalid_launch_worktree")
        verified_worktree_repository_root(worktree)
        self.descriptor.update(
            {
                "callsign": str(spec.callsign),
                "routing_name": str(spec.callsign).lower(),
                "cwd": str(worktree.resolve()),
                "assignment_id": spec.assignment_id,
            }
        )
        prepared = self.store.prepare_provider_launch(self.descriptor, self.at)
        self.descriptor["descriptor_digest"] = prepared["descriptor_digest"]
        try:
            if prepared["state"] == "active":
                stored = self.store.provider_launch_descriptor(str(self.descriptor["descriptor_id"]))
                if stored is None:
                    raise StorageRefusal("provider_launch_unknown", "active Pi descriptor disappeared")
                endpoint = {
                    "tab_id": str(stored["tab_id"]),
                    "pane_id": str(stored["pane_id"]),
                    "terminal_id": str(stored["terminal_id"]),
                }
                self.descriptor["pane_id"] = endpoint["pane_id"]
                observation = self._observation(endpoint, restart=False)
            else:
                endpoint = self._allocate()
                self._created = endpoint
                self.descriptor["pane_id"] = endpoint["pane_id"]
                self._command(
                    (
                        "herdr", "agent", "start", str(self.descriptor["routing_name"]), "--kind", "pi",
                        "--pane", endpoint["pane_id"], "--timeout", str(self.startup_timeout_ms), "--",
                        *pi_start_arguments(self.descriptor),
                    ),
                    "Herdr Pi start",
                    timeout=(self.startup_timeout_ms // 1000) + 10,
                )
                observation = self._observation(endpoint, restart=False)
                self.store.bind_provider_launch(
                    str(self.descriptor["descriptor_id"]), prepared["version"], observation, self.at
                )
        except Exception as exc:
            raise LaunchAdapterError(
                exc.code if isinstance(exc, StorageRefusal) else "launch_adapter_failure",
                cleanup_required=self._created is not None,
                cleanup_proven=False,
            ) from exc
        session_id = observation["session_id"]
        runtime_generation = "herdr:" + hashlib.sha256(
            f"{endpoint['terminal_id']}\0{session_id}".encode()
        ).hexdigest()[:24]
        self._receipt = {
            "verified": True,
            "assignment_id": spec.assignment_id,
            "task_id": spec.task_id,
            "champion_agent_id": spec.champion_agent_id,
            "callsign": spec.callsign,
            "runtime_instance_id": f"runtime:{spec.champion_agent_id}",
            # Herdr's provider-native Pi identity is the exact JSONL path. Keep
            # the UUID alongside it for reporting, but bind the canonical
            # runtime/cleanup identity to the path Herdr will read back.
            "thread_id": observation["session_path"],
            "session_id": session_id,
            "session_path": observation["session_path"],
            "endpoint": endpoint["pane_id"],
            "runtime_generation": runtime_generation,
            "harness_kind": "pi-thread",
            "backend_kind": "herdr",
            "routing_name": str(spec.callsign).lower(),
            "display_agent": self.descriptor["provider_kind"],
            "repository": spec.repository,
            "issue": spec.issue,
            "branch": spec.branch,
            "worktree": spec.worktree,
            "capabilities": list(spec.required_capabilities),
        }
        return dict(self._receipt)

    def deliver_context(self, receipt: Mapping[str, Any], context: str) -> dict[str, Any]:
        body = context.encode()
        if not body or len(body) > MAX_CONTEXT_BYTES:
            raise LaunchAdapterError("launch_context_invalid", cleanup_required=True)
        self._command(
            ("herdr", "agent", "prompt", str(receipt["routing_name"]), context),
            "Herdr Pi context delivery",
        )
        endpoint = self._created or {
            "tab_id": "",
            "pane_id": str(receipt["endpoint"]),
            "terminal_id": "",
        }
        observation = self._observation(endpoint, restart=False)
        return {
            "context_sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "effect_sha256": _digest({"descriptor_id": self.descriptor["descriptor_id"], "context_sha256": hashlib.sha256(body).hexdigest()}),
            "display_receipt": observation,
        }

    def verify_active_title(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        descriptor = self.store.provider_launch_descriptor(str(self.descriptor["descriptor_id"]))
        if descriptor is None or descriptor["state"] != "active":
            raise StorageRefusal("launch_title_restore_refused", "Pi launch descriptor is not active")
        self.descriptor = {
            **descriptor["descriptor"],
            "descriptor_digest": descriptor["descriptor_digest"],
            "session_id": descriptor["session_id"],
            "session_path": descriptor["session_path"],
        }
        return self._observation(
            {
                "tab_id": str(descriptor["tab_id"]),
                "pane_id": str(descriptor["pane_id"]),
                "terminal_id": str(descriptor["terminal_id"]),
            },
            restart=False,
        )

    def cleanup(self, _receipt: Mapping[str, Any] | None) -> bool:
        if self._created is None:
            return True
        target = (
            ("herdr", "tab", "close", self._created["tab_id"])
            if self.descriptor["role"] == "champion"
            else ("herdr", "pane", "close", self._created["pane_id"])
        )
        try:
            completed = self.runner.run(target, timeout_seconds=30)
        except Exception:
            return False
        if completed.returncode != 0:
            return False
        self._created = None
        return True


def resume_pi_after_restart(
    store: Any,
    *,
    descriptor_id: str,
    restart_id: str,
    pane_id: str,
    at: str,
    runner: CommandRunner | None = None,
    startup_timeout_ms: int = 120_000,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    claim = store.claim_provider_restart(descriptor_id, restart_id, pane_id, at)
    if claim["state"] == "effect_applied":
        return {**claim, "idempotent": True}
    descriptor = dict(claim["descriptor"])
    descriptor.update(
        {
            "descriptor_digest": store.provider_launch_descriptor(descriptor_id)["descriptor_digest"],
            "session_id": claim["session_id"],
            "session_path": claim["session_path"],
            "pane_id": pane_id,
        }
    )
    worktree = Path(str(descriptor["cwd"]))
    if (
        not worktree.is_absolute()
        or not worktree.is_dir()
        or worktree.is_symlink()
        or worktree.resolve() != worktree
    ):
        raise StorageRefusal(
            "launch_scope_invalid",
            "Pi restart cwd is no longer the exact authorized worktree",
        )
    verified_worktree_repository_root(worktree)
    adapter = HerdrPiLaunchAdapter(
        store,
        descriptor,
        at=at,
        runner=runner,
        startup_timeout_ms=startup_timeout_ms,
        environment=environment,
    )
    stored = store.provider_launch_descriptor(descriptor_id)
    assert stored is not None
    endpoint = {
        "tab_id": str(stored["tab_id"]),
        "pane_id": str(stored["pane_id"]),
        "terminal_id": str(stored["terminal_id"]),
    }
    try:
        observation = adapter._observation(endpoint, restart=True)
    except StorageRefusal:
        process_info = adapter._command(
            ("herdr", "pane", "process-info", "--pane", pane_id),
            "Herdr restored Pi pane inspection",
        ).get("process_info")
        processes = (
            process_info.get("foreground_processes")
            if isinstance(process_info, Mapping)
            else None
        )
        shell_pid = process_info.get("shell_pid") if isinstance(process_info, Mapping) else None
        foreground_group = (
            process_info.get("foreground_process_group_id")
            if isinstance(process_info, Mapping)
            else None
        )
        shell_only = (
            isinstance(processes, list)
            and len(processes) == 1
            and isinstance(processes[0], Mapping)
            and processes[0].get("pid") == shell_pid
            and processes[0].get("argv0") in {"zsh", "bash", "fish", "sh"}
            and processes[0].get("cwd") == descriptor["cwd"]
        )
        pi_running = (
            isinstance(processes, list)
            and len(processes) == 1
            and isinstance(processes[0], Mapping)
            and processes[0].get("argv0") == "pi"
            and processes[0].get("cwd") == descriptor["cwd"]
        )
        if pi_running:
            adapter._process_exact(pane_id, restart=True)
            adapter._report_resume_state(endpoint)
            observation = adapter._observation(endpoint, restart=True)
        elif (
            not isinstance(processes, list)
            or not isinstance(shell_pid, int)
            or foreground_group != shell_pid
            or (processes and not shell_only)
        ):
            raise StorageRefusal(
                "provider_restart_process_ambiguous",
                "restored Pi pane is not an exact available shell and cannot be resumed",
            )
        else:
            adapter._command(
                (
                    "herdr", "agent", "start", str(descriptor["routing_name"]), "--kind", "pi",
                    "--pane", pane_id, "--timeout", str(startup_timeout_ms), "--",
                    *pi_start_arguments(descriptor, restart=True),
                ),
                "Herdr Pi exact restart resume",
                timeout=(startup_timeout_ms // 1000) + 10,
            )
            adapter._report_resume_state(endpoint)
            observation = adapter._observation(endpoint, restart=True)
    receipt = {
        "schema": "league.pi-restart-receipt.v1",
        "descriptor_id": descriptor_id,
        "restart_id": restart_id,
        "pane_id": pane_id,
        "session_id": observation["session_id"],
        "session_path": observation["session_path"],
        "runtime_kind": "pi",
        "provider_kind": descriptor["provider_kind"],
    }
    return store.complete_provider_restart(
        descriptor_id, restart_id, claim["intent_digest"], receipt, at
    )


def deterministic_pi_session_id(descriptor_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"league.pi-session\0{descriptor_id}"))


__all__ = [
    "HerdrPiLaunchAdapter",
    "deterministic_pi_session_id",
    "pi_launch_environment",
    "pi_start_arguments",
    "resume_pi_after_restart",
]
