---
schemaVersion: 1
id: "change-note-native-prompt-intake"
revision: 1
type: "change-note"
status: "draft"
title: "Separate durable prompt intake from watcher availability"
repository: "league-of-orchestrator"
capabilityIds: ["prompt-intake"]
createdAt: "2026-09-08T00:13:53Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed native composer latency and persistent watcher startup remain unverified."]
modules: ["canonical-watcher", "request-turn"]
interfaces: ["native-prompt-hook", "request-turn-pipe"]
seams: []
adapters: ["codex", "cursor", "pi"]
relatedRecords: []
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #66","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/66","kind":"issue"},{"label":"Pull request #219","url":"https://github.com/Vinosaamaa/league-of-orchestrator/pull/219","kind":"pull-request"}]
verification: {"state":"verified","evidenceRefs":["tests/test_canonical_watcher.py: native capture with an unresponsive supervisor and preserved mutation refusal.","tests/test_canonical_watcher.py: committed bytes survive notification failure and exact native or broker replay.","tests/test_persistent_supervisor.py: complete synthetic suite passed in isolation."]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 66
pr: 219
release: null
run: null
---
# Separate durable prompt intake from watcher availability

Native prompt intake previously tried the persistent broker before a direct fallback. An unresponsive service could retain its ownership lock or socket and reject human input with `supervisor_ownership_uncertain`. The existing tests explicitly required this refusal.

## Change

Codex, Cursor and Pi native prompt hooks now validate the exact runtime and commit the original prompt through canonical SQLite before notifying the watcher. Binding is checked again inside the capture transaction. Existing invocation identity, duplicate suppression and user-priority generation are retained. Notification uses its existing quarter-second bound; notification failure cannot reject already-committed input. This does not take supervisor ownership or relax Stop and mutation-authorization protections.

The same PR refuses terminal stdin for `request turn` before storage or claims. The supported one-process pipe avoids terminal line-buffer truncation and stalls.

## Verification boundary

The stalled-supervisor regression failed before the fix and passes for all three providers afterward. A separate reader sees committed bytes before an injected notification failure; exact native and delayed broker retries retain one prompt and one generation advance. The canonical watcher suite passes. The persistent-supervisor suite passes in isolation after a concurrent run exceeded one existing two-second timing assertion by approximately 0.07 seconds; that threshold was not relaxed.

These are synthetic source results. They do not prove installed native composer delivery, a healthy OS-managed watcher, or complete product acceptance. The previously recorded separate-model benchmark is diagnostic only and is not evidence of an extra classifier in normal prompt handling.
