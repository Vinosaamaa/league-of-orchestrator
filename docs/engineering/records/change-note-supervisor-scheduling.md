---
schemaVersion: 1
id: "change-note-supervisor-scheduling"
revision: 1
type: "change-note"
status: "draft"
title: "Keep user-facing supervision out of discretionary background scheduling"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T04:14:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed startup and sustained healthy supervision require live acceptance."]
modules: ["supervisor-service"]
interfaces: ["launchd-service-install"]
seams: []
adapters: ["launchd"]
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
verification: {"state":"verified","evidenceRefs":["tests/test_supervisor_service.py: synthetic install, restart, exact rollback and scheduling-template validation."]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 66
pr: null
release: null
run: null
---
# Keep user-facing supervision out of discretionary background scheduling

## Evidence and change

The persistent supervisor serves latency-sensitive prompt and control traffic.
Its launchd template nevertheless selected `ProcessType=Background`. A bounded
comparison completed ordinary command startup in 0.889 seconds while explicit
background scheduling exceeded six seconds. This establishes scheduling-sensitive
latency, not a complete kernel-level explanation of every observed stall.

The source template and renderer now require `ProcessType=Standard`. Startup
deadlines, exact identity and source hashes, ownership fences, service backups,
and rollback checks remain unchanged. The existing exact-owner expired-lease
renewal fix remains part of the baseline.

## Acceptance boundary

Synthetic service tests pass. Local grouped baseline runs still encountered
timing failures; isolated read-only provider-hook timings were 1.226, 0.770 and
0.886 seconds without relaxing the existing two-second limit. These isolated
results do not replace the full baseline or installed acceptance.

This recovery slice does not enable background prompt classification, alter
Champion state, clear obligations, or claim that the complete orchestrator ships.
Its candidate must prove source parity, supported installation, live status,
renewal and rollback separately. The broader issue remains open.
