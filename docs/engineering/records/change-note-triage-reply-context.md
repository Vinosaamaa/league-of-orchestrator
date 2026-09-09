---
schemaVersion: 1
id: "change-note-triage-reply-context"
revision: 1
type: "change-note"
status: "draft"
title: "Supply bounded native reply context to prompt triage"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-09T06:00:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed acceptance is pending; Stop-only context does not include in-turn commentary or other providers."]
modules: ["persistent-supervision"]
interfaces: ["prompt-classification"]
seams: []
adapters: ["codex", "sqlite"]
relatedRecords: ["change-note-ambiguous-triage-routing"]
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #66","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/66","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_triage_context.py", "tests/test_triage_codex_protocol.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 66
pr: null
release: null
run: null
---
# Context is distinct from process persistence

A reusable classifier process creates a fresh model conversation per input.
Without the preceding assistant question, short contextual replies can become
unnecessary unspecified-task requests even though capture and delivery succeed.

The native Codex Stop event already supplies the latest assistant message.
League now accepts that optional field only for one verified active/idle
Shotcaller runtime and records bounded session-local context. Inference remains
outside the database transaction. Reply context does not settle any request or
generate an outbox delivery. Empty or duplicate Stop continuations do not erase
useful context. A fresh thread receives only the bounded reply and its existing
compact classification input; no extra model call or transcript replay is added.

The synthetic fixture covers exact native entrypoint capture, unchanged prompt
bytes, bounded UTF-8 data, replay, invalid identity, foreign session/runtime/owner,
future and stale timestamps, and absence of context-generated delivery. Two real
model samples using synthetic prompts linked an explicit wait-test invitation in
10.66 seconds and preserved missing-reference uncertainty in 7.26 seconds. Those
are individual classification samples, not a latency benchmark or installed E2E.

Limits: this field is provided at Codex Stop. It does not claim equivalent native
reply capture for other adapters or commentary delivered during an unfinished
turn. Captured reply text remains local diagnostic/context data and must not be
included in public evidence exports.
