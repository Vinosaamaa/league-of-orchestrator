---
schemaVersion: 1
id: "change-note-champion-identity-recovery"
revision: 1
type: "change-note"
status: "draft"
title: "Recover one retained Champion without global display reconciliation"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T09:00:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed exact-target recovery and subsequent cleanup remain pending."]
modules: ["runtime-identity"]
interfaces: ["runtime-reconcile-champion-identity"]
seams: []
adapters: ["herdr"]
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
verification: {"state":"verified","evidenceRefs":["tests/test_champion_identity_recovery.py", "tests/test_runtime_identity.py", "tests/test_runtime_handoff.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 66
pr: null
release: null
run: null
---
# Recover one retained Champion without global display reconciliation

## Failure and bounded change

Retained Champions can have an old hook-derived generation even when their
immutable session survives native restoration. Pre-handoff recovery correctly
refuses such an earlier mismatch. Global display reconciliation is unsuitable
because it inspects unrelated owners and requires their publication metadata.

The new owner-authorized command names one existing Champion, runtime, session,
endpoint and expected generation. It validates the calling Shotcaller and the
unique native session, exact worktree, route and provider. Two process observations
must agree. The existing storage operation repeats owner and agent-version checks
inside its conditional transaction before restoring the runtime generation.

## Preservation and verification

The operation does not resume, restart, close or rename an agent. It does not
complete tasks, release callsigns or clean repositories. Existing bulk recovery
and handoff semantics are unchanged. Exact retries are idempotent. The focused
synthetic test covers process changes, duplicate sessions, wrong routes, working
directories, panes and generations, owner changes, dry runs and preserved work.

Source tests do not establish installation, native recovery or teardown. Those
remain separate gates under the parent product acceptance issue.
