#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from pathlib import Path

CLASSIFICATIONS = {
    "none": "none",
    "change note": "change-note",
    "adr": "adr",
    "architecture review": "architecture-review",
    "feature retrospective": "feature-retrospective",
    "postmortem": "postmortem",
    "capability dossier": "capability-dossier",
}

PLACEHOLDER_REASONS = {
    "todo",
    "n/a",
    "na",
    "none",
    "replace with a concrete reason",
}
PLACEHOLDER_REASON_PREFIXES = ("todo", "n/a", "na", "none", "replace")

CLASSIFICATION_PATTERN = re.compile(
    rf"^\s*-\s*\[[xX]\]\s*({'|'.join(re.escape(label) for label in CLASSIFICATIONS)})(?:\s*[—-]\s*reason:\s*(.*))?\s*$",
    re.IGNORECASE,
)

RECEIPT_DIRECTORY = "docs/engineering/changes/"
RECEIPT_SCHEMA_PATH = Path(__file__).parents[1] / "docs" / "contracts" / "engineering-pull-request-receipt.schema.json"
RECEIPT_SCHEMA = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
HISTORICAL_SCHEMA_PATH = Path(__file__).parents[1] / "docs" / "contracts" / "engineering-historical-backfill-batch.schema.json"
HISTORICAL_SCHEMA = json.loads(HISTORICAL_SCHEMA_PATH.read_text(encoding="utf-8"))
RECEIPT_FIELDS = frozenset(RECEIPT_SCHEMA["required"])
RECEIPT_PROPERTIES = RECEIPT_SCHEMA["properties"]
RECEIPT_DEFINITIONS = RECEIPT_SCHEMA["$defs"]
RECEIPT_CLASSIFICATIONS = frozenset(RECEIPT_PROPERTIES["classification"]["enum"])
SOURCE_KINDS = {"issue", "pull-request", "commit", "release", "run", "documentation"}
CONFIDENCE_VALUES = {"verified", "high", "medium", "low", "unknown"}
RECORD_REF_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@[1-9]\d*$")
RECEIPT_PATH_PATTERN = re.compile(r"^docs/engineering/changes/pr-[1-9]\d*\.md$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MERGED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
REPOSITORY_FULL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HISTORICAL_AUTHORIZATION_URL = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(issues|pull)/[1-9]\d*#issuecomment-[1-9]\d*$"
)
HISTORICAL_AUTHORIZATION = (
    "I authorize publication of this bounded historical Engineering backfill batch under the residual-link policy."
)
MAX_SOURCES = RECEIPT_PROPERTIES["sources"]["maxItems"]
MAX_SOURCE_LABEL_LENGTH = RECEIPT_PROPERTIES["sources"]["items"]["properties"]["label"]["maxLength"]
MAX_SOURCE_URL_LENGTH = RECEIPT_PROPERTIES["sources"]["items"]["properties"]["url"]["maxLength"]
MAX_STRING_LIST_ITEMS = RECEIPT_DEFINITIONS["stringList"]["maxItems"]
MAX_STRING_LENGTH = RECEIPT_DEFINITIONS["stringList"]["items"]["maxLength"]
MAX_RECORD_REFS = RECEIPT_DEFINITIONS["recordRefs"]["maxItems"]
MAX_RECORD_REF_LENGTH = RECEIPT_DEFINITIONS["recordRefs"]["items"]["maxLength"]
MAX_FRONTMATTER_BYTES = 65_536
MAX_RECORD_BYTES = MAX_FRONTMATTER_BYTES
MAX_RECEIPT_BYTES = 131_072
MAX_BATCH_RECORDS = 64
FRONTMATTER_BYTES_PATTERN = re.compile(rb"\A---\r?\n.*?\r?\n---(?:\r?\n|\Z)", re.DOTALL)


def markdown_lines_outside_fences(body: str):
    fence = None
    for line in body.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= fence[1]:
                fence = None
            continue
        if marker:
            fence = (marker.group(1)[0], len(marker.group(1)))
            continue
        yield line


def selected_classifications(body: str):
    return [
        (CLASSIFICATIONS[match.group(1).lower()], (match.group(2) or "").strip())
        for line in markdown_lines_outside_fences(body)
        if (match := CLASSIFICATION_PATTERN.match(line))
    ]


def validate(body: str, record_types: list[str]):
    selected = selected_classifications(body)
    if len(selected) != 1:
        raise ValueError("Select exactly one Engineering impact classification in the pull request body.")
    classification, reason = selected[0]
    if classification == "none":
        normalized_reason = re.sub(r"[.!]+$", "", reason.strip()).lower()
        starts_with_placeholder = any(
            normalized_reason.startswith(prefix)
            and (
                len(normalized_reason) == len(prefix)
                or normalized_reason[len(prefix)] in " \t:;,.!?—-_"
            )
            for prefix in PLACEHOLDER_REASON_PREFIXES
        )
        has_substantive_word = re.search(r"[^\W_]{2,}", reason) is not None
        if (
            len(reason) < 12
            or normalized_reason in PLACEHOLDER_REASONS
            or starts_with_placeholder
            or not has_substantive_word
        ):
            raise ValueError("Engineering impact `None` requires a concrete reason.")
        if record_types:
            raise ValueError("A canonical Engineering record changed, so Engineering impact cannot be `None`.")
        return classification
    unique_types = sorted(set(record_types))
    if unique_types and unique_types != [classification]:
        raise ValueError(f"Engineering impact `{classification}` does not match record type(s): {', '.join(unique_types)}.")
    return classification


def frontmatter_document(markdown: str, path: str):
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)(.*)\Z", markdown, re.DOTALL)
    if not match:
        raise ValueError(f"Engineering document has no leading front matter: {path}.")
    values = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"Engineering document has invalid front matter: {path}.")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key or key in values or not raw_value:
            raise ValueError(f"Engineering document has invalid front matter: {path}.")
        if raw_value.startswith(("'", "|", ">", "&", "*", "!")):
            raise ValueError(
                f"Engineering document front matter uses unsupported YAML-only syntax; "
                f"use one key per line with JSON-compatible structured values: {path}."
            )
        if raw_value[0] in '[{"' or raw_value in {"true", "false", "null"} or re.fullmatch(r"-?\d+", raw_value):
            try:
                values[key] = json.loads(raw_value)
            except json.JSONDecodeError as error:
                raise ValueError(f"Engineering document has invalid JSON-compatible front matter: {path}.") from error
        else:
            values[key] = raw_value
    return values, match.group(2).strip()


def string_list(
    value,
    field: str,
    path: str,
    *,
    max_items: int = MAX_STRING_LIST_ITEMS,
    max_length: int = MAX_STRING_LENGTH,
):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"Receipt `{field}` must be a list of nonempty strings: {path}.")
    if len(value) > max_items or any(len(item) > max_length for item in value):
        raise ValueError(
            f"Receipt `{field}` must contain at most {max_items} items of at most {max_length} characters: {path}."
        )
    if len(value) != len(set(value)):
        raise ValueError(f"Receipt `{field}` must not contain duplicates: {path}.")
    return value


def required_receipt_path(changed_files: list[str], pr_number: int):
    expected = f"{RECEIPT_DIRECTORY}pr-{pr_number}.md"
    receipt_paths = [
        path for path in changed_files
        if path.startswith(RECEIPT_DIRECTORY) and path.endswith(".md")
    ]
    if receipt_paths != [expected]:
        raise ValueError(f"Every pull request must change exactly one receipt at `{expected}`.")
    return expected


def validate_receipt_fields(receipt, path, *, repository, pr_number, pr_title, classification):
    missing = sorted(RECEIPT_FIELDS - receipt.keys())
    extra = sorted(receipt.keys() - RECEIPT_FIELDS)
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if extra:
            detail.append(f"unknown {', '.join(extra)}")
        raise ValueError(f"Receipt v1 fields are invalid ({'; '.join(detail)}): {path}.")

    if type(receipt["schemaVersion"]) is not int or receipt["schemaVersion"] != 1:
        raise ValueError(f"Receipt `schemaVersion` must be 1: {path}.")
    if not isinstance(receipt["repository"], str) or not REPOSITORY_PATTERN.fullmatch(receipt["repository"]):
        raise ValueError(f"Receipt `repository` is invalid: {path}.")
    if receipt["repository"] != repository:
        raise ValueError(f"Receipt `repository` must be `{repository}`: {path}.")
    if type(receipt["pr"]) is not int or receipt["pr"] != pr_number:
        raise ValueError(f"Receipt `pr` must be {pr_number}: {path}.")
    if not isinstance(receipt["title"], str) or not 1 <= len(receipt["title"]) <= 160:
        raise ValueError(f"Receipt `title` must contain 1–160 characters: {path}.")
    if receipt["title"] != pr_title:
        raise ValueError(f"Receipt `title` must match the pull request title: {path}.")
    if receipt["classification"] not in RECEIPT_CLASSIFICATIONS:
        raise ValueError(f"Receipt `classification` is invalid: {path}.")
    if receipt["classification"] != classification:
        raise ValueError(f"Receipt `classification` must match Engineering impact `{classification}`: {path}.")
    if receipt["reconstructed"] is not False:
        raise ValueError(f"A forward pull-request receipt must set `reconstructed` to false: {path}.")
    if receipt["confidence"] not in CONFIDENCE_VALUES:
        raise ValueError(f"Receipt `confidence` is invalid: {path}.")
    string_list(receipt["unknowns"], "unknowns", path)


def validate_receipt_record_refs(receipt, path, *, classification, record_index):
    rich_record_refs = validate_record_refs(
        receipt.get("richRecordRefs"),
        path,
    )
    if classification == "none" and rich_record_refs:
        raise ValueError(f"A `none` receipt cannot link a rich record: {path}.")
    if classification != "none" and not rich_record_refs:
        raise ValueError(f"A material receipt requires at least one exact rich record reference: {path}.")
    for reference in rich_record_refs:
        if reference not in record_index:
            raise ValueError(f"Receipt rich record reference `{reference}` does not resolve at the pull-request head: {path}.")
        if record_index[reference] != classification:
            raise ValueError(f"Receipt rich record reference `{reference}` does not have the matching type `{classification}`: {path}.")

    for field in ("headCommit", "mergeCommit"):
        value = receipt[field]
        if value is not None and (not isinstance(value, str) or not COMMIT_PATTERN.fullmatch(value)):
            raise ValueError(f"Receipt `{field}` must be null or a 40-character lowercase commit: {path}.")
    merged_at = receipt["mergedAt"]
    if merged_at is not None and (not isinstance(merged_at, str) or not MERGED_AT_PATTERN.fullmatch(merged_at)):
        raise ValueError(f"Receipt `mergedAt` must be null or a UTC timestamp without fractional seconds: {path}.")


def validate_record_refs(value, path):
    rich_record_refs = string_list(
        value,
        "richRecordRefs",
        path,
        max_items=MAX_RECORD_REFS,
        max_length=MAX_RECORD_REF_LENGTH,
    )
    if any(not RECORD_REF_PATTERN.fullmatch(reference) for reference in rich_record_refs):
        raise ValueError(f"Receipt `richRecordRefs` contains an invalid exact record reference: {path}.")
    return rich_record_refs


def receipt_record_refs(markdown: str, path: str):
    receipt, _ = frontmatter_document(markdown, path)
    return validate_record_refs(receipt.get("richRecordRefs"), path)


def validate_receipt_sources(receipt, path, *, repository, pr_number, pr_url):
    sources = receipt["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"Receipt `sources` must contain at least one source: {path}.")
    if len(sources) > MAX_SOURCES:
        raise ValueError(f"Receipt `sources` must contain at most {MAX_SOURCES} sources: {path}.")
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"label", "url", "kind"}:
            raise ValueError(f"Receipt source fields are invalid: {path}.")
        if not isinstance(source["label"], str) or not 1 <= len(source["label"]) <= MAX_SOURCE_LABEL_LENGTH:
            raise ValueError(f"Receipt source label is invalid: {path}.")
        if (
            not isinstance(source["url"], str)
            or not source["url"].startswith("https://")
            or len(source["url"]) > MAX_SOURCE_URL_LENGTH
        ):
            raise ValueError(
                f"Receipt source URL must use HTTPS and contain at most {MAX_SOURCE_URL_LENGTH} characters: {path}."
            )
        if source["kind"] not in SOURCE_KINDS:
            raise ValueError(f"Receipt source kind is invalid: {path}.")
    expected_pr_url = pr_url or f"https://github.com/Vinosaamaa/{repository}/pull/{pr_number}"
    if not any(source["kind"] == "pull-request" and source["url"] == expected_pr_url for source in sources):
        raise ValueError(f"Receipt must cite its exact pull request URL `{expected_pr_url}`: {path}.")


def validate_receipt_verification(receipt, path):
    verification = receipt["verification"]
    if not isinstance(verification, dict) or set(verification) != {"state", "evidenceRefs"}:
        raise ValueError(f"Receipt `verification` fields are invalid: {path}.")
    if verification["state"] not in {"verified", "not-recorded"}:
        raise ValueError(f"Receipt verification state is invalid: {path}.")
    evidence_refs = string_list(verification["evidenceRefs"], "verification.evidenceRefs", path)
    known_merge_facts = any(receipt[field] is not None for field in ("headCommit", "mergeCommit", "mergedAt"))
    if known_merge_facts and (verification["state"] != "verified" or not evidence_refs):
        raise ValueError(f"Receipt commit and merge facts require verified evidence: {path}.")
    if receipt["visibility"] != "public-safe":
        raise ValueError(f"Receipt `visibility` must be `public-safe`: {path}.")
    if receipt["publicationEligibility"] != "eligible":
        raise ValueError(f"Receipt `publicationEligibility` must be `eligible`: {path}.")


def validate_receipt_body(receipt, body, path):
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\r?\n\s*\r?\n", body) if paragraph.strip()]
    if len(paragraphs) != 2 or paragraphs[0] != f"# {receipt['title']}":
        raise ValueError(f"Receipt body must contain its title heading and one factual summary paragraph: {path}.")
    summary = " ".join(paragraphs[1].split())
    if not summary or len(summary) > 280:
        raise ValueError(f"Receipt summary must contain at most 280 characters: {path}.")


def validate_receipt(
    markdown: str,
    path: str,
    *,
    repository: str,
    pr_number: int,
    pr_title: str,
    classification: str,
    record_index: dict[str, str],
    pr_url: str | None = None,
):
    receipt, body = frontmatter_document(markdown, path)
    validate_receipt_fields(
        receipt,
        path,
        repository=repository,
        pr_number=pr_number,
        pr_title=pr_title,
        classification=classification,
    )
    validate_receipt_record_refs(receipt, path, classification=classification, record_index=record_index)
    validate_receipt_sources(
        receipt,
        path,
        repository=repository,
        pr_number=pr_number,
        pr_url=pr_url,
    )
    validate_receipt_verification(receipt, path)
    validate_receipt_body(receipt, body, path)

    return receipt


def equal_string_sets(left, right):
    return (
        isinstance(left, list)
        and isinstance(right, list)
        and len(left) == len(set(left))
        and len(right) == len(set(right))
        and sorted(left) == sorted(right)
    )


def bounded_record_refs(value, property_schema):
    return (
        isinstance(value, list)
        and len(value) <= property_schema["maxItems"]
        and len(value) == len(set(value))
        and all(
            isinstance(reference, str)
            and len(reference) <= property_schema["items"]["maxLength"]
            and RECORD_REF_PATTERN.fullmatch(reference)
            for reference in value
        )
    )


def parse_historical_batch_manifest(markdown: str):
    try:
        manifest = json.loads(markdown)
    except json.JSONDecodeError as error:
        raise ValueError("The historical batch manifest must be valid JSON.") from error
    properties = HISTORICAL_SCHEMA["properties"]
    receipt_paths = manifest.get("receiptPaths") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or set(manifest) != set(HISTORICAL_SCHEMA["required"]):
        raise ValueError("The historical batch manifest has unsupported or missing fields.")
    if (
        manifest.get("schemaVersion") != 1
        or not isinstance(manifest.get("repository"), str)
        or not re.fullmatch(properties["repository"]["pattern"], manifest["repository"])
        or type(manifest.get("pullRequest")) is not int
        or manifest["pullRequest"] < 1
        or not isinstance(manifest.get("privacyAuthorizationUrl"), str)
        or len(manifest["privacyAuthorizationUrl"]) > properties["privacyAuthorizationUrl"]["maxLength"]
        or not HISTORICAL_AUTHORIZATION_URL.fullmatch(manifest["privacyAuthorizationUrl"])
        or not isinstance(receipt_paths, list)
        or not properties["receiptPaths"]["minItems"] <= len(receipt_paths) <= properties["receiptPaths"]["maxItems"]
        or len(receipt_paths) != len(set(receipt_paths))
        or any(
            not isinstance(path, str)
            or len(path) > properties["receiptPaths"]["items"]["maxLength"]
            or not RECEIPT_PATH_PATTERN.fullmatch(path)
            for path in receipt_paths
        )
        or not bounded_record_refs(manifest.get("recordRefs"), properties["recordRefs"])
        or not bounded_record_refs(manifest.get("addedRecordRefs"), properties["addedRecordRefs"])
    ):
        raise ValueError("The historical batch manifest has invalid bounded fields.")
    return manifest


def load_authorization_comment(repository_full_name: str, comment_id: str):
    result = subprocess.run(
        ["gh", "api", f"repos/{repository_full_name}/issues/comments/{comment_id}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("Unable to verify the historical batch privacy authorization comment.")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("The historical batch privacy authorization response is invalid.") from error


def verify_historical_authorization(manifest, repository_full_name: str, load_comment=None):
    loader = load_comment or load_authorization_comment
    match = re.search(r"#issuecomment-([1-9]\d*)$", manifest["privacyAuthorizationUrl"])
    if not match:
        raise ValueError("The historical batch privacy authorization comment URL is invalid.")
    comment = loader(repository_full_name, match.group(1))
    if (
        comment.get("html_url") != manifest["privacyAuthorizationUrl"]
        or comment.get("author_association") != "OWNER"
        or (comment.get("body") or "").strip() != HISTORICAL_AUTHORIZATION
    ):
        raise ValueError("The historical batch requires an exact repository-owner privacy authorization comment.")


def record_path_for_ref(record_ref: str):
    record_id, _ = record_ref.rsplit("@", 1)
    return f"docs/engineering/records/{record_id}.md"


def parse_reconstructed_receipt(markdown: str, path: str, repository: str, record_index: dict[str, str]):
    receipt, body = frontmatter_document(markdown, path)
    missing = sorted(RECEIPT_FIELDS - receipt.keys())
    extra = sorted(receipt.keys() - RECEIPT_FIELDS)
    if missing or extra:
        raise ValueError(f"Receipt v1 fields are invalid: {path}.")
    if receipt.get("reconstructed") is not True:
        raise ValueError("Every historical receipt must be reconstructed, repository-owned, and match its numbered path.")
    if receipt.get("repository") != repository:
        raise ValueError("Every historical receipt must be reconstructed, repository-owned, and match its numbered path.")
    match = RECEIPT_PATH_PATTERN.fullmatch(path)
    if not match or receipt.get("pr") != int(path.rsplit("-", 1)[1].removesuffix(".md")):
        raise ValueError("Every historical receipt must be reconstructed, repository-owned, and match its numbered path.")
    classification = receipt.get("classification")
    if classification not in RECEIPT_CLASSIFICATIONS:
        raise ValueError(f"Receipt `classification` is invalid: {path}.")
    validate_receipt_record_refs(receipt, path, classification=classification, record_index=record_index)
    validate_receipt_sources(
        receipt,
        path,
        repository=repository,
        pr_number=receipt["pr"],
        pr_url=f"https://github.com/Vinosaamaa/{repository}/pull/{receipt['pr']}",
    )
    validate_receipt_verification(receipt, path)
    validate_receipt_body(receipt, body, path)
    return {
        "path": path,
        "pr": receipt["pr"],
        "repository": receipt["repository"],
        "classification": classification,
        "richRecordRefs": receipt["richRecordRefs"],
        "reconstructed": True,
    }


def validate_historical_batch(
    *,
    manifest,
    manifest_path: str,
    pull_request_number: int,
    repository: str,
    repository_full_name: str,
    changed_files: list[str],
    historical_receipts: list[dict],
    added_record_refs: list[str],
    added_reconstructed: list[bool],
    base_existing_paths: list[str],
):
    expected_manifest_path = f"docs/engineering/backfill/pr-{pull_request_number}.json"
    if (
        manifest["repository"] != repository
        or manifest["pullRequest"] != pull_request_number
        or manifest_path != expected_manifest_path
        or not REPOSITORY_FULL_NAME_PATTERN.fullmatch(repository_full_name)
        or not manifest["privacyAuthorizationUrl"].startswith(f"https://github.com/{repository_full_name}/")
    ):
        raise ValueError("A historical batch manifest must match the current repository and pull request.")
    if f"docs/engineering/changes/pr-{pull_request_number}.md" in manifest["receiptPaths"]:
        raise ValueError("A historical batch manifest must not claim the current pull request's forward receipt.")
    if not equal_string_sets(manifest["receiptPaths"], [receipt["path"] for receipt in historical_receipts]):
        raise ValueError("The historical batch manifest must enumerate its reconstructed receipts exactly.")
    referenced = sorted({ref for receipt in historical_receipts for ref in receipt["richRecordRefs"]})
    if not equal_string_sets(manifest["recordRefs"], referenced):
        raise ValueError("The historical batch manifest must enumerate exactly the rich records referenced by reconstructed receipts.")
    if not equal_string_sets(manifest["addedRecordRefs"], added_record_refs):
        raise ValueError("The historical batch manifest must enumerate the changed reconstructed receipts and rich records exactly.")
    if any(flag is not True for flag in added_reconstructed):
        raise ValueError("Historical rich records must set reconstructed true.")
    if any(reference not in referenced for reference in added_record_refs):
        raise ValueError("Every rich record added by a historical batch must exist at head and be linked by a reconstructed receipt.")
    for receipt in historical_receipts:
        if receipt["pr"] == pull_request_number or receipt.get("reconstructed") is not True:
            raise ValueError("Every historical receipt must be reconstructed, repository-owned, and match its numbered path.")
    allowed = [
        f"docs/engineering/changes/pr-{pull_request_number}.md",
        manifest_path,
        *manifest["receiptPaths"],
        *[record_path_for_ref(reference) for reference in manifest["addedRecordRefs"]],
    ]
    if not equal_string_sets(allowed, changed_files):
        raise ValueError("A historical publication pull request may contain only its forward receipt, batch manifest, and declared historical documents.")
    if base_existing_paths:
        raise ValueError("Historical batch documents are add-only; accepted receipts and records cannot be modified or deleted.")
    return {
        "historicalReceiptCount": len(historical_receipts),
        "historicalRecordCount": len(added_record_refs),
    }


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def bounded_git_blob(revision: str, path: str):
    object_spec = f"{revision}:{path}"
    size_result = subprocess.run(
        ["git", "cat-file", "-s", object_spec],
        capture_output=True,
        text=True,
    )
    if size_result.returncode != 0:
        return None
    try:
        object_size = int(size_result.stdout.strip())
    except ValueError as error:
        raise ValueError("Unable to read the pull-request receipt Git object.") from error
    if object_size > MAX_RECEIPT_BYTES:
        raise ValueError(
            f"Pull-request receipt is oversized; maximum {MAX_RECEIPT_BYTES} bytes: {path}."
        )
    blob_result = subprocess.run(
        ["git", "cat-file", "blob", object_spec],
        capture_output=True,
        check=True,
    )
    if len(blob_result.stdout) != object_size:
        raise ValueError("Unable to read the pull-request receipt Git object.")
    return blob_result.stdout.decode("utf-8", errors="strict")


def git_objects_exist(revision: str, paths: list[str]):
    unique_paths = list(dict.fromkeys(paths))
    if not unique_paths:
        return set()
    result = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        input="".join(f"{revision}:{path}\n" for path in unique_paths),
        capture_output=True,
        text=True,
        check=True,
    )
    headers = result.stdout.splitlines()
    if len(headers) != len(unique_paths):
        raise ValueError("Unable to check canonical Engineering record Git objects.")
    existing = set()
    for path, header in zip(unique_paths, headers):
        if header.endswith(" missing"):
            continue
        parts = header.split()
        if len(parts) != 3 or parts[1] != "blob":
            raise ValueError("Unable to check canonical Engineering record Git objects.")
        existing.add(path)
    return existing


def matching_record_paths(revision: str, record_ids: set[str]):
    if not record_ids:
        return []
    grep_patterns = []
    for record_id in sorted(record_ids):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", record_id):
            raise ValueError(f"Canonical Engineering record reference has an invalid id: {record_id}.")
        for encoded_id in (record_id, f'"{record_id}"'):
            grep_patterns.extend(
                ["-e", rf"^[[:space:]]*id[[:space:]]*:[[:space:]]*{encoded_id}[[:space:]]*$"]
            )
    result = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "-E",
            "-z",
            *grep_patterns,
            revision,
            "--",
            "docs/engineering/records",
        ],
        capture_output=True,
    )
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    prefix = f"{revision}:".encode()
    paths = []
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        if not entry.startswith(prefix):
            raise ValueError("Unable to resolve a canonical Engineering record Git object.")
        path = entry[len(prefix):].decode("utf-8", errors="strict")
        if path.endswith(".md"):
            paths.append(path)
    return paths


def record_type_from_markdown(markdown: str, path: str):
    try:
        metadata, _ = frontmatter_document(markdown, path)
    except ValueError:
        raise ValueError(
            f"Changed canonical Engineering record has no single valid type in leading front matter: {path}."
        ) from None
    record_type = metadata.get("type")
    if not isinstance(record_type, str) or not re.fullmatch(r"[a-z][a-z-]*", record_type):
        raise ValueError(f"Changed canonical Engineering record has no single valid type in leading front matter: {path}.")
    return record_type


def iter_frontmatters_at(revision: str, paths: list[str]):
    if not paths:
        return
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        for chunk_start in range(0, len(paths), MAX_BATCH_RECORDS):
            chunk = paths[chunk_start : chunk_start + MAX_BATCH_RECORDS]
            process.stdin.write("".join(f"{revision}:{path}\n" for path in chunk).encode())
            process.stdin.flush()
            for path in chunk:
                header = process.stdout.readline().decode("utf-8", errors="strict").rstrip("\n")
                if header.endswith(" missing"):
                    raise ValueError(f"Canonical Engineering record is missing at the pull-request head: {path}.")
                parts = header.split()
                if len(parts) != 3 or parts[1] != "blob":
                    raise ValueError("Unable to read a canonical Engineering record Git object.")
                object_size = int(parts[2])
                if object_size > MAX_RECORD_BYTES:
                    raise ValueError(f"Canonical Engineering record has invalid or oversized front matter: {path}.")
                prefix = process.stdout.read(object_size)
                remaining = object_size - len(prefix)
                while remaining:
                    discarded = process.stdout.read(min(remaining, 65_536))
                    if not discarded:
                        raise ValueError("Unable to read a canonical Engineering record Git object.")
                    remaining -= len(discarded)
                if process.stdout.read(1) != b"\n":
                    raise ValueError("Unable to read a canonical Engineering record Git object.")
                frontmatter_match = FRONTMATTER_BYTES_PATTERN.match(prefix)
                if not frontmatter_match:
                    raise ValueError(f"Canonical Engineering record has invalid or oversized front matter: {path}.")
                metadata, _ = frontmatter_document(
                    frontmatter_match.group().decode("utf-8", errors="strict"),
                    path,
                )
                yield path, metadata
    finally:
        process.stdin.close()
        process.stdout.close()
        stderr = process.stderr.read()
        process.stderr.close()
        return_code = process.wait()
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, process.args, stderr=stderr)


def validate_changed_record_paths(revision: str, changed_paths: list[str]):
    missing_changed_paths = set(changed_paths) - git_objects_exist(revision, changed_paths)
    if missing_changed_paths:
        path = sorted(missing_changed_paths)[0]
        raise ValueError(
            f"Canonical Engineering records cannot be deleted; publish a superseding record instead: {path}."
        )


def load_record_metadata(revision: str, paths):
    return dict(iter_frontmatters_at(revision, sorted(paths)))


def validated_record_id(metadata, path):
    record_id = metadata.get("id")
    if not isinstance(record_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", record_id):
        raise ValueError(f"Canonical Engineering record has an invalid id: {path}.")
    return record_id


def discover_referenced_record_metadata(revision: str, changed_metadata, required_refs):
    record_ids = {
        reference.rsplit("@", 1)[0]
        for reference in required_refs or []
    }
    record_ids.update(
        validated_record_id(metadata, path)
        for path, metadata in changed_metadata.items()
    )
    candidate_paths = set(matching_record_paths(revision, record_ids)) - set(changed_metadata)
    candidate_metadata = load_record_metadata(revision, candidate_paths)
    return {
        path: metadata
        for path, metadata in candidate_metadata.items()
        if metadata.get("id") in record_ids
    }


def build_record_index(metadata_by_path, changed_paths):
    changed_path_set = set(changed_paths)
    index = {}
    changed_types = []
    for path, metadata in sorted(metadata_by_path.items()):
        record_id = validated_record_id(metadata, path)
        record_revision = metadata.get("revision")
        record_type = metadata.get("type")
        if type(record_revision) is not int or record_revision < 1:
            raise ValueError(f"Canonical Engineering record has an invalid revision: {path}.")
        if record_type not in set(CLASSIFICATIONS.values()) - {"none"}:
            raise ValueError(f"Canonical Engineering record has an invalid type: {path}.")
        if metadata.get("confidence") == "verified":
            verification = metadata.get("verification")
            evidence_refs = verification.get("evidenceRefs") if isinstance(verification, dict) else None
            if (
                not isinstance(verification, dict)
                or verification.get("state") != "verified"
                or not isinstance(evidence_refs, list)
                or not evidence_refs
                or any(not isinstance(reference, str) or not reference for reference in evidence_refs)
            ):
                raise ValueError(
                    f"Canonical Engineering record with verified confidence requires recorded evidence: {path}."
                )
        reference = f"{record_id}@{record_revision}"
        if reference in index:
            raise ValueError(f"Duplicate canonical Engineering record reference: {reference}.")
        index[reference] = record_type
        if path in changed_path_set:
            changed_types.append(record_type)
    return index, changed_types


def record_index_and_changed_types_at(
    revision: str,
    changed_paths: list[str],
    required_refs: list[str] | None = None,
):
    validate_changed_record_paths(revision, changed_paths)
    changed_metadata = load_record_metadata(revision, changed_paths)
    referenced_metadata = discover_referenced_record_metadata(
        revision,
        changed_metadata,
        required_refs,
    )
    return build_record_index(
        changed_metadata | referenced_metadata,
        changed_paths,
    )


def validate_record_history(base: str, changed_paths: list[str]):
    replaced_paths = git_objects_exist(base, changed_paths)
    if replaced_paths:
        path = sorted(replaced_paths)[0]
        raise ValueError(
            "An accepted canonical Engineering record revision is immutable; "
            f"publish a new record instead of replacing it in place: {path}."
        )


def main():
    event_path = Path(sys.argv[1] if len(sys.argv) > 1 else "")
    if not event_path.is_file():
        raise ValueError("A pull-request event path is required.")
    event = json.loads(event_path.read_text(encoding="utf-8"))
    pull_request = event.get("pull_request", {})
    base = pull_request.get("base", {}).get("sha")
    head = pull_request.get("head", {}).get("sha")
    pr_number = pull_request.get("number") or event.get("number")
    pr_title = pull_request.get("title")
    repository = event.get("repository", {}).get("name") or pull_request.get("base", {}).get("repo", {}).get("name")
    repository_full_name = event.get("repository", {}).get("full_name")
    pr_url = pull_request.get("html_url")
    if not base or not head or type(pr_number) is not int or not pr_title or not repository or not pr_url:
        raise ValueError("Pull request base, head, number, title, repository, and URL are required.")
    changed = git("diff", "--name-only", base, head).splitlines()
    record_paths = [path for path in changed if path.startswith("docs/engineering/records/") and path.endswith(".md")]
    receipt_paths = [path for path in changed if path.startswith("docs/engineering/changes/") and path.endswith(".md")]
    manifest_paths = [path for path in changed if path.startswith("docs/engineering/backfill/") and path.endswith(".json")]
    historical_mode = len(receipt_paths) > 1 or len(manifest_paths) > 0
    expected_manifest_path = f"docs/engineering/backfill/pr-{pr_number}.json"
    historical_paths = set()
    historical_receipts = []
    manifest = None
    extra_record_refs = []
    if historical_mode:
        if not isinstance(repository_full_name, str) or not REPOSITORY_FULL_NAME_PATTERN.fullmatch(repository_full_name) or not repository_full_name.endswith(f"/{repository}"):
            raise ValueError("A historical publication pull request requires the exact owning repository full name.")
        if manifest_paths != [expected_manifest_path]:
            raise ValueError("A historical publication pull request must change its one numbered batch manifest.")
        manifest_markdown = bounded_git_blob(head, expected_manifest_path)
        if manifest_markdown is None:
            raise ValueError("The historical batch manifest must exist at the pull request head.")
        manifest = parse_historical_batch_manifest(manifest_markdown)
        verify_historical_authorization(manifest, repository_full_name)
        historical_markdown = []
        for path in manifest["receiptPaths"]:
            markdown = bounded_git_blob(head, path)
            if markdown is None:
                raise ValueError("Every declared historical receipt must exist at the pull request head.")
            historical_markdown.append(markdown)
            receipt_fields, _ = frontmatter_document(markdown, path)
            extra_record_refs.extend(receipt_fields.get("richRecordRefs") or [])
        historical_paths = {expected_manifest_path, *manifest["receiptPaths"], *record_paths}
    validate_record_history(base, record_paths)
    forward_changed = [path for path in changed if path not in historical_paths]
    receipt_path = required_receipt_path(forward_changed, pr_number)
    receipt_markdown = bounded_git_blob(head, receipt_path)
    if receipt_markdown is None:
        raise ValueError(f"Pull-request receipt is missing at the pull-request head: {receipt_path}.")
    required_record_refs = list(dict.fromkeys([
        *receipt_record_refs(receipt_markdown, receipt_path),
        *[reference for reference in extra_record_refs if isinstance(reference, str)],
    ]))
    record_index, changed_record_types = record_index_and_changed_types_at(
        head,
        record_paths,
        required_record_refs,
    )
    if historical_mode:
        historical_receipts = [
            parse_reconstructed_receipt(markdown, path, repository, record_index)
            for path, markdown in zip(manifest["receiptPaths"], historical_markdown)
        ]
    classification = validate(
        pull_request.get("body") or "",
        [] if historical_mode else changed_record_types,
    )
    receipt = validate_receipt(
        receipt_markdown,
        receipt_path,
        repository=repository,
        pr_number=pr_number,
        pr_title=pr_title,
        classification=classification,
        record_index=record_index,
        pr_url=pr_url,
    )
    if historical_mode:
        changed_metadata = load_record_metadata(head, record_paths)
        added_record_refs = []
        added_reconstructed = []
        for path, metadata in sorted(changed_metadata.items()):
            record_id = validated_record_id(metadata, path)
            added_record_refs.append(f"{record_id}@{metadata['revision']}")
            added_reconstructed.append(metadata.get("reconstructed") is True)
        historical_document_paths = [*manifest["receiptPaths"], *record_paths]
        base_existing_paths = sorted(git_objects_exist(base, historical_document_paths))
        historical_result = validate_historical_batch(
            manifest=manifest,
            manifest_path=expected_manifest_path,
            pull_request_number=pr_number,
            repository=repository,
            repository_full_name=repository_full_name,
            changed_files=changed,
            historical_receipts=historical_receipts,
            added_record_refs=added_record_refs,
            added_reconstructed=added_reconstructed,
            base_existing_paths=base_existing_paths,
        )
        print(
            f"Engineering impact: {classification}; historical batch: "
            f"{historical_result['historicalReceiptCount']} receipt(s), "
            f"{historical_result['historicalRecordCount']} rich record(s)."
        )
        return
    print(
        f"Engineering impact: {classification}; receipt PR #{receipt['pr']}; "
        f"{len(changed)} changed file(s)."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
