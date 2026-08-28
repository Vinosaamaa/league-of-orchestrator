# JSON/JSONL producer-consumer dependency audit

Status: complete evidence for issues [#18](https://github.com/Vinosaamaa/league-of-orchestrator/issues/18) and [#6](https://github.com/Vinosaamaa/league-of-orchestrator/issues/6)
Audit source date: 2026-08-27; owner-source refresh: 2026-08-28
Mode: read-only source, installed-contract, path, schema, dependency, and runtime inspection

## Result and publication boundary

This is the public-safe form of the complete audit supplied for issue #18. It
preserves the artifact contracts, producer/consumer relationships, transaction
assumptions, unknowns, schema mapping, and migration boundaries while omitting
private absolute paths, live record values, runtime identities, machine
topology, transcript content, and credentials or other secret-like material.

The audit and refresh did not install software, change global hooks, create a
League database, migrate state, modify live Roster records, or operate an agent
endpoint. The prototype added with this decision uses synthetic temporary state
only.

The evidence supports this storage split:

- Move cross-record canonical coordination state to one embedded SQLite file:
  agent/task identity, ordered events and current state, callsign reservations,
  requests and assignment receipts, delivery acknowledgement, exact project
  identity, watcher coordination state, and resource leases.
- Keep user-selected policy and integration configuration as files: hooks,
  model-routing tiers, optional Lead preference, installed guides,
  completions, launchers, and skill-manager state.
- Keep immutable teardown archives and bounded evidence as files.
- After cutover, generate bounded JSON/JSONL only for export, backup, and
  compatibility. Never run a permanent JSON/JSONL plus SQLite dual canonical
  write path.

## Authority and source binding

| Surface | Public-safe binding | Authority and disposition |
| --- | --- | --- |
| Installed watcher | Stable `agent-watcher` launcher and its installed standard-library Python runtime | Current live implementation; installed bytes were inspected read-only. Exact local paths and payloads are omitted. |
| Current source owner | [`terminal-environment-toolkit`](https://github.com/Vinosaamaa/terminal-environment-toolkit) at `51cfad445843c3f2cab7884f3ddff0a3d8a67d77` | Owns the current launcher, runtime, installer, completion, examples, global guide, and orchestration reference. Runtime and installed bytes matched at refresh time; reverify before migration. |
| League baseline | League commit `7eebc36b7811ee079b4379fcc055c44f1ed9f5cd`, imported from toolkit revision `93635786746b1d2bea21cca7d276e2106aa99fb5` | Public bootstrap authority, but not installed authority. Its runtime predates the recoverable-launch additions listed below. |
| League schemas | `schema/agent-status.schema.json`, `schema/agent-update.schema.json`, and `schema/agent-routing.schema.json` | Authoring aids only. Runtime validation also enforces duplicate-key, path-identity, exact-identity, and snapshot/event-parity rules. |
| Live Roster data | Home-relative Roster record contract under `.agents/shotcallers/` | Each represented agent owns its record pair. Personal values are not repository source and are not published. |
| Hook configuration | Harness-owned hook JSON | User/runtime configuration. The current installer preserves unrelated handlers. |
| Herdr/tmux observations | Adapter output | External runtime evidence. Only bounded identity/receipt fields belong in League state. |
| Other products and skills | Product- or skill-manager-owned files | No exact League-state consumer was found in the inspected path-name scan; shared concepts do not confer ownership. |

The imported League runtime uses only the Python standard library. The current
repository has no SQLite adapter, ORM, or database server dependency. At refresh
time CPython 3.14.6 loaded SQLite 3.53.4; a separate system CLI reported 3.51.0.
Only the loaded application library controls the WAL gate.

## Classification legend

- **Canonical state**: authoritative domain identity or lifecycle state.
- **Configuration**: user- or release-selected behavior, not transactional task
  history.
- **Coordination state**: durable cursor, delivery, claim, lease, or recovery
  metadata.
- **Archive/evidence**: immutable or append-only receipt retained after active
  state is retired.
- **IPC/export**: transient or generated representation, never a second live
  authority.

## Complete producer-consumer matrix

### Active Roster, launch, watcher, allocation, and delivery

| ID | Artifact/path contract | Producers | Consumers | Class and retention | Atomicity, ordering, and recovery assumptions | Migration disposition |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | `~/.agents/shotcallers/<Shotcaller>/status.json` | External initial creator; current watcher has no Shotcaller transition writer | Session scoping, Stop/UserPrompt hooks, direct delivery, status, tests, guides | Canonical current Shotcaller state while active | Strict object/path/callsign validation; no enforced Shotcaller event parity or recovery writer | Import exact current incarnation/state; retain backup; export only after cutover |
| R2 | `~/.agents/shotcallers/<Shotcaller>/champions/<Champion>/status.json` | Herdr launch creates; `transition` replaces; teardown removes after proof and archive | Launch/preflight, status/watch, delivery, reconciliation, teardown, tests, guides | Canonical current Champion state | Strict UTF-8/duplicate-key and exact identity; staged launch `fsync`s files but not parent directory; latest event parity required | `agent_instances` plus `tasks`; JSON current snapshot becomes export only |
| R3 | `~/.agents/shotcallers/<Shotcaller>/champions/<Champion>/updates.jsonl` | Launch creates event 1; `transition` appends; tests use synthetic logs | Pair validator, watcher scanner, delivery freshness, teardown archive | Canonical per-agent lifecycle history | Cooperative `flock`; file order only; no global order; incomplete final line ignored by streaming; append-before-snapshot crash can leave mismatch | Ordered `events` with stable ID and per-entity version; preserve legacy digest mapping; JSONL export only |
| R4 | `~/.agents/shotcallers/<Shotcaller>/updates.jsonl` when present | External/manual creator | Guides imply it; current watcher does not scan or parity-check it | Ambiguous evidence | No enforced writer, order, parity, or recovery contract | Strictly classify if present; otherwise retain backup and report unknown rather than invent history |
| L1 | `~/.agents/shotcallers/<Shotcaller>/pending-launches/<task-id>.json` | Current toolkit `launch`/`resume` reservation and retry path | Launch preflight, exact retry binding, runtime cleanup, record publication | Canonical in-flight launch/recovery state, schema 1 | Shotcaller launch lock; phases `reserved`/`started`; binds task, callsign, route/display, target, project quartet, attempt, and observed runtime generation; deleted after activation | `launch_attempts`/claims in DB; keep one recoverable transaction state and stable attempt ID; no public payload export |
| L2 | `~/.agents/shotcallers/<Shotcaller>/pending-launches/failed/<task-id>[-<attempt-id>].json` | Current toolkit failed-launch writer | Human/operator recovery and tests | Immutable failure receipt/evidence | First stable receipt plus immutable per-attempt collision form; sanitized error and bounded cleanup/observed identity; ordinary atomic JSON write | Retain immutable receipt file or bounded evidence object; DB stores attempt state and optional receipt hash/pointer |
| L3 | `~/.agents/shotcallers/<Shotcaller>/.launch.lock` and launch temp files | Launch/retry implementation | Concurrent launch participants and crash cleanup | Transient implementation state | `flock` serializes pending reservation; pool mutation, runtime start, pending file, and record publication are still separate steps | Do not import locks/temps; DB transaction owns reservation and attempt state; runtime side effects remain compensated/idempotent |
| W1 | `~/.local/state/agent-watcher/shotcallers/<Shotcaller>/state.json`, schema 2 | Watcher Store controls, scan, wait, hooks, reconciliation, delivery | Scoped watcher commands and hooks | Coordination state | Sidecar state lock and wait lock; atomic replace lacks file/directory `fsync`; offsets/digests, queues, generations, wait PID/start, and debounce share one file | Split into watcher scopes, version cursors, deliveries, wait leases, and reconciliation |
| W2 | `~/.local/state/agent-watcher/state.json` | Unscoped/historical commands | Commands without resolved Shotcaller | Legacy coordination state | Same Store contract as W1; can coexist and diverge from scoped state | Import only settings with defined global meaning; otherwise preserve backup and initialize explicit scopes |
| W3 | Watcher `.state.lock`, `.wait.lock`, and temp files | Watcher Store | `flock` participants, bounded crash handling | Transient implementation state | Path/inode locks; abandoned temps are not runtime-recovered | Do not import; new lock/recovery artifacts remain implementation details |
| C1 | `~/.agents/league-champions.json` | Herdr launch reserve; teardown/launch rollback release; other creation remains external | Launch/preflight, teardown, tests, guide | Canonical visible allocation | No schema version; `available` plus `in_use`; current pending entries add `task_id` and `pending`; pool, runtime, pending attempt, and record publication are not one transaction | `callsigns`, leases, agent incarnations, and events; issue #13 owns cooldown/history policy |
| C2 | `~/.agents/scientists.json`, schema 1 | Installer creates if absent; hidden-worker allocate/release mutates | Allocator, releaser, tests, guide | Canonical hidden allocation | Sidecar lock, file `fsync`, replace; no directory `fsync`, history, or stale-allocation recovery | Agent incarnations/leases/events; deterministic export only |
| D1 | Pending material candidates embedded in W1 | Watcher scan or reconciliation | Wait, supervise, direct delivery | Coordination outbox | Source path/offset/digest identity; freshness re-read under record lock; lexical cross-record order; superseded candidate suppression | Query stable events/delivery rows; bounded claim lease; no path-offset identity |
| D2 | W1 `delivered_events` and `seen` | Watcher/direct adapter | Deduplication and response handling | Local delivery receipt/cursor | Capped arrays; crash after send can redeliver; claim-before-output can lose observation; current `delivered` means channel acceptance, not receiver acknowledgement | Delivery state machine: pending/claimed/accepted/acknowledged/failed/superseded; map legacy entries as accepted only |
| D3 | Caller-selected Lead event JSON | External caller | `lead-relay` | IPC/export envelope | Dynamic path; material event object validation only | Stable event-ID lookup; retain versioned offline adapter only if needed |
| D4 | Caller-selected Lead relay-state JSON | `lead-relay` after successful command | `lead-relay` deduplication | Coordination receipt | Dynamic path, sidecar lock, capped event+Lead digests; subprocess success is not receiver acknowledgement | Import only an exact verified configured path; otherwise start delivery rows without guessing |
| D5 | Lead delivery JSON on subprocess stdin | `lead-relay` | Caller-supplied delivery adapter | Transient IPC | No schema negotiation or remote acknowledgement | Versioned event/recipient/attempt envelope behind an adapter |

### Policy, project, request, resource, and lifecycle evidence

| ID | Artifact/path contract | Producers | Consumers | Class and retention | Atomicity, ordering, and recovery assumptions | Migration disposition |
| --- | --- | --- | --- | --- | --- | --- |
| P1 | `~/.agents/agent-routing.json`, schema 1 | Installer creates if absent; user/config owner edits | Model routing, guide, tests | Configuration | Read without lock; selected result is emitted but not persisted by current runtime | Retain configuration file; persist resolved model/effort/tier/reason on assignment |
| P2 | `~/.agents/lead-shotcaller.json`, schema 1 | Installer creates if absent; later writer unspecified | Lead relay and guide | Configuration, not task ownership | Read without lock; reassignment affects future relay identity only | Retain preference file; ownership and delivery state belong in DB |
| P3 | Project catalog JSON | No current artifact or producer | No current consumer; issue #9 owns design | Absent/planned | No legacy contract to migrate | Create `projects` only from proven exact repository identity; do not invent aliases/history |
| P4 | Durable request inbox and assignment JSON/JSONL | No current artifact or producer | No current consumer; issue #17 owns semantics | Absent/planned | No legacy contract; transcripts/status text are not import sources | Add requests/claims/assignment receipts when implemented; no fabricated historical rows |
| P5 | `~/.agents/task-resources.json`, schema 1 | Installer creates if absent; lease add/renew writer remains unspecified; teardown removes verified leases | Teardown, resource inspection, tests, guide | Canonical resource lease state | Reads unlocked; release rechecks under lock after external stop; crash can leave stale lease | `resource_leases` with active/releasing/released/stale and exact generation/start identity |
| P6 | Caller-selected hidden-worker release evidence JSON | Parent/caller reconciliation | Hidden-worker release | Evidence input/IPC | Dynamic path; exact identity plus three release gates | Resolve against DB state; retain only versioned offline evidence adapter |
| T1 | Caller-selected active teardown manifest JSON, schema 2 | Shotcaller/external generator; tests create synthetic fixtures | Teardown dry-run/execute | Pre-execution evidence; archived copy becomes immutable | Fail-closed identity, Git, PR/CI, release/smoke/rejection, resource, callsign, path, and secret checks; external cleanup cannot be one DB transaction | Keep versioned generated file from DB plus owner receipts; never second current-state authority |
| T2 | Archived `status.json` and `updates.jsonl` | Teardown copies active pair into a staged archive | Human audit/support and teardown verification | Immutable archive/evidence | Staged then atomically published; destination collision refuses | Retain byte-for-byte; optional DB path/hash pointer only |
| T3 | Archived `task.json` | Teardown summary writer | Human audit/support and tests | Immutable archive/evidence | Atomic file write within stage; schema is not separately versioned today | Retain as versioned file; DB may point/hash, not duplicate authority |
| T4 | Archived `teardown-manifest.json` | Teardown copies input then appends result | Human audit/support and tests | Immutable evidence with one current post-publication update | Cleanup, publication, record deletion, callsign release, and final manifest update are sequential; no recovery journal | Retain final receipt file; DB teardown state must make finalization idempotent |
| T5 | Bounded archive evidence text/JSON/JSONL/log/Markdown | Teardown copies caller-named safe regular files | Human audit/support | Immutable evidence | Size/type/symlink/NUL/duplicate/secret-like checks fail closed; scan is not proof of all-secret absence | Retain files; optional class/path/hash metadata only; never bulk-import contents |
| T6 | Timestamped installer backups | Installer before replacement | Operator rollback and installer trap | Backup/evidence; may include private configuration | Ordinary copies; retention unspecified | Keep restricted outside DB under explicit retention; never include payloads in public export |

### Hooks, adapters, wrappers, schemas, exports, and skills

| ID | Artifact/path contract | Producers | Consumers | Class and retention | Assumptions and compatibility | Migration disposition |
| --- | --- | --- | --- | --- | --- | --- |
| H1 | Harness hook JSON such as `~/.codex/hooks.json` | Harness/user tools; installer renders and preserves unrelated handlers | Harness runtime | Configuration | Watcher handlers are idempotent by exact command; running sessions may require explicit restart/trust after future changes | Retain file; stable `league` command path after authorized cutover; never store unrelated hooks in DB |
| H2 | Hook JSON stdin | Harness runtime | Hook payload reader; current watcher uses only session identity | Transient IPC | Bounded input; empty/unmapped fails open; payload is not retained | Retain IPC; version/capability check if schema changes; never persist full prompt payload |
| H3 | Watcher/League JSON stdout | Every command | Shell hooks, agents, tests, canaries | IPC/export | Current command-specific shapes lack one common versioned envelope | Define versioned stable command schemas; output is never canonical state |
| A1 | Herdr JSON stdout | Herdr | Launch/retry, delivery, reconciliation, endpoint verification, tests | Adapter observation | Exact identity matching; current source also binds pending retries to runtime generation; ambiguity fails closed | Remain outside DB except bounded locator/receipt fields; issue #7 owns adapter versioning |
| A2 | tmux formatted text | tmux | Delivery, reconciliation, endpoint verification | Adapter observation | Exact pane/socket plus formatted fields; not JSON | Remain outside DB except bounded locator/receipt fields; issue #7 owns adapter versioning |
| I1 | `agent-watcher`, `install-agent-watcher`, `_agent-watcher`; installed launcher/bundle/completion under `~/.local` and `~/.zfunc` | Source owner | Shell, user, hooks | Executable/integration files | Launcher resolves symlink; installer stages, backs up, swaps, preserves user config, and verifies parity | Remain source/install files; transfer ownership only through separately authorized release and rollback proof |
| I2 | Installed `agent-status.example.json` and `agent-updates.example.jsonl` | Installer from maintained source | Human authoring, guide, parity tests | Sanitized example/export | Example pair must match exactly and every task identity must be replaced before use | Retain examples; after cutover demonstrate non-canonical export |
| I3 | League JSON Schemas/examples | League source | Tests and human authors; runtime does not execute schemas | Authoring contract | Runtime rules are stricter; only status/update/routing currently have formal schemas | Retain; add versioned schemas for stable exported artifacts and parity tests |
| S1 | Installed custom skills under `~/.agents/skills` and `~/.codex/skills` | Separate skill sources/installers | Agent runtime | Configuration/code | Exact League path-name scan found no state parser/producer | Keep outside DB; issue #10 owns provenance/capability declarations |
| S2 | `~/.agents/.skill-lock.json` | Skill-manager tooling | Skill-manager tooling | Separate install state | Producer not proven in League/toolkit owners; payload not a migration source | Out of scope; retain unchanged |
| S3 | Firstmate and other product JSON/JSONL | Other product owners | Their own runtimes | Separate product state | Independent locks, inboxes, hooks, adapter data, and session contracts | Out of scope; never import based on conceptual similarity |

## Exact current schemas and invariants

### Roster snapshot and event

Every current visible snapshot requires `callsign`, `role`, `shotcaller`,
`kind`, `address`, `thread_id`, `task`, `status`, `updated_at`, `update`,
`blocker`, and `next`. A Champion also requires `backend`, `task_id`,
`repository`, `issue`, `branch`, and `worktree`. `routing_name` and
`display_agent` are optional only as a pair.

Runtime-only rules include:

- invalid UTF-8 and duplicate JSON keys fail closed;
- record-directory, callsign, and owning Shotcaller identities must agree;
- current Champion thread, backend, pane, task, and project quartet must be
  exact, non-placeholder, and complete or all-null where allowed;
- Herdr routing name equals the lowercase callsign and is paired with the
  displayed backend kind;
- `repository`, `issue`, `branch`, and `worktree` are all exact or all null;
- supported lifecycle states are `active`, `started`, `working`, `progress`,
  `blocked`, `completed`, `complete`, `cancelled`, `canceled`, `failed`, and
  `ready_to_land`;
- the latest Champion event exactly matches snapshot status, timestamp, and
  update text.

Each current JSONL event requires `at`, `status`, and non-empty `update`.
Additional keys are accepted, but duplicate keys are rejected. There is no
stored event ID or sequence. The watcher derives a digest from source path,
byte offset, and exact line content.

### Watcher schema 2

The current watcher state combines control, wait lease, scan cursor, delivery,
and reconciliation domains in one object:

`schema`, `enabled`, `allow_stop_once`, `stop_blocked`, `generation`,
`initialized`, `last_active`, `offsets`, `seen`,
`user_message_generation`, `wait_active`, `wait_generation`, `wait_pid`,
`wait_process_start`, `pending_events`, `delivered_events`, `last_event_id`, and
`reconciliation`.

SQLite must separate these domains so corruption or cleanup in one does not
reset the others.

### Launch schema 1 and callsign allocation

The current owner source adds a pending launch schema with `task_id`,
`callsign`, `routing_name`, `display_agent`, `address`, `pool`, `record`,
`herdr_session`, `attempt_id`, and `phase`; it may carry exact project identity,
resume thread, start time, and observed runtime generation. Phase is `reserved`
or `started`. Failed-launch receipts are immutable and include a sanitized
failure class plus cleanup proof.

Visible callsign state has no top-level schema version. It contains
role-specific available arrays and an `in_use` mapping; pending current-source
reservations add task identity and a pending marker. Hidden-worker state is
schema 1 with available and active assignments. Neither pool preserves complete
immutable allocation/release history.

### Resources and teardown

Resource schema 1 contains exact task-owned process leases and shared-browser
owners. A process lease requires resource/task/owner/endpoint/generation/PID and
OS process-start identity. A shared-resource lease uses exact task, owner, and
generation identity.

Teardown manifest schema 2 validates task, agent, adapter, Git, publication,
CI, release/deployment/smoke or explicit rejection, resource, callsign, grace,
terminal/idle, pending-decision, archive, and optional evidence gates. The
Python validator and synthetic lifecycle fixture are the executable contract;
there is no maintained JSON Schema or generator.

## Code, guide, wrapper, skill, and test inventory

| Source | Responsibility |
| --- | --- |
| Toolkit `agent_watcher.py` | Strict decoders; Roster parity; recoverable Herdr launch/resume and pending receipts; callsign mutation; watcher Store; delivery; reconciliation; resource cleanup; hidden workers; Lead relay; model routing; hooks; archive; teardown; JSON IPC |
| League `src/agent_watcher.py` | Imported pre-recoverable-launch baseline; not installed authority and not modified by this decision |
| Toolkit `install-agent-watcher` and `shell-completions/_agent-watcher` | Install/rollback parity, preserved configuration, hook rendering, default file creation, and command exposure |
| Toolkit `agent-watcher` and League `bin/agent-watcher` | Resolve source path and execute runtime; no state model |
| Installed/global `AGENTS.md` and toolkit `global-agent-instructions/shared-AGENTS.md` | Record ownership, path templates, callsign pools, transition, supervision, and teardown boundaries |
| `AGENT_ORCHESTRATION_REFERENCE.md` | Exact record schemas, delivery/reconciliation semantics, launch lifecycle, archive paths, resources, hidden workers, Lead, routing, install, and rollback contracts |
| `AGENT_OPERATIONS_REFERENCE.md` | Installed topology, watcher/transition workflow, hooks, archives, and task-resource handoff |
| `TMUX_QUICK_GUIDE.md` | Codex/Cursor hook reload and terminal trust boundary; no canonical state writer |
| League `docs/ARCHITECTURE.md`, `MIGRATION.md`, `PROVENANCE.md`, `ROADMAP.md`, and ADRs | Public baseline, source ownership, planned issues, decision, and cutover boundaries |
| Installed custom skills and agent-skills source | No exact consumer of the audited League paths found in the scoped scan |
| Firstmate source | No exact consumer of audited League paths; adapter techniques are compatibility evidence only |

Current owner-source `test-agent-watcher.py`, `test-agent-record-contract.py`,
`test-agent-delivery.py`, `test-agent-reconciliation.py`, and
`test-agent-lifecycle.py` cover strict parsing/parity, watcher controls and
deduplication, delivery freshness, append/snapshot rollback, reconciliation,
recoverable launch/resume and trust retry, callsign reservation, task resources,
hidden workers, Lead relay, routing, proof-gated teardown, archive/secret
refusal, hook preservation, and fake Herdr/tmux adapters.
`test-install-agent-watcher.zsh` covers isolated install parity and preserved
configuration. Watcher/delivery canaries exist but were inspected, not run.
The owner workflow runs these maintained surfaces in CI.

League contains adapted `test_agent_watcher.py`, `test_record_contract.py`,
`test_delivery.py`, `test_reconciliation.py`, and `test_lifecycle.py`, plus
`test_schema_examples.py`. Migration-specific SQLite tests did not exist before
`test_sqlite_store_prototype.py` in this decision PR.

## Known gaps and unknown paths

1. Shotcaller creation, tmux Champion creation, and other-backend creation
   remain external to the watcher.
2. The current source added recoverable Herdr pending-launch files, but the
   League baseline predates them; a future cutover must import or explicitly
   retire this newer contract rather than treating the League copy as installed
   authority.
3. Callsign reservation, runtime start, pending attempt, and record publication
   remain multiple filesystem/process steps. Recovery is compensating and
   idempotent, not one transaction.
4. Dynamic Lead event/relay-state, hidden-release evidence, teardown manifest,
   and optional archive-evidence paths are caller supplied. Migration must use
   an explicit manifest and never scan the home directory by guess.
5. No implemented task-resource lease add/renew writer was found.
6. No maintained teardown-manifest generator or JSON Schema exists.
7. Delivery has local channel acceptance but no end-recipient acknowledgement;
   legacy receipts cannot be relabeled acknowledged.
8. JSONL has per-file order only. Cross-record selection is lexical path then
   byte offset, not a global occurrence order.
9. Append, snapshot replace, watcher receipt, resource release, archive
   publication, active-record deletion, and callsign release span crash windows
   without one recovery journal.
10. Current teardown advances `ready_to_land` to `completed` before archive and
    cleanup. A later failure makes the same manifest fail its initial status
    gate, and this write conflicts with the stated record-owner boundary.
11. Filesystem atomics omit some parent-directory `fsync` operations; watcher
    state replacement also omits file `fsync`.
12. Runtime validation and JSON Schema are independently maintained and already
    differ in scope.
13. Project catalog, request inbox, assignment claim, and assignment receipt
    state do not exist. Migration must not infer them from transcripts or text.
14. Model routing resolution is emitted but not durably bound to the assignment.
15. Callsign release history, cooldown, and immutable incarnation identity are
    absent.
16. Skill-manager lock ownership is outside current League/toolkit sources.
17. Installer backup retention is unspecified and may include private config.
18. Failed-launch receipts can contain bounded runtime observations and local
    record targets; public export must omit or redact those fields while private
    recovery retains them.
19. Loaded SQLite version can differ from system CLI or package metadata. The
    future released runtime and stable launcher remain unpinned until a later
    install issue proves exact binding.

## Proposed canonical schema map

This is the accepted logical map, not production DDL. The bounded prototype in
`prototypes/sqlite_store.py` implements only the issue-#6 operations needed to
test the decision. Later implementation issues own the full versioned schema.

| Table/domain | Proven entities and constraints |
| --- | --- |
| `schema_migrations` | Ordered version, name, checksum, application version, applied time; refuse gaps, checksum drift, and newer versions |
| `projects` | Exact project ID and repository identity; aliases remain issue #9 scope |
| `tasks` | Immutable task ID, optional exact project quartet, state/version, current owner; all-null/all-exact repository invariant |
| `callsigns` | Callsign and role, enablement, usage/release metadata; policy such as cooldown belongs to issue #13 |
| `agent_instances` | Immutable incarnation ID, callsign, role/owner/task, harness locator, current state/version, route/display, resolved model choice, retirement/archive pointer |
| `callsign_leases` | One live callsign to one agent incarnation; reservation/release event in same transaction |
| `launch_attempts` | Stable task/attempt identity, intended locator, phase, exact runtime generation, compensation/recovery result |
| `requests` and claim/assignment receipts | Parent/order/idempotency, project/task/owner, disposition/version, lease token/expiry, exact accepted agent version; no fabricated legacy rows |
| `resource_leases` | Exact task/owner/kind/endpoint/generation/process-start, state and release time |
| `events` | Stable event ID, one entity target, per-entity version, type/status/update/time, bounded detail; unique entity/version |
| `watcher_scopes` | Enable/Stop/user-message generations and bounded wait lease, one per exact Shotcaller incarnation |
| `watcher_cursors` | Stable source entity plus next version; no file offsets after import |
| `runtime_reconciliation` | Scope/agent observation, consecutive count, time, bounded adapter evidence |
| `deliveries` | Event/recipient key, pending/claimed/accepted/acknowledged/failed/superseded, attempts, claim expiry, bounded receipt |

Every connection enables and verifies foreign keys, sets a bounded busy timeout,
and applies an explicit synchronous policy. A transition inserts one event and
compare-and-swap updates current state in one short transaction. Callsign
reservation and task-owner transfer similarly update all canonical rows and
their event/receipt together. No transaction remains open while prompting,
running Git/GitHub, waiting on a process, or operating an adapter.

## Files that remain files

1. Harness-owned hook configuration.
2. Model-routing tier configuration.
3. Optional Lead preference while it is a relay preference, not ownership.
4. Installed policy guides and orchestration reference.
5. Launcher, completion, and installed bundle files.
6. Sanitized authoring schemas and examples.
7. Immutable archives containing record copies, task summary, teardown
   manifest, and bounded evidence.
8. Restricted timestamped installer backups.
9. Separate skill-manager state.
10. Firstmate and other product-owned state.
11. Immutable pre-migration JSON/JSONL backup.
12. On-demand deterministic, versioned, redacted JSON/JSONL exports.
13. Immutable failed-launch receipts or equivalent bounded evidence objects.

After cutover, active snapshots/events, visible/hidden pools, watcher state,
pending-launch state, resource registry, and relay deduplication cease to be
live file authority. A compatibility export may reproduce their public schema,
but no live path writes both it and SQLite.

## Stable command boundary

Agents use stable `league` commands and never receive SQL as an operating
interface. The internal store may evolve without changing these capabilities:

| Capability | Transactional contract |
| --- | --- |
| `league agent status` / `league agent transition` | Read current incarnation; append event and compare-and-swap snapshot together |
| `league callsign reserve` / `league callsign release` | Unique lease plus incarnation/event; idempotent retry and explicit recovery |
| `league delivery claim` / `ack` / `fail` | Stable event/recipient identity, bounded claim, attempts, acceptance versus acknowledgement |
| `league project resolve` | Exact canonical project identity; aliases only after issue #9 |
| `league task transfer-owner` | Task/Squad-owner version update, event, and assignment receipt in one transaction |
| `league request ...` | Future issue-#17 request/claim lifecycle with opaque claim token and no fabricated history |
| `league resource ...` | Exact lease generation/start identity and fail-closed release |
| `league storage integrity` / `migrate` / `backup` / `export` | Versioned operator result; no private public payload; no second writable authority |

Adapters, SQL, pragmas, retries, and database paths are implementation details
behind these commands. Issue #21 may expand the design narrative, but does not
own or reopen this accepted minimal storage choice.

## Migration and cutover order

### 0. Decision and release gates

1. Accept this audit and ADR; keep current filesystem behavior until a separate
   implementation and migration issue is authorized.
2. Freeze versioned schemas, database location, local-filesystem requirement,
   stable command envelopes, runtime binding, and rollback owner.
3. Check the SQLite library loaded by the exact released runtime. Enable WAL
   only at 3.51.3 or newer; otherwise explicitly select rollback journal.
4. Verify `foreign_keys=ON`, bounded busy timeout, synchronous policy,
   migrations, backup, export, and refusal behavior on every connection path.

### 1. Non-live prototype

Use temporary synthetic state to prove callsign reservation, event plus current
state, delivery claim/acknowledgement, exact project lookup, atomic owner
transfer, contention bounds, migrations, integrity/foreign-key checks, verified
backup, and deterministic redacted export. This PR performs only this stage.

### 2. Pre-migration backup and inventory

Under separately granted authority, resolve exact installed source parity,
quiesce all writers through an explicit cutover gate, copy only the manifest of
audited state/config/archive metadata into a restricted backup, hash and
strictly validate it, and abort on malformed, changing, ambiguous, colliding,
or unknown dynamic paths.

### 3. Dry-run import

Import from the immutable backup into a temporary DB. Assign immutable agent
incarnations and per-entity versions; preserve deterministic legacy digests;
reconcile leases to exact owners; map legacy delivery as accepted, never
acknowledged; derive version cursors instead of byte offsets; keep archives
outside; and fabricate no absent request, project alias, model choice, or
receipt history.

### 4. Behavior parity

Run the storage-neutral lifecycle suite with fake adapters, compare deterministic
exports/counts/hashes, reopen after crash injection, validate migrations,
integrity, and foreign keys, and use read-only shadow queries only. Do not
shadow-write both stores.

### 5. Atomic cutover

Reacquire the global gate, prove no writer is active, re-hash live inputs,
install the exact released DB-capable command while preserving unrelated
configuration, publish one canonical-store marker, make every new command
refuse legacy writes, and start SQLite as the sole authority. Preserve the old
files read-only in backup; do not delete them.

### 6. Post-cutover and rollback

Run focused command, integrity, foreign-key, backup/restore, export, and adapter
smoke checks under their own authority. Back up through the SQLite backup API or
a correct quiescent/checkpoint procedure; never copy only the main file while
WAL is active.

Before the first DB-canonical write, rollback restores the exact legacy bundle
and immutable file backup after validation. After a DB-canonical write, never
restore stale JSON directly: quiesce, use a tested down-migrator to a new staged
legacy tree, validate every invariant, atomically switch authority, and preserve
the DB plus receipts. If down-migration is lossy, rollback is blocked and
forward repair is required. Exactly one store is writable before and after.

## Verification required before migration authority

- Import failures: duplicate keys, invalid UTF-8, truncated/missing-newline
  JSONL, snapshot/log skew, placeholders, partial identity, path mismatch,
  orphan pools/resources, scoped/global watcher ambiguity, unknown relay paths,
  launch-attempt conflicts, and archive collisions.
- Transaction/concurrency: competing reservations and transitions, stale
  versions, owner transfer, request idempotency/claims, lease generations,
  bounded busy retries, and crash points around external launch/cleanup.
- Delivery/hooks/adapters: baseline without replay, supersession, disabled
  preservation, Stop/user priority, claim/send/accept/ack crash points,
  deduplication by stable event ID, fake Herdr/tmux malformed/conflict cases,
  hook preservation, and versioned command output.
- Recovery/teardown: commit/migration/backup/checkpoint/archive/resource/process
  failures, disk/permission/corruption/busy cases, stale PID/wait leases, retry
  after the current pre-cleanup completed transition, and exact no-wrong-target
  guarantees.
- Export/security/rollback: deterministic bounded schema, default omission of
  request/update text, runtime locators, local paths, and receipts from public
  output; no secret-like content; loaded-runtime WAL assertion; backup sidecar
  correctness; and exactly one canonical writer across rollback.

## Final disposition

| Current artifact | Decision |
| --- | --- |
| Active Shotcaller/Champion snapshots and events | SQLite current rows and ordered events; JSON/JSONL export only |
| Visible/hidden callsign pools and pending launches | SQLite callsigns, leases, agent incarnations, launch attempts, and events |
| Watcher control/cursors/pending/delivered/reconciliation | SQLite scopes, version cursors, delivery states, wait leases, and reconciliation |
| Request inbox/claims/assignment receipts | New SQLite state under issue #17; no fabricated import |
| Exact project identity | SQLite projects from proven identity; aliases wait for issue #9 |
| Resource leases | SQLite leases and events |
| Model routing tiers and optional Lead preference | Retained file configuration; resolved choices and actual ownership/delivery in DB |
| Hooks, guides, wrappers, completions, and skills | Retained files under existing owners |
| Teardown and failed-launch receipts, task summary, archive pair, bounded evidence | Retained immutable files or bounded evidence objects with optional DB path/hash pointer |
| Installer backup and pre-migration snapshot | Restricted retained backup; never live dual authority |
| Other product state | Out of scope; never imported |

The audit satisfies issue #18. The accepted decision in ADR 0002 satisfies the
issue-#6 choice only; it grants no live implementation, install, hook change,
migration, release, or teardown authority.
