---
schemaVersion: 1
id: "change-note-supervisor-scheduling"
revision: 4
type: "change-note"
status: "draft"
title: "Keep user-facing supervision out of discretionary background scheduling"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-08T04:14:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Atomic provider idle-check-and-send is not implemented.", "Host-level wait interruption and complete installed triage, delivery, and cleanup acceptance remain pending."]
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

## Consolidated prompt classification and receive work

The same draft now includes the approved dedicated classification lane and
prompt-only ON/OFF switch. One lazily started stdio process classifies prompts;
it does not execute tasks or launch nested classifiers. Compact semantic output
is committed with canonical request IDs and its internal notification. No model
call occurs while idle. OFF preserves existing requests and Champion obligations.

An explicit inbox read and acknowledgement reuse durable outbox leases. Waiting
recipients receive a tool result, not a prompt; acknowledgement removes pending
delivery but does not complete the referenced work. Interrupted reads remain
recoverable. Fresh busy or unknown runtime observations refuse prompt delivery.

The local baseline and affected lifecycle checks pass. The grouped lifecycle run
exposed old assertions that treated a wait result as automatic acknowledgement;
corrected focused receive cases and the remaining suite passed without relaxing
deadlines or identity checks. Coverage includes two requests from one prompt,
distinct checklist IDs, no false completion, expired-read recovery, exact idle
identity, unavailable receivers, native hook activation, and supervisor renewal.

This consolidated source is not installed. The idle check and send are still
separate operations, so this draft does not satisfy the strict busy/idle race
acceptance criterion. Short database polling likewise does not prove host-level
wait interruption. Installed end-to-end acceptance and cleanup remain open.

## Bounded Stop-loop recovery

The every-Stop-blocks policy caused repeated native model continuations without
new work or user steering. The Codex hook now recognizes an already-continued
turn and reads the exact bound scope's last reported wait generation before
contacting the broker. An unchanged continuation returns empty native output;
fresh captured steering rearms the reminder. This read-only path neither clears
obligations nor claims a supervisor handoff. Initial reminders, malformed-input
validation, other providers, Champion Stop and completion evidence remain intact.

The narrow installed-base commit `fe4cf0180f9d42c0ca73905f87551bea1406d5ca`
contains only this recovery, tests and guidance, and is retained in this PR's
ancestry. Its baseline and native-generation tests passed. The staged native
launcher regression verifies repeated continuation suppression, unchanged
logical database contents, unavailable-broker independence and fresh steering.

That candidate is installed with exact source hashes and retained rollback.
Only the hook implementation and matching League guidance differ from the prior
release. No schema migration, worker activation or supervisor restart occurred.
A post-install native health probe verified all existing watcher bindings with
unchanged ownership fences. Final real-turn Stop acceptance remains the next
boundary; this is not acceptance of the unfinished consolidated worker release.
