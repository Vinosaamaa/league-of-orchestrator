#!/usr/bin/env python3
"""Regression tests for both author and committer metadata in unpublished commits."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "public_safety.py"
GOOD = "12345+synthetic-agent@users.noreply.github.com"
LEGACY_GOOD = "synthetic-agent@users.noreply.github.com"
BAD = "synthetic@example.invalid"


def run(root: Path, *arguments: str, check: bool = True, env: dict[str, str] | None = None):
    return subprocess.run(
        arguments, cwd=root, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )


def commit(root: Path, name: str, author: str, committer: str) -> str:
    (root / "record.txt").write_text(name + "\n", encoding="utf-8")
    run(root, "git", "add", "record.txt")
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "GIT_AUTHOR_NAME": "Synthetic Author",
        "GIT_AUTHOR_EMAIL": author,
        "GIT_COMMITTER_NAME": "Synthetic Committer",
        "GIT_COMMITTER_EMAIL": committer,
    }
    run(root, "git", "commit", "-m", name, env=environment)
    return run(root, "git", "rev-parse", "HEAD").stdout.strip()


def gate(root: Path, base: str):
    return run(root, sys.executable, str(SCRIPT), "--base", base, "--head", "HEAD", check=False)


def repository(root: Path, name: str) -> tuple[Path, str]:
    work = root / name
    work.mkdir()
    run(work, "git", "init", "-q")
    return work, commit(work, "base", GOOD, GOOD)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="league-public-safety-") as temporary:
        root = Path(temporary)
        good_root, good_base = repository(root, "good")
        commit(good_root, "good", GOOD, GOOD)
        commit(good_root, "legacy-good", LEGACY_GOOD, LEGACY_GOOD)
        passed = gate(good_root, good_base)
        assert passed.returncode == 0 and "author+committer" in passed.stdout
        author_root, author_base = repository(root, "bad-author")
        bad_author = commit(author_root, "bad-author", BAD, GOOD)
        failed = gate(author_root, author_base)
        assert failed.returncode == 1 and "author_identity_not_noreply" in failed.stderr
        assert BAD not in failed.stderr and bad_author in failed.stderr
        committer_root, committer_base = repository(root, "bad-committer")
        bad_committer = commit(committer_root, "bad-committer", GOOD, BAD)
        failed = gate(committer_root, committer_base)
        assert failed.returncode == 1 and "committer_identity_not_noreply" in failed.stderr
        assert BAD not in failed.stderr and bad_committer in failed.stderr
    assert BAD not in SCRIPT.read_text(encoding="utf-8")
    print("PASS: unpublished author and committer no-reply identities fail closed without value echo")


if __name__ == "__main__":
    main()
