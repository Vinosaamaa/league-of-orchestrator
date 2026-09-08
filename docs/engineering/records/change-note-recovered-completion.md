---
schemaVersion: 1
id: "change-note-recovered-completion"
revision: 1
type: "change-note"
status: "draft"
title: "Accept completed work after exact runtime recovery"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T10:20:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed recovered task acceptance remains pending."]
modules: ["assignments"]
interfaces: ["task-transition"]
seams: []
adapters: ["sqlite"]
relatedRecords: ["change-note-accepted-cleanup@1"]
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #66","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/66","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_assignment_dispatch.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 66
pr: null
release: null
run: null
---
# Accept completed work after exact runtime recovery

Runtime recovery deliberately preserves task and assignment state. Consequently
an accepted retained Champion whose assignment was fenced as stale could never
report its finished task: the ordinary transition required an active assignment.

Allow only completion for a cleanup-pending stale-runtime assignment with an
existing launch acceptance receipt and unchanged task owner. The normal exact
runtime, verified live generation, expected task version, legal transition and
coordinator checks still apply. Working and ready-to-land updates remain refused.
No worker is restarted or assignment reactivated. Its cleanup obligation stays
pending until the separate receipt-gated cleanup operation succeeds.

The synthetic fixture reproduces the refusal, restores the exact runtime, then
verifies completion and one outbox event. Failed runtime, wrong owner, wrong
version, absent launch receipt, unrelated cleanup cause and nonterminal progress
all refuse without partial writes. An exact retry is idempotent.
