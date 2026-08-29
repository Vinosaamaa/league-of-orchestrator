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
- `direct` (Shotcaller-direct), `hidden` (recorded scientist), and `champion`
  (visible local Champion) are execution modes; `squad_route` is a separate
  pending owner-transfer outcome. Request state is one
  of `open`, `routed`, `accepted`, `in_progress`, `awaiting_user`, `blocked`,
  `awaiting_requester`, `deferred`, `answered`, or `cancelled`.
- Repository initialization/writes, configuration writes, migrations,
  supervised tests, and long work require a visible Champion before mutation.
  User-selected model, effort, and explicit route are recorded unchanged.
- Shotcaller-owned intake is fenced by the stable Squad owner. A draining or
  superseded Shotcaller incarnation refuses new prompts; the atomic rollover
  switch makes only the successor accepting while unresolved durable requests
  retain their identity and are redirected transactionally.
- A task becoming `completed` or `ready_to_land` never answers its request.
  The owner records a result and a response reference explicitly.
- A routed owner result and ownership return commit in one transaction.
- Domain change, event, and recipient-specific outbox insertion commit in one
  transaction. Transport may repeat; one unique recipient receipt applies the
  database effect exactly once.
- Request claims, outbox dispatch leases, and watcher registration leases have
  independent holders, fences, expiry, and recovery.
- Inspection and rollback exports cap prompt payload bodies at 16 MiB in
  addition to the record-count bound, refusing before payload rows are
  materialized when either budget is exceeded.

## Command map

`league request` provides `intake`, `triage`, `claim`, `release`, `dispatch`,
`decide-route`, `route`, `accept`, `progress`, `reconcile-progress`,
`awaiting-user`, `block`, `defer`, `cancel`, `result`, `answer`, and
`unresolved`. `unresolved --before-action` accepts `reply`,
`wait`, `handoff`, or `end` and returns a bounded page plus total counts.

`league squad register`, `accept`, and `status` expose the pending exact-runtime
registration contract. Registration cannot activate routing; acceptance
atomically creates stable Squad/intake/event/requester-outbox state, and active
replacement remains a guarded rollover operation.

`league assign` exposes the shared durable state machine: `prepare` commits the
role-specific reservation, `launching` commits external-launch intent,
`activate` accepts only the exact machine-readable Champion or hidden-scientist
receipt, `reconcile-runtime` fences a stale active runtime, and `block`
preserves either `blocked` or `cleanup_pending`. `finish-hidden` is the only
cleanup-gated hidden result delivery; hidden scientists emit no routine
progress. `AssignmentService` performs visible Champion transitions
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
