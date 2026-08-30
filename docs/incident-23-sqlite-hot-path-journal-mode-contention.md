# Incident Review: SQLite hot-path contention and incomplete runtime wiring

**Issue:** [#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23)  
**Date:** 2026-08-29  
**Severity:** P0  
**Status:** Open; installed 0.2.21 remains the stable pointer, merged 0.2.22 is not installed, and the 0.2.23 source candidate is pending PR/CI with no installation authority

**Scope:** Local League prompt intake, Stop handling, supervision, visible Champion launch, and cleanup

## Executive summary

League's canonical database was already operating in write-ahead logging (WAL)
mode. A foreground supervisor kept a normal WAL connection open. At the same
time, every newly started prompt or Stop hook opened `SQLiteStorage` with
`request_wal=False`. The storage constructor interpreted that hot-path flag as
an instruction to execute `PRAGMA journal_mode=DELETE`.

Changing WAL to DELETE requires an exclusive database lock. The open supervisor
connection made that mode change unsafe, so SQLite returned `SQLITE_BUSY` before
the prompt or Stop operation began. Stop converted the refusal into a safe retry
message, but UserPromptSubmit returned an error. Codex did not automatically
resend that rejected prompt, leaving the ordinary prompt queued and uncaptured
by League.

The installed correction makes journal mode maintenance-only. Migration may
establish a mode under exclusive authority. Every normal supervisor, prompt,
Stop, transition, delivery, and reporting connection now reads and validates
the established mode without changing it. No timeout was increased.

The installed long-lived-supervisor gate then exposed three independent
release blockers. The stable CLI had no adapter-backed one-command visible
Champion launch. The installed global guide still directed agents to retired
JSON records and legacy writer commands. Finally, a correctly launched Codex
sandbox could not access the canonical League root, and the stable request
surface could list an untriaged prompt only by hash rather than return its exact
retained body for model-authored semantic triage.

The successor correction adds one recoverable `league assign run` path around
the existing assignment service and real Herdr/Codex adapter, gives the
Champion only the exact canonical League root as an additional writable root,
and replaces chatty turn choreography with one bounded `league request turn`
process. That process emits exact intake, accepts model-authored triage and
routing, stays open without holding a transaction, accepts final answers or
results, returns the full obligation boundary, and exits. UserPromptSubmit
still only captures exact bytes and wakes the Shotcaller; it does not alter the
body, inject instructions, choose a semantic split, or start the turn process.

A later performance review found that the earlier “legacy chatty turn” label
was inaccurate: it measured repeated SQLite commands, not the preserved JSON
runtime. The corrected ten-sample benchmark runs the exact retired JSON watcher
and installed one-process SQLite command on temporary six-item fixtures. The
authoritative median, p95, phase, output, and command-count results are recorded
once in the corrected comparison table below. The active source guide now
contains only SQLite instructions; the retired record contract is preserved as
a non-installable historical artifact.

Installed 0.2.20 completed the exact disposable flow from capture and explicit
triage through launch, transition, Shotcaller delivery, proof-gated cleanup,
callsign release, zero active residue, and SQLite integrity. The stable release
then advanced to 0.2.21. Fresh revalidation found that its new authoritative
session bootstrap succeeded, but the launcher rejected Herdr's documented
silent-success metadata command because League required JSON from every
adapter command. The 0.2.22 candidate accepts only exit-zero with exactly empty
stdout and stderr for that one metadata command; all other malformed, noisy, or
nonzero results remain fail-closed. The complete staged candidate lifecycle
passes, but issue #23 remains open until those exact bytes are merged, installed,
and accepted without changing the owner-controlled UserPromptSubmit setting.

## Original SQLite architecture

The accepted architecture uses one local SQLite database as the canonical
state machine. JSON and JSONL are immutable migration inputs, backups, or
bounded exports only. They are not a second writer. Every stable command opens
the canonical root, validates the established journal mode, performs a bounded
domain operation, and returns a deterministic receipt.

| Responsibility | Principal tables | Invariant |
| --- | --- | --- |
| Schema and import | `schema_migrations`, `import_runs`, `imported_artifacts`, `legacy_event_aliases` | Reviewed migrations are contiguous; source hashes remain traceable. |
| Projects and identity | `projects`, `project_aliases`, `agent_instances`, `runtime_instances`, `runtime_bindings`, `squads` | One exact actor/runtime/repository identity owns each live operation. |
| Prompt and request | `prompts`, `prompt_payloads`, `prompt_quarantine`, `prompt_items`, `request_sources`, `requests`, `request_claims`, `request_dispatches`, `request_results`, `response_references` | Prompt bytes are exact-once; semantic accounting is explicit; mutations require claims and versions. |
| Assignment and callsign | `callsign_queue`, `callsign_assignments`, `task_assignments`, `assignment_receipts`, `launch_attempts` | A callsign reservation and visible runtime move through recoverable phases together. |
| Task and delivery | `tasks`, `task_transitions`, `events`, `delivery_outbox`, `outbox_dispatch_leases`, `delivery_attempts`, `recipient_receipts` | Material transition and outbox row commit atomically; delivery is exactly-once by receipt. |
| Watcher and obligations | `watcher_scopes`, `watcher_cursors`, `watcher_seen`, `watcher_registrations`, `obligations`, `cleanup_obligations` | Stop and wake decisions derive from durable generations and unresolved work. |
| Cleanup and resources | `task_resources`, `resource_leases`, `cleanup_operations`, `cleanup_actions`, `cleanup_action_receipts`, `teardown_receipts` | External cleanup requires a plan, lease, exact effect receipt, and resumable action state. |
| Rollover and routing | `shotcaller_intake`, `rollover_operations`, `active_champion_snapshots`, `squad_registration_offers`, `model_routing_decisions`, `model_routing_outcomes` | Owner replacement and routing are explicit, acknowledgement-gated changes. |
| Evidence and publication | `activity_evidence`, `repository_artifacts`, `report_specs` | Durable artifacts and reports retain provenance without exposing local-only evidence. |

SQLite is retained because these flows require multi-record transactions,
foreign keys, unique idempotency keys, compare-and-swap versions, one ordered
outbox, and crash-resumable cleanup. Reintroducing mutable JSON would recreate
split-brain writers, partial file updates, ambiguous event offsets, and
cross-file parity failures. The performance correction removes process
choreography; it does not weaken the transactional model.

## Exact symptom and error

With one foreground supervisor open on the canonical WAL database:

- two Garen Stop attempts returned a normal retry message beginning
  `League canonical state is busy; unresolved obligations remain authoritative`;
- the next Darius UserPromptSubmit failed with
  `ERROR: busy: SQLite contention exceeded the bounded timeout`;
- Codex did not automatically resend the rejected hook event;
- the prompt therefore remained queued without proof of canonical League
  capture.

A later model-visible message is not evidence that the UserPromptSubmit hook
captured the original bytes. Canonical prompt and payload rows are the required
evidence.

## What previously worked

- Ordinary prompt capture worked when no connection forced a conflicting
  journal-mode negotiation.
- Exact retry deduplication worked once a prompt reached canonical intake.
- League 0.2.8 gave distinct queued prompt bodies distinct deterministic source
  identities, fixing the earlier source-key collision.
- League 0.2.9 preserved obligations when Stop encountered bounded writer
  contention.
- Short transition, prompt, and Stop concurrency passed because those tests did
  not keep a conflicting WAL supervisor connection open.

## What failed

- Hot-path connection setup treated `request_wal=False` as permission to change
  canonical database mode.
- `src/league/canonical_watcher.py` supplied that flag for supervise, prompt,
  and Stop processes.
- `src/league/sqlite_store.py` executed
  `PRAGMA journal_mode=DELETE` on every such connection.
- WAL-to-DELETE negotiation required an exclusive lock and failed before hook
  identity resolution, prompt capture, obligation counting, or Stop generation
  handling.
- The prompt hook surfaced the storage refusal to Codex. Because the rejected
  hook event was not resent, exact-once storage could not recover it later.

## Observed issue-23 P0 failures

| Boundary | Observed symptom | Root cause | Settled correction |
| --- | --- | --- | --- |
| Legacy import parity | Exact Garen and Darius initialization sentences produced `snapshot_event_mismatch`. | Historical Shotcaller status/event text differed at initialization. | Hash-bound, initialization-only reconciliation modifies a temporary snapshot and emits an immutable receipt; default remains fail-closed. |
| Multiple legacy pairs | Normalizing one pair exposed the second mismatch. | The first contract authorized only one singular object. | Ordered duplicate-free list authorizes only exact independent pairs; overlap, stale hash, partial, late, broad, or non-Shotcaller use refuses. |
| Visible assignment import | Current callsign records were rejected for one real visible locator field. | Import/storage did not model the preserved owner-visible locator. | The canonical model preserves and round-trips the field; malformed or stale values refuse. |
| Archived watcher cursor | Preflight returned `unknown_consumer`. | A retained cursor referenced an archived/non-active Roster source. | Exact classification imports archive metadata only; it invents no agent, history, seen row, cursor delivery, or wake. |
| Pending launch aliases | Preserved `created_at`, `resume_thread_id`, and `task` were unsupported. | Legacy and canonical names differed. | Conflict-checked aliases normalize to `started_at`, `resume_thread`, and task summary without inventing values. |
| Identity collision | A live legacy supervisor wrote while a snapshot was being revalidated. | Writers were not fully quiesced. | Treat changed sentinels as concurrent drift, take a fresh baseline once quiesced, and never hand-edit history. |
| Stop and supervise dispatch | SQLite status worked, Stop returned `{}`, and `supervise` was fenced. | Stable compatibility dispatch routed read/status to SQLite but supervise/Stop to the retired writer. | Stable watcher compatibility routes Stop, prompt, supervise, and delivery to SQLite while actual legacy writers remain fenced. |
| Material-event fallback | A completed Champion event did not wake its Shotcaller. | Active-watcher ownership and no-watcher direct fallback were not both proven. | Outbox delivery owns one watcher wake or one direct fallback; duplicates do not prompt twice and offline rows stay pending. |
| Runtime identity | Unverified prompts, including Champion prompts, returned `runtime_unverified`. | Prompt intake incorrectly required the actor to be a live Shotcaller and attempted a Shotcaller wake for Champions. | Verified Champions capture without Shotcaller wake; unverified runtimes quarantine/deduplicate and return success. |
| Prompt source identity | Distinct queued prompts returned `prompt_source_conflict`. | Adapter/session/raw turn identity was reused for different prompt bodies. | Deterministic source identity includes exact body hash; exact retry deduplicates and different bodies remain distinct. |
| Stop continuity | Stop blocked once, then later turns ended despite obligations. | The one-shot guard was coupled to a prompt generation that failed intake did not advance. | Every accepted or quarantined prompt increments user/wait generation in the same transaction; the next prompt rearms Stop. |
| SQLite busy | Long-lived supervisor plus prompt or Stop returned bounded `busy`. | Every hot open requested DELETE against established WAL, requiring an exclusive mode-change lock. | Only migration establishes journal mode; hot paths read and validate it without mutation. |
| Sandbox access | A visible Champion could not open canonical state. | Workspace-write granted only the repository worktree. | Launcher adds only the exact canonical League root as a writable root; generic open errors identify this boundary. |
| Missing launcher | Legacy launch was correctly fenced but no stable one-command replacement existed. | Assignment phases existed without the real Herdr/Codex adapter composition. | `league assign run` owns reserve, launch, generated UUID observation, verification, activation, context, and failure cleanup. |
| Installed launch pending | The first installed visible E2E returned `cleanup_pending` and left one exact pre-session pane blocked in `launch_pending`; after exact rollback was fixed, later probes exposed `agent_not_ready` and a startup timeout. | Rollback required `/exit` even though no Codex session existed. Startup encoded root access as an unsupported configuration key, attempted to manufacture repository trust through a command-line config override that the persisted trust gate intentionally ignores, and then combined Herdr's `--approve-for-me` mode with an incompatible explicit `--sandbox` argument. | Pre-session rollback verifies routing name, pane, terminal, cwd, and pending state before closing the exact pane. Launch validates the linked Git back-reference without bypassing trust, relies on Herdr's workspace-write approval mode, and adds only the exact League root with the supported `--add-dir` flag. Untrusted repositories refuse and clean up. |
| Launch identity observation | Codex started and exposed its generated UUID in the exact terminal title before Herdr populated `agent_session`; rename then exposed the display title before parsed title tokens. | The adapter assumed all equivalent Herdr identity views became visible atomically. | Launch accepts only the UUID-shaped initial Codex title, exact endpoint/cwd/terminal, and stable Herdr state-change sequence; rename accepts the exact displayed title. Later context and cleanup require that same launch generation when session metadata is absent. |
| Silent metadata receipt | Installed 0.2.21 obtained the authoritative Codex session, then returned `launch_adapter_failed` even though `pane report-metadata` exited zero and applied the exact title/tokens. | The generic adapter parser required a JSON `result` from a Herdr command whose successful contract is intentionally silent. | Only this metadata call accepts exact exit-zero with empty stdout and stderr. Nonzero, noisy, or malformed results still refuse and run exact cleanup. |
| Production cleanup runtime kind | The installed visible launcher records the canonical Champion harness as `codex-thread`, while production cleanup accepted only the older `codex` spelling. | Launch and cleanup independently narrowed the same supported Codex runtime identity to different enum values. | Production cleanup accepts only `codex` or `codex-thread`, still requires Herdr, the exact session/pane/runtime generation, verified active/idle state, and the unchanged proof-gated action plan. |
| Working task supervision | The first full installed E2E launched Lux, but its `working` task transition woke the watcher as `champions-idle`. | Watcher task filters included `accepted` and `in_progress` but omitted canonical task states `working` and `progress`, so the active Champion disappeared from the supervision snapshot. | Both obligation and supervision queries include the full active task-state vocabulary; the exact working Champion remains visible and emits a material update. |
| Watcher delivery race | After the task-state filter fix, the next full E2E woke on a transient pending-delivery count before the transition dispatcher recorded the watcher receipt. | Supervision inferred delivery from agent/obligation snapshots instead of observing the canonical `watcher_event` recipient receipt. | The watcher stays registered through transient pending-outbox changes and wakes only after the exact recipient receipt; it returns that event ID/status/update. Non-delivery obligation changes remain wakeable. |
| Task transition delivery omission | After receipt-based supervision landed, the next full E2E durably recorded a working task event and pending outbox but the watcher correctly waited until timeout. | The stable `agent transition` command invoked installed delivery after commit, while the stable `task transition` command returned immediately after creating its outbox. | Both stable transition commands now dispatch their exact committed outbox through the same installed delivery adapter; unavailable recipients still leave the outbox pending. |
| Cleanup branch deletion depended on the primary checkout | The next full lifecycle passed launch, both transition wakes, and turn commit, then cleanup stopped after closing the endpoint and removing the worktree. | Cleanup proved the canary head was contained by its explicit base ref, but `git branch -d` independently compared it with the shared repository's unrelated checked-out branch. | After the explicit ancestry or squash-tree proof, cleanup deletes only the exact expected local ref with Git's compare-and-delete operation; a moved ref still refuses. |
| Acceptance BEGIN mismatch | A later disposable run launched correctly but Lux replied that it was still waiting for `BEGIN`; no task transition was written. | The acceptance context required the literal `BEGIN` sentinel, while the follow-up command omitted it. | The disposable harness sends the required sentinel explicitly. This was a harness-input defect, not a League storage or delivery failure. |
| Context delivery residue | Successful context delivery could leave the assignment-activation outbox pending. | The context receipt and activation outbox were stored by separate mechanisms without an atomic recipient receipt. | Context delivery now atomically records the exact recipient effect and marks only the matching activation outbox delivered; conflicts refuse. |
| Chatty turn latency | Six serialized triage operations took seconds; a cold command cost roughly two hundred milliseconds locally. | Each semantic item started a new Python process, reopened SQLite, reparsed CLI arguments, and re-ran policy setup. | One `request turn` process spans exact intake, atomic begin, model work, atomic commit, and final boundary. |
| Benchmark provenance | The prior result called 26 repeated SQLite commands a “legacy” turn. | The benchmark compared command choreography but did not execute the retired JSON watcher. | The reproducible comparison now invokes the preserved JSON command on temporary record pairs and the installed SQLite command on temporary canonical state; it reports median/p95 by phase and command count. |
| Manual supervise misuse | Active model turns invoked status/supervise polling. | Guidance treated a compatibility watcher process as per-turn choreography. | The source guide prohibits per-turn polling; installed event-driven watcher/direct fallback handles delivery outside the model turn. |

## Chronological timeline

| Phase | Evidence-bound event |
| --- | --- |
| League 0.2.5 | A second distinct prompt reused a source identity and failed with `prompt_source_conflict`. |
| Initial acceptance | An identical Kled retry proved deduplication but did not prove two different consecutive Garen prompts. |
| League 0.2.8 | Source identity added the exact body hash. Two distinct Garen prompt bodies then produced distinct durable source IDs, while exact retries remained inert. |
| League 0.2.9 | Stop gained a bounded retry block and prompt intake gained a separate bounded wait. A short transition/prompt/Stop canary passed. |
| Owner-visible failure | A long-lived Darius foreground supervisor remained connected to canonical WAL. Garen Stop retried twice, then Darius prompt intake failed `SQLITE_BUSY` before capture. |
| Containment | The owner disabled only League UserPromptSubmit. League Stop remained enabled. The supervisor and canonical state were preserved. |
| Root-cause reproduction | A synthetic WAL database with one long-lived open connection reproduced `database is locked` when a second hot connection requested DELETE. |
| Candidate correction | Normal connections read and validate established WAL or DELETE. Only migration may establish or change journal mode. |
| Installed journal gate | The exact installed release kept a long-lived supervisor open while prompt and Stop completed without busy; canonical mode remained WAL. |
| Missing launcher discovery | `agent-watcher launch` correctly remained fenced, while stable `league assign` exposed only prepare, launching, and activate phases with no operational adapter command. |
| Guide and sandbox discovery | The installed guide still mandated legacy JSON writers, and a launched Champion could not open the canonical League root from its default workspace-only sandbox. |
| Triage discovery | Prompt capture and Stop backstop were durable, but the Shotcaller lacked a stable exact-body query for explicit turn-start semantic triage. |
| First successor candidate | `league assign run`, owner-only exact-body intake, narrow Codex writable-root binding, SQLite-only guidance, and exact launch cleanup were implemented together. |
| Performance evidence | Cold CLI calls still cost about two hundred milliseconds each and the first exact-body design required one process per prompt/action. |
| Owner correction | The prompt hook remained capture-and-wake only; no control text could be inserted into the owner body. Semantic decisions remained model output. |
| One-process candidate | `league request turn` now keeps one PID and one connection through exact intake, atomic triage/claim/routing, model work, atomic answer/result/delivery commit, and the final full obligation boundary. |
| Corrected installed comparison | Ten same-workload samples ran the preserved JSON watcher and installed one-process SQLite command. The authoritative phase, median, p95, output, and command counts are in the corrected comparison table; the absent JSON semantic-begin phase is zero, not fabricated. |
| Guide retirement | The final JSON-era orchestration excerpt was archived with its full source hash; the installable source guide and generated Champion context removed active record-pair and retired-writer instructions. |
| Documentation gate | The owning incident/design report was expanded to record architecture, failure history, decisions, commands, performance, recovery, acceptance, and remaining limits before release. |
| First installed lifecycle attempt | Prompt capture and one-process triage passed, but visible launch stopped at `cleanup_pending`. Stable reporting proved the reserved assignment and cleanup obligation; Herdr proved one exact pre-session `launch_pending` pane. The pane was closed before correction. |
| Second installed lifecycle attempt | Exact pre-session rollback passed and exposed `agent_not_ready`; later pane receipts exposed both the ignored command-line trust override and Codex's exact `--approve-for-me` versus `--sandbox` parser conflict. The successor validates the linked worktree, preserves the persisted trust boundary, relies on Herdr's workspace-write approval mode, and adds one exact `--add-dir`. |
| Corrected real candidate launch | A disposable worktree of an already trusted repository started Codex, observed the generated UUID and exact display title, delivered bounded context, exited the exact agent, closed the pane, removed the linked worktree, and returned `cleaned=true`. |
| Cleanup contract review | The final installed E2E preparation found that visible `codex-thread` runtimes could not enter the production cleanup executor. The exact enum compatibility was added before the final lifecycle run; no cleanup proof or adapter scope was relaxed. |
| First full installed lifecycle | Capture, one-process triage, routing, and visible launch passed. Lux's first task transition exposed the missing `working`/`progress` watcher states and returned `champions-idle`; the disposable agent, pane, worktree, and branch were rolled back before correction. |
| Second full installed lifecycle | Lux remained active, but supervision returned `obligations-changed` while the transition outbox was briefly pending; stable reporting showed the exact task event durable and its outbox still pending after the watcher unregistered. Rollback again removed all external disposable resources. |
| Third full installed lifecycle | Receipt-based supervision waited safely, exposing that `task transition` never called the installed dispatcher. The exact working event and pending outbox remained durable; the disposable endpoint and Git resources rolled back before the symmetric dispatch fix. |
| Fourth full installed lifecycle | The supported retry passed capture, one-process semantic triage, visible launch, working/completed delivery, and turn commit. Production cleanup closed the endpoint and removed the worktree, then refused local branch deletion because the shared primary checkout was unrelated; fallback removed the exact remaining branch. |
| Final installed lifecycle | After the exact-ref cleanup fix and explicit acceptance `BEGIN`, League 0.2.20 completed capture, same-process triage/routing/commit, visible Lux launch, working and completed watcher delivery, cleanup, callsign release, integrity, and zero residue. |
| Installed 0.2.21 revalidation | Source and installed bytes matched across all 104 release files and canonical WAL integrity passed. The visible launch obtained the authoritative Codex session, then misclassified Herdr's successful empty metadata response as `launch_adapter_failed`; exact cleanup removed the disposable endpoint and Git resources. The stable pointer remained unchanged. |
| Staged 0.2.22 candidate | The exact silent-success contract and nonzero failure regression passed. A fresh disposable candidate completed two captures, one-process triage/routing/commit, visible launch, working/completed delivery, cleanup, callsign release, integrity, and zero residue. Merge, installation, and installed acceptance remain pending. |

## Technical root cause

The canonical database had one established journal mode: WAL. That mode is a
database-level property shared by every connection.

The hot path created this incompatible sequence:

1. A foreground supervisor opened the canonical database and remained alive.
2. A prompt or Stop process opened another `SQLiteStorage` connection.
3. `canonical_watcher` passed `request_wal=False`.
4. `SQLiteStorage.__init__` selected DELETE and executed
   `PRAGMA journal_mode=DELETE`.
5. SQLite required an exclusive lock to change the database from WAL to DELETE.
6. The existing supervisor connection prevented that maintenance operation.
7. Connection setup returned `SQLITE_BUSY`; prompt and Stop domain logic never
   began.

The supervisor did not corrupt state or violate an ownership rule. The system
incorrectly asked a normal hook connection to perform a maintenance-only mode
change while another correct connection was active.

## Failure flow

```text
Canonical database: WAL
          |
          v
Foreground supervisor keeps a WAL connection open
          |
          +------------------------------+
          |                              |
          v                              v
Prompt hook opens                   Stop hook opens
request_wal=False                  request_wal=False
          |                              |
          +-------------+----------------+
                        v
             PRAGMA journal_mode=DELETE
                        |
                        v
          Exclusive WAL-to-DELETE lock required
                        |
                        v
                    SQLITE_BUSY
                        |
          +-------------+----------------+
          |                              |
          v                              v
Prompt rejected; no capture        Stop safely asks to retry
```

## Canonical flows after the correction

### Prompt, semantic triage, request, and dispatch

1. Codex UserPromptSubmit or Cursor before-submit computes a deterministic
   source identity from adapter, session, raw event identity, and exact body
   hash. One transaction stores the unchanged body/hash/byte count, advances
   the wait generation, and wakes only a verified Shotcaller.
2. A verified Champion prompt is stored against that Champion without a
   Shotcaller wake. Missing identity is quarantined and deduplicated without
   blocking ordinary input; later binding requires one exact verified runtime.
3. The Shotcaller starts one `request turn` process. The process returns bounded
   exact untriaged bodies and waits for explicit model output.
4. Garen supplies only the ordered semantic items and routing choices in its
   normal reasoning pass. The already-open adapter deterministically manufactures
   item/request/claim/dispatch IDs, timestamps, hashes, locators, and arguments;
   the begin transaction persists them atomically. A partial batch rolls back.
5. For an ordinary direct turn the same process later commits answer references
   or request results and any return outbox effects. Non-ordinary defer, block,
   cancel, awaiting-user, or acknowledgement-gated cross-Squad route remains an
   explicit dedicated command.

### Assignment and visible launch

1. `assign run` checks the claimed Champion-dispatched request and deterministic
   assignment/agent identities.
2. One transaction reserves the next compatible callsign and creates pending
   task, agent, assignment, and cleanup-obligation state.
3. A second transaction marks launching before any external effect.
4. The adapter creates one unfocused visible tab in the exact issue worktree,
   starts Codex through Herdr's workspace-write approval mode with one exact
   canonical-root `--add-dir`, while preserving Codex's persisted repository-trust
   boundary, then observes
   the generated thread UUID, and verifies workspace, pane, terminal, cwd,
   routing name, backend kind, and display title.
5. If startup fails before a Codex session exists, rollback closes only an exact
   `launch_pending` pane after matching its routing name, terminal, and worktree.
   A started session still requires exact thread identity and `/exit`.
6. Bounded context delivery atomically records the matching assignment-activation
   recipient receipt and settles that outbox, so a successful one-command launch
   cannot leave a phantom pending delivery.
7. Activation stores the verified runtime and callsign receipt atomically. A
   bounded SQLite-only context is delivered once and its content/effect hashes
   become an immutable event.
8. Any post-effect refusal closes only the proven owned endpoint. If closure,
   runtime settlement, or callsign release is unproven, assignment and cleanup
   stay `cleanup_pending` rather than claiming rollback.

### Transition, outbox, and supervision

1. A Champion uses one `task transition` command with exact runtime, expected
   version, transition key, event ID, outbox ID, recipient, state, update, next
   action, blocker, and timestamp.
2. Task state, transition event, and delivery outbox row commit in one SQLite
   transaction. A retry with the same key is inert; different content refuses.
3. A registered active watcher claims and acknowledges the outbox exactly once.
   With no watcher, one verified direct Herdr fallback is attempted. Duplicate
   retry cannot prompt twice; unavailable recipients leave the outbox pending.
4. User messages have priority over material-event waits. The active model turn
   does not start, supervise, or poll a watcher process.
5. Stop performs one bounded in-process decision. It blocks the first end in a
   generation while any request, prompt, assignment, Champion, delivery, task,
   or cleanup obligation remains; an identical retry may allow to avoid a loop;
   the next accepted/quarantined prompt rearms the generation.

### Legacy reconciliation and canonical cutover

1. No-apply preflight hashes a fresh explicit inventory, manifest, plan, source
   sentinels, hooks, installed commands, and all declared legacy artifacts.
2. Only exact initialization-only Shotcaller reconciliation pairs with current
   source hashes may normalize the temporary import snapshot. Duplicate,
   overlapping, stale, broad, ambiguous, late, or non-Shotcaller entries refuse.
3. The receipt binds original artifact hashes, normalized hash, authoritative
   status/time/update triple, reason, and result. Live legacy files remain
   byte-identical.
4. Under owner cutover authority, one global lock freezes legacy writers/hooks,
   captures backup plus final delta, imports and verifies, then switches stable
   launcher, hooks, readers, and writer epoch together. Failure restores the
   exact prior pointer and preserves pending events.
5. The retired legacy tree remains an immutable archive with a restoration
   runbook. It is absent from the active path only after cold-start SQLite-only
   acceptance proves independence.

### Cleanup, teardown, crash resume, and rollover

1. A terminal task creates or exposes its cleanup obligation; `cleanup plan`
   validates repository, artifact, installation/deployment, runtime, worktree,
   branch, callsign, resource, and archive proof before claiming an operation.
2. `cleanup execute` leases one ordered action at a time. Each external effect
   writes an immutable receipt before the next action. A crash resumes from the
   first unreceipted action; it does not repeat a proven effect.
3. Teardown closes the exact Champion process/tab, verifies exit, settles the
   runtime, preserves remote branches and durable artifacts/history, releases
   the exact callsign, and removes only an explicit clean accepted worktree and
   eligible local branch. Any ambiguity stays pending.
4. Shotcaller rollover first drains intake, freezes an active-Champion snapshot,
   binds the replacement runtime, acknowledges ownership, and commits the owner
   fence atomically. Abort restores the prior accepting owner only before
   commit; post-commit cleanup follows normal proof-gated teardown.

## Why prior canaries missed it

1. The earliest acceptance covered identical retry deduplication, not two
   distinct consecutive Garen prompts.
2. The 0.2.8 canary proved corrected source identity but did not keep a
   long-lived foreground supervisor connection open.
3. The 0.2.9 contention test held a short write transaction. It proved Stop's
   safe retry behavior, but not connection initialization against established
   WAL.
4. Several synthetic paths established rollback-journal mode before starting
   concurrency, so a later `request_wal=False` did not request a mode change.
5. Model delivery was previously observed near some hook attempts, but model
   receipt cannot prove canonical hook capture.

## User impact

- An ordinary prompt was rejected by the hook before League captured it.
- Codex did not automatically resend the rejected prompt.
- The prompt remained queued or stuck from League's perspective.
- Garen could not settle Stop through normal supervision while journal-mode
  setup kept returning busy.
- No evidence indicates legacy-state mutation, manual SQLite editing, prompt
  publication, credential exposure, or canonical data loss.

## Owner questions and settled answers

| Owner question | Settled answer |
| --- | --- |
| Why would cutover roll back? | Only a failed proof gate triggers rollback. A safe refusal preserves the old installed pointer and canonical evidence; rollback is not inferred from delay or uncertainty. |
| What was the missing guarded cutover command for? | Preflight proved a snapshot but could not freeze writers, import the final delta, switch readers/writers/hooks atomically, smoke, and restore on failure. The later guarded executor supplied that apply boundary. |
| Why did the old Shotcaller directory still exist? | Cutover archived its bytes before proving runtime independence. Presence was retained rollback evidence, not proof that SQLite still depended on it. Active-path removal required a recoverable absent-directory E2E. |
| Can the old directory eventually be removed? | Yes only after installed commands and both hook families cold-start with that path absent. The immutable archived copy and restoration runbook remain so a future owner can restore deliberately. |
| Is the new watcher also named `agent-watcher`? | Yes. The stable compatibility name remains, but SQLite status, hooks, delivery, and supervisor registration route to the canonical implementation. Actual legacy writer verbs remain fenced. |
| Are hooks uninstalled and reinstalled? | Hook entries continue to invoke the stable installed compatibility binary. Release installation replaces exact tested bytes and verifies Codex and Cursor payload paths; it does not revive JSON writers. |
| Does Cursor participate in League? | The supported Cursor before-submit and Stop adapters follow the same capture/quarantine/generation and unresolved-boundary rules as Codex, with adapter-specific payload identity. Acceptance must prove both installed hook paths. |
| Why did new Codex windows ask for workspace trust? | A launcher may create a real visible thread, but it must never accept trust on the user's behalf. An unresolved trust prompt is a launch failure with recoverable cleanup state. |
| Where are release, backup, and legacy receipts stored? | In the restricted issue-23 cutover archive, keyed by immutable release/cutover identifiers. Hosted artifacts mention only safe identifiers and hashes, never machine-local paths. |
| Does UserPromptSubmit tell the model how to triage? | No. It stores the exact prompt and wakes. The permanent guide tells the Shotcaller to open the turn protocol; the model supplies the semantic split explicitly. |
| Why keep SQLite instead of returning to JSON? | SQLite supplies atomic task/outbox transitions, versioned claims, uniqueness, foreign keys, and resumable teardown. The latency problem came from repeated Python launches, not from the database transaction model. |
| How is one visible Champion launched now? | `league assign run` composes reservation, launching, real endpoint creation, generated thread observation, verification, activation, bounded context, and exact failure cleanup. |
| Should Garen run `supervise` or status while answering? | No. Active turns use one request-turn process. Event-driven watcher registration or direct fallback handles delivery outside the model turn; Stop is the omission backstop. |

## Immediate containment

The owner disabled only the League UserPromptSubmit hook group. League Stop
remained enabled. The active supervisor and canonical database were preserved
for diagnosis. The implementation process did not re-enable hooks, stop the
supervisor, edit SQLite directly, or rewrite canonical history.

## Corrective code

The installed journal-mode fix made four bounded changes:

1. `SQLiteStorage.for_migration` remains the maintenance path that may establish
   WAL or DELETE.
2. Normal `SQLiteStorage` opens execute read-only `PRAGMA journal_mode`, accept
   only WAL or DELETE, and validate that established WAL uses SQLite 3.51.3 or
   newer.
3. `canonical_watcher` no longer asks supervisor, prompt, or Stop connections
   to select DELETE.
4. Focused tests keep a real supervisor subprocess open while prompt and Stop
   hook subprocesses open concurrently against the same established WAL store.

Busy timeouts are unchanged. There is no schema migration, prompt rewrite,
outbox rewrite, or second canonical store.

### Successor runtime wiring

The successor candidate adds the smallest missing operational slice:

1. `league request turn` opens one process and connection. It emits exact
   retained prompt bodies only for the live owning Shotcaller, in deterministic
   order and under explicit count and byte bounds. The model authors an ordered
   semantic decision and routing plan for every prompt; the adapter supplies
   only mechanical IDs, hashes, timestamps, JSON, and command arguments.
2. One begin JSON line atomically commits all prompt items, request claims, and
   routing decisions. Missing, duplicate, reordered, stale, cross-owner, or
   conflicting content rolls back the whole begin phase. The process then holds
   no SQLite transaction while the model works.
3. One commit JSON line atomically records bounded direct answers or results,
   any resulting outbox effects, and then returns the same full obligation
   counts used by Stop. The exact PID exits only after `phase=committed`.
   Capture bytes are never rewritten and no control text is injected.
4. `league assign run` composes the existing `AssignmentService`: reserve a
   canonical callsign, mark launching, create one unfocused Herdr tab, start
   Codex, observe its generated UUID, verify endpoint/cwd/title identity,
   activate atomically, then deliver one bounded League context.
5. The launched Codex remains in workspace-write mode and receives only the
   exact canonical League state root as an additional writable root. No broad
   filesystem or network authority is added.
6. Pre-activation failure releases only a proven owned endpoint and reservation.
   Post-activation context failure first records `cleanup_pending`; it settles
   to blocked only after exact endpoint close, runtime close, and callsign
   release receipts agree.
7. The source-managed global guide now requires the one-process turn protocol,
   explicit model-authored triage, the one-command launcher, event-driven
   delivery, and proof-gated cleanup. It prohibits per-turn status, unresolved,
   or supervise polling. Legacy writers remain fenced.
8. An inaccessible database now reports `state_root_unavailable` with the
   narrow-root or trusted-broker remedy instead of the misleading generic
   storage-open failure.

### One-process turn sequence

```text
UserPromptSubmit hook (one in-process storage transaction)
        |
        v
exact prompt bytes + deterministic source identity + wake generation
        |
        v
league request turn  [one PID, one SQLite connection]
        |
        +--> emits exact bounded intake
        |       |
        |       v
        |    model chooses semantic items and routing
        |       |
        +<-- begin JSON line
        |       |
        |       v
        |    atomic triage + claims + routing plans
        |       |
        +--> begun receipt + current obligation boundary
        |
        |    no transaction held while model works
        |
        +<-- commit JSON line with answer/result references
        |       |
        |       v
        |    atomic outcomes + delivery effects
        |
        +--> phase=committed + final full obligation boundary
        |
        v
process exits; Stop remains the one-shot omission backstop
```

## Acceptance matrix

### Command-level performance evidence

The evidence has three deliberately separate layers:

1. **Protocol mock:** five same-process PTY samples without SQLite or model
   reasoning. Startup-to-intake and decisions-to-plan were each about 251–252
   ms, commit-to-boundary was 0.003–0.011 ms, total median was 503.123 ms, and
   total maximum was 504.013 ms. The two roughly 250 ms stages were dominated
   by the supported minimum tool yield. This validates only the protocol shape.
2. **Real candidate CLI + SQLite:** six exact synthetic prompts on one real
   candidate process. Median whole-turn infrastructure time was 195.750 ms;
   startup/intake was 169.193 ms, decision/plan was 3.489 ms,
   commit/boundary was 21.820 ms, and the pure database phases were 3.339 ms.
   These independently observed phase values are not mechanically added.
3. **Garen semantic triage:** excluded and intentionally variable model
   reasoning. Triage happens in the same normal Garen reasoning pass—never a
   second model invocation. Garen supplies semantic items and routing choices;
   the adapter supplies IDs, claim tokens, timestamps, hashes, locators, JSON,
   and arguments to the already-open process.

`scripts/benchmark_request_turn.py` uses synthetic temporary state and measures
command/storage overhead only; it does not compare model quality or vanilla
versus orchestrated chat. The retired JSON path has no durable semantic
triage/claim begin operation, so that phase is reported as zero rather than
invented. This omission favors JSON and means the benchmark compares the
closest preserved six-item lifecycle, not feature parity.

The corrected installed comparison uses ten samples. Process startup is the
parent-side child-process creation overhead; interpreter and CLI initialization
remain inside the following intake or commit phase and the observed total.
Intake, begin, and commit exclude model reasoning. Per-phase medians do not
mechanically sum to the median total because each statistic is computed
independently.

| Corrected path/phase | Commands per turn | Median | p95 |
| --- | ---: | ---: | ---: |
| Retired JSON process startup | 7 | 18.432 ms | 19.923 ms |
| Retired JSON intake | same 7-command turn | 234.224 ms | 286.239 ms |
| Retired JSON begin | unsupported; no fabricated write | 0.000 ms | 0.000 ms |
| Retired JSON commit | 6 per-item transition commands | 1,449.399 ms | 1,575.075 ms |
| Retired JSON total | 7 | 1,696.567 ms | 1,865.879 ms |
| Installed 0.2.21 one-process SQLite startup | 1 | 2.911 ms | 4.134 ms |
| Installed 0.2.21 one-process SQLite intake | same process | 162.527 ms | 186.899 ms |
| Installed 0.2.21 one-process SQLite begin | same process | 3.670 ms | 4.161 ms |
| Installed 0.2.21 one-process SQLite commit/boundary/exit | same process | 22.463 ms | 22.914 ms |
| Installed 0.2.21 one-process SQLite total | 1 | 191.289 ms | 216.914 ms |

The largest observed SQLite phase output was 8,854 bytes under a 1,100,000-byte
bound. The normal-turn budget is exactly one `request turn` process and zero
per-prompt status, unresolved, supervise, triage, claim, dispatch, or answer
shell-outs.

| Measurement | Process launches | Exact local result |
| --- | ---: | ---: |
| Twenty cold `request unresolved` CLI calls | 20 | 3,870.940 ms total; 193.547 ms mean |
| Prior chatty six-prompt turn | 26 | 5,259.356 / 5,322.373 / 5,453.322 ms min/median/max |
| Accepted real candidate startup → intake | 1 shared process | 169.193 ms |
| Accepted real candidate decisions → begun plan | same process | 3.489 ms |
| Accepted real candidate commit → final boundary | same process | 21.820 ms |
| Accepted real candidate whole infrastructure turn | 1 | 195.750 ms median |
| Accepted real candidate pure database phases | 0 | 3.339 ms |

The result identifies process startup and repeated connection/argument setup as
the dominant local cost. The release target is one Shotcaller League process
for an ordinary direct turn. UserPromptSubmit and Stop remain separate automatic
boundary-hook processes, each performing one bounded in-process storage
operation; they are not extra commands invoked by Garen.
The prior installed 0.2.20 E2E remains historical proof: one stable turn PID,
170.582 ms turn infrastructure, 39,168.040 ms complete lifecycle, and receipt
SHA-256 `b8d5e1f9359116f93459dd82ee4206f921b3a0138b155018f3b97f6b65c9bd5d`.
Fresh installed 0.2.21 revalidation stopped at the silent metadata receipt and
cleaned up exactly. The staged 0.2.22 candidate then used one stable PID for two
prompts: intake 155.247 ms, begin 4.069 ms, commit 4.132 ms, and 163.448 ms turn
infrastructure. Its complete visible lifecycle took 39,374.893 ms and left zero
residue. Candidate receipt SHA-256:
`4de78bf3e3028edd05ed6248cfa57ccfa58d29396bdcd524b4ea2c4ce1c25ff9`.

### Installed command inventory and deferred surfaces

| Surface | Normal use after release | Status/boundary |
| --- | --- | --- |
| `league request turn` | One ordinary Shotcaller process: intake, atomic begin, model work, atomic commit, final obligation boundary | Preferred active-turn surface. |
| `league request triage`, `claim`, `dispatch`, `answer`, `result`, `unresolved`, `untriaged` | Recovery, inspection, compatibility, or non-model automation | Supported but deferred from normal active turns because separate invocations recreate chatty overhead. |
| `league request route`, `awaiting-user`, `block`, `defer`, `cancel` | Explicit non-ordinary owner decision with claim/version evidence | Dedicated commands remain; they are not silently inferred by the adapter. |
| `league assign run` | One visible Champion reservation-through-context launch | Preferred launcher. Manual `prepare`/`launching`/`activate` remain lower-level recovery surfaces. |
| `league shotcaller create` | In-place creation from the exact calling unnamed Codex/Herdr pane | Allocates a callsign internally; creates no Squad, layout, or process. |
| `league task transition` | One material Champion transition plus event/outbox commit | One command per material transition; routine heartbeat polling is not added. |
| `league delivery claim-outbox`, `ack-outbox`, `fail-outbox`, `backlog` | Event-driven watcher/direct fallback delivery accounting | Internal/stable adapter surface; duplicate receipt is idempotent. |
| `league cleanup plan`, `execute`, `status`, `reconcile` | Proof-gated cleanup and crash resume | `reconcile` is limited to exact disposable-canary policy. |
| `league rollover prepare`, `bindings`, `acknowledge`, `commit`, `intake-plan`, `reconcile-intake`, `reconcile-descendant`, `abort`, `drain`, `status` | Explicit Shotcaller owner replacement and exact bounded successor reconciliation | Separate reliability workflow; not part of an ordinary request turn. |
| `league storage migrate`, `backup`, `import`, `integrity`, `export` | Maintenance under explicit authority | Only migrate may establish journal mode; import defaults no-apply. |
| `league roster`, `report`, `artifact`, `resource`, `evidence`, `project`, `squad`, `routing`, `skill` | Canonical read/model/policy/evidence commands | No direct SQL or legacy file dependency. |
| `agent-watcher` SQLite hooks/delivery and long-lived supervisor | Automatic hook boundary or installed event-driven runtime | Stable compatibility name; never polled by an active turn. |
| `agent-watcher launch`, legacy `transition`, legacy teardown/reconcile writers | None | Permanently fenced while SQLite is canonical. |
| Legacy JSON/JSONL mutation and direct `sqlite3` | None | Retired/unsupported; archives are read-only evidence. |

| Gate | Required evidence | Candidate status |
| --- | --- | --- |
| Established-mode unit | Long-lived WAL connection plus second hot open; mode remains WAL and both connections read canonical state | Passed |
| Actual supervisor regression | Foreground supervisor subprocess remains open while prompt and Stop subprocesses start together | Passed |
| Prompt durability | Hook returns success; exact prompt body hash and byte count exist once | Passed in synthetic focused test |
| Stop safety | Concurrent Stop returns a normal block, never raw `ERROR: busy` | Passed in synthetic focused test |
| Supervisor wake | The same foreground wait exits for user priority | Passed in synthetic focused test |
| Focused affected suites | Watcher, migration, concurrency, request, acceptance, live-cutover, and public-safety gates | Passed locally; public-safety reruns on committed bytes |
| Exact-head CI | Hosted checks bind to the published successor PR head | Pending for the 0.2.23 successor PR |
| Installed release | Tested merge tree and exact source-managed guide installed with prior release retained | Current 0.2.21 remains installed; 0.2.22 was not installed and 0.2.23 has no installation authority |
| Whole direct turn | One exact PID emits two prompt bodies, atomically begins triage/claims/routing, remains alive, atomically commits answer/result and delivery, returns full unresolved/cleanup boundary, then exits without a second League spawn | Passed in focused synthetic test |
| Turn latency | Cold CLI, 26-process prior choreography, one-process whole turn, and open-connection batch phases measured separately | Passed; exact table above |
| Visible launch | One command reserves, starts, observes generated UUID, verifies, activates, and briefs the exact Herdr/Codex runtime | Installed 0.2.21 failed on the silent metadata receipt; focused and staged 0.2.22 candidate passed |
| Sandbox access | Launched Codex receives only the exact canonical League root as an added writable root and can use stable commands | Passed in installed E2E |
| Failure cleanup | Unproven cleanup remains pending; proven endpoint/runtime/callsign cleanup settles blocked with zero active lease | Passed in focused synthetic test |
| Disposable installed E2E | Capture, explicit triage, assign run, transition, current-owner watcher delivery, terminal cleanup, zero residue, integrity | 0.2.21 failed and cleaned exactly; staged 0.2.22 passed; the corrected 0.2.23 installed gate requires separate authority |

## Successor rollover and placement regression (0.2.23 source candidate)

Post-rollover inspection found a separate canonical ownership gap. The
successor could run a valid request turn, yet newly submitted prompts were not
bound to its verified runtime, inherited prompt ownership still pointed at the
predecessor, and frozen Champion task/runtime/delivery rows could still route
material work to the predecessor. This invalidates earlier installed acceptance
claims for the affected boundaries; a staged or synthetic success is not an
installed successor-flow receipt.

The source candidate applies the following generic contracts without rewriting
capture history:

- schema 12 permits exact imported task-assignment reconciliation, schema 13
  permits standalone Shotcaller callsign scope, schema 14 adds mutable current
  prompt ownership beside immutable capture provenance, and schema 15 adds
  one-time exact Stop-feedback suppression;
- `league shotcaller create` creates a canonical Shotcaller from the exact
  current, unnamed Codex/Herdr pane, allocates its callsign internally, renames
  that same pane, verifies the unchanged thread/terminal/worktree, and performs
  no workspace, tab, pane, split, or process creation;
- `league squad register` remains a separate contract for attaching a Squad to
  an already-created live Shotcaller;
- `league assign run` remains the distinct Champion creation contract and must
  create a new Herdr tab root rather than reuse or split the Shotcaller pane;
- rollover commits only the Squad owner fence. Each frozen Champion then moves
  its agent owner, task coordinator/version, task assignment/version, callsign
  assignment/version, verified runtime, and exact still-pending deliveries in
  one compare-and-swap transaction. Claimed, missing, closed, ambiguous, stale,
  broad, or mismatched state refuses;
- `league rollover intake-plan` exposes repeatable exact pages. Reconciliation
  transfers only current triage/request/obligation ownership; original prompt
  actor, runtime, session, source key, body identity, and creation time remain
  immutable. New requests from inherited prompts retain the original requester;
- Stop displays the resolved callsign, never the raw provider turn identifier.
  Only League's exact emitted feedback token for that scope, turn, and
  generation is suppressed. A genuine second native steer in the same turn
  rearms one block.

### Regression-ticket matrix

| Ticket | Prior accepted contract now under regression | Source acceptance in this candidate |
| --- | --- | --- |
| #3 | Exact material delivery reaches the current owner and remains pending when unavailable | Descendant delivery moves only with its canonical task/assignment transfer; claim races refuse. |
| #5 | Stop blocks once without an infinite self-feedback loop | Callsign-only text plus exact one-time self-feedback suppression and genuine-steer rearm. |
| #7 | Runtime identity is provider-bound and fail-closed | Current-pane Shotcaller bootstrap and imported-Champion live adapter verification require exact thread/pane/terminal/worktree evidence. |
| #17 | Every prompt is durable before semantic action | First successor prompt binds with zero quarantine; mutable current owner does not alter immutable capture provenance. |
| #66 | One process spans intake, semantic begin, and outcome commit | The same PID remains open through both phases; a concurrent native steer is captured promptly without a polling wait. Installed proof remains pending. |

### Four independent clocks

1. **Native steer:** the Codex provider accepts and exposes a real user message
   to the active turn. League does not poll or impose a checkpoint allowance on
   this latency.
2. **Event wake:** prompt capture publishes user priority and a Champion
   material event wakes an event-driven watcher or exact direct fallback.
3. **Checkpoint lifetime:** an optional fallback may remain yielded and
   interruptible; it is not started inside an ordinary active turn and its
   maximum lifetime is not a prompt-latency budget.
4. **Stale health:** heartbeat grace classifies watcher health independently of
   native steering, event delivery, and checkpoint lifetime.

Focused source tests now cover schema 15 migration/receipt alignment, one
same-PID successor capture/begin/commit flow with zero quarantine, a prompt hook
accepted while that process is yielded, all six semantic dispositions without
minting a deferred request, more-than-500 intake paging, A-to-B-to-C provenance,
fake-only Herdr descendant verification, exact CAS transfers, delivery claim
races, all three placement contracts, and Stop feedback/rearm behavior. These
are source-candidate results only. Installed 0.2.21 remains unchanged and the
owner-visible successor-to-Champion lifecycle is still a post-merge,
separately-authorized release gate.

## Issue #66 successor: inline triage and persistent event supervision

The owner-source issue-#66 investigation found two later live-path failures:
canonical persistent supervision was off, and genuine owner steers were not
appearing in the active Shotcaller's final intake boundary. The repository-local
successor does not repair live state. It adds the source boundary required for
a separately authorized repair:

The current installed League version observed read-only for issue #66 is
0.2.27. Its watcher command inventory does not include the source candidate's
service lifecycle, and no persistent League service is live. Historical 0.2.21
and 0.2.23 observations elsewhere in this incident remain explicitly scoped to
those earlier releases.

- UserPromptSubmit is a bounded exact-capture client of one persistent local
  service, not a per-turn supervisor or foreground wait.
- One renewable/fenced registration and same-user Unix socket carry exact user
  priority and Champion events without idle snapshot polling.
- The active Shotcaller authors semantic JSON during its normal reasoning turn;
  ordinary `request turn` starts no second classifier.
- Pre-decision intake automatically includes a 12-row/24,576-byte deterministic
  same-owner candidate shortlist. Truncation cannot block a direct answer, but
  incomplete or changed inventory fences external dispatch.
- SQLite provides exact source-event idempotency, not fuzzy matching. Duplicate,
  follow-up, and deferred decisions cite one supplied candidate/version.
- Stop only reports bounded unresolved summaries. An explicit schema-16
  compare-and-swap transition lets the active Shotcaller supersede one
  same-owner duplicate while preserving both sources and refusing any request
  with external execution evidence.

The complete design, immutable Luna before-baseline, measured 27-cell inline
matrix, limitations, and remaining install/live gates are in
[`docs/research/issue-66-inline-triage-supervision-benchmark.md`](research/issue-66-inline-triage-supervision-benchmark.md).
The service template remains inert. No install, hook edit, service load, model
route, live import, or cutover occurred.

## Rollback

The 0.2.23 candidate advances the database from schema 11 through the contiguous
schema-15 migration sequence. Migration, staged acceptance, cutover receipt,
backup, integrity check, and rollback must all name schema 15 exactly. If a
future authorized installation or acceptance fails:

1. keep UserPromptSubmit disabled;
2. atomically restore the prior installed League release pointer;
3. preserve the canonical WAL database, prompt rows, obligations, outbox, and
   supervisor evidence unchanged;
4. do not retry the rejected prompt or fabricate capture;
5. record the first failing gate and keep issue #23 open.

### Required release and teardown receipts

The final installed gate must bind rather than narrate each effect:

| Receipt | Required binding |
| --- | --- |
| Release/install | Tested source commit/tree, merged commit/tree parity, version, tracked input hashes, installed executable/package hashes, guide source/installed hash, backup identity, installed smoke, rollback verification. |
| Turn | One process identity for all phases, ordered prompt IDs/body hashes/byte counts, triage digest, claims, routing receipts, answer/result receipts, outbox effects, final obligation counts. Hosted evidence omits the local PID. |
| Launch | Request/claim/task/assignment/agent/callsign identity, repository/issue/worktree binding, generated thread identity, backend/routing/display/cwd verification, context hash/bytes/effect hash. |
| Transition/delivery | Transition key, task version, event ID, outbox ID, recipient, watcher or direct effect receipt, duplicate-retry result, pending result for an unavailable recipient. |
| Cleanup plan | Task class, proof-manifest digest, operation ID, action order, owner fence, lease, first unproven boundary. |
| Cleanup action | Action ID/kind, external effect digest, before/after identity, receipt hash, operation fence, completion time. |
| Crash resume | Prior completed action receipts, first unreceipted action, new lease/fence, no replay of proven external effects, final teardown receipt. |
| Teardown | Exact endpoint/runtime close, worktree/branch decision, preserved remote branch and artifacts/history, callsign release, cleanup settlement, zero-residue Roster/report, integrity result. |
| Legacy retirement | Inactive source-tree hash manifest, archive identifier, restoration runbook hash, absent-active-path cold-start checks, rollback move proof. |

## Remaining risks

- A supervisor process started before installation continues running its already
  loaded code until the owner replaces it.
- Unsupported or externally altered journal modes must remain fail-closed.
- WAL safety still depends on the loaded SQLite runtime meeting the pinned
  minimum version.
- Hook success alone still does not prove canonical prompt capture; acceptance
  must inspect the stable League storage surface.
- The one-process turn is bounded by count/bytes and transaction phases, but a
  model that never sends its commit line leaves the process waiting; Stop and
  owner interruption remain the recovery boundary. No permanent daemon is
  introduced.
- Cross-Squad acknowledgement routing and explicit defer/block/cancel remain
  dedicated commands instead of being folded into the ordinary direct-turn
  batch. This preserves semantic authority.
- UserPromptSubmit remained owner-disabled during release and acceptance. The
  implementation does not change that owner-controlled hook setting.
- The passing 0.2.22 lifecycle is staged-candidate evidence, not installed
  proof. The 0.2.23 successor correction remains source-only. The stable pointer
  remains 0.2.21 until separately authorized release installation and rollback
  verification.

## Action items

| Priority | Action | Owner | Tracking | Status |
| --- | --- | --- | --- | --- |
| P0 | Make normal connections validate rather than change established journal mode | Issue #23 implementer | Issue #23 | Installed and accepted |
| P0 | Add long-lived supervisor plus concurrent prompt/Stop regression | Issue #23 implementer | Issue #23 | Installed and accepted |
| P0 | Add one-process exact-body intake, atomic semantic begin, atomic answer/result commit, and full obligation boundary without prompt injection | Issue #23 implementer | Issue #23 | Installed and accepted |
| P0 | Add adapter-backed `league assign run` with narrow canonical-root access | Issue #23 implementer | Issue #23 | Installed and accepted |
| P0 | Archive the retired JSON-era guide contract and install only SQLite-native guidance from exact merged source bytes | Issue #23 implementer | Issue #23 | Installed with byte parity |
| P0 | Publish Markdown and self-contained HTML incident artifacts | Issue #23 implementer | Issue #23 | Complete |
| P0 | Run focused tests, public-safety scan, and exact-head CI | Issue #23 implementer | Issue #23 | 0.2.23 successor PR pending |
| P0 | Install the exact tested release with rollback retained | Release owner | Issue #23 | No authority for 0.2.23; pointer preserved at 0.2.21 |
| P0 | Run one installed disposable capture-to-cleanup E2E and prove zero residue/integrity | Issue #23 implementer | Issue #23 | Staged 0.2.22 passed; corrected 0.2.23 installed gate pending |
| P0 | Publish corrected retired-JSON versus installed one-process median/p95 and command-count evidence | Issue #23 implementer | Issue #23 | Complete |
| P0 | Re-enable UserPromptSubmit only after the installed disposable gate | Owner | Issue #23 | Not yet safe; not changed by this work |
| P1 | Keep journal-mode mutation restricted to explicit maintenance commands | League maintainers | Storage contract | Ongoing invariant |

## Resolution criterion

Issue #23 does not yet meet its final installed resolution criterion. The
staged 0.2.22 candidate proved two exact prompt captures; one stable turn PID
for model-authored triage, routing, and commit; one verified visible Champion
with canonical-root access; working and completed watcher delivery; exact
endpoint/worktree/branch cleanup; callsign release; no unresolved obligations;
zero residue; and SQLite integrity. The same proof must run after the exact
0.2.23 successor bytes pass PR/CI, are merged under separate authority, and are
installed under separate authority with rollback retained.
