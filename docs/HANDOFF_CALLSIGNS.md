# Guarded rollover and callsign queue

Issues [#8](https://github.com/Vinosaamaa/league-of-orchestrator/issues/8)
and [#13](https://github.com/Vinosaamaa/league-of-orchestrator/issues/13)
share one repository-local storage slice. It extends the accepted continuation
policy without installing League, importing live state, launching a real
runtime, changing global watcher state, or cutting over canonical authority.
Issue #23 retains those gates.

The slice is contiguous migration v6,
`guarded-rollover-and-shuffled-callsign-queue`, checksum
`879ef4addfe6725e31c31a5aa1db9078d7c066a26610eaa2753f749c6e53ab75`.
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

## Startup context and bounded runner (#8 / PR #54)

`league agent startup-context --agent-id <id> --runtime-instance-id <id> --at <time>`
reads one exact accepted Champion or successor Shotcaller runtime. It refuses
ambiguous runtimes, unreconciled Champion owners, stale successor owner/intake
fences, and missing native adapter capabilities. Its 64-KiB, 128-obligation
output contains identities, task/request state, owning and original requesting
Shotcallers, Squad/routing context, permitted actions and pending obligations.
It excludes prompt bodies, summaries, raw provider output, thread/endpoint
locators and local paths. Successor expiry follows the current snapshot revision,
not the original plan's expiry. Historical requesters remain historical.

`league rollover run --manifest <file> --at <time>` uses
`league-shotcaller-rollover-run.schema.json` and the existing staged storage
APIs. Initial invocation prepares and returns one bounded binding page. The
successor reads all pages through `rollover bindings`, then explicitly supplies
the existing pages receipt with `run --pages <file>` or uses `rollover acknowledge`.
Reading a page never acknowledges it. A later `run` consumes the durable
acknowledgement and commits the existing atomic owner/intake/event/outbox switch.
Exact retries inspect the persisted operation, including after original plan
expiry; they do not re-prepare, launch a runtime, or deliver another owner event.

The switched result names the existing descendant/intake reconciliation and
guarded cleanup commands. Those commands keep their exact per-row identity,
version, authority and cleanup-plan gates. `run --abort-receipt <file>` and
`run --drain-receipt <file>` consume the existing cleanup receipt schemas through
the existing stages; they never close a process, mark a runtime closed, or
manufacture a cleanup receipt. Drain still refuses remaining obligations or a
live predecessor. `cleanup execute` already derives the final drain receipt
for its exact switched predecessor, so a subsequent plain `run` observes completion.
No bulk descendant/obligation rewrite or second cleanup mechanism is added.

The runner uses the registered native runtime kinds `codex-thread`,
`cursor-thread` (the Cursor CLI adapter), and `pi-thread`, plus registered
multiplexer capabilities. It adds no executable-command adapter registry.
`runtime matrix` reports source support, not live acceptance. The focused suite
uses temporary canonical records and synthetic runtime observations for both
Codex-to-Cursor and Cursor-to-Codex directions. Installed, native bidirectional
end-to-end acceptance remains a separately authorized release gate. `run`
requires explicit authority; automatic grants retain the protected staged path.

### Switched recovery when the active set has changed

`snapshot_refresh_set_changed` remains a hard refusal: never shrink or replace
the frozen set to make refresh pass. For an unchanged surviving frozen binding,
the supported `rollover reconcile-descendant` path does not require snapshot
refresh or unexpired snapshot paging. It requires the retained original snapshot
digest and row receipt, current exact versions, fresh registered-adapter runtime
verification, and the exact pending descendant outbox IDs. It rechecks the row,
membership, owner fence and runtime before its atomic reconciliation; retry
reuses the same reconciliation identity. Other descendants remain untouched.

The focused expired/changed-set regression proves refresh refusal and exact
survivor reconciliation/retry without changing the snapshot or Champion identity.
This is conditional recovery, not proof that any live survivor matches. If a
survivor's binding changed, preserve the precise refusal. If its original page
receipt is unavailable, do not backdate a read, use direct SQL, or reconstruct
its digest: a bounded read-only historical-receipt accessor would be the smallest
missing surface, not a weaker snapshot refresh or another state machine.

### Exact imported one-row compatibility

For an unchanged frozen imported binding with both route and displayed provider
absent, `reconcile-descendant` reuses snapshot refresh's imported null-route
eligibility proof. The native adapter must find exactly one matching live
pane/thread/worktree, with the existing lowercase-callsign route and Codex
provider. Unnamed unrelated endpoints are not route matches. This operation
does not rename the live endpoint. Only after verification does the existing
owner transaction adopt the paired routing fields, alongside the task,
assignment, callsign, and exact pending deliveries. The original frozen row
and imported history remain immutable. Modern, partially populated, foreign,
ambiguous, and changed frozen bindings still refuse before adoption.

An existing legacy hook runtime is accepted only when its ID and `hook:`
generation exactly reproduce the hook producer's hash of adapter kind, thread,
backend, and endpoint. Its actor, provider, capabilities, verification, and
active/idle status must still agree. Fresh native inspection verifies the
terminal, thread, route, worktree, and readiness; the existing runtime receipt
binds that terminal and observation sequence. The historical hook generation
is not replaced by the different terminal/thread-based `herdr:` fingerprint.
No runtime is duplicated, and existing runtime rows remain unchanged.

The existing read-only `rollover_descendant_target` preflight now returns
`pending_outbox_ids`: the complete sorted set of pending predecessor deliveries
whose source event belongs to this exact Champion or task. This is necessary
because the recipient-wide backlog can hide eligible rows behind unrelated
work. It exposes IDs only, does not filter out future-due pending rows, and
refuses claimed deliveries or sets above 1,000 IDs rather than truncating.
Pass that exact set through the existing repeated `--pending-outbox-id` option;
commit repeats the same preflight under its transaction, including the frozen
private-binding digest check. Changed sets, stale versions, and injected
failures leave adoption and reconciliation rolled back together.

These exceptions do not accept a runtime materialized after the frozen row or
any other changed binding, do not refresh the broad snapshot, and do not add a
schema, service, inspection command, or live recovery authority.

### Retired original with an exact terminal handoff

Expired switched-snapshot refresh may retain an original that is no longer live
only when the existing lifecycle producers prove its completed handoff:

- Runtime replacement: the frozen binding matches the immutable replacement
  intent; retirement and handoff receipts match the closed original and the
  current accepted successor task/runtime/callsign binding.
- Exact-thread continuation: completed cleanup and issue-close receipts, released
  original callsign, archived opaque thread identity, issue reopen, incarnation
  linkage, and accepted resumed assignment all agree. The original remains
  retired with its runtime closed; resume capabilities and current successor
  identity must still be exact.

Refresh keeps the original task, callsign, binding digest, and membership. A
successor justified by that proof is not inserted into the frozen set. The new
snapshot version has new row receipts, while every prior snapshot remains
unchanged. Its progress receipt and bounded `bindings` page expose
`retired_handoff_satisfied`, bound to the existing terminal handoff receipt.
This is evidence, not another lifecycle state machine or synthetic acceptance.

Do not run `reconcile-descendant` on that retired original: the live-operation
refusal remains intact. Refresh does not resurrect it, rebind a callsign, adopt
the successor into this snapshot, or settle pending delivery/cleanup/intake
obligations. Surviving live originals still use exact one-row reconciliation.
Unproven disappearance, foreign additions, modified frozen hashes, incomplete
handoffs, ambiguous lineage, and stale identity still refuse. Proof is reread
under the refresh transaction and after final live observation; any concurrent
change rolls back. Exact refresh retries reuse the committed receipt.

`tests/test_rollover_retired_original.py` uses the actual replacement,
cleanup/issue-reopen, and assignment services with temporary records and fake
external adapters. It covers two retired originals, mixed live membership,
tampering, stale versions, CAS, rollback, and retry. These are synthetic source
receipts only; installation and live recovery require separate owner authority.
