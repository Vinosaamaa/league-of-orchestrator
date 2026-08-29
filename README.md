# League of Orchestrator

League of Orchestrator is a small, local-first lifecycle layer for coordinating
Summoners, Shotcallers, and Champions across agent harnesses and terminal
backends. It preserves exact identity, durable progress, narrow authority, and
proof-gated cleanup without introducing a service or control plane.

The repository baseline was established by
[issue #2](https://github.com/Vinosaamaa/league-of-orchestrator/issues/2). The
repository-local SQLite store and command facade are tracked by
[issue #19](https://github.com/Vinosaamaa/league-of-orchestrator/issues/19),
under the [project plan in #1](https://github.com/Vinosaamaa/league-of-orchestrator/issues/1).
Nothing here installs files, changes hooks, or connects to live Roster state.

## Baseline in this candidate

- A byte-preserved watcher core proven in `terminal-environment-toolkit`.
- Strict JSON/JSONL Roster validation and matching snapshot/event semantics.
- Durable watcher offsets, event deduplication, scoped delivery, and bounded
  runtime reconciliation.
- Atomic Herdr launch preflight with routing/display identity verification and
  reservation rollback on a failed launch.
- Current thin Herdr and tmux adapters, with their portability limits explicit.
- Semantic model/effort routing with explicit user overrides preserved.
- Fail-closed schema-2 teardown verification, including local-install byte
  parity and later-main squash proof, plus archive boundaries.
- Synthetic examples, authoring schemas, and focused local regression tests.
- One standard-library SQLite implementation behind a `Storage` protocol and
  stable `league` command facade.
- Nine contiguous checksummed schema migrations, a loaded-runtime WAL gate, verified
  backups, integrity checks, expected-version writes, and bounded contention.
- A strict manifest importer covering every canonical issue-#18 artifact
  family, with dry-run digest confirmation before apply.
- Deterministic bounded inspection and restricted rollback exports with
  machine-readable schemas.
- Prompt-once intake, complete bounded triage, request claims and states,
  explicit direct/hidden/Champion dispatch, and unresolved reconciliation.
- Recoverable visible-Champion assignment with exact acceptance receipts,
  source-bound transition outbox delivery, unique recipient effects, and fair
  backlog draining.
- One role-aware bounded Shotcaller Stop decision with ordinary-message
  priority and separate request, dispatch, and watcher leases.
- Opaque capability-based harness/backend bindings, typed task resources, and
  recoverable proof-first teardown with immutable per-action receipts.
- Assignment-neutral semantic model/effort routing that preserves explicit
  choices and permits one evidence-triggered safe-boundary escalation.
- A sanitized custom-skill provenance inventory, bounded install-parity audit,
  and provider/model-neutral runtime capability matrix with explicit shared
  inline fallback and specialist refusal.
- A canonical-ID project catalog with exact local/repository identities,
  aliases/codes, and many-to-many advisory Squad suggestions that never move
  work or override explicit routing.
- One bounded read-only project-grouped Roster snapshot with exact evidence
  references, outbound redaction, and an accepted terminal-first design.
- One persisted seeded callsign queue with compatibility-first allocation,
  exact reservation rollback, tail release, and immutable assignment history.
- One guarded disposable Shotcaller rollover for a stable Squad with a bounded
  immutable Champion snapshot, exact successor acknowledgement, and one atomic
  owner/event/outbox switch.
- Deterministic bounded activity reports with stable JSON, exact range/timezone
  and scope, immutable show/since specifications, completion gates, indexed
  pagination, and JSON-derived Markdown/portable HTML.
- Structured local-only project/evidence classification and one fail-closed
  final-rendered-payload validator shared by every League remote adapter.
- One provider-neutral v1 routing slice with explicit/continuation/unique-strong
  Squad ownership, exact direct-tiny bounds, recorded hidden scientists,
  acknowledgement-gated transfer, safe Squad registration, parent-request
  progress coalescing, versioned provider routing, and one evidence-triggered
  model escalation.

The proven watcher remains one deep module. The new storage slice is a small
modular monolith: one composite `Storage` interface with cohesive administrative,
lifecycle, delivery, and transfer subprotocols; one `SQLiteStorage` facade over
a shared connection/transaction core and focused SQL operation modules; one
import planner; and one CLI. The watcher does not import it yet, so this feature
branch cannot become a second live canonical writer.

## Local development

Requirements: Python 3, Git, and a POSIX shell.

```sh
make test
make test-storage
make test-project-roster
make test-request-lifecycle
make test-runtime-lifecycle
make test-routing-policy
make test-skill-contracts
make test-handoff-callsigns
make test-acceptance
make test-reporting-privacy
make test-affected
make test-all
```

`make test` runs the inherited baseline once, `make test-storage` runs the
storage slice once, `make test-project-roster` runs the focused issues #9/#12
contract, `make test-request-lifecycle` runs the grouped lifecycle
suite, `make test-runtime-lifecycle` runs issues #7/#11/#14, and
`make test-routing-policy` runs issue #36's deterministic owner/execution,
Squad registration, parent-progress, and hidden-scientist contract, while
`make test-skill-contracts` runs issue #10's synthetic provenance, privacy,
duplicate-parity, CLI, and runtime-fallback contract, while
`make test-handoff-callsigns` runs issues #8/#13, and `make test-acceptance`
runs both the isolated issue-#23 foundation and the complete no-apply
pre-cutover command. `make test-affected` composes storage,
acceptance, request lifecycle, runtime/skill/routing lifecycle, handoff/callsign,
reporting/privacy, and public-safety
coverage, and `make test-reporting-privacy` runs issues #22/#25 report, privacy, staged-guide,
metadata, incident, renderer, pagination, and latency contracts.
`make test-all` adds the inherited baseline once. Every
target uses temporary fixtures only. It does not install files, contact GitHub,
mutate global agent state, or operate live Herdr/tmux sessions.

The repository now proves a staged-inactive release, exact read-only shadow,
backup/restore rehearsal, and deterministic proposed-mutation manifest through
`league acceptance preflight`; that command writes only beneath its explicit
temporary root and stops at `awaiting_authority`. The repository does not yet
own or perform live installation. The currently installed
watcher and rollback process remain owned by `terminal-environment-toolkit`
until a later migration proves source/installed parity and rollback from this
repository. See [migration](docs/MIGRATION.md) and
[provenance](docs/PROVENANCE.md).

Inspect the stable command inventory without creating state:

```sh
./bin/league --help
./bin/league help inventory
./bin/league --state-root /absolute/isolated/state-root storage --help
```

Every state operation requires an explicit existing absolute state root;
machine-readable help is read-only and needs no root. The database
filename, SQL, pragmas, and transaction details remain internal. The output
contracts are [command](schema/league-command-output.schema.json),
[import report](schema/league-import-report.schema.json), and
[export](schema/league-export.schema.json) JSON Schemas. The advisory surfaces
add [project catalog](schema/league-project-catalog.schema.json) and
[Roster snapshot](schema/league-roster-snapshot.schema.json) contracts.

The grouped request-lifecycle command and transaction map is documented in
[request lifecycle](docs/REQUEST_LIFECYCLE.md). Its implementation is inert
until issue #23 separately proves installation and cutover.
The adapter, resource, cleanup, and routing contracts are documented in
[runtime lifecycle](docs/runtime-lifecycle.md) and remain equally repository-local.
The custom-root provenance and capability boundary is documented in
[skill capabilities](docs/skill-capabilities.md). Its validation, audit, and
matrix commands require explicit config/root/profile inputs and create no state.
The project catalog and bounded Roster are documented in
[project catalog](docs/PROJECT_CATALOG.md); the chosen design-only terminal
direction is [Project Ledger](docs/design/terminal-roster-ui.md).
The report source, commands, completion gates, renderers, and report skill are
documented in [reporting](docs/REPORTING.md). The classification, final-byte
remote boundary, publication metadata gate, and staged shared guidance are in
[privacy](docs/PRIVACY.md).

## Repository-local SQLite implementation; cutover still gated

Issue [#6](https://github.com/Vinosaamaa/league-of-orchestrator/issues/6)
accepts one embedded SQLite canonical store, using the complete dependency audit
from [#18](https://github.com/Vinosaamaa/league-of-orchestrator/issues/18).
Agents use stable `league` commands and never SQL; there is no server, ORM, or
permanent dual canonical store. Issue #19 implements that boundary for explicit
isolated roots. The dry-run importer reports exact artifact and row counts,
ordering, retained files, the complete audit disposition, and a digest that is
required before apply. Unknown consumers, duplicate keys or identities,
malformed/truncated records, snapshot/event skew, and target collisions refuse.
Import reads are bound to validated file descriptors, retained-file and
audit-coverage reports are strict schemas, roster offsets are indexed, and
bounded JSONL export emits rows without retaining a second complete row graph.

See [ADR 0002](docs/adr/0002-sqlite-canonical-store.md), the
[dependency audit](docs/research/json-jsonl-state-dependency-audit.md), and the
[prototype benchmark](docs/research/sqlite-storage-prototype-benchmark.md).

The implementation enables WAL only when the SQLite library loaded by the
executing Python runtime is version **3.51.3 or newer**. SQLite's
[WAL-reset documentation](https://www.sqlite.org/wal.html#the_wal_reset_bug)
identifies the affected range through 3.51.2 and the fix in 3.51.3. An older or
unverifiable loaded runtime selects rollback-journal mode and reports the
refusal; a nearby `sqlite3` executable never substitutes for this check.

Issue [#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23)
owns the isolated acceptance harness, staged installation, read-only live-state
shadow, reversible pointer switch, and separately authorized cutover. Until
those gates pass, the filesystem watcher remains the only live authority.
The repository-local foundation and its one explicit-root command are
documented in [isolated acceptance](docs/ACCEPTANCE.md). It records the later
acceptance-receipt extensions for request, assignment, watcher, Stop, and
teardown as pending. All seven now have repository-local implementations and
focused deterministic tests, but have not been folded into a cutover receipt;
the harness does not claim real Codex, Cursor, Pi, Herdr, or tmux support from
fake adapters.

## Project map

- [Architecture and authority boundaries](docs/ARCHITECTURE.md)
- [Filesystem Roster baseline decision](docs/adr/0001-filesystem-roster-baseline.md)
- [Accepted SQLite canonical-store decision](docs/adr/0002-sqlite-canonical-store.md)
- [JSON/JSONL dependency audit](docs/research/json-jsonl-state-dependency-audit.md)
- [SQLite prototype and benchmark](docs/research/sqlite-storage-prototype-benchmark.md)
- [Reversible migration and install boundary](docs/MIGRATION.md)
- [Guarded rollover and shuffled callsign queue](docs/HANDOFF_CALLSIGNS.md)
- [Isolated acceptance and reversible cutover foundation](docs/ACCEPTANCE.md)
- [Exact source provenance](docs/PROVENANCE.md)
- [Repository-local request lifecycle](docs/REQUEST_LIFECYCLE.md)
- [Repository-local runtime lifecycle](docs/runtime-lifecycle.md)
- [Skill provenance and runtime capability contract](docs/skill-capabilities.md)
- [Advisory project catalog and project-grouped Roster](docs/PROJECT_CATALOG.md)
- [Deterministic activity reports](docs/REPORTING.md)
- [Outbound privacy boundary](docs/PRIVACY.md)
- [Research-backed orchestration and model routing policy](docs/research/orchestration-model-routing-policy-evidence.md)
- [Terminal-first Project Ledger design](docs/design/terminal-roster-ui.md)
- [Baseline versus planned issues](docs/ROADMAP.md)

## Non-goals for this implementation PR

No interactive Roster UI or controller, adapter cutover, global install, hook mutation, live import,
watcher replacement, daemon, merge, release, deployment, or teardown is
introduced here.
