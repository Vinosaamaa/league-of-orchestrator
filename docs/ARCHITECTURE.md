# Architecture baseline

## Authority and vocabulary

- **Summoner**: the user and final authority for scope, merge, release,
  deployment, and direct steering.
- **Shotcaller**: a visible coordinator that owns routing and landing decisions.
- **Champion**: a visible issue-bound teammate that may implement, test, commit,
  publish, and prepare a pull request, but may not merge or deploy.
- **Roster**: durable status snapshots and append-only material updates for
  visible agents.
- **Lead**: an optional relay destination, never a superior authority or
  scheduler.

Disposable Shotcaller handoff and project aliases remain planned work. The
storage layer can represent exact projects, tasks, Squads, and owner transfers;
it does not implement request triage or runtime handoff policy.

## Current modules

The repository keeps the proven runtime and new storage boundary separate:

- `src/agent_watcher.py` owns strict record decoding, snapshot/event parity,
  watcher state, durable offsets, event deduplication, transition routing,
  runtime reconciliation, Herdr/tmux subprocess boundaries, atomic Herdr launch
  preflight and reservation rollback, hidden-worker allocation, optional Lead
  relay, semantic model routing, task-resource checks, and fail-closed teardown.
- `bin/agent-watcher` is a path-resolving launcher with no domain behavior.
- `schema/`, `examples/`, and `config/` define the public authoring surface.
- `tests/` exercises the imported behavior with temporary synthetic fixtures.
- `src/league/storage.py` composes the only domain-facing persistence interface
  from cohesive administrative, lifecycle, delivery, and transfer protocols;
  `storage_types.py` owns the stable refusal and typed import-plan contract.
- `src/league/sqlite_store.py` is the sole SQLite implementation and facade.
  `sqlite_core.py` owns the shared transaction mechanics; focused
  `sqlite_*_ops.py` modules own lifecycle, delivery, import, and export SQL,
  while the facade owns connection policy, migrations, integrity, and backup.
- `adapter_types.py`, `adapters.py`, and `runtime.py` own opaque namespaced
  identity, capability registration, and the generic harness/backend runtime
  sequence. They contain no Codex UUID, Herdr, or tmux branch in lifecycle core.
- `cleanup.py` owns task-class policy selection, typed-resource rules, ordered
  proof-first plans, and crash-resumable adapter execution;
  `sqlite_runtime_ops.py` owns the matching bindings, routing evidence,
  cleanup obligations, fences, actions, and immutable receipts.
- `routing.py` exposes the assignment-neutral semantic routing API. It neither
  creates assignments nor imports request lifecycle code.
- `src/league/importer.py` strictly decodes the explicit issue-#18 manifest and
  produces an in-memory plan; it never opens a database or writes legacy files.
- `src/league/cli.py` and `bin/league` expose stable domain commands and
  versioned JSON envelopes without a general query or SQL command.
- `schema/league-*.schema.json` defines command, import-report, and export
  output contracts.

This layout is intentionally a small modular monolith, not a set of shallow
database wrappers. Issue
[#7](https://github.com/Vinosaamaa/league-of-orchestrator/issues/7) supplies the
repository-local adapter interfaces and capability matrix; issue #23 retains
the isolated genuine-canary and cutover gates.

## Durable flow

1. A visible agent owns one `status.json` snapshot and one append-only
   `updates.jsonl` file.
2. Current-format reads reject malformed UTF-8, duplicate JSON keys, unsupported
   states, incomplete Champion identity, and snapshot/event mismatch.
3. Herdr launch preflight reconciles the Roster, callsign pool, and live
   endpoint. It reserves the lowercase routing name, starts the endpoint, and
   records the routing name and displayed backend kind only after post-start
   verification; a mismatch rolls back the reservation.
4. A transition appends one event and atomically replaces the matching snapshot
   while holding the record lock.
5. The watcher baselines existing events, tracks byte offsets and event digests,
   and emits only eligible scoped material transitions.
6. Runtime reconciliation observes adapters without mutating Roster records or
   inferring completion.
7. Teardown verifies the full schema-2 manifest before any archive, endpoint,
   worktree, branch, resource, record, or callsign mutation. Squash proof may
   tolerate later unrelated main commits only when every changed file matches;
   local-install proof requires exact released source/installed byte parity and
   a smoke receipt.

The repository-local SQLite path is separately testable:

1. `league storage migrate` checks the SQLite library loaded in-process,
   selects WAL only at 3.51.3+, enables and verifies foreign keys, sets a bounded
   busy timeout and `synchronous=FULL`, then applies contiguous checksummed
   migrations in a transaction. An existing schema requires a verified backup.
2. `league storage import` opens each manifest component beneath a caller-supplied
   explicit source root through one validated descriptor. Dry-run is the
   default; apply requires the exact typed-plan digest and an empty target.
3. Domain writes use short `BEGIN IMMEDIATE` transactions. Agent transition,
   exact callsign retries, delivery claim expiry/reclaim and acknowledgement,
   and task/Squad owner transfer enforce stable identity and expected-version
   preconditions.
4. Inspection export is bounded and redacted. JSONL rows are emitted from
   ordered cursors without a second complete in-memory copy. Rollback export is
   deterministic, written mode `0600` beneath the explicit state root, and
   reported by digest without exposing its path.
5. Schema v3 adds runtime bindings, typed task resources, cleanup
   obligations/operations/actions/receipts, and routing decisions/outcomes.
   Adapter effects stay outside transactions and are bridged by fences and
   immutable receipts.
   The parallel request-lifecycle branch also originated from schema v2 and
   currently proposes its own v3. Integration must assign the two migrations
   contiguous final numbers and recompute the renumbered candidate checksum
   before shared release history; an already-applied migration checksum is
   never rewritten or silently accepted.
6. Configuration, hooks, guides, launchers, immutable failure/teardown/archive
   evidence, installer backups, and other-product state remain files.

`src/agent_watcher.py` does not import `league`. The filesystem baseline is the
only live writer until issue #23 switches every consumer at one authorized
generation; there is no dual canonical write path.

`src/league/acceptance.py` is the issue-#23 repository-local harness. It owns
explicit-root sentinels, deterministic fake adapters, fixture migration parity,
staged release/rollback proof, a sandbox-only generation pointer and cutover
lock, fault-injected operation receipts, and exact fake canary cleanup. It has
no global path defaults and exposes no canonical cutover operation. The request,
assignment, watcher, Stop, and teardown extension assertions remain pending
until their owning issues merge.

## Portability boundary

The repository-local lifecycle core is adapter-neutral and accepts opaque
namespaced session/endpoint identity. Named Codex, Herdr, and tmux compatibility
contracts preserve the baseline capability surface, while Pi proves the shared
flow only through deterministic doubles. The imported watcher and installed
runtime remain Codex/Herdr/tmux-specific and unchanged. Agent/backend agnosticism
therefore remains unverified until issue #23 runs a fully isolated genuine
canary and an authorized cutover; this branch makes no stronger claim.

## Dependencies and side effects

Runtime and storage code use the Python standard library. Adapter commands are invoked only
by explicit delivery, reconciliation, resource, or teardown operations. The
local test command substitutes temporary records, repositories, state stores,
and fake adapter executables; SQLite tests additionally require explicit
temporary source and state roots. None touches live Roster state.
