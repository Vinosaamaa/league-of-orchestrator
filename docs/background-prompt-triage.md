# Dedicated prompt triage — issue #66

Decision: approved for implementation, including the prompt-only ON/OFF switch.
Status: local implementation in progress; not installed or accepted end to end.

Verification: local baseline passes. The affected lifecycle run passed through
the existing Stop cases, then exposed obsolete automatic-acknowledgement
assertions. Updated focused receive cases and the remaining lifecycle checks
passed. Multi-request inbox coverage verifies distinct IDs without false task
completion. Atomic idle-only send and native wait interruption remain unverified;
these test results do not authorize declaring installed acceptance.

## Active shipping goal

Solo owner completes the orchestrator, not Job Journey or Interview Prep.
Finish existing Champion work without further implementation delegation, ship
the approved runtime changes, verify the installed end-to-end flow, then retire
each eligible Champion with its work and receipts preserved. Do not equate a
merged source slice or an empty prompt queue with product completion.

- [x] Restore installed supervisor health through the source-managed release.
- [ ] Complete persistent prompt classification and prompt-only ON/OFF.
- [ ] Deliver Champion and triage updates through busy inbox / wait / idle wake.
- [ ] Verify interruptible waiting, or report the actual provider limitation.
- [ ] Merge, install, and pass live prompt-to-answer-to-cleanup acceptance.
- [ ] Reconcile all outstanding orchestrator requests and inherited tasks.
- [ ] Clean up every eligible Champion individually; preserve unproven work.

The product goal record currently retains an older objective. This checklist
records the owner's revised scope; the available goal API can update status,
but cannot edit an active objective. Job Journey and Interview Prep are excluded.

Recovery receipt: the committed scheduling candidate passed the exported-source
baseline and installed through the supported service installer. Repeated native
health probes across renewal verified all three existing bindings with unchanged
fences. A sandboxed probe instead returned `PermissionError`; its generic
`process_unreachable` report was not evidence of service failure. Probe permission
diagnostics are being corrected separately. Background triage remains excluded
from this installed recovery; full product acceptance is still pending.

Acceptance: one persistent, League-owned classifier; compact internal delivery
of canonical request IDs; prompt-only ON/OFF; no nested classifier agents;
no inference for idle polling or known answer/status updates. Champion tasks,
requests, deliveries, supervision, and cleanup continue unchanged when OFF.

The worker is not a native subagent of the Shotcaller. Its inference backend is
independent of the recipient's Codex, Cursor, or Pi delivery adapter. It never
executes requested work or declares that work completed.

The Shotcaller consumes the classified checklist before reporting per-request
answers. It retains ownership of execution, authorization and evidence-backed
answer receipts. An early acknowledgement cannot close unknown subrequests.

## Policy

Unconfigured owners retain the installed inline behavior for compatibility.
Enabling prompt triage transfers pending prompt classification to the worker.
Disabling pauses that pending queue, preserves existing requests, and explicitly
skips classification for prompts arriving while OFF. Those skipped prompts are
not replayed on reenablement. No switch touches Champion state.

Classification and its internal notification commit together. Native intake
must verify and exclude internal notifications so delivery cannot recursively
create prompts. Retries preserve exact ownership, versions and duplicate
protection. A configuration change invalidates an in-flight classification.

OFF must not block the Shotcaller on paused prompt classification. It does not
cancel or complete existing requests, disable hooks, stop the watcher, or change
Champion request handling. The effective mode must be inspectable per owner.

## Delivery and status ownership

League records a pending notification with the committed checklist, then makes
it available to the recipient's registered receive path. No human follow-up or
Stop-hook invocation is required to initiate delivery. A failed delivery remains
pending; a successful submission is not proof that the Shotcaller acted on it.

Internal delivery is still model input. The existing push transport submits a
verified operational message through the agent prompt interface; it is not a
new human prompt. A pending foreground wait can instead return an event as tool
output. Do not promise silent model-context mutation or immediate interruption
of every provider. Verify each supported receive path before claiming parity.

The existing push policy defers notifications while a League Shotcaller turn is
active (`owner_turn_active`). This is a request-processing marker cleared by
`commit_interactive_request_turn`, not proof of provider idleness and not the
Stop-hook boundary. Its absence must not authorize prompt injection into an
otherwise busy Shotcaller.

The default receive policy applies equally to Champion and triage updates.
Busy delivery requires an explicit, bounded in-turn inbox
read/checkpoint that returns committed checklists as tool output, without a new
prompt or an end-turn prerequisite. Champion events use the same checkpoint.
The worker must remain able to commit while
the owner turn is active. If classification is pending, expose that fact rather
than inventing request IDs or marking the prompt answered.

Only verified provider-idle delivery uses an internal wake message. An active
foreground wait returns an event through its tool result instead. Busy or
unknown provider state retains pending work for the checkpoint, never falls
back to prompt spam. The idle check and send must be bound to the exact runtime
and handle a concurrent transition back to busy without blindly submitting.
Checkpoint consumption and push delivery reconcile the same notification
identity so an event already consumed in-turn does not cause a later wake.
The local inbox implementation reserves each returned update for 60 seconds;
exact acknowledgement records receipt, not task completion. An interrupted
read expires and can be claimed again. The foreground receive has its own
30-second renewable lease; the older `wait_active` flag also describes general
supervision and must not suppress push delivery by itself. A killed wait loses
its receive lease without deleting any notifications.
Until this path
is implemented and tested, do not enable background triage live or claim that
Champion delivery alone supplies in-turn triage results.

Firstmate source comparison: its Codex supervision protocol uses explicit
`fm-wake-drain.sh` reads and a foreground checkpoint only at a genuine idle
boundary. Its Pi watcher instead calls `sendUserMessage` with
`deliverAs: "followUp"`; Cursor uses a stop-hook-owned park and follow-up.
These are distinct provider mechanisms, not proof of a universal automatic
busy-context injection facility. Reuse durable queue presentation and explicit
acknowledgement, not an assumption that every provider has the same receive API.

The worker only splits/classifies prompts. League supplies request IDs and
delivers their concise summaries. The Shotcaller uses those IDs to claim work
and record evidence-backed answers through existing request commands. Known
answer/status updates require no classifier call. Stop remains an omission
check, not a classifier or automatic completion mechanism.

Champion status transitions already update canonical state and enqueue their
notifications together. They require no prompt classification and remain
independent of the prompt-triage switch.

## Interruptible foreground waiting

Approved requirement: foreground waiting returns on host-observed user input,
a pending League event, or its bounded deadline. User input takes priority.
Cancellation retires only the exact wait operation, never the watcher, queued
events, a Champion, or unrelated tools. A League event is returned as tool
output, without a prompt submission. An event arriving concurrently with user
input stays pending unless its exact receipt is acknowledged.

The current shell wait observes SQLite's `user_message_generation` after
capture; it cannot observe input still queued inside a provider UI. Reducing
the database poll interval does not establish host-level interruption. A new
adapter must connect the actual host input/cancellation signal to the wait.
This session's interruptible clock tool demonstrates that host's behavior,
not a portable capability already exposed by installed provider CLIs.

Codex's official app-server documentation distinguishes `turn/steer` (append
input to the active turn) from `turn/interrupt` (cancel the entire turn).
Neither description alone proves cancellation of one wait tool. Do not replace
wait-only cancellation with a whole-turn interrupt or reconnect/resume an
already active writer. Verify the installed adapter and owned host connection
before using any app-server capability:
https://learn.chatgpt.com/docs/app-server

Where host cancellation is unavailable, label a short bounded-wait fallback
explicitly; do not claim instant response or zero model overhead. Idle waiting
must not become a tight model-driven polling loop. Measure human submission to
wait-return latency, event-to-tool-result latency, and idle inference usage in
real provider acceptance, separately from SQL and classification timings.

## Token and latency boundaries

The classifier receives exact new prompt text and bounded relevant request
context, not the Shotcaller's transcript. It returns semantic changes only;
code supplies IDs, timestamps and version fields. One persistent process serves
the queue; it makes no inference calls while idle. Report input/output tokens
and queue-to-classification latency separately from database commit time.
Both models may read the original prompt, so lower total token usage is not
guaranteed. The primary benefit is moving classification off the conversation's
critical path. Do not describe persistence as free inference.

The Codex backend reuses one stdio process with ephemeral per-prompt request
contexts. These are not nested subagents. It unsubscribes the prior context
before starting the next, so previous prompt bodies are not replayed. The
installed app-server refuses `thread/rollback` for paginated threads, despite
exposing its deprecated schema; that mechanism is not used. Fixed provider
instruction overhead remains measurable and must not be called free or minimal.

## Required acceptance

- One persistent classifier processes easy and multi-request prompts without
  nested agents; exact prompt capture does not wait for inference.
- Each committed request checklist reaches the exact Shotcaller, is excluded
  from human-prompt capture, and supports per-request answer receipts.
- With the Shotcaller turn still active, classification commits and its inbox
  checkpoint returns the checklist before that turn ends; subsequent push
  recovery does not wake the owner again for the consumed notification.
- Champion events follow the same busy-checkpoint/idle-wake contract, including
  a provider still busy after its League request marker has been committed,
  unknown provider state, and a busy/idle race. No separate prompt is sent while
  busy, and no queued event is lost during the receive-path transition.
- User input interrupts a live foreground wait through the provider's actual
  input path without pressing Escape; simultaneous Champion/triage events stay
  durable, and cancellation leaves supervision and unrelated tools running.
- OFF pauses pending classification, skips new OFF-period prompts explicitly,
  invalidates in-flight work, and preserves every existing Champion obligation.
- ON resumes the paused queue without replaying OFF-period skipped prompts.
- Duplicate deliveries and retries do not create duplicate requests; worker or
  delivery failure preserves pending work and exposes a bounded diagnostic.
- Installed end-to-end verification covers capture, classify, delivery, answer,
  switch OFF/ON, and unchanged Champion updates before declaring completion.

Implementation lane: solo owner, existing issue #66 worktree, branch
`fix/66-background-prompt-triage`; one follow-up PR with focused synthetic
coverage and end-to-end acceptance before enabling the feature live.
