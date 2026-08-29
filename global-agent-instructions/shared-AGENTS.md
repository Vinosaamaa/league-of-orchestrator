# Communication and authority

- Language -> precise and brief; define an unfamiliar state or mechanism in plain language before relying on it.
- Local file -> use a clickable absolute Markdown link when the surface supports it.
- Authority -> mutate only the scope the user named; a nearby problem, tool permission, or earlier approval does not expand authority.
- Evidence -> verify unstable or material claims at their owning source; otherwise label them unverified.
- Active Codex writer -> never resume the same thread from another process; queue a message to its exact thread when direct delivery is required.

# League runtime

League is SQLite-native. The canonical local command prefix is:

```sh
$HOME/.local/bin/league --state-root "$HOME/.local/state/league"
```

- Canonical writes -> use stable `league` domain commands only; never read or write the database with `sqlite3` or direct SQL.
- Compatibility watcher -> `agent-watcher` is an installed SQLite adapter for `status`, `supervise`, prompt-hook, Stop-hook, and delivery only.
- State root -> use the exact canonical root above; a launched Champion receives only that root as an additional Codex workspace-write root.
- Storage refusal -> preserve state and report the exact error code; never bypass a guard or hand-edit canonical state.
- Installed parity -> before a release claim, verify the tested source revision, installed release revision, executable hashes, migration receipt, smoke receipt, and rollback receipt.

## Roles and ownership

- Summoner -> final authority for scope, priority, merge, release, installation, deployment, cutover, and teardown.
- Shotcaller -> user-facing router designated by the user; owns semantic prompt triage, request routing, supervision, landing, and cleanup decisions.
- Champion -> one visible teammate thread with a canonical League agent, task, assignment, runtime, and callsign identity.
- Hidden worker -> short bounded collaboration worker outside the visible League Roster and Champion callsign pool.
- Every delegate -> notify its owner when blocked or `ready_to_land`; durable state does not replace delivery.
- Champion issue assignment -> implementation, focused tests, commit, push, PR/update, and exact-head CI only unless the user separately authorizes merge, release, installation, deployment, or teardown.

## Prompt intake and one-process turn contract

- UserPromptSubmit and beforeSubmitPrompt -> capture the exact local prompt bytes once and wake the verified Shotcaller; never rewrite the body, inject control text, mine transcripts, infer a split, or fabricate missed prompts.
- Missing runtime identity -> quarantine and deduplicate the exact prompt without blocking ordinary user input; bind later only to one verified runtime.
- Start of every Shotcaller turn -> start exactly one bounded League process; keep its PID/stdin/stdout until the turn commits:

```sh
$HOME/.local/bin/league --state-root "$HOME/.local/state/league" request turn \
  --owner-agent-id <shotcaller-agent-id>
```

- Intake phase -> the process returns every bounded exact untriaged body and then waits on stdin; it does not insert instructions into any body.
- Semantic split -> the Shotcaller model reads each exact returned body and chooses the ordered items, summaries, dispositions, relationships, work kind, and direct/hidden/Champion routing plan; an adapter may manufacture only mechanical IDs, claim tokens, JSON serialization, timestamps, hashes, locators, and command arguments.
- Begin phase -> send one semantic-only JSON line containing ordered `decisions` and same-order routing `plans`; the open adapter manufactures item/request/claim/dispatch identity and time, atomically commits the batch, returns its mechanical receipts plus the current full obligation boundary, and keeps the same process alive.
- Commit phase -> before reply, wait, handoff, or end, send that same process one semantic-only `actions` line; the adapter manufactures versions, response/result/event/outbox identity, locators, hashes, and time, atomically records outcomes and delivery effects, returns `phase=committed` plus the final full obligation boundary, and exits.
- Process budget -> an ordinary direct turn launches one Shotcaller League process total. Do not invoke per-prompt `request triage`, per-request claim/dispatch/answer, `request unresolved`, status, or supervise commands inside the active turn.
- Transaction boundary -> begin and commit are separate atomic transactions on one connection; League holds no transaction while the model works between them, so hooks and Champion transitions remain writable.
- Exact retry -> identical triage, claims, routing, answers, and results are idempotent; missing, reordered, duplicated, conflicting, stale-version, cross-owner, or partially specified batches refuse without partial commit.
- Complete accounting -> every prompt item is explicitly classified as new request, follow-up, context, acknowledgement, duplicate, or deferred; do not silently omit text.
- Non-ordinary action -> acknowledgement-gated cross-Squad route, defer, awaiting-user, block, cancel, Champion assignment, transition, and cleanup retain their dedicated stable one-command operations; preserve claims and expected versions and reflect their result in the open turn's final commit/boundary.
- Before reply, wait, handoff, or end -> require the turn process's final boundary to account for every request, untriaged prompt, delivery, assignment, task, Champion, and cleanup obligation; do not shell out to a second unresolved query.
- Omission backstop -> an untriaged prompt remains unresolved and the installed Stop hook blocks one end attempt for the current generation; do not treat Stop as the normal triage mechanism.
- User priority -> an accepted user prompt rearms the current Shotcaller generation and wakes an active supervisor; ordinary user input outranks material-event waits.
- Hook budget -> UserPromptSubmit and Stop each open canonical storage once and perform one bounded in-process operation; they never shell through `league` subcommands.

## Visible Champion launch

- User asks for one Champion -> create exactly one visible Herdr/Codex Champion; do not also spawn a hidden coordination agent.
- Dispatch first -> triage the prompt, claim its request, and record Champion execution mode before launch.
- Launch -> use the one recoverable production command `league assign run`; do not manually chain prepare, launching, and activate.
- Exact command inputs -> supply the request and claim, task identity and summary, Shotcaller agent ID, repository URL, issue, branch, absolute issue worktree, one-or-two-word display task, requested model and effort, and any required capabilities.
- Generated IDs -> omit assignment and Champion agent IDs when deterministic League IDs are sufficient; an explicit retry must reuse every identity-bearing input.
- Backend boundary -> launch only from the current Herdr session. League reserves a callsign, marks launch intent, creates one unfocused tab at the exact worktree, starts Codex, observes its generated thread UUID, verifies routing name, display kind, pane, terminal, workspace, cwd, and title, then atomically activates the assignment.
- Sandbox boundary -> the new Codex thread remains in `workspace-write`; League adds only the exact canonical League state root to its writable roots so stable context, transition, delivery, and cleanup commands work.
- Context -> after activation, League delivers one bounded SQLite-only assignment context containing the canonical IDs and stable command paths; the adapter does not make semantic decisions.
- Launch failure -> record `blocked` only after the exact owned endpoint is proven gone and the reservation is released; otherwise record `cleanup_pending` with the exact runtime and callsign cleanup obligation.
- Trust prompt -> League never accepts a directory-trust prompt for the user; an unresolved trust boundary fails launch and preserves recoverable state.

Example shape:

```sh
$HOME/.local/bin/league --state-root "$HOME/.local/state/league" assign run \
  --request-id <request-id> --claim-token <claim-token> \
  --task-id <task-id> --task-summary <summary> \
  --coordinator-agent-id <shotcaller-agent-id> \
  --repository <public-repository-url> --issue <number> \
  --branch <issue-branch> --worktree <absolute-issue-worktree> \
  --task-label <one-or-two-words> --model <model> --effort <effort>
```

## Champion work and delivery

- Initial context -> verify callsign, agent ID, task ID, assignment ID, runtime ID, repository, issue, branch, and worktree before substantive work.
- Material task state -> use `league task transition` with the exact task, runtime, expected version, transition ID/key, event ID, outbox ID, recipient Shotcaller, update, next action, blocker, and time.
- Routine progress -> keep updates bounded and evidence-based; do not emit heartbeat theater.
- Delivery -> task transition and recipient outbox commit in the same SQLite transaction. An active watcher owns the wake; otherwise the verified direct adapter attempts exactly once; unavailable recipients leave the outbox pending.
- Supervision -> delivery uses the already registered event-driven SQLite watcher or exact-once direct fallback. An active Shotcaller turn never starts or polls `agent-watcher supervise` or status; supervisor process lifecycle belongs to installed runtime wiring outside the model turn.
- Duplicate delivery -> exact event/outbox retry is idempotent and must not prompt twice.
- Ready to land -> preserve the Champion, endpoint, worktree, branch, callsign, task, receipts, and unpublished state until the Shotcaller proves every authorized gate.

## Landing and cleanup

- Repository work -> land only an exact-head green PR after explicit merge authority; prove merge/tree parity and required publication, installation, deployment, or smoke receipts for the task class.
- No-release work -> require exact merged artifact bytes plus a durable publication receipt; do not invent deployment or installation proof.
- Local install -> require exact tracked source/build inputs, tested revision, installed artifact hash parity, backup, install receipt, smoke receipt, and verified rollback.
- Cleanup plan -> use the stable `league cleanup plan` or the narrowly scoped supported reconciliation command with an exact manifest; missing or conflicting identity fails closed.
- Cleanup execute -> close only the verified Champion process and pane/tab, verify exit, settle the runtime, archive required durable evidence, release the exact callsign, and remove only the explicit clean accepted worktree and eligible local branch.
- Preserve -> remote branches, primary/shared/other-issue worktrees, dirty or unpublished work, uncertain resources, Shotcallers, persistent supervisors, and unrelated panes are never removed automatically.
- Failure -> keep the canonical cleanup obligation pending with its first exact blocker; never hand-edit canonical or retired storage.

# Issues, worktrees, and lanes

- Repository writer -> one issue-owned worktree and branch from exact current `origin/main`; shared and primary checkouts remain read-only.
- Before mutation -> record repository, issue, lane, exact worktree, branch, HEAD/tree, cleanliness, divergence, and intended PR.
- Fast lane -> localized, reversible, low-risk work; retain the issue/worktree/branch/PR/CI lifecycle and run focused affected verification.
- Reliability lane -> high-risk work with explicit end-to-end owner-source proof.
- Dirty, divergent, ambiguous, or unpublished state -> preserve and report; never reset, force-delete, or overwrite it.
- Cleanup -> remove only an exact verified task worktree without force, then its eligible local branch; remote deletion requires separate authority.

# Engineering and verification

- Design -> prefer the smallest complete module behind the existing stable interface; avoid parallel state, wrappers without a concrete need, and speculative redesign.
- Storage -> SQLite is canonical; migration inputs, backups, and bounded exports are evidence only, never a second writer.
- Tests -> use synthetic temporary roots and fake adapters; never point tests at live League state, a live multiplexer, or a real repository worktree.
- Real acceptance -> use one explicitly authorized disposable identity and exact owner-source checks; API success or a synthetic subprocess alone does not prove visible behavior.
- Local baseline -> run only the focused affected tests in Fast lane unless a stricter repository gate requires more.
- Public safety -> audit candidate bytes and reachable Git objects before network publication; stop on credentials, tokens, private endpoints, personal data, transcripts, generated machine state, or local identifiers.
- Sensitive finding -> report only repository path plus data class; never echo the value.

# Outbound privacy

- Remote payload, public or private -> omit machine-local paths and identifiers, credentials or private endpoints, personal or employment data, and containing logs or screenshots; use repository-relative paths, approved public URLs, opaque League IDs, hashes, or explicit placeholders, then validate the final rendered payload before any network call.

# Guide changes

- Source -> this file owns the installed global League workflow; edit it in the issue-owned repository branch, never hand-edit the installed copy.
- Style -> one trigger, action, and boundary per bullet; keep commands exact and rationale in repository docs.
- Release -> install exact merged source bytes only after the release gate, with a backup, byte-parity receipt, and tested rollback.
- Contradiction -> re-read the affected guide after editing and resolve any retained retired-writer instruction before installation.
