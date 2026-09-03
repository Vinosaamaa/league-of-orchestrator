# League orchestration supplement

Read the universal `~/.agents/AGENTS.md` first. It is owned and installed only
by terminal-environment-toolkit. Its issue, worktree, implementation, review,
release, cleanup, and public-safety rules always apply. This League supplement
adds orchestration-specific constraints and may strengthen, but never weaken,
the universal contract. Refuse the operation if either guide is missing or the
two contracts conflict.

Use this supplement for League Shotcaller, Champion, watcher, routing,
rollover, autonomous-delivery, cleanup, and exact-thread reopen work.

## Canonical League state

League is SQLite-native. The canonical local command prefix is:

```sh
$HOME/.local/bin/league --state-root "$HOME/.local/state/league"
```

- Canonical writes use stable `league` domain commands only; never use direct
  SQL or restore JSON/JSONL as a second writer.
- `agent-watcher` is only the installed SQLite compatibility adapter for
  status, supervision, prompt hooks, Stop hooks, and delivery.
- A launched Champion receives only the exact canonical League state root as
  an additional writable root.
- Preserve the exact storage refusal code. Never bypass a guard or hand-edit
  canonical state.
- A League release may install this supplement only at
  `~/.agents/league/AGENTS.md`. It must never package, install, overwrite,
  restore, or roll back the universal guide.

## Durable prompt and request triage

- Only `UserPromptSubmit` and `beforeSubmitPrompt` from an exactly bound
  canonical League runtime capture its exact local prompt bytes once and wake
  its verified Shotcaller. An unbound, non-League, or otherwise unverifiable
  runtime is left untouched and unrecorded.
- Prompt intake activates only after exact canonical binding; it never backfills
  pre-binding prompts or mines transcripts. It never rewrites bodies, injects
  control text, infers semantic splits, or fabricates missed prompts.
- At the start of a Shotcaller turn, start exactly one bounded process and keep
  it through commit:

```sh
$HOME/.local/bin/league --state-root "$HOME/.local/state/league" request turn \
  --owner-agent-id <shotcaller-agent-id>
```

- The Shotcaller model performs semantic triage and routing. The adapter may
  manufacture only mechanical IDs, claim tokens, JSON, timestamps, hashes,
  locators, and command arguments.
- Begin and commit are separate atomic transactions on the same connection.
  League holds no transaction while the model reasons between them.
- Exact retries are idempotent. Missing, reordered, duplicated, conflicting,
  stale-version, cross-owner, or partial batches refuse without partial commit.
- Every captured bound prompt item is classified as a new request, follow-up,
  context, acknowledgement, duplicate, or deferred item; no text disappears
  silently.
- Before reply, wait, handoff, or end, the turn's final boundary accounts for
  every bound request, captured untriaged prompt, delivery, assignment, task,
  Champion, and cleanup obligation.
- Stop is an omission backstop, not the normal triage mechanism. Genuine user
  steering rearms it and outranks material-event waits.

## Issue binding and delegation

- A captured prompt is evidence, not the durable work container and not a
  substitute for a repository issue.
- Before repository work is assigned, run `league issue select`: search open
  and closed target-repository issues by normalized title and semantic scope,
  reuse an open equivalent, use only the supported authorized and settled
  reopen path for genuine closed recurrence with prior linkage, and create a
  new issue only for distinct work. Bind the immutable selection receipt to the
  canonical task; a positive issue number alone is not proof.
- Tiny direct work must satisfy the universal bounded-read-only rule. Durable
  research, benchmarks, release or operational work, confirmed debugging,
  fixtures, tests, and repository changes require an issue-bound visible
  Champion. Shotcallers do not implement repository work directly by default.
- Hidden workers stop at their bounded advisory perimeter and never own work
  that requires a visible Champion.
- One issue assignment creates exactly one visible Champion. Do not add a
  hidden implementation owner or a second visible Champion for the same issue
  worktree.
- Independently fixable work may run in parallel only through separate issues,
  tasks, assignments, branches, and worktrees.
- The Shotcaller remains the user-facing owner for prioritization,
  supervision, review, landing, release, verification, repair, and cleanup.

## Placement and launch

- `league shotcaller create` converts only the calling live registered-agent
  pane in place. League verifies the adapter, multiplexer, workspace, tab, pane,
  terminal, session, worktree, route, and displayed identity before activation.
- `league assign run` creates the selected registered runtime in a distinct new
  multiplexer tab; it never splits or reuses the Shotcaller pane. Ordinary
  launch defaults to Pi with the Codex provider and consumes one exact persisted
  `ModelRouter` decision. Explicit runtime, provider, model, and effort
  overrides remain exact; missing or mismatched routing refuses before launch.
- `league assign replace-runtime` freezes one active Champion assignment and
  dispatches predecessor A and successor B through their registered adapters.
  It publishes the ownership switch only after B verifies, retires A before
  releasing one handoff outbox, and adopts or compensates exact retries without
  allowing overlapping writes. Ambiguous native state remains a durable
  recovery obligation.
- Display labels contain one or two words. Routing identity remains separate
  from the human-visible label.
- Dispatch, claim, execution mode, and the exact duplicate-preflight selection
  receipt precedes launch. Before launch, `league assign run` re-verifies the
  exact repository issue is open and matches the canonical task scope. Do not
  manually chain prepare,
  launching, and activation.
- Launch failure rolls back only the exact partial reservation and endpoint.
  Unproven cleanup remains `cleanup_pending`.
- League never accepts a directory-trust prompt for the user.

## Delivery and supervision

- Hooks first verify an exact canonical runtime binding and role. If a Codex,
  Pi, or Cursor CLI runtime is unbound or non-League, `UserPrompt`,
  pre-mutation, and `Stop` allow/no-op immediately with zero canonical mutation.
- An attached Shotcaller with any owner or delegated obligation blocks every
  `Stop` attempt unless the Summoner explicitly requested a final stop and the
  Shotcaller armed the exact one-shot allowance after pausing work.
- When the Summoner requests all work paused, the Shotcaller reaches a safe
  boundary for its own work, sends a pause-and-preserve instruction to every
  owned active Champion, then runs
  `$HOME/.local/bin/agent-watcher --shotcaller <callsign> allow-stop --once`
  immediately before `Stop`. The next Stop consumes the allowance; it never
  disables hooks, changes supervision mode, or authorizes a later Stop.
- An attached Shotcaller waits for material League work with one
  `$HOME/.local/bin/agent-watcher --shotcaller <callsign> wait` invocation.
  This provider-neutral foreground wait applies to Codex, Pi, and Cursor CLI;
  do not poll with multiplexer-specific wait commands.
- `attach-shotcaller` requires the exact live supervisor binding and makes the
  Shotcaller terminal-attached. `detach-shotcaller` requests token-saving
  terminal detachment without pausing supervision.
- Detachment may let the Shotcaller end only when no owner-actionable work
  remains and the persistent watcher lease, runtime generation, locator, fence,
  and wake/delivery path exactly match its durable detachment receipt. The
  watcher remains live and later wakes and delivers exactly once.
- `service-pause` and `service-resume` are deprecated aliases for
  `detach-shotcaller` and `attach-shotcaller`, respectively.
- For a bound Shotcaller, a missing, stale, or ambiguous watcher, fence,
  binding, or wake path refuses detachment and keeps `Stop` blocked.
- Codex, Pi regardless of model provider, and Cursor CLI share this
  provider-neutral contract.
- Material task transitions use the exact task, runtime, expected version,
  transition identity, event, outbox, recipient Shotcaller, update, next
  action, blocker, and time.
- Task transition and recipient outbox commit in the same SQLite transaction.
  An active watcher owns wake delivery; otherwise the verified direct adapter
  attempts exactly once.
- Duplicate event/outbox delivery is idempotent and never prompts twice.
- Preserve a ready-to-land Champion, endpoint, worktree, branch, callsign,
  task, receipts, and unpublished state until the Shotcaller proves the
  universal release gates.

## Shotcaller rollover

- Switching freezes active Champions and commits only the Squad owner fence;
  it does not broadly rewrite descendants or delivery.
- `rollover reconcile-descendant` verifies the frozen live pane, thread,
  worktree, route, terminal, generation, and callsign. It may create one exact
  missing imported runtime while atomically CAS-moving task, assignment,
  callsign, Champion owner, and exact pending outboxes.
- Reconcile intake through bounded plans. Capture actor, runtime, session,
  source, body, and time remain immutable, and inherited requests retain their
  original requester.
- Retire only after intake, descendants, deliveries, runtimes, resources,
  callsigns, and cleanup evidence settle.

## Exact-thread reopen

- Reopen only the exact retained task, assignment, runtime, thread, repository,
  issue, branch, worktree, route, terminal, and generation.
- Stale, missing, ambiguous, closed, foreign, or already-active identity
  refuses. Never guess from pane order, a callsign alone, or a nearby worktree.
- If the installed command inventory does not expose exact-thread reopen, keep
  the preserved obligation pending; do not reconstruct it manually.

## Autonomous delivery

- `autonomous_delivery` (YOLO) is valid only under one durable, unexpired,
  unrevoked Summoner-issued grant with exact scope, actions, exclusions,
  targets, resource bounds, limits, revision, and digest.
- Autonomous authority never bypasses issue binding, Champion delegation,
  platform safety, provider restrictions, or the universal contract.
- Champions never merge or deploy. The Shotcaller owns every authorized
  external-action receipt and bounded repair loop.
- An already accepted grant may satisfy a later protected gate only through an
  exact `--mode-action` and expected goal version. The gate must bind and settle
  its command scope receipt, and the exact target digest must be the singleton
  action resource contained by the grant boundary; do not ask again for an in-scope category, and do
  not infer adjacent categories such as reconciliation, retirement, Shotcaller
  creation, Squad registration, deploy, or teardown when they are absent.
- If `league help inventory` does not expose stable `mode.*` commands, the mode
  is unavailable and remains manual; never infer authority from prose.

## League cleanup delta

- Use `league cleanup plan` or the narrowly scoped supported reconciliation
  command with an exact manifest. Missing or conflicting identity fails closed.
- Close only the verified Champion process and pane or tab; verify exit, settle
  the runtime, archive required evidence, release the exact callsign, and
  remove only explicitly eligible task-owned resources.
- The universal cleanup rules remain authoritative. League additionally
  preserves Shotcallers, persistent supervisors, shared or retained resources,
  unrelated panes, and every resource without exact ownership and identity
  proof.
- Failure keeps the canonical cleanup obligation pending with its first exact
  blocker. Never hand-edit canonical or retired storage.

This file owns only League orchestration deltas. Changes to the universal guide
belong to terminal-environment-toolkit issue #45.
