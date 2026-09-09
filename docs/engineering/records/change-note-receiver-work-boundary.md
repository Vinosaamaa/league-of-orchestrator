---
schemaVersion: 1
id: "change-note-receiver-work-boundary"
revision: 1
type: "change-note"
status: "draft"
title: "Separate receiver work from inline triage"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-09T02:09:11Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed busy delivery remains a separate acceptance gate."]
modules: ["delivery", "watcher"]
interfaces: ["delivery-inbox", "stop-hook"]
seams: []
adapters: ["sqlite"]
relatedRecords: []
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #23","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/23","kind":"issue"},{"label":"Issue #66","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/66","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_in_turn_inbox.py","tests/test_stop_continuation.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: null
release: null
run: null
---
# Separate receiver work from inline triage

Background triage correctly leaves the inline request transaction closed.
Using that transaction as the only durable busy signal allowed a native
idle observation to trigger a prompt during ongoing receiver work.

Persist a small independent work boundary in existing watcher metadata. Input,
inbox reads and successful native wakes mark busy. Only an allowed Stop clears
it; silence, receipt acknowledgement and request completion cannot imply idle.
The dispatch policy and installed transport both check known work. Waiting
still uses its outstanding tool, and explicit owner controls retain their path.

Regression checks reproduce the committed-inline-marker case, consume updates
without a prompt adapter, preserve work across blocked and stale Stop, and
verify allowed Stop plus subsequent native wake and checkpoint boundaries.
No new process, model call, schema migration, or cleanup relaxation is added.
