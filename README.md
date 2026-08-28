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
- Two checksummed schema migrations, a loaded-runtime WAL gate, verified
  backups, integrity checks, expected-version writes, and bounded contention.
- A strict manifest importer covering every canonical issue-#18 artifact
  family, with dry-run digest confirmation before apply.
- Deterministic bounded inspection and restricted rollback exports with
  machine-readable schemas.

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
make test-acceptance
make test-affected
make test-all
```

`make test` runs the inherited baseline once, `make test-storage` runs the
storage slice once, `make test-acceptance` runs the isolated issue-#23
foundation, and `make test-affected` composes storage plus acceptance without
overlap. `make test-all` composes baseline plus the affected suite. Every
target uses temporary fixtures only. It does not install files, contact GitHub,
mutate global agent state, or operate live Herdr/tmux sessions.

The repository does not yet own live installation. The currently installed
watcher and rollback process remain owned by `terminal-environment-toolkit`
until a later migration proves source/installed parity and rollback from this
repository. See [migration](docs/MIGRATION.md) and
[provenance](docs/PROVENANCE.md).

Inspect the stable command inventory without creating state:

```sh
./bin/league --help
./bin/league --state-root /absolute/isolated/state-root storage --help
```

Every operation requires an explicit existing absolute state root. The database
filename, SQL, pragmas, and transaction details remain internal. The output
contracts are [command](schema/league-command-output.schema.json),
[import report](schema/league-import-report.schema.json), and
[export](schema/league-export.schema.json) JSON Schemas.

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
request, assignment, watcher, Stop, and teardown slices as pending and does not
claim real Codex, Cursor, Pi, Herdr, or tmux support from fake adapters.

## Project map

- [Architecture and authority boundaries](docs/ARCHITECTURE.md)
- [Filesystem Roster baseline decision](docs/adr/0001-filesystem-roster-baseline.md)
- [Accepted SQLite canonical-store decision](docs/adr/0002-sqlite-canonical-store.md)
- [JSON/JSONL dependency audit](docs/research/json-jsonl-state-dependency-audit.md)
- [SQLite prototype and benchmark](docs/research/sqlite-storage-prototype-benchmark.md)
- [Reversible migration and install boundary](docs/MIGRATION.md)
- [Isolated acceptance and reversible cutover foundation](docs/ACCEPTANCE.md)
- [Exact source provenance](docs/PROVENANCE.md)
- [Baseline versus planned issues](docs/ROADMAP.md)

## Non-goals for this implementation PR

No Roster UI, adapter cutover, global install, hook mutation, live import,
watcher replacement, daemon, merge, release, deployment, or teardown is
introduced here.
