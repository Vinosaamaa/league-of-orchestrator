# Scoped autonomous delivery and issue-first delegation

Issue [#81](https://github.com/Vinosaamaa/league-of-orchestrator/issues/81)
adds one durable policy named `autonomous_delivery`; **YOLO mode** is only its
owner-facing alias. The Shotcaller acts as itself under a Summoner-issued grant.
It never impersonates the Summoner, transfers authority to a Champion, or
bypasses provider and platform safety.

## Defects and root causes

The prior lifecycle recorded request ownership, Champion assignment, repository
artifacts, landing evidence, and cleanup evidence separately, but had no
canonical object that answered which irreversible actions a Shotcaller could
perform without returning for authority. Treating a prompt or an informal
approval sentence as that object made scope, expiry, revocation, limits, and
retries ambiguous.

Visible assignment also accepted an issue number as unverified metadata. It did
not prove that the issue belonged to the named repository, remained open, or
contained durable scope, acceptance, and authority boundaries. A captured
prompt could therefore become the effective work container even though prompts
are intake evidence rather than issue-owned implementation state.

The first issue-first draft still began after issue creation. It could verify a
new issue without proving that the Shotcaller had searched equivalent open and
closed work, which allowed two concurrent or repeated requests to create the
same durable container. Job Journey #216/#217 is the regression receipt: #216
already held the work and #217 was later closed as its duplicate.

Finally, routing described the direct-tiny boundary through signals, but did not
make every durable work kind authoritative. A caller could label repository,
research, benchmark, release, operational, reproduction, debugging, or bug-fix
work as bounded direct or hidden work and rely on inconsistent signal input.

## Decisions

Migration v16 adds immutable authorization grants, one exact delivery goal per
active grant revision, separate revocation receipts, bounded external-action
uses, repair obligations, and immutable repository-issue bindings. Scope changes
create the next grant revision; an old grant remains evidence but cannot
authorize a new action. SQLite triggers reject grant and issue-binding mutation.

The same migration adds one CAS-fenced issue-scope lease and immutable
per-task selection receipts. `league issue select` normalizes the requested
title and the issue Objective/Scope words, searches every bounded GitHub page
across open and closed issues, and records `reuse_open`, `reopen_closed`, or
`create_distinct`. Open matches win deterministically. Closed recurrence uses
the existing authorize/effect/settle action path, then exact selection retry
after the owner API reports open; its settled receipt preserves any prior task,
assignment, Champion, runtime, and session linkage. A create that
crashes before settlement is recovered by searching again after the lease,
rather than issuing another create.

`league mode authorize` accepts a strict `league.autonomous-grant.v1` document.
It records issuer and Shotcaller identities, exact goal, project/repository,
environment and deployment-target scope, allowed actions, exclusions,
sensitive inclusions, resource boundary, start/expiry, limits, revision,
version, and a canonical digest. Missing authority remains `manual` with goal
state `awaiting_authority`.

`league mode use` is the only autonomous external-action entrance. It validates
the active revision, Shotcaller owner, time window, exact scope, action list,
exclusions, sensitive categories, nested resource boundary, and configured
attempt, concurrency, cost, changed-file, and duration totals in one
transaction. The returned digest is idempotent evidence for later landing,
deployment, verification, issue-reopen, or cleanup receipts.

`league mode settle` retains the external result digest. A failure cannot become
delivery success: it moves the goal to `repair_pending` and creates or advances
one bounded repair obligation. Successful verification reaches `delivered`;
successful cleanup reaches `cleaned`. `league mode transition` permits only
checked non-external edges, and `league mode revoke` prevents every new use
while retaining already-started action evidence.

Before the production `league assign run` path reserves a callsign, it proves
the supplied selection digest from canonical SQLite and reads the
exact GitHub issue from the repository owner API. The issue must match the
repository and number and record scope, acceptance, and authority boundaries.
Only a public issue locator, title, body digest, canonical task-scope digest,
state, verifier kind, and receipt digest enter SQLite; issue body bytes do not.
The issue body, normalized title, semantic-scope digest, and canonical task
scope must match the durable selection. Missing, unproven, wrong-repository,
scope-mismatched, changed, and closed issues refuse.

Direct answers and acknowledgements remain issue-free. A read-only check may be
direct only when it is pre-bounded, answer-or-routing-only, at most five minutes
and two task actions, and creates no artifact, mutation, test, benchmark,
browser operation, reproduction, or implementation. Durable research,
benchmarks, release and operational work, repository reproduction, debugging,
bug fixes, tests, migrations, and repository/configuration writes require a
visible Champion and an issue-owned worktree. Hidden scientists may advise only
inside the same bounded read-only perimeter.

## Rejected alternatives

- Prompt text as authority was rejected because intake evidence has no immutable
  revision, expiry, limit, or revocation identity.
- A mutable “YOLO enabled” flag was rejected because scope edits would erase the
  authority that explained earlier actions.
- Action checks performed after an external tool call were rejected because a
  refusal cannot undo the external effect.
- Storing full issue bodies was rejected because a digest proves binding without
  duplicating public or later-sensitive content in canonical state and exports.
- Trusting an issue number supplied to the launcher was rejected because it
  does not establish repository ownership, state, or durable scope.
- Searching only open issues or only exact title bytes was rejected because it
  misses closed recurrence and trivial case/punctuation differences.
- Performing search then create without a SQLite owner fence was rejected
  because concurrent Shotcallers could both observe absence and create.
- Letting hidden workers own implementation was rejected because it removes the
  visible issue/worktree owner required for review, delivery, and cleanup.

## Invariants

- Manual is the default; every autonomous use names one active immutable grant.
- The external-action owner is always the granted live Shotcaller, never a
  Champion or hidden scientist.
- New uses refuse stale, future, expired, revoked, conflicting, or ambiguous
  authority and every out-of-scope or over-limit action.
- Platform-safety bypass, unavailable permission, ambiguous target, provider
  restriction, and unsupported cleanup remain unconditional refusals.
- Verification failure creates repair work; it never implies delivery or
  teardown.
- Assignment issue verification completes before callsign reservation, task
  creation, adapter launch, or tab creation.
- One repository/title/semantic-scope lease owns issue creation at a time;
  every task gets an immutable selection receipt before assignment.
- Grant, action, repair, issue, backup, export, installation, deployment,
  production verification, and cleanup receipts remain separate facts.

## Migration, rollback, and evidence

Migration v16 is contiguous after v15 and uses the existing checksummed,
transactional migration ledger. Upgrading an existing store still requires the
normal verified pre-migration SQLite backup. The existing online-backup path
copies and verifies v16 rows, and rollback exports include them as non-canonical
records. Inspection exports redact exact goals, issuer identity, scope/resource
details, action scope/risk/resource data, repair failures, and issue titles.

Focused synthetic tests use temporary SQLite roots and fake GitHub/Herdr
adapters. They cover default manual status, grant retry/CAS, expiry, revocation,
scope and sensitive refusal, configured usage limits, Shotcaller-only ownership,
repair creation, backup/restore, redacted and rollback export,
missing/wrong/closed/mismatched/unproven issue refusal, open-equivalent reuse,
authorized closed recurrence with prior linkage, distinct-scope creation,
concurrent-create serialization, valid issue-first launch, exact retry, and the
expanded direct/hidden routing boundary.

## Remaining limits and cutover boundary

The issue selector/verifier currently supports GitHub through the installed
`gh api` owner surface; other forges fail closed. Semantic matching is a
deterministic normalization of the title and Objective/Scope words, so the
Shotcaller remains responsible for authoring one canonical scope and deciding
whether genuinely changed work is distinct. A successful repository
test or pull request does not prove merge, release, installation, deployment,
production verification, or cleanup. Those actions still require their exact
authority and receipts, and installed/live acceptance remains a separate gate.
