# Deterministic League activity reports

> **Status: repository-local contract for issue #22.** Reports read only an
> explicit migrated League state root. They do not inspect harness transcripts,
> Roster files, multiplexer panes, repositories, or installed agent state.

## Stable source and derived renderers

`league.report.v1` JSON is the sole report source. Markdown and portable HTML
are deterministic renderers over that exact object; neither renderer queries
storage or invents evidence. The source-managed HTML template has no scripts,
remote fonts, images, or runtime assets. Its completion seal and evidence
ledger follow the repository's Project Ledger visual language.

Each report records its exact inclusive or exclusive `from`, inclusive `to`,
IANA timezone, owner/Squad/project/all scope, canonical event watermark,
source-table watermark, immutable specification hash, and content hash. A
stored report specification does not copy source evidence. `report show`
re-runs the stored specification and says whether the observed content hash is
identical; a changed historical source is an explicit unverified reproduction
gap, never a silently rewritten report.

The opaque report identity binds both the specification and content hashes.
Repeating unchanged input is idempotent; a historical source repair produces a
new immutable report identity even when a coarse table watermark is unchanged.

## Public commands

All examples use placeholders and one explicit isolated state root.

```sh
./bin/league --state-root <state-root> report \
  --from 2026-08-28T00:00:00-07:00 \
  --to 2026-08-28T23:59:59-07:00 \
  --timezone America/Los_Angeles --owner <callsign> --format json

./bin/league --state-root <state-root> report \
  --today --timezone America/Los_Angeles --squad squad:example --format markdown

./bin/league --state-root <state-root> report \
  --since-report report:example --to 2026-08-29T12:00:00-07:00 --format json

./bin/league --state-root <state-root> report show report:example --format html
```

`--since-report` starts strictly after the stored report's `to` value and
inherits its timezone and scope unless the caller explicitly selects another
exact scope. `--today` resolves local midnight and the observed upper bound in
the named timezone, then writes those resolved values into the report.

The default output is outbound-safe. `--local-diagnostic` may include local
task and prompt summaries on local standard output, but the remote-adapter
boundary categorically refuses that mode. The repository-owned
`skills/league-report/SKILL.md` invokes these public commands only. It contains
no SQL or transcript parsing and opens HTML in a new Agent Chrome tab only
after an explicit visual request.

## Evidence and completion

Canonical prompt items, requests, direct or delegated dispatch, tasks,
Champion assignments, callsign reserve/activate/release history, model/effort
routing, harness/backend bindings, guarded rollover operations, canonical
`owner_changed` events, transitions, resources, cleanup, and teardown receipts
are read from indexed League tables. Rollover facts expose only opaque IDs and
recorded hashes; they never copy the local handoff plan or event detail JSON.
`league evidence record --input <json>` adds bounded immutable
activity evidence for issue, commit, pull request, check, merge, install,
deployment, smoke, rollback, authority, handoff, continuation, teardown, and
recurring-repair facts. Exact canonical JSON local evidence stays local with an
opaque reference and verified SHA-256; outbound reports include only the safe
summary, public URL when explicitly approved, hashes, verification state, and
presence signal. Local-diagnostic reports may show the full local value.

Recurring repair facts carry one stable repair ID and phase, so repeated
failure/attempt/fix/final records group without hiding the underlying facts.
Unknown or unverified fields are explicit gaps.

`everything_finished` is true only when all scoped gates are settled:

- requests and tasks;
- assignments and ready-to-land work;
- delivery and required release/authority evidence;
- prepared, acknowledged, or switched handoffs not yet completed or aborted;
- verification obligations and evidence gaps;
- active resources and cleanup obligations.

A terminal task status is insufficient by itself: the matching transition and
all other gates must also be proved.

## Bounds and performance contract

A report scans at most 100,000 facts, returns at most 1,000 facts per page,
binds an opaque cursor to the exact specification hash, and caps gaps and
repair groups. Source timestamp/scope indexes support streaming merge and
bounded pagination. Focused synthetic budgets are 500 ms for a 2,000-fact
typical day and 3,000 ms for a 50,000-fact history on the repository test
runtime. The test refuses a regression instead of weakening either budget.

The JSON, activity-evidence, and outbound-receipt contracts are
[`league-report.schema.json`](../schema/league-report.schema.json),
[`league-activity-evidence.schema.json`](../schema/league-activity-evidence.schema.json),
and [`league-outbound-receipt.schema.json`](../schema/league-outbound-receipt.schema.json).
