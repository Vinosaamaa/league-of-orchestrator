"""Render stable report JSON into bounded Markdown and portable HTML."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from .privacy import validate_final_rendered_payload
from .storage_types import StorageRefusal


REPORT_FORMATS = frozenset({"json", "markdown", "html"})
MAX_RENDERED_REPORT_BYTES = 1_000_000
_TEMPLATE = Path(__file__).with_name("report_template.html")


def _json_bytes(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _display(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _scope_label(identity: dict[str, Any]) -> str:
    scope = identity["scope"]
    return scope["kind"] if scope["id"] is None else f"{scope['kind']}:{scope['id']}"


def _markdown(report: dict[str, Any]) -> bytes:
    identity = report["report"]
    completion = report["completion"]
    lines = [
        f"# League report {identity['report_id']}",
        "",
        f"**Everything finished:** `{str(completion['everything_finished']).lower()}`  ",
        f"**Completion status:** `{completion['status']}`  ",
        f"**Range:** `{identity['from']}` {'through' if identity['from_inclusive'] else 'after'} `{identity['to']}`  ",
        f"**Timezone:** `{identity['timezone']}`  ",
        f"**Scope:** `{_scope_label(identity)}`  ",
        f"**Watermark:** `{identity['event_watermark']}`  ",
        f"**Source watermark:** `{identity['source_watermark']}`  ",
        f"**Specification:** `{identity['spec_hash']}`  ",
        f"**Content:** `{identity['content_hash']}`",
        "",
        "## Completion gates",
        "",
        "| Gate | Status | Count |",
        "|---|---:|---:|",
    ]
    lines.extend(
        f"| {gate['kind']} | {gate['status']} | {gate['count']} |"
        for gate in completion["gates"]
    )
    lines.extend(["", "## Chronological evidence", ""])
    if not report["chronological"]:
        lines.append("No evidence in this page.")
    for fact in report["chronological"]:
        owner = fact["owner"] or {}
        callsign = owner.get("callsign") or owner.get("actor_id") or "unowned"
        gaps = ", ".join(fact["gaps"]) if fact["gaps"] else "none"
        lines.extend(
            [
                f"### {fact['occurred_at']} · {fact['category']} · {fact['action']}",
                "",
                f"- Owner: `{callsign}`",
                f"- Subject: `{fact['subject']['kind']}:{fact['subject']['id']}`",
                f"- State: `{_display(fact['state'])}`",
                f"- Verification: `{fact['verification']}`",
                f"- Summary: {fact['summary']}",
                f"- Gaps: `{gaps}`",
                "",
            ]
        )
    lines.extend(["## Owner groups", ""])
    for group in report["owner_grouped"]:
        owner = group["owner"] or {}
        label = owner.get("callsign") or owner.get("actor_id") or "unowned"
        lines.append(f"- `{label}`: {group['count']} fact(s)")
    lines.extend(["", "## Recurring repairs", ""])
    groups = report["recurring_repairs"]["groups"]
    if not groups:
        lines.append("No recurring repair evidence in this report.")
    for group in groups:
        lines.append(
            f"- `{group['stable_id']}`: {group['repetitions']} event(s), "
            f"final state `{_display(group['final_state'])}`"
        )
    if report["pagination"]["next_cursor"]:
        lines.extend(["", f"Next cursor: `{report['pagination']['next_cursor']}`"])
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _fact_card(fact: dict[str, Any]) -> str:
    owner = fact["owner"] or {}
    owner_label = owner.get("callsign") or owner.get("actor_id") or "unowned"
    gaps = " · ".join(fact["gaps"]) if fact["gaps"] else "none"
    return "".join(
        (
            '<article class="fact">',
            '<div class="fact-time">', html.escape(fact["occurred_at"]), "</div>",
            '<div class="fact-main"><div class="fact-line">',
            '<span class="category">', html.escape(fact["category"]), "</span>",
            '<strong>', html.escape(fact["action"]), "</strong>",
            '<span class="state">', html.escape(_display(fact["state"])), "</span>",
            "</div>",
            '<p class="summary">', html.escape(fact["summary"]), "</p>",
            '<div class="meta">owner <code>', html.escape(owner_label),
            "</code> · subject <code>", html.escape(fact["subject"]["kind"]), ":",
            html.escape(fact["subject"]["id"]), "</code> · verification <code>",
            html.escape(fact["verification"]), "</code> · gaps <code>",
            html.escape(gaps), "</code></div></div></article>",
        )
    )


def _html(report: dict[str, Any]) -> bytes:
    try:
        template = _TEMPLATE.read_text(encoding="utf-8")
    except OSError as exc:
        raise StorageRefusal("report_template_unavailable", "report HTML template is unavailable") from exc
    identity = report["report"]
    completion = report["completion"]
    gates = "".join(
        '<li><span>{}</span><strong class="{}">{}</strong><code>{}</code></li>'.format(
            html.escape(gate["kind"]),
            "settled" if gate["status"] == "settled" else "open",
            html.escape(gate["status"]),
            gate["count"],
        )
        for gate in completion["gates"]
    )
    owner_groups = "".join(
        '<li><span>{}</span><strong>{}</strong></li>'.format(
            html.escape(
                (group["owner"] or {}).get("callsign")
                or (group["owner"] or {}).get("actor_id")
                or "unowned"
            ),
            group["count"],
        )
        for group in report["owner_grouped"]
    ) or '<li><span>no owners on this page</span><strong>0</strong></li>'
    repairs = "".join(
        '<li><code>{}</code><span>{} event(s)</span><strong>{}</strong></li>'.format(
            html.escape(group["stable_id"]),
            group["repetitions"],
            html.escape(_display(group["final_state"])),
        )
        for group in report["recurring_repairs"]["groups"]
    ) or '<li><span>no recurring repairs</span><strong>settled</strong></li>'
    facts = "".join(_fact_card(fact) for fact in report["chronological"])
    if not facts:
        facts = '<div class="empty">No evidence in this page.</div>'
    values = {
        "REPORT_ID": html.escape(identity["report_id"]),
        "VERDICT": "EVERYTHING FINISHED" if completion["everything_finished"] else "WORK REMAINS",
        "VERDICT_CLASS": "finished" if completion["everything_finished"] else "unfinished",
        "COMPLETION_STATUS": html.escape(completion["status"]),
        "FACT_TOTAL": str(report["totals"]["facts"]),
        "FROM": html.escape(identity["from"]),
        "TO": html.escape(identity["to"]),
        "FROM_MODE": "inclusive" if identity["from_inclusive"] else "exclusive",
        "TIMEZONE": html.escape(identity["timezone"]),
        "SCOPE": html.escape(_scope_label(identity)),
        "WATERMARK": str(identity["event_watermark"]),
        "SOURCE_WATERMARK": html.escape(identity["source_watermark"]),
        "SPEC_HASH": html.escape(identity["spec_hash"]),
        "CONTENT_HASH": html.escape(identity["content_hash"]),
        "REPRODUCTION": "exact" if identity["reproduction"]["matches_stored_hash"] else "mismatch",
        "GATES": gates,
        "FACTS": facts,
        "OWNERS": owner_groups,
        "REPAIRS": repairs,
        "PAGE": f"{report['pagination']['returned']} of {report['pagination']['total']}",
        "NEXT": "available" if report["pagination"]["next_cursor"] else "complete",
    }
    for marker, value in values.items():
        template = template.replace("{{" + marker + "}}", value)
    if re.search(r"\{\{[A-Z_]+\}\}", template):
        raise StorageRefusal("report_template_invalid", "report HTML template has unresolved markers")
    return template.encode("utf-8")


def render_report(report: dict[str, Any], format_name: str) -> bytes:
    """Render once from stable JSON and validate the exact outbound-safe bytes."""

    if format_name not in REPORT_FORMATS:
        raise StorageRefusal("invalid_report_format", "report format is unsupported")
    if format_name == "json":
        payload = _json_bytes(report)
    elif format_name == "markdown":
        payload = _markdown(report)
    else:
        payload = _html(report)
    if len(payload) > MAX_RENDERED_REPORT_BYTES:
        raise StorageRefusal("report_render_too_large", "rendered report exceeds its byte bound")
    if report["mode"] == "outbound_safe":
        validate_final_rendered_payload(
            payload,
            destination_visibility="public",
            approved_urls=tuple(report["approved_public_urls"]),
            field=f"report.{format_name}",
        )
    return payload


__all__ = ["MAX_RENDERED_REPORT_BYTES", "REPORT_FORMATS", "render_report"]
