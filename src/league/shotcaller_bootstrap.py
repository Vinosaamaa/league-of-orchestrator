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
LIVE_STATUSES = {"active", "blocked", "done", "idle", "waiting", "working"}
ROUTING_ALIAS = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TITLE_OWNER_TOKEN = "shotcaller_title_owner"
TITLE_SOURCE_TOKEN = "shotcaller_title_source"


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
        self._resume_owned_route = False
        self._resume_owned_metadata = False
        self._publication_attempt: dict[str, Any] | None = None
        self._expected_published_sequence: int | None = None
        self._provider_route_only_alias: str | None = None
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

    def _routing_name(self, agent: Mapping[str, Any]) -> str | None:
        """Read only explicit Herdr route fields, never presentation tokens."""

        bindings: set[str] = set()
        for field in ("name", "routing_name", "routing_alias"):
            value = agent.get(field)
            if value is None or value == "":
                continue
            if not isinstance(value, str) or not ROUTING_ALIAS.fullmatch(value):
                raise StorageRefusal(
                    "shotcaller_identity_unverified",
                    "calling Codex routing observation is ambiguous",
                )
            bindings.add(value)
        if len(bindings) > 1:
            raise StorageRefusal(
                "shotcaller_identity_unverified",
                "calling Codex routing observation is ambiguous",
            )
        return next(iter(bindings), None)

    def _presentation_title(self, agent: Mapping[str, Any]) -> str | None:
        value = agent.get("terminal_title_stripped", agent.get("terminal_title"))
        if not isinstance(value, str):
            return None
        agent_kind = agent.get("agent")
        suffix = f" | {agent_kind}" if isinstance(agent_kind, str) else ""
        return value[: -len(suffix)] if suffix and value.endswith(suffix) else value

    def _presentation_source(
        self,
        agent: Mapping[str, Any],
        *,
        provider_route_only_alias: str | None = None,
    ) -> str | None:
        """Return an explicit source or a fully proven Herdr provider presentation."""

        source = agent.get("metadata_source")
        if "metadata_source" in agent:
            if isinstance(source, str) and source:
                return source
            return None
        tokens = agent.get("tokens")
        authority_source = _session_source(agent)
        thread_id = _session(agent)
        title = self._presentation_title(agent)
        if (
            not isinstance(tokens, Mapping)
            or not isinstance(authority_source, str)
            or not isinstance(thread_id, str)
            or not isinstance(title, str)
        ):
            return None
        labels = (
            tokens.get("callsign"),
            tokens.get("sidebar_name"),
            tokens.get("thread_title"),
        )
        if (
            not all(isinstance(value, str) and value for value in labels)
            or len(set(labels)) != 1
            or tokens.get("harness") != "codex"
            or tokens.get("identity_thread_id") != thread_id
            or tokens.get("identity_title") != f"Codex | {labels[0]}"
            or title != labels[0]
        ):
            return None
        route = self._routing_name(agent)
        token_route = tokens.get("routing_alias")
        orchestrator_identity = tokens.get("orchestrator_identity")
        if route is None:
            if (
                (token_route is not None and token_route != "")
                or (
                    orchestrator_identity is not None
                    and orchestrator_identity != ""
                )
            ):
                return None
        elif token_route in {None, ""} and orchestrator_identity in {None, ""}:
            pass
        elif (
            token_route != route
            or orchestrator_identity != f"codex · {route}"
            or (
                str(labels[0]).casefold() != route.casefold()
                and provider_route_only_alias != route
                and self._provider_route_only_alias != route
            )
        ):
            return None
        return authority_source

    def _route_only_recovery_source(
        self,
        spec: ShotcallerBootstrapSpec,
        pane: Mapping[str, Any],
        agent: Mapping[str, Any],
        expected_alias: str | None,
        recovery: Mapping[str, Any] | None,
    ) -> str | None:
        if expected_alias is None or not isinstance(recovery, Mapping):
            return None
        assignment = recovery.get("assignment")
        baseline = recovery.get("baseline")
        publication = recovery.get("publication")
        tokens = agent.get("tokens")
        title = self._presentation_title(agent)
        source = self._presentation_source(
            agent, provider_route_only_alias=expected_alias
        )
        source_less_provider = "metadata_source" not in agent
        endpoint_generation = "herdr:" + hashlib.sha256(
            f"{agent.get('terminal_id')}\0{spec.thread_id}".encode("utf-8")
        ).hexdigest()[:24]
        if (
            not isinstance(assignment, Mapping)
            or not isinstance(baseline, Mapping)
            or not isinstance(publication, Mapping)
            or not isinstance(tokens, Mapping)
            or not isinstance(source, str)
            or not self._exact(spec, pane, agent)
            or self._routing_name(agent) != expected_alias
            or assignment.get("assignment_id") != spec.assignment_id
            or assignment.get("agent_id") != spec.agent_id
            or assignment.get("callsign", "").lower() != expected_alias
            or assignment.get("role") != "shotcaller"
            or assignment.get("scope")
            != {"kind": "shotcaller", "id": spec.agent_id}
            or assignment.get("state") != "reserved"
            or assignment.get("version") != 1
            or assignment.get("runtime_instance_id") not in {
                None,
                spec.runtime_instance_id,
            }
            or baseline.get("schema")
            != "league.shotcaller-bootstrap-baseline.v2"
            or baseline.get("routing_name") is not None
            or baseline.get("terminal_id") != agent.get("terminal_id")
            or baseline.get("endpoint_generation") != endpoint_generation
            or baseline.get("presentation_source") != source
            or baseline.get("title") != title
            or baseline.get("sidebar_name")
            != str(tokens.get("sidebar_name", ""))
            or baseline.get("thread_title")
            != str(tokens.get("thread_title", ""))
            or publication.get("schema")
            != "league.shotcaller-bootstrap-publication.v1"
            or publication.get("assignment_id") != spec.assignment_id
            or publication.get("agent_id") != spec.agent_id
            or publication.get("callsign") != assignment.get("callsign")
            or publication.get("routing_name") != expected_alias
            or publication.get("terminal_id") != agent.get("terminal_id")
            or publication.get("endpoint_generation") != endpoint_generation
            or publication.get("session_identity") != spec.thread_id
            or publication.get("worktree")
            != str(Path(self.options.worktree).resolve())
            or publication.get("presentation_source") != source
            or publication.get("title") != title
            or publication.get("sidebar_name")
            != str(tokens.get("sidebar_name", ""))
            or publication.get("thread_title")
            != str(tokens.get("thread_title", ""))
            or publication.get("baseline_digest") != digest(baseline)
            or type(publication.get("observed_state_change_seq")) is not int
            or not isinstance(agent.get("state_change_seq"), int)
            or agent["state_change_seq"]
            < publication["observed_state_change_seq"]
            or (
                source_less_provider
                and (
                    tokens.get("callsign") != title
                    or tokens.get("harness") != "codex"
                    or tokens.get("identity_thread_id") != spec.thread_id
                    or tokens.get("identity_title") != f"Codex | {title}"
                    or not (
                        (
                            tokens.get("routing_alias") in {None, ""}
                            and tokens.get("orchestrator_identity") in {None, ""}
                        )
                        or (
                            tokens.get("routing_alias") == expected_alias
                            and tokens.get("orchestrator_identity")
                            == f"codex · {expected_alias}"
                        )
                    )
                )
            )
            or tokens.get(TITLE_OWNER_TOKEN) not in (None, "")
            or tokens.get(TITLE_SOURCE_TOKEN) not in (None, "")
        ):
            return None
        return source

    def _owned_presentation_source(
        self, spec: ShotcallerBootstrapSpec, agent: Mapping[str, Any], route: str | None
    ) -> str | None:
        tokens = agent.get("tokens")
        title = self._presentation_title(agent)
        explicit_source = agent.get("metadata_source")
        if (
            route is None
            or not isinstance(tokens, Mapping)
            or (
                "metadata_source" in agent
                and explicit_source
                not in {self._title_source(spec), _session_source(agent)}
            )
            or tokens.get(TITLE_OWNER_TOKEN) != self._title_owner(spec)
            or tokens.get(TITLE_SOURCE_TOKEN) != self._title_source(spec)
            or not isinstance(title, str)
            or tokens.get("sidebar_name") != title
            or tokens.get("thread_title") != title
            or title.casefold() != route.casefold()
        ):
            return None
        return self._title_source(spec)

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
        route_only_recovery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not THREAD_UUID.fullmatch(spec.thread_id):
            raise StorageRefusal(
                "shotcaller_identity_unverified", "calling Codex thread identity is invalid"
            )
        pane, agent = self._current()
        tokens = agent.get("tokens")
        routing_name = self._routing_name(agent)
        owned_source = self._owned_presentation_source(spec, agent, routing_name)
        route_only_candidate = bool(
            allow_unpublished
            and expected_alias is not None
            and routing_name == expected_alias
            and owned_source is None
        )
        if route_only_candidate:
            presentation_source = self._route_only_recovery_source(
                spec, pane, agent, expected_alias, route_only_recovery
            )
            if presentation_source is not None:
                self._provider_route_only_alias = expected_alias
        else:
            presentation_source = self._presentation_source(agent)
        if presentation_source is None:
            presentation_source = owned_source
        routing_exact = (
            routing_name is None
            if expected_alias is None
            else routing_name == expected_alias
            or (allow_unpublished and routing_name is None)
        )
        if (
            not self._exact(spec, pane, agent)
            or not routing_exact
            or not isinstance(tokens, Mapping)
            or not isinstance(presentation_source, str)
        ):
            raise StorageRefusal(
                "shotcaller_identity_unverified",
                "calling Codex must be exact, current, and not already routing-bound",
            )
        self._observed = {
            "terminal_id": str(agent["terminal_id"]),
            "state_change_seq": int(agent["state_change_seq"]),
            "tokens": dict(tokens) if isinstance(tokens, Mapping) else {},
            "title": self._presentation_title(agent),
            "routing_name": routing_name,
            "presentation_source": presentation_source,
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
        self,
        spec: ShotcallerBootstrapSpec,
        baseline: Mapping[str, Any],
        callsign: str,
    ) -> dict[str, Any]:
        """Fence a legacy recovery against writes after its first observation."""

        pane, agent = self._current()
        tokens = agent.get("tokens")
        title = self._presentation_title(agent)
        routing_name = self._routing_name(agent)
        presentation_source = self._presentation_source(agent)
        common_exact = bool(
            baseline.get("schema") == "league.shotcaller-bootstrap-baseline.v2"
            and self._exact(spec, pane, agent)
            and isinstance(tokens, Mapping)
            and agent.get("terminal_id") == baseline.get("terminal_id")
            and presentation_source == baseline.get("presentation_source")
            and str(tokens.get("sidebar_name", "")) == baseline.get("sidebar_name")
            and str(tokens.get("thread_title", "")) == baseline.get("thread_title")
            and str(title) == baseline.get("title")
        )
        unpublished = bool(
            common_exact
            and routing_name is None
            and agent.get("state_change_seq") == baseline.get("state_change_seq")
        )
        route_only_publication = bool(
            common_exact
            and routing_name == callsign.lower()
            and isinstance(agent.get("state_change_seq"), int)
            and agent["state_change_seq"] >= baseline.get("state_change_seq")
        )
        if not unpublished and not route_only_publication:
            raise StorageRefusal(
                "shotcaller_metadata_unverified",
                "legacy Shotcaller presentation changed before publication",
            )
        self._resume_owned_route = route_only_publication
        return {
            "schema": "league.shotcaller-bootstrap-publication.v1",
            "assignment_id": spec.assignment_id,
            "agent_id": spec.agent_id,
            "callsign": callsign,
            "routing_name": callsign.lower(),
            "terminal_id": str(agent["terminal_id"]),
            "endpoint_generation": str(baseline["endpoint_generation"]),
            "session_identity": spec.thread_id,
            "worktree": str(Path(self.options.worktree).resolve()),
            "presentation_source": str(presentation_source),
            "title": str(title),
            "sidebar_name": str(tokens.get("sidebar_name", "")),
            "thread_title": str(tokens.get("thread_title", "")),
            "baseline_digest": digest(baseline),
            "observed_state_change_seq": int(agent["state_change_seq"]),
        }

    def use_publication_attempt(
        self,
        spec: ShotcallerBootstrapSpec,
        baseline: Mapping[str, Any],
        publication: Mapping[str, Any],
        callsign: str,
    ) -> None:
        expected = {
            "schema": "league.shotcaller-bootstrap-publication.v1",
            "assignment_id": spec.assignment_id,
            "agent_id": spec.agent_id,
            "callsign": callsign,
            "routing_name": callsign.lower(),
            "terminal_id": self._observed["terminal_id"] if self._observed else None,
            "endpoint_generation": baseline.get("endpoint_generation"),
            "session_identity": spec.thread_id,
            "worktree": str(Path(self.options.worktree).resolve()),
            "presentation_source": baseline.get("presentation_source"),
            "title": baseline.get("title"),
            "sidebar_name": baseline.get("sidebar_name"),
            "thread_title": baseline.get("thread_title"),
            "baseline_digest": digest(baseline),
        }
        if (
            set(publication) != set(expected) | {"observed_state_change_seq"}
            or any(publication.get(key) != value for key, value in expected.items())
            or type(publication.get("observed_state_change_seq")) is not int
            or publication["observed_state_change_seq"] < baseline.get("state_change_seq", 0)
        ):
            raise StorageRefusal(
                "bootstrap_publication_unverified",
                "stored Shotcaller publication attempt is not exact",
            )
        self._publication_attempt = dict(publication)
        self._published_source = self._title_source(spec)

    def require_current_publication(
        self,
        spec: ShotcallerBootstrapSpec,
        baseline: Mapping[str, Any],
        callsign: str,
    ) -> None:
        if self._publication_attempt is None:
            raise StorageRefusal(
                "bootstrap_publication_unverified",
                "Shotcaller publication attempt was not loaded",
            )
        pane, agent = self._current()
        tokens = agent.get("tokens")
        sequence = agent.get("state_change_seq")
        route = self._routing_name(agent)
        baseline_exact = bool(
            self._exact(spec, pane, agent)
            and isinstance(tokens, Mapping)
            and self._presentation_source(agent) == baseline.get("presentation_source")
            and self._presentation_title(agent) == baseline.get("title")
            and str(tokens.get("sidebar_name", "")) == baseline.get("sidebar_name")
            and str(tokens.get("thread_title", "")) == baseline.get("thread_title")
        )
        owned_exact = self._published_exact(spec, callsign, pane, agent)
        if (
            not isinstance(sequence, int)
            or sequence < self._publication_attempt["observed_state_change_seq"]
            or self._observed is None
            or sequence != self._observed["state_change_seq"]
            or not (
                baseline_exact and route in {None, callsign.lower()}
                or owned_exact and route == callsign.lower()
            )
        ):
            raise StorageRefusal(
                "shotcaller_metadata_unverified",
                "Shotcaller publication identity or presentation changed",
            )
        self._resume_owned_route = route == callsign.lower()
        self._resume_owned_metadata = owned_exact

    def publish(self, spec: ShotcallerBootstrapSpec, callsign: str) -> dict[str, Any]:
        if self._observed is None:
            raise StorageRefusal(
                "shotcaller_identity_unverified", "calling identity was not inspected"
            )
        alias = callsign.lower()
        resume_owned_metadata = self._resume_owned_metadata
        if not self._resume_owned_route:
            self._run(
                ("herdr", "agent", "rename", self.options.pane_id, alias),
                "Herdr Shotcaller routing rename",
            )
        self._resume_owned_route = False
        self._resume_owned_metadata = False
        pane, agent = self._current()
        if not self._exact(spec, pane, agent) or self._routing_name(agent) != alias:
            raise StorageRefusal(
                "shotcaller_identity_unverified",
                "same-pane Shotcaller routing identity did not verify",
            )
        if not resume_owned_metadata:
            if self._publication_attempt is not None and not self._baseline_presentation_exact(
                spec, pane, agent
            ):
                raise StorageRefusal(
                    "shotcaller_metadata_unverified",
                    "Shotcaller provider presentation changed before title publication",
                )
            self._report_title(spec, callsign, agent)
        self._stable_published(spec, callsign)
        return self._receipt(spec, callsign)

    def _baseline_presentation_exact(
        self,
        spec: ShotcallerBootstrapSpec,
        pane: Mapping[str, Any],
        agent: Mapping[str, Any],
    ) -> bool:
        attempt = self._publication_attempt
        tokens = agent.get("tokens")
        return bool(
            attempt is not None
            and self._exact(spec, pane, agent)
            and isinstance(tokens, Mapping)
            and agent.get("terminal_id") == attempt["terminal_id"]
            and self._presentation_source(agent) == attempt["presentation_source"]
            and self._presentation_title(agent) == attempt["title"]
            and str(tokens.get("sidebar_name", "")) == attempt["sidebar_name"]
            and str(tokens.get("thread_title", "")) == attempt["thread_title"]
        )

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
        self._expected_published_sequence = sequence + 1
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
                "--token",
                f"{TITLE_OWNER_TOKEN}={self._title_owner(spec)}",
                "--token",
                f"{TITLE_SOURCE_TOKEN}={self._title_source(spec)}",
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
        presentation_source = self._presentation_source(agent)
        authority_source = _session_source(agent)
        ownership_exact = bool(
            isinstance(tokens, Mapping)
            and tokens.get(TITLE_OWNER_TOKEN) == self._title_owner(spec)
            and tokens.get(TITLE_SOURCE_TOKEN) == self._title_source(spec)
        )
        return bool(
            self._exact(spec, pane, agent)
            and self._routing_name(agent) == callsign.lower()
            and ownership_exact
            and (
                presentation_source in {self._title_source(spec), authority_source}
                or (presentation_source is None and ownership_exact)
            )
            and isinstance(tokens, Mapping)
            and tokens.get("sidebar_name") == callsign
            and tokens.get("thread_title") == callsign
            and self._presentation_title(agent) == callsign
        )

    def _stable_published(
        self, spec: ShotcallerBootstrapSpec, callsign: str
    ) -> None:
        prior: tuple[str, int] | None = None
        consecutive = 0
        for _ in range(50):
            pane, agent = self._current()
            authority_source = _session_source(agent)
            presentation_source = self._presentation_source(agent)
            sequence = agent.get("state_change_seq")
            published_exact = self._published_exact(spec, callsign, pane, agent)
            endpoint_exact = bool(
                self._exact(spec, pane, agent)
                and self._routing_name(agent) == callsign.lower()
                and isinstance(authority_source, str)
                and (isinstance(presentation_source, str) or published_exact)
                and isinstance(sequence, int)
            )
            if not endpoint_exact:
                raise StorageRefusal(
                    "shotcaller_metadata_unverified",
                    "same-pane Shotcaller route or metadata authority changed",
                )
            if published_exact:
                if (
                    self._expected_published_sequence is not None
                    and consecutive == 0
                    and sequence != self._expected_published_sequence
                ):
                    raise StorageRefusal(
                        "shotcaller_metadata_unverified",
                        "Shotcaller presentation changed during title publication",
                    )
                key = (presentation_source or self._title_source(spec), sequence)
                consecutive = consecutive + 1 if key == prior else 1
                prior = key
                if consecutive >= 2:
                    self._expected_published_sequence = None
                    return
            else:
                raise StorageRefusal(
                    "shotcaller_metadata_unverified",
                    "newer Shotcaller display metadata is not bootstrap-owned",
                )
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
            presentation_source = self._presentation_source(agent)
            authority_source = _session_source(agent)
            routing_name = self._routing_name(agent)
            current_title = self._presentation_title(agent) or ""
            owned_display = bool(
                isinstance(tokens, Mapping)
                and isinstance(routing_name, str)
                and tokens.get(TITLE_OWNER_TOKEN)
                == self._title_owner_from_source(self._published_source)
                and tokens.get(TITLE_SOURCE_TOKEN) == self._published_source
                and tokens.get("sidebar_name") == current_title
                and tokens.get("thread_title") == current_title
                and current_title.casefold() == routing_name.casefold()
            )
            if presentation_source is None and isinstance(authority_source, str):
                presentation_source = (
                    self._published_source if owned_display else authority_source
                )
            if (
                not self._exact_placeholder(pane, agent)
                or not isinstance(tokens, Mapping)
                or not isinstance(presentation_source, str)
                or not isinstance(authority_source, str)
            ):
                return False
            baseline_exact = bool(
                current_title == previous_title
                and str(tokens.get("sidebar_name", "")) == previous_sidebar
                and str(tokens.get("thread_title", "")) == previous_thread_title
            )
            preserve_newer_presentation = not owned_display and not baseline_exact
            protected = {
                "metadata_source": presentation_source,
                "title": current_title,
                "tokens": dict(tokens),
            }
            self._run(
                ("herdr", "agent", "rename", self.options.pane_id, "--clear"),
                "Herdr Shotcaller routing rollback",
            )
            if baseline_exact:
                pane, agent = self._current()
                current_tokens = agent.get("tokens")
                expected_tokens = dict(protected["tokens"])
                if expected_tokens.get("routing_alias") == routing_name:
                    expected_tokens.pop("routing_alias", None)
                if expected_tokens.get("orchestrator_identity") == (
                    f"codex · {routing_name}"
                ):
                    expected_tokens.pop("orchestrator_identity", None)
                if (
                    self._exact_placeholder(pane, agent)
                    and self._routing_name(agent) is None
                    and self._presentation_source(agent)
                    == protected["metadata_source"]
                    and (self._presentation_title(agent) or "")
                    == protected["title"]
                    and current_tokens == expected_tokens
                ):
                    return True
            if preserve_newer_presentation:
                pane, agent = self._current()
                current_tokens = agent.get("tokens")
                sequence = agent.get("state_change_seq")
                authority_source = _session_source(agent)
                if (
                    not self._exact_placeholder(pane, agent)
                    or self._routing_name(agent) is not None
                    or self._presentation_source(agent)
                    != protected["metadata_source"]
                    or (self._presentation_title(agent) or "") != protected["title"]
                    or not isinstance(current_tokens, Mapping)
                    or not isinstance(sequence, int)
                    or not isinstance(authority_source, str)
                ):
                    return False
                expected_tokens = dict(current_tokens)
                expected_tokens.pop(TITLE_OWNER_TOKEN, None)
                expected_tokens.pop(TITLE_SOURCE_TOKEN, None)
                restore_tokens: list[tuple[str, str]] = []
                for key, value in (
                    ("sidebar_name", previous_sidebar),
                    ("thread_title", previous_thread_title),
                ):
                    current_value = current_tokens.get(key)
                    if (
                        isinstance(current_value, str)
                        and isinstance(routing_name, str)
                        and current_value.casefold() == routing_name.casefold()
                    ):
                        restore_tokens.append((key, value))
                        if value:
                            expected_tokens[key] = value
                        else:
                            expected_tokens.pop(key, None)
                arguments = [
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
                ]
                for key, value in restore_tokens:
                    arguments.extend(("--token", f"{key}={value}"))
                arguments.extend(
                    (
                        "--token",
                        f"{TITLE_OWNER_TOKEN}=",
                        "--token",
                        f"{TITLE_SOURCE_TOKEN}=",
                        "--seq",
                        str(sequence + 1),
                    )
                )
                self._run(
                    tuple(arguments),
                    "Herdr Shotcaller display-token rollback",
                    silent=True,
                )
                pane, agent = self._current()
                return bool(
                    self._exact_placeholder(pane, agent)
                    and self._routing_name(agent) is None
                    and self._presentation_source(agent)
                    == protected["metadata_source"]
                    and (self._presentation_title(agent) or "")
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
                    "--token",
                    f"{TITLE_OWNER_TOKEN}=",
                    "--token",
                    f"{TITLE_SOURCE_TOKEN}=",
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
            title = self._presentation_title(agent) or ""
            return bool(
                self._exact_placeholder(pane, agent)
                and observed_sidebar == previous_sidebar
                and observed_thread == previous_thread_title
                and title == previous_title
                and self._routing_name(agent) is None
            )
        except StorageRefusal:
            return False

    @staticmethod
    def _title_owner_from_source(source: str | None) -> str | None:
        prefix = "league-shotcaller-"
        if isinstance(source, str) and source.startswith(prefix):
            return source[len(prefix) :]
        return None

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
        publication = (
            self.store.shotcaller_bootstrap_publication(spec.assignment_id)
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
            route_only_recovery=(
                {
                    "assignment": existing,
                    "baseline": baseline,
                    "publication": publication,
                }
                if existing is not None
                and existing["state"] == "reserved"
                and baseline is not None
                and publication is not None
                else None
            ),
        )
        if (
            existing is not None
            and existing["state"] == "reserved"
            and baseline is not None
            and publication is not None
        ):
            self.store.bind_shotcaller_bootstrap_runtime(
                spec.assignment_id, 1, spec.runtime_instance_id
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
        published = observed.get("routing_name") == str(reserved["callsign"]).lower()
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
                if publication is None:
                    candidate = self.adapter.require_current_recovery_baseline(
                        spec, baseline, str(reserved["callsign"])
                    )
                    publication = self.store.record_shotcaller_bootstrap_publication(
                        spec.assignment_id, 1, candidate
                    )["publication"]
                self.store.bind_shotcaller_bootstrap_runtime(
                    spec.assignment_id, 1, spec.runtime_instance_id
                )
                self.adapter.use_publication_attempt(
                    spec, baseline, publication, str(reserved["callsign"])
                )
                self.adapter.require_current_publication(
                    spec, baseline, str(reserved["callsign"])
                )
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
            if restored:
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
