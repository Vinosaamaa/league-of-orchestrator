# SQLite storage decision evidence

Status: accepted input for issues [#18](https://github.com/Vinosaamaa/league-of-orchestrator/issues/18) and [#6](https://github.com/Vinosaamaa/league-of-orchestrator/issues/6)
Evidence date: 2026-08-28
Scope: public-safe research and read-only local/runtime inspection. No live install, hook change, state migration, database creation, or endpoint was performed.

## Decision

Use one embedded SQLite database as the canonical store for transactional League coordination state. The database is a local application file opened through the application runtime; there is no SQLite server and no ORM. Agents use stable `league` commands as the public interface and never execute SQL. SQL, transactions, pragmas, retries, and schema details stay behind that command boundary.

Keep non-transactional material in its existing file form:

- user-selected policy and integration configuration remain files;
- immutable teardown archives and bounded evidence remain files;
- JSON/JSONL becomes a versioned backup/export format after cutover, not a second live write authority.

There must be no permanent dual-canonical JSON/JSONL plus SQLite write path. During a future cutover, one backend is authoritative at a time, with an explicit abort/rollback boundary.

## Evidence from the dependency audit

The complete JSON/JSONL producer-consumer audit supplied for issue #18 was reviewed as sanitized source evidence. Its relevant conclusions are:

- active roster identity, lifecycle events/current state, callsign reservations, delivery state, project identity, requests/assignments, and resource leases are cross-record coordination state and are candidates for one transactional store;
- routing, Lead preference, hooks, completions, installed guides, and skill-manager state are configuration or integration state and remain file-owned;
- teardown manifests, archives, installer backups, and evidence are receipts or immutable artifacts and remain file-owned;
- the existing JSON snapshot/event contract has no durable event ID or sequence and derives identity from path/offset/content; a database design must assign stable IDs and explicit state transitions;
- absent or planned artifacts are not inferred as legacy rows; no request, project, or assignment history is fabricated during import;
- JSON output is IPC/export, not canonical state. Full hook payloads, transcripts, personal record values, credentials, tokens, cookies, and secrets are not imported or published.

This mapping preserves the current filesystem baseline until a separately authorized migration. It does not claim that the live state has been migrated.

## SQLite operational contract

The future command implementation should apply these invariants on every opened connection:

| Invariant | Required behavior | Evidence |
| --- | --- | --- |
| Foreign keys | Enable `PRAGMA foreign_keys=ON` outside a transaction and verify it; do not rely on a default that may change. | [SQLite foreign-key support](https://www.sqlite.org/foreignkeys.html) and [PRAGMA foreign_keys](https://www.sqlite.org/pragma.html#pragma_foreign_keys) state that enforcement is per connection and historically defaults off. |
| Contention | Configure a bounded busy timeout and still handle `SQLITE_BUSY` as an explicit retry/failure outcome. | [`sqlite3_busy_timeout`](https://sqlite.org/c3ref/busy_timeout.html) describes the bounded busy handler; [SQLite transactions](https://sqlite.org/lang_transaction.html) documents the single-writer rule and `SQLITE_BUSY`. |
| Integrity | Run `PRAGMA integrity_check` and `PRAGMA foreign_key_check` in validation/backup/cutover checks; treat anything other than `ok`/zero violations as failure. | [PRAGMA integrity_check and foreign_key_check](https://www.sqlite.org/pragma.html#pragma_integrity_check) documents the checks and explicitly says `integrity_check` does not find foreign-key errors. |
| Migrations | Store ordered, reviewed migrations and apply each in a transaction with an application-owned version marker such as `PRAGMA user_version`; make reruns idempotent and refuse unknown or skipped versions. | [PRAGMA user_version](https://www.sqlite.org/pragma.html#pragma_user_version) defines the application-owned integer; [ALTER TABLE](https://sqlite.org/lang_altertable.html) documents SQLite's limited schema-change subset. |
| Backup | Provide a stable command that creates a verified snapshot before migration/cutover and records its digest/metadata without exposing its path or contents. Prefer the Online Backup API or an equivalent safe SQLite snapshot method. | [SQLite Online Backup API](https://www.sqlite.org/backup.html) describes a consistent snapshot and incremental copy semantics. |
| Export | Provide a stable command that emits versioned, deterministic JSON/JSONL for inspection/interchange, with secret-like and private machine data excluded. Export is read-only and never a live write path. | [SQLite CLI](https://sqlite.org/cli.html) distinguishes the human CLI from the library; repository audit evidence classifies JSON output as IPC/export. |

`PRAGMA integrity_check` is not a replacement for foreign-key checking. The prototype must prove both checks and must validate the command's non-zero/error behavior with synthetic corrupt or violating fixtures where safely possible.

### Journal-mode gate

WAL is conditional, not the default assumption. Before enabling it, the application must inspect the SQLite library actually loaded by the released runtime and require version **3.51.3 or newer**. SQLite's official WAL documentation identifies the WAL-reset corruption bug through 3.51.2 and its fix in 3.51.3. If the loaded library is below that floor, or cannot be established, use the default rollback-journal mode (`journal_mode=DELETE`) and report that WAL was refused.

The gate concerns the library, not a nearby executable or package label. SQLite's [run-time library version interface](https://sqlite.org/c3ref/libversion.html) returns the version of the loaded library. The [SQLite command-line documentation](https://sqlite.org/cli.html#sqlite_command_line_program_versus_the_sqlite_library) separately explains that the `sqlite3` CLI is an application that passes input to a library. The [WAL documentation](https://www.sqlite.org/wal.html) also notes that WAL requires all processes to use the same host and introduces checkpoint and `-wal`/`-shm` lifecycle concerns.

Current official release evidence: [SQLite 3.51.3 release notes](https://www2.sqlite.org/releaselog/3_51_3.html) record the WAL-reset fix, and [the current WAL-reset section](https://www.sqlite.org/wal.html#the_wal_reset_bug) states that versions through 3.51.2 are affected and 3.51.3+ is fixed. This is the source for the floor; it is not a claim that this repository currently enables WAL.

## Public interface and schema evidence

The command boundary should cover the operations currently spread over JSON/JSONL files without exposing SQL:

| Command capability | Canonical records it owns | Required properties |
| --- | --- | --- |
| agent/status and agent/transition | agent identity, current state, ordered events | stable agent/event IDs; one transaction for event plus current-state update; exact transition preconditions |
| callsign reserve/release | callsign reservation and owner | uniqueness, lease/release state, idempotent retry, no process-scan inference |
| delivery claim/ack/fail | delivery attempts and acknowledgement | stable event/recipient ID, lease expiry, bounded attempts, explicit acknowledgement distinction |
| project/request/assignment | exact project identity and future request ownership | no fabricated historical rows; foreign keys and explicit dispositions |
| resource lease/release | task-owned process/browser/resource lease | exact generation/start identity, lifecycle states, fail-closed release |
| integrity/migrate/backup/export | database maintenance and compatibility artifacts | operator-readable result, no private payloads, versioned output, no hidden live install |

The minimal prototype schema should demonstrate the relationships needed for those boundaries: a migration ledger; agents; events; callsign reservations; deliveries; projects; requests/assignments; resource leases; and export/backup metadata. Names can change during implementation, but every foreign key, uniqueness rule, lifecycle state, and public command must be documented and tested at the interface boundary. Agents must not receive a database filename as an invitation to use the SQLite CLI or SQL.

## Bounded prototype and benchmark acceptance

The prototype is decision evidence, not a production migration. It should run only against synthetic temporary fixtures and should prove:

1. Schema creation and a second idempotent migration run succeed; an unknown or skipped migration refuses to run.
2. Two synthetic writers preserve one canonical current state and an ordered event history under contention, with foreign-key violations rejected and bounded busy handling observable.
3. A backup can be opened and passes both integrity checks; export is deterministic and contains no private absolute paths or secret-like values.
4. The runtime-version gate selects WAL only for a loaded library at or above 3.51.3 and selects rollback journal for a simulated lower/unknown version. The check must inspect the binding/library used by the prototype, not only `sqlite3 --version`.
5. The stable command interface produces the same contract on success, refusal, retry, and corruption/integrity failure without requiring agents to issue SQL.

Use a small fixed synthetic workload representative of the audit (for example, repeated transitions, reservations, delivery claims, and reads across a bounded number of records). Capture elapsed time, throughput, peak RSS, CPU time, lock waits/retries, and correctness results for the existing JSON/JSONL fixture path and the SQLite prototype. Report workload size, runtime/library version, journal mode, and number of repetitions. This comparison establishes bounded feasibility and contention behavior; it does not create a performance SLA or justify a live rollout by itself.

The implemented prototype and focused results are recorded in
[`sqlite-storage-prototype-benchmark.md`](sqlite-storage-prototype-benchmark.md).
Its live connection always inspects `sqlite3.sqlite_version_info`; a separate
pure policy function supplies the simulated below-floor branch without allowing
an application connection to override its actually loaded version.

## Migration and cutover boundaries

No live installation, global hook change, or state migration is part of this decision or prototype.

Future work must keep these boundaries explicit:

- **Prepare:** pin and verify the actual released runtime/library, render an isolated install, review the versioned schema and commands, and prove source/installed parity plus a tested rollback package.
- **Snapshot:** quiesce file writers, copy/backup the complete JSON/JSONL state using an operator-approved manifest, and record counts/digests without publishing private paths or payloads.
- **Import and validate:** import only classified canonical state; preserve configuration and archives as files; reject collisions, malformed records, unknown fields that affect ownership, and integrity/foreign-key failures; make the import retryable and idempotent.
- **Cut over:** switch the stable `league` command implementation once, after parity checks. SQLite becomes canonical; JSON/JSONL is read-only export/backup. Do not run dual canonical writes.
- **Abort/rollback:** before cutover, discard only the isolated candidate database. After cutover, stop commands, preserve evidence, restore the last verified database backup or return to the pre-cutover file authority under the tested procedure, and never reconcile two independently changing authorities.
- **Aftercare:** run focused integrity, command, backup/restore, export, and runtime smoke checks. A later install/deployment authority must separately prove exact bytes and rollback; this research note is not that proof.

## Read-only local/runtime observations

Commands were run from the repository worktree with output reduced to version, module, and command facts:

| Check | Observation | Interpretation |
| --- | --- | --- |
| Source import scan | Runtime and tests import only Python standard-library modules; no SQLite adapter, ORM, or server dependency is present. | SQLite is not implemented in the current bootstrap. |
| Current CLI help | The existing watcher exposes its current lifecycle/storage-adjacent commands, but no SQLite store or WAL command. | This is pre-decision baseline evidence, not the future `league` interface. |
| Loaded CPython binding | CPython 3.14.6 reports loaded SQLite library **3.53.4** through `sqlite3.sqlite_version`. | Meets the WAL floor for this shell only; the unpinned `python3` launcher is not a release proof. |
| System `sqlite3` CLI | **3.51.0**. | Below the WAL floor and not proof of the application's loaded library. |
| Maintained alternate `sqlite3` CLI | **3.53.4**. | Meets the floor for that CLI process only; still not application-runtime proof. |
| Python package listing | Only `pip` and `wheel` were listed; no SQLite package is installed in the inspected environment. | The binding is supplied by the Python runtime, not a separately observed SQLite package. |

The distinction is material: a CLI executable version, a package/distribution version, a header/build version, and the library loaded by the application can differ. The future startup assertion must record and compare the loaded library version itself, and must fall back to rollback journal when that assertion cannot pass.

## Source list

All external technical sources used here are first-party SQLite documentation:

- [Write-Ahead Logging](https://www.sqlite.org/wal.html), including [the WAL-reset bug](https://www.sqlite.org/wal.html#the_wal_reset_bug)
- [SQLite 3.51.3 release notes](https://www2.sqlite.org/releaselog/3_51_3.html)
- [SQLite command-line shell](https://sqlite.org/cli.html)
- [Run-time library version numbers](https://sqlite.org/c3ref/libversion.html)
- [SQLite transactions](https://sqlite.org/lang_transaction.html)
- [SQLite pragmas](https://www.sqlite.org/pragma.html), including [foreign keys](https://www.sqlite.org/pragma.html#pragma_foreign_keys), [integrity checks](https://www.sqlite.org/pragma.html#pragma_integrity_check), [busy timeout](https://www.sqlite.org/pragma.html#pragma_busy_timeout), and [user version](https://www.sqlite.org/pragma.html#pragma_user_version)
- [SQLite foreign-key support](https://www.sqlite.org/foreignkeys.html)
- [SQLite Online Backup API](https://www.sqlite.org/backup.html)
- [SQLite ALTER TABLE](https://sqlite.org/lang_altertable.html)
