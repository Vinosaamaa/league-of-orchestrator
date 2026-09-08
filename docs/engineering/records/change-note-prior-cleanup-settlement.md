---
schemaVersion: 1
id: "change-note-prior-cleanup-settlement"
revision: 1
type: "change-note"
status: "draft"
title: "Settle assignments from existing completed cleanup receipts"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T10:50:50Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed reconciliation of historical assignment rows remains pending."]
modules: ["cleanup"]
interfaces: ["cleanup-execute"]
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
verification: {"state":"verified","evidenceRefs":["tests/test_production_cleanup.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 66
pr: null
release: null
run: null
---
# Settle assignments from existing completed cleanup receipts

Older cleanup operations finished external actions and their final receipt but
left an active assignment. Fixing new finalizations did not settle these rows:
the completed-operation retry returned before reaching assignment settlement.

The existing execute path still reinspects completed actions without applying
them. Finalization additionally verifies the final receipt against the complete
action-receipt digest and current policy, then applies the same exact closed
runtime, released callsign and assignment checks as new cleanup. It updates only
an unsettled assignment and records one audit event. Original cleanup actions,
operation, final receipt and task state remain unchanged. Subsequent exact
retries make no assignment write. Conflicting receipts refuse atomically.

The synthetic production fixture models a historical active assignment after
successful cleanup and checks reconciliation, immutable original operation,
one audit event, conflicting-receipt refusals and idempotent retry.
