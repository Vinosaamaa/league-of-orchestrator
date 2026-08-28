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
implementation. The harness composes the issue-#19 storage/import surface with
deterministic synthetic adapters and the source-managed legacy fixture. Its
staging manifest copies exact repository bytes only beneath an explicit
task-owned prefix and verifies a stable-pointer rollback there. No installer,
global hook mutation, live import, watcher replacement, external delivery, or
real harness/backend operation is included.

The global `--state-root` parser option is optional only so `acceptance run`
can use its separately named temporary root. Every storage and domain command
still refuses without an explicit state root, and acceptance refuses a supplied
state root to prevent ambiguous ownership. Focused command tests cover both
refusals.

Tests that require process inspection explicitly inject the single
`tests/fakes/ps` adapter through `tests/process_adapter.py`; Make targets do not
alter `PATH` for unrelated tests. This keeps self-process and resource-lifecycle
contracts testable in restricted CI and agent sandboxes that deny host process
inspection. The adapter is test-only; production process inspection and
behavior are unchanged.
