---
schemaVersion: 1
id: "change-note-compact-stop-feedback"
revision: 1
type: "change-note"
status: "draft"
title: "Keep Stop reminders compact without clearing obligations"
repository: "league-of-orchestrator"
capabilityIds: []
createdAt: "2026-09-09T12:00:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Native provider connection failures remain independent of Stop formatting."]
modules: ["supervision"]
interfaces: ["Stop"]
seams: []
adapters: ["codex", "cursor", "pi"]
relatedRecords: ["change-note-compact-inbox-receipts@1"]
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #23","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/23","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_shotcaller_stop.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: null
release: null
run: null
---
# Keep Stop reminders compact without clearing obligations

Stop reminders previously repeated up to ten request summaries and ten prompt
bodies, making a long-standing queue dominate the terminal. They now show exact
counts for the same obligation categories. The query no longer loads prompt
bodies for display; canonical request inspection still retains the details.

The change does not alter blocker decisions, completion state, delivery receipt
semantics, or fresh-user priority. Feedback hashes continue to bind the exact
emitted text. Tests cover long retained prompts, compact output, exact feedback
consumption, and unchanged-generation continuation suppression.
