"""In-place creation of one canonical Shotcaller from the calling Codex runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .sqlite_callsign_ops import digest
from .storage import Storage, StorageRefusal
from .visible_launch import CommandRunner, SubprocessRunner


THREAD_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
LIVE_STATUSES = {"active", "blocked", "idle", "waiting", "working"}


@dataclass(frozen=True)
class ShotcallerBootstrapSpec:
    assignment_id: str
    agent_id: str
    runtime_instance_id: str
    thread_id: str
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShotcallerBootstrapOptions:
    workspace_id: str
    tab_id: str
    pane_id: str
    worktree: str


def _result(completed: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    try:
        envelope = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageRefusal(
            "shotcaller_identity_unverified", f"{label} returned malformed JSON"
        ) from exc
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if completed.returncode != 0 or not isinstance(result, dict):
        raise StorageRefusal(
            "shotcaller_identity_unverified", f"{label} did not verify"
        )
    return result


def _session(agent: Mapping[str, Any]) -> str | None:
    value = agent.get("agent_session")
    session = value.get("value") if isinstance(value, Mapping) else None
    return str(session) if isinstance(session, str) else None


class HerdrShotcallerBootstrapAdapter:
    """Inspect and annotate only the calling pane; never create layout or process."""

    def __init__(
        self,
        options: ShotcallerBootstrapOptions,
        runner: CommandRunner | None = None,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.options = options
        self.runner = runner or SubprocessRunner()
        self.environment = dict(environment or os.environ)
        self._observed: dict[str, Any] | None = None
        worktree = Path(options.worktree)
        if (
            self.environment.get("HERDR_ENV") != "1"
            or self.environment.get("HERDR_WORKSPACE_ID") != options.workspace_id
            or self.environment.get("HERDR_TAB_ID") != options.tab_id
            or self.environment.get("HERDR_PANE_ID") != options.pane_id
            or not worktree.is_absolute()
            or not worktree.is_dir()
            or worktree.is_symlink()
        ):
            raise StorageRefusal(
                "shotcaller_identity_unverified",
                "Shotcaller creation requires the exact current Herdr pane and worktree",
            )

    def _run(
        self, arguments: tuple[str, ...], label: str, *, silent: bool = False
    ) -> dict[str, Any]:
        completed = self.runner.run(arguments, timeout_seconds=30)
        if silent and completed.returncode == 0 and not completed.stdout and not completed.stderr:
            return {}
        return _result(completed, label)

    def _current(self) -> tuple[dict[str, Any], dict[str, Any]]:
        pane_result = self._run(
            ("herdr", "pane", "current", "--current"), "current Herdr pane"
        )
        pane = pane_result.get("pane")
        inventory = self._run(("herdr", "agent", "list"), "Herdr agent inventory")
        agents = inventory.get("agents")
        if not isinstance(pane, Mapping) or not isinstance(agents, list):
            raise StorageRefusal(
                "shotcaller_identity_unverified", "current Herdr identity is incomplete"
            )
        matches = [
            dict(item)
            for item in agents
            if isinstance(item, Mapping) and item.get("pane_id") == self.options.pane_id
        ]
        if len(matches) != 1:
            raise StorageRefusal(
                "shotcaller_identity_unverified",
                "current pane does not contain exactly one visible Codex runtime",
            )
        return dict(pane), matches[0]

    def _exact(
        self,
        spec: ShotcallerBootstrapSpec,
        pane: Mapping[str, Any],
        agent: Mapping[str, Any],
    ) -> bool:
        terminal_id = agent.get("terminal_id")
        sequence = agent.get("state_change_seq")
        worktree = str(Path(self.options.worktree).resolve())
        return bool(
            agent.get("agent") == "codex"
            and agent.get("workspace_id") == self.options.workspace_id
            and agent.get("tab_id") == self.options.tab_id
            and agent.get("pane_id") == self.options.pane_id
            and pane.get("workspace_id") == self.options.workspace_id
            and pane.get("tab_id") == self.options.tab_id
            and pane.get("pane_id") == self.options.pane_id
            and pane.get("terminal_id") == terminal_id
            and agent.get("cwd") == worktree
            and agent.get("foreground_cwd") == worktree
            and _session(agent) == spec.thread_id
            and agent.get("agent_status") in LIVE_STATUSES
            and isinstance(terminal_id, str)
            and terminal_id
            and (
                self._observed is None
                or terminal_id == self._observed["terminal_id"]
            )
            and isinstance(sequence, int)
            and sequence >= 0
        )

    def inspect(
        self, spec: ShotcallerBootstrapSpec, *, expected_alias: str | None = None
    ) -> dict[str, Any]:
        if not THREAD_UUID.fullmatch(spec.thread_id):
            raise StorageRefusal(
                "shotcaller_identity_unverified", "calling Codex thread identity is invalid"
            )
        pane, agent = self._current()
        tokens = agent.get("tokens")
        routing_exact = (
            agent.get("name") in {None, ""}
            if expected_alias is None
            else agent.get("name") == expected_alias
        )
        if not self._exact(spec, pane, agent) or not routing_exact:
            raise StorageRefusal(
                "shotcaller_identity_unverified",
                "calling Codex must be exact, current, and not already routing-bound",
            )
        self._observed = {
            "terminal_id": str(agent["terminal_id"]),
            "state_change_seq": int(agent["state_change_seq"]),
            "tokens": dict(tokens) if isinstance(tokens, Mapping) else {},
            "title": agent.get("terminal_title_stripped", agent.get("terminal_title", "")),
            "endpoint_generation": "herdr:"
            + hashlib.sha256(
                f"{agent['terminal_id']}\0{spec.thread_id}".encode("utf-8")
            ).hexdigest()[:24],
        }
        return dict(self._observed)

    def publish(self, spec: ShotcallerBootstrapSpec, callsign: str) -> dict[str, Any]:
        if self._observed is None:
            raise StorageRefusal(
                "shotcaller_identity_unverified", "calling identity was not inspected"
            )
        alias = callsign.lower()
        self._run(
            ("herdr", "agent", "rename", self.options.pane_id, alias),
            "Herdr Shotcaller routing rename",
        )
        self._run(
            (
                "herdr",
                "pane",
                "report-metadata",
                self.options.pane_id,
                "--source",
                "league-shotcaller-" + hashlib.sha256(spec.assignment_id.encode()).hexdigest()[:16],
                "--agent",
                "codex",
                "--display-agent",
                "codex",
                "--title",
                callsign,
                "--token",
                f"sidebar_name={callsign}",
                "--token",
                f"thread_title={callsign}",
                "--seq",
                str(self._observed["state_change_seq"] + 1),
            ),
            "Herdr Shotcaller metadata",
            silent=True,
        )
        pane, agent = self._current()
        tokens = agent.get("tokens")
        if not self._published_exact(spec, callsign, pane, agent):
            raise StorageRefusal(
                "shotcaller_metadata_unverified",
                "same-pane Shotcaller routing metadata did not verify",
            )
        return self.current_receipt(spec, callsign)

    def _published_exact(
        self,
        spec: ShotcallerBootstrapSpec,
        callsign: str,
        pane: Mapping[str, Any],
        agent: Mapping[str, Any],
    ) -> bool:
        tokens = agent.get("tokens")
        return bool(
            self._exact(spec, pane, agent)
            and agent.get("name") == callsign.lower()
            and isinstance(tokens, Mapping)
            and tokens.get("sidebar_name") == callsign
            and tokens.get("thread_title") == callsign
        )

    def current_receipt(
        self, spec: ShotcallerBootstrapSpec, callsign: str
    ) -> dict[str, Any]:
        if self._observed is None:
            raise StorageRefusal(
                "shotcaller_identity_unverified", "calling identity was not inspected"
            )
        pane, agent = self._current()
        if not self._published_exact(spec, callsign, pane, agent):
            raise StorageRefusal(
                "shotcaller_metadata_unverified",
                "same-pane Shotcaller route and display identity did not verify",
            )
        return {
            "schema": "league.runtime-acceptance.v1",
            "verified": True,
            "assignment_id": spec.assignment_id,
            "agent_id": spec.agent_id,
            "callsign": callsign,
            "runtime_instance_id": spec.runtime_instance_id,
            "harness_kind": "codex-thread",
            "backend_kind": "herdr",
            "session_identity": spec.thread_id,
            "endpoint_identity": self.options.pane_id,
            "endpoint_generation": self._observed["endpoint_generation"],
            "routing_name": callsign.lower(),
            "display_agent": "codex",
            "capabilities": list(spec.capabilities),
        }

    def restore(self) -> bool:
        if self._observed is None:
            return True
        previous_sidebar = self._observed["tokens"].get("sidebar_name", "")
        previous_thread_title = self._observed["tokens"].get("thread_title", "")
        previous_title = self._observed["title"] or ""
        try:
            self._run(
                ("herdr", "agent", "rename", self.options.pane_id, "--clear"),
                "Herdr Shotcaller routing rollback",
            )
            self._run(
                (
                    "herdr",
                    "pane",
                    "report-metadata",
                    self.options.pane_id,
                    "--source",
                    "league-shotcaller-rollback",
                    "--agent",
                    "codex",
                    "--display-agent",
                    "codex",
                    "--title",
                    str(previous_title),
                    "--token",
                    f"sidebar_name={previous_sidebar}",
                    "--token",
                    f"thread_title={previous_thread_title}",
                    "--seq",
                    str(self._observed["state_change_seq"] + 2),
                ),
                "Herdr Shotcaller metadata rollback",
                silent=True,
            )
            pane, agent = self._current()
            tokens = agent.get("tokens")
            observed_sidebar = tokens.get("sidebar_name", "") if isinstance(tokens, Mapping) else ""
            observed_thread = tokens.get("thread_title", "") if isinstance(tokens, Mapping) else ""
            title = agent.get("terminal_title_stripped", agent.get("terminal_title", "")) or ""
            return bool(
                self._exact_placeholder(pane, agent)
                and observed_sidebar == previous_sidebar
                and observed_thread == previous_thread_title
                and title == previous_title
                and agent.get("name") in {None, ""}
            )
        except StorageRefusal:
            return False

    def _exact_placeholder(
        self, pane: Mapping[str, Any], agent: Mapping[str, Any]
    ) -> bool:
        if self._observed is None:
            return False
        worktree = str(Path(self.options.worktree).resolve())
        return bool(
            agent.get("agent") == "codex"
            and agent.get("agent_status") in LIVE_STATUSES
            and agent.get("workspace_id") == self.options.workspace_id
            and agent.get("tab_id") == self.options.tab_id
            and agent.get("pane_id") == self.options.pane_id
            and pane.get("workspace_id") == self.options.workspace_id
            and pane.get("tab_id") == self.options.tab_id
            and pane.get("pane_id") == self.options.pane_id
            and agent.get("terminal_id") == self._observed["terminal_id"]
            and pane.get("terminal_id") == self._observed["terminal_id"]
            and agent.get("cwd") == worktree
            and agent.get("foreground_cwd") == worktree
            and _session(agent) is not None
            and "herdr:"
            + hashlib.sha256(
                f"{self._observed['terminal_id']}\0{_session(agent)}".encode("utf-8")
            ).hexdigest()[:24]
            == self._observed["endpoint_generation"]
        )


class ShotcallerBootstrapService:
    """Reserve, annotate, activate, and receipt one existing Codex endpoint."""

    def __init__(self, store: Storage, adapter: HerdrShotcallerBootstrapAdapter, clock: Any) -> None:
        self.store = store
        self.adapter = adapter
        self.clock = clock

    def bootstrap(self, spec: ShotcallerBootstrapSpec, *, fault: Any = None) -> dict[str, Any]:
        existing = self.store.callsign_assignment_status(spec.assignment_id)
        completed = self.store.shotcaller_bootstrap_status(spec.assignment_id)
        expected_alias = (
            str(existing["callsign"]).lower()
            if existing is not None and existing["state"] in {"reserved", "active"}
            else None
        )
        observed = self.adapter.inspect(spec, expected_alias=expected_alias)
        if completed is not None:
            return completed
        at = self.clock.now()
        reserved = self.store.allocate_callsign(
            spec.assignment_id,
            spec.agent_id,
            "shotcaller",
            "shotcaller",
            spec.agent_id,
            spec.capabilities,
            at,
        )
        published = False
        try:
            if existing is not None and existing["state"] == "active":
                receipt = self.adapter.current_receipt(spec, str(reserved["callsign"]))
            else:
                published = True
                receipt = self.adapter.publish(spec, str(reserved["callsign"]))
            return self.store.record_shotcaller_bootstrap(
                spec.assignment_id, 1, receipt, at, fault=fault
            )
        except Exception as exc:
            if existing is not None and existing["state"] == "active":
                raise
            restored = self.adapter.restore() if published else True
            failure_digest = digest(
                {
                    "assignment_id": spec.assignment_id,
                    "thread_id": spec.thread_id,
                    "identity_digest": digest(observed),
                    "failure_class": getattr(exc, "code", type(exc).__name__),
                    "metadata_restored": restored,
                }
            )
            canonical_restored = False
            try:
                rolled_back = self.store.rollback_callsign(
                    spec.assignment_id, 1, failure_digest, at
                )
                canonical_restored = rolled_back["state"] == "rolled_back"
            except StorageRefusal:
                canonical_restored = False
            if not restored or not canonical_restored:
                raise StorageRefusal(
                    "shotcaller_creation_cleanup_unproven",
                    "Shotcaller routing or canonical cleanup could not be verified",
                ) from exc
            raise
