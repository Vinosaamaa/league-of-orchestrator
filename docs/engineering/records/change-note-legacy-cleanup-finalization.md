---
schemaVersion: 1
id: "change-note-legacy-cleanup-finalization"
revision: 1
type: "change-note"
status: "draft"
title: "Recover fully receipted cleanup with a legacy callsign"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T10:00:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed final-receipt recovery remains pending."]
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
# Recover fully receipted cleanup with a legacy callsign

The prior finalization check wrongly required a runtime pointer in an older
callsign record. Its exact agent binding and release digest were present, and
the task assignment independently named the exact closed runtime. Installed
cleanup finished every external action but refused its final database receipt.
The fixture had always populated this optional pointer and missed the case.

Permit a null legacy callsign pointer while refusing a non-null conflict. Keep
the exact task-assignment/runtime/owner and callsign-release checks.

For this already-finished-effects state, the existing execute command may claim
a new fence only when the previous final receipt reports an identity mismatch
and every action has its completion receipt. Preserve that previous receipt
verbatim in an audit event in the same transaction that clears its current slot.
The ordinary executor reinspects every completed action and refuses any changed
external state; it never reapplies those actions. Incomplete-action blocks are
not made retryable. No schema migration or worker restart is introduced.

The production fixture reproduces the legacy null pointer and an interruption
followed by final-receipt failure, then verifies recovery, preserved failure
history, assignment settlement and exact duplicate execution.
