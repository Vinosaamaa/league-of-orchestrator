# ADR 0002: Use one embedded SQLite canonical store

- Status: Accepted for future implementation
- Date: 2026-08-28
- Decision authority: Summoner acceptance for issues [#6](https://github.com/Vinosaamaa/league-of-orchestrator/issues/6) and [#18](https://github.com/Vinosaamaa/league-of-orchestrator/issues/18)
- Implementation and migration authority: Not granted by this ADR
- Supersedes: ADR 0001 only after a separately authorized, verified cutover

## Context

The current filesystem Roster preserves one JSON snapshot and one append-only
JSONL event file per visible agent. That baseline is inspectable and proven for
single-record lifecycle updates, but coordination now crosses records and file
families: callsign reservation, recoverable launch, watcher delivery,
acknowledgement, project/task ownership, request assignment, and resource
leases.

The complete [producer-consumer audit](../research/json-jsonl-state-dependency-audit.md)
shows that these operations cannot be made one transaction by extending the
current collection of independently locked files. It also identifies important
files that are configuration or evidence rather than database state.

The [primary-source research](../research/sqlite-storage-decision-evidence.md)
and [bounded prototype/benchmark](../research/sqlite-storage-prototype-benchmark.md)
provide the decision evidence. The prototype is isolated and synthetic; the
production watcher remains unchanged.

## Decision

Use one local embedded SQLite database as the sole canonical store for
transactional League coordination state after a separately authorized cutover.

- SQLite is an application library and local file, not a server.
- Use the Python standard-library binding in the bounded prototype; introduce no
  ORM or database service.
- Agents operate stable `league` commands and never execute SQL. Database paths,
  SQL, pragmas, transactions, and retry behavior are internal details.
- Store canonical agent/task identity, current state, ordered events, callsign
  leases, launch attempts, delivery state, exact project identity, requests and
  assignment receipts, watcher coordination, and resource leases.
- Keep hooks, routing tiers, optional Lead preference, guides, launchers,
  completions, and skill-manager state as files.
- Keep immutable teardown/failed-launch receipts, archives, rollback backups,
  and bounded evidence as files, with optional database hashes/pointers.
- JSON/JSONL becomes deterministic versioned export/backup only after cutover.
  There is no permanent dual canonical write path.

ADR 0001 remains the active runtime decision until one canonical cutover is
separately authorized, implemented, and verified.

## Connection and maintenance contract

Every application connection must:

1. enable and verify `PRAGMA foreign_keys=ON` outside a transaction;
2. configure a bounded busy timeout and return an explicit retry/refusal when
   contention exceeds it;
3. apply an explicit synchronous policy;
4. run only reviewed, checksummed, ordered migrations and refuse gaps, drift,
   and newer unknown schema versions;
5. support both `integrity_check` and `foreign_key_check` because neither
   substitutes for the other;
6. create verified backups through SQLite's backup API or an equivalent correct
   quiescent procedure; and
7. emit deterministic, bounded, redacted, explicitly non-canonical exports.

### Journal-mode safety gate

WAL is allowed only when the SQLite library actually loaded by the released
League runtime reports version **3.51.3 or newer**. The gate does not accept the
version of a nearby CLI, package label, header, or build tool as a substitute.

If the loaded version is older, missing, or unverifiable, the runtime selects
rollback journal (`DELETE`) and reports that WAL was refused. The current
read-only shell observation loaded SQLite 3.53.4 and therefore let the
prototype use WAL; a separate system CLI reported 3.51.0. This is decision
evidence, not future release authorization.

## Stable interface

The first production implementation may add commands only for a proved domain
operation. It must not expose generic query or SQL execution.

| Stable capability | Internal transaction boundary |
| --- | --- |
| `league agent status` / `transition` | Read current incarnation; insert event and compare-and-swap current state together |
| `league callsign reserve` / `release` | Unique callsign lease, incarnation state, and event together |
| `league delivery claim` / `ack` / `fail` | Stable event/recipient identity, bounded claim, attempts, and explicit acknowledgement |
| `league project resolve` | Exact canonical repository/project identity |
| `league task transfer-owner` | Task/Squad-owner version, event, and assignment receipt together |
| `league request ...` | Future request/claim lifecycle with opaque token and expiry |
| `league resource ...` | Exact lease generation/start identity and fail-closed release |
| `league storage integrity` / `migrate` / `backup` / `export` | Versioned operator result with no second writable authority |

Harness/backend logic stays outside the store. An adapter supplies or consumes
bounded locators and receipts; it does not add Herdr, tmux, Codex, Cursor, or Pi
business rules to the schema.

## Alternatives

| Option | Correctness | Complexity | Latency | Recovery/inspection | Migration and replacement |
| --- | --- | --- | --- | --- | --- |
| Keep JSON snapshot plus JSONL and add more locks | Cannot make cross-file reservation, ownership, delivery, and resource changes one transaction; path/offset event identity remains fragile | Low initially, but grows a bespoke recovery journal and lock order | Fast for isolated reads/writes | Human-readable, but crash windows and global reconciliation remain manual | No cutover now, but every new domain deepens later migration |
| One embedded SQLite file | Short ACID transactions, foreign keys, uniqueness, stable IDs, compare-and-swap versions, and atomic owner transfer | Small standard-library adapter plus migrations/backup/export; no service | Prototype showed ample bounded local feasibility; one writer means contention still requires bounds | Integrity/FK checks, backup API, deterministic export, and explicit recovery state | One staged import/cutover; stable commands and exports support later replacement |
| Database server | Strong transactions but adds process, authentication, deployment, and availability concerns without a local workload need | Highest operational surface | Network/process overhead and new failure modes | Mature tools, disproportionate ownership burden | Unnecessary service migration |
| ORM over SQLite | Same database properties, but hides SQL/pragmas/migration details that are central to this small storage contract | Adds dependency and abstraction surface | No decision-relevant benefit | Can complicate exact schema and failure evidence | Harder to keep the interface minimal and portable |

SQLite is accepted because it is the smallest option that supplies the missing
cross-record transaction and query semantics without a service or ORM.

## Prototype evidence

The bounded prototype implements migrations, runtime journal selection,
foreign keys, busy timeout, synchronous policy, callsign reservation,
event-plus-current transition, delivery acknowledgement, project lookup,
atomic task-owner transfer with receipt, integrity checks, verified backup, and
deterministic export.

Focused tests also run two writers against the same expected agent version:
exactly one transition commits and the stale writer is refused without an extra
event. The 32-agent, 8-transition, 5-repetition benchmark completed 288
overlapping transition/read operations per repetition with exact parity and
clean integrity/foreign-key checks. On this observation SQLite's median was
0.037462 seconds versus 0.143870 seconds for the synthetic filesystem pair.
This is feasibility evidence, not a production SLA or universal performance
claim.

## Migration and rollback boundary

This decision defines, but does not perform, the future sequence:

1. freeze versioned schema/commands/runtime and prove exact release/install and
   rollback packages;
2. explicitly quiesce all writers and create a restricted immutable backup from
   the audited manifest;
3. dry-run a strict idempotent import into a temporary database;
4. prove storage-neutral behavior parity, integrity, foreign keys, recovery,
   backup/restore, and deterministic exports with fake adapters;
5. revalidate source hashes and atomically switch the stable `league` command to
   one canonical store; and
6. preserve legacy files read-only while SQLite becomes the only writer.

Before the first database-canonical write, rollback may restore the validated
legacy backup. Afterwards, rollback requires a tested down-migration from the
database into a newly staged legacy tree; stale JSON is never restored over
newer database state. Lossy down-migration blocks rollback and requires forward
repair.

This ADR and prototype perform no live install, global hook change, state
migration, database placement, release, or teardown. Issue
[#21](https://github.com/Vinosaamaa/league-of-orchestrator/issues/21) may produce
the broader design narrative without reopening this minimal accepted choice or
crossing these authority boundaries.

## Consequences

- Cross-record invariants gain one transactional boundary and stable identity.
- The store remains a deep internal module behind small commands.
- SQLite's one-writer model is accepted for the bounded local workload, with
  explicit busy timeout, short transactions, and measured contention.
- WAL remains conditional on the exact loaded runtime; rollback journal is a
  supported fallback, not an error state.
- Public inspection remains available through bounded redacted exports, while
  configuration and immutable evidence retain their proper file ownership.
- Live behavior remains the ADR-0001 filesystem baseline until later authority
  and verification complete one reversible cutover.
