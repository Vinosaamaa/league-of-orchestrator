"""Verify and bind one public repository issue before visible implementation launch."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, Sequence

from .privacy import validate_final_rendered_payload
from .sqlite_project_ops import canonical_repository
from .storage_issue import BeginIssueSelectionCommand, CompleteIssueSelectionCommand
from .storage_types import StorageRefusal


ISSUE_RECEIPT_SCHEMA = "league.repository-issue.v1"
MAX_ISSUE_BODY_BYTES = 262_144
URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)


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


@dataclass(frozen=True)
class IssueSelectionSpec:
    task_id: str
    task_summary: str
    coordinator_agent_id: str
    repository: str
    issue_title: str
    issue_body: str


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


def _normalized_words(value: str, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise StorageRefusal("issue_selection_invalid", f"{label} is invalid")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))
    if not normalized:
        raise StorageRefusal("issue_selection_invalid", f"{label} is invalid")
    return normalized


def normalize_issue_title(value: str) -> str:
    return _normalized_words(value, "issue title", maximum=512)


def _issue_body_contract(body: str) -> tuple[str, str]:
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > MAX_ISSUE_BODY_BYTES:
        raise StorageRefusal("issue_scope_incomplete", "issue body is empty or exceeds its bound")
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        heading = normalize_issue_title(match.group(1))
        sections.setdefault(heading, body[start:end].strip())
    scope = next(
        (sections[name] for name in ("objective", "scope", "what") if sections.get(name)),
        None,
    )
    acceptance = next(
        (sections[name] for name in ("acceptance", "verification") if sections.get(name)),
        None,
    )
    authority = next(
        (
            sections[name]
            for name in ("hard boundaries", "authority", "safety")
            if sections.get(name)
        ),
        None,
    )
    if scope is None or acceptance is None or authority is None:
        raise StorageRefusal(
            "issue_scope_incomplete",
            "repository issue must record scope, acceptance, and authority boundaries",
        )
    normalized_scope = _normalized_words(scope, "semantic issue scope", maximum=65_536)
    return normalized_scope, hashlib.sha256(normalized_scope.encode("utf-8")).hexdigest()


def semantic_scope_digest(body: str) -> str:
    return _issue_body_contract(body)[1]


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
    selection_receipt_digest: str,
) -> dict[str, Any]:
    owner, repository_name = _github_repository(spec.repository)
    if payload.get("pull_request") is not None:
        raise StorageRefusal("issue_identity_refused", "pull requests are not issue containers")
    try:
        number = payload["number"]
    except KeyError as exc:
        raise StorageRefusal("issue_identity_refused", "issue response has no exact number") from exc
    state = payload.get("state")
    title = payload.get("title")
    body = payload.get("body")
    issue_url = payload.get("html_url")
    repository_url = payload.get("repository_url")
    expected_api = f"https://api.github.com/repos/{owner}/{repository_name}"
    expected_url = f"https://github.com/{owner}/{repository_name}/issues/{spec.issue}"
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or number != spec.issue
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
    _, scope_digest = _issue_body_contract(body)
    if state == "closed":
        raise StorageRefusal(
            "issue_closed",
            "closed issue requires an authorized reopen followed by an exact open-issue retry",
        )
    if not isinstance(selection_receipt_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", selection_receipt_digest
    ):
        raise StorageRefusal("issue_selection_unproven", "issue selection receipt is invalid")
    receipt = {
        "schema": ISSUE_RECEIPT_SCHEMA,
        "repository": spec.repository,
        "repository_key": canonical_repository(spec.repository)[1],
        "issue": spec.issue,
        "issue_url": issue_url,
        "issue_state": state,
        "issue_title": title.strip(),
        "normalized_title": normalize_issue_title(title),
        "issue_body_digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "semantic_scope_digest": scope_digest,
        "task_scope_digest": issue_scope_digest(
            spec.repository, spec.issue, spec.task_id, spec.task_summary
        ),
        "issue_selection_receipt_digest": selection_receipt_digest,
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
        "normalized_title",
        "issue_body_digest",
        "semantic_scope_digest",
        "task_scope_digest",
        "issue_selection_receipt_digest",
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
    if value.get("normalized_title") != normalize_issue_title(value["issue_title"]):
        raise StorageRefusal("issue_receipt_invalid", "issue receipt normalized title is invalid")
    for key in (
        "issue_body_digest",
        "semantic_scope_digest",
        "task_scope_digest",
        "issue_selection_receipt_digest",
    ):
        if not isinstance(value.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise StorageRefusal("issue_receipt_invalid", "issue receipt digest field is invalid")
    value["receipt_digest"] = observed
    return value


class GitHubIssueVerifier:
    """Read one issue through GitHub's owner API before any launch mutation."""

    def __init__(
        self,
        runner: CommandRunner,
        *,
        command: str = "gh",
        selection_receipt_digest: str,
    ) -> None:
        self.runner = runner
        self.command = command
        self.selection_receipt_digest = selection_receipt_digest

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
            selection_receipt_digest=self.selection_receipt_digest,
        )


class GitHubIssueSelectionService:
    """Search, select, reopen, or create one issue behind a durable scope lease."""

    def __init__(self, store: Any, runner: CommandRunner, *, command: str = "gh") -> None:
        self.store = store
        self.runner = runner
        self.command = command

    def _run_json(
        self,
        arguments: Sequence[str],
        refusal_code: str,
        *,
        input_value: Mapping[str, Any] | None = None,
    ) -> Any:
        if input_value is None:
            completed = self.runner.run(arguments, timeout_seconds=30)
        else:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="league-issue-", suffix=".json"
            ) as handle:
                json.dump(input_value, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                completed = self.runner.run(
                    (*arguments, "--input", handle.name), timeout_seconds=30
                )
        if completed.returncode != 0:
            raise StorageRefusal(refusal_code, "GitHub issue operation failed")
        try:
            return json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageRefusal(refusal_code, "GitHub issue response was malformed") from exc

    @staticmethod
    def _candidate(payload: Mapping[str, Any], owner: str, repository_name: str) -> dict[str, Any]:
        try:
            number = payload["number"]
        except KeyError as exc:
            raise StorageRefusal(
                "issue_selection_search_failed", "GitHub issue search returned invalid identity"
            ) from exc
        state = payload.get("state")
        title = payload.get("title")
        body = payload.get("body")
        url = payload.get("html_url")
        expected_url = f"https://github.com/{owner}/{repository_name}/issues/{number}"
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or state not in {"open", "closed"}
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(body, str)
            or not body.strip()
            or url != expected_url
        ):
            raise StorageRefusal(
                "issue_selection_search_failed", "GitHub issue search returned invalid fields"
            )
        return {
            "number": number,
            "state": state,
            "title": title.strip(),
            "body": body,
            "html_url": url,
        }

    def _all_issues(self, owner: str, repository_name: str) -> list[dict[str, Any]]:
        payload = self._run_json(
            (
                self.command,
                "api",
                "--method",
                "GET",
                "--paginate",
                "--slurp",
                f"repos/{owner}/{repository_name}/issues?state=all&per_page=100",
            ),
            "issue_selection_search_failed",
        )
        if not isinstance(payload, list):
            raise StorageRefusal(
                "issue_selection_search_failed", "GitHub issue search was not a bounded list"
            )
        pages = payload if payload and all(isinstance(page, list) for page in payload) else [payload]
        if len(pages) > 100:
            raise StorageRefusal(
                "issue_selection_search_failed", "GitHub issue search exceeded its page bound"
            )
        issues: list[dict[str, Any]] = []
        for page in pages:
            for raw in page:
                if not isinstance(raw, dict):
                    raise StorageRefusal(
                        "issue_selection_search_failed", "GitHub issue search item was malformed"
                    )
                if raw.get("pull_request") is not None:
                    continue
                issues.append(self._candidate(raw, owner, repository_name))
        return issues

    def select(
        self,
        spec: IssueSelectionSpec,
        owner_attempt_id: str,
        at: str,
        *,
        reopen_action_receipt_digest: str | None = None,
    ) -> dict[str, Any]:
        owner, repository_name = _github_repository(spec.repository)
        repository_key = canonical_repository(spec.repository)[1]
        normalized_title = normalize_issue_title(spec.issue_title)
        _, scope_digest = _issue_body_contract(spec.issue_body)
        approved_urls = tuple(match.group(0).rstrip(".,);]") for match in URL.finditer(spec.issue_body))
        validate_final_rendered_payload(
            f"{spec.issue_title}\n{spec.issue_body}",
            destination_visibility="public",
            approved_urls=approved_urls,
            field="issue.selection_candidate",
        )
        selection_identity = "\0".join(
            (repository_key, normalized_title, scope_digest)
        ).encode("utf-8")
        selection_key = f"issue-scope:{hashlib.sha256(selection_identity).hexdigest()}"
        try:
            instant = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise StorageRefusal("issue_selection_invalid", "selection time is invalid") from exc
        if instant.tzinfo is None:
            raise StorageRefusal("issue_selection_invalid", "selection time needs a timezone")
        lease_expires = (instant.astimezone(timezone.utc) + timedelta(seconds=120)).isoformat().replace(
            "+00:00", "Z"
        )
        acquired = self.store.begin_issue_selection(
            BeginIssueSelectionCommand(
                selection_key=selection_key,
                task_id=spec.task_id,
                task_summary=spec.task_summary,
                coordinator_agent_id=spec.coordinator_agent_id,
                repository=spec.repository,
                repository_key=repository_key,
                normalized_title=normalized_title,
                semantic_scope_digest=scope_digest,
                owner_attempt_id=owner_attempt_id,
                lease_expires_at=lease_expires,
                at=at,
            )
        )
        if acquired["state"] == "completed":
            return acquired["receipt"]
        version = int(acquired["version"])
        try:
            equivalents = []
            for candidate in self._all_issues(owner, repository_name):
                if normalize_issue_title(candidate["title"]) != normalized_title:
                    continue
                try:
                    candidate_scope = semantic_scope_digest(candidate["body"])
                except StorageRefusal as exc:
                    if exc.code == "issue_scope_incomplete":
                        raise StorageRefusal(
                            "equivalent_issue_incomplete",
                            "equivalent issue lacks required scope, acceptance, or authority",
                        ) from exc
                    raise
                if candidate_scope == scope_digest:
                    equivalents.append(candidate)
            open_matches = sorted(
                (candidate for candidate in equivalents if candidate["state"] == "open"),
                key=lambda candidate: candidate["number"],
            )
            closed_matches = sorted(
                (candidate for candidate in equivalents if candidate["state"] == "closed"),
                key=lambda candidate: candidate["number"],
            )
            reopen_digest = None
            if open_matches:
                selected = open_matches[0]
                if reopen_action_receipt_digest is None:
                    decision = "reuse_open"
                else:
                    self.store.verify_issue_reopen_authority(
                        reopen_action_receipt_digest,
                        spec.coordinator_agent_id,
                        spec.repository,
                        int(selected["number"]),
                    )
                    reopen_digest = reopen_action_receipt_digest
                    decision = "reopen_closed"
            elif closed_matches:
                selected = closed_matches[0]
                if reopen_action_receipt_digest is None:
                    raise StorageRefusal(
                        "issue_reopen_required",
                        "equivalent closed issue requires exact Shotcaller reopen authority",
                    )
                self.store.verify_issue_reopen_authority(
                    reopen_action_receipt_digest,
                    spec.coordinator_agent_id,
                    spec.repository,
                    int(selected["number"]),
                )
                raise StorageRefusal(
                    "issue_reopen_not_observed",
                    "reopen receipt exists but the owner API still reports the issue closed",
                )
            else:
                raw = self._run_json(
                    (
                        self.command,
                        "api",
                        "--method",
                        "POST",
                        f"repos/{owner}/{repository_name}/issues",
                    ),
                    "issue_creation_failed",
                    input_value={"title": spec.issue_title, "body": spec.issue_body},
                )
                if not isinstance(raw, dict):
                    raise StorageRefusal("issue_creation_failed", "created issue response was malformed")
                selected = self._candidate(raw, owner, repository_name)
                if (
                    selected["state"] != "open"
                    or normalize_issue_title(selected["title"]) != normalized_title
                    or semantic_scope_digest(selected["body"]) != scope_digest
                ):
                    raise StorageRefusal(
                        "issue_creation_failed", "GitHub did not return the exact created issue"
                    )
                decision = "create_distinct"
            return self.store.complete_issue_selection(
                CompleteIssueSelectionCommand(
                    selection_key=selection_key,
                    expected_version=version,
                    owner_attempt_id=owner_attempt_id,
                    task_id=spec.task_id,
                    task_summary=spec.task_summary,
                    coordinator_agent_id=spec.coordinator_agent_id,
                    repository=spec.repository,
                    repository_key=repository_key,
                    normalized_title=normalized_title,
                    semantic_scope_digest=scope_digest,
                    decision=decision,
                    issue=int(selected["number"]),
                    issue_url=str(selected["html_url"]),
                    issue_title=str(selected["title"]),
                    issue_body_digest=hashlib.sha256(
                        str(selected["body"]).encode("utf-8")
                    ).hexdigest(),
                    duplicate_matches=len(equivalents),
                    reopen_action_receipt_digest=reopen_digest,
                    at=at,
                )
            )
        except BaseException as original:
            try:
                self.store.release_issue_selection(
                    selection_key, owner_attempt_id, version, at
                )
            except StorageRefusal as release_error:
                raise release_error from original
            raise
