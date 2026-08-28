"""Fail-closed outbound classification and final rendered-payload validation.

League keeps exact local evidence in its owning local store.  This module owns
the single boundary that decides whether final rendered bytes may cross a
remote adapter.  A rejection deliberately reports only a category and field;
the unsafe value is never copied into the exception text.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import unquote, urlsplit

from .storage_types import StorageRefusal


MAX_OUTBOUND_BYTES = 1_000_000
DESTINATION_VISIBILITIES = frozenset({"public", "private"})
PAYLOAD_MODES = frozenset({"outbound", "local_diagnostic"})

_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_APPROVED_LEAGUE_ID = re.compile(
    r"\b(?:project|request|task|event|report|evidence|repair|actor|squad|assignment|"
    r"resource|cleanup|operation|callsign):[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\b"
)
_ABSOLUTE_UNIX_PATH = re.compile(
    r"(?<![A-Za-z0-9._-])/(?:Users|home|private|tmp|var|Volumes|Library|Applications|"
    r"opt|usr|etc|workspace|workspaces|mnt|srv|root)(?:/|\\|\b)",
    re.IGNORECASE,
)
_GENERIC_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\[({'\"=:])/(?!/)(?:[^\s<>'\"`]|\\ )+",
    re.MULTILINE,
)
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:[\\/](?:Users|Documents|Temp|AppData|ProgramData)[\\/]", re.IGNORECASE)
_HOME_ALIAS = re.compile(r"(?:^|[\s\[({'\"=:])(?:~[/\\]|\$HOME\b|\$\{HOME\}|%USERPROFILE%)", re.IGNORECASE)
_FILE_URL = re.compile(r"\bfile\s*:\s*(?:/{1,3}|%2f)", re.IGNORECASE)
_BARE_UUID = re.compile(
    r"(?<![A-Za-z0-9:_-])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![A-Za-z0-9:_-])",
    re.IGNORECASE,
)
_LABELED_RUNTIME_ID = re.compile(
    r"\b(?:thread|thread_id|session|session_id|pane|pane_id|tab|tab_id|window|window_id)\s*[:=]",
    re.IGNORECASE,
)
_PANE_ADDRESS = re.compile(r"(?<![A-Za-z0-9])(?:w[0-9A-Za-z]+:p[0-9A-Za-z]+|%[0-9]+)(?![A-Za-z0-9])")
_PID = re.compile(r"\b(?:pid|process[_ -]?id)\s*[:=]\s*[0-9]{1,10}\b", re.IGNORECASE)
_SOCKET = re.compile(r"\b(?:socket|sock(?:et)?[_ -]?path)\s*[:=]|[^\s]+\.sock\b", re.IGNORECASE)
_LOCAL_IDENTITY = re.compile(
    r"\b(?:username|user_name|local_user|hostname|host_name|machine_name)\s*[:=]",
    re.IGNORECASE,
)
_LOOPBACK = re.compile(r"\b(?:localhost|127(?:\.[0-9]{1,3}){3}|0\.0\.0\.0|::1|\[::1\])\b", re.IGNORECASE)
_PRIVATE_IPV4 = re.compile(
    r"\b(?:10(?:\.[0-9]{1,3}){3}|192\.168(?:\.[0-9]{1,3}){2}|"
    r"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2})\b"
)
_LOCAL_HOST = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9.-]*\.local\b", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?:authorization|password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|private[_-]?key|secret(?:[_-]?(?:name|value))?)\s*[:=]",
    re.IGNORECASE,
)
_SECRET_MATERIAL = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}|"
    r"\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b)"
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<![A-Za-z0-9])(?:\+?1[ .-]?)?\(?[2-9][0-9]{2}\)?[ .-][0-9]{3}[ .-][0-9]{4}(?![A-Za-z0-9])")
_PERSONAL_FIELD = re.compile(
    r"\b(?:applicant|candidate|employer|date[_ -]?of[_ -]?birth|dob|social[_ -]?security|"
    r"street[_ -]?address|personal[_ -]?name|account[_ -]?id)\s*[:=]",
    re.IGNORECASE,
)
_ESCAPED_CODEPOINT = re.compile(r"\\u00(2f|5c|3a|7e)", re.IGNORECASE)
_ESCAPED_HEX = re.compile(r"\\x(2f|5c|3a|7e)", re.IGNORECASE)


@dataclass(frozen=True)
class ClassifiedValue:
    """A structured value whose classification is checked before rendering."""

    value: Any
    classification: str
    field: str


@dataclass(frozen=True)
class ValidationReceipt:
    schema: str
    payload_sha256: str
    byte_count: int
    destination_visibility: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "payload_sha256": self.payload_sha256,
            "bytes": self.byte_count,
            "destination_visibility": self.destination_visibility,
        }


class PrivacyRefusal(StorageRefusal):
    """A local-only category/field refusal that never carries the unsafe value."""

    def __init__(self, category: str, field: str) -> None:
        self.category = category
        self.field = field
        super().__init__(
            "outbound_payload_rejected",
            f"outbound payload rejected: category={category} field={field}",
        )


def _refuse(category: str, field: str) -> None:
    raise PrivacyRefusal(category, field)


def _validate_classification(classification: Any, field: str) -> None:
    if classification == "local_only":
        _refuse("local_only", field)
    if classification != "outbound_safe":
        _refuse("classification_unknown", field)


def validate_structured_fields(value: Any, *, field: str = "payload") -> None:
    """Reject structured local-only values before a remote render is attempted."""

    if isinstance(value, ClassifiedValue):
        _validate_classification(value.classification, value.field)
        validate_structured_fields(value.value, field=value.field)
        return
    if isinstance(value, Mapping):
        if "classification" in value:
            _validate_classification(value["classification"], field)
        for key, item in value.items():
            validate_structured_fields(item, field=f"{field}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_structured_fields(item, field=f"{field}[{index}]")


def _decode_escaped_forms(text: str) -> str:
    current = text
    replacements = {"2f": "/", "5c": "\\", "3a": ":", "7e": "~"}
    for _ in range(3):
        decoded = html.unescape(unquote(current))
        decoded = decoded.replace("\\/", "/")
        decoded = _ESCAPED_CODEPOINT.sub(
            lambda match: replacements[match.group(1).casefold()], decoded
        )
        decoded = _ESCAPED_HEX.sub(
            lambda match: replacements[match.group(1).casefold()], decoded
        )
        if decoded == current:
            break
        current = decoded
    return current


def _json_text_values(text: str) -> Iterable[str]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, UnicodeError):
        return

    def visit(item: Any) -> Iterable[str]:
        if isinstance(item, str):
            yield item
        elif isinstance(item, Mapping):
            for key, nested in item.items():
                yield str(key)
                yield from visit(nested)
        elif isinstance(item, list):
            for nested in item:
                yield from visit(nested)

    yield from visit(value)


def _public_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return "." in hostname
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
    )


def _strip_approved_urls(text: str, approved_urls: frozenset[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        candidate = match.group(0).rstrip(".,);]")
        suffix = match.group(0)[len(candidate) :]
        if candidate not in approved_urls or not _public_url(candidate):
            _refuse("private_or_unapproved_endpoint", "payload")
        return "<approved-public-url>" + suffix

    return _URL.sub(replace, text)


def _validate_text_fragment(
    text: str, *, approved_urls: frozenset[str], field: str
) -> None:
    checked = _strip_approved_urls(text, approved_urls)
    checked = _APPROVED_LEAGUE_ID.sub("<league-id>", checked)
    checks: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("file_url", _FILE_URL),
        ("home_alias", _HOME_ALIAS),
        ("absolute_path", _ABSOLUTE_UNIX_PATH),
        ("absolute_path", _WINDOWS_PATH),
        ("absolute_path", _GENERIC_ABSOLUTE_PATH),
        ("runtime_identifier", _LABELED_RUNTIME_ID),
        ("runtime_identifier", _PANE_ADDRESS),
        ("runtime_identifier", _BARE_UUID),
        ("process_identifier", _PID),
        ("socket", _SOCKET),
        ("local_identity", _LOCAL_IDENTITY),
        ("private_endpoint", _LOOPBACK),
        ("private_endpoint", _PRIVATE_IPV4),
        ("private_endpoint", _LOCAL_HOST),
        ("secret_material", _SECRET_ASSIGNMENT),
        ("secret_material", _SECRET_MATERIAL),
        ("personal_data", _EMAIL),
        ("personal_data", _PHONE),
        ("personal_data", _PERSONAL_FIELD),
    )
    for category, pattern in checks:
        if pattern.search(checked):
            _refuse(category, field)


def _validate_text(text: str, *, approved_urls: frozenset[str], field: str) -> None:
    normalized = _decode_escaped_forms(text)
    _validate_text_fragment(normalized, approved_urls=approved_urls, field=field)
    for item in _json_text_values(normalized):
        _validate_text_fragment(
            _decode_escaped_forms(item), approved_urls=approved_urls, field=field
        )


def validate_final_rendered_payload(
    payload: bytes | str,
    *,
    destination_visibility: str,
    mode: str = "outbound",
    structured_fields: Optional[Any] = None,
    approved_urls: Iterable[str] = (),
    field: str = "payload",
) -> ValidationReceipt:
    """Validate the exact bytes immediately before a remote transport call.

    Public and private destinations intentionally execute the identical checks.
    Local diagnostics are categorically ineligible for this remote boundary.
    """

    if destination_visibility not in DESTINATION_VISIBILITIES:
        _refuse("destination_unknown", "destination_visibility")
    if mode not in PAYLOAD_MODES:
        _refuse("payload_mode_unknown", "mode")
    if mode == "local_diagnostic":
        _refuse("local_diagnostic_remote_forbidden", "mode")
    if structured_fields is not None:
        validate_structured_fields(structured_fields)
    encoded = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if len(encoded) > MAX_OUTBOUND_BYTES:
        _refuse("payload_too_large", field)
    if b"\x00" in encoded:
        _refuse("binary_or_screenshot", field)
    try:
        text = encoded.decode("utf-8")
    except UnicodeDecodeError:
        _refuse("binary_or_screenshot", field)
    approved = frozenset(str(item) for item in approved_urls)
    if any(not _public_url(item) for item in approved):
        _refuse("private_or_unapproved_endpoint", "approved_urls")
    _validate_text(text, approved_urls=approved, field=field)
    return ValidationReceipt(
        schema="league.outbound-validation.v1",
        payload_sha256=hashlib.sha256(encoded).hexdigest(),
        byte_count=len(encoded),
        destination_visibility=destination_visibility,
    )


__all__ = [
    "ClassifiedValue",
    "PrivacyRefusal",
    "ValidationReceipt",
    "validate_final_rendered_payload",
    "validate_structured_fields",
]
