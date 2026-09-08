#!/usr/bin/env python3
"""Create one canonical forward Engineering pull-request receipt."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

OWNER = "Vinosaamaa"
REPOSITORY = "league-of-orchestrator"
CLASSIFICATIONS = {
    "none",
    "change-note",
    "adr",
    "architecture-review",
    "feature-retrospective",
    "postmortem",
    "capability-dossier",
}
RECORD_REF = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@[1-9]\d*$")
CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
PUBLIC_UNSAFE_PATTERNS = (
    re.compile(r"(?:^|[\s(\"'`])/(?:Users|home|root)/[^\s)\"'`]+"),
    re.compile(r"(?:^|[\s(\"'`])/(?:private/tmp|tmp|var|opt|srv|workspace|mnt|Volumes)/[^\s)\"'`]+"),
    re.compile(r"(?:^|[\s(\"'`])~/[^\s)\"'`]+"),
    re.compile(r"\b[A-Za-z]:\\[^\s\"'`]+"),
    re.compile(r"\\\\[^\s\\]+\\[^\s\"'`]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:password|access[_-]?token|api[_-]?key|client[_-]?secret)\s*[:=]\s*[^\s]{8,}", re.I),
    re.compile(r"\b(?:thread|task)_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgit@[A-Za-z0-9.-]+:[^\s]+"),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]+@[^\s/]+"),
    re.compile(r"\b[A-Z0-9._%+-]+@(?!example\.com\b)[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
)

HELP_EPILOG = """Authoring order:
  1. Decide the Engineering impact before opening the pull request.
  2. For material work, commit a new rich record first, or select an existing
     exact revision already available at the pull-request head.
  3. Open a draft pull request to obtain its repository-local number.
  4. Run this command and commit the generated pr-<number>.md file.
  5. Select the matching Engineering-impact checkbox. For None, replace its
     placeholder with a concrete reason of at least 12 characters.

Non-material example:
  python3 scripts/new-engineering-receipt.py \\
    --pr <number> \\
    --title "Correct Engineering workflow labels" \\
    --summary "Renamed one local label without changing a Module or Interface." \\
    --classification none

Material example:
  python3 scripts/new-engineering-receipt.py \\
    --pr <number> \\
    --title "Adopt the Engineering Journal boundary" \\
    --summary "Adopted the reviewed Journal contract and deterministic projection." \\
    --classification architecture-review \\
    --rich-record-ref <id>@<revision>

This scaffold creates canonical Markdown only. CI validates it, and Arc's build
derives JSON, search, backlinks, Statistics, portable static HTML, and the website
projection. It does not author prose or diagrams.
"""


class ScaffoldError(ValueError):
    pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Create the canonical compact Engineering receipt for a League pull request.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    result.add_argument("--pr", type=int, required=True)
    result.add_argument("--title", required=True)
    result.add_argument("--summary", required=True)
    result.add_argument("--classification", required=True, choices=sorted(CLASSIFICATIONS))
    result.add_argument("--rich-record-ref", action="append", default=[])
    return result


def one_line(value: str, label: str, maximum: int) -> str:
    if not value or value != value.strip() or CONTROL_CHARACTER.search(value):
        raise ScaffoldError(f"{label} must be one non-empty line without surrounding whitespace.")
    if len(value) > maximum:
        raise ScaffoldError(f"{label} exceeds {maximum} characters.")
    return value


def require_public_safe(*values: str) -> None:
    if any(pattern.search(value) for value in values for pattern in PUBLIC_UNSAFE_PATTERNS):
        raise ScaffoldError("Receipt text is not public-safe.")


def receipt_directory(root: Path) -> Path:
    root = root.resolve()
    if not (root / "AGENTS.md").is_file() or not (root / "docs/contracts/engineering-pull-request-receipt.schema.json").is_file():
        raise ScaffoldError("Run this command from the League of Orchestrator repository root.")
    current = root
    for segment in ("docs", "engineering", "changes"):
        current = current / segment
        current.mkdir(mode=0o755, exist_ok=True)
        if current.is_symlink() or not current.is_dir():
            raise ScaffoldError("The canonical Engineering receipt directory is unsafe.")
    if current.resolve() != root / "docs/engineering/changes":
        raise ScaffoldError("The canonical Engineering receipt directory is unsafe.")
    return current


def render(pr: int, title: str, summary: str, classification: str, refs: list[str]) -> str:
    url = f"https://github.com/{OWNER}/{REPOSITORY}/pull/{pr}"
    compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"""---
schemaVersion: 1
repository: {REPOSITORY}
pr: {pr}
title: {compact(title)}
classification: {classification}
richRecordRefs: {compact(refs)}
reconstructed: false
confidence: verified
unknowns: []
headCommit: null
mergeCommit: null
mergedAt: null
sources: {compact([{"label": f"Pull request #{pr}", "url": url, "kind": "pull-request"}])}
verification: {compact({"state": "verified", "evidenceRefs": [f"pull-request:{pr}"]})}
visibility: public-safe
publicationEligibility: eligible
---
# {title}

{summary}
"""


def run(arguments: argparse.Namespace, root: Path) -> Path:
    if arguments.pr < 1:
        raise ScaffoldError("PR number must be a positive integer.")
    title = one_line(arguments.title, "Title", 160)
    summary = one_line(arguments.summary, "Summary", 280)
    if re.match(r"^#{1,6}\s", summary):
        raise ScaffoldError("Summary must be a factual paragraph, not a Markdown heading.")
    require_public_safe(title, summary)
    refs = sorted(arguments.rich_record_ref)
    if len(refs) > 16:
        raise ScaffoldError("A receipt cannot link more than 16 rich Engineering records.")
    if len(refs) != len(set(refs)):
        raise ScaffoldError("Rich-record references must be unique.")
    if any(len(reference) > 180 or not RECORD_REF.fullmatch(reference) for reference in refs):
        raise ScaffoldError("Every rich-record reference must use the exact id@revision format.")
    if arguments.classification == "none" and refs:
        raise ScaffoldError("Classification none cannot link a rich Engineering record.")
    if arguments.classification != "none" and not refs:
        raise ScaffoldError("A material classification requires at least one exact rich-record reference.")

    directory = receipt_directory(root)
    target = directory / f"pr-{arguments.pr}.md"
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as error:
        raise ScaffoldError(f"Refusing to overwrite docs/engineering/changes/pr-{arguments.pr}.md.") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(render(arguments.pr, title, summary, arguments.classification, refs))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def main() -> int:
    try:
        target = run(parser().parse_args(), Path.cwd())
    except ScaffoldError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Created {target.relative_to(Path.cwd().resolve()).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
