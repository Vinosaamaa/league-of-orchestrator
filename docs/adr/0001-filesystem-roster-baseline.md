# ADR 0001: Retain the filesystem Roster for bootstrap

- Status: Accepted for the active issue-#2 baseline; ADR 0002 applies only after
  a separately authorized cutover
- Date: 2026-08-26
- Supersession owner: issue #6

## Context

The proven installed watcher uses one JSON snapshot and one append-only JSONL
event log per visible agent, plus a separate watcher state file. Issue #1 names
embedded SQLite as a candidate for later transactional ownership and handoff,
but explicitly does not authorize a live migration. Issue
[#18](https://github.com/Vinosaamaa/league-of-orchestrator/issues/18) must first
inventory every JSON/JSONL producer and consumer that a storage change could
affect.

## Decision

Import and test the current filesystem contract unchanged for bootstrap:

- `status.json` is the current readable snapshot.
- `updates.jsonl` is the append-only lifecycle history.
- The latest event must exactly match snapshot status, timestamp, and update.
- Owner writes use the record lock and atomic snapshot replacement.
- Watcher delivery state remains separate and schema-versioned.
- No live Roster files are copied into this repository.
- No SQLite database, migration, or WAL configuration is introduced.

The JSON Schemas in `schema/` are authoring aids. The runtime validator remains
authoritative because it also rejects duplicate keys, enforces record-path
identity, and validates latest-event parity.

Issue #6 has selected SQLite for a future canonical store in
[ADR 0002](0002-sqlite-canonical-store.md). The application runtime's loaded
SQLite library must be version 3.51.3 or newer before WAL is enabled. The upstream
[WAL-reset documentation](https://www.sqlite.org/wal.html#the_wal_reset_bug)
records a rare corruption bug through 3.51.2 and its fix in 3.51.3. A system or
Homebrew `sqlite3` CLI version is not a substitute for checking the library that
the application binding actually loads.

## Consequences

The bootstrap preserves installed behavior and has no migration side effects.
It does not solve atomic multi-agent ownership transfer. The filesystem remains
the active runtime contract until a later issue implements and verifies the
single canonical cutover in ADR 0002, using issue #18 as dependency evidence.
