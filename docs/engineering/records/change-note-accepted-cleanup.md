---
schemaVersion: 1
id: "change-note-accepted-cleanup"
revision: 1
type: "change-note"
status: "draft"
title: "Finish accepted cleanup without orphaned assignments"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T09:45:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed cleanup acceptance remains pending."]
modules: ["cleanup"]
interfaces: ["cleanup-execute"]
seams: []
adapters: ["git", "sqlite"]
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
verification: {"state":"verified","evidenceRefs":["tests/test_real_cleanup.py", "tests/test_production_cleanup.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 66
pr: null
release: null
run: null
---
# Finish accepted cleanup without orphaned assignments

Two inherited-cleanup failures are covered in one repair. A squash applied on
an advanced base could be accepted and installed but fail whole-tree equality.
Separately, ordinary cleanup could finish its external actions while leaving
the assignment active, with no final cleanup receipt. A focused fixture failed
with that exact leftover state before the correction.

The Git adapter retains its ancestor and identical-tree proofs. Its additional
case reconstructs a conflict-free merge of the accepted branch onto the
published squash parent and requires exact full-tree equality. It does not use
patch similarity or modify a worktree, index or reference. Git may create
unreferenced objects during reconstruction. Missing publication, conflicts and
post-acceptance branch changes refuse.

The final SQLite transaction now settles ordinary completed assignments too.
It verifies the assignment's exact closed runtime and exact callsign-release
receipt before attaching the teardown digest. Task acceptance is not changed,
and no worker is resumed. Retention and normal removal share this final step.

Real-Git synthetic fixtures verify advanced-base acceptance and preservation on
refusal. Production synthetic fixtures verify active and recovered-stale
assignments, interrupted cleanup, exact retries and retained repositories.
These checks do not establish installed or live cleanup completion.
