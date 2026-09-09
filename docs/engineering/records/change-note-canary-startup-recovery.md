---
schemaVersion: 1
id: "change-note-canary-startup-recovery"
revision: 1
type: "change-note"
status: "draft"
title: "Refuse and recover blocked disposable canary startup"
repository: "league-of-orchestrator"
capabilityIds: []
createdAt: "2026-09-09T06:47:57.822Z"
reconstructed: false
confidence: "verified"
unknowns: ["Full installed product lifecycle acceptance remains open."]
modules: ["acceptance"]
interfaces: ["cleanup-canary"]
seams: []
adapters: ["herdr", "codex"]
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
verification: {"state":"verified","evidenceRefs":["tests/test_real_cleanup.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: 236
release: null
run: null
---
# Refuse and recover blocked disposable canary startup

A start result is not proof that the native directory-trust gate has cleared.
Inspect the disposable startup screen before sending readiness input, including
when the start command refuses but leaves a native process running. A detected
trust gate records an explicit refusal; it is never accepted automatically.

Failure cleanup rechecks the exact named Codex pane and trust screen, cancels
that startup with Ctrl-C, and requires the agent to disappear before existing
exact-resource cleanup continues. Changed identity, changed screen, or a process
that remains live preserves the failed resources and refuses cleanup. Normal
readiness still requires a genuine challenge reply, not echoed prompt text.

Command diagnostics expose only literal categories and numeric exits; combined
failures preserve both refusal codes without terminal contents or arguments.
Focused fixtures cover successful and refused starts at trust, zero prompt
submission, exact cancellation, changed identity, stuck exit, and data exclusion.

The explicit `--codex-yolo` acceptance option forwards the native bypass flag to
this disposable Codex invocation only. It defaults off and removes an interactive
`codex` shell function only inside the newly created disposable pane, verifying
binary command resolution before launch. No global shell configuration, trust
record, or retained agent changes; hook trust is not bypassed. The directory-trust
refusal remains in place if the native gate appears.
