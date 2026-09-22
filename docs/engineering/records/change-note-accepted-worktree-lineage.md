---
schemaVersion: 1
id: "change-note-accepted-worktree-lineage"
revision: 1
type: "change-note"
status: "draft"
title: "Settle accepted results after a verified worktree move"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-22T23:45:00Z"
reconstructed: false
confidence: "verified"
unknowns: []
modules: ["requests"]
interfaces: ["request-result"]
seams: []
adapters: ["sqlite"]
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
verification: {"state":"verified","evidenceRefs":["tests/test_request_lifecycle.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: 246
release: null
run: null
---
# Settle accepted results after a verified worktree move

The legacy result gate compared immutable launch locations with current agent
locations, rejecting completed work after a supported move. It now reuses the
existing reconciliation verifier and binds the original location, current
location, assignment, Champion, runtime, callsign and session exactly.

The original receipt is never rewritten. Missing completion, altered intent,
unrecorded movement and identity mismatches still refuse without mutation.
Focused tests cover those refusals and successful result-to-answer settlement.
