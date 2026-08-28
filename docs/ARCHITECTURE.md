# Architecture baseline

## Authority and vocabulary

- **Summoner**: the user and final authority for scope, merge, release,
  deployment, and direct steering.
- **Shotcaller**: a visible coordinator that owns routing and landing decisions.
- **Champion**: a visible issue-bound teammate that may implement, test, commit,
  publish, and prepare a pull request, but may not merge or deploy.
- **Roster**: durable status snapshots and append-only material updates for
  visible agents.
- **Lead**: an optional relay destination, never a superior authority or
  scheduler.

Squad ownership, disposable Shotcaller handoff, and a project catalog are
planned work. They are not represented as completed baseline behavior.

## Current modules

The bootstrap deliberately keeps one deep runtime module:

- `src/agent_watcher.py` owns strict record decoding, snapshot/event parity,
  watcher state, durable offsets, event deduplication, transition routing,
  runtime reconciliation, Herdr/tmux subprocess boundaries, atomic Herdr launch
  preflight and reservation rollback, hidden-worker allocation, optional Lead
  relay, semantic model routing, task-resource checks, and fail-closed teardown.
- `bin/agent-watcher` is a path-resolving launcher with no domain behavior.
- `schema/`, `examples/`, and `config/` define the public authoring surface.
- `tests/` exercises the imported behavior with temporary synthetic fixtures.

This layout is intentionally not split into shallow wrapper modules. Issue
[#7](https://github.com/Vinosaamaa/league-of-orchestrator/issues/7) owns the
future adapter interfaces; extraction should happen against those acceptance
criteria while the focused suite preserves behavior.

## Durable flow

1. A visible agent owns one `status.json` snapshot and one append-only
   `updates.jsonl` file.
2. Current-format reads reject malformed UTF-8, duplicate JSON keys, unsupported
   states, incomplete Champion identity, and snapshot/event mismatch.
3. Herdr launch preflight reconciles the Roster, callsign pool, and live
   endpoint. It reserves the lowercase routing name, starts the endpoint, and
   records the routing name and displayed backend kind only after post-start
   verification; a mismatch rolls back the reservation.
4. A transition appends one event and atomically replaces the matching snapshot
   while holding the record lock.
5. The watcher baselines existing events, tracks byte offsets and event digests,
   and emits only eligible scoped material transitions.
6. Runtime reconciliation observes adapters without mutating Roster records or
   inferring completion.
7. Teardown verifies the full schema-2 manifest before any archive, endpoint,
   worktree, branch, resource, record, or callsign mutation. Squash proof may
   tolerate later unrelated main commits only when every changed file matches;
   local-install proof requires exact released source/installed byte parity and
   a smoke receipt.

## Portability boundary

The baseline is not fully agent- or backend-agnostic. Champion identity requires
a Codex-shaped UUID, automatic hooks are Codex-specific, Herdr/tmux are
hard-coded branches, and the atomic launch command is currently Herdr-specific.
Semantic routing accepts explicit model and effort strings, but the example
defaults name current OpenAI models. These are known inputs to issues #7 and
#10, not claims of completed portability.

## Dependencies and side effects

Runtime code uses the Python standard library. Adapter commands are invoked only
by explicit delivery, reconciliation, resource, or teardown operations. The
local test command substitutes temporary records, repositories, state stores,
and fake adapter executables; it does not touch live Roster state.
