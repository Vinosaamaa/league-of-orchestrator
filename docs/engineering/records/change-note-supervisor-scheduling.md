---
schemaVersion: 1
id: "change-note-supervisor-scheduling"
revision: 2
type: "change-note"
status: "draft"
title: "Keep user-facing supervision out of discretionary background scheduling"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T04:14:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Complete installed triage, delivery, and cleanup acceptance remains pending."]
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

Synthetic service tests and the full baseline passed against the immutable
committed-source export. Earlier grouped runs encountered timing failures;
isolated read-only provider-hook timings were 1.226, 0.770 and 0.886 seconds
without relaxing the existing two-second limit.

The scheduling candidate installed through the supported installer. Two native
health probes across renewal verified all three existing bindings with unchanged
ownership fences. A restricted probe returned a socket PermissionError while
the unrestricted probe remained healthy; the generic process-unreachable label
was not proof of a dead service. The source diagnostic now reports
`probe_permission_denied` in that case, preserving the failed health result and
all ownership gates. Synthetic coverage checks aggregate and single-owner
permission failures separately from refused connections. This diagnostic change
is not yet installed.

This recovery slice does not enable background prompt classification, alter
Champion state, clear obligations, or claim that the complete orchestrator ships.
The installed candidate proves source parity, supported installation, live status
and renewal; rollback remains backed by the retained installer receipt and
synthetic rollback test, not a destructive live rollback rehearsal. The broader
issue remains open.
