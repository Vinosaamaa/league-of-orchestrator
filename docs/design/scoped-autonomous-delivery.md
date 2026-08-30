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

The initial autonomous-delivery slice also stopped at `mode use`: later
protected command gates still demanded their older one-off authority flag or
digest. A valid goal-scoped grant therefore caused repeated owner prompts for
in-scope live reconciliation, Shotcaller creation, and Squad registration, and
the protected effect had no durable semantic binding to its mode action.

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

Migration v18 adds immutable authorization grants, one exact delivery goal per
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

`league mode authorize` accepts a strict
[`league.autonomous-grant.v1`](../../schema/league-autonomous-grant.schema.json)
document.
It records issuer and Shotcaller identities, exact goal, project/repository,
environment and deployment-target scope, allowed actions, exclusions,
sensitive inclusions, resource boundary, start/expiry, limits, revision,
version, and a canonical digest. Missing authority remains `manual` with goal
state `awaiting_authority`.

`league mode use` accepts the strict
[`league.autonomous-action.v1`](../../schema/league-autonomous-action.schema.json)
document and is the only autonomous external-action entrance. It validates
the active revision, Shotcaller owner, time window, exact scope, action list,
exclusions, sensitive categories, nested resource boundary, and configured
attempt, concurrency, cost, changed-file, and duration totals in one
transaction. The returned digest is idempotent evidence for later landing,
deployment, verification, issue-reopen, or cleanup receipts.

`league mode settle` retains the external result digest. A failure cannot become
delivery success: it moves the goal to `repair_pending` and creates or advances
one bounded repair obligation. Successful verification reaches `delivered`;
successful cleanup reaches `cleaned`. `league mode transition` permits only
checked non-external edges, and `league mode revoke` accepts only the exact
Summoner identity recorded by the immutable grant. Revocation prevents every
new use while retaining already-started action evidence. Durable per-goal usage
counters make limit checks constant-size while each action receipt remains the
immutable audit source.

Migration v20 carries the same accepted grant into later protected command
gates. Each gate maps to one explicit action category: live reconciliation,
retirement, Shotcaller creation, Squad registration, or teardown. Supplying
`--mode-action` and `--expected-mode-goal-version` atomically validates the
active grant and binds the normalized action receipt to the exact command-name
and command-scope digest before the protected operation runs. Success or
failure then settles both the action and one immutable
[`league.protected-gate-receipt.v1`](../../schema/league-protected-gate-receipt.schema.json)
receipt. Rollover preparation derives its automatic authority digest from that
exact use receipt, and legacy-display reconciliation treats it as the required
owner authorization; either command refuses mixed manual and mode authority.

The protected mapping covers assignment runtime/display reconciliation,
Shotcaller creation, Squad registration/acceptance, rollover
prepare/commit/descendant/intake reconciliation, rollover drain, callsign
release, and cleanup execution/reconciliation. Landing, release, installation,
deployment, verification, smoke, repair, issue reopen, and cleanup retain their
existing explicit action-use boundary. No category is implied by the goal or
by another allowed action.

Before the production `league assign run` path reserves a callsign, it proves
the supplied selection digest from canonical SQLite and reads the
exact GitHub issue from the repository owner API. The issue must match the
repository and number and record scope, acceptance, and authority boundaries.
Only a public issue locator, title, body digest, canonical task-scope digest,
state, verifier kind, and receipt digest enter SQLite; issue body bytes do not.
The storage boundary requires the receipt for every visible Champion caller and
matches its exact repository, URL, title, task, selection receipt, and semantic
scope before reservation. The immutable assignment binding also records one
canonical digest over the task identity, normalized task/issue title, exact
repository issue, and semantic issue scope. Active retries repeat the owner-API
read and revalidate that migration-18 binding; an older active assignment with
no binding refuses for explicit reconciliation. Isolated acceptance may use the explicit
`synthetic-fixture` verifier only for reserved `.invalid` repositories; it is
never owner-API or live-runtime proof.
The canonical task summary and issue title must normalize to the same duplicate
identity. The exact title bytes returned by the owner API must still equal the
title stored by the durable selection receipt. The issue body,
semantic-scope digest, and canonical task scope must also match. Missing,
unproven, wrong-repository, title-mismatched, scope-mismatched, changed, and
closed issues refuse.

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
- A protected effect begins only after one exact grant use is durably bound to
  its command and canonical scope; every outcome durably settles that binding.
- Accepted goal-scoped authority suppresses duplicate owner prompts only for
  categories explicitly listed in the grant. Adjacent categories still refuse.
- Verification failure creates repair work; it never implies delivery or
  teardown.
- Assignment issue verification completes before callsign reservation, task
  creation, adapter launch, or tab creation.
- One repository/title/semantic-scope lease owns issue creation at a time;
  every task gets an immutable selection receipt before assignment.
- Grant, action, repair, issue, backup, export, installation, deployment,
  production verification, and cleanup receipts remain separate facts.

## Migration, rollback, and evidence

Migrations v18 and v20 are contiguous in the repository sequence and use the existing checksummed,
transactional migration ledger. Upgrading an existing store still requires the
normal verified pre-migration SQLite backup. Migration v20 adds immutable
protected-use and protected-settlement tables without rewriting v18 authority
rows. The existing online-backup path copies and verifies both sets of rows,
and rollback exports include them as non-canonical records. Inspection exports redact exact goals, issuer identity, scope/resource
details, action scope/risk/resource data, repair failures, and issue titles.
The import registry includes every authority, action, protected-gate, repair,
selection, and binding table, so collision checks and atomic import table
coverage cannot omit the autonomous-delivery slice.

Focused synthetic tests use temporary SQLite roots and fake GitHub/Herdr
adapters. They cover default manual status, grant retry/CAS, expiry, revocation,
scope and sensitive refusal, configured usage limits, Shotcaller-only ownership,
repair creation, backup/restore, redacted and rollback export,
missing/wrong/closed/mismatched/unproven issue refusal, open-equivalent reuse,
authorized closed recurrence with prior linkage, distinct-scope creation,
concurrent-create serialization, valid issue-first launch, exact retry, and the
expanded direct/hidden routing boundary. Protected-gate tests prove sequential
multi-action propagation under one grant, an adjacent ungranted refusal before
operation, two-writer CAS, exact use/settlement persistence, and command-facade
delivery. Installed/live acceptance remains a separate gate.

## Remaining limits and cutover boundary

The issue selector/verifier currently supports GitHub through the installed
`gh api` owner surface; other forges fail closed. Semantic matching is a
deterministic normalization of the title and Objective/Scope words, so the
Shotcaller remains responsible for authoring one canonical scope and deciding
whether genuinely changed work is distinct. A successful repository
test or pull request does not prove merge, release, installation, deployment,
production verification, or cleanup. Those actions still require their exact
authority and receipts, and installed/live acceptance remains a separate gate.
