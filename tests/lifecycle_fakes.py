"""Deterministic clocks, IDs, and adapters for request-lifecycle tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from league.issue_first import issue_scope_digest, normalize_issue_title, semantic_scope_digest
from league.request_services import (
    AssignmentSpec,
    DeliveryReceipt,
    DeliveryUnavailable,
    LaunchAdapterError,
)
from league.sqlite_project_ops import canonical_repository
from league.storage_issue import BeginIssueSelectionCommand, CompleteIssueSelectionCommand


SYNTHETIC_ISSUE_REPOSITORY = "https://example.invalid/league.git"
SYNTHETIC_ISSUE_TITLE = "Synthetic visible Champion assignment"
SYNTHETIC_ISSUE_BODY = """## Objective
Exercise one deterministic visible Champion assignment.

## Verification
Prove exact issue-first assignment and lifecycle behavior.

## Hard boundaries
Use only temporary SQLite state and deterministic adapters.
"""


def issue_bound_spec(
    store: Any,
    spec: AssignmentSpec,
    at: str,
    *,
    repository: str | None = None,
) -> AssignmentSpec:
    """Persist deterministic issue-selection evidence for one synthetic Champion."""
    repository = repository or SYNTHETIC_ISSUE_REPOSITORY
    repository_key = canonical_repository(repository)[1]
    issue_title = spec.task_summary
    normalized_title = normalize_issue_title(issue_title)
    scope_digest = semantic_scope_digest(SYNTHETIC_ISSUE_BODY)
    selection_identity = "\0".join(
        (repository_key, normalized_title, scope_digest)
    ).encode("utf-8")
    selection_key = f"issue-scope:{hashlib.sha256(selection_identity).hexdigest()}"
    owner_attempt_id = f"attempt:{spec.task_id}"
    begun = store.begin_issue_selection(
        BeginIssueSelectionCommand(
            selection_key=selection_key,
            task_id=spec.task_id,
            task_summary=spec.task_summary,
            coordinator_agent_id=spec.coordinator_agent_id,
            repository=repository,
            repository_key=repository_key,
            normalized_title=normalized_title,
            semantic_scope_digest=scope_digest,
            owner_attempt_id=owner_attempt_id,
            lease_expires_at="2099-01-01T00:00:00Z",
            at=at,
        )
    )
    selection = store.complete_issue_selection(
        CompleteIssueSelectionCommand(
            selection_key=selection_key,
            expected_version=begun["version"],
            owner_attempt_id=owner_attempt_id,
            task_id=spec.task_id,
            task_summary=spec.task_summary,
            coordinator_agent_id=spec.coordinator_agent_id,
            repository=repository,
            repository_key=repository_key,
            normalized_title=normalized_title,
            semantic_scope_digest=scope_digest,
            decision="reuse_open",
            issue=spec.issue,
            issue_url=f"https://{repository_key}/issues/{spec.issue}",
            issue_title=issue_title,
            issue_body_digest=hashlib.sha256(
                SYNTHETIC_ISSUE_BODY.encode("utf-8")
            ).hexdigest(),
            duplicate_matches=1,
            reopen_action_receipt_digest=None,
            at=at,
        )
    )
    bound = replace(spec, repository=repository)
    receipt = {
        "schema": "league.repository-issue.v1",
        "repository": repository,
        "repository_key": repository_key,
        "issue": bound.issue,
        "issue_url": f"https://{repository_key}/issues/{bound.issue}",
        "issue_state": "open",
        "issue_title": issue_title,
        "normalized_title": normalized_title,
        "issue_body_digest": hashlib.sha256(
            SYNTHETIC_ISSUE_BODY.encode("utf-8")
        ).hexdigest(),
        "semantic_scope_digest": scope_digest,
        "task_scope_digest": issue_scope_digest(
            repository, bound.issue, bound.task_id, bound.task_summary
        ),
        "issue_selection_receipt_digest": selection["receipt_digest"],
        "verifier_kind": (
            "synthetic-fixture"
            if repository_key.partition("/")[0].endswith(".invalid")
            else "github-api"
        ),
        "verified_at": at,
    }
    receipt["receipt_digest"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return replace(bound, issue_receipt=receipt)


class FakeClock:
    def __init__(self, value: str = "2026-01-01T01:00:00Z") -> None:
        self.value = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    def now(self) -> str:
        return self.value.isoformat(timespec="seconds").replace("+00:00", "Z")

    def after(self, seconds: int) -> str:
        return (self.value + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )

    def advance(self, seconds: int) -> str:
        self.value += timedelta(seconds=seconds)
        return self.now()


class FakeIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def new(self, kind: str) -> str:
        value = self.counts.get(kind, 0) + 1
        self.counts[kind] = value
        return f"{kind}-fake-{value}"


class FakeLaunchAdapter:
    def __init__(self, *, failure: Optional[LaunchAdapterError] = None) -> None:
        self.failure = failure
        self.calls: list[AssignmentSpec] = []

    def launch(self, spec: AssignmentSpec) -> dict[str, Any]:
        self.calls.append(spec)
        if self.failure is not None:
            raise self.failure
        return {
            "verified": True,
            "assignment_id": spec.assignment_id,
            "task_id": spec.task_id,
            "champion_agent_id": spec.champion_agent_id,
            "callsign": spec.callsign,
            "runtime_instance_id": f"runtime:{spec.champion_agent_id}",
            "thread_id": f"thread:{spec.champion_agent_id}",
            "endpoint": f"synthetic:{spec.callsign.lower()}",
            "runtime_generation": f"generation:{spec.champion_agent_id}",
            "harness_kind": "codex-thread",
            "backend_kind": "herdr",
            "routing_name": spec.callsign.lower(),
            "display_agent": "codex",
            "repository": spec.repository,
            "issue": spec.issue,
            "branch": spec.branch,
            "worktree": spec.worktree,
            "capabilities": list(spec.required_capabilities),
        }


@dataclass
class SentEnvelope:
    channel: str
    target: dict[str, Any]
    envelope: dict[str, Any]


class FakeDeliveryAdapter:
    def __init__(
        self,
        *,
        unavailable: bool = False,
        mismatch_event_id: Optional[str] = None,
    ) -> None:
        self.unavailable = unavailable
        self.mismatch_event_id = mismatch_event_id
        self.sent: list[SentEnvelope] = []

    def send(
        self,
        channel: str,
        target: dict[str, Any],
        envelope: dict[str, Any],
    ) -> DeliveryReceipt:
        self.sent.append(SentEnvelope(channel, dict(target), dict(envelope)))
        if self.unavailable:
            raise DeliveryUnavailable("synthetic closed endpoint")
        return DeliveryReceipt(
            outbox_id=envelope["outbox_id"],
            event_id=self.mismatch_event_id or envelope["event_id"],
            recipient_agent_id=envelope["recipient_agent_id"],
            effect_kind="inbox_event",
            effect_id=f"effect:{envelope['event_id']}",
        )
