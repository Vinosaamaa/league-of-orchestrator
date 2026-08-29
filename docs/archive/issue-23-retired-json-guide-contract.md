# Retired JSON orchestration guide contract

Historical reference only. This file is not active agent guidance and must not
be installed. It preserves the final JSON-era Shotcaller/Champion record
contract so a future recovery review can understand the retired system without
restoring it.

- Source class: final active global orchestration guide before the SQLite-only
  source-managed guide was installed.
- Full source SHA-256:
  `9b68c3b9d77bd431c36729bc000af323e56ea9d7e96637ed67e9be0e15ba44ad`.
- Sanitization: this verbatim orchestration excerpt contains no user name,
  machine-specific absolute path, runtime identifier, prompt, transcript,
  credential, or private endpoint. Unrelated global browser and editor guidance
  was intentionally excluded.
- Runtime status: retired, read-only, and unsupported as an active writer.

## Verbatim retired contract excerpt

### Roster records

- Every Shotcaller and Champion owns one `status.json` snapshot and one
  append-only `updates.jsonl`; follow `~/.agents/agent-status.example.json` and
  `~/.agents/agent-updates.example.jsonl`; distinguish roles with `role`.
- Record paths -> Shotcaller:
  `~/.agents/shotcallers/<Shotcaller>/{status.json,updates.jsonl}`; Champion:
  `~/.agents/shotcallers/<Shotcaller>/champions/<Champion>/{status.json,updates.jsonl}`.
- Initial assignment -> record exact thread UUID, pane, backend, immutable task,
  and repository identity; validate before work, resume, routing, or landing.
- Record ownership -> each agent updates only its own pair; a Shotcaller reads but
  never repairs or advances a Champion's records.
- Material transition -> use `agent-watcher transition` to atomically append the
  event and replace its matching snapshot; mutate only the agent-owned pair.
- Status or routing -> read Roster records first; contact the Champion only when
  records are missing, stale, or unclear.
- Repository writing by a Champion or main agent -> follow `Issues and worktrees`
  and bind each writer to its exact worktree.
- Before replying, a Shotcaller -> reconcile direct delegates and report new
  blocked or `ready_to_land` results; durable records alone are not delivery.
- Active Roster -> run `agent-watcher --shotcaller <Callsign> supervise` in the
  current turn; ordinary user prompts outrank watcher wakes.
- Active record with settled/missing runtime -> run backend reconciliation;
  preserve records and report only a debounced `champion_stalled` event.

### Champion teardown

- `ready_to_land` -> preserve Champion and worktree until Shotcaller-confirmed
  class-specific acceptance manifest; failed or conflicting proof -> preserve +
  report.
- Shotcaller-generated teardown manifest -> exact Champion/task/endpoint,
  class-applicable repository/publication/release/archive and callsign proof
  required; never target Shotcallers, persistent supervisors, shared Chrome, or
  remote branches.
- Landed no-release work -> require exact PR/merge/clean/published state and one
  exact durable-artifact publication receipt; never fabricate install/deployment.
- Compiled local install -> require exact tracked build inputs, recorded/current
  installed binary hash, and build/install/smoke receipts; never infer parity from
  a nondeterministic rebuild.
- Accepted local analysis -> require repository-null identity, exact accepted
  summary/artifact hashes, owner acceptance, immutable archive, and no unpublished
  repository work; never run Git cleanup.
- Cancellation -> inspect the exact thread, branch, worktree, and
  unpublished changes before cleanup.
- Dirty or unpublished worker state -> preserve + report; never discard
  automatically.
- Accepted worker output -> commit + publish or integrate before teardown.
- Exact Champion process -> terminate gracefully; verify exit; close only its
  pane/tab.
- Finished task identity -> archive final status; release its callsign without
  rewriting historical identity.
- Verified teardown -> remove its active Roster records and return its callsign
  to the matching `available.shotcaller` or `available.champion` pool.
- Worktree and branch cleanup -> follow `Issues and worktrees`; an explicitly
  rejected worker worktree must be clean with no unpublished work; missing proof
  -> preserve and report.
