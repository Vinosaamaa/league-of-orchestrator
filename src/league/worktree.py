"""Public, read-only verification for one exact Git worktree binding."""

from __future__ import annotations

import subprocess
import tempfile
import re
from pathlib import Path
from typing import Protocol, Sequence

from .storage_types import StorageRefusal


MAX_GIT_OUTPUT_BYTES = 1024 * 1024
_GITHUB_REPOSITORIES = (
    re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$"),
    re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?$"),
)


def normalized_github_repository(repository: str) -> str:
    """Normalize every GitHub remote form supported by issue actions."""

    match = None
    for pattern in _GITHUB_REPOSITORIES:
        match = pattern.fullmatch(repository)
        if match is not None:
            break
    if match is None:
        raise StorageRefusal(
            "issue_binding_mismatch", "GitHub repository identity is unsupported"
        )
    return f"{match.group(1)}/{match.group(2)}"


class GitCommandRunner(Protocol):
    def run(
        self, arguments: Sequence[str], *, timeout_seconds: int = 30
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessGitRunner:
    """Run one bounded Git command without exposing unbounded output."""

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
                    if stream.tell() > MAX_GIT_OUTPUT_BYTES:
                        raise StorageRefusal(
                            "workspace_binding_unsafe",
                            "continuation Git output exceeded its bound",
                        )
                    stream.seek(0)
                    values.append(stream.read().decode("utf-8"))
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
            raise StorageRefusal(
                "workspace_binding_unsafe", "continuation Git command did not complete"
            ) from exc
        return subprocess.CompletedProcess(
            list(arguments), process.returncode, values[0], values[1]
        )


def verified_worktree_repository_root(worktree: Path) -> Path:
    """Return the exact owning repository root for a standalone or linked worktree."""

    marker = worktree / ".git"
    if marker.is_dir() and not marker.is_symlink():
        return worktree.resolve()
    if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 4096:
        raise StorageRefusal(
            "launch_scope_invalid", "visible launch worktree has no exact Git identity"
        )
    try:
        line = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise StorageRefusal(
            "launch_scope_invalid", "visible launch Git identity could not be read"
        ) from exc
    if not line.startswith("gitdir: ") or "\n" in line or "\0" in line:
        raise StorageRefusal(
            "launch_scope_invalid", "visible launch Git identity is malformed"
        )
    supplied = Path(line.removeprefix("gitdir: "))
    git_dir = (
        supplied if supplied.is_absolute() else marker.parent / supplied
    ).resolve(strict=False)
    common_dir = git_dir.parent.parent
    repository = common_dir.parent
    backref = git_dir / "gitdir"
    try:
        recorded_marker = Path(backref.read_text(encoding="utf-8").strip()).resolve()
    except (OSError, UnicodeError) as exc:
        raise StorageRefusal(
            "launch_scope_invalid", "visible launch worktree registration is incomplete"
        ) from exc
    exact = (
        git_dir.is_dir()
        and git_dir.parent.name == "worktrees"
        and common_dir.name == ".git"
        and common_dir.is_dir()
        and not common_dir.is_symlink()
        and repository.is_dir()
        and not repository.is_symlink()
        and recorded_marker == marker.resolve()
    )
    if not exact:
        raise StorageRefusal(
            "launch_scope_invalid", "visible launch worktree registration is not exact"
        )
    return repository.resolve()


__all__ = [
    "GitCommandRunner",
    "SubprocessGitRunner",
    "normalized_github_repository",
    "verified_worktree_repository_root",
]
