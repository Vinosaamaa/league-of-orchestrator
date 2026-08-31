# Issue #85: Launch title overwrite

## Incident

The visible Champion launcher wrote and verified `<Callsign> · <Task>` before
delivering its launch handshake or assignment context. Herdr/Codex could then
auto-title the visible sidebar, thread, and terminal from that prompt. League
recorded context delivery without observing the final display state, so the
canonical routing identity remained correct while the human-facing identity
drifted to prompt text.

The same class affected in-place Shotcaller bootstrap. An owner prompt could
auto-title the previously unbound Codex pane after League published the newly
allocated callsign, causing final metadata verification to race the provider
write and roll back an otherwise exact same-pane creation.

After that correction shipped, one active Champion created by the older
launcher still retained prompt-derived display metadata. Its assignment,
runtime, callsign, route, and endpoint were exact, but it had no modern
launch-title ownership/final-sequence receipt. Normal `assign run` retry must
not reinterpret that legacy state or manufacture ownership.

A separate live in-place Shotcaller attempt exposed a transient provider read
failure: the exact current-pane query returned non-JSON once, so bootstrap
failed before Squad creation even though a later read of the same pane was
valid. Retrying a read-only identity observation is safe; replaying rename,
metadata, reservation, runtime, or Squad writes is not.

## Owning-layer correction

`league assign run` now treats the initial metadata report as a seed, not the
final acceptance gate. The real launch adapter applies assignment-scoped title
ownership and source tokens, waits for the bounded context turn to settle,
restores the exact callsign/task metadata with a sequence derived from the
fresh endpoint observation, and requires two identical final observations of
all three visible title surfaces before context delivery is recorded.

The final ordering invariant is:

1. verify the generated runtime and exact endpoint;
2. seed launch-owned routing and display metadata;
3. activate and deliver the bounded assignment context with a settled wait;
4. require the same endpoint, thread, routing name, metadata source, agent
   authority source, and ownership token;
5. restore `<Callsign> · <Task>` with the fresh observed sequence;
6. require two consecutive matching sidebar, task-label, thread-title, and
   terminal-title observations at one state-change sequence;
7. bind that source/sequence observation into the successful context receipt.

`league shotcaller create` applies the same stable-observation rule without a
new prompt or endpoint. It publishes the callsign into the calling pane, permits
at most one restoration when the effective presentation source is the same
Codex authority or the bootstrap source, and requires two matching source and
state-sequence observations before activation. Exact retry re-observes the pane
and creates no layout or process. A newer user-owned presentation source is
never overwritten; rollback clears only the League route, preserves that title,
restores only League-owned sidebar/thread tokens to the durable baseline,
preserves unrelated user tokens, releases the exact reservation, and registers
no Squad.

An exact completed retry sends no handshake or context prompt. It re-observes
the live endpoint and either accepts the stable title or performs one
ownership-safe restoration. If a newer user-owned or unowned write is present,
League performs no metadata mutation and records `cleanup_pending` against the
exact runtime. Unproven cleanup remains a truthful cleanup obligation.

Generated Champion task labels use a deterministic two-word default derived
from the task summary. Explicit labels remain limited to at most two words, so
the generated visible contract has no vague one-word fallback.

## Legacy recovery invariant

`league assign reconcile-legacy-display` is the only supported recovery path
for an already-active pre-fix Champion. It requires explicit owner
authorization plus exact assignment version, Champion agent, runtime,
callsign, pane, terminal, thread, physical worktree, route, two-word target,
and the complete expected source/title/state-sequence tuple.

The stable command contract is:

```text
league --state-root <canonical-root> assign reconcile-legacy-display \
  --assignment-id <exact-assignment> --expected-version <version> \
  --champion-agent-id <exact-agent> --runtime-instance-id <exact-runtime> \
  --callsign <exact-callsign> --pane-id <exact-pane> \
  --terminal-id <exact-terminal> --thread-id <exact-thread> \
  --worktree <exact-physical-worktree> --routing-name <exact-route> \
  --expected-presentation-json <source-title-sequence-json> \
  --target-task-label <exact-two-word-label> --owner-authorized --at <timestamp>
```

The presentation JSON normally has exactly three fields: `source`, `title`,
and the integer `state_change_seq`. A retained endpoint may add only
`"agent_status":"done"`; no other status or extra field is accepted.

The store first appends one immutable intent after proving that the canonical
assignment/runtime/route are exact, that only one live runtime matches, and
that no valid modern launch-title receipt exists. The adapter then requires two
fresh identical observations of the expected legacy presentation. Its single
metadata report uses a reconciliation-specific League source because Herdr
sequence freshness is source-scoped. The expected global state-change sequence
still fences the observation: any interleaved provider or user write makes the
post-effect sequence inexact. League then clears only its own source-scoped
title and tokens, verifies the newer presentation is stable, and refuses the
race. Final success requires two identical observations of the target sidebar,
task label, thread title, terminal title, League source, exact next global
sequence, and unchanged unrelated token map. Only then does SQLite append the
immutable final receipt.

An exact completed retry re-observes the final endpoint and returns the stored
receipt byte-for-byte without another metadata report. Changed identity,
route, expected presentation, user title, runtime cardinality, target, intent,
or final observation refuses. A failed race retains the intent but never
records a false result.

The operation does not create, replace, stop, or clean a runtime; allocate or
release a callsign; create layout; register a Squad; or mutate task progress.
It patches only display keys owned by this recovery and preserves every
unrelated metadata token.

### Retained-done classification

A pre-fix Champion may finish at the provider while League deliberately retains
its pane, route, runtime row, assignment, and callsign. That endpoint is not an
active worker and must not be made to look active merely to repair its stale
handshake title. The retained-done v2 intent is therefore available only when
the exact canonical assignment and callsign assignment are still active, one
verified runtime binds the same agent/thread/pane/generation, the route and
physical worktree remain exact, the task is already in a terminal state, and
the legacy context contains no modern display receipt. If the task is not yet
terminal, reconciliation refuses with `legacy_display_lifecycle_unsettled`;
the ordinary durable task transition must settle lifecycle first.

The v2 intent records the terminal lifecycle class and expected provider status
`done`. The final receipt binds that same status to the stable source, title,
sequence, and observation digest. Metadata repair does not prompt, start,
close, rename, or resume the endpoint, and the final canonical events use a
completed status instead of claiming active work. Two fresh observations still
fence the effect, unrelated tokens are preserved, modern ownership metadata or
identity drift refuses, and exact retry revalidates the stored receipt without
a second report.

## Canonical role-token invariant

League publishes exactly one provider-neutral role key with its owned display
overlay: `orchestrator_role=shotcaller` for a canonically verified Shotcaller or
`orchestrator_role=champion` for a canonically verified Champion. The value is
derived from the assignment/callsign role, never from provider title text or a
best-effort guess. Missing and unknown roles therefore emit no token.

Champion launch, active retry, legacy reconciliation, and retained-done
reconciliation include the token in the same final source/sequence receipt as
their title. Shotcaller create and exact retry include it in the existing
title-owner/source overlay and the durable creation event receipt. Codex and
Cursor presentation authorities follow the same rule. An exact owned retry may
restore a changed value; an unowned source or ambiguous role refuses. Rollback
clears only League's role token while preserving unrelated provider/user
metadata.

This is only the League metadata half of the cross-repository presentation
contract. League adds no marker text, glyph, color, ANSI, conditional renderer
logic, name/title length change, or pane styling.

Shotcaller bootstrap applies a corresponding read boundary: current-pane and
agent-inventory queries may retry malformed JSON up to three attempts each, but
only before any canonical or Herdr mutation. Valid-but-mismatched identity and
persistently malformed output still refuse. Rename and metadata reports are
never retried by this mechanism, preserving provider and placement safety.

## Retired bootstrap retry invariant

A clean failed in-place Shotcaller bootstrap retains its original rolled-back
callsign assignment and a version-2 retired `unbound` agent row. A later create
for the same exact agent and running Codex thread may atomically re-reserve that
same callsign and rebind the retired row only when the stored bootstrap
baseline proves the same terminal/thread generation and the prior assignment,
rollback event, role, scope, and failure receipt are complete.

The pre-baseline compatibility path is narrower: it accepts only the complete
version-2 rolled-back shape with exactly empty agent metadata and no active
resource. The current route must be absent, the presentation source must not be
League-owned, and sidebar, thread, and terminal titles must contain no retired
callsign. League stores that clean presentation as a v2 durable baseline in the
same transaction as re-reservation, then requires an exact second
source/title/token/thread/terminal/generation/sequence observation before any
Herdr publication. Before that rename, League persists one immutable publication
attempt bound to the reservation, agent, callsign, endpoint identity, provider
presentation, and v2 baseline digest. If the process crashes immediately after
the routing rename, an exact retry resumes only when the alias is the reserved
callsign and every endpoint and presentation byte still matches that attempt.
The current global state-change sequence may be newer because unrelated state
can advance it; League does not reuse that global value as Herdr's source-local
metadata sequence. Retry skips the duplicate rename, applies an explicitly
owner-tagged League overlay, and accepts only the exact first post-effect global
fence followed by two stable observations. A later provider or user
presentation is never reasserted over. A mismatch after the exact admission
fence clears the League-owned alias and restores only baseline display tokens
before rolling back the new reservation when that cleanup is provable. If an
interleaved thread or terminal-generation change prevents exact external
restoration, League leaves the new reservation, lease, agent baseline,
publication attempt, and history intact as a recoverable cleanup obligation.
A later create proceeds only if the original exact presentation returns;
otherwise the retained reservation requires the dedicated cleanup lifecycle.

Installed 0.2.36 revealed that Herdr may publish the route alias and its route
tokens while retaining the provider's original command title and identity
tokens. That state is not an arbitrary prebound pane: League may pass the
initial inspection only when the existing reserved assignment, v2 baseline,
and immutable publication attempt all match the alias, agent, thread, terminal
generation, worktree, provider source/title/sidebar/thread envelope, and a
non-regressing global observation. After this read-only proof, League records
one immutable runtime binding before any additional Herdr effect. A future
runtime mismatch refuses. The second pre-effect read must keep the exact first
observation sequence, although a global advance that occurred before retry
remains allowed relative to the older publication attempt.

An arbitrary route, missing baseline/publication, changed provider
title/source/tokens, different assignment, endpoint drift, or an interleaved
sequence refuses without a rename, metadata report, prompt, layout, process, or
Squad action. The existing alias and reservation remain paired as the truthful
cleanup obligation. When a race occurs only after admission, canonical rollback
still waits for proven external alias cleanup; an exact unchanged provider
baseline needs no metadata rewrite after the alias is cleared.

One installed pre-baseline generation retained exactly
`scope_kind=squad` and a historical Squad `scope_id` instead of empty metadata.
That frozen profile is accepted only when those are the only keys, both values
exactly match the sole prior rolled-back assignment, and the verified current
thread equals the retired agent ID. The complete version-2 agent, assignment,
rollback-event, available-callsign, and no-runtime/no-Squad/no-offer/no-lease
fences remain unchanged. Re-reservation atomically stores the clean v2
presentation baseline and normalizes durable metadata to `shotcaller` plus the
exact agent/thread ID before any Herdr mutation. Extra keys, scope or subject
tampering, another thread, incomplete history, an active assignment, or any
owned resource refuses without publication. Exact retry is receipt-identical;
finalization failure restores the captured presentation, rolls back only the
new attempt, and retains the original historical assignment and event.

Installed Herdr 0.2.32 exposed an additional identity-shape distinction. An
unbound Codex pane can have a provider-generated callsign/sidebar/thread/title
such as an owner prompt while having no routing binding and no
`metadata_source` field. League now treats those values only as presentation:
it infers the provider source solely from a complete, self-consistent Codex
session/thread/identity-title envelope and normalizes Herdr's terminal
` | codex` suffix. A real bind still requires a consistent top-level `name`,
`routing_name`, or `routing_alias`; conflicting fields, a present invalid
source, partial tokens, or endpoint identity drift refuse before mutation.

Recovery refuses before any Herdr mutation when assignment history is
ambiguous, the thread generation differs, the agent is not the exact bootstrap
residue, the callsign is not available, or any runtime, lease, Squad,
registration offer, or active assignment remains. The reservation and agent
version updates share one SQLite transaction. A finalization failure rolls back
only the new attempt and preserves the original rolled-back assignment and
events. Successful exact retry returns the same creation receipt without a
second rename, metadata write, prompt, layout action, or process start.

## Regression boundary

Focused fake-Herdr coverage schedules a prompt-derived write after prompt
acceptance and after the early League metadata seed. The fake exposes display
source, source sequence, and endpoint state-change sequence. Coverage proves
the assigned title survives the settling window, the stored receipt matches the
final observation, exact retry sends no prompt, same-owner drift gets at most
one restoration, newer user metadata is not overwritten, and the five owner
examples derive deterministic two-word labels. The fixtures use temporary
repositories and synthetic identities only. Shotcaller coverage additionally
proves delayed owner-prompt settling, prompt-free exact retry, one owned
restoration, user-title refusal, canonical rollback, no Squad, and zero
layout/process creation.

Legacy coverage mirrors the observed pre-fix shape with synthetic identities:
an exact active route plus prompt-derived display and no modern receipt. It
proves one compare-and-set repair, receipt-identical retry, unrelated-token
preservation, modern-receipt refusal, ambiguous-runtime/route refusal, and
user races both before the write and in the final read-to-write window. The
last-window fixture models Herdr's real per-source sequence rule and proves the
League overlay is cleared while the newer user title/source and unrelated
tokens survive.
