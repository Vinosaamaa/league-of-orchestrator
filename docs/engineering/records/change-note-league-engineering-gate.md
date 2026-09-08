---
schemaVersion: 1
id: "change-note-league-engineering-gate"
revision: 1
type: "change-note"
status: "released"
title: "Require authored Engineering receipts for League changes"
repository: "league-of-orchestrator"
capabilityIds: ["engineering-records"]
createdAt: "2026-09-07T23:12:25Z"
reconstructed: false
confidence: "verified"
unknowns: []
modules: ["engineering-records"]
interfaces: ["pull-request-engineering-impact"]
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
sources: [{"label":"Issue #217","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/217","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["issue:217","tests/test_engineering_gate.py: seven passing receipt, classification, exact-reference and scaffold tests.","make test: the complete existing synthetic baseline passed with the new gate tests."]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 217
pr: null
release: null
run: null
---
# Require authored Engineering receipts for League changes

League had no repository Engineering receipt gate. This change adopts the shared canonical receipt contract already used by the Arc repositories.

## Change

Every PR selects one impact classification and supplies its exact numbered receipt. Material changes reference an authored id@revision; None requires a concrete reason. The independent Ubuntu check runs on source and metadata changes, validates exact repository and PR identity, rejects immutable-record replacement, and preserves bounded historical-publication authorization. The scaffold only creates canonical Markdown. Journal ingestion remains a separate exact-commit trust decision in Interview Arc.

## Verification and delivery

tests/test_engineering_gate.py: seven passing receipt, classification, exact-reference and scaffold tests.

make test: the complete existing synthetic baseline passed with the new gate tests.

Local checks establish the implementation behavior. Hosted validation and merge remain separate delivery steps.
