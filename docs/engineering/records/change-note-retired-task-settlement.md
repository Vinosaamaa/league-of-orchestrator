---
schemaVersion: 1
id: "change-note-retired-task-settlement"
revision: 1
type: "change-note"
status: "draft"
title: "Settle accepted tasks after historical cleanup"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-09T01:10:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed historical task reconciliation remains a separate gate."]
modules: ["cleanup"]
interfaces: ["cleanup-execute"]
seams: []
adapters: ["sqlite"]
relatedRecords: ["change-note-prior-cleanup-settlement@1"]
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #23","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/23","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_production_cleanup.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: null
release: null
run: null
---
# Settle accepted tasks after historical cleanup

Historical owner acceptance could complete a Champion and its cleanup while
leaving its task ready to land. Ordinary task transitions correctly refuse
that now-closed runtime, so reopening it is not a safe settlement mechanism.

Completed cleanup reconciliation now recognizes only the exact already-accepted
Champion and ready-to-land task after revalidating immutable archived proof,
action receipts, the closed runtime, released callsign and owner binding.
It records completion through the existing atomic task-transition/outbox writer.
It does not reopen a runtime, replay teardown, answer requests, or alter the
original cleanup receipt. Exact repeats do not emit another event.

The synthetic regression reproduces the stale task, rejects receipt and owner
conflicts without partial writes, preserves an unaccepted task, and checks
one completion/outbox, stable cleanup receipts and idempotent retry.
