---
schemaVersion: 1
id: "change-note-acceptance-contract-alignment"
revision: 1
type: "change-note"
status: "draft"
title: "Align final acceptance with current lifecycle contracts"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-09T00:45:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed full lifecycle acceptance remains a separate gate."]
modules: ["acceptance", "precutover"]
interfaces: ["acceptance-run", "acceptance-preflight"]
seams: []
adapters: ["sqlite", "synthetic-cleanup"]
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
verification: {"state":"verified","evidenceRefs":["tests/test_acceptance_harness.py", "tests/test_pre_cutover.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: null
release: null
run: null
---
# Final acceptance contract alignment

The aggregate acceptance run exposed outdated fixtures after the schema 25,
conditional wake, quiet Stop continuation, and cleanup-settlement changes.
The migration report includes its target schema twice. Substituting schema 24
in the current plan reproduces the prior pinned digest exactly; fixture-source
and imported-row digests are unchanged. Receipt schemas now match schema 25.

The synthetic cleanup adapter previously reported external success without
closing its synthetic runtime or releasing its callsign in the disposable
database. It now uses the normal storage APIs with the exact synthetic action
identities. The lifecycle records its result and answer before cleanup retires
the active assignment. No production identity, delegation, or cleanup guard is
weakened. The isolated lifecycle again reaches an answered request, receipted
cleanup, and an allowed final Stop.

The remaining test contracts assert the already-shipped idle-delivery
capability, its observation sequence, the actual migrated database version,
and quiet unchanged Stop continuation with fresh-steering rearm. These checks
do not establish installed runtime or device acceptance.
