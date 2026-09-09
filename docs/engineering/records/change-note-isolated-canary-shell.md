---
schemaVersion: 1
id: "change-note-isolated-canary-shell"
revision: 1
type: "change-note"
status: "draft"
title: "Isolate disposable shell startup and Python selection"
repository: "league-of-orchestrator"
capabilityIds: []
createdAt: "2026-09-09T12:00:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Full installed prompt-triage and delivery acceptance remains separate."]
modules: ["acceptance"]
interfaces: ["cleanup-canary"]
seams: []
adapters: ["herdr", "codex"]
relatedRecords: ["change-note-native-start-readiness@1"]
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #23","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/23","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_real_cleanup.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: 238
release: null
run: null
---
# Isolate disposable shell startup and Python selection

The native startup probe raced the interactive shell by sending setup commands
before launch. Removing those commands also exposed login-shell PATH selection:
the system Python loaded an older SQLite and refused an established WAL store.

Explicit YOLO canaries now select a private Zsh configuration at pane creation.
It contains only a quoted PATH prepend for the test runner's Python directory.
This avoids conflicting user functions and restores interpreter selection after
login-shell initialization. Global configuration, ordinary launches, human trust,
and exact-resource cleanup guards are unchanged.

Focused fixtures verify the scoped environment, minimal startup file and absence
of setup-command injection. Native candidate acceptance passed its random model
challenge, publication guard, interrupted cleanup recovery, six ordered cleanup
actions and final Stop allow. The native test preserved its repository and did
not perform hosted publication or retained Champion teardown. Full installed
prompt-triage and delivery acceptance is not established by that test.
