# League of Orchestrator

League of Orchestrator is a small, local-first lifecycle layer for coordinating
Summoners, Shotcallers, and Champions across agent harnesses and terminal
backends. It preserves exact identity, durable progress, narrow authority, and
proof-gated cleanup without introducing a service or control plane.

This bootstrap slice is tracked by
[issue #2](https://github.com/Vinosaamaa/league-of-orchestrator/issues/2), under
the [project plan in #1](https://github.com/Vinosaamaa/league-of-orchestrator/issues/1).
It does not install files or connect to live Roster state.

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

The core is intentionally one deep standard-library Python module. This keeps
the proven behavior together while future issues extract stable interfaces only
when their acceptance criteria require it.

## Local development

Requirements: Python 3, Git, and a POSIX shell.

```sh
make test
```

That command uses temporary fixtures only. It does not install files, contact
GitHub, mutate `~/.agents`, or operate live Herdr/tmux sessions.

The repository does not yet own live installation. The currently installed
watcher and rollback process remain owned by `terminal-environment-toolkit`
until a later migration proves source/installed parity and rollback from this
repository. See [migration](docs/MIGRATION.md) and
[provenance](docs/PROVENANCE.md).

## Accepted SQLite decision; migration still planned

Issue [#6](https://github.com/Vinosaamaa/league-of-orchestrator/issues/6)
accepts one embedded SQLite canonical store, using the complete dependency audit
from [#18](https://github.com/Vinosaamaa/league-of-orchestrator/issues/18).
Agents will use stable `league` commands and never SQL; there is no server, ORM,
or permanent dual canonical store. The decision, sanitized audit, and bounded
prototype remain non-live: no installed database is created, no migration runs,
and no hook or live Roster state changes.

See [ADR 0002](docs/adr/0002-sqlite-canonical-store.md), the
[dependency audit](docs/research/json-jsonl-state-dependency-audit.md), and the
[prototype benchmark](docs/research/sqlite-storage-prototype-benchmark.md).

Before any future application runtime enables WAL, the SQLite library loaded by
that runtime must be version **3.51.3 or newer**. SQLite's
[WAL-reset documentation](https://www.sqlite.org/wal.html#the_wal_reset_bug)
identifies the affected range through 3.51.2 and the fix in 3.51.3. Checking an
`sqlite3` command-line executable does not prove which SQLite library Python or
another application runtime has loaded.

For maintained macOS command-line tooling, prefer Homebrew's
[`sqlite` formula](https://formulae.brew.sh/formula/sqlite), currently 3.53.4:

```sh
brew install sqlite
brew upgrade sqlite
```

Run the relevant install or upgrade command explicitly; this repository runs
neither. Check the system CLI, Homebrew CLI, and Python runtime separately:

```sh
/usr/bin/sqlite3 --version
/opt/homebrew/opt/sqlite/bin/sqlite3 --version
python3 -c 'import sqlite3; print(sqlite3.sqlite_version)'
```

Verified machine evidence on 2026-08-27 was:

| Check | Version | Meaning |
| --- | --- | --- |
| `/usr/bin/sqlite3` | 3.51.0 | Below the WAL floor; not runtime proof. |
| `/opt/homebrew/opt/sqlite/bin/sqlite3` | 3.53.4 | Maintained Homebrew CLI; meets the floor. |
| Python `sqlite3.sqlite_version` | 3.53.4 | Loaded Python runtime library; meets the floor. |

These observations are not a WAL authorization. Re-run the runtime check in the
eventual application environment before issue #6 permits WAL configuration.

## Project map

- [Architecture and authority boundaries](docs/ARCHITECTURE.md)
- [Filesystem Roster baseline decision](docs/adr/0001-filesystem-roster-baseline.md)
- [Accepted SQLite canonical-store decision](docs/adr/0002-sqlite-canonical-store.md)
- [JSON/JSONL dependency audit](docs/research/json-jsonl-state-dependency-audit.md)
- [SQLite prototype and benchmark](docs/research/sqlite-storage-prototype-benchmark.md)
- [Reversible migration and install boundary](docs/MIGRATION.md)
- [Exact source provenance](docs/PROVENANCE.md)
- [Baseline versus planned issues](docs/ROADMAP.md)

## Non-goals for the bootstrap

No SQLite store, Roster UI, Shotcaller handoff, project catalog, new callsign
allocator, plugin framework, daemon, live migration, install, merge, release,
or teardown is introduced here.
