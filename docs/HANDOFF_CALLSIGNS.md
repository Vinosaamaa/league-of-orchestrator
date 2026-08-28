# Guarded rollover and callsign queue

Issues [#8](https://github.com/Vinosaamaa/league-of-orchestrator/issues/8)
and [#13](https://github.com/Vinosaamaa/league-of-orchestrator/issues/13)
share one repository-local storage slice. It extends the accepted continuation
policy without installing League, importing live state, launching a real
runtime, changing global watcher state, or cutting over canonical authority.
Issue #23 retains those gates.

The slice is contiguous migration v6,
`guarded-rollover-and-shuffled-callsign-queue`, checksum
`e2a2a2ec80840923f351b96f40c16e45c4c3d156567fb585254c5be9c73efa0d`.
Canonical project/Squad
migration v5 remains unchanged. Migration v6 evolves the existing callsign
assignment and event tables rather than creating parallel assignment, event,
outbox, request, runtime, or cleanup state machines.

## Callsign queue

Each role has one persisted seed, shuffle version, queue version, and ordered
queue. Initial migration and later catalog additions use a deterministic
seeded hash order. The order is generated once; allocation never reshuffles it.

`league callsign` exposes:

- `reconcile` — compare-and-set one complete role catalog, retaining existing
  order and appending deterministic additions;
- `allocate` — reserve the first enabled callsign whose declared capabilities
  cover every requested capability;
- `activate` — accept only one exact verified runtime receipt for the reserved
  incarnation;
- `rollback` — return an unactivated reservation to its exact prior position;
- `release` — after exact runtime cleanup proof, append an activated assignment
  to the queue tail; and
- `status` — return the persisted seed/version, counts, and bounded ordered
  entries.

Allocation scans increasing persisted position. Disabled or capability-
incompatible available entries are counted and skipped without mutation.
`callsign_unavailable` is returned only when no compatible available entry
exists; its bounded reason object reports exact active, reserved, incompatible,
and per-reason counts. Recency is never a ban: if the tail entry is the sole
compatible available callsign, it is selected normally.

Queue and immutable assignment history are separate. Reservation creates a new
assignment and incarnation identity. Activation removes its queue position;
release appends a new position. Rollback preserves the original position.
Released and rolled-back history retains the original subject, scope, callsign,
accepted runtime, queue versions, receipt digests, and timestamps. Reuse creates
another row and never rewrites prior task, thread, callsign, event, or archive
identity.

All mutations use one `BEGIN IMMEDIATE` transaction and compare-and-set
versions. Concurrent allocators therefore cannot select the same entry. A
crash before commit rolls back the queue reservation and assignment together;
a retry of the same immutable assignment is idempotent.

## Stable Squad rollover

A Squad retains one stable `squad_id`, one current Shotcaller incarnation, one
owner version, and one owner fence. Active Champion membership is stable across
Shotcaller replacement. The Champion's task, thread, repository, branch,
worktree, runtime, capability, callsign, and historical parent bindings are not
rewritten.

`league rollover` exposes:

- `prepare` — validate explicit or stored automatic authority and the exact
  already-reserved successor identity, persist a bounded public-safe handoff plan, and
  freeze an immutable active-Champion binding snapshot;
- `bindings` — read that snapshot in stable bounded pages using opaque cursors;
- `acknowledge` — verify the complete, non-repeated page set plus exact handoff,
  snapshot, successor runtime, callsign acceptance, identity, and capability
  digests;
- `commit` — compare the original owner version/fence and perform one atomic
  owner switch, intake fence change, unresolved-intake redirect,
  `owner_changed` event, and one successor outbox row;
- `abort` — before the owner switch only, close/reconcile successor resources
  and either restore an unactivated reservation or tail-release an activated
  callsign; and
- `drain` — after the switch, require successor proof, zero predecessor intake
  or delivery obligations, exact predecessor runtime cleanup, archive/resource
  receipts, and callsign release before closing the predecessor.

The handoff stores a reference to the active-Champion snapshot, never the full
binding map. Every page repeats snapshot ID, version, total count, page bound,
expiry, and digest. Rows expose only stable task/Champion IDs, callsign, and an
exact binding digest; thread, endpoint, worktree, and other private locators are
hashed and remain in canonical redacted storage. Acknowledgement rejects an
expired snapshot, missing or repeated range, cursor/version change, count or
digest mismatch, changed owner fence, changed active-Champion binding set, or
changed successor runtime/capability evidence.

Before the switch, the predecessor remains the sole accepting Shotcaller and
the successor intake fence is closed. The commit transaction makes the
successor accepting and the predecessor draining while changing the one Squad
owner pointer. Request intake checks that fence, so the predecessor refuses new
intake immediately and a crash cannot leave two accepting owners. A retry after
an uncertain commit returns the one persisted owner event and outbox identity;
it never emits a duplicate. A crash after the switch rolls forward through
idempotent drain and never rolls ownership back.

## Public and authority boundaries

Handoff plans are capped at 65,536 bytes and reject secrets, credentials,
tokens, cookies, transcripts, private keys, local absolute paths, local
endpoints, and unbounded values. Command output contains public-safe stable IDs,
versions, counts, states, and digests. Inspection exports redact runtime,
workspace, plan-body, receipt, and other private fields.

Explicit authority and refusal states remain durable. Rollover authority grants
only the already authorized same-scope owner replacement. It grants no new
task, merge, deploy, install, teardown, or publication authority. Direct SQL is
unsupported; callers use the stable command envelope and storage facade.

Repository-local deterministic tests use temporary state roots and synthetic
adapters only. They do not establish real Herdr/tmux/Codex support, installation,
live migration, cutover, or smoke. Issue #23 must record those separately
authorized receipts.
