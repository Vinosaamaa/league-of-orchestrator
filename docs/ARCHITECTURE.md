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

Disposable Shotcaller handoff and persistent callsign allocation are now
implemented repository-locally by contiguous migration v6. Migration v7 adds
bounded reporting and outbound privacy; migration v8 adds the routing-policy
slice: owner routing separate from execution,
pending Squad registration, role-aware hidden-scientist assignments, versioned
provider routing, and parent-request progress/outbox state. The storage layer
also implements the canonical prompt/request lifecycle, exact owner return,
adapter-neutral runtime binding, recoverable teardown, advisory project
catalog, and bounded project-grouped Roster. Installation, live migration,
cutover, autonomous planning, and an interactive Roster controller remain out
of scope.

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
  from cohesive administrative, lifecycle, request, assignment, outbox,
  watcher, delivery, transfer, project, Roster, evidence, and reporting protocols;
  `storage_types.py` owns the stable refusal and typed import-plan contract.
- `src/league/sqlite_store.py` is the sole SQLite implementation and facade.
  `sqlite_core.py` owns the shared transaction mechanics; focused
  `sqlite_*_ops.py` modules own lifecycle, request, assignment, delivery,
  watcher, catalog, Squad registration, request progress, Roster, reporting,
  import, and export SQL,
  while the facade owns connection policy, migrations, integrity, and backup.
- `src/league/importer.py` strictly decodes the explicit issue-#18 manifest and
  produces an in-memory plan; it never opens a database or writes legacy files.
- `src/league/request_services.py` owns injected visible-launch and delivery
  adapter boundaries; production adapter selection remains outside the store.
- `src/league/cursor_steering.py` owns the Cursor/Herdr direct-delivery effect:
  exact pane/session/status/process proof, idle submit versus working steer,
  and durable retry fencing. `sqlite_cursor_steering_*` owns only its intent
  and receipt state; it never reads terminal output or performs provider I/O.
- `src/league/pi_launch.py` owns explicit Pi runtime launch and exact-session
  restart through Herdr. Cursor/Codex selection is a descriptor field and Pi
  CLI argument, never a session-home choice. `pi_session_migration.py` verifies
  a shell-only restart boundary, first-record JSONL identity/lineage, bounded
  unified-inventory uniqueness, and byte-exact copy. The schema-22 operation
  modules persist launch, migration, and restart intent/receipts separately
  from provider I/O.
- `src/league/sqlite_continuation_ops.py` owns immutable provider-thread
  lineages, per-cleanup archives, exclusive continuation claims, issue-reopen
  fences, and runtime incarnations. `continuation.py` owns the bounded GitHub
  issue adapter, recoverable reopen service, and read-only Git binding check.
  These extend the existing cleanup and assignment lifecycles; they do not form
  a second scheduler or provider-session store.
- `src/league/adapters.py`, `adapter_types.py`, and `runtime.py` own opaque
  capability contracts and orchestration over injected harness and terminal
  adapters. `cleanup.py` and `routing.py` own proof-first teardown policy and
  assignment-neutral model/effort selection; `sqlite_runtime_ops.py` persists
  their bindings, decisions, resources, operations, and receipts.
- `src/league/agent_adapters/` is the explicit Codex, Pi, and Cursor CLI
  translation registry. Every adapter uses the same lifecycle vocabulary and
  advertises only the operations its native contract supports;
  `SharedLifecyclePolicy` owns the common accept/refuse seam consumed by prompt
  capture and exposed for issue #81 enforcement. Each adapter also owns its
  provider-specific visible-launch factory, so the CLI selects an adapter and
  does not contain a Codex/Pi/Cursor launch switch. Cursor configured inside Pi
  remains only a provider field, not a Cursor CLI runtime identity or session
  pool.
- `src/league/multiplexer_adapters/` independently registers Herdr and tmux.
  Multiplexer adapters advertise only callable native operations. The current
  Herdr adapter owns restored-agent discovery, exact endpoint/process
  observation, routing, Champion-tab/Shotcaller-pane placement, display
  metadata, delivery, and close transport; the tmux adapter advertises no
  unimplemented capability. `display_replay.py` composes the selected
  multiplexer adapter with the canonical runtime's agent adapter and contains no
  Herdr command strings. It reconstructs presentation from existing schema-22
  state rather than creating a second durable presentation store.
- `routing.py` is the single model/effort policy implementation. Ordinary
  visible launch defaults to Pi+Codex but requires the exact persisted routing
  decision for its request/task/assignment, role, provider, and capabilities.
  Explicit overrides are accepted only as an exact model+effort pair. The
  packaged schema-3 policy and schema-1/2 migration/rollback path preserve the
  strong-worker baseline and cannot silently fall back to Luna.
- `src/league/skill_contracts.py` owns strict custom-skill provenance,
  capability-profile resolution, bounded content hashing, and sanitized
  duplicate/install parity. It consumes the existing adapter matrix but does
  not load skills, inspect bodies semantically, or mutate a custom root.
- `src/league/privacy.py` owns the single exact-byte outbound validator;
  `remote_adapters.py` makes it mandatory immediately before every current or
  future League remote transport. `guidance.py` owns an explicit-root staging
  and rollback API for only `league/AGENTS.md`. It refuses the universal
  `AGENTS.md` target before mutation and cannot discover a home directory or
  switch a release pointer.
- `src/league/sqlite_report_ops.py` streams indexed canonical facts into one
  bounded stable JSON report. `reporting.py` derives Markdown and portable HTML
  from that object without another data source. The repository reporting skill
  invokes only the public command facade.
- `src/league/cli.py` and `bin/league` expose stable domain commands and
  versioned JSON envelopes without a general query or SQL command.
- `schema/league-*.schema.json` defines command, import-report, and export
  output contracts.

This layout is intentionally a small modular monolith, not a set of shallow
database wrappers. Issue
[#7](https://github.com/Vinosaamaa/league-of-orchestrator/issues/7) owns the
repository-local adapter interfaces; issue #23 still owns installed-driver and
real-runtime acceptance against those contracts.

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
   ordered compatibility-scanned callsign allocation, exact reservation
   rollback, tail release, delivery claim expiry/reclaim and acknowledgement,
   and task/Squad owner transfer enforce stable identity and expected-version
   preconditions.
4. Inspection export is bounded and redacted. JSONL rows are emitted from
   ordered cursors without a second complete in-memory copy. Rollback export is
   deterministic, written mode `0600` beneath the explicit state root, and
   reported by digest without exposing its path.
5. Prompt capture is idempotent by adapter/session/source event. Complete
   triage creates ordered prompt items and independently finishable requests;
   request claims, execution mode, request state, and task state stay separate.
6. Routes, owner results, task transitions, and their exact event/outbox rows
   commit atomically. Transport is at least once; unique recipient receipts
   apply the database effect once. Request, dispatch, and watcher leases remain
   distinct.
7. Assignment is recoverable across pending, launching, active, blocked, and
   cleanup-pending states. Active requires one exact role-specific verified
   receipt. Champions require issue/repository/branch/worktree identity; hidden
   scientists require exact owner/request/subtask/model/effort/reason/budgets,
   remain outside the visible Roster, and deliver terminal-only.
8. The role-aware Stop decision combines unresolved requests, active tasks,
   assignments, deliveries, and cleanup, while blocking at most once per fresh
   wait generation and yielding to ordinary user messages.
9. Schema v4 evolves v3's one-per-task cleanup obligation rather than adding a
   competing lifecycle record. Request/assignment paths may create the initial
   obligation; verified teardown atomically advances it with ownership,
   task-class, disposition, one cleanup operation, and ordered actions.
10. Runtime bindings persist opaque namespaced session/endpoint identity,
   endpoint generation, and declared capabilities. Cleanup validates every
   action before its first external effect and then records immutable,
   fence-bound receipts for crash-safe resumption.
11. Schema v16 replaces the all-history provider-session uniqueness index with
   live-only uniqueness, then adds one permanent lineage for each opaque
   provider thread, one immutable archive per completed incarnation, and one
   exclusive active continuation claim per archive. Cleanup planning writes the
   archive before effects; finalization exposes it only after the exact owning
   issue is observed closed and the final teardown receipt exists.
12. An explicit continuation verifies the new repository/issue/branch/worktree
   binding, completed acceptance, healthy context, exact-resume declarations,
   instruction reconciliation, closed linked runtime history, and absence of a
   live worktree owner. A fenced external action reopens only the archived issue.
   Ordinary assignment then allocates a current callsign, launches a fresh
   endpoint/runtime against the exact provider thread, and appends a lineage
   incarnation. The old endpoint and worktree are never restored.
13. Orchestration resolves explicit route, continuation, then one unique strong
   eligible Squad before local direct/Champion execution. Canonical ownership
   moves only after acknowledgement. Parent progress has immediate and
   15-minute changed-only aggregate classes plus one five-minute-grace overdue
   escalation; no heartbeat is synthesized.
14. Model routing records policy/provider versions, structured semantic
   signals, explicit and expiring operator overrides, capability fallback,
   evidence-gated downgrade, and at most one safe-boundary escalation child.
15. Configuration, hooks, guides, launchers, immutable failure/teardown/archive
   evidence, installer backups, and other-product state remain files. The
   universal agent guide is terminal-environment-toolkit-owned; League owns
   only its `league/AGENTS.md` orchestration supplement.
16. Skill roots remain external file-owned inputs. The repository stores only
    public labels, identity/provenance/version/capability declarations, content
    hashes, and sanitized parity receipts; it stores no root path or skill body.
17. Project aliases, codes, exact roots/repositories, and ordered suggested
    Squads are versioned catalog facts. Suggestion changes never mutate tasks,
    assignments, requests, events, or instructions; explicit routing stays
    separate and authoritative.
18. `league.roster-snapshot.v1` groups current work from one bounded read
    transaction. It is non-canonical, has explicit limits/truncation, and links
    every item to exact canonical keys without persisting a report cache.
19. `league.report.v1` streams timestamp-indexed canonical facts and refuses
    both fact and completion scans above their explicit bounds. Markdown and
    portable HTML are pure renderers over that versioned JSON object.
20. Exact project roots and full evidence remain classified `local_only`.
    Every remote adapter validates the final rendered bytes with the same
    fail-closed policy immediately before invoking its injected transport.

`src/agent_watcher.py` does not import `league`. The filesystem baseline is the
only live writer until issue #23 switches every consumer at one authorized
generation; there is no dual canonical write path.

`src/league/acceptance.py` is the issue-#23 repository-local harness. It owns
explicit-root sentinels, deterministic fake adapters, fixture migration parity,
staged release/rollback proof, a sandbox-only generation pointer and cutover
lock, fault-injected operation receipts, and exact fake canary cleanup. It has
no global path defaults and exposes no canonical cutover operation. The
original foundation receipt keeps its request, assignment, watcher, Stop, and
teardown extension points pending for compatibility.

`src/league/precutover.py` composes those merged repository-local slices in a
separate strict receipt. It copies only explicitly bound legacy files into an
isolated shadow, checks exact imported legacy fields and counts, stages both
League launchers inactive, rehearses exact backup/restore, measures an
event-driven test listener, and emits an unapplied mutation/rollback manifest.
Its runtime canaries are deterministic contract doubles, never real-runtime
proof. Its terminal operation is `awaiting_authority`; it has no apply path.

`src/league/real_canary.py` is the narrow exception to double-only runtime
coverage. Under one explicit temporary root it composes a real Herdr/Codex
Champion, real SQLite lifecycle state, and the adapter-backed cleanup executor.
It deliberately crashes after the archive external effect and resumes through a
new CLI process, proving durable operation fencing and observation-based
idempotency before exact endpoint, worktree, branch, and callsign release. This
does not generalize into a production executor or prove any other harness or
backend. The repository-artifact and exact-evidence contract for the final
disposable gate is versioned in [`docs/ACCEPTANCE.md`](ACCEPTANCE.md); the gate
performs no hosted mutation.

## Portability boundary

The repository-local runtime core uses opaque namespaced session identity and
declared capabilities. Codex+Herdr and Codex+tmux remain named contracts, and a
deterministic Pi adapter proves the shared lifecycle without being labeled a
real-runtime canary. The imported live watcher is unchanged: its Champion UUID,
Codex hook, Herdr/tmux branch, and Herdr launch assumptions remain until issue
#23 verifies and authorizes a cutover. Provider model names remain configuration
data. Repository-local portability is implemented without claiming installed
portability.

The skill capability matrix selects one pair from the same registered adapter
matrix, then evaluates orthogonal harness/tool/platform/browser/forge/
delegation/multiplexer declarations. It contains no provider or model field.
Shared research delegates only with `background-visible-agents`; otherwise it
runs inline. Specialist capability gaps refuse rather than becoming false
portability claims.

## Dependencies and side effects

Runtime and storage code use the Python standard library. Adapter commands are invoked only
by explicit delivery, reconciliation, resource, or teardown operations. The
local test command substitutes temporary records, repositories, state stores,
and fake adapter executables; SQLite tests additionally require explicit
temporary source and state roots. None touches live Roster state.
