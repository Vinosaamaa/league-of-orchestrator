---
schemaVersion: 1
id: "change-note-retained-worktree-retirement"
revision: 1
type: "change-note"
status: "draft"
title: "Retire completed endpoints without deleting retained worktrees"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-22T22:58:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed retirement acceptance remains pending."]
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
sources: [{"label":"Issue #23","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/23","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_production_cleanup.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: null
release: null
run: null
---
# Retire completed endpoints without deleting retained worktrees

Completed work can leave historical uncommitted files that must survive endpoint
retirement. Existing retention supports only clean standalone clones; ordinary
worktree cleanup correctly refuses dirty files.

An explicit registered-worktree retention resource now selects endpoint-only
retirement. Acceptance and release must be complete, and canonical owner,
assignment, runtime, and callsign checks remain unchanged. The read-only adapter
verifies exact Git registration, head, branch, and a fingerprint covering staged,
unstaged, and untracked bytes. Git deletion actions are forbidden. Retention does
not assert that preserved changes were merged or published. Ordinary removal and
standalone retention retain their existing requirements.

Synthetic real-Git tests exercise preservation of all three change categories,
changed-content refusal, ownership and manifest refusal, interrupted execution,
and duplicate completion. These tests do not alone prove installed retirement.
