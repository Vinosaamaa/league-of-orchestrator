---
schemaVersion: 1
id: "change-note-integrated-acceptance-entrypoint"
revision: 1
type: "change-note"
status: "draft"
title: "Run the integrated lifecycle through the acceptance entry point"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-22T19:00:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Full native prompt-to-cleanup acceptance remains separate."]
modules: ["acceptance", "precutover", "real_canary"]
interfaces: ["acceptance-run"]
seams: []
adapters: ["sqlite", "synthetic-cleanup"]
relatedRecords: ["change-note-acceptance-contract-alignment@1"]
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #23","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/23","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_acceptance_harness.py", "tests/test_pre_cutover.py", "tests/test_real_cleanup.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: null
release: null
run: null
---
# Integrated acceptance entry point

The ordinary acceptance command still emitted five fixed pending assertions
after the underlying lifecycle had become executable through pre-cutover.
It now runs that same isolated check before declaring its operation complete.
Request intake, fixture triage, assignment, receipted delivery, answer,
identity-bound synthetic cleanup, and the blocked-to-allowed Stop transition
must succeed. An injected lifecycle failure leaves a blocked, retryable
operation without a success receipt.

Successful new receipts include integrated evidence and no pending lifecycle
assertions. The schema continues to accept historical receipts only with their
pending assertions intact. Runtime support remains explicitly unverified:
fake adapters and fixture classifications cannot prove native model inference,
prompt transport, or installed end-to-end behavior. No production state or
Champion endpoint is changed by this verification.

The disposable native canary also waits for the requested Codex route and
current input screen before sending its single challenge. Process recognition
alone can precede the first native frame and lose that input. The bounded wait
retains the human-only directory-trust gate and refuses without prompting if
the requested input screen never appears.
