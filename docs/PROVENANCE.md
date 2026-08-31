# Source provenance

## Schema-16 release compatibility repair

Issue #90 restores the already-canonical schema-16 migration omitted from the
merged 0.2.28 release candidate. The exact migration source comes from public
commit `c0bc88412a5be2db66030f43f5fe9e35c0d77877`; the source file SHA-256 is
`5fbe8039100354ac8c7ad4a3b0add87ed41b5e4b9c01fc86678d404146637d45`.
Migration v16 remains named
`issue-coupled-cleanup-and-exact-thread-continuation` with checksum
`a7fee02de43dbbde897b67e44c00e37805bf82790917d2f5392be70e4143ef3f`.

This compatibility repair copies only the migration definition, migration
ledger entry, release-receipt schema declarations, and rollback coverage. It
does not import continuation runtime operations or duplicate the semantic
request-reconciliation and supervisor changes under review in PR #94.

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
| `global-agent-instructions/shared-AGENTS.md` | `67a1fec8e2e341ee55ae5936b5c402167d6fcf94c6d0154ad0da06d516d59a69` | Universal guide remains terminal-environment-toolkit-owned and is never copied, packaged, installed, restored, or rolled back by League. Toolkit issue #45 owns its reconciliation. |

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
Non-Shotcaller prompts bind once to their exact verified runtime without a wake
actor or watcher generation; missing or unverified runtime identity remains a
no-wake quarantine. An exact thread/backend/address match may bootstrap only a
`prompt.capture` runtime for that Champion; it grants no Stop or supervision
capability. SQLite supervision accepts only one verified runtime owned
by an active Shotcaller. No legacy file, transcript, or second writer is read
or changed by this compatibility path.

The queued-prompt successor changes only hook source identity: it adds the
exact prompt body hash to adapter, session, and raw event identity because one
Codex turn can submit multiple queued user messages. Exact retries remain one
prompt; distinct bodies remain distinct prompts. A stale owner/source collision
is quarantined without wake instead of escaping the hook as an ordinary-input
failure.

The Stop-contention successor deliberately changes only canonical hook
contention behavior. An `agent.transition` transaction owns SQLite's single
writer reservation until its event, state, and delivery outbox commit together;
a concurrent Stop must also reserve the writer to persist its one-shot guard.
Stop now waits a bounded 250 milliseconds and converts only a retryable SQLite
busy refusal into a normal fail-closed continuation without consuming the
terminal generation. Prompt intake keeps its exact-once transaction and uses a
separate bounded one-second wait, so it is neither silently stripped nor routed
through a second store. All non-busy refusals and public storage commands retain
their prior behavior.

The journal-mode contention successor changes the connection-policy boundary,
not the timeout. Migration connections remain the only path that may establish
or change SQLite journal mode under maintenance. Normal connections query the
existing mode, accept only DELETE or WAL, and refuse WAL when the loaded runtime
is older than 3.51.3. The canonical watcher no longer requests DELETE while
opening supervisor, prompt, or Stop connections. This preserves the database's
established WAL mode across transition, delivery, hook, watcher, and reporting
commands and removes the exclusive WAL-to-DELETE negotiation that previously
failed before hook processing.

The issue-#23 one-process runtime successor deliberately replaces per-prompt
and per-request model command choreography with one bounded `request turn`
process and one canonical connection. UserPromptSubmit remains exact
capture-and-wake only. The Shotcaller model authors semantic triage and routing
in its normal reasoning pass; the adapter supplies only mechanical IDs, claim
tokens, times, hashes, locators, JSON, and arguments. Begin atomically persists
triage, claims, and routing plans; commit atomically persists answers/results
and delivery effects; the connection holds no transaction while the model
works. The same successor composes the existing assignment phases and real
Herdr/Codex boundary into `assign run`, grants only the exact canonical state
root as an additional workspace-write root, and records bounded context or
exact failure-cleanup receipts. Those League-specific rules now live only in
the orchestration supplement and no longer claim universal-guide ownership.

The issue-#23 rollover-successor correction deliberately separates immutable
prompt capture provenance from mutable current triage ownership, moves each
frozen Champion's agent/task/assignment/callsign/pending-delivery ownership in
one exact transaction, and creates a new Shotcaller by converting only the
verified calling Codex/Herdr pane in place. Squad registration remains a later,
separate operation; visible Champion creation retains its new-tab-root
contract. Stop user-visible text now names the resolved callsign, and only the
exact League-emitted feedback for the same scope/turn/generation is suppressed
from rearming. Genuine native steering remains provider-owned and always
rearms.

Migrations 12 through 17 are contiguous and named
`nullable-request-rollover-descendant-assignments`,
`standalone-shotcaller-callsign-scope`,
`immutable-prompt-provenance-current-owner`, and
`exact-stop-feedback-suppression`, followed by
`issue-coupled-cleanup-and-exact-thread-continuation` and
`immutable-switched-rollover-snapshot-revisions`. The final migration preserves
every prior snapshot revision while allowing the exact switched operation to
CAS-point at one refreshed immutable revision. The source-only refresh observes
two fake Herdr inventories through the production adapter boundary, requires
their normalized endpoint/route/session/terminal/sequence evidence to remain
identical, and binds both observation digests to the committed receipt. The
second inventory runs inside the consistent deferred transaction immediately
before pointer CAS; a recording writer probe proves the external read reserves
no SQLite writer lock. Runtime generation is deterministically derived from the
observed terminal and exact thread/session proof, and must match canonical
generation when present. It does not reuse an expired page or row input.
Focused tests use temporary roots and
recording fake Herdr adapters for no-runtime import, closed/mismatched/ambiguous
refusal, exact CAS and delivery-claim races, repeatable intake paging,
A-to-B-to-C provenance, current-pane publish/rollback fault injection, the
same-PID request-turn lifecycle, yielded prompt-hook acceptance, and exact
Stop-feedback behavior. No installed release, global guide, live SQLite state,
Herdr layout, or stable pointer is changed by this source candidate.

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

Issue #23 deliberately adds one narrower descendant reconciliation shape for
tasks created by the supported legacy importer before canonical assignment
fields existed. It accepts only the exact version-1 imported task shell, the
deterministic imported callsign assignment, one canonical import run with
linked Roster artifacts, and aliased legacy transition provenance. After a
fake Herdr adapter verifies the complete frozen pane, thread, terminal,
routing, worktree, generation, and callsign identity, `done` is normalized to
canonical `idle`; no other identity mismatch is normalized. The same
transaction creates the missing runtime and assignment, CAS-binds the task,
callsign, and Champion to the committed successor, retargets only the declared
exact pending outboxes, and records one immutable receipt. Modern descendant
reconciliation is unchanged. Synthetic supported-import parity, stale-proof,
exact retry, every-boundary fault rollback, outbox, immutable prompt-owner, and
bounded intake paging tests cover the deliberate compatibility difference; no
live state, filesystem writer, hook, installation, reconciliation apply, or
teardown is exercised.

## Reporting and privacy implementation provenance

The issue-#22/#25 reporting, privacy, guarded remote adapter, report renderer,
HTML template, report skill, source-managed League instruction, explicit-root
guidance adapter, schemas, documentation, public-safety gate, and focused tests
are original League implementation. No harness transcript, live Roster,
multiplexer, browser profile, global instruction, installed file, remote
transport, deployment, or personal/application record was read or modified to
author them. Visual language is adapted from this repository's source-managed
Project Ledger artifact; no asset or runtime dependency is copied from it.

The candidate preserves exact local roots, repositories, and evidence only in
the canonical local store. Outbound projections contain bounded summaries,
approved public URLs, opaque League IDs, hashes, and explicit placeholders.
The report skill uses only public `league report` commands and the League
supplement remains an uninstalled source input owned for later staging by issue
#23.

## Guide ownership correction provenance

Issue #90 replaces the incorrectly universal League guide source with
`global-agent-instructions/league/AGENTS.md`, SHA-256
`1067522f0c7608fc8c4a657fa005c99f8d058df4fd979b7b3415c89535db4fbe`.
The supplement retains only League orchestration deltas and refers universal
issue, worktree, implementation, review, release, cleanup, and public-safety
behavior to the terminal-environment-toolkit-owned guide. Toolkit issue #45
owns the short universal trigger and makes no changes through this repository.

The source candidate is `0.2.28`, the first unallocated release identity after
the retained `0.2.25`, `0.2.26`, and stable `0.2.27` releases. The isolated
installer refuses a pre-existing `0.2.28` release or release-bundle directory
before changing its synthetic stable pointer or existing bytes; it never
reuses a release identity as an overwrite target.

The guidance adapter and isolated release rehearsal accept only
`league/AGENTS.md`, reject `AGENTS.md` and universal path forms before file or
pointer mutation, install exact packaged supplement bytes, restore only the
prior supplement, and prove one universal hash across pre-install,
post-install, and post-rollback receipts. These are deterministic temporary-root
proofs; this candidate performs no installation, stable-pointer change, live
guide mutation, merge, or toolkit edit.

The issue-#90 blocker correction extends those invariants to the production
`run_pre_cutover`/`run_live_cutover` boundary. Universal guide paths now refuse
during plan validation, exact `0.2.28` release and release-bundle collisions
refuse before the lock or any filesystem mutation, and the locked executor
rechecks them before apply. Inode-aware temporary-root tests also prove that
refusal changes no node or tree and that a late rollback does not replace an
unchanged universal guide or League supplement.

The release-staging boundary now opens every manifest path component relative
to one root descriptor without following symlinks, binds the opened file to one
regular identity, and checks
that both staged copies are regular files with exact source bytes. Release
reads use bounded buffers and reject oversized sources before allocation. A
staging
crash atomically quarantines a candidate path, verifies its recorded
device/inode identity, and removes only identities newly reserved by that
attempt before retry. Cleanup failures cannot replace the original staging
refusal. A matching marker pair lets a later process recover only an exact
VERSION-only partial stage with unchanged source bytes and recorded directory
identities; all other existing candidates still refuse. Focused temporary-root
coverage proves process-death recovery, foreign directory and symlink
preservation, marker/source/inode/extra-content refusal, post-switch pointer
rollback, VERSION regular-file identity, and byte parity. Descriptor-relative
writes cannot follow swapped staging subdirectories, and final source identity
checks include size and timestamp. Guide-hash preservation and rollback remain
verified without changing any retained release or live pointer.

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

## Launch title provenance

Issue #85 deliberately changes visible Champion launch finalization. The
pre-context metadata write remains an initial display seed, but successful
context delivery is no longer sufficient by itself: the same launch-owned
adapter must restore and verify the exact callsign/task sidebar, thread, and
terminal title after the context prompt settles. The final context receipt is
bound to the launch metadata source, agent authority source, and a stable
post-context state-change sequence. A retry sends no second prompt: it freshly
observes the exact live endpoint and may perform at most one ownership-safe
restoration. Changed ownership metadata refuses restoration and records a
cleanup-pending title-validation failure without overwriting the display.
Generated labels now default deterministically to exactly two words and
explicit labels obey the same maximum. Focused fake-Herdr coverage exercises a
delayed context auto-title race, post-context restoration, retry deduplication,
owned retry repair, unowned-metadata refusal, and two-word derivation without a
live runtime. The same provenance rule now covers in-place Shotcaller bootstrap:
the allocated callsign title must settle for two effective-source and sequence
observations, exact retry may restore same-authority drift once without a
prompt, and rollback preserves newer user-owned presentation metadata while
restoring only League-owned sidebar/thread tokens to baseline and releasing the
League route and callsign reservation. Focused fakes prove unrelated user
tokens remain exact, zero layout/process creation, and no Squad registration.
One owner-authorized compatibility exception covers a pre-baseline version-2
retired unbound Shotcaller residue whose metadata is exactly empty. It still
requires the complete single rolled-back assignment/event history and absence
of every active resource. League accepts only a current unbound presentation
whose source is not League-owned and whose title/sidebar/thread contain no
retired callsign, then atomically records a v2 baseline containing its source,
title, endpoint generation, and state-change sequence with the new reservation.
A second exact observation before publication refuses any source, title, token,
thread, terminal, generation, route, or sequence race. Before the first rename,
League records an immutable assignment-bound publication attempt containing the
exact endpoint, provider presentation, baseline digest, and observed global
state-change sequence. An exact reserved retry may resume an already-published
callsign alias when every identity and presentation byte still matches that
attempt and the current global sequence is not older. It does not require
`baseline + 1`: Herdr metadata freshness is source-local, while unrelated pane
state can advance the global observation sequence. Retry skips the duplicate
rename and reports the League overlay without supplying that global value as a
Herdr source sequence. Explicit owner/source tokens identify the completed
overlay, followed by an exact first post-effect global fence and two stable
observations. Any later provider or user presentation refuses and is preserved.
Installed 0.2.36 exposed one more provider shape: the routing rename can publish
the exact alias and corresponding route tokens while leaving the prompt-derived
provider title, callsign, sidebar, thread, and identity title unchanged. Initial
inspection now treats that routed presentation only as a candidate. It is
admitted when an exact reserved canonical assignment and v2 baseline/publication
attempt bind the callsign, endpoint, thread, terminal generation, physical
worktree, provider source/title/tokens, and a non-regressing observation. The
read-only proof is followed by one immutable runtime binding; future attempts
with another runtime refuse. The later pre-effect observation must equal the
initial sequence, so an interleaved write cannot use the admission window.
Missing or malformed proof, arbitrary routing, assignment/runtime drift, and
changed endpoint or provider presentation refuse before Herdr mutation while
retaining the alias reservation as the truthful cleanup obligation. A race
after admission restores the exact provider baseline and clears the owned alias
before canonical rollback only when that external cleanup is proven. Otherwise
League retains the reservation, lease, rebound agent, baseline, publication
attempt, and runtime binding as the cleanup obligation.
One still-older frozen profile contains exactly two metadata keys. It is
eligible only when `scope_kind=squad`, `scope_id` exactly equals the sole prior
rolled-back assignment's historical Squad scope, and the currently verified
thread ID exactly equals the retired agent ID. Every existing version-2 agent,
single-assignment, exact-subject, rollback-event, available-callsign, and
no-runtime/no-Squad/no-offer/no-lease fence remains mandatory. League captures
the clean current v2 presentation baseline in the same reservation transaction
and normalizes durable metadata to `scope_kind=shotcaller` and
`scope_id=<exact-agent-thread-id>` before Herdr publication. Any extra metadata
key, changed historical scope, different thread, incomplete history, owned
resource, or interleaved presentation refuses without publication.
Installed Herdr can omit `metadata_source` while still exposing its complete
provider presentation envelope. Shotcaller preflight accepts that shape only
when `harness`, identity thread/title, callsign, sidebar, thread, logical
terminal title, and provider session source agree exactly; the terminal's
provider suffix is normalized without changing the stored title. Presentation
tokens never establish routing. Only consistent top-level `name`,
`routing_name`, or `routing_alias` fields do, and any conflicting route,
present-but-invalid source, partial envelope, thread, terminal, or generation
observation refuses before mutation. A settled Herdr `done` Codex remains a
live exact endpoint under the same fences.
The reopened issue adds one deliberately separate legacy provenance path:
owner-authorized reconciliation binds the exact canonical Champion identity to
the expected live presentation, writes immutable intent before the external
effect, and applies one reconciliation-specific source overlay because Herdr
sequence freshness is source-scoped. The global observation sequence detects
an interleaved presentation; that path clears only the League overlay and
refuses after the newer presentation stabilizes. A final receipt is recorded
only after two stable exact observations. The path also refuses modern
receipts, orphaned history, runtime ambiguity, route drift, and user-title
races while preserving unrelated tokens; exact retry returns the same stored
receipt without another metadata write. In-place Shotcaller identity inspection also
retries only transient malformed read-only Herdr results, with persistent
malformation still refusing before reservation or publication.

## Scoped autonomous-delivery provenance

Issue #81 adds original League-only migration v18, `ModeStorage`, the six
`league mode` commands, strict grant/action/receipt schemas, issue-first GitHub
verification, source-managed guidance, and focused synthetic tests. It derives
no authority from prompt text and imports no external implementation.

The deliberate baseline differences are durable immutable Summoner grant
revisions, Shotcaller-owned external-action uses, checked limits and goal
transitions, revocation, bounded repair obligations, and one immutable
repository-issue binding before visible launch mutation. Repository,
configuration, migration, test, benchmark, durable research, release,
operational, reproduction, debugging, and bug-fix work now force visible
Champion execution; the prior direct-tiny answer/check path is preserved.

The owner-found duplicate-issue regression deliberately extends v17 with
a normalized repository/title/semantic-scope lease and immutable per-task issue
selection receipts. Open equivalents are reused, genuine closed recurrence
requires the existing settled Shotcaller reopen authority and preserves prior
Champion/runtime session linkage, distinct work creates once, and concurrent
creators fail closed behind the SQLite owner fence. `assign run` now requires
that receipt in addition to the fresh owner-API issue verification. The task
summary and issue title share one normalized duplicate identity while the
selection and owner-API receipt retain exact title-byte equality.

Migration v18 is named
`scoped-autonomous-delivery-and-issue-first-assignment`, checksum
`b517b9103fedcc0db8a1f0dd7d06d475f309f3a135d87356209ab34dbd957631`.
Existing migrations remain byte-for-byte unchanged. Focused tests use only
temporary roots and fake GitHub/Herdr boundaries; they perform no live grant,
merge, release, installation, deployment, production action, or teardown.
The rules live in the League supplement; the toolkit-owned universal guide and
the supplement's existing 16 KiB fail-closed staging bound remain unchanged.

The issue-#81 continuation adds original League-only migration v20 and one
protected-gate executor. It deliberately carries an already accepted exact
grant through later assignment reconciliation, Shotcaller creation, Squad
registration, rollover, retirement, and teardown gates. Each protected use is
immutably bound to its command category and canonical scope digest before the
effect, and each effect outcome settles a separate receipt. It does not infer
authority from mode state, combine manual and autonomous authority, bypass an
existing platform or provider refusal, or broaden any grant category.
The current correction additionally requires the exact target digest as a
singleton action resource contained by the immutable grant resource boundary,
makes settled receipt retry effect-free, and records an immutable action-use
goal fence so a later authorized concurrent use cannot strand an older one at
settlement. Pre-v20 in-progress rows without that fence refuse for explicit
reconciliation.

Migration v20 is named
`autonomous-protected-gate-authority-propagation`, checksum
`b36865213f931b6522f2f8c807dcea60c3949a08eab05772c6ad8567fbdcf71a`.
Existing migrations remain byte-for-byte unchanged. Focused temporary-root
tests cover one grant across multiple protected actions, adjacent and foreign
target refusal before operation, effect-free settled retry, exact CLI
propagation, two-writer use CAS, max-concurrency settlement, immutable
receipt migration rollback, backup/export/import, and schema validation. This
source slice performs no installation, live grant mutation, reconciliation,
Squad creation, deployment, teardown, or merge.

## Issue-coupled cleanup and exact-thread continuation provenance

Issue #83 implements the continuation portion of the accepted issue-#15 policy
as original League code; the integrated repository version remains `0.2.29`.
Migration v16 is contiguous and named
`issue-coupled-cleanup-and-exact-thread-continuation`. It deliberately replaces
the historical all-row runtime-session uniqueness index with a live-row partial
unique index, while adding permanent thread lineages, immutable cleanup
archives, linked runtime incarnations, and exclusive fenced continuation
operations. Historical runtime, task, assignment, cleanup, Git, callsign, and
issue receipts remain separate and are never rewritten into a synthetic
continuous runtime.

The deliberate baseline differences are: eligible Champion cleanup may append
one exact owning-issue close action after callsign release; archive availability
requires that action's verified receipt and the final teardown receipt; and an
explicit successor may claim that archive, reopen only its owning issue, and
activate a new runtime carrying the same provider thread identity. The callsign
queue remains unchanged and may allocate any currently compatible entry.

The GitHub issue adapter, read-only Git binding check, Codex exact-resume launch
argument, post-start thread equality check, storage protocols/operations, CLI
commands, incident analysis, and focused synthetic tests are original to this
repository. Provider thread values remain opaque in the canonical store. The
current launch edge verifies the Codex UUID required by the installed Codex CLI;
other providers fail closed until an operational exact-resume driver exists.
Live Herdr acceptance showed that a resumed Codex process does not republish its
session identifier automatically. The `0.2.29` launch edge therefore verifies
the exact foreground `codex resume` argv and worktree before reporting that
opaque session identifier through Herdr's canonical Codex metadata source and
next sequence; mismatched or ambiguous processes still fail before activation.
The `0.2.29` correction deliberately replaces the duplicated cleanup
task-state/disposition rules with one shared matrix at both the atomic planning
and pre-claim execution boundaries. An explicit owner cancellation or rejection
may clean a `ready_to_land` task; truly incompatible combinations refuse before
claiming a cleanup revision. Focused synthetic recovery persists a fence-zero
cancelled plan, reopens the canonical store as the upgraded executor, executes
without replanning or direct state edits, and proves the completed retry is
idempotent.
The v16 target-version binding changes only the deterministic acceptance
fixture's dry-run report digest; its source and imported-row parity digests are
unchanged.

## Semantic-triage ablation provenance

Issue #66 adds a repository-local diagnostic benchmark, public synthetic
120-prompt corpus, structured-output schema, and focused fake-model test. The
triage-off arm is benchmark-only and does not create a second production path;
normal League behavior continues to require model-authored semantic accounting.

The installed 0.2.27 turn refuses a 25-prompt batch because its internal bound
is 20. The source candidate deliberately raises only that existing bound to 25
so the owner-required 1/6/25 scaling matrix can execute. It does not change the
per-prompt 32-item bound, 20-new-request plan bound, transaction shape, storage
schema, migration set, journal policy, hooks, watcher, installation, or live
state. The benchmark and focused test use explicit synthetic temporary roots.

## Inline-triage and persistent-supervision provenance

The issue-#66 successor keeps the completed Luna xhigh ablation receipt
immutable and changes the production design rather than selecting a faster
synchronous classifier. The active Shotcaller authors ordered semantic items
for its existing one-process turn. The adapter validates, exact-deduplicates,
version-links, and commits locally; no ordinary turn starts a second model.

The deliberate source differences are a 12-row/24,576-byte deterministic
same-owner candidate shortlist, complete-snapshot fencing before external
dispatch, deterministic off-path candidate pages, schema-19 agent-authored
duplicate reconciliation, and one persistent event-driven supervisor runtime
with renewable/fenced ownership. Stop remains an omission backstop and does not
merge requests. A source launchd template declares the intended owner boundary
but is neither rendered nor installed.

The deliberate supervision follow-up adds Calm filtering plus durable
supervising/paused policy state, one exact pause receipt, bounded resume
reconciliation, one-shot Champion Stop protection, and fenced canonical
runtime reconciliation. Calm with supervision on keeps an event-driven wait
outside model inference and uses the registered Unix socket. Calm with
supervision off ends the model turn while the non-model monitor and its lease
remain live; routine transitions stay silent and attention uses the verified
exact-once direct recipient path. Real owner prompts keep priority.

Normal transition delivery is immediate. A missing runtime gets one
configurable 60-second grace before CAS-safe reconciliation. A 300-second
bounded SQLite audit is lost-notification/restart recovery only. The monitor
renews silently every 20 seconds, ownership expires after 60 seconds, and the
launchd template throttles restart to five seconds. The retained one-second
`supervise` loop is diagnostic compatibility, not the production runtime.

Owner-source installed 0.2.28 truth remains distinct: its foreground legacy
loop has a 30-second runtime snapshot, two matching observations (about 60
seconds) before a stall fallback, and a 300-second liveness deadline that only
resets silently. It has no separate OS timer or always-running liveness process,
and both timers vanish when the foreground loop exits. Those legacy timers are
not the source candidate behavior. The launchd/socket source in this change
remains uninstalled.

The post-0.2.35 issue-#66 Stop correction treats Codex `turn_id` as a turn
scope, not a per-prompt event key. Each real `UserPromptSubmit` invocation
mints one opaque League capture identity, carries that same identity through a
broker retry or direct fallback, and binds it to the immutable prompt/source
provenance. Two genuine same-turn invocations therefore remain distinct even
when their prompt bytes are identical. Stop rearms only from a committed
durable wait event; a fresh-looking terminal identifier alone cannot add a
second block. The exact pending League feedback remains one-time suppressed,
and the matching Stop retry is allowed. This source-only correction adds no
schema migration and performs no installation, hook mutation, live
reconciliation, or runtime cutover.

The issue-#123 successor deliberately replaces the one-binding physical
supervisor assumption with one root-scoped service that discovers active Squad
Shotcallers and holds an independent durable fence for each. Root lock/socket
ownership remains singular, but binding registration, cursor, generation,
priority, Calm policy, recovery, and delivery identity never cross Squads.
`request turn` now marks its exact active/committed boundary: attention persists
without starting a concurrent model turn, and Stop hands pending delivery to a
live fenced service only after the request commit and only when no immediate
owner action remains. The launchd template provides RunAtLoad, failed-exit
restart, and five-second throttling but remains inert and uninstalled.

The 3×3 prompt-size/intent-count matrix measures exact capture, JSON sideband,
candidate linking, SQLite commit, and one-process completion on synthetic
temporary roots. Its gold sideband proves local mechanics only; it does not
claim active-model split quality, installed prompt capture, live supervision,
or the #23 owner-visible E2E. No global file, hook, service, model route,
canonical database, or live runtime is changed.

Current main's issue-coupled continuation, rollover-snapshot, scoped
autonomous-delivery, and request-reconciliation migrations remain canonical
schemas 16 through 19. Issue #81 appends protected-gate authority propagation
as schema 20; it does not renumber, replace, or mutate any earlier migration.
The deterministic acceptance dry-run report follows the current schema target. Its
legacy-source digest and exact
post-import parity digest remain unchanged; only the truthful target-version
report digest changes.

## Rollover runtime capability provenance

Issue #23 preserves callsign capabilities as minimum requirements, not an
exact runtime inventory. Snapshot refresh and descendant reconciliation accept
one verified canonical runtime only when every active callsign requirement is
present in that runtime's normalized immutable capability set. A strict
runtime superset is retained unchanged; missing requirements, malformed sets,
runtime drift, and unverified identities still refuse before reconciliation.

The immutable refresh and descendant reconciliation receipts record both the
minimum requirement set and the actual canonical runtime set. This source-only
correction changes no schema, callsign requirement, live runtime, hook,
installed release, or active rollover state. Focused synthetic tests cover
superset preservation, missing requirements, runtime drift, unverified
identity, exact retry, and unchanged snapshot/ownership CAS boundaries.

## Partial-progress rollover refresh provenance

Issue #23 deliberately extends only the expired switched-rollover refresh
boundary. A descendant still owned by the predecessor keeps the prior checks.
A descendant already owned by the successor is accepted only when one durable
`rollover_descendant_reconciled` receipt proves the same operation and exact
task, assignment, callsign, runtime, capability, and outbox transfer. Capture
history and prior snapshot revisions remain immutable. The proof validator
requires the complete reconciliation receipt schema for both newly created and
pre-existing task assignments; any missing, extra, or type-changed immutable
live-evidence field refuses even when the attacker recomputes the outer digest.

The replacement snapshot retains the complete original Champion/task/callsign
set. Its immutable refresh receipt records predecessor-pending and
successor-reconciled progress separately; `rollover bindings` exposes only the
proved successor entries as terminal markers. Missing, duplicate, forged,
stale, partially retargeted, or concurrently changed proof refuses before the
snapshot pointer CAS. This changes no schema, release, installation, or live
rollover state.

### Historical imported-descendant receipt compatibility

The first source release that reconciled imported legacy task shells wrote an
exact durable receipt before minimum and actual runtime capability lists were
added to that receipt schema. Issue #23 keeps that historical evidence usable
without treating arbitrary missing fields as compatible. The compatibility
profile is generic to imported legacy descendants and requires the exact old
field set, `source_shape=imported_legacy_partial`, and atomically created runtime
and assignment. The reconciliation event digest and the created assignment's
unchanged acceptance receipt must agree exactly with that historical receipt.

Refresh then re-proves the original immutable snapshot row and current task,
assignment, callsign, verified runtime, callsign-capability subset, and complete
pending-outbox transfer. The actual canonical runtime capability superset is
retained in the refreshed binding and receipt; it is never replaced by the
historical receipt's absent fields. A current receipt with deleted fields, a
pre-existing assignment, a missing or duplicate event, a changed acceptance
copy, malformed types, extra fields, or canonical/outbox drift still refuses
before any refreshed snapshot row or pointer mutation. This source-only fix
adds no schema migration and performs no live refresh or reconciliation. For
the historical profile, `pending_delivery_count` must equal the sorted unique
`retargeted_outbox_ids` count. Older receipts that counted unenumerated
successor-pending deliveries remain unverifiable and refuse instead of being
guessed from current state.

### Imported legacy null-route adoption

Issue #23 permits one additional mutation inside the existing switched,
expired-snapshot refresh transaction. A predecessor-owned descendant with both
canonical route and display identity null may adopt a route only when its task,
callsign assignment, and import run/artifact/legacy-event linkage still match
the exact `imported_legacy_partial` production shape. A modern task whose route
was cleared, a successor-owned row, or any non-null partial identity refuses.
The frozen binding may equal the current null-route binding exactly, or it may
equal that same current non-runtime identity with every runtime field null when
the sole later change is exactly one verified active/idle canonical runtime.
That runtime must match agent kind, backend, thread/session, endpoint, required
capabilities, and the live Herdr observation. A prior frozen runtime change,
any non-runtime identity change, multiple/unverified/inactive runtimes, or a
capability gap refuses.

The Herdr adapter must find exactly one live interactive Codex endpoint by the
combined canonical pane, exact thread/session, and normalized callsign. The
top-level Herdr name must equal the lowercase callsign; terminal titles,
sidebar tokens, and other display metadata are not routing evidence. Optional
explicit `routing_name` and `routing_alias` fields may be absent, null, or the
empty string; every non-empty explicit route field must exactly equal the same
lowercase callsign, while non-string, whitespace, or conflicting values refuse.
Exact pane, route, session, cwd, foreground cwd, terminal, state sequence, live
status, and deterministic runtime generation are observed twice. For older
Herdr inventory revisions, an absent `interactive_ready` field is accepted only
when the exact endpoint is settled at `done` or `idle`; explicit `false`, null,
or malformed values refuse. Active, working, blocked, waiting, or unknown
status with the field absent also refuses, while the existing affirmative
`interactive_ready=true` contract remains valid for recognized live statuses.
Any missing, mismatched, closed, unready, drifting, or overlapping endpoint
refuses before mutation. Per-descendant refusals expose only the callsign or a
stable opaque locator, never local paths or provider thread identifiers.

After the final stable observation, League rechecks the frozen source binding,
agent version, callsign assignment/version/requirements, and zero-or-one exact
runtime identity/generation/status/capabilities. It then CAS-updates only the
null route plus `display_agent=codex`, increments that agent version, and emits
one immutable hash-bound adoption event/receipt. The receipt binds the frozen
source, pre-adoption current, and resulting binding digests plus the actual
runtime id, generation, status, and capability set. The refreshed snapshot rows
are constructed from the post-adoption canonical bindings, and the enclosing
refresh receipt binds every adopted descendant's prior and resulting agent
version. Route events,
agent updates, snapshot rows, and the rollover pointer share one transaction;
fault or CAS failure rolls all of them back. No schema, live rollover, hook,
installation, layout, process, task owner, or successor row changes here.
