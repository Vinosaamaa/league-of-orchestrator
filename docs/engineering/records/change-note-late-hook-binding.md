---
schemaVersion: 1
id: "change-note-late-hook-binding"
revision: 1
type: "change-note"
status: "draft"
title: "Reconcile an exact hook registration added after the frozen snapshot"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T11:56:40Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed individual recovery remains pending."]
modules: ["rollover"]
interfaces: ["rollover-reconcile-descendant"]
seams: []
adapters: ["sqlite", "herdr"]
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
verification: {"state":"verified","evidenceRefs":["tests/test_shotcaller_rollover.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 66
pr: null
release: null
run: null
---
# Exact post-snapshot hook binding

An imported Champion can acquire a native hook runtime after the owner freezes
its transfer snapshot. Comparing every current field against that older digest
rejects the legitimate added registration. A global snapshot refresh additionally
depends on unrelated historical descendants and is unnecessary for this case.

The individual recovery path accepts only a verified runtime with the exact
legacy hook producer hash and matching canonical actor, provider, session and
endpoint. Replacing only the runtime fields with their absent pre-registration
values must reproduce the frozen digest; exact imported provenance remains
required. The final transaction rechecks the same proof. Existing runtime and
snapshot records are preserved, including on idempotent retries.

A synthetic regression reproduces the original refusal and verifies recovery
after expiry. Additional tests change a frozen branch, runtime generation or
verification state between observation and commit and require atomic refusal.
The full rollover check and required baseline pass. This is source verification,
not a claim of installed full-product acceptance.
