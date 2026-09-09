---
schemaVersion: 1
id: "change-note-compact-inbox-receipts"
revision: 1
type: "change-note"
status: "draft"
title: "Save private inbox receipts with compact terminal output"
repository: "league-of-orchestrator"
capabilityIds: []
createdAt: "2026-09-09T12:00:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["This change does not resolve provider transport stalls."]
modules: ["delivery"]
interfaces: ["delivery inbox", "delivery ack-inbox"]
seams: []
adapters: []
relatedRecords: []
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #23","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/23","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_in_turn_inbox.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: null
release: null
run: null
---
# Save private inbox receipts with compact terminal output

Copying full delivery envelopes through a visible file-edit tool floods the
terminal with bookkeeping JSON. The CLI now optionally writes that exact receipt
itself and returns a short list of event IDs, kinds, and summaries with a file
reference. The normal output contract remains available without the new flag.

The destination must be absolute and newly created with private permissions.
Existing files and symlinks refuse before any delivery claim. The CLI flushes
and syncs the receipt before returning success. A failed write never acknowledges
delivery; the existing expiring claim remains recoverable. Reading or saving a
receipt never marks a request complete.

Focused tests verify destination refusals, permissions, compact output, and
exact acknowledgement using the saved file. This change addresses terminal noise,
not model response latency, transport failures, or full product acceptance.
