# Reversible migration boundary

## Bootstrap rule

Issue #2 establishes source ownership without changing installed behavior. No
global file, hook, live Roster record, callsign pool, watcher state, endpoint,
worktree, or repository ref is migrated by the local test command.

## Inventory

The imported baseline is refreshed to toolkit merge
`93635786746b1d2bea21cca7d276e2106aa99fb5`. Its runtime, launcher,
orchestration reference, and shared guide match the currently installed copies
at the hashes recorded in `docs/PROVENANCE.md`.

| Current toolkit surface | Bootstrap disposition | Reason |
| --- | --- | --- |
| `agent_watcher.py` | Imported byte-for-byte as `src/agent_watcher.py` | Includes the released launch/preflight, routing/display identity, local-install proof, and later-main squash-proof baseline. |
| `agent-watcher` | Imported as `bin/agent-watcher`; repository-relative source path adapted | Keeps stable symlink resolution while matching the new layout. |
| Focused watcher/lifecycle/delivery/record/reconciliation tests | Imported under `tests/`; paths and historical fixture identity sanitized | Covers launch/preflight, routing/display identity, local-install proof, and squash proof without real user or repository data. |
| Roster and routing examples | Re-authored as synthetic examples plus authoring schemas, including the optional paired routing/display fields | Avoids carrying historical runtime identity into public bytes while documenting the released record surface. |
| `install-agent-watcher` and Zsh completion | Retained in `terminal-environment-toolkit` | The bootstrap must not change live installation or shell integration. |
| Global agent guide and orchestration reference | Refreshed in the provenance inventory but retained in their current owner | A repository bootstrap must not silently replace installed policy. |
| Scientist, Lead, resource, hook, and watcher state files | Not imported | They are live or installation-time configuration, not source fixtures. |
| Historical evidence and canary output | Not imported | Provenance uses commits and hashes; machine-generated evidence is unnecessary. |

## Later installation gate

A later issue may transfer installation ownership only after it:

1. verifies the exact released source revision;
2. renders an isolated install into a temporary prefix;
3. proves wrapper, runtime, completion, schemas, guide, and hook parity;
4. backs up every replaced destination;
5. installs exact released bytes without overwriting user-selected config;
6. runs safe Herdr and tmux smoke checks where required; and
7. prints and tests the exact rollback procedure.

Until then, rollback and installation remain the toolkit's responsibility. This
repository supplies no command that writes global state.

## Repository-local import contract

Issue #19 implements a non-live, explicit-root migration surface:

1. `league storage migrate` creates or upgrades only the state root named by
   the caller. Upgrading an existing schema requires a collision-free verified
   backup inside that root.
2. The import manifest explicitly lists every canonical source family:
   Roster pairs, pending launches, scoped/global watcher state, visible and
   hidden callsign pools, Lead relay receipts, and resource registries. Empty
   lists state absence; no home-directory scan or guessed dynamic path exists.
3. The manifest separately lists retained archive/evidence/config files and
   unknown consumers. Retained bytes are checked and hashed but never imported;
   any unknown consumer blocks.
4. Dry-run strictly validates UTF-8, duplicate keys, JSONL termination,
   snapshot/event parity, exact identity, source offsets/digests, ordering,
   leases, delivery ownership, references, and target collisions. It reports
   exact artifact/row counts and one deterministic digest without local paths.
5. Apply requires that exact digest, recomputes it from the plan, rechecks an
   empty target, and inserts all canonical rows in one transaction. A failure or
   injected crash leaves the target empty. Legacy files are never modified.
6. `storage integrity`, verified Online Backup API snapshots, redacted
   inspection export, and mode-`0600` rollback export supply bounded recovery
   evidence. Exports are explicitly non-canonical.

ADR 0002 accepts one embedded SQLite canonical store using the complete issue
#18 dependency audit, but it grants no additional authority. Future migration
must follow the staged inventory, immutable backup, dry-run import, parity,
single cutover, and rollback boundaries in
`docs/research/json-jsonl-state-dependency-audit.md`. Agents must use stable
`league` commands, never SQL; JSON/JSONL becomes export/backup only after
cutover, with no permanent dual canonical write path.

The journal policy in [ADR 0002](adr/0002-sqlite-canonical-store.md#journal-mode-safety-gate)
is normative; migration receipts report the loaded-runtime decision instead of
restating a second policy here. Issue #23 must repeat that gate against exact
staged/released bytes and owns the isolated sandbox, read-only live shadow,
atomic pointer switch, rollback orchestration, and post-switch smoke. This
repository still performs no global install, hook edit, live import, watcher
replacement, or cutover.

The issue-#23 foundation is exercised through `league acceptance run`, and its
complete no-apply continuation is exercised through `league acceptance
preflight`, as documented in `docs/ACCEPTANCE.md`. Both create only a
namespaced disposable home
beneath an explicit temporary root, validates caller-specified byte/config/fake
process sentinels, imports the complete synthetic legacy fixture, stages and
rolls back the exact repository release beneath its own prefix, and fault-tests
the generation pointer under its own lock. Preflight additionally copies only
explicitly bound legacy files into an isolated shadow, proves exact parity and
rollback, integrates the merged lifecycle through fake adapters, stages both
launchers inactive, and emits only an unapplied mutation manifest. These are
pre-cutover mechanics, not authority or real-adapter proof.

The repository-local schema is now contiguous `[1,2,3,4,5,6,7,8,9]`. Versions 1
and 2 remain the issue-#19 store, version 3 is the request lifecycle, version 4
is the runtime lifecycle, and v5 is
`advisory-project-catalog-and-roster-indexes`. Canonical v6 is
`guarded-rollover-and-shuffled-callsign-queue`, checksum
`879ef4addfe6725e31c31a5aa1db9078d7c066a26610eaa2753f749c6e53ab75`.
It evolves existing event and callsign-assignment tables, derives queue and
Squad-intake state during migration/import, and verifies foreign keys before
restoring enforcement.

Version 7 is `bounded-reporting-and-outbound-privacy`, checksum
`bebe90eb841eac2a0b42d3f89e321cb4f3f8b23b02d92febf5a4ea2a50727cde`. It adds structured project privacy classifications,
reporting indexes, immutable activity evidence, and report specifications. It
does not duplicate source evidence or create parallel request, outbox, runtime,
assignment, rollover, or callsign authority.

Version 8 adds bounded routing policy and requester progress. Version 9 adds
repository-owned artifact declarations and exact merged-publication receipts,
and blocks cleanup while any declaration is unresolved. Canonical migration
names and checksums live in the routing and repository-artifact sections of
[`docs/PROVENANCE.md`](PROVENANCE.md). The strict acceptance migration schema
names all nine contiguous versions; an older receipt contract cannot silently
accept a partial current migration.

Dry-run import plans bind `target_schema_version` into their digest. A plan
created for an earlier schema is refused as `import_plan_incompatible` before
row replay and must be regenerated from its retained source artifacts.

An existing explicit-root database still requires the ordinary verified
pre-upgrade backup. Issue #23 retains installed-state, live migration, rollback
rehearsal, guidance installation, and cutover ownership. The source-managed
shared guidance and explicit-root adapter are release inputs only; this
migration does not mutate a home directory or global harness state.
