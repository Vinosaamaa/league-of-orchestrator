---
schemaVersion: 1
id: "change-note-native-start-readiness"
revision: 1
type: "change-note"
status: "draft"
title: "Wait for disposable native startup readiness"
repository: "league-of-orchestrator"
capabilityIds: []
createdAt: "2026-09-09T07:40:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Full native lifecycle acceptance awaits human directory trust."]
modules: ["acceptance"]
interfaces: ["cleanup-canary"]
seams: []
adapters: ["herdr", "codex"]
relatedRecords: ["change-note-canary-startup-recovery@1"]
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
pr: 237
release: null
run: null
---
# Wait for disposable native startup readiness

Shell setup output can arrive before the shell becomes available for native
launch. Retry only the exact atomic `agent_pane_busy` refusal, at most twenty
times with 100 ms pauses. Other failures and uncertain partial launches refuse.

The explicit `--trust-wait-seconds` option retains the disposable trust screen
for human interaction, bounded to 300 seconds. It sends no input to that gate.
After the gate clears, exact native identity and model display must match before
the normal challenge response proves readiness. Default immediate refusal and
exact failure cleanup remain unchanged.

Focused tests and the repository baseline pass. A native candidate progressed
past shell setup to directory trust. That proves the observed startup race is
resolved for that run, not that the full product lifecycle has passed.
