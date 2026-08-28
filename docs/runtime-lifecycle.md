# Repository-local runtime lifecycle

This slice implements issues
[#7](https://github.com/Vinosaamaa/league-of-orchestrator/issues/7),
[#11](https://github.com/Vinosaamaa/league-of-orchestrator/issues/11), and
[#14](https://github.com/Vinosaamaa/league-of-orchestrator/issues/14) behind
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
exact cleanup action. `shared_lease` and `persistent_retain` resources remain
representable in the registry, with only `release_lease` and `retain`
respectively, but refuse task teardown because they are not exclusively owned.

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
fence prevents a stale executor from writing receipts. Cleanup becomes
`cleanup_completed` only after every action receipt and the final teardown
receipt exist.

Runtime exit uses the same recoverable shape: it atomically claims a binding
version and monotonically increasing exit fence before sending exit or close,
then verifies and finalizes. An expired lease may be reclaimed after a crash;
inspection reconciles an already-exited session or missing endpoint without
repeating the external action.

## Model and effort routing

`ModelRouter` is the stable assignment-neutral API. It accepts a semantic role
profile and records the chosen tier, concrete model, effort, reason, and which
fields were explicit. Provider-specific model names remain in configuration.

The default bounded/checkable route stays on `WORKER_STRONG` until a configured
representative evaluation explicitly approves `WORKER_FAST`. Explicit user
model or effort values are preserved exactly. Only the enumerated concrete
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
identity, already-closed/missing exact resources, shared/persistent refusal,
policy classes, rejected/cancelled/failed work, and public-path exclusions.
