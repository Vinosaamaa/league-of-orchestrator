---
schemaVersion: 1
id: "change-note-settlement-summary"
revision: 1
type: "change-note"
status: "draft"
title: "Derive settlement summaries from immutable proof"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-09T01:30:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed corrective reconciliation remains a separate gate."]
modules: ["cleanup"]
interfaces: ["cleanup-execute"]
seams: []
adapters: ["sqlite"]
relatedRecords: ["change-note-retired-task-settlement@1"]
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
# Derive settlement summaries from immutable proof

The installed reconciliation correctly completed an accepted task, but copied
agent text that cleanup had changed to callsign-release progress. The task
summary therefore described teardown instead of its acceptance evidence.

Settlement now cites the verified final cleanup receipt. One corrective event
may replace only the exact untouched task result from the earlier settlement;
the original event is retained. A later task version or different result is
not overwritten. Both paths retain all identity and archived-proof checks.

The regression uses cleanup-overwritten agent text, verifies the receipt-based
summary, preserves later task revisions, and checks one corrective outbox and
idempotent retry without altering the original event.
