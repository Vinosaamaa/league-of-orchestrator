"""Verify and bind one public repository issue before visible implementation launch."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .sqlite_project_ops import canonical_repository
from .storage_types import StorageRefusal


ISSUE_RECEIPT_SCHEMA = "league.repository-issue.v1"


class CommandRunner(Protocol):
    def run(
        self, arguments: Sequence[str], *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]: ...


class IssueSpec(Protocol):
    task_id: str
    task_summary: str
    repository: str
    issue: int


class IssueVerifier(Protocol):
    def verify(self, spec: IssueSpec, at: str) -> dict[str, Any]: ...


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def issue_scope_digest(
    repository: str, issue: int, task_id: str, task_summary: str
) -> str:
    return _digest(
        {
            "repository_key": canonical_repository(repository)[1],
            "issue": issue,
            "task_id": task_id,
            "task_summary": task_summary,
        }
    )


def _github_repository(repository: str) -> tuple[str, str]:
    exact, key = canonical_repository(repository)
    del exact
    host, _, path = key.partition("/")
    parts = path.split("/")
    if host != "github.com" or len(parts) != 2:
        raise StorageRefusal(
            "issue_provider_unsupported",
            "issue-first verification currently requires one exact GitHub repository",
        )
    return parts[0], parts[1]


def build_issue_receipt(
    spec: IssueSpec,
    payload: Mapping[str, Any],
    at: str,
    *,
    reopen_action_receipt_digest: str | None = None,
) -> dict[str, Any]:
    owner, repository_name = _github_repository(spec.repository)
    if payload.get("pull_request") is not None:
        raise StorageRefusal("issue_identity_refused", "pull requests are not issue containers")
    try:
        number = int(payload["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageRefusal("issue_identity_refused", "issue response has no exact number") from exc
    state = payload.get("state")
    title = payload.get("title")
    body = payload.get("body")
    issue_url = payload.get("html_url")
    repository_url = payload.get("repository_url")
    expected_api = f"https://api.github.com/repos/{owner}/{repository_name}"
    expected_url = f"https://github.com/{owner}/{repository_name}/issues/{spec.issue}"
    if (
        number != spec.issue
        or state not in {"open", "closed"}
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(body, str)
        or not body.strip()
        or issue_url != expected_url
        or repository_url != expected_api
    ):
        raise StorageRefusal(
            "issue_identity_refused", "issue response does not match the exact repository issue"
        )
    if title.strip() != spec.task_summary.strip():
        raise StorageRefusal(
            "issue_scope_mismatch", "task summary must match the exact repository issue title"
        )
    headings = body.casefold()
    scope_recorded = any(
        marker in headings for marker in ("## objective", "## what", "## scope")
    )
    acceptance_recorded = any(
        marker in headings for marker in ("## acceptance", "## verification")
    )
    authority_recorded = any(
        marker in headings
        for marker in ("## hard boundaries", "## authority", "## safety", "## scope")
    )
    if not (scope_recorded and acceptance_recorded and authority_recorded):
        raise StorageRefusal(
            "issue_scope_incomplete",
            "repository issue must record scope, acceptance, and authority boundaries",
        )
    if state == "closed":
        raise StorageRefusal(
            "issue_closed",
            "closed issue requires an authorized reopen followed by an exact open-issue retry",
        )
    if reopen_action_receipt_digest is not None and not (
        isinstance(reopen_action_receipt_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", reopen_action_receipt_digest)
    ):
        raise StorageRefusal("issue_receipt_invalid", "issue reopen receipt digest is invalid")
    receipt = {
        "schema": ISSUE_RECEIPT_SCHEMA,
        "repository": spec.repository,
        "repository_key": canonical_repository(spec.repository)[1],
        "issue": spec.issue,
        "issue_url": issue_url,
        "issue_state": state,
        "issue_title": title.strip(),
        "issue_body_digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "task_scope_digest": issue_scope_digest(
            spec.repository, spec.issue, spec.task_id, spec.task_summary
        ),
        "reopen_action_receipt_digest": reopen_action_receipt_digest,
        "verifier_kind": "github-api",
        "verified_at": at,
    }
    receipt["receipt_digest"] = _digest(receipt)
    return receipt


def validate_issue_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "repository",
        "repository_key",
        "issue",
        "issue_url",
        "issue_state",
        "issue_title",
        "issue_body_digest",
        "task_scope_digest",
        "reopen_action_receipt_digest",
        "verifier_kind",
        "verified_at",
        "receipt_digest",
    }
    if set(receipt) != required or receipt.get("schema") != ISSUE_RECEIPT_SCHEMA:
        raise StorageRefusal("issue_receipt_invalid", "issue receipt schema or fields are invalid")
    value = dict(receipt)
    observed = value.pop("receipt_digest", None)
    if not isinstance(observed, str) or observed != _digest(value):
        raise StorageRefusal("issue_receipt_invalid", "issue receipt digest does not match its fields")
    if value.get("repository_key") != canonical_repository(str(value.get("repository")))[1]:
        raise StorageRefusal("issue_receipt_invalid", "issue receipt repository key is invalid")
    issue = value.get("issue")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise StorageRefusal("issue_receipt_invalid", "issue receipt number is invalid")
    if value.get("issue_state") != "open" or value.get("verifier_kind") != "github-api":
        raise StorageRefusal("issue_receipt_invalid", "issue receipt state or verifier is invalid")
    if not isinstance(value.get("issue_title"), str) or not value["issue_title"].strip():
        raise StorageRefusal("issue_receipt_invalid", "issue receipt title is invalid")
    for key in ("issue_body_digest", "task_scope_digest"):
        if not isinstance(value.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise StorageRefusal("issue_receipt_invalid", "issue receipt digest field is invalid")
    reopen_digest = value.get("reopen_action_receipt_digest")
    if reopen_digest is not None and not (
        isinstance(reopen_digest, str) and re.fullmatch(r"[0-9a-f]{64}", reopen_digest)
    ):
        raise StorageRefusal("issue_receipt_invalid", "issue reopen receipt digest is invalid")
    value["receipt_digest"] = observed
    return value


class GitHubIssueVerifier:
    """Read one issue through GitHub's owner API before any launch mutation."""

    def __init__(
        self,
        runner: CommandRunner,
        *,
        command: str = "gh",
        reopen_action_receipt_digest: str | None = None,
    ) -> None:
        self.runner = runner
        self.command = command
        self.reopen_action_receipt_digest = reopen_action_receipt_digest

    def verify(self, spec: IssueSpec, at: str) -> dict[str, Any]:
        owner, repository_name = _github_repository(spec.repository)
        completed = self.runner.run(
            (
                self.command,
                "api",
                "--method",
                "GET",
                f"repos/{owner}/{repository_name}/issues/{spec.issue}",
            ),
            timeout_seconds=30,
        )
        if completed.returncode != 0:
            raise StorageRefusal(
                "issue_verification_failed", "repository issue could not be verified at GitHub"
            )
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageRefusal(
                "issue_verification_failed", "repository issue response was malformed"
            ) from exc
        if not isinstance(payload, dict):
            raise StorageRefusal(
                "issue_verification_failed", "repository issue response was malformed"
            )
        return build_issue_receipt(
            spec,
            payload,
            at,
            reopen_action_receipt_digest=self.reopen_action_receipt_digest,
        )
