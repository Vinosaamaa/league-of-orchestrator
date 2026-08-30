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

The store first appends one immutable intent after proving that the canonical
assignment/runtime/route are exact, that only one live runtime matches, and
that no valid modern launch-title receipt exists. The adapter then requires two
fresh identical observations of the expected legacy presentation. Its single
metadata report advances the exact observed presentation source at the next
state-change sequence; a newer provider or user write therefore rejects the
stale compare-and-set. Final success requires two identical observations of
the target sidebar, task label, thread title, terminal title, source, sequence,
and unchanged unrelated token map. Only then does SQLite append the immutable
final receipt.

An exact completed retry re-observes the final endpoint and returns the stored
receipt byte-for-byte without another metadata report. Changed identity,
route, expected presentation, user title, runtime cardinality, target, intent,
or final observation refuses. A failed race retains the intent but never
records a false result.

The operation does not create, replace, stop, or clean a runtime; allocate or
release a callsign; create layout; register a Squad; or mutate task progress.
It patches only display keys owned by this recovery and preserves every
unrelated metadata token.

Shotcaller bootstrap applies a corresponding read boundary: current-pane and
agent-inventory queries may retry malformed JSON up to three attempts each, but
only before any canonical or Herdr mutation. Valid-but-mismatched identity and
persistently malformed output still refuse. Rename and metadata reports are
never retried by this mechanism, preserving provider and placement safety.

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
user races both before the write and in the final read-to-write window.
