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
