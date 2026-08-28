# Repository-local request lifecycle

This grouped slice implements issues #3, #4, #5, and #17 against explicit
isolated SQLite roots. The accepted issue-#21 design is canonical. Nothing in
this slice installs League, imports live state, edits hooks, prompts a real
agent, replaces the watcher, cuts over canonical authority, or performs
teardown; issue #23 owns those gates.

## Stable invariants

- A complete prompt payload is stored once by adapter, session, and source
  event. Ordered prompt items account for every meaningful part; only
  independently finishable parts create request rows.
- `direct`, `hidden`, and `champion` are execution modes. Request state is one
  of `open`, `routed`, `accepted`, `in_progress`, `awaiting_user`, `blocked`,
  `awaiting_requester`, `deferred`, `answered`, or `cancelled`.
- Repository initialization/writes, configuration writes, migrations,
  supervised tests, and long work require a visible Champion before mutation.
  User-selected model, effort, and explicit route are recorded unchanged.
- A task becoming `completed` or `ready_to_land` never answers its request.
  The owner records a result and a response reference explicitly.
- A routed owner result and ownership return commit in one transaction.
- Domain change, event, and recipient-specific outbox insertion commit in one
  transaction. Transport may repeat; one unique recipient receipt applies the
  database effect exactly once.
- Request claims, outbox dispatch leases, and watcher registration leases have
  independent holders, fences, expiry, and recovery.

## Command map

`league request` provides `intake`, `triage`, `claim`, `release`, `dispatch`,
`route`, `accept`, `awaiting-user`, `block`, `defer`, `cancel`, `result`,
`answer`, and `unresolved`. `unresolved --before-action` accepts `reply`,
`wait`, `handoff`, or `end` and returns a bounded page plus total counts.

`league assign` exposes the durable state machine: `prepare` commits the
reservation, `launching` commits external-launch intent, `activate` accepts
only the exact machine-readable Champion receipt, and `block` preserves either
`blocked` or `cleanup_pending`. `AssignmentService` performs those transitions
around one injected visible launch adapter without holding a database
transaction across launch.

`league task transition` binds one explicit task transition to its exact event
and coordinator outbox row, including the verified Champion runtime generation
in the bounded envelope. `league delivery claim-outbox`, `ack-outbox`,
`fail-outbox`, and `backlog` expose bounded database operations. The injected
`DeliveryService` always attempts the named source outbox first, then drains a
fair per-recipient backlog. An active watcher owns delivery; verified direct
fallback is eligible only when no active watcher registration exists.

`league hook stop` combines active Champion tasks, pending assignments,
unresolved requests, pending deliveries, and cleanup obligations. It yields to
fresh ordinary user messages, blocks at most once for one fresh wait
generation, reports stale terminal generations without reusing their output,
honors explicit allow-stop-once, and otherwise allows the turn to end. It does
not create a polling model loop.

`league help inventory` emits the versioned command, state, lease, and schema
inventory without opening a state root.

## Verification boundary

`make test-request-lifecycle` uses explicit temporary roots, deterministic
clocks and IDs, fake launch/delivery adapters, and synthetic records only. It
covers P100 with direct R1, routed R2, and local-Champion R3; the Heimerdinger
cross-wire regression; empty-repository delegation before first write;
multi-prompt restart/handoff; two-window claims; duplicate transport; stale
wait generations; reconnect and closed endpoints; ordinary-message priority;
cancellation; and unresolved reconciliation before reply, wait, handoff, and
end.
