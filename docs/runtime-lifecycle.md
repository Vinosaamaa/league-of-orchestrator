# Repository-local runtime lifecycle

This slice implements issues
[#7](https://github.com/Vinosaamaa/league-of-orchestrator/issues/7),
[#11](https://github.com/Vinosaamaa/league-of-orchestrator/issues/11), and
[#14](https://github.com/Vinosaamaa/league-of-orchestrator/issues/14) behind
the existing `Storage` facade. It does not install, migrate live records, or
claim a real harness/backend canary. Issue
[#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23) owns that
final gate.

## Adapter boundary

Core session and endpoint identities are `namespace:opaque-value` strings.
Core validates the namespace envelope and exact equality only. A harness
adapter owns create, identify, title, prompt, status, hook, interrupt, resume,
and exit semantics. A backend adapter owns allocation, input transport,
inspection, and close. Missing declarations return `unsupported_capability`;
unknown adapters return `adapter_unknown`.

The compatibility matrix is intentionally honest:

| Harness/backend | Create | Identify/prompt/status/hook/interrupt/exit | Resume | Allocate | Input/inspect/close | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| Codex + Herdr | supported | supported | unsupported | supported | supported | inherited contract |
| Codex + tmux | supported at harness boundary | supported | unsupported | unsupported | supported | inherited contract |
| Pi + Herdr | unverified | unverified | unverified | unverified | unverified | real canary pending |
| Pi + tmux | unverified | unverified | unverified | unsupported | unverified | real canary pending |
| Pi + deterministic backend double | exercised | exercised | exercised | exercised | exercised | isolated test only |

`league runtime matrix` exposes the machine-readable matrix. The deterministic
Pi test covers create → identify → route/prompt → durable transition → wake →
interrupt → resume → exact guarded exit. It is never reported as real-runtime
proof. Separate deterministic contract tests cover Codex+Herdr creation and
Codex+tmux attach/input/inspect/close behavior without launching either backend.

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

A completed, rejected, cancelled, or failed task creates a separate
`cleanup_pending` obligation. The planner validates every policy field,
resource, pending-decision gate, and legacy identity pointer before it writes a
plan. It then claims one cleanup revision whose actions are fixed in this
order: archive validated identity/policy/evidence; task-owned resource actions;
harness exit; backend close; applicable exact worktree and local branch; and
callsign release last.

External effects run outside SQLite. Every pending adapter and identity is
preflighted read-only before the first effect, each action is inspected again
before use, verified afterwards, and receives one immutable receipt. A crash after the
effect but before the receipt is recovered by inspection: an already-observed
intended state records `already_applied` instead of repeating the effect. A
fence prevents a stale executor from writing receipts. Cleanup becomes
`cleanup_completed` only after every action receipt and the final teardown
receipt exist.

## Model and effort routing

`ModelRouter` is the stable assignment-neutral API. It accepts a semantic role
profile and records the chosen tier, concrete model, effort, reason, and which
fields were explicit. Provider-specific model names remain in configuration.

The default bounded/checkable route stays on `WORKER_STRONG` until a configured
representative evaluation explicitly approves `WORKER_FAST`. Explicit user
model or effort values are preserved exactly. Only the enumerated concrete
failure classes permit one safe-boundary escalation; a second escalation, or a
route already at the strongest worker tier, records `blocked`. Outcomes record
success, corrections, latency, and cost by routing decision and role.

The issues #3/#4/#5/#17 assignment branch consumes this API. This slice does
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
