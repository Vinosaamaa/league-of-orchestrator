#!/usr/bin/env python3
"""Fail closed unless unpublished authors and committers are GitHub no-reply."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys


NOREPLY = re.compile(r"^(?:[0-9]+\+[A-Za-z0-9_.-]+@users\.noreply\.github\.com|noreply@github\.com)$", re.IGNORECASE)
COMMIT = re.compile(r"^[0-9a-f]{40,64}$")


def identity_log(base: str, head: str) -> bytes:
    return subprocess.run(
        (
            "git", "log", "--reverse", "-z", "--format=%H%x00%ae%x00%ce",
            f"{base}..{head}",
        ),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def verify(base: str, head: str) -> tuple[list[str], int]:
    fields = identity_log(base, head).split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    failures: list[str] = []
    if len(fields) % 3:
        return ["identity_record_invalid"], len(fields) // 3
    for offset in range(0, len(fields), 3):
        try:
            commit, author, committer = (
                item.decode("ascii") for item in fields[offset : offset + 3]
            )
        except UnicodeDecodeError:
            failures.append("identity_record_invalid")
            continue
        if not COMMIT.fullmatch(commit):
            failures.append("unresolved_commit_identity")
            continue
        if not NOREPLY.fullmatch(author):
            failures.append(f"commit {commit}: author_identity_not_noreply")
        if not NOREPLY.fullmatch(committer):
            failures.append(f"commit {commit}: committer_identity_not_noreply")
    return failures, len(fields) // 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    try:
        failures, commits = verify(args.base, args.head)
    except subprocess.CalledProcessError:
        print("public-safety: git_range_unavailable", file=sys.stderr)
        return 2
    for failure in failures:
        print(f"public-safety: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(f"PASS: public-safety author+committer no-reply commits={commits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
