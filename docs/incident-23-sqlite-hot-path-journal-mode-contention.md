# Incident Review: SQLite hot-path contention and incomplete runtime wiring

**Issue:** [#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23)  
**Date:** 2026-08-29  
**Severity:** P0  
**Status:** Journal mode and owner-visible turn latency accepted; final installed lifecycle gate pending

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
against synthetic record pairs and the installed one-process SQLite command
against the same six summaries and outcomes. Installed SQLite measured one
process/command, 198.795 ms median and 220.493 ms p95 total. Retired JSON needed
seven processes/commands, 1,435.357 ms median and 1,504.255 ms p95 total even
though its missing durable semantic-begin phase was counted as zero in its
favor. The active source guide now contains only SQLite instructions; the
retired record contract is preserved as a non-installable historical artifact.

This report does **not** declare issue #23 resolved. Resolution requires the
exact installed disposable flow from capture and explicit triage through launch,
transition, Shotcaller delivery, proof-gated cleanup, callsign release, zero
active residue, and SQLite integrity.

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
| Corrected installed comparison | Ten same-workload samples measured retired JSON at 7 commands and 1,435.357/1,504.255 ms median/p95 versus installed SQLite at 1 command and 198.795/220.493 ms. The absent JSON semantic-begin phase was reported explicitly as zero, not fabricated. |
| Guide retirement | The final JSON-era orchestration excerpt was archived with its full source hash; the installable source guide and generated Champion context removed active record-pair and retired-writer instructions. |
| Documentation gate | The owning incident/design report was expanded to record architecture, failure history, decisions, commands, performance, recovery, acceptance, and remaining limits before release. |

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
   starts Codex in workspace-write, adds only the canonical state root, observes
   the generated thread UUID, and verifies workspace, pane, terminal, cwd,
   routing name, backend kind, and display title.
5. Activation stores the verified runtime and callsign receipt atomically. A
   bounded SQLite-only context is delivered once and its content/effect hashes
   become an immutable event.
6. Any post-effect refusal closes only the proven owned endpoint. If closure,
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
| Retired JSON process startup | 7 | 17.673 ms | 19.317 ms |
| Retired JSON intake | same 7-command turn | 197.936 ms | 238.892 ms |
| Retired JSON begin | unsupported; no fabricated write | 0.000 ms | 0.000 ms |
| Retired JSON commit | 6 per-item transition commands | 1,213.282 ms | 1,260.159 ms |
| Retired JSON total | 7 | 1,435.357 ms | 1,504.255 ms |
| Installed one-process SQLite startup | 1 | 2.526 ms | 2.860 ms |
| Installed one-process SQLite intake | same process | 169.672 ms | 192.753 ms |
| Installed one-process SQLite begin | same process | 3.587 ms | 3.949 ms |
| Installed one-process SQLite commit/boundary/exit | same process | 21.614 ms | 31.996 ms |
| Installed one-process SQLite total | 1 | 198.795 ms | 220.493 ms |

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
The installed E2E measures the combined infrastructure path as a separate
fourth result. It does not add mock, candidate, or model reasoning numbers.
Multi-second installed handoffs fail the gate and require an in-process League
adapter while preserving one external Garen process and SQLite.

### Installed command inventory and deferred surfaces

| Surface | Normal use after release | Status/boundary |
| --- | --- | --- |
| `league request turn` | One ordinary Shotcaller process: intake, atomic begin, model work, atomic commit, final obligation boundary | Preferred active-turn surface. |
| `league request triage`, `claim`, `dispatch`, `answer`, `result`, `unresolved`, `untriaged` | Recovery, inspection, compatibility, or non-model automation | Supported but deferred from normal active turns because separate invocations recreate chatty overhead. |
| `league request route`, `awaiting-user`, `block`, `defer`, `cancel` | Explicit non-ordinary owner decision with claim/version evidence | Dedicated commands remain; they are not silently inferred by the adapter. |
| `league assign run` | One visible Champion reservation-through-context launch | Preferred launcher. Manual `prepare`/`launching`/`activate` remain lower-level recovery surfaces. |
| `league task transition` | One material Champion transition plus event/outbox commit | One command per material transition; routine heartbeat polling is not added. |
| `league delivery claim-outbox`, `ack-outbox`, `fail-outbox`, `backlog` | Event-driven watcher/direct fallback delivery accounting | Internal/stable adapter surface; duplicate receipt is idempotent. |
| `league cleanup plan`, `execute`, `status`, `reconcile` | Proof-gated cleanup and crash resume | `reconcile` is limited to exact disposable-canary policy. |
| `league rollover prepare`, `bindings`, `acknowledge`, `commit`, `abort`, `drain`, `status` | Explicit Shotcaller owner replacement | Separate reliability workflow; not part of an ordinary request turn. |
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
| Exact-head CI | Hosted checks bind to the published successor PR head | Pending |
| Installed release | Tested merge tree and exact source-managed guide installed with prior release retained | Pending |
| Whole direct turn | One exact PID emits two prompt bodies, atomically begins triage/claims/routing, remains alive, atomically commits answer/result and delivery, returns full unresolved/cleanup boundary, then exits without a second League spawn | Passed in focused synthetic test |
| Turn latency | Cold CLI, 26-process prior choreography, one-process whole turn, and open-connection batch phases measured separately | Passed; exact table above |
| Visible launch | One command reserves, starts, observes generated UUID, verifies, activates, and briefs a fake Herdr/Codex runtime | Passed in focused synthetic test |
| Sandbox access | Launched Codex receives only the exact canonical League root as an added writable root and can use stable commands | Pending real gate |
| Failure cleanup | Unproven cleanup remains pending; proven endpoint/runtime/callsign cleanup settles blocked with zero active lease | Passed in focused synthetic test |
| Disposable installed E2E | Capture, explicit triage, assign run, transition, Garen watcher delivery, terminal cleanup, zero residue, integrity | Pending and required for resolution |

## Rollback

The release contains no database schema or data migration. If installed
acceptance fails:

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
  loaded code until the owner replaces it for the acceptance gate.
- Unsupported or externally altered journal modes must remain fail-closed.
- WAL safety still depends on the loaded SQLite runtime meeting the pinned
  minimum version.
- Hook success alone still does not prove canonical prompt capture; acceptance
  must inspect the stable League storage surface.
- The operational launcher and guide have not yet passed exact-head CI,
  installation, or the one disposable real E2E.
- The one-process turn is bounded by count/bytes and transaction phases, but a
  model that never sends its commit line leaves the process waiting; Stop and
  owner interruption remain the recovery boundary. No permanent daemon is
  introduced.
- Cross-Squad acknowledgement routing and explicit defer/block/cancel remain
  dedicated commands instead of being folded into the ordinary direct-turn
  batch. This preserves semantic authority.
- Writable-root syntax is mechanically covered but remains unproven against the
  installed Champion sandbox until the real launch gate.
- UserPromptSubmit remains owner-disabled until the exact installed acceptance
  is ready; the implementation does not re-enable it.

## Action items

| Priority | Action | Owner | Tracking | Status |
| --- | --- | --- | --- | --- |
| P0 | Make normal connections validate rather than change established journal mode | Issue #23 implementer | Issue #23 | Installed and accepted |
| P0 | Add long-lived supervisor plus concurrent prompt/Stop regression | Issue #23 implementer | Issue #23 | Installed and accepted |
| P0 | Add one-process exact-body intake, atomic semantic begin, atomic answer/result commit, and full obligation boundary without prompt injection | Issue #23 implementer | Issue #23 | Candidate complete |
| P0 | Add adapter-backed `league assign run` with narrow canonical-root access | Issue #23 implementer | Issue #23 | Candidate complete |
| P0 | Archive the retired JSON-era guide contract and install only SQLite-native guidance from exact merged source bytes | Issue #23 implementer | Issue #23 | Candidate complete; install pending |
| P0 | Publish Markdown and self-contained HTML incident artifacts | Issue #23 implementer | Issue #23 | Candidate complete |
| P0 | Run focused tests, public-safety scan, and exact-head CI | Issue #23 implementer | Issue #23 | Local tests passed; CI pending |
| P0 | Install the exact tested release with rollback retained | Release owner | Issue #23 | Pending |
| P0 | Run one installed disposable capture-to-cleanup E2E and prove zero residue/integrity | Issue #23 implementer | Issue #23 | Pending |
| P0 | Publish corrected retired-JSON versus installed one-process median/p95 and command-count evidence | Issue #23 implementer | Issue #23 | Installed comparison passed; final E2E timing pending |
| P0 | Re-enable UserPromptSubmit only after the installed disposable gate | Owner | Issue #23 | Pending |
| P1 | Keep journal-mode mutation restricted to explicit maintenance commands | League maintainers | Storage contract | Ongoing invariant |

## Resolution criterion

Issue #23 remains open. The incident may be called resolved only after the exact
installed disposable gate proves: two prompts capture and the same one-process
turn receives explicit model-authored triage/routing and commits without
conflict or busy; `league assign run` launches one
verified visible Champion that can access canonical League state; the Champion
transitions working and wakes Garen; terminal cleanup closes only that endpoint,
releases the callsign, leaves zero active residue, and passes SQLite integrity.
