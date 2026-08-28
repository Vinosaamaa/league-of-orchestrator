# Bounded SQLite storage prototype and benchmark

Evidence date: 2026-08-28
Scope: synthetic temporary fixtures only; decision evidence, not a production SLA

## What the prototype proves

`prototypes/sqlite_store.py` is an isolated standard-library prototype. It is
not imported by the watcher, installed globally, or connected to live state.
Its focused test proves:

- a checksummed version-1 migration and idempotent reopen;
- refusal of a newer unknown schema version;
- per-connection foreign keys, bounded busy timeout, and full synchronous mode;
- loaded-runtime WAL selection at 3.51.3+ and a pure policy check proving
  rollback-journal fallback for a simulated 3.51.2 version, without allowing
  the live connection to override its actual loaded version;
- atomic callsign reservation and incarnation event;
- atomic event plus compare-and-swap current-state transition;
- explicit delivery claim and acknowledgement;
- exact project lookup;
- atomic task-owner transfer plus assignment receipt;
- two writers racing on the same expected version: one commit, one refusal;
- `integrity_check` plus `foreign_key_check`;
- a backup that reopens and passes both checks; and
- deterministic, bounded, explicitly non-canonical JSON export with no database
  or private absolute path.

The public interface evidence is the domain-method boundary in the prototype
and the stable `league` command table in ADR 0002. The prototype deliberately
has no general SQL command.

## Benchmark method

The benchmark compares only the operations shared by the current filesystem
contract and the SQLite prototype: one ordered lifecycle transition plus current
snapshot update, followed by current-state reads. Initialization is outside the
timed region on both sides.

- 32 synthetic agents
- 8 transitions per agent
- 1 final current-state read per agent
- 288 measured operations per repetition
- 5 fresh temporary-fixture repetitions
- filesystem side: append and `fsync` JSONL, then atomic JSON snapshot replace
- SQLite side: one short transaction per event/current update, then indexed
  current-state reads
- correctness gate on every repetition: final version parity; SQLite integrity
  `ok`; zero foreign-key violations
- elapsed wall time, process CPU time, throughput, and process peak RSS recorded

Run from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 prototypes/run_storage_benchmark.py \
  --agents 32 --transitions 8 --repetitions 5
```

## Observation

The runtime used for this observation was CPython 3.14.6 loading SQLite 3.53.4,
so the prototype selected WAL. The benchmark does not use the version of a
separate `sqlite3` CLI as runtime proof.

| Metric | JSON snapshot + JSONL fixture | SQLite prototype |
| --- | ---: | ---: |
| Median wall seconds | 0.143870 | 0.037462 |
| p95 wall seconds | 0.158827 | 0.038248 |
| Median CPU seconds | 0.111316 | 0.031458 |
| Median operations/second | 2,001.81 | 7,687.72 |
| Process peak RSS MiB | 28.47 | 28.47 |
| Snapshot/event parity | pass | pass |
| Integrity / foreign-key check | not applicable | `ok` / zero violations |

This single local observation shows that the embedded design is comfortably
feasible for a representative bounded Roster workload. The roughly 3.8x median
throughput ratio is not a universal claim: filesystem, SQLite build, journal
mode, hardware, concurrency, checkpoint policy, and workload shape can change
it. Correctness and the smaller recovery surface, not this timing ratio, drive
the decision.

The broader transactional operations—callsign reservation, delivery
acknowledgement, project lookup, and atomic owner transfer—are exercised in the
focused prototype test but are not assigned a misleading JSON timing baseline;
the current filesystem model does not provide one equivalent cross-file
transaction for those operations.

## Limits and next proof

- Synthetic state only; no live Roster, hooks, multiplexer, repository
  worktree, or installed runtime is touched.
- Two connections prove same-version contention behavior, not a high-contention
  scaling ceiling.
- WAL checkpoint growth, long readers, disk-full behavior, corrupt-database
  recovery, multi-process crash injection, and backup/restore across a released
  install remain future implementation/cutover tests.
- The exact released runtime must repeat the loaded-library check. A runtime
  below 3.51.3 or with unknown binding must use rollback journal.
- No benchmark result authorizes installation or migration.
