# Source provenance

## Verified source snapshot

- Repository: https://github.com/Vinosaamaa/terminal-environment-toolkit
- Source revision: `93635786746b1d2bea21cca7d276e2106aa99fb5`
- Revision subject: `Fix teardown proof and atomic Champion launch (#41)`
- Verification date: 2026-08-27

At refresh time, the toolkit checkout and its local `origin/main` were at that
revision. Its watcher runtime, launcher, orchestration reference, and shared
guide were byte-equal to the installed copies. No installed-state mutation was
used for that comparison.

## Imported source map

| Candidate path | Toolkit source path | Source SHA-256 | Adaptation |
| --- | --- | --- | --- |
| `src/agent_watcher.py` | `agent_watcher.py` | `ab77f3f0d97cb1b09e34c005c45e90b94a8c0e0612f115169bc62827e524e0d7` | None; byte-for-byte import. |
| `bin/agent-watcher` | `agent-watcher` | `3a049f5315e131bda3deed22cbd18f5b14f7d54b2821fcb8dddbda3d9fbba034` | Launch target changed to `src/agent_watcher.py`. |
| `config/agent-routing.example.json` | `global-agent-instructions/agent-routing.example.json` | `2d475dd727526336b6635d4cf7b9af14c7e2497456ebdff3117ae4adcea3bbdb` | None. |
| `tests/test_agent_watcher.py` | `shell-completions/test-agent-watcher.py` | `203540768c12be053871cf287f5c7a32cf28319eac9be0f2e0d38f634fabad70` | CLI path, description, synthetic identity, and test-only subprocess ceilings. |
| `tests/test_lifecycle.py` | `shell-completions/test-agent-lifecycle.py` | `3bf652906b86f6eb164a43a80625587d6f0f68d35dd3033fe7d67b92251dc780` | CLI path plus synthetic launch identity, URLs, endpoints, and secret-rejection sentinels. |
| `tests/test_delivery.py` | `shell-completions/test-agent-delivery.py` | `9c321d6410455b4246f38415bd4934d2e1d6f9ab6c67db5c76d4995f01763e9c` | CLI path, description, synthetic identity, and test-only subprocess ceiling. |
| `tests/test_record_contract.py` | `shell-completions/test-agent-record-contract.py` | `bcca213715b8262d9567298d9848540a78917d80b4a559f29b01ab2bef62895a` | Paths, synthetic runtime/repository identity, and test-only subprocess ceiling. |
| `tests/test_reconciliation.py` | `shell-completions/test-agent-reconciliation.py` | `eb487c3f748b33c5f851af726077defcf813842d07e866ff72c1f470a88d1bcb` | CLI/module paths, description, synthetic identity, and test-only subprocess ceiling. |

The wrapper fix originated at `dfd76b8e043dbd14450d07d497f4b87defbde5ef`.
The lifecycle, routing, model, and teardown baseline originated at
`44bcedf25a4e11fc3822cc0fe11275f73ae9614f`. The released source snapshot above
also includes atomic Champion launch/preflight, paired routing/display identity,
local-install byte-parity proof, and later-main squash proof.

## Deliberately retained sources

These toolkit files were inventoried but not copied:

| Toolkit path | SHA-256 | Reason |
| --- | --- | --- |
| `install-agent-watcher` | `f051a516fbc4000bfbc1ee71b4cb9afd2e10dcbadcf994d78960da2518ca4495` | Live install and rollback remain toolkit-owned. |
| `shell-completions/_agent-watcher` | `667d96d44c06fda95096a768c64d89790bafbe43c81124ca46511473d1592b2e` | Completion moves with a later installation slice. |
| `AGENT_ORCHESTRATION_REFERENCE.md` | `ec2eff727ac52d23434d92c1f07ee7806206b4574fb7e33dbd3bf30040baccd4` | Installed global policy is not silently replaced. |
| `global-agent-instructions/shared-AGENTS.md` | `e1dff2fcb7fac2afe4704584221d5fb19a25b21c887c2cf48792ad69804f4c35` | Global guide ownership changes require a release gate. |

The example and schema files in this repository are new, synthetic authoring
artifacts derived from the runtime validator rather than copies of live Roster
records. They document the optional paired `routing_name` and `display_agent`
fields; runtime validation additionally requires the routing name to equal the
lowercase callsign.

## Storage-decision evidence refresh

Issue #18 refreshed the read-only owner-source and installed-contract audit on
2026-08-28. The current toolkit source revision inspected was
`51cfad445843c3f2cab7884f3ddff0a3d8a67d77`; its installed runtime bytes matched
at inspection time. Since the bootstrap revision above, the owner source added
recoverable launch/resume state, including pending-launch JSON, immutable failed
launch receipts, launch locking, task-bound pending callsign reservations, and
exact runtime-generation binding.

Those later runtime changes are inventoried in
`docs/research/json-jsonl-state-dependency-audit.md` but are deliberately not
folded into the baseline watcher.

## Issue-#19 implementation provenance

`src/league/`, `bin/league`, the `league-*.schema.json` contracts, and the
focused `test_sqlite_storage_*` suite are original League implementation for
issue #19. The composite protocol, shared SQLite transaction core, focused
operation modules, and single facade implement the accepted ADR and complete
dependency audit using only Python's standard-library `sqlite3` binding. The earlier
`prototypes/sqlite_store.py` remains decision evidence: it informed the proved
operations and safety gates but is not imported, copied as the production
module, installed, or connected to live state.

The implementation deliberately leaves `src/agent_watcher.py`,
`bin/agent-watcher`, global hooks, installed files, live Roster/callsign/watcher
state, and immutable archives unchanged. Issue #23 owns staged acceptance and
any later reversible cutover.

## Issue-#23 acceptance provenance

`src/league/acceptance.py`, `schema/league-acceptance-receipt.schema.json`,
`docs/ACCEPTANCE.md`, and `tests/test_acceptance_harness.py` are original League
implementation for issue #23. `VERSION` and the CLI extension are also original
League work. The canonical behavior, safety boundaries, and verification
contract live in [`docs/ACCEPTANCE.md`](ACCEPTANCE.md); this file records source
origin and ownership only.

The issue-#23 continuation adds `src/league/precutover.py`, the strict
pre-cutover plan/receipt schemas, and `tests/test_pre_cutover.py` as original
League work. Its deliberate provenance difference is adding the already
imported `bin/agent-watcher` and `src/agent_watcher.py` to the staged manifest;
the behavior and verification contract remain canonical in
[`docs/ACCEPTANCE.md`](ACCEPTANCE.md).

The issue-#23 legacy-pair successor adds one original League-only exception to
the no-apply pre-cutover snapshot: an exact-hash, single-pair Shotcaller
initialization reconciliation. The general legacy importer remains unchanged
and fail-closed. Focused synthetic coverage proves that the exception rewrites
only the temporary snapshot, emits a create-once receipt, and rejects every
unbound or post-initialization use; its canonical contract remains in
[`docs/ACCEPTANCE.md`](ACCEPTANCE.md).

The issue-#23 archived-cursor successor adds original League migration-only
classification for exact hash-bound watcher cursors whose Roster source is
already archived or non-active. The deliberate difference from the filesystem
baseline is that retained archive evidence is represented in restricted
watcher metadata while no agent, event, cursor, seen row, or delivery is
invented. Unclassified or ambiguous consumers remain fail-closed; the
canonical contract remains in [`docs/ACCEPTANCE.md`](ACCEPTANCE.md).

The issue-#23 pending-launch successor adds original League migration-only
aliases for the retained `created_at`, `resume_thread_id`, and `task` fields.
The deliberate difference is limited to exact, conflict-checked normalization
into the existing start-time, resume-thread, and task-summary model; original
artifact bytes remain authoritative and ordinary parsing stays fail-closed.
The canonical contract remains in [`docs/ACCEPTANCE.md`](ACCEPTANCE.md).

The real cleanup canary, its strict adapter/receipt schemas, and focused fake
adapter tests are original League work for issue #23. The issue-#40 artifact
lifecycle and issue-#39/PR-#41 report receipts are composed inputs, not
reimplemented here; the cleanup-canary contract and runtime scope remain
canonical in [`docs/ACCEPTANCE.md`](ACCEPTANCE.md).

The issue-#23 final cutover successor removes an idle-only Herdr readiness wait
that was incompatible with settled background-agent states, while preserving
the same bounded prompt and exact cleanup identity. It also makes the complete
pre-cutover legacy system an immutable, hash-verifiable inactive archive with a
co-located restoration runbook; it does not reactivate or rewrite legacy data.

The issue-#23 post-cutover compatibility successor keeps the stable watcher
name while routing Stop and bounded supervision through the canonical SQLite
store. It counts preserved Shotcaller-to-Champion ownership from imported agent
instances and leaves every legacy mutation command fenced.

The same successor binds complete Codex and Cursor prompt-hook payloads to one
verified runtime and deterministic source identity, then wakes supervision in
the intake transaction. Semantic triage remains a model decision expressed
through the already-merged request commands. Transition delivery uses the
canonical outbox and recipient receipt; no transcript backfill or legacy state
writer is introduced.

The issue-#23 real-Codex Stop successor deliberately changes only canonical
Codex Stop compatibility: its one-shot anti-loop key is the exact stable
session/turn pair from Codex's real payload, rather than a hash of mutable
payload fields or prompt-intake generation alone. Cursor and the public storage
Stop command retain their existing wait-generation behavior. Focused installed-
shape coverage proves first-block, same-turn allow, new-turn reblock, malformed
payload refusal, and unchanged prompt-quarantine/user-priority semantics.

The issue-#23 installed-routing successor derives the canonical state root from
the exact writer pointer when no explicit test root is supplied and resolves a
hook session through its one live verified runtime generation before consulting
the imported legacy thread identity. Runtime ambiguity remains a refusal.
Non-Shotcaller prompts are retained once in quarantine without a wake actor or
watcher generation, while SQLite supervision accepts only one verified runtime
owned by an active Shotcaller. No legacy file, transcript, or second writer is
read or changed by this compatibility path.

Tests that require process inspection explicitly inject the single
`tests/fakes/ps` adapter through `tests/process_adapter.py`; Make targets do not
alter `PATH` for unrelated tests. This keeps self-process and resource-lifecycle
contracts testable in restricted CI and agent sandboxes that deny host process
inspection. The adapter is test-only; production process inspection and
behavior are unchanged.

## Grouped request-lifecycle implementation provenance

The issue-#21 request-lifecycle design merged at `ff72125` is the canonical
invariant source for this grouped #3/#4/#5/#17 slice. The third reviewed SQLite
migration, request/assignment/outbox/watcher operation modules, injected
adapter services, CLI families, schemas, and focused tests are original League
implementation. They extend the merged issue-#19 repository-local facade and
do not modify `src/agent_watcher.py`, installed files, global hooks, live
Roster/callsign/watcher state, or canonical authority. Issue #23 retains all
installation, live import, real-runtime canary, reversible cutover, and rollback
ownership.

The grouped implementation was squash-merged from PR #31 as
`fa2c5f862c5bd223057a6b9b34f5b11607a747be`. Its schema migrations 1, 2, and 3
remain byte-for-byte canonical in this descendant; the runtime slice does not
renumber or rewrite their names, statements, or checksums.

## Runtime-lifecycle implementation provenance

The issue-#7/#11/#14 adapter, runtime, cleanup, resource, routing, storage, CLI,
documentation, and focused-test modules are original League implementation.
They extend the canonical request-lifecycle store with contiguous migration v4,
`adapter-runtime-cleanup-and-routing`, checksum
`01892d93311ce0b5486077b00e6d3adea60fd3c91006663358317260ad21cd2d`.
Migration v4 evolves v3's existing one-per-task cleanup obligation and adds
opaque runtime bindings, typed resources, cleanup operations/actions/receipts,
and durable routing evidence; it does not duplicate request claims,
assignments, delivery outbox, watcher, or Stop state machines.

Codex+Herdr and Codex+tmux are named adapter contracts. Pi and all destructive
cleanup adapters in the suite are deterministic isolated doubles, not
real-runtime evidence. Installed drivers, a genuine isolated canary, global
installation, live migration, cutover, and rollback remain issue-#23 gates.

## Skill-contract implementation provenance

`src/league/skill_contracts.py`, the `league skill` CLI family, skill JSON
schemas/config, `docs/skill-capabilities.md`, the sanitized audit receipt, and
`tests/test_skill_contracts.py` are original League implementation for issue
#10. The current custom-root audit read only direct custom-skill entries and a
separate existing lockfile's public source-owner identifiers. It did not copy
skill bodies, symlink targets, machine paths, runtime identity, credentials, or
private endpoints into repository bytes.

Ten skills have one recorded public source owner from that lock record. The
remaining thirteen are explicitly `unrecorded`; all versions and source-byte
parity remain unrecorded/unverified where no authoritative versioned source
bytes were proved. Per-copy hashes use League's documented deterministic tree
hash, while duplicate parity compares those declared copy hashes. No installed
skill, global config, hook, adapter, or runtime was modified.

## Project catalog and terminal Roster design provenance

The issue-#9 advisory catalog, project-grouped snapshot, stable project/Roster
commands and schemas, synthetic tests, issue-#12 Markdown design, and matching
HTML review artifact are original League implementation. They extend only the
canonical repository-local storage facade. They do not modify the filesystem
watcher, install files, read live Roster records, call live runtime adapters,
change task/request ownership, copy project instructions, or implement an
interactive TUI/controller.

Migration v5 is contiguous and named
`advisory-project-catalog-and-roster-indexes`, checksum
`5477db9879d6a4a9a29bb8188b398bd6db9a7a786e40e86ab819a0a938790faf`.
It adds project fields, aliases, ordered suggested Squads, and bounded Roster
lookup indexes without rewriting migrations 1 through 4. Exact repository and
root data is local-only; outbound catalog/Roster reads and inspection exports
redact it. All tests use isolated synthetic state roots and fake identities.

## Guarded rollover and callsign queue provenance

The issue-#8/#13 storage operations, command families, plan schema, synthetic
tests, and `docs/HANDOFF_CALLSIGNS.md` are original League implementation. They
implement the accepted continuation and callsign policies only inside an
explicit repository-local SQLite root. They do not read or mutate the global
Roster, installed watcher, live callsign files, real terminal/runtime state,
repository worktrees, or cutover generation.

Migration v6 is contiguous after canonical project/Squad v5 and is named
`guarded-rollover-and-shuffled-callsign-queue`, checksum
`879ef4addfe6725e31c31a5aa1db9078d7c066a26610eaa2753f749c6e53ab75`.
It deliberately replaces the old public exact-name callsign reserve/release
commands with one persisted queue allocator. Focused queue tests cover the
behavioral difference: front scanning, capability skips without reordering,
exact rollback, tail release, sole-compatible reuse, immutable history, and
concurrent/crash retry. Focused rollover tests cover the bounded snapshot,
exact acknowledgement, owner CAS, single event/outbox, intake fencing,
pre-switch abort, post-switch drain, and unchanged Champion bindings.

## Reporting and privacy implementation provenance

The issue-#22/#25 reporting, privacy, guarded remote adapter, report renderer,
HTML template, report skill, source-managed shared instruction, explicit-root
guidance adapter, schemas, documentation, public-safety gate, and focused tests
are original League implementation. No harness transcript, live Roster,
multiplexer, browser profile, global instruction, installed file, remote
transport, deployment, or personal/application record was read or modified to
author them. Visual language is adapted from this repository's source-managed
Project Ledger artifact; no asset or runtime dependency is copied from it.

The candidate preserves exact local roots, repositories, and evidence only in
the canonical local store. Outbound projections contain bounded summaries,
approved public URLs, opaque League IDs, hashes, and explicit placeholders.
The report skill uses only public `league report` commands and the cross-harness
guide remains an uninstalled source input owned for later staging by issue #23.

This branch was rebased onto canonical main
`f015a5c34efca039accc911f8995a340eb067fc7`, whose merge tree is
`8e18b33caca431b12a462da31610abdf5af318a1`, before assigning contiguous
migration v7. Migration v7 is named
`bounded-reporting-and-outbound-privacy`, checksum
`bebe90eb841eac2a0b42d3f89e321cb4f3f8b23b02d92febf5a4ea2a50727cde`;
canonical v6 remains byte-for-byte unchanged.

The v7 import-plan contract now binds its target schema version and returns a
dedicated compatibility refusal for retained pre-v7 plans; this changes only
the deterministic dry-run report digest, not imported canonical rows or the v7
migration checksum.

## Routing-policy and requester-progress provenance

The issue-#36 orchestration/model policy, Squad registration commands, hidden
scientist assignment role, parent-request progress projection, decision corpus,
research report, and synthetic tests are original League implementation. They
extend the merged #4 assignment/request state machine and #14 model-routing
records; they add no learned router, planner, scheduler, Lead hierarchy,
installed writer, live adapter, migration, or cutover.
The new provider-policy example is `config/league-model-routing.example.json`;
the imported `config/agent-routing.example.json` remains unchanged for the
filesystem watcher contract.

Migration v8 is contiguous after the reporting/privacy v7 and is named
`bounded-routing-policy-and-request-progress`, checksum
`593e2cf05d0200463800b6be7cbf5918a9b5fc3304f793d2ec3fad30b538e80c`.
It deliberately separates stable Squad ownership from current Shotcaller
runtime, owner routing from execution mode, hidden-worker validation from
Champion validation, and child delivery from requester progress. Focused tests
cover exact route precedence/tie refusal, registration acceptance atomicity,
direct bounds, hidden persistence/promotion/stale-runtime fencing, model
override/downgrade/escalation, and immediate/coalesced/overdue progress.
The v8 target-version binding changes the deterministic acceptance fixture's
dry-run report digest only; its source and imported-row parity digests remain
unchanged.
The review follow-up groups dispatch policy inputs in the immutable
`OrchestrationSignals` value object without changing their stored record. A
runtime observation may now replace its declared capability list explicitly;
omitting capabilities preserves the prior list, and focused routing tests cover
the supported update and duplicate refusal.
The completed review also shares one bounded read-only safety predicate between
direct and hidden decisions, strictly rejects non-boolean JSON signals, requires
one live runtime to satisfy the complete Squad capability set, and carries a
pending routed owner across Shotcaller rollover. Focused routing, progress,
hidden-promotion, and rollover tests cover those boundaries. Existing migration
index recreation, progress-generation uniqueness, committed offer expiry, hidden
Roster exclusion, and immediate stale-owner escalation were retained after their
focused tests disproved the reported regressions.

## Repository-artifact publication provenance

Issue #40 adds only repository-local durable declarations, merged-publication
receipts, stable artifact commands, and a cleanup refusal for unresolved
publication. Migration v9 is contiguous and named
`repository-owned-artifact-publication`, checksum
`9231da781de45a8e912cd7193034a0b1b56f3a13e5e737e5681f18f6c6e3c852`.
The deliberate baseline difference is that a declared repository artifact now
blocks cleanup until its pull request, tested head, and merge receipt are
recorded. Focused synthetic CLI coverage proves the refusal and release; no
live repository, runtime, installation, cutover, or teardown is exercised.

## Production cleanup successor provenance

The issue-#11 successor keeps the PR-#30 policy and action tables intact and
adds one stable `cleanup execute` path over the canonical `Storage` facade. The
deliberate baseline differences are that an exact task-owned shared lease can
now be released without touching its shared resource, persistent resources are
retained without an action, completed actions are reverified on resume, and a
non-retryable execution failure records a blocked operation/obligation plus one
immutable final receipt. An exact switched rollover predecessor is the only
Shotcaller cleanup exception; its drain receipt is derived from the completed
action receipts.

Focused deterministic tests and one synthetic crash-resume cleanup E2E use
only explicit temporary roots and fake Herdr/process state. This successor does
not read or mutate the installed watcher, live SQLite state, a real terminal,
the global resource registry, an active callsign, or any user worktree, and it
performs no install, cutover, live teardown, or merge.
