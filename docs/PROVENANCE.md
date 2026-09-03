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
| `src/agent_watcher.py` | `agent_watcher.py` | `ab77f3f0d97cb1b09e34c005c45e90b94a8c0e0612f115169bc62827e524e0d7` | Originally byte-for-byte; issue #66 deliberately removes the universal automatic second-Stop allowance while retaining the explicit operator one-shot override. |
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

Codex+Herdr and Codex+tmux were the original named adapter contracts. Issue #84
deliberately adds Cursor+Herdr and Pi+Herdr to the production visible-assignment
and cleanup driver while leaving the generic `RuntimeLifecycle` backend
contract-only. Provider session values remain opaque to core storage; the
provider boundary owns exact validation and resume arguments. Pi shell
confinement and presentation are a release-local, per-process extension and
sandbox profile. Prompt intake, pre-mutation authorization, and Stop/rearm are
now a separate source-managed profile extension. It is inert for an unbound
ordinary Pi session and activates only when the existing canonical hook command
proves that exact Pi session. Focused fake-adapter tests cover Codex, Cursor,
and Pi; they are not live-provider evidence. Merge, installation, live provider
canaries, cutover, rollback, and teardown remain separate gates.

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

The deliberate supervision follow-up keeps notification and model attachment as
independent axes. `all_material`/`calm` controls only filtering; Calm persists
routine transitions silently in every attachment state. `attached`/`detached`
controls only model participation while the non-model monitor, lease, and socket
stay live. Attached delivery uses the fenced watcher channel; detached delivery
uses the exact-once direct recipient path; attach returns a bounded silent-event
reconciliation. Real owner prompts keep priority.

Normal transition delivery is immediate. A missing runtime gets one
configurable 60-second grace before CAS-safe reconciliation. A 300-second
bounded SQLite audit is lost-notification/restart recovery only. The monitor
renews silently every 20 seconds, ownership expires after 60 seconds, and the
launchd template throttles restart to five seconds. The retained one-second
`supervise` loop is diagnostic compatibility, not the production runtime.

Installed 0.2.45 truth remains distinct. Read-only owner evidence showed the
release healthy but `agent-watcher --shotcaller Ashe service-status` returned
`live:false`, `monitor_live:false`, `reason:registration_missing`;
`service-resume` refused `supervisor_not_live`, and no persistent service process
existed. The source candidate therefore adds a hash-authorized launchd
install/start/restart/rollback controller around the existing template. This
change does not execute that controller, install the Herdr plugin, restart Herdr,
or mutate live canonical state.

The post-0.2.35 issue-#66 capture correction still treats Codex `turn_id` as a
turn scope, not a per-prompt event key. Each real `UserPromptSubmit` invocation
mints one opaque League capture identity, carries it through broker retry or
direct fallback, and binds it to immutable prompt/source provenance. Two genuine
same-turn invocations remain distinct even when bytes match. This successor
intentionally supersedes the old anti-loop behavior: an attached Shotcaller now
blocks every unchanged Stop while any obligation remains. A detached Shotcaller
blocks owner-actionable work and may allow delegated-only work only when the
stored lease, runtime generation, Unix locator, watcher ID, and fence still match
its detachment receipt. Exact League feedback suppression remains one-time, but
it never grants Stop. The legacy focused regression is
`tests/test_agent_watcher.py`; canonical repeated-Stop and detachment coverage is
in `tests/test_shotcaller_stop.py`.

The issue-#123 successor deliberately replaces the one-binding physical
supervisor assumption with one root-scoped service that discovers active Squad
Shotcallers and holds an independent durable fence for each. Root lock/socket
ownership remains singular, but binding registration, cursor, generation,
priority, notification policy, attachment, recovery, and delivery identity never
cross Squads. The launchd controller accepts only exact source/template hashes,
preserves one exact prior plist and manifest, uses RunAtLoad plus failed-exit
restart, waits for aggregate live status, restarts through the OS manager, and
rolls back only matching installed/backup bytes. `service-run` is never launched
by a model turn.

The installed Herdr asynchronous restore command remains provider- and
multiplexer-neutral. It now requires the OS watcher to be live before restart,
pings the exact Shotcaller actor, CAS-rebinds only that restored runtime and
watcher fence, verifies it again, then replays metadata for restored Codex and Pi
sessions (including Pi with Codex or Cursor provider). It never creates, resumes,
prompts, or closes a process.

### Semantic owner-stop follow-up

Ashe's post-release live exercise proved a remaining control gap: an explicit
owner stop required repeated manual `allow-stop-once`, a new prompt generation
blocked again, and delegated work continued. This issue-#66 follow-up is original
League code rebased onto exact `origin/main`
`8f8051b697fe5d4a7a618611c1c9c2498d882d4e` without changing its 0.2.52
release identity or bytes. It does not interpret natural
language in hooks. The active Shotcaller alone emits a structured semantic
`owner_control`; the final request-turn transaction records its exact prompt,
owner, scope, and user-message generation together with deterministic delegated
control outboxes. A requested interruption targets only active Champion/hidden-
worker agents owned by that Shotcaller and only when each has one exact verified
runtime. Codex and Pi use their declared provider-native steering/prompt surface,
Cursor retains state-aware steering, and the multiplexer remains registry-
selected.

The request transaction commits recording, request effects, outboxes, and turn
state atomically; provider steering is necessarily post-commit. The persistent
service retries current `dispatch_pending`/`failed` controls from exact bound
scopes. Exact recipient receipts prevent replayed provider effects, and a
transient final authorization write remains pending rather than falsely marking
delivery failed. Owner control bypasses attached watcher routing and resolves the
captured delegated runtime directly.

Authorization is withheld until every requested outbox has an exact recipient
receipt. Stop consumes it for the matching user generation, permits an identical
terminal-generation retry without a loop, and refuses reuse by another terminal
or owner prompt. Pending/failed delivery remains a visible refusal. The generic
one-shot override is retired and refuses without mutation; notification mode,
attachment mode, and verified detached watcher handoff remain independent.
Startup and hook paths now use the same bounded
scope resolver: one valid scope wins, a sole persistent-service owner reconciles
multiple historical candidates, and all other ambiguity fails with Shotcaller
and candidate-count repair evidence rather than generic
`supervisor_binding_invalid`.

The owner-machine benchmark plan is reproducible and synthetic: from the exact
candidate worktree run
`PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark_watcher_service.py --samples 500 --include-owner-stop`.
It creates only a temporary three-Squad state, one non-model service thread, and
500 samples per operation. The current run produced schema v2 with service ping
p50/p95 3.597/4.099 ms, targeted ping 0.102/0.175 ms, and semantic owner-stop
record plus two Stop decisions 0.275/0.404 ms. The one-line JSON receipt SHA-256
is `cb7c492d49974437c14ab68daffa76650437b99687e4079bc4e17a9370269369`.
The default command without `--include-owner-stop` remains schema v1 compatible.

### Current-main reconciliation and encountered failures

This successor reconciles reviewed PR-#126 head
`ac6ce35b3a46c78b62afdd6018bda8aacc325d19` with `origin/main`
`02376107ebf2544191cea5de0571ecaf26bfea1c` (the League 0.2.45 / PR-#138
line) without replacing its Codex, Pi, Cursor CLI, Herdr, or tmux adapter
contracts. The reconciliation encountered and retained the following bounded
failure evidence:

| Failure | Resolution / remaining owner action |
| --- | --- |
| Installed 0.2.45 had no watcher registration/process; status reported `registration_missing` and attachment resume refused `supervisor_not_live`. | Added the source-only exact launchd install/start/restart/rollback path and actionable Stop/attachment refusal. Ashe still owns installation and live proof. |
| Main integration conflicted in the roadmap, hook broker, and persistent runtime. | Kept #84 adapter-neutral broker/restore behavior and merged it with actor-targeted, per-Squad bindings; focused provider/multiplexer and multi-Squad tests cover the result. |
| The first restored-agent focused run rebound Ashe successfully but status still read the empty active-Squad registration snapshot for the explicit compatibility binding. | Status now reads the resolved exact actor registration in the same canonical snapshot; restored Codex/Pi metadata and watcher delivery pass. |
| Old Calm tests expected pause to mutate monitor state and old committed-turn tests expected attached Stop handoff. | Replaced them with the independent four-state notification/attachment matrix and repeated attached-Stop blocking. Deprecated names are aliases only. |
| The first rollback assertion expected one top-level aggregate reason. | Aggregate status remains per-Squad and the test now verifies every binding's `registration_missing` reason. |
| The first exact-once delivery assertion counted unrelated seeded startup backlog. | Exact-once acceptance now counts only the target event identity and separately proves one outbox attempt and one recipient receipt. |
| Short test leases exposed a fence race: routine renewal rotated the fence, so a status/publish snapshot could become stale while the same process still owned the service; detachment receipts also expired semantically on renewal. | Routine renewal now extends the same exact live-owner fence; only process startup, stale-owner takeover, or restored-runtime rebind advances it. Five repeated delivery runs and restart fence assertions pass. |
| The first affected request-turn run omitted `turn_commit_pending` from attached aggregate obligations after the detachment split. | The attached aggregate now retains the pending-turn guard; the grouped request lifecycle passes. |
| Main's runtime-replacement pre-tool test expected the pre-#123 two-field broker result. | Its exact expectation now includes the resolved `actor_agent_id` required for per-Squad dispatch; the full provider-neutral runtime lifecycle passes. |
| The first acceptance run used a legacy synthetic wake locator for direct detachment. | The fixture now declares the same persistent/Unix identity required by production while remaining temporary and effect-free. |
| Main's in-place Shotcaller bootstrap has a valid pre-Squad request turn, but the first multi-Squad integration required an active Squad too early. | Turn ownership again accepts one exact active Shotcaller; only OS service discovery requires an active Squad. Bootstrap and multi-Squad gates both pass. |
| Fresh exact-head review found attachment authorization could validate the old service fence before the transaction but create a receipt for a concurrent takeover fence. | The service now passes its exact watcher ID and fence into the attachment transaction; a focused takeover race proves stale attachment refuses without changing policy. |
| Independent review of exact head `c3edada152fad2e7f8c77ae894ce99c9d60167d7` found detached Stop could hand off `blocked` or `ready_to_land` tasks as delegated-only work. | Those task states are now explicit owner-actionable decisions in detached mode; focused regressions prove both remain blocked. |
| The same review found every detached block path returned before persisting its Stop receipt, while the explicit one-shot override was cleared without first being consumed. | One transaction-local helper now persists the blocked/wait/feedback tuple for attached, detached-owner, and unavailable-supervisor refusals; `allow-stop --once` is checked and consumed first, and the immediately following Stop blocks again. |
| The same review found an old supervisor could raise its own fence after takeover, including during restored-runtime rebind. | Every renewal and rebind now supplies the previous watcher ID/fence as an atomic storage CAS; a synthetic takeover remains canonical through the old process's next renewal, which exits fenced. |
| The same review found `test-all` did not execute the multi-Squad service acceptance and an idempotent install trusted its manifest's rollback claim without re-reading the backup. | The multi-Squad test is now in the required request-lifecycle gate, and idempotent install validates exact backup presence and hash before reporting `rollback_ready`. |
| Independent review of replacement head `07539fb74b6ed0f288b3664b423598d988e2a34b` found the local service socket accepted an unscoped Stop that terminated every Squad. | Service Stop now carries the exact complete binding set, watcher IDs, fences, runtime IDs, and runtime generations; the service validates that aggregate identity against its live canonical leases before acknowledging. Unscoped and stale aggregate requests leave the service live. |
| The same review found `service-start` verified only the installed plist, not the source executable/template hashes authorized by the manifest. | Start/restart now re-reads both bounded user-owned source files and refuses drift before invoking the service manager; focused executable and template drift tests prove the live synthetic service is not restarted. |
| Independent review of follow-up head `becc4f68103df666d36f30291d65a44de30d11d3` found the aggregate Stop payload named runtime generation but fenced registration validation did not compare it with the current canonical runtime row. | Registration snapshots now join the referenced runtime generation and every single/batched fence validation compares it exactly; a generation-only drift regression proves Stop refuses and leaves the multiplexed process live. |
| Inline review of merged head `d83a9ebb9f6dae905d8e5e59470cc82697b99a80` found aggregate Stop rediscovered every reported callsign separately. | Stop now loads active bindings once, maps the service-reported callsigns in memory, and retains one single-binding lookup only for the pre-Squad compatibility path. A focused regression rejects any per-callsign lookup for a three-Squad stop. |
| The same inline review found Stop held the in-process fence lock across SQLite validation, allowing a delayed reader to starve lease renewal. | Stop snapshots local identity under the lock, validates one batched canonical read without that lock or a write transaction, then reacquires the lock and rejects any local rebind before setting the stop event. A delayed-validation regression proves renewal advances during the delay. |
| The same inline review found source hashes were checked before launchd consumed their paths. | Install and start/restart now revalidate executable and template bytes after verified service liveness; detected check/use drift stops the launched job (and restores an in-progress install) before success. The supported release writer creates immutable versioned paths, and non-cooperating same-user mutation cannot be made an OS security boundary because that user can already control the process; the operation therefore promises exact successful receipts and fail-closed drift cleanup, not hostile-same-user execution isolation. |
| Independent review of follow-up PR #144 found the active-manifest idempotent install branch still omitted that post-liveness source check. | Fresh install retains its rollback wrapper, while start/restart and idempotent install now share one post-liveness validator that unloads a launched job before refusing drift. The executable/template × start/idempotent-install matrix proves no operation reports success or leaves launchd loaded after check/use drift. |
| Ashe's released live path needed repeated manual `allow-stop-once`; a newer prompt reblocked and delegated work continued. | Added a semantic, generation-scoped canonical owner control. It is never inferred from text, is transactionally durable, optionally emits exact owner-only delegated controls, authorizes only after receipts, and allows only its consumed terminal retry. |
| The owner-stop red test initially lacked the new module, then exposed that a committed request-turn marker rejected the next prompt generation as `shotcaller_turn_active`. | Added the provider-neutral executor and permits replacement of a committed turn only after durable `user_message_generation` advances; same-generation concurrency still refuses. |
| The first multi-Squad owner-stop fixture collided with an occupied callsign, then violated the callsign foreign key, and also retained an unrelated active hidden worker without a verified runtime. | The test now uses unoccupied catalog identities and terminalizes only that unrelated fixture worker. Production correctly retains `owner_stop_target_invalid` when an active delegated runtime is absent or ambiguous. |
| The first owner-stop teardown called a nonexistent in-process supervisor method. | The regression now stops its temporary service through the supported exact aggregate `stop_supervisor` IPC contract. |
| Initial scope reconciliation excluded imported watcher schema v2 and changed one malformed-policy assertion from `supervision_policy_invalid` to a generic scope code. | Valid historical scopes explicitly include initialized schema v2/v3; a sole malformed policy preserves its precise refusal, while multi-candidate invalidity/ambiguity returns bounded repair evidence. |
| The first affected request-lifecycle gate reached the expected malformed Calm policy but the resolver hid its precise refusal. | The resolver now replays `_policy_from_scope` for a sole invalid candidate; the failing test and remaining affected request tests pass. |
| The owner-machine benchmark's stale unscoped teardown was rejected by the hardened aggregate Stop protocol. | The benchmark now uses `stop_supervisor(state)`, which snapshots and validates the complete synthetic binding set. No production relaxation was made. |
| PR #150 review found post-commit owner steering had no durable recovery, delivery and finalization failures were conflated, and unexpected adapter failures could escape after the turn committed. | Exact active-scope recovery now retries pending/failed controls; receipt-backed retries are idempotent, finalization failure remains pending, and external exceptions return bounded durable failure evidence. |
| PR #150 review found owner steering reused ordinary delivery and could select an attached watcher despite requiring an exact direct runtime. | Every adapter has a distinct declared steering handler, and owner control uses an explicit direct-target resolver fenced by the captured runtime identity. |
| PR #150 review found per-owner scope, delegate-runtime, and receipt lookups were N+1 and the canonical watcher imported a private obligation helper. | Startup scopes, delegate runtimes, and outbox states are batch-loaded under existing bounds; `obligation_counts` is now a public operation helper. |
| PR #150 review found the benchmark changed its default schema and imported test-only identity constants. | Default `run(samples)` preserves v1 output; `--include-owner-stop` opts into v2, using benchmark-owned synthetic identities. |
| The new P0 unbound Stop regression first failed because absent broker resolution mapped no actor to a supervisor refusal. | Codex, Cursor, and Pi now emit their allow/no-op before terminal-generation or supervisor mutation when exact actor resolution returns none; bound Shotcallers still fail closed and Champions retain their transition gate. |
| Follow-up review found the generic one-shot bit could still authorize a bound attached Shotcaller. | Both canonical and retired JSON compatibility commands now refuse actionably without mutation, legacy stored bits are ignored, and same-generation plus rearmed Stops re-evaluate and block while obligations remain. This deliberately supersedes the baseline one-shot behavior. |
| The first cross-provider rearm regression treated the imported fixture manifest as a clock and raised `AttributeError`; its next run also expected Codex's `decision` shape from Cursor/Pi follow-up adapters. | The test now uses the fixture's canonical timestamp and provider-neutral nonempty-block/empty-allow assertions across Codex, Cursor, and Pi retries. |
| The first unexpected-adapter recovery run left the outbox lease claimed, so the next service attempt recorded `delivery_claimed`. | Definitive `DeliveryUnavailable` releases to bounded retry; unexpected post-send failures instead enter `awaiting_receipt` and cannot resend until exact reconciliation. |
| The first retired-JSON one-shot regression looked for a Shotcaller-scoped state file although its control fixture intentionally uses the root compatibility state. | The test now snapshots the exact root state, proves command refusal makes no change, injects a legacy bit, and proves repeated Stop still blocks. |
| Fresh review of `4b232b8729ef6ec08389dd378b2d50e3c1c8e15d` found an ambiguous adapter failure or process crash after an external pause could be retried without transport-level deduplication. | Owner-control dispatch now distinguishes definitive unavailability from ambiguous post-claim failure. Ambiguous and interrupted in-flight effects durably enter `awaiting_receipt`; recovery never resends them without exact receipt reconciliation, so Stop remains fail-closed rather than duplicating a pause. |
| Fresh review of `fbafdf48196fd5b4d1f33c55182843f699f5d11b` found production `InstalledDeliveryAdapter` collapsed provider response loss back into definitive `DeliveryUnavailable`, so the new ambiguity fence was bypassed. | A distinct `DeliveryAmbiguous` now survives the installed Codex/Pi Herdr operation and Cursor steering paths into durable `awaiting_receipt`; production-path response-loss regressions prove one prompt across repeated recovery for all three runtimes. |
| The first production response-loss fixture reused occupied `Thresh` and failed its callsign uniqueness constraint. | The regression now uses unoccupied synthetic pool callsigns per isolated state; production identity checks remain unchanged. |
| Affected Cursor race and missing-ack tests still expected retryable `pending` after text/input had been applied. | Those post-effect cases now truthfully expect durable `awaiting_receipt`; pre-effect process/input refusals remain retryable and existing no-second-input assertions remain intact. |

No row above involved a live install, Herdr restart, live canonical-state
mutation, real multiplexer effect, or provider call.

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
Issue #84 appends Cursor steering intent/effect receipts as schema 21. It does
not rewrite delivery history or treat a terminal command's exit status as
proof that Cursor accepted the steer.
The same issue appends Pi provider launch, unified-session migration, and
restart-effect receipts as schema 22. It does not alter any earlier migration.
Restart display reconciliation adds no migration. It reconstructs independent
agent and multiplexer adapter selections from the existing schema-22 runtime,
assignment, Shotcaller publication, context-delivery, and Pi launch records.
Schemas 1 through 22 remain unchanged.
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

## Cursor CLI steering provenance

Issue #84 preserves the existing canonical delivery/outbox boundary and adds
one provider-specific effect adapter behind it. The defect was that generic
direct delivery treated every interactive agent as an idle prompt target. That
cannot safely steer a working Cursor CLI: one Enter submits an idle prompt,
while Cursor's working-state interrupt contract is literal text followed by
two Enter keys. An exit-zero input command alone also cannot prove that the
same Cursor session observed the input.

The adapter therefore binds the canonical runtime generation to exact live
Herdr evidence: pane, Cursor session, routing name when present, interactive
readiness, status, revision, state-change sequence, one foreground
`cursor-agent` process, PID, and foreground process group. It uses only
supported direct Herdr commands. It does not scrape a transcript or run a
Python or heredoc command wrapper. Idle/done delivery uses `herdr agent prompt`
for literal text plus one Enter. Working delivery records its intent, repeats
the complete observation, sends literal text, records that phase, repeats the
unchanged observation again, and only then sends exactly two Enter keys.

Schema 21 records prompt length and digest rather than prompt bytes. A retry
after a proved effect returns the same receipt without terminal input. A retry
after a refusal returns the same refusal. A retry after an incomplete intent or
text phase refuses as outcome-ambiguous, preventing duplicate text or a second
interrupt. Wrong pane, replaced session, route/provider mismatch, unavailable
input, missing or ambiguous process, state race, command ambiguity, and absent
post-effect state advance all fail closed.

Routed-request prompts carry a versioned structured envelope plus the complete
`league request accept-routed` argument vector. That stable command derives its
lease and deterministic claim token from current canonical state, so the
Cursor Shotcaller does not reconstruct internal timestamps or claim details.
`league delivery dispatch` exposes the state-aware provider selection through
the stable CLI.

Focused synthetic installed-adapter tests cover idle submit, working steer,
the pre-interrupt state race, duplicate retry, wrong pane, replaced session,
ambiguous/missing process, unavailable input, post-steer acknowledgement, and
idempotent routed acceptance. The migration test proves schema-20-to-21 crash
rollback and exact retry. This source candidate does not install League, steer
a live Cursor process, replay an event, replace a Champion, clean up a runtime,
or claim owner-visible acceptance.

## Pi provider lifecycle and unified-inventory provenance

The live integration failure that triggered the schema-22 slice had four
separate causes. A restored Herdr layout did not restart Pi. The runtime record
did not receive Pi's exact session path. Pi's native late title overwrote the
canonical callsign/task presentation. Finally, three Cursor-backed Pi sessions
were absent from plain Pi's All-session resume view.

The last failure came from a temporary wrapper created by Kuma. It selected
Cursor correctly but also set a provider-specific Pi home. That turned a
provider choice into a second session pool, so sessions written there were
invisible to the standard unified Pi inventory. The wrapper has since been
deleted. This candidate has no wrapper dependency and never sets
`PI_CODING_AGENT_DIR` or `--session-dir`; Cursor and Codex are provider
arguments to one Pi runtime and one inventory.

The durable launch descriptor records runtime, provider, model, effort, exact
cwd, role, placement, callsign, project code, two-word task label, routing
name, session mode, lineage, and release/state roots. A new project fork can
occur once for an exact parent path plus cwd. Restarts use the bound child
session path, never fork again. One restart intent/effect key prevents a retry
from starting a second process or session.

Pi's trust prompt is treated as pre-activation. League first verifies the
assigned worktree as the exact repository root and only then supplies Pi's
scoped `--approve` input for that cwd; it does not change global trust. The Pi
extension reports canonical metadata only after native `session_start` exposes
an absolute session file and ID. Activation requires exact cwd, one Pi process,
provider/session/descriptor metadata, canonical title, and two consecutive
stable readbacks. A late native `π - session - folder` title therefore cannot
win the activation race.

For legacy sessions, migration is permitted only when the exact restored Herdr
pane is shell-only. League reads the first JSONL record, verifies the parent
session's first record when present, bounds the unified inventory scan, rejects
duplicate IDs or changed bytes, and creates a mode-0600 destination with
exclusive create and fsync. It never rewrites the JSONL or parent path. Crash
retries recognize the same digest and finish the descriptor bind without
creating a second identity.

Implementation failures were kept as regression evidence. The first focused
test used a private SQLite timestamp helper that the storage facade does not
expose; the implementation now uses a local strict RFC3339 validator. The
first late-title test injected the native title into the start-command receipt
instead of the first live readback; the fake now models the real ordering. The
schema-21 rollback test initially advanced through the new schema-22 target;
it now pins its intended target while schema 22 has its own crash/rollback
test. None of these failures changed live Pi sessions.

The controlled three-session acceptance found additional provider-faithful
boundaries. Unified Pi initially had no Cursor credential even though the
provider documents automatic desktop/CLI discovery; a no-session canary failed
closed, then the supported `/login cursor` flow succeeded and the canary passed.
Herdr represents a shell-only pane as one shell process, not always an empty
process list, so the guard now accepts only the exact shell PID/process group,
known shell executable, and stored cwd. Pi rejected the first extension load
because a strict-mode local binding used the reserved name `arguments`; the
binding was renamed and an explicit extension-load smoke was added.

Restored panes also do not propagate League or pane environment into the new Pi
child. Every required launch field, including the exact pane, is now a Pi CLI
extension flag. Herdr metadata retains higher-priority legacy token names and
bounds token/path length, so launch proof uses collision-free `launch_*`
metadata, native session paths when available, and SHA-256 for paths and parent
lineage. A first parent-digest key exceeded Herdr's supported key length and was
shortened. A nanosecond metadata sequence exceeded Herdr's safe numeric range;
the adapter now matches Pi's microsecond-scale sequence.

Two attempted metadata commands used a dotted name and then a command added
after an explicit-extension reload. Pi treated each as a model prompt because
dotted names are not parsed as slash commands and `/reload` does not retain a
new CLI-explicit extension command in that already-running process. Both turns
were interrupted after read-only discovery commands; the issue worktree stayed
clean. Exact restart reconciliation now lives in the launcher, not a reload
command. Herdr also accepted but did not expose a supplemental native session
report for one already-detected legacy pane. For exact resume only, League uses
the byte-bound durable path plus launch path digest and exact process/cwd; fresh
create/fork still requires Herdr's native absolute session path.

Final live acceptance copied the three stopped JSONLs byte-for-byte into the
unified inventory, preserved both Champion parent paths, showed all three in
plain `pi --resume` under All, resumed the exact stored children and cwd, and
returned effect-applied receipts. Repeating all three restart IDs returned the
stored receipts with unchanged foreground PIDs. The clean issue-215 worktree
remained clean, and the issue-190 dirty/untracked inventory remained exact.
This is a Pi provider installation and live source-candidate acceptance; it is
not a League merge or League installation claim.

Independent exact-head review then found five pre-landing contract gaps. The
Pi runtime receipt used its UUID where Herdr cleanup reads the provider-native
JSONL path; the receipt now binds `thread_id` to that path while reporting both
path and UUID. The routed Cursor action omitted the required state root; the
emitted argv now includes it and the test executes that exact argv. Restart
did not revalidate its stored cwd before supplying scoped approval. The first
fix only required a canonical Git worktree and still admitted a different
repository recreated at the same path. The descriptor now binds the canonical
repository root and exact `.git` marker filesystem identity; restart must
reproduce that digest before any resume. The
inventory scan returned on its first matching UUID and could miss a later
duplicate; it now completes the bounded scan and refuses more than one match.
Finally, terminal Cursor status `done` was treated as idle; steering now
refuses it without an input effect. Focused regressions cover all five fixes.

A final pre-landing pass found two Pi retry defects that the earlier fresh-
launch acceptance did not exercise. An already-active launch could reuse the
correct pane but context verification discarded its stored tab and terminal
IDs, so the idempotent assignment retry failed after prompting. Context
verification now reloads and fences the complete active endpoint before its
readback. Pi launch selection also performed the Codex continuation lookup
before choosing the runtime branch, even though Pi owns an explicit session
descriptor. That lookup now exists only in the Codex branch. The focused Pi
provider and visible-launch suites cover the replacement behavior, and the
full repository gate passes without touching a live Pi process.

Installed 0.2.39 acceptance then exposed four source-only blind spots. The
release manifest did not include the Pi extension or sandbox profile, the
exact resume command had no Herdr-startup caller, migration rejected sessions
already stored in the unified inventory, and restart applied Champion Git
worktree proof to Shotcallers intentionally rooted at a non-Git project
folder. The corrective release stages both integration files explicitly,
allows byte-identical in-place adoption only at the same shell-only boundary,
keeps exact Git identity mandatory for Champions, and binds a Shotcaller to
the exact project-directory device and inode. Launch metadata now carries the
durable descriptor ID and state root required by the Herdr startup plugin;
the plugin derives one generation from the live socket identity and invokes
the existing effect-fenced resume command. Focused acceptance covers the
installed file manifest, already-unified adoption, role-aware cwd binding,
and repeated same-generation recovery. A live three-agent restart remains the
final installed gate.

The same install found a macOS pointer-update hazard: moving a temporary
symlink onto a stable symlink whose target is a directory followed the target
instead of replacing the stable link. The install removed only that temporary
artifact and used an atomic path replacement. Future installers must verify
the stable link target after every switch and must not use directory-following
`mv` semantics for release activation.

The final single-profile adoption correction preserves Pi's immutable child
history without reviving the retired provider-specific inventory. Two existing
Champion children still contain their historical parent path, while the exact
parent JSONL already exists in the unified inventory under the same filename.
Migration manifest v2 therefore binds a separately verified parent-evidence
path and digest inside the unified root. League validates its regular-file
identity, containment, filename, digest, and session UUID, but leaves the child
bytes and embedded parent path unchanged. Root sessions cannot declare parent
evidence. A proposal to recreate the retired profile path with filesystem links
was rejected because it would violate the one-active-profile contract; no such
path is created by this release.

The first 0.2.41 installation staging attempt also refused before activation
because the prior installed Python release had accumulated bytecode cache files.
The repository-local launchers previously depended on the caller to suppress
bytecode, and the staged acceptance environment accidentally supplied that
setting, hiding the installed-shape defect. Both League launchers now suppress
bytecode themselves. Acceptance removes the masking environment for launcher
checks, executes both launchers, and then compares every post-execution staged
file and digest with the source-owned release manifest. The rejected staging
directory was never activated and contained no canonical data.

The first adopted Shotcaller resume then exposed a Herdr metadata-capacity
boundary that the fake adapter did not model. The new descriptor digest selected
a second League metadata source while the old source already contributed enough
tokens to reach Herdr's limit. Pi started with the exact session, but the
post-start metadata report refused before the restart effect was committed.
The same intent was preserved and completed without starting another process by
reusing the owner-verified prior source and clearing only redundant League-owned
legacy runtime/session tokens. The permanent launcher now derives that source
only from matching Pi routing metadata, passes it explicitly to the Pi extension,
and publishes a reduced non-duplicative token set. Native Pi session identity,
toolkit presentation tokens, JSONL bytes, and Job Journey state remain unchanged.

## Provider-neutral restart display provenance

A real named Herdr restart restored the same Codex/Pi sessions, panes, working
directories, and process identities without duplicates, but discarded every
League display token. The sidebar's role/provider/title fallback matched its
missing inputs. A display-only replay restored all four named presentations,
which isolated ownership to League restart replay rather than the renderer.

The first source candidate incorrectly introduced a schema-23 duplicate
presentation store and a nonexistent blocking startup-barrier dependency.
Owner correction removed both before publication. The final core reconciler
selects both registries without provider or Herdr command strings, reconstructs
presentation from existing canonical records, binds a newly restored terminal
to the exact native session/cwd/routing name and one foreground process, and
advances the stable League metadata source from the observed native sequence.
The Herdr adapter reports at most 16 tokens per call and requires two stable
readbacks. An exact retry observes convergence and performs no report; a missing,
replaced, duplicated, or mismatched session refuses without launching a process.

Herdr's supported `[[startup]]` hook runs asynchronously after session restore
and API readiness. The bundled plugin uses that one-shot hook directly. A brief
fallback display is therefore expected and accepted; eventual exact convergence
is the contract. Hook failure remains visible in Herdr's plugin command log but
does not cause League to target a best guess. Disabling the plugin retains
ordinary Herdr startup. This repository does not install, patch, restart, or
steer the live Herdr server, and the focused restart regression uses only
synthetic canonical state and a fake Herdr adapter.
The full repository gate also exposed an older help assertion that omitted the
already-merged `continuation` command while the parser correctly advertised it.
Only that expected command inventory was updated; CLI behavior is unchanged.

## Issue-#84 adapter and routing completion

The final #84 repository candidate keeps provider selection out of the command
facade. `assign run` asks the registered Codex, Pi, or Cursor-CLI adapter for
its visible-launch driver; the dedicated adapter folder validates native
create/resume/provider inputs. Multiplexer placement, discovery, routing,
metadata, delivery, and close effects likewise flow through the multiplexer
registry. Herdr advertises those concrete operations; tmux advertises none
until a callable native implementation lands. Shared contract tests require a
callable method for every advertised capability.

Ordinary Champion launch now defaults to Pi+Codex. It consumes exactly one
persisted `ModelRouter` decision and verifies the bound request/task/assignment,
Champion role, selected provider, required capabilities, and selected state.
Model and effort are optional CLI inputs only as an exact paired override.
Explicit runtime/provider overrides remain exact, including Pi+Cursor. The
schema-3 release policy retains Sol/xhigh as the unevaluated strong-worker
baseline. A bounded, idempotent migration installs retained schema-1/2 policy
only with an explicit destination and backup; rollback is digest-fenced. No
install or migration was applied to user state in this lane.

The shared pre-tool decision seam is implemented here for all three agent
adapters. Issue #81 remains the owner of autonomous authorization evidence and
its installed hook policy; #84 neither fabricates authorization nor duplicates
that producer. The public restart entrypoint is `runtime
reconcile-restored-agent`; `replay-restored-display` remains a compatibility
step inside that operation.

Focused verification exposed and resolved only repository-local or synthetic
failures:

| Failure | Resolution |
| --- | --- |
| The script-style tests were first invoked through unittest discovery and reported zero tests. | Re-ran each file through its supported direct Python entrypoint. |
| The Shotcaller adapter refactor initially referenced the former harness option name. | Bound identity and presentation to `runtime_kind` and added Codex, Cursor, Pi+Codex, and Pi+Cursor cases. |
| Multiplexer test doubles did not accept the production runner timeout keyword. | Kept the production runner contract explicit and updated the synthetic doubles. |
| A routing-aware Roster fixture omitted its required assignment role. | Added the exact synthetic Champion role before asserting the persisted decision. |
| A restored Cursor fixture inserted its runtime before the canonical agent row. | Corrected fixture order; production foreign-key behavior was unchanged. |
| A synthetic metadata-effect error exceeded the bounded report chunk. | Reduced only the fake error text and retained the production limit. |
| The new adapter-factory fixture used a hyphenated fake Herdr workspace ID rejected by the production identity grammar. | Replaced it with a valid synthetic `w...` identity; production validation was unchanged. |
| The restricted test sandbox denied creation of the supervisor Unix socket. | The same focused suite is rerun with only temporary-directory socket permission; no live service or endpoint is used. |
| The missing-supervisor fixture retained two verified Shotcaller runtimes, so the exact-binding guard refused before the intended Stop assertion. | Closed only the fixture's obsolete second runtime, matching the already-established delivery fixture setup. |

No installation, live Herdr restart, live agent discovery, prompt, steering,
cleanup, migration, or cutover is claimed by these source and synthetic tests.

The independent #84 repository audit found that the first generic replacement
candidate persisted only launch and completion receipts, leaving process-crash
gaps around successor creation, route promotion, and predecessor retirement.
Schema 23 now records an intent state before every external effect, fences task,
agent, and pre-tool mutation while the operation is open, adopts only one exact
staged successor, and compensates verified post-switch failures. Synthetic
faults cover interruption after launch, both route renames, physical retirement,
and each canonical receipt commit. Pi descriptors settle to one resumable owner,
and the service layer dispatches the successor handoff exactly once after the
predecessor retirement receipt commits.

The same audit found that public Pi resume and migration commands selected Herdr
directly while the capability matrix claimed a neutral seam. Both commands now
resolve `provider_session_lifecycle` through the multiplexer registry. Herdr
owns the current implementation; tmux advertises no such capability and refuses
before reading a migration manifest or applying a process effect.

The final exact-head audit found four additional source-only gaps before
publication. First, a missing successor receipt could be mistaken for proof of
absence after an ambiguous crash-gap discovery. Unknown identity now records an
open recovery obligation and retains the mutation fence; only explicit native
absence or verified cleanup can roll back. Second, direct Cursor CLI had not
participated in the A-to-B transaction matrix. The registered Cursor adapter now
covers predecessor and successor success, launch-crash recovery, post-switch
compensation, retirement, and exactly-once handoff. Third, the canonical
pre-tool hook path now proves the same open-replacement refusal for Codex, Pi,
and Cursor. Fourth, Pi owns its bounded descriptor storage transaction while
core validates the exact operation, assignment, participant, and source adapter
before invoking it atomically. Cross-assignment, cross-adapter, and
cross-operation probes refuse before an adapter callback; no non-Pi descriptor
lifecycle is claimed. Launch-gap recovery also retains the replacement fence
when no exact staged successor can be bound, because absence of a routing name
does not prove that a pre-start pane or tab was never created.

PR #136 merged the independently audited #84 candidate at tree
`71a2f71b9eb1a226a7a7c6c2c3346f3c4fcd70d0`. Release `0.2.44` assigns the
next unallocated immutable install identity to those provider-lifecycle bytes;
the release delta changes only the version contract, its deterministic staging
expectations, and this provenance record.

Release `0.2.45` accepts the retained minimal schema-1 routing shape produced
by the historical installer (`schema` plus exact tier selections). The
migration preserves those tiers, supplies the conservative schema-3
`WORKER_STRONG` policy and unevaluated fast-tier evidence, rejects unknown or
malformed fields, backs up the original bytes, and retains exact rollback. It
does not silently select Luna for the strongest tier.

Release `0.2.46` assigns the next immutable install identity to merged PR #126
at source tree `67d0fd8b23e8cf1e2e2e5b1d647282b64f8ae978`. It adds the
OS-manager-owned multi-Squad watcher service, repeated attached Stop blocking,
verified detached handoff, independent Calm and attachment policy, watcher
takeover fencing, exact restored-runtime rebinding, and durable exact-once
delivery. Installation and live Herdr restart acceptance remain separate,
receipt-bearing cutover operations.

Release `0.2.47` corrects the self-contained release manifest exposed by the
0.2.46 install preflight: the hash-bound launchd template is now staged and
verified beside the watcher binary. The operator no longer needs an issue
worktree or any source outside the immutable installed release to run
`service-install`.

Release `0.2.48` corrects the live launchd environment exposed by the first
0.2.47 service-install attempt. The rendered LaunchAgent now binds the exact
canonical writer pointer and puts the installer's validated Python interpreter
directory first on its bounded `PATH`, preventing launchd from selecting the
system Python with an SQLite runtime too old for the canonical WAL database.
The ordinary launcher contract remains unchanged, and a clean-environment
integration regression proves that the rendered service invokes the canonical
multi-Squad watcher.

Release `0.2.49` makes that failed installation recoverable through the same
supported installer. A retry may replace a `rolled_back` manifest only after
proving that launchd is unloaded, no unmanaged watcher is live, and the exact
prior plist and rollback backup bytes are still restored. Both absent-prior and
existing-prior cases prove failed-start rollback, retry, live startup, and a
second exact rollback without manual file or database edits.

Release `0.2.50` assigns the next immutable install identity to PR #144's
reviewed source tree `0037b95d4c2a73a98c312f630d0172d41b4bb36d`. Aggregate
service Stop validates the complete actor, watcher, fence, runtime, and runtime-
generation set, while start/restart and idempotent install revalidate exact
executable/template bytes after liveness and stop the service before refusing
observable check/use drift. The release preserves 0.2.49 launchd runtime
selection and exact rolled-back-install recovery.

Issue #127 separates total retirement of an already-stopped Champion from the
existing destructive cleanup plan. Migration 24 stores one exact operation and
absence-proof receipt. Agent and multiplexer registries own provider and native
inventory semantics; core has no Cursor-, Pi-, Codex-, Herdr-, or tmux-specific
retirement branch. One bounded immediate SQLite transaction revalidates the
identity, inspects exact Herdr pane/process state, and closes
the stale runtime, terminalizes and retires the Champion, removes only its Squad
membership, and releases its callsign. Repository coordinates and bytes are
never cleanup inputs or effects. Exact retry after restart is receipt-only;
identity drift, untransferred ownership, live/ambiguous endpoints, and
unsupported pairs refuse without partial mutation.
Supported canonical launch/resume writers cannot interleave with the proof;
orphan provider processes, unstructured absence, and oversized proof or identity
values fail closed. Provider aliases are normalized to canonical adapter
identity, and indexed lookups bound active callsign/assignment checks.

Release `0.2.51` assigns the next immutable install identity to merged PR #146
at main commit `8778dc2982903807151bc4b3f5b1f172afeb5836` and reviewed source
tree `db5463ffe123a5644e51a1d6794eeda7c9644929`. The release preserves the
schema-24 stopped-agent retirement behavior exactly; its delta changes only the
version contract, deterministic release-staging expectations, and this
provenance record.

Installed 0.2.51 restart acceptance then refused before display replay because
legacy Shotcaller agent rows do not carry repository or worktree coordinates,
while replay tried to derive their project code from `agent.repository`.
Shotcaller presentation now uses the existing verified bootstrap publication as
its canonical restart cwd and presentation source when the durable agent cwd is
absent. An existing non-empty durable cwd must match the publication exactly or
replay fails closed. One exact active Squad project supplies the project label
when present; a global Shotcaller with no project association uses the
publication worktree basename. Multiple or uncoded project links and missing,
malformed, or identity-mismatched publications fail closed with bounded
agent/runtime diagnostics. Replay accepts
both the current agent-scoped Shotcaller assignment and the legacy Squad-scoped
shape only when that Squad is active and owned by the same exact agent. Champion
presentation remains task-to-project plus exact launch worktree based.
Synthetic Ashe, Azir, and Qiyana restart regressions cover the global, IA, and
JJ labels and both assignment shapes with zero process creation or session
resume.

Release `0.2.52` assigns the next immutable install identity to merged PR #148
at main commit `6b660c08d0d5975d72ee7ba2ce334b9bfbdd4f79` and reviewed source
tree `5731a4ec4231f8136743030eef8911de75477480`. It packages the corrected
restored Shotcaller publication-cwd and project-label behavior without another
behavior change; this release delta changes only the version contract,
deterministic release-staging expectations, and this provenance record.

Release `0.2.53` assigns the next immutable install identity to merged PR #150
at main commit `5ade2ee432a707981764c3951ce6862adb4c14df` and exact reviewed/merged
tree `32fbdf9ad6eb9373dec85a794f81ee588d153dfc`. It packages the
provider-neutral semantic owner-Stop control, retired generic one-shot bypass,
and ambiguity-fenced delegated steering without another runtime behavior
change. This release continuation aligns the source-managed installed League
supplement with that merged contract: prompt intake activates only after exact
canonical binding and never backfills, unbound or unverifiable runtimes remain
untouched and unrecorded, attached obligations always block Stop, and detached
handoff requires an exact durable watcher receipt. The release delta is limited
to that normative source policy, its semantic regression, the version contract,
deterministic release-staging expectations, and this provenance record.

The issue-#84 provider-hook follow-up adds one registry-declared installation
contract for Codex, Pi, and Cursor CLI. Codex and Cursor retain their native
profile hook JSON shapes; Cursor is now an explicit installed bootstrap rather
than an implicit core branch. Pi declares `integrations/pi/league-hooks.mjs` as
its release asset and installs those bytes as the discoverable profile entry
`league-hooks.ts`; Pi does not auto-discover `.mjs` profile entries. Its envelope
asks the existing `pi-input-hook`, `pi-pre-tool-hook`, and `pi-stop-hook`
commands to prove canonical binding atomically with the hook action. A
locally persisted receipt activates only the exact Pi session ID and absolute
session path after that bound proof. Ordinary unregistered Pi remains usable
and mutation-free when League is unavailable; once activated, the same outage
fails prompt, mutation, and Stop safely. A later bound event promotes the
running session without a Pi relaunch. The launch extension retains only
launch-scoped sandbox and presentation behavior, preventing duplicate lifecycle
handlers.

Codex and Cursor commands validate their provider-native envelopes directly;
callers never supply a fabricated authorization or bootstrap field. Codex
`PreToolUse` produces Codex's native permission decision, while Cursor uses
generic `preToolUse` and produces Cursor's native permission object so file,
task, and MCP tools cannot bypass the shared policy. All three adapters resolve
an unbound native session to an immediate provider-native allow/no-op before
prompt quarantine or supervisor ownership. A focused real-socket regression
keeps an aggregate supervisor live while unbound prompt, pre-tool, and Stop
events for all three providers complete within the hook deadline with no
canonical-table change.

`league provider-hooks upgrade` is the supported one-time release-install step
for an existing provider profile. It derives the complete target inventory from
the adapter registry, validates candidate bytes before mutation, writes an
exact prepared manifest and backups, installs all targets, verifies every
result, and rolls the set back on failure. `provider-hooks rollback` restores
that manifest exactly. Repeating either settled operation is effect-free;
prepared crash recovery restores the prior profile before returning. Staged
release acceptance runs upgrade, rollback, and a repeated active upgrade in a
disposable profile.

This follow-up consumes the stable Stop response only. Issue #66 remains the
owner of canonical Stop, supervisor, and rearm semantics; changes in that issue
must preserve the adapter hook output contract rather than be duplicated here.
Exact-head review found that native hook commands did not quote an absolute
watcher path, malformed Codex hook groups could escape the canonical refusal,
the Pi idempotence check could read an unbounded existing target, and one fixed
temporary name made a stale or concurrent installer block valid publication.
The corrected installers shell-quote native command arguments, reject malformed
groups without mutation, compare Pi targets with a bounded read, and use a
unique same-directory atomic-write temporary file.
The repair pass also found these acceptance failures before publication:

| Failure | Resolution |
| --- | --- |
| Native Codex and Cursor hook payloads lacked the fabricated bootstrap and authorization fields expected by the first draft. | Adapter-owned translators now establish provenance from the installed command, validate exact native input, and render native allow or deny output. |
| An ordinary unbound Codex prompt reached aggregate-supervisor ownership resolution and failed with `supervisor_ownership_uncertain`. | Unbound actor resolution now returns provider-native no-op before supervision ownership; the real aggregate-socket parity test covers prompt, pre-tool, and Stop for Codex, Cursor, and Pi. |
| A globally loaded Pi extension could consume ordinary prompts during a watcher outage. | Only a durable exact-session activation receipt selects managed fail-closed behavior; unregistered sessions remain inert. |
| Existing installations had no atomic way to add the full Codex, Cursor, and Pi hook set. | The registry-derived provider-hook upgrade/rollback command and disposable staged-install acceptance now cover that release step. |
The candidate changes source, synthetic installers, and immutable release
manifest bytes only. It does not edit the active Pi profile, install a release,
restart Herdr, or mutate live League state.

Release `0.2.54` assigns the next immutable install identity to merged PR #153
at main commit `dbffad2b8ef3b2d9b75a7d3ad0d18b628b338ec0` and exact
reviewed/merged tree `9de3c1fb5ce24c09f9960c33351abeb642be54fb`. It packages
provider-native Codex, Cursor CLI, and Pi hook bootstraps behind the shared
adapter registry. Unbound or non-League prompt, pre-mutation, and Stop events
return provider-native allow/no-op output before supervisor ownership checks
and make zero canonical mutations; exact canonical binding activates the same
installed hooks. The release also packages bounded, symlink-safe hook upgrades
with exact rollback, unlimited Cursor Stop continuation, and real ordinary-Pi
exact-session resume acceptance. This release delta changes only the version
contract, deterministic release-staging expectations, and this provenance
record beyond the reviewed merged tree.

The issue-#84 completed-display follow-up corrects one retained-session status
boundary discovered during installed restart reconciliation. Herdr reports an
interactive, still-present Codex session as `done` after its model turn ends;
that state does not mean the pane, terminal, thread, worktree, or provider
session is absent. Owner-authorized legacy display reconciliation therefore
accepts `done` only after the same exact endpoint, thread, worktree, route,
source, sequence, active assignment, verified runtime, and acceptance-receipt
checks used for other present states. A genuinely stopped endpoint remains
ineligible. The focused regression proves a retained `done` Champion receives
one durable display receipt while a stopped or source-less presentation still
fails before mutation.

Release `0.2.55` assigns the next immutable install identity to merged PR #156
at main commit `e1f8d58868d588bb60bb3272d10d34889656ef46` and exact
reviewed/merged tree `ad563e6c7492245ff2e0129a3f9754b65c221cc2`. It packages
the retained completed-Champion display reconciliation correction without any
additional runtime, hook, watcher, provider, multiplexer, or storage-contract
change. This release delta changes only the version contract, deterministic
release-staging expectations, and this provenance record beyond that merged
tree.

The final issue-#84 live acceptance exposed one Herdr projection boundary:
`agent get` can omit `metadata_source` while the installed tab-status plugin is
the active display owner. The legacy adapter keeps the immutable native session
source as its baseline presentation identity, but recognizes the tab-status
owner only from that plugin's complete identity-token tuple and uses
`local.tab-status` for the guarded `applies-to-source` write. Partial or
conflicting tuples fall back to the native source and remain fail-closed. The
owned overlay records its exact authority for retry and verification.

Release `0.2.58` assigns the next immutable install identity to merged PR #162
at main commit `1052e4d1649aa0362ca901138fcec8422a212dbf` and exact
reviewed/merged tree `21d065c0e096bee538fcbff88b4deabc4258d5e0`. It packages
the Herdr presentation-authority correction without any additional hook,
watcher, provider, multiplexer, or storage-contract change. This release delta
changes only the version contract, deterministic release-staging expectations,
and this provenance record beyond that merged tree.

The installed Herdr projection keeps an agent's global `state_change_seq`
unchanged for display-only `report-metadata` updates and advances pane
`revision` instead. Legacy display acceptance therefore permits exactly two
stable sequence projections: the owner-authorized baseline value used by
current Herdr, or baseline plus one used by the compatible synthetic/legacy
projection. In both cases the dedicated League source, complete ownership
tokens, exact title, exact underlying presentation authority, and two stable
readbacks remain mandatory; any other sequence still refuses.

Release `0.2.59` assigns the next immutable install identity to merged PR #164
at main commit `4053a7b01ba22f880b2ea9514979f483633a19e8` and exact
reviewed/merged tree `6d44d6010c7d6dccaf3c9b4bc13d69ca2e1ee12f`. It packages
the current-Herdr display-only sequence projection correction without any
additional hook, watcher, provider, multiplexer, or storage-contract change.
This release delta changes only the version contract, deterministic
release-staging expectations, and this provenance record beyond that merged
tree.

The issue-#84 legacy-restart follow-up handles the exact retained-session shape
found during the first installed reconciliation. One verified Vi pane and
immutable Codex thread remained live in a clean follow-up worktree after its
original acceptance worktree had been preserved. The existing owner-authorized
display repair now accepts an optional, all-or-nothing predecessor worktree and
branch tuple. Its durable v2 intent preserves that predecessor identity; final
display acceptance and the current `agent_instances` worktree/branch update
commit in one transaction and increment the agent CAS version exactly once.
Incomplete tuples, a wrong predecessor, an endpoint race, and repeated effects
still fail closed. The original assignment acceptance receipt remains immutable.

Current Herdr agent inventory can also omit the derived `metadata_source` and
`display_agent` fields while retaining the exact native session source and
League-owned presentation tokens. Legacy repair derives only its pre-effect
source from that immutable native session, then derives its owned post-effect
source from the exact reconciliation token. General restored presentation
verification accepts an omitted derived field only when one unambiguous owned
source token and the exact `display_provider` token match the canonical
presentation. Explicitly present but empty or conflicting fields remain a hard
failure. Focused tests cover source-less Codex, Cursor, and Pi reconciliation,
stable idempotent retry, and the predecessor-to-current worktree transition.

Release `0.2.56` assigns the next immutable install identity to merged PR #158
at main commit `8bbf3105244813cc436034b42c41671218715cdd` and exact
reviewed/merged tree `5fea15065dec14b1c192fc5620084a945f3fc9fd`. It packages
the atomic legacy Champion worktree reconciliation and source-less Herdr
presentation verification correction without any additional hook, watcher,
provider, multiplexer, or storage-contract change. This release delta changes
only the version contract, deterministic release-staging expectations, and
this provenance record beyond that merged tree.

The issue-#84 live legacy repair also treats a restored Herdr terminal
generation as part of the same owner-authorized transition. The immutable
assignment acceptance receipt continues to identify the predecessor runtime
generation. A v3 reconciliation intent binds that exact predecessor to the
generation derived from the verified live terminal and immutable provider
thread. Final presentation acceptance updates the runtime generation and the
current Champion worktree/branch in one SQLite transaction; collisions,
partial tuples, stale generations, and endpoint races still fail closed, and
an exact retry is effect-free.

Release `0.2.57` assigns the next immutable install identity to merged PR #160
at main commit `bade8dae760ca841fe2bfd022cacbd9498e0e93a` and exact
reviewed/merged tree `210d07ab837f23579cdd6cc0aff48e839645121e`. It packages
the restored-terminal runtime-generation reconciliation correction without any
additional hook, watcher, provider, multiplexer, or storage-contract change.
This release delta changes only the version contract, deterministic
release-staging expectations, and this provenance record beyond that merged
tree.

The issue-#84 restored-agent pass now consumes an exact legacy reconciliation
receipt as the Champion's canonical display receipt when the original launch
predates display receipts. Both the durable parser and replay path accept
Herdr's display-only behavior, where the exact pane observation is updated while
the workspace state sequence remains at the owner-authorized baseline. Final
worktree reconciliation compares physical path identity before using the exact
stored path in its SQL compare-and-set, so macOS `/var` and `/private/var`
aliases cannot create a false conflict. The combined transition-and-replay test
is registered in the focused suite and verifies immutable predecessor evidence,
the restored runtime generation, idempotent retry, and canonical replay.

Release `0.2.60` assigns the next immutable install identity to merged PR #166
at main commit `768089227a36583f052f62c45f777eac7feb7d6d` and exact
reviewed/merged tree `6d3b91176a25c24965b32ccbffe74b236a9805e5`. It packages
the restored legacy Champion receipt replay and physical-worktree CAS repair
without any additional hook, watcher, provider, multiplexer, or storage-contract
change. This release delta changes only the version contract, deterministic
release-staging expectations, and this provenance record beyond that merged
tree.

The issue-#84 legacy display repair now supports the ordinary restored-session
case where Herdr assigns a new terminal identity while the Champion remains in
the exact same worktree and branch. A v4 durable intent binds the immutable
acceptance generation to the verified restored generation without fabricating
a worktree transition or incrementing the agent CAS version. Final runtime and
display acceptance remain one transaction, and retry remains effect-free.

Release `0.2.61` assigns the next immutable install identity to merged PR #168
at main commit `f281dea3709928b5c262fe4ead7b95e220a6fd29` and exact
reviewed/merged tree `99c2551416e099f34f56c9c831e10070dc762700`. It packages
the same-worktree restored-generation reconciliation repair without any
additional hook, watcher, provider, multiplexer, or storage-contract change.
This release delta changes only the version contract, deterministic
release-staging expectations, and this provenance record beyond that merged
tree.

The final issue-#84 live pass found that `local.tab-status` is intentionally a
volatile presentation writer: status-icon refreshes advance that metadata
source even when the native Codex session is unchanged. Legacy reconciliation
therefore anchors its ownership tokens to the immutable `herdr:codex` session
source, publishes no pane title of its own, and asks the installed status plugin
to render the exact identity tokens. The adapter accepts the target only when
the rendered pane title and the complete owned token tuple agree. This
supersedes the earlier `local.tab-status` authority choice without changing
already-finalized receipts, which retain and verify their recorded authority.

Release `0.2.62` assigns the next immutable install identity to merged PR #170
at main commit `c0952c96358e8476e89710d3010405d5bf19ce63` and exact
reviewed/merged tree `9e0fbc61dd6aaae956dc1ab07522c601077fcfd6`. It packages
the stable native-source display reconciliation and token-only status-renderer
handoff without any additional hook, watcher, provider, multiplexer, or
storage-contract change. This release delta changes only the version contract,
deterministic release-staging expectations, and this provenance record beyond
that merged tree.
