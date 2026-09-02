# Repository-local runtime lifecycle

This slice implements issues
[#7](https://github.com/Vinosaamaa/league-of-orchestrator/issues/7),
[#11](https://github.com/Vinosaamaa/league-of-orchestrator/issues/11), and
[#14](https://github.com/Vinosaamaa/league-of-orchestrator/issues/14), with
issue [#83](https://github.com/Vinosaamaa/league-of-orchestrator/issues/83)
implementing the accepted issue-#15 continuation policy behind
the existing `Storage` facade. It does not install, migrate live records, or
claim a real harness/backend canary. Issue
[#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23) owns that
final gate.

The canonical request-lifecycle migrations remain versions 1, 2, and 3;
runtime lifecycle is contiguous migration v4, project/Roster catalog is v5,
and guarded rollover plus callsign allocation is v6. The runtime migration is
named
`adapter-runtime-cleanup-and-routing`, checksum
`01892d93311ce0b5486077b00e6d3adea60fd3c91006663358317260ad21cd2d`.
It evolves v3's existing one-per-task `cleanup_obligations` row with optional
verified-teardown metadata; it does not create a parallel request, assignment,
outbox, watcher, or Stop lifecycle.

## Adapter boundary

Core session and endpoint identities are `namespace:opaque-value` strings;
the backend also supplies an opaque endpoint generation that is persisted and
revalidated before every input or inspection. Core validates the namespace
envelope and exact equality only. A harness
adapter owns create, identify, title, prompt, status, hook, interrupt, resume,
and exit semantics. A backend adapter owns allocation, input transport,
inspection, and close. Missing declarations return `unsupported_capability`;
unknown adapters return `adapter_unknown`.

`league runtime matrix` is the authoritative, generated compatibility matrix.
It distinguishes undeclared operations (`unsupported`) from declared contracts
whose repository-local driver is absent (`driver_unavailable`). Herdr and tmux
are named contract-only backends in this branch; tmux allocation is additionally
undeclared. The deterministic
Pi test covers create → identify → route/prompt → durable transition → wake →
interrupt → resume → exact guarded exit. It is never reported as real-runtime
proof. Separate deterministic contract tests cover Codex+Herdr creation and
Codex+tmux attach/input/inspect/close behavior without launching either backend.

Issue #10's `league skill matrix` reuses this generated adapter matrix as its
runtime-pair evidence. Skill requirements remain a separate provider/model-
neutral capability vector; they do not add adapter kinds or driver selection.
See [skill capabilities](skill-capabilities.md).

## Typed resources and cleanup

Each task resource declares owner, type, lifetime, expected identity, cleanup
action, adapter, and applicability. `task_owned` resources may perform their
exact cleanup action. A `shared_lease` permits only the `lease` adapter to
release the exact task/owner/endpoint/generation row; it never stops or restarts
the shared resource. A `persistent_retain` registration is validated but has no
cleanup action and remains active after the task completes.

The planner selects one versioned policy:

| Task class | Required evidence beyond exact identity and terminal/idle endpoint |
| --- | --- |
| analysis/no repository | none |
| local Git | exact registered worktree/branch, clean state, no unpublished work |
| PR/CI | local Git plus exact published head, green CI, and integration proof |
| deployed service | PR/CI plus exact deployed revision and smoke |
| rejected/cancelled | the applicable local evidence plus explicit decision; no invented PR/deploy proof |
| failed | the applicable local evidence plus preserved-failure proof |

A completed, rejected, cancelled, or failed task advances the task's separate
cleanup obligation to `cleanup_pending`. If the request lifecycle already
created that obligation, v4 preserves its identity and advances its version.
The planner validates every policy field,
resource, pending-decision gate, and legacy identity pointer before it writes a
plan. It then claims one cleanup revision whose actions are fixed in this
order: archive validated identity/policy/evidence; task-owned resource actions;
harness exit; backend close; applicable exact worktree and local branch; and
callsign release last.

New resource registrations, the cleanup obligation, its claimed operation, and
all actions commit in one SQLite transaction. A planning conflict or injected
failure rolls the entire set back, so it cannot leave active orphan resources.

External effects run outside SQLite. Every pending adapter and identity is
preflighted read-only before the first effect, each action is inspected again
before use, verified afterwards, and receives one immutable receipt. A crash after the
effect but before the receipt is recovered by inspection: an already-observed
intended state records `already_applied` instead of repeating the effect. A
fence prevents a stale executor from writing receipts. A resumed attempt also
reverifies every previously completed action. A non-retryable stale identity,
unsupported policy, or failed verification atomically records `blocked`, fixes
the action when applicable, advances the obligation revision, and writes one
immutable final refusal receipt. Cleanup becomes
`cleanup_completed` only after every action receipt and the final teardown
receipt exist.

`league cleanup execute` is the production command boundary. It accepts only an
existing operation ID, expected fence, executor identity, lease expiry, and
timestamp. Before claiming the fence, it reconstructs the exact obligation and
revision, task/disposition, owner, registered resources, runtime rows, adapter
policy, and proof from canonical SQLite through the `Storage` interface. It
does not accept a manifest or adapter-config path. The current production
driver supports verified Codex+Herdr, exact registered Git/callsign/process
actions, and exact shared-lease release; another harness/backend policy refuses
clearly before effects.

Automatic Champion cleanup and post-switch predecessor Shotcaller drain share
this executor. Shotcaller cleanup is accepted only when the archived plan still
matches the exact switched rollover predecessor and version. Completion derives
the rollover drain receipt from immutable cleanup action receipts; retries reuse
the same cleanup operation and rollover receipt.

## Already-stopped total retirement

Issue [#127](https://github.com/Vinosaamaa/league-of-orchestrator/issues/127)
adds a narrow retirement path for an exact Champion whose provider process and
multiplexer pane are already absent while an imported runtime remains active.
This is not repository cleanup. `runtime retire-stopped-agent` never exits,
closes, resumes, launches, prompts, steers, deletes, or rewrites external state.

Core resolves the runtime kind and multiplexer kind only through their adapter
registries. The agent adapter validates its provider and process vocabulary;
the multiplexer adapter proves that the exact endpoint, route, native session,
pane, and registered provider process names have no live or ambiguous inventory
match. An unsupported pair refuses without a fallback. Herdr uses structured
`agent list` and exact-pane `process-info`; only an explicit structured
`pane_not_found` failure envelope on stderr with exit status 1 establishes pane
absence. Successful process inspection is accepted only as bounded structured
JSON on stdout. Both streams require finite JSON and exactly one top-level
member: `result` on success or `error` on failure. Mixed result/error envelopes
and non-finite constants fail closed. tmux remains explicitly unsupported until
its adapter can provide equivalent owner-source evidence.

One bounded `BEGIN IMMEDIATE` transaction rechecks the immutable runtime/session/
endpoint/generation, expected agent and callsign versions, unique active runtime
and callsign ownership, and transferred-task boundary before performing that
external read-only proof. It then marks the runtime
closed and unverified, releases the exact callsign at the queue tail,
terminalizes and retires the Champion, removes only that Champion's Squad
membership, records immutable proof and receipt digests, and emits a retirement
event. A fault at either internal boundary rolls everything back. Reopening the
store and retrying the same operation returns the stored receipt without
consulting or changing the multiplexer again. Supported League launch and resume
paths also require canonical write ownership, so they cannot interleave between
proof and settlement; the concurrency acceptance holds the proof open and
observes an exact retryable writer refusal. Same-user raw process injection is
outside League's process-security boundary, while any pane or registered
provider process present when proof runs is still refused. Repository coordinates are
retained only as immutable agent history; no filesystem adapter participates.

Retirement identity fields and serialized proof bytes are bounded before
persistence. Supported provider aliases normalize to the adapter's canonical
provider before comparison, digesting, and receipt storage. Composite indexes
bound the unique active-callsign and active-assignment checks.

Focused acceptance uses synthetic SQLite state, temporary retained bytes, and
fake adapter inventories. It covers direct Codex, direct Cursor CLI, Pi with
Cursor, Pi with Codex, an injected non-Herdr multiplexer, imported callsign/runtime
binding, live/ambiguous/mismatched/orphan-process refusal, unsupported pairs,
proof-versus-resume concurrency, transaction rollback, and exact retry after
storage restart.

## Issue-coupled cleanup and exact-thread continuation

Migration v16 is named
`issue-coupled-cleanup-and-exact-thread-continuation`. It retains historical
runtime rows while limiting `(harness_kind, session_ref)` uniqueness to live
`active` or `idle` runtimes. This permits a later runtime incarnation to carry
the same opaque provider thread identity only after every recorded incarnation
in that lineage is closed. Unlinked reuse, multiple live rows, or a thread that
appears outside its lineage refuses.

Issue-coupled cleanup is opt-in and restricted to a completed Champion whose
`pr_ci` or `deployed_service` proof is complete. The manifest adds an exact
provider-thread archive and one final `issue_close` action. The planner checks
that the archive matches the canonical task owner, runtime, callsign,
repository, issue, branch, worktree, completed acceptance, cleanup proof,
instruction/policy digests, context health, and declared durable/exact/safe
resume capabilities. It inserts the lineage/archive/incarnation with the cleanup
plan in one transaction. External action order remains proof archive, resources,
session exit, endpoint close, Git cleanup, callsign release, then issue close.
Any earlier retryable failure leaves the issue open. An already-closed exact
issue is reconciled without repeating the external action. The provider-thread
archive becomes `available` only after the issue-close action receipt and final
teardown receipt commit together.

Continuation is never automatic. `league continuation prepare` takes one
archive ID plus the intended successor assignment/task/agent and exact new Git
binding. The store exclusively claims it only when the archive is available,
all linked runtimes are closed, context is healthy, exact resume and safe
worktree rebinding are declared, no current agent owns the new worktree, and
governing instruction drift has an explicit reconciliation digest. A concrete
benefit must be one of same-task recovery, same-artifact revision, or an
unresolved decision chain; otherwise the caller must choose a fresh thread.

`league continuation reopen` uses version, fence, executor, and lease
preconditions around the exact owning-issue reopen. A crash before recording the
receipt is recovered by observing that same issue already open. Only then may
the predeclared assignment run. The Codex/Herdr driver invokes `codex resume`
with the archived thread UUID, binds the new worktree, skips the fresh-thread
identity handshake, and refuses unless Herdr reports that exact thread on the
new endpoint. Activation uses the normal callsign queue; it never reserves the
historical callsign specially. It writes a new runtime incarnation and retains
the prior task, cleanup, close, and reopen receipts permanently.

The current operational exact-resume driver is Codex on Herdr. A provider with
no exact durable resume or no safe worktree-rebind declaration fails closed;
the core continues to store provider identifiers as opaque namespaced strings.

Runtime exit uses the same recoverable shape: it atomically claims a binding
version and monotonically increasing exit fence before sending exit or close,
then verifies and finalizes. An expired lease may be reclaimed after a crash;
inspection reconciles an already-exited session or missing endpoint without
repeating the external action.

## Model and effort routing

`ModelRouter` is the stable assignment-neutral API. It accepts semantic
task/risk/verification signals and records policy/provider versions, chosen
tier, provider, model, effort, bounded reason, explicit fields, active operator
override, and capability fallback. Provider-specific model names remain in
versioned configuration.

The default bounded/checkable route stays on `WORKER_STRONG` until a configured
representative evaluation explicitly approves `WORKER_FAST`. Explicit user
provider, model, or effort values are preserved exactly, before an expiring
operator override and ordinary policy. Only the enumerated concrete
failure classes permit one safe-boundary escalation; a second escalation, or a
route already at the strongest worker tier, records `blocked`. Outcomes record
success, corrections, latency, and cost by routing decision and role. Storage
atomically permits only one child for each prior decision. Outcome retries are
idempotent by `outcome_id` only when every recorded field matches.

The merged #3/#4/#5/#17 assignment slice can consume this API. This slice does
not create prompt inbox, request claim, assignment, outbox, or Stop-hook state.

## Verification

All destructive behavior is exercised only through explicit temporary SQLite
roots and deterministic adapters:

```sh
make test-runtime-lifecycle
make test-affected
make test-all
```

The fault suite crashes after every planned external action, resumes without a
duplicate effect, retries completed teardown idempotently, and covers stale
identity, already-closed/missing exact resources, exact shared-lease release,
persistent retention, policy classes, rejected/cancelled/failed work, and
public-path exclusions. The production slice adds one synthetic cleanup E2E;
it does not perform a live teardown.
