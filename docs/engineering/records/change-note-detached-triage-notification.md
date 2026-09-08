---
schemaVersion: 1
id: "change-note-detached-triage-notification"
revision: 1
type: "change-note"
status: "draft"
title: "Notify the persistent prompt worker while its owner is detached"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T12:35:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Native human capture and installed end-to-end acceptance remain open."]
modules: ["persistent-supervision"]
interfaces: ["prompt-notification"]
seams: []
adapters: ["sqlite", "unix-socket"]
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
verification: {"state":"verified","evidenceRefs":["tests/test_persistent_supervisor.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 66
pr: null
release: null
run: null
---
# Detached prompt-worker notification

The prompt-only switch committed its setting but reported no worker notification
for detached owners. The notification helper accepted only the watcher delivery
route, conflating terminal detachment with absence of background supervision.
Durable prompts were preserved, but prompt classification and user-priority
signals could wait for background recovery instead of their immediate signal.

The direct route now resolves the owner's existing watcher registration and
checks the exact actor, runtime and unexpired lease. The existing socket protocol
verifies its fence and runtime generation. Unknown routes and stale or foreign
registrations remain silent failures with no prompt fallback. The change neither
restarts the classifier nor changes semantic classification or task completion.

A focused regression first fails against the previous implementation. It covers
both message kinds, identity mismatch, lease expiry and a fenced server refusal.
The isolated supervisor test verifies a real socket acknowledgement without
incrementing user priority or invoking the terminal wake adapter for triage.
Installed human-prompt capture and complete product acceptance remain open.
