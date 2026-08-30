"""Issue-close/reopen adapters and fenced exact-thread continuation service."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from .storage import Storage, StorageRefusal


MAX_ISSUE_OUTPUT_BYTES = 256 * 1024
_REPOSITORY = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")


class IssueCommandRunner(Protocol):
    def run(
        self, arguments: Sequence[str], *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessIssueRunner:
    def run(
        self, arguments: Sequence[str], *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]:
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.run(
                    list(arguments),
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout_seconds,
                    check=False,
                )
                values: list[str] = []
                for stream in (stdout, stderr):
                    if stream.tell() > MAX_ISSUE_OUTPUT_BYTES:
                        raise StorageRefusal(
                            "issue_action_failed", "issue adapter output exceeded its bound", retryable=True
                        )
                    stream.seek(0)
                    values.append(stream.read().decode("utf-8"))
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
            raise StorageRefusal(
                "issue_action_failed", "issue adapter command did not complete", retryable=True
            ) from exc
        return subprocess.CompletedProcess(
            list(arguments), process.returncode, values[0], values[1]
        )


def _issue_identity(value: Mapping[str, Any]) -> tuple[str, int, str]:
    repository = value.get("repository")
    issue = value.get("issue")
    state = value.get("state")
    match = _REPOSITORY.fullmatch(str(repository))
    if (
        set(value) != {"repository", "issue", "state"}
        or match is None
        or isinstance(issue, bool)
        or not isinstance(issue, int)
        or issue < 1
        or state not in {"open", "closed"}
    ):
        raise StorageRefusal("issue_binding_mismatch", "issue action identity is invalid")
    return f"{match.group(1)}/{match.group(2)}", issue, state


class GitHubIssueAdapter:
    """Exact GitHub issue state adapter with bounded, receipt-bearing retries."""

    kind = "issue"

    def __init__(
        self,
        runner: Optional[IssueCommandRunner] = None,
        *,
        executable: Optional[str] = None,
    ) -> None:
        self.runner = runner or SubprocessIssueRunner()
        self.executable = executable or shutil.which("gh-axi") or "gh-axi"

    def inspect(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        expected = action.get("expected_identity")
        intended = action.get("intended_state")
        if not isinstance(expected, Mapping) or not isinstance(intended, Mapping):
            raise StorageRefusal("issue_binding_mismatch", "issue action has no exact identity")
        repository, issue, _ = _issue_identity(expected)
        intended_repository, intended_issue, _ = _issue_identity(intended)
        if repository != intended_repository or issue != intended_issue:
            raise StorageRefusal("issue_binding_mismatch", "issue action changes owning identity")
        completed = self.runner.run(
            (self.executable, "issue", "view", str(issue), "--repo", repository)
        )
        if completed.returncode != 0:
            raise StorageRefusal(
                "issue_action_failed", "owning issue state could not be inspected", retryable=True
            )
        states = [
            line.split(":", 1)[1].strip()
            for line in completed.stdout.splitlines()
            if line.startswith("  state:")
        ]
        if len(states) != 1 or states[0] not in {"open", "closed"}:
            raise StorageRefusal(
                "issue_action_failed", "owning issue state response was ambiguous", retryable=True
            )
        return {"repository": expected["repository"], "issue": issue, "state": states[0]}

    def apply(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        intended = action.get("intended_state")
        if not isinstance(intended, Mapping):
            raise StorageRefusal("issue_binding_mismatch", "issue intended state is missing")
        repository, issue, state = _issue_identity(intended)
        command = "close" if state == "closed" else "reopen"
        arguments = [self.executable, "issue", command, str(issue), "--repo", repository]
        if command == "close":
            arguments.extend(("--reason", "completed"))
        completed = self.runner.run(tuple(arguments))
        if completed.returncode != 0:
            raise StorageRefusal(
                "issue_action_failed", "owning issue state change failed", retryable=True
            )
        return {
            "provider": "github",
            "action": command,
            "effect_sha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def intended(action: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
        return dict(observation) == dict(action["intended_state"])


class ContinuationIssueReopener:
    def __init__(self, store: Storage, adapter: GitHubIssueAdapter) -> None:
        self.store = store
        self.adapter = adapter

    def execute(
        self,
        operation_id: str,
        *,
        expected_version: int,
        expected_fence: int,
        executor_id: str,
        leased_until: str,
        at: str,
    ) -> dict[str, Any]:
        operation = self.store.continuation_status(operation_id)
        if operation is None:
            raise StorageRefusal("continuation_unknown", "continuation operation does not exist")
        lineage = operation["lineage"]
        capabilities = lineage["resume_capabilities"]
        from .visible_launch import THREAD_UUID

        thread_identity = str(lineage["thread_identity"])
        if (
            lineage["provider_kind"] != "codex"
            or capabilities.get("exact_resume") is not True
            or capabilities.get("safe_worktree_rebind") is not True
            or not thread_identity.startswith("codex:")
            or THREAD_UUID.fullmatch(thread_identity.removeprefix("codex:")) is None
        ):
            raise StorageRefusal(
                "resume_unsupported",
                "no operational exact-resume driver accepts this provider archive",
            )
        claimed = self.store.claim_issue_reopen(
            operation_id,
            expected_version,
            expected_fence,
            executor_id,
            leased_until,
            at,
        )
        if claimed["state"] in {"issue_reopened", "launching", "resumed"}:
            return claimed
        operation = self.store.continuation_status(operation_id)
        if operation is None:
            raise StorageRefusal("continuation_unknown", "continuation operation disappeared")
        expected = {
            "repository": operation["repository"],
            "issue": int(operation["issue"]),
            "state": "closed",
        }
        intended = {**expected, "state": "open"}
        action = {"expected_identity": expected, "intended_state": intended}
        before = dict(self.adapter.inspect(action))
        if self.adapter.intended(action, before):
            outcome = "already_applied"
            receipt = {"reconciled": True}
        else:
            if before != expected:
                raise StorageRefusal(
                    "issue_binding_mismatch", "owning issue changed before exact reopen"
                )
            receipt = dict(self.adapter.apply(action))
            after = dict(self.adapter.inspect(action))
            if not self.adapter.intended(action, after):
                raise StorageRefusal(
                    "issue_action_failed", "owning issue reopen did not verify", retryable=True
                )
            outcome = "applied"
        return self.store.record_issue_reopen(
            operation_id,
            int(claimed["version"]),
            int(claimed["fence"]),
            outcome,
            receipt,
            at,
        )


def verified_binding(
    *,
    repository: str,
    issue: int,
    branch: str,
    worktree: str,
) -> dict[str, Any]:
    """Verify one exact new Git worktree without mutating it."""

    from .visible_launch import SubprocessRunner as LaunchRunner
    from .visible_launch import _codex_trust_root

    path = Path(worktree)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise StorageRefusal("workspace_binding_unsafe", "continuation worktree is not exact")
    _codex_trust_root(path)
    runner = LaunchRunner()

    def git(*arguments: str) -> str:
        completed = runner.run(("git", "-C", str(path), *arguments))
        if completed.returncode != 0:
            raise StorageRefusal("workspace_binding_unsafe", "continuation Git binding did not verify")
        return completed.stdout.strip()

    top = git("rev-parse", "--show-toplevel")
    observed_branch = git("branch", "--show-current")
    head = git("rev-parse", "HEAD")
    origin = git("remote", "get-url", "origin")
    normalized_origin = origin.removesuffix(".git")
    if (
        Path(top).resolve() != path.resolve()
        or observed_branch != branch
        or branch.lower() in {"main", "master"}
        or normalized_origin != repository.removesuffix(".git")
        or re.fullmatch(r"[0-9a-f]{40}", head) is None
        or issue < 1
    ):
        raise StorageRefusal("workspace_binding_unsafe", "continuation Git identity is stale or conflicting")
    return {
        "verified": True,
        "repository": repository,
        "issue": issue,
        "branch": branch,
        "worktree": str(path.resolve()),
        "head": head,
    }


__all__ = [
    "ContinuationIssueReopener",
    "GitHubIssueAdapter",
    "IssueCommandRunner",
    "SubprocessIssueRunner",
    "verified_binding",
]
