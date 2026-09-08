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

- Only Codex `UserPromptSubmit`, Cursor `beforeSubmitPrompt`, and Pi `input`
  from an exactly bound canonical League runtime capture its exact local prompt
  bytes once and wake its verified Shotcaller. An unbound, non-League, or
  otherwise unverifiable runtime is left untouched and unrecorded.
- Prompt intake activates only after exact canonical binding; it never backfills
  pre-binding prompts or mines transcripts. It never rewrites bodies, injects
  control text, infers semantic splits, or fabricates missed prompts.
- At the start of a Shotcaller turn, inspect `league request triage-status
  --owner-agent-id <id>`. Unconfigured owners retain inline classification.
- Inline mode only: start exactly one bounded process and keep it through commit:

```sh
$HOME/.local/bin/league --state-root "$HOME/.local/state/league" request turn \
  --owner-agent-id <shotcaller-agent-id>
```

- Inline mode: the Shotcaller model performs semantic triage and routing. The adapter may
  manufacture only mechanical IDs, claim tokens, JSON, timestamps, hashes,
  locators, and command arguments.
- Background mode: the dedicated League worker classifies new prompts; consume
  its canonical request checklist through the inbox. Do not classify again or
  mark unknown subrequests answered before that checklist arrives.
- Explicit prompt-triage ON/OFF uses `league request triage-mode --owner-agent-id
  <id> --mode on|off --expected-version <version> --at <time>`. OFF pauses pending
  classification and records new prompts as skipped; it never cancels existing
  requests, Champion work, deliveries, supervision, or cleanup.
- Begin and commit are separate atomic transactions on the same connection.
  League holds no transaction while the model reasons between them.
- Exact retries are idempotent. Missing, reordered, duplicated, conflicting,
  stale-version, cross-owner, or partial batches refuse without partial commit.
- Every enabled captured bound prompt item is classified as a new request, follow-up,
  context, acknowledgement, duplicate, or deferred item; no text disappears
  silently.
- Before reply, wait, handoff, or end, the turn's final boundary accounts for
  every bound request, captured untriaged prompt, delivery, assignment, task,
  Champion, and cleanup obligation.
- Stop is an omission backstop, not the normal triage mechanism. Genuine user
  steering rearms it and outranks material-event waits.
- Stop feedback is an operational continuation, not new Summoner steering.
  Resolve untriaged input through its configured classifier; inspect a worker
  failure once instead of repeatedly attempting Stop or reclassifying inline.
- A routine Stop block never authorizes hook disablement, `service-start`,
  detachment, request cancellation, `/new`, or `allow-stop --once`; use the
  named recovery only for its exact refusal, and reserve the one-shot allowance
  for an explicit Summoner stop after work is paused.

## Issue binding and delegation

- A captured prompt is evidence, not the durable work container and not a
  substitute for a repository issue.
- Before repository work is assigned, run `league issue select`: search open
  and closed target-repository issues by normalized title and semantic scope,
  reuse an open equivalent, use only the supported authorized and settled
  reopen path for genuine closed recurrence with prior linkage, and create a
  new issue only for distinct work. Bind the immutable selection receipt to the
  canonical task; a positive issue number alone is not proof.
- Tiny direct work follows the universal authority and engineering rules.
  Durable research, benchmarks, release or operational work, confirmed
  debugging, fixtures, tests, and repository changes require an issue-bound
  visible Champion. Direct repository implementation refuses with
  `delegation_required` through the shared provider policy.
- Read-only diagnostics and supported recovery commands remain available;
  recovery still requires its own exact authority. Prompt intake, Stop, and
  detachment do not depend on implementation delegation.
- Hidden workers stop at their bounded advisory perimeter and never own work
  that requires a visible Champion.
- One issue assignment creates exactly one visible Champion. Do not add a
  hidden implementation owner or a second visible Champion for the same issue
  worktree.
- A Champion-routed request cannot record its initial result until its exact
  settled task proves the immutable issue-selection receipt, semantic issue
  binding, distinct visible Champion runtime, and active assignment.
  Answers require that result; accepted evidence survives cleanup and rollover.
- Independently fixable work may run in parallel only through separate issues,
  tasks, assignments, branches, and worktrees.
- A Champion starts from the exact assigned issue, acceptance criteria,
  worktree, branch, and intended handoff; inspect only task-relevant source and
  existing changes.
- Champion implementation uses the smallest source-managed change and fastest
  faithful focused check first; broaden verification only for concrete risk or
  failure.
- While an in-scope action remains, continue it directly; do not substitute
  status narration, unchanged polling, unrelated investigation, or speculative
  refactoring.
- One authoritative blocker or repeated identical failure stops retries; report
  the exact command, refusal, preserved state, and required owner action once.
- Champion completion reports changed files, exact verification, and any
  remaining blocker; never publish, merge, release, install, clean up, or keep
  monitoring unless explicitly assigned.
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

- During work, read `league delivery inbox --owner-agent-id <id>
  --runtime-instance-id <runtime> --at <time>` at a task boundary and before
  reporting request completion. It returns pending Champion and triage updates
  without submitting a prompt; an empty inbox requires no immediate repeat.
- After receiving an inbox or wait result, acknowledge its exact saved receipt
  with `league delivery ack-inbox --receipt <file> --at <time>`. Receipt removes
  pending delivery only; report request/task completion separately with evidence.
- An interrupted read leaves an expiring claim, not a receipt. Read again after
  expiry; never synthesize acknowledgement IDs or erase queued updates.
- Only an outstanding foreground receive leases tool-result delivery. General
  supervision and `wait_active` are not proof that a receive tool is waiting.
- Busy receivers consume updates through inbox checkpoints; waiting receivers
  receive tool results. Wake prompts require verified idle identity; unknown
  activity retains updates. Native idle-check/send races remain a release gate.
- Hooks first verify an exact canonical runtime binding and role. If a Codex,
  Pi, or Cursor CLI runtime is unbound or non-League, `UserPrompt`,
  pre-mutation, and `Stop` allow/no-op immediately with zero canonical mutation.
- A Shotcaller Stop reports unresolved work, but a native Codex Stop continuation
  already notified at the unchanged wait generation ends quietly. New captured
  steering rearms the reminder. This does not complete work, acknowledge delivery,
  pause Champions, or claim a healthy supervisor handoff.
- Other Stop attempts retain their obligation checks and the explicit one-shot
  owner-stop path; never clear records merely to make a turn end.
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
belong to the terminal-environment-toolkit repository.
