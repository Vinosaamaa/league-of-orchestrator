"""Owner-authorized recovery of one exact pre-fix Champion display."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .storage import Storage, StorageRefusal
from .storage_assignment import LegacyDisplayReconciliationCommand
from .visible_launch import (
    CommandRunner,
    SubprocessRunner,
    _agent_object,
    _result_object,
    _session_id,
    _session_source,
)


THREAD_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
PRESENT_STATUSES = {"active", "blocked", "done", "idle", "waiting", "working"}
OWNERSHIP_TOKENS = {
    "launch_title_owner",
    "launch_title_source",
    "launch_title_applies_to",
    "legacy_display_owner",
    "legacy_display_assignment",
    "legacy_display_source",
    "legacy_display_applies_to",
}
LEGACY_OWNERSHIP_TOKENS = {
    key for key in OWNERSHIP_TOKENS if key.startswith("legacy_display_")
}
_LAST_METADATA_SEQUENCE = 0


def _next_metadata_sequence() -> int:
    """Return a process-monotonic sequence for League's metadata source."""
    global _LAST_METADATA_SEQUENCE
    candidate = time.time_ns()
    if candidate <= _LAST_METADATA_SEQUENCE:
        candidate = _LAST_METADATA_SEQUENCE + 1
    _LAST_METADATA_SEQUENCE = candidate
    return candidate


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _canonical_title(agent: Mapping[str, Any]) -> str:
    tokens = agent.get("tokens")
    if (
        isinstance(tokens, Mapping)
        and isinstance(tokens.get("legacy_display_source"), str)
        and str(tokens.get("legacy_display_source")).startswith("league-legacy-")
        and isinstance(tokens.get("thread_title"), str)
    ):
        target = str(tokens["thread_title"])
        rendered = str(agent.get("title") or "")
        marker = "Codex | "
        if marker in rendered and rendered.split(marker, 1)[1].strip() == target:
            return target
        return ""
    title = agent.get("terminal_title_stripped", agent.get("terminal_title", ""))
    value = str(title) if isinstance(title, str) else ""
    return value.removesuffix(" | codex")


def _effect_sequence_exact(
    spec: "LegacyDisplayReconciliationSpec", observed: Any
) -> bool:
    expected = spec.expected_state_change_seq
    return bool(
        type(expected) is int
        and type(observed) is int
        and observed in {expected, expected + 1}
    )


def _presentation_authority(
    agent: Mapping[str, Any], tokens: Mapping[str, Any], session_source: str | None
) -> str | None:
    for key in ("legacy_display_applies_to", "launch_title_applies_to"):
        owned_authority = tokens.get(key)
        if isinstance(owned_authority, str) and owned_authority:
            return owned_authority
    if (
        tokens.get("identity_title_mode") == "tokens-only"
        and tokens.get("identity_thread_id") == _session_id(agent)
        and tokens.get("harness") == agent.get("agent")
        and isinstance(tokens.get("provider_label"), str)
        and bool(tokens.get("provider_label"))
        and isinstance(tokens.get("sidebar_name"), str)
        and bool(tokens.get("sidebar_name"))
        and isinstance(tokens.get("thread_title"), str)
        and bool(tokens.get("thread_title"))
    ):
        return session_source
    return session_source


@dataclass(frozen=True)
class LegacyDisplayReconciliationSpec:
    assignment_id: str
    expected_version: int
    champion_agent_id: str
    runtime_instance_id: str
    callsign: str
    pane_id: str
    terminal_id: str
    thread_id: str
    worktree: str
    routing_name: str
    expected_presentation_source: str | None
    expected_title: str | None
    expected_state_change_seq: int | None
    target_task_label: str
    owner_authorized: bool
    previous_worktree: str | None = None
    previous_branch: str | None = None
    branch: str | None = None


class HerdrLegacyDisplayAdapter:
    """Compare and patch only one already-running Herdr Champion endpoint."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.environment = dict(environment or os.environ)
        if self.environment.get("HERDR_ENV") != "1":
            raise StorageRefusal(
                "legacy_display_scope_invalid",
                "legacy display reconciliation requires the current Herdr session",
            )

    def _run(
        self,
        arguments: tuple[str, ...],
        label: str,
        *,
        silent: bool = False,
    ) -> dict[str, Any]:
        completed = self.runner.run(arguments, timeout_seconds=30)
        if silent and completed.returncode == 0 and not completed.stdout and not completed.stderr:
            return {}
        return _result_object(completed, label)

    def _agent(self, routing_name: str) -> dict[str, Any]:
        result = self._run(
            ("herdr", "agent", "get", routing_name),
            "legacy Champion inspection",
        )
        return _agent_object(result)

    def _wait_for_plugin_action(self, action: Mapping[str, Any]) -> None:
        log = action.get("log")
        if not isinstance(log, Mapping):
            raise StorageRefusal(
                "legacy_display_unverified",
                "legacy Champion status refresh returned no exact log",
            )
        log_id = log.get("log_id")
        status = log.get("status")
        if not isinstance(log_id, str) or not log_id:
            raise StorageRefusal(
                "legacy_display_unverified",
                "legacy Champion status refresh returned no exact log identity",
            )
        for attempt in range(51):
            if status == "succeeded":
                return
            if status not in {"queued", "running"}:
                raise StorageRefusal(
                    "legacy_display_unverified",
                    "legacy Champion status refresh did not succeed",
                )
            if attempt == 50:
                break
            time.sleep(0.1)
            result = self._run(
                (
                    "herdr",
                    "plugin",
                    "log",
                    "list",
                    "--plugin",
                    "local.tab-status",
                    "--limit",
                    "50",
                ),
                "legacy Champion status refresh log inspection",
            )
            logs = result.get("logs")
            matches = (
                [
                    item
                    for item in logs
                    if isinstance(item, Mapping) and item.get("log_id") == log_id
                ]
                if isinstance(logs, list)
                else []
            )
            if not matches:
                continue
            if len(matches) != 1:
                raise StorageRefusal(
                    "legacy_display_unverified",
                    "legacy Champion status refresh log is ambiguous",
                )
            status = matches[0].get("status")
        raise StorageRefusal(
            "legacy_display_unverified",
            "legacy Champion status refresh did not finish in time",
        )

    def _observe(
        self, spec: LegacyDisplayReconciliationSpec
    ) -> tuple[dict[str, Any], dict[str, str]]:
        agent = self._agent(spec.routing_name)
        tokens = agent.get("tokens")
        session_source = _session_source(agent)
        explicit_source = agent.get("metadata_source")
        source = (
            explicit_source
            if "metadata_source" in agent
            else (
                tokens.get("legacy_display_source")
                if isinstance(tokens, Mapping)
                and isinstance(tokens.get("legacy_display_source"), str)
                and tokens.get("legacy_display_source")
                else session_source
            )
        )
        authority = (
            _presentation_authority(agent, tokens, session_source)
            if isinstance(tokens, Mapping)
            else session_source
        )
        sequence = agent.get("state_change_seq")
        worktree = str(Path(spec.worktree).resolve())
        exact = bool(
            agent.get("agent") == "codex"
            and agent.get("agent_status") in PRESENT_STATUSES
            and agent.get("name") == spec.routing_name
            and agent.get("pane_id") == spec.pane_id
            and agent.get("terminal_id") == spec.terminal_id
            and agent.get("cwd") == worktree
            and agent.get("foreground_cwd") == worktree
            and _session_id(agent) == spec.thread_id
            and THREAD_UUID.fullmatch(spec.thread_id)
            and isinstance(source, str)
            and bool(source)
            and isinstance(authority, str)
            and bool(authority)
            and isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence >= 0
            and isinstance(tokens, Mapping)
        )
        if not exact:
            raise StorageRefusal(
                "legacy_display_identity_unverified",
                "legacy Champion pane, terminal, thread, worktree, or route is not exact",
            )
        token_map = {str(key): str(value) for key, value in tokens.items()}
        projection = {
            "pane_id": spec.pane_id,
            "terminal_id": spec.terminal_id,
            "thread_id": spec.thread_id,
            "worktree": worktree,
            "routing_name": spec.routing_name,
            "presentation_source": source,
            "authority_source": authority,
            "title": _canonical_title(agent),
            "state_change_seq": sequence,
            "tokens_digest": _digest(token_map),
        }
        return projection, token_map

    def _matches_expected(
        self,
        spec: LegacyDisplayReconciliationSpec,
        observation: Mapping[str, Any],
    ) -> bool:
        return bool(
            observation.get("presentation_source")
            == spec.expected_presentation_source
            and observation.get("title") == spec.expected_title
            and observation.get("state_change_seq")
            == spec.expected_state_change_seq
        )

    def _receipt(
        self,
        spec: LegacyDisplayReconciliationSpec,
        reconciliation_id: str,
        observation: Mapping[str, Any],
    ) -> dict[str, Any]:
        target = f"{spec.callsign} · {spec.target_task_label}"
        return {
            "schema": "league.legacy-display-reconciliation.v1",
            "reconciliation_id": reconciliation_id,
            "assignment_id": spec.assignment_id,
            "champion_agent_id": spec.champion_agent_id,
            "runtime_instance_id": spec.runtime_instance_id,
            "source": str(observation["presentation_source"]),
            "applies_to_source": str(observation["authority_source"]),
            "state_change_seq": int(observation["state_change_seq"]),
            "sidebar_name": spec.callsign,
            "task_label": spec.target_task_label,
            "thread_title": target,
            "terminal_title": target,
            "observation_digest": _digest(observation),
        }

    def _reconciliation_tokens(
        self,
        spec: LegacyDisplayReconciliationSpec,
        reconciliation_id: str,
        source: str,
        authority: str,
    ) -> dict[str, str]:
        target = f"{spec.callsign} · {spec.target_task_label}"
        owner = hashlib.sha256(spec.assignment_id.encode("utf-8")).hexdigest()[:16]
        return {
            "callsign": spec.callsign,
            "sidebar_name": spec.callsign,
            "task_label": spec.target_task_label,
            "thread_title": target,
            "identity_title": f"Codex | {target}",
            "identity_title_mode": "tokens-only",
            "legacy_display_owner": owner,
            "legacy_display_assignment": reconciliation_id,
            "legacy_display_source": source,
            "legacy_display_applies_to": authority,
        }

    def _metadata_source(self, reconciliation_id: str) -> str:
        return f"league-legacy-{reconciliation_id.rsplit(':', 1)[-1]}"

    def _owns_overlay(
        self,
        spec: LegacyDisplayReconciliationSpec,
        reconciliation_id: str,
        observation: Mapping[str, Any],
        tokens: Mapping[str, str],
    ) -> bool:
        source = self._metadata_source(reconciliation_id)
        expected = self._reconciliation_tokens(
            spec,
            reconciliation_id,
            source,
            str(observation.get("authority_source", "")),
        )
        return all(tokens.get(key) == value for key, value in expected.items())

    def _clear_owned_overlay(
        self,
        spec: LegacyDisplayReconciliationSpec,
        reconciliation_id: str,
        authority: str,
        sequence: int,
    ) -> None:
        source = self._metadata_source(reconciliation_id)
        owned = self._reconciliation_tokens(
            spec, reconciliation_id, source, authority
        )
        clear_arguments = tuple(
            part for key in owned for part in ("--clear-token", key)
        )
        try:
            self._run(
                (
                    "herdr",
                    "pane",
                    "report-metadata",
                    spec.pane_id,
                    "--source",
                    source,
                    "--applies-to-source",
                    authority,
                    "--agent",
                    "codex",
                    "--clear-title",
                    "--clear-display-agent",
                    *clear_arguments,
                    "--seq",
                    str(max(sequence, _next_metadata_sequence())),
                ),
                "legacy Champion display rollback",
                silent=True,
            )
        except StorageRefusal as exc:
            raise StorageRefusal(
                "legacy_display_unverified",
                "League-owned legacy display overlay could not be cleared",
            ) from exc
        prior: tuple[dict[str, Any], dict[str, str]] | None = None
        stable = 0
        for _ in range(3):
            try:
                observation, tokens = self._observe(spec)
            except StorageRefusal as exc:
                raise StorageRefusal(
                    "legacy_display_unverified",
                    "League-owned legacy display overlay clearance could not be observed",
                ) from exc
            current = (observation, tokens)
            exact = bool(
                observation["presentation_source"] != source
                and not LEGACY_OWNERSHIP_TOKENS.intersection(tokens)
            )
            if exact:
                stable = stable + 1 if current == prior else 1
                prior = current
                if stable == 2:
                    return
            else:
                stable = 0
                prior = None
            time.sleep(0.1)
        raise StorageRefusal(
            "legacy_display_unverified",
            "League-owned legacy display overlay did not clear exactly",
        )

    def _pending_effect_exact(
        self,
        spec: LegacyDisplayReconciliationSpec,
        reconciliation_id: str,
        observation: Mapping[str, Any],
        tokens: Mapping[str, str],
    ) -> bool:
        target = f"{spec.callsign} · {spec.target_task_label}"
        source = self._metadata_source(reconciliation_id)
        expected = self._reconciliation_tokens(
            spec,
            reconciliation_id,
            source,
            str(observation.get("authority_source", "")),
        )
        return bool(
            observation.get("presentation_source") == source
            and observation.get("title") == target
            and _effect_sequence_exact(spec, observation.get("state_change_seq"))
            and all(tokens.get(key) == value for key, value in expected.items())
            and not (OWNERSHIP_TOKENS - set(expected)).intersection(tokens)
        )

    def reconcile(
        self,
        spec: LegacyDisplayReconciliationSpec,
        reconciliation_id: str,
    ) -> dict[str, Any]:
        baseline, baseline_tokens = self._observe(spec)
        if not self._matches_expected(spec, baseline):
            if self._pending_effect_exact(
                spec, reconciliation_id, baseline, baseline_tokens
            ):
                observed, tokens = self._observe(spec)
                if observed == baseline and tokens == baseline_tokens:
                    return self._receipt(spec, reconciliation_id, observed)
            if self._owns_overlay(
                spec, reconciliation_id, baseline, baseline_tokens
            ):
                self._clear_owned_overlay(
                    spec,
                    reconciliation_id,
                    str(baseline["authority_source"]),
                    int(baseline["state_change_seq"]) + 1,
                )
            raise StorageRefusal(
                "legacy_display_race",
                "legacy Champion presentation changed after owner authorization",
            )
        if OWNERSHIP_TOKENS.intersection(baseline_tokens):
            raise StorageRefusal(
                "legacy_display_ambiguous",
                "legacy Champion already exposes display ownership metadata",
            )

        # A second fresh read is the ordering barrier. Herdr sequences are scoped
        # per metadata source, so the effect uses a fresh process-monotonic value
        # on League's dedicated source. The agent state sequence remains the
        # independent guard for interleaved native presentation changes.
        current, current_tokens = self._observe(spec)
        if current != baseline or current_tokens != baseline_tokens:
            raise StorageRefusal(
                "legacy_display_race",
                "legacy Champion presentation changed before reconciliation",
            )
        target = f"{spec.callsign} · {spec.target_task_label}"
        source = self._metadata_source(reconciliation_id)
        authority = str(current["authority_source"])
        sequence = _next_metadata_sequence()
        reconciliation_tokens = self._reconciliation_tokens(
            spec, reconciliation_id, source, authority
        )
        token_only_status = current_tokens.get("identity_title_mode") == "tokens-only"
        token_arguments = tuple(
            part
            for key, value in reconciliation_tokens.items()
            for part in ("--token", f"{key}={value}")
        )
        report = (
            "herdr",
            "pane",
            "report-metadata",
            spec.pane_id,
            "--source",
            source,
            "--applies-to-source",
            authority,
            "--agent",
            "codex",
            "--display-agent",
            "codex",
            *(("--clear-title",) if token_only_status else ("--title", target)),
            *token_arguments,
            "--seq",
            str(sequence),
        )
        try:
            self._run(
                report,
                "legacy Champion display reconciliation",
                silent=True,
            )
        except StorageRefusal as exc:
            observed, tokens = self._observe(spec)
            if observed != baseline or tokens != baseline_tokens:
                raise StorageRefusal(
                    "legacy_display_race",
                    "legacy Champion presentation changed during reconciliation",
                ) from exc
            raise StorageRefusal(
                "legacy_display_unverified",
                "legacy Champion compare-and-set metadata write was refused",
            ) from exc
        if token_only_status:
            try:
                action = self._run(
                    (
                        "herdr",
                        "plugin",
                        "action",
                        "invoke",
                        "local.tab-status.sync",
                    ),
                    "legacy Champion status presentation refresh",
                    silent=True,
                )
                self._wait_for_plugin_action(action)
            except StorageRefusal as exc:
                observed, tokens = self._observe(spec)
                if self._owns_overlay(
                    spec, reconciliation_id, observed, tokens
                ):
                    self._clear_owned_overlay(
                        spec,
                        reconciliation_id,
                        authority,
                        max(sequence + 1, int(observed["state_change_seq"]) + 1),
                    )
                raise StorageRefusal(
                    "legacy_display_unverified",
                    "legacy Champion status presentation refresh failed",
                ) from exc
        expected_tokens = {
            **baseline_tokens,
            **reconciliation_tokens,
        }
        prior_digest: str | None = None
        stable = 0
        final: dict[str, Any] | None = None
        observed = current
        observation_error: StorageRefusal | None = None
        for _ in range(3):
            try:
                observed, tokens = self._observe(spec)
            except StorageRefusal as exc:
                observation_error = exc
                break
            exact = bool(
                observed["presentation_source"] == source
                and observed["authority_source"] == authority
                and observed["title"] == target
                and _effect_sequence_exact(spec, observed["state_change_seq"])
                and tokens == expected_tokens
            )
            if not exact:
                break
            digest = _digest(observed)
            stable = stable + 1 if digest == prior_digest else 1
            prior_digest = digest
            final = observed
            if stable == 2:
                break
            time.sleep(0.1)
        if stable != 2 or final is None:
            self._clear_owned_overlay(
                spec,
                reconciliation_id,
                authority,
                max(sequence + 1, int(observed["state_change_seq"]) + 1),
            )
            if observation_error is not None:
                raise StorageRefusal(
                    "legacy_display_unverified",
                    "legacy Champion final display observation failed after the owned effect",
                ) from observation_error
            raise StorageRefusal(
                "legacy_display_race",
                "legacy Champion presentation changed during reconciliation",
            )
        return self._receipt(spec, reconciliation_id, final)

    def verify_receipt(
        self,
        spec: LegacyDisplayReconciliationSpec,
        receipt: Mapping[str, Any],
    ) -> None:
        observation, tokens = self._observe(spec)
        target = f"{spec.callsign} · {spec.target_task_label}"
        exact = bool(
            observation["presentation_source"] == receipt.get("source")
            and observation["authority_source"] == receipt.get("applies_to_source")
            and observation["state_change_seq"] == receipt.get("state_change_seq")
            and observation["title"] == target
            and _digest(observation) == receipt.get("observation_digest")
            and tokens.get("sidebar_name") == spec.callsign
            and tokens.get("task_label") == spec.target_task_label
            and tokens.get("thread_title") == target
            and tokens.get("legacy_display_assignment")
            == receipt.get("reconciliation_id")
            and tokens.get("legacy_display_source") == receipt.get("source")
            and tokens.get("legacy_display_applies_to")
            == receipt.get("applies_to_source")
        )
        if not exact:
            raise StorageRefusal(
                "legacy_display_race",
                "completed legacy display reconciliation no longer matches the live endpoint",
            )


class LegacyDisplayReconciliationService:
    def __init__(self, store: Storage, adapter: HerdrLegacyDisplayAdapter, clock: Any) -> None:
        self.store = store
        self.adapter = adapter
        self.clock = clock

    def _command(
        self, spec: LegacyDisplayReconciliationSpec
    ) -> LegacyDisplayReconciliationCommand:
        return LegacyDisplayReconciliationCommand(**vars(spec), at=self.clock.now())

    def reconcile(self, spec: LegacyDisplayReconciliationSpec) -> dict[str, Any]:
        command = self._command(spec)
        intent = self.store.begin_legacy_display_reconciliation(command)
        if intent["receipt"] is not None:
            self.adapter.verify_receipt(spec, intent["receipt"])
            return {
                "assignment_id": spec.assignment_id,
                "reconciliation_id": intent["reconciliation_id"],
                "state": "reconciled",
                "receipt": intent["receipt"],
                "idempotent": True,
            }
        receipt = self.adapter.reconcile(spec, intent["reconciliation_id"])
        return self.store.finalize_legacy_display_reconciliation(
            command, receipt, self.clock.now()
        )
