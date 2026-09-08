---
schemaVersion: 1
id: "change-note-ambiguous-triage-routing"
revision: 1
type: "change-note"
status: "draft"
title: "Preserve uncertainty in background prompt classification"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T21:10:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Broad semantic accuracy and installed acceptance remain unproven."]
modules: ["persistent-supervision"]
interfaces: ["prompt-classification"]
seams: []
adapters: ["codex", "sqlite"]
relatedRecords: []
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #66","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/66","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_triage_codex_protocol.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 66
pr: null
release: null
run: null
---
# Ambiguous reference preservation

The background classifier receives a prompt and candidate request summaries,
not the recipient's full recent conversation. An unspecified reference was
incorrectly resolved to an unrelated candidate. Successful capture, inference
and inbox delivery therefore did not establish correct semantic routing.

Instructions now treat candidates as possible matches rather than evidence of
an omitted subject. An ambiguous ask remains an unlinked request with uncertainty
preserved; factual context remains context. Explicitly named follow-ups can
still link. This does not replay uncaptured conversation or change prompt bytes.

A second model sample exposed a structured-output gap: context could carry a
request reference although canonical commit rejects it. The output schema now
encodes the existing kind/reference/delay rules. No storage guard is weakened.

The synthetic protocol fixture verifies both instruction fields and all schema
variants are supplied on each request. Two real classifier samples, in one
owned process with synthetic temporary state, verify an unknown reference stays
unlinked and an explicit hook-repair follow-up links to the hook request. Both
outputs pass the actual canonical triage commit. Their inference durations were
approximately 10.7 and 9.1 seconds; these are individual model samples, not a
latency benchmark or installed end-to-end acceptance. The process was reaped.

Unknown-reference routing is a model instruction, not a deterministic guarantee
of semantic accuracy. Broader accuracy and installed worker behavior remain
separate gates. Receipt acknowledgement does not mark any request completed.
