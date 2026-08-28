#!/usr/bin/env python3
"""Focused final-rendered-payload and guarded remote-adapter regressions."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from league.privacy import ClassifiedValue, PrivacyRefusal, validate_final_rendered_payload  # noqa: E402
from league.remote_adapters import (  # noqa: E402
    REMOTE_ADAPTER_KINDS,
    RemoteOutcomeUnknown,
    RenderedPayload,
    remote_adapter,
)


class FakeTransport:
    def __init__(self, receipt_id: str = "synthetic-remote-receipt") -> None:
        self.calls: list[bytes] = []
        self.idempotency_keys: list[str] = []
        self.receipt_id = receipt_id

    def send(self, payload: bytes, *, idempotency_key: str) -> dict[str, str]:
        self.calls.append(payload)
        self.idempotency_keys.append(idempotency_key)
        return {"receipt_id": self.receipt_id}


def refusal(payload: str, category: str, visibility: str = "public") -> None:
    try:
        validate_final_rendered_payload(payload, destination_visibility=visibility)
    except PrivacyRefusal as exc:
        assert exc.category == category, (payload, exc.category, category)
        assert payload not in str(exc)
        assert "category=" in str(exc) and "field=" in str(exc)
        return
    raise AssertionError(f"expected {category}: {payload!r}")


def test_rejection_matrix() -> None:
    cases = (
        ("/Users/example/Projects/repository", "absolute_path"),
        ("\\u002fUsers\\u002fexample\\u002fworktree", "absolute_path"),
        ("%2Fprivate%2Ftmp%2Farchive", "absolute_path"),
        ("file:///tmp/report.html", "file_url"),
        ("~/Library/Application Support/profile", "home_alias"),
        ("thread_id=01900000-0000-4000-8000-000000000000", "runtime_identifier"),
        ("pane=w1:p44", "runtime_identifier"),
        ("pid=42000", "process_identifier"),
        ("socket=/tmp/synthetic.sock", "absolute_path"),
        ("http://127.0.0.1:8080/private", "private_or_unapproved_endpoint"),
        ("10.2.3.4", "private_endpoint"),
        ("password=synthetic-secret", "secret_material"),
        ("ghp_abcdefghijklmnopqrstuvwxyz123456", "secret_material"),
        ("candidate: synthetic person", "personal_data"),
        ("person@example.invalid", "personal_data"),
        ("Traceback at /var/folders/synthetic/log.py", "absolute_path"),
    )
    for payload, category in cases:
        refusal(payload, category)
        refusal(payload, category, "private")
    nested = "/Users/example/Projects/repository"
    for _ in range(4):
        nested = quote(nested, safe="")
    refusal(nested, "absolute_path")
    excessive = "/Users/example/Projects/repository"
    for _ in range(17):
        excessive = quote(excessive, safe="")
    refusal(excessive, "encoding_depth")


def test_allowlist_and_structured_classification() -> None:
    url = "https://github.com/Vinosaamaa/league-of-orchestrator/issues/25"
    allowed = (
        "docs/PRIVACY.md\n"
        "project:league\nrequest:req-22\ntask:reporting\n"
        f"{url}\n{'a' * 64}\n<local-only>\n<unknown>"
    )
    receipt = validate_final_rendered_payload(
        allowed, destination_visibility="public", approved_urls=(url,)
    )
    assert receipt.byte_count == len(allowed.encode("utf-8"))
    try:
        validate_final_rendered_payload(
            "safe body",
            destination_visibility="private",
            structured_fields=ClassifiedValue("hidden", "local_only", "task.worktree"),
        )
    except PrivacyRefusal as exc:
        assert exc.category == "local_only" and exc.field == "task.worktree"
        assert "hidden" not in str(exc)
    else:
        raise AssertionError("structured local_only value reached outbound rendering")
    for classification, category in (
        ("local_only", "local_only"),
        ("future_classification", "classification_unknown"),
    ):
        try:
            validate_final_rendered_payload(
                "safe body",
                destination_visibility="private",
                structured_fields={"classification": classification, "value": "hidden"},
            )
        except PrivacyRefusal as exc:
            assert exc.category == category
        else:
            raise AssertionError("mapping classification diverged from ClassifiedValue")
    refusal("safe body", "local_diagnostic_remote_forbidden") if False else None
    try:
        validate_final_rendered_payload(
            "safe body", destination_visibility="public", mode="local_diagnostic"
        )
    except PrivacyRefusal as exc:
        assert exc.category == "local_diagnostic_remote_forbidden"
    else:
        raise AssertionError("local diagnostic became remotely targetable")


def test_live_state_issue_body_incident_regression() -> None:
    transport = FakeTransport()
    adapter = remote_adapter("github_issue", "private", transport)
    live_state = {
        "task_id": "task:incident-regression",
        "status": "ready_to_land",
        "worktree": "/Users/example/Projects/worktrees/incident",
    }
    rendered = "\n".join(f"{key}: {value}" for key, value in live_state.items()).encode()
    try:
        adapter.send(RenderedPayload(rendered))
    except PrivacyRefusal as exc:
        assert exc.category == "absolute_path"
        assert transport.calls == []
        assert live_state["worktree"] not in str(exc)
    else:
        raise AssertionError("unsafe live-state issue body reached its network transport")

    sanitized = b"task:incident-regression\nstatus: ready_to_land\nworktree: <local-only>"
    receipt = adapter.send(RenderedPayload(sanitized))
    assert transport.calls == [sanitized]
    assert receipt["redacted"] is True and "receipt_id" not in receipt
    assert receipt["idempotency_key"] == transport.idempotency_keys[0]


def test_every_remote_adapter_uses_same_guard() -> None:
    for kind in sorted(REMOTE_ADAPTER_KINDS | {"future_league_remote"}):
        transport = FakeTransport()
        adapter = remote_adapter(kind, "public", transport)
        try:
            adapter.send(RenderedPayload(b"session_id=synthetic-private-session"))
        except PrivacyRefusal as exc:
            assert exc.category == "runtime_identifier"
            assert transport.calls == []
        else:
            raise AssertionError(f"{kind} bypassed final payload validation")

    oversized = FakeTransport("x" * 513)
    adapter = remote_adapter("future_remote", "private", oversized)
    try:
        adapter.send(RenderedPayload(b"safe bounded body"))
    except RemoteOutcomeUnknown as exc:
        assert exc.code == "remote_outcome_unknown"
        assert oversized.calls == [b"safe bounded body"]
        assert oversized.idempotency_keys == [exc.idempotency_key]
        assert len(exc.idempotency_key) == 64
    else:
        raise AssertionError("oversized remote receipt identity was treated as delivered")


def main() -> None:
    test_rejection_matrix()
    test_allowlist_and_structured_classification()
    test_live_state_issue_body_incident_regression()
    test_every_remote_adapter_uses_same_guard()
    print("PASS: identical fail-closed outbound boundary, incident regression, and redacted receipts")


if __name__ == "__main__":
    main()
