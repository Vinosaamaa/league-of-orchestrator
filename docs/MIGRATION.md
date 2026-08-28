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

## Live-state migration gate

Live Roster migration requires a separately tested schema migration with backup,
integrity validation, collision refusal, idempotent retry, and rollback. Issue #2
does not authorize or implement that operation.
