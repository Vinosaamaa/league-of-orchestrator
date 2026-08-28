#!/usr/bin/env python3
"""Fail closed unless unpublished authors and committers are GitHub no-reply."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys


NOREPLY = re.compile(r"^(?:[0-9]+\+[A-Za-z0-9_.-]+@users\.noreply\.github\.com|noreply@github\.com)$", re.IGNORECASE)
COMMIT = re.compile(r"^[0-9a-f]{40,64}$")


def git(*arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ).stdout


def verify(base: str, head: str) -> list[str]:
    commits = [item for item in git("rev-list", "--reverse", f"{base}..{head}").splitlines() if item]
    failures: list[str] = []
    for commit in commits:
        if not COMMIT.fullmatch(commit):
            failures.append("unresolved_commit_identity")
            continue
        identities = git("show", "-s", "--format=%ae%n%ce", commit).splitlines()
        if len(identities) != 2:
            failures.append(f"commit {commit}: identity_record_invalid")
            continue
        if not NOREPLY.fullmatch(identities[0]):
            failures.append(f"commit {commit}: author_identity_not_noreply")
        if not NOREPLY.fullmatch(identities[1]):
            failures.append(f"commit {commit}: committer_identity_not_noreply")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    try:
        failures = verify(args.base, args.head)
    except subprocess.CalledProcessError:
        print("public-safety: git_range_unavailable", file=sys.stderr)
        return 2
    for failure in failures:
        print(f"public-safety: {failure}", file=sys.stderr)
    if failures:
        return 1
    commits = len([item for item in git("rev-list", f"{args.base}..{args.head}").splitlines() if item])
    print(f"PASS: public-safety author+committer no-reply commits={commits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
