"""In-place creation of one canonical Shotcaller from the calling Codex runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
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


class _MalformedHerdrResult(StorageRefusal):
    def __init__(self, label: str) -> None:
        super().__init__(
            "shotcaller_identity_unverified", f"{label} returned malformed JSON"
        )


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
        raise _MalformedHerdrResult(label) from exc
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


def _session_source(agent: Mapping[str, Any]) -> str | None:
    value = agent.get("agent_session")
    source = value.get("source") if isinstance(value, Mapping) else None
    return str(source) if isinstance(source, str) and source else None


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
        self._restore_baseline: dict[str, Any] | None = None
        self._published_source: str | None = None
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

    def _read(self, arguments: tuple[str, ...], label: str) -> dict[str, Any]:
        """Retry only a transient malformed read; never replay a mutation."""

        for attempt in range(3):
            try:
                return self._run(arguments, label)
            except _MalformedHerdrResult:
                if attempt == 2:
                    raise
        raise AssertionError("bounded Herdr identity read retry exhausted")

    def _current(self) -> tuple[dict[str, Any], dict[str, Any]]:
        pane_result = self._read(
            ("herdr", "pane", "current", "--current"), "current Herdr pane"
        )
        pane = pane_result.get("pane")
        inventory = self._read(("herdr", "agent", "list"), "Herdr agent inventory")
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
        self,
        spec: ShotcallerBootstrapSpec,
        *,
        expected_alias: str | None = None,
        allow_unpublished: bool = False,
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
            or (allow_unpublished and agent.get("name") in {None, ""})
        )
        if (
            not self._exact(spec, pane, agent)
            or not routing_exact
            or not isinstance(tokens, Mapping)
            or not isinstance(agent.get("metadata_source"), str)
            or not agent["metadata_source"]
        ):
            raise StorageRefusal(
                "shotcaller_identity_unverified",
                "calling Codex must be exact, current, and not already routing-bound",
            )
        self._observed = {
            "terminal_id": str(agent["terminal_id"]),
            "state_change_seq": int(agent["state_change_seq"]),
            "tokens": dict(tokens) if isinstance(tokens, Mapping) else {},
            "title": agent.get("terminal_title_stripped", agent.get("terminal_title", "")),
            "routing_name": agent.get("name") or None,
            "presentation_source": agent.get("metadata_source"),
            "endpoint_generation": "herdr:"
            + hashlib.sha256(
                f"{agent['terminal_id']}\0{spec.thread_id}".encode("utf-8")
            ).hexdigest()[:24],
        }
        if self._observed["routing_name"] is None:
            self._restore_baseline = self.restoration_baseline()
        return dict(self._observed)

    def recovery_baseline(self) -> dict[str, Any]:
        baseline = self.restoration_baseline()
        baseline["schema"] = "league.shotcaller-bootstrap-baseline.v2"
        baseline["presentation_source"] = self._observed["presentation_source"]
        return baseline

    def restoration_baseline(self) -> dict[str, Any]:
        if self._observed is None:
            raise StorageRefusal(
                "shotcaller_identity_unverified", "calling identity was not inspected"
            )
        tokens = self._observed["tokens"]
        return {
            "schema": "league.shotcaller-bootstrap-baseline.v1",
            "terminal_id": self._observed["terminal_id"],
            "endpoint_generation": self._observed["endpoint_generation"],
            "state_change_seq": self._observed["state_change_seq"],
            "routing_name": self._observed["routing_name"],
            "sidebar_name": str(tokens.get("sidebar_name", "")),
            "thread_title": str(tokens.get("thread_title", "")),
            "title": str(self._observed["title"] or ""),
        }

    def use_restoration_baseline(self, baseline: Mapping[str, Any]) -> None:
        if (
            self._observed is None
            or baseline.get("schema")
            not in {
                "league.shotcaller-bootstrap-baseline.v1",
                "league.shotcaller-bootstrap-baseline.v2",
            }
            or baseline.get("routing_name") is not None
            or baseline.get("terminal_id") != self._observed["terminal_id"]
            or baseline.get("endpoint_generation") != self._observed["endpoint_generation"]
        ):
            raise StorageRefusal(
                "bootstrap_baseline_unverified",
                "stored Shotcaller bootstrap baseline does not match the current endpoint",
            )
        self._restore_baseline = dict(baseline)

    def require_current_recovery_baseline(
        self, spec: ShotcallerBootstrapSpec, baseline: Mapping[str, Any]
    ) -> None:
        """Fence a legacy recovery against writes after its first observation."""

        pane, agent = self._current()
        tokens = agent.get("tokens")
        title = agent.get("terminal_title_stripped", agent.get("terminal_title", "")) or ""
        exact = bool(
            baseline.get("schema") == "league.shotcaller-bootstrap-baseline.v2"
            and self._exact(spec, pane, agent)
            and agent.get("name") in {None, ""}
            and isinstance(tokens, Mapping)
            and agent.get("terminal_id") == baseline.get("terminal_id")
            and agent.get("state_change_seq") == baseline.get("state_change_seq")
            and agent.get("metadata_source") == baseline.get("presentation_source")
            and str(tokens.get("sidebar_name", "")) == baseline.get("sidebar_name")
            and str(tokens.get("thread_title", "")) == baseline.get("thread_title")
            and str(title) == baseline.get("title")
        )
        if not exact:
            raise StorageRefusal(
                "shotcaller_metadata_unverified",
                "legacy Shotcaller presentation changed before publication",
            )

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
        pane, agent = self._current()
        if not self._exact(spec, pane, agent) or agent.get("name") != alias:
            raise StorageRefusal(
                "shotcaller_identity_unverified",
                "same-pane Shotcaller routing identity did not verify",
            )
        self._report_title(spec, callsign, agent)
        self._stable_published(spec, callsign)
        return self._receipt(spec, callsign)

    def _title_owner(self, spec: ShotcallerBootstrapSpec) -> str:
        return hashlib.sha256(spec.assignment_id.encode()).hexdigest()[:16]

    def _title_source(self, spec: ShotcallerBootstrapSpec) -> str:
        return "league-shotcaller-" + self._title_owner(spec)

    def _report_title(
        self,
        spec: ShotcallerBootstrapSpec,
        callsign: str,
        agent: Mapping[str, Any],
    ) -> None:
        authority_source = _session_source(agent)
        sequence = agent.get("state_change_seq")
        if not isinstance(authority_source, str) or not isinstance(sequence, int):
            raise StorageRefusal(
                "shotcaller_metadata_unverified",
                "same-pane Shotcaller metadata authority is incomplete",
            )
        self._published_source = self._title_source(spec)
        self._run(
            (
                "herdr",
                "pane",
                "report-metadata",
                self.options.pane_id,
                "--source",
                self._title_source(spec),
                "--applies-to-source",
                authority_source,
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
                str(sequence + 1),
            ),
            "Herdr Shotcaller metadata",
            silent=True,
        )

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
            and agent.get("metadata_source") == self._title_source(spec)
            and isinstance(tokens, Mapping)
            and tokens.get("sidebar_name") == callsign
            and tokens.get("thread_title") == callsign
            and agent.get("terminal_title") == callsign
            and agent.get("terminal_title_stripped") == callsign
        )

    def _stable_published(
        self, spec: ShotcallerBootstrapSpec, callsign: str
    ) -> None:
        prior: tuple[str, int] | None = None
        consecutive = 0
        restored = False
        for _ in range(50):
            pane, agent = self._current()
            authority_source = _session_source(agent)
            presentation_source = agent.get("metadata_source")
            sequence = agent.get("state_change_seq")
            endpoint_exact = bool(
                self._exact(spec, pane, agent)
                and agent.get("name") == callsign.lower()
                and isinstance(authority_source, str)
                and isinstance(presentation_source, str)
                and isinstance(sequence, int)
            )
            if not endpoint_exact:
                raise StorageRefusal(
                    "shotcaller_metadata_unverified",
                    "same-pane Shotcaller route or metadata authority changed",
                )
            if self._published_exact(spec, callsign, pane, agent):
                key = (presentation_source, sequence)
                consecutive = consecutive + 1 if key == prior else 1
                prior = key
                if consecutive >= 2:
                    return
            else:
                if presentation_source not in {
                    authority_source,
                    self._title_source(spec),
                }:
                    raise StorageRefusal(
                        "shotcaller_metadata_unverified",
                        "newer Shotcaller display metadata is not bootstrap-owned",
                    )
                if restored:
                    raise StorageRefusal(
                        "shotcaller_metadata_unverified",
                        "owned Shotcaller display metadata did not settle",
                    )
                self._report_title(spec, callsign, agent)
                restored = True
                prior = None
                consecutive = 0
            time.sleep(0.1)
        raise StorageRefusal(
            "shotcaller_metadata_unverified",
            "same-pane Shotcaller display metadata did not become stable",
        )

    def current_receipt(
        self, spec: ShotcallerBootstrapSpec, callsign: str
    ) -> dict[str, Any]:
        if self._observed is None:
            raise StorageRefusal(
                "shotcaller_identity_unverified", "calling identity was not inspected"
            )
        self._stable_published(spec, callsign)
        return self._receipt(spec, callsign)

    def _receipt(
        self, spec: ShotcallerBootstrapSpec, callsign: str
    ) -> dict[str, Any]:
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
        if self._restore_baseline is None:
            return False
        previous_sidebar = self._restore_baseline["sidebar_name"]
        previous_thread_title = self._restore_baseline["thread_title"]
        previous_title = self._restore_baseline["title"]
        try:
            pane, agent = self._current()
            tokens = agent.get("tokens")
            presentation_source = agent.get("metadata_source")
            authority_source = _session_source(agent)
            if (
                not self._exact_placeholder(pane, agent)
                or not isinstance(tokens, Mapping)
                or not isinstance(presentation_source, str)
                or not isinstance(authority_source, str)
            ):
                return False
            preserve_newer_presentation = presentation_source not in {
                authority_source,
                self._published_source,
            }
            protected = {
                "metadata_source": presentation_source,
                "title": agent.get(
                    "terminal_title_stripped", agent.get("terminal_title", "")
                )
                or "",
                "tokens": dict(tokens),
            }
            self._run(
                ("herdr", "agent", "rename", self.options.pane_id, "--clear"),
                "Herdr Shotcaller routing rollback",
            )
            if preserve_newer_presentation:
                pane, agent = self._current()
                current_tokens = agent.get("tokens")
                sequence = agent.get("state_change_seq")
                authority_source = _session_source(agent)
                if (
                    not self._exact_placeholder(pane, agent)
                    or agent.get("name") not in {None, ""}
                    or agent.get("metadata_source") != protected["metadata_source"]
                    or (
                        agent.get(
                            "terminal_title_stripped",
                            agent.get("terminal_title", ""),
                        )
                        or ""
                    )
                    != protected["title"]
                    or not isinstance(current_tokens, Mapping)
                    or not isinstance(sequence, int)
                    or not isinstance(authority_source, str)
                ):
                    return False
                expected_tokens = dict(current_tokens)
                for key, value in (
                    ("sidebar_name", previous_sidebar),
                    ("thread_title", previous_thread_title),
                ):
                    if value:
                        expected_tokens[key] = value
                    else:
                        expected_tokens.pop(key, None)
                self._run(
                    (
                        "herdr",
                        "pane",
                        "report-metadata",
                        self.options.pane_id,
                        "--source",
                        "league-shotcaller-rollback",
                        "--applies-to-source",
                        authority_source,
                        "--agent",
                        "codex",
                        "--display-agent",
                        "codex",
                        "--token",
                        f"sidebar_name={previous_sidebar}",
                        "--token",
                        f"thread_title={previous_thread_title}",
                        "--seq",
                        str(sequence + 1),
                    ),
                    "Herdr Shotcaller display-token rollback",
                    silent=True,
                )
                pane, agent = self._current()
                return bool(
                    self._exact_placeholder(pane, agent)
                    and agent.get("name") in {None, ""}
                    and agent.get("metadata_source") == protected["metadata_source"]
                    and (
                        agent.get(
                            "terminal_title_stripped",
                            agent.get("terminal_title", ""),
                        )
                        or ""
                    )
                    == protected["title"]
                    and agent.get("tokens") == expected_tokens
                )
            pane, agent = self._current()
            authority_source = _session_source(agent)
            sequence = agent.get("state_change_seq")
            if (
                not self._exact_placeholder(pane, agent)
                or not isinstance(authority_source, str)
                or not isinstance(sequence, int)
            ):
                return False
            self._run(
                (
                    "herdr",
                    "pane",
                    "report-metadata",
                    self.options.pane_id,
                    "--source",
                    "league-shotcaller-rollback",
                    "--applies-to-source",
                    authority_source,
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
                    str(sequence + 1),
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
        baseline = (
            self.store.shotcaller_bootstrap_baseline(spec.assignment_id)
            if existing is not None
            else None
        )
        expected_alias = (
            str(existing["callsign"]).lower()
            if existing is not None and existing["state"] in {"reserved", "active"}
            else None
        )
        observed = self.adapter.inspect(
            spec,
            expected_alias=expected_alias,
            allow_unpublished=existing is not None and existing["state"] == "reserved",
        )
        if baseline is not None:
            self.adapter.use_restoration_baseline(baseline)
        elif (
            existing is not None
            and existing["state"] == "reserved"
        ):
            raise StorageRefusal(
                "shotcaller_creation_cleanup_unproven",
                "pre-existing Shotcaller reservation has no durable pre-publication baseline",
            )
        if completed is not None:
            receipt = self.adapter.current_receipt(spec, str(completed["callsign"]))
            return self.store.record_shotcaller_bootstrap(
                spec.assignment_id, 1, receipt, self.clock.now()
            )
        at = self.clock.now()
        reserved = self.store.allocate_callsign(
            spec.assignment_id,
            spec.agent_id,
            "shotcaller",
            "shotcaller",
            spec.agent_id,
            spec.capabilities,
            at,
            recovery_baseline=self.adapter.recovery_baseline(),
            recovery_thread_id=spec.thread_id,
        )
        published = False
        try:
            if baseline is None:
                baseline = self.store.shotcaller_bootstrap_baseline(spec.assignment_id)
                if baseline is None:
                    baseline = self.store.record_shotcaller_bootstrap_baseline(
                        spec.assignment_id, 1, self.adapter.restoration_baseline()
                    )["baseline"]
                self.adapter.use_restoration_baseline(baseline)
            if baseline.get("schema") == "league.shotcaller-bootstrap-baseline.v2":
                if fault:
                    fault("after_shotcaller_recovery_reserved")
                self.adapter.require_current_recovery_baseline(spec, baseline)
            if existing is not None and existing["state"] == "active":
                receipt = self.adapter.current_receipt(spec, str(reserved["callsign"]))
            else:
                published = True
                receipt = self.adapter.publish(spec, str(reserved["callsign"]))
                if fault:
                    fault("after_shotcaller_publish")
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
