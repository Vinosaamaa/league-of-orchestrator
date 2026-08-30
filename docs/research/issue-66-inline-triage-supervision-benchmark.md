# Issue #66 inline semantic triage and persistent supervision

**Date:** 2026-08-30  
**Issue:** [#66](https://github.com/Vinosaamaa/league-of-orchestrator/issues/66)  
**Implementation measured:** `01964ae094ae6d34bfe6544ff589e95986d55ae7`  
**Implementation tree:** `48a9e13efc33416a51735d2416c20296eba6e71d`  
**Source version:** League 0.2.24  
**Installed version observed read-only:** League 0.2.27  
**Status:** source candidate only; open, unmerged, uninstalled, and not live

## Decision

Keep SQLite and keep model-authored semantic triage. Remove the second semantic
model invocation from the owner-response critical path.

The active Shotcaller already has to understand the owner's prompt to answer or
route it. That same model turn must author an ordered semantic sideband for the
already-open `league request turn` process. League then performs only bounded
validation, exact-event idempotency, candidate-version checks, deterministic
request IDs, and atomic SQLite commits. A separate classifier is allowed only
for orphaned or backlog prompts and must execute asynchronously after prompt
capture returns.

This changes orchestration around SQLite rather than replacing SQLite. The
measured local commit phase remains a few milliseconds; process startup and the
separate diagnostic model call are much larger costs.

## Owner-source findings and boundaries

Two live failures motivated this source slice:

1. No persistent League supervisor service was active. A benchmark process was
   not production supervision.
2. Genuine prompts reached the active owner turn but its canonical final
   boundary still reported zero untriaged prompts. The installed 0.2.27 watcher
   command inventory also lacks the new `service-run`, `service-status`, and
   `service-stop` surfaces.

The installed hook/runtime failure was reproduced as a bounded
`state_root_unavailable` refusal in the earlier read-only preflight. This work
does not change that installed root, hooks, service manager, runtime, database,
or stable command pointer. Source tests are not installed proof.

## Source architecture

```text
Codex owner turn
  |
  | UserPromptSubmit: exact bytes + exact event identity
  v
persistent local supervisor (outside model turns)
  |-- one renewable/fenced watcher lease
  |-- event-driven Unix socket; no steady SQLite snapshot polling
  |-- user prompt priority over Champion events
  |-- asynchronous orphan/backlog recovery adapter
  v
SQLite canonical prompt intake
  |
  v
one `league request turn` process
  |-- exact prompts + bounded same-owner candidate shortlist
  |<-- active Shotcaller authors ordered semantic JSON sideband
  |-- validate + exact dedup/link + atomic begin commit
  |<-- answer/result actions from the same active turn
  `-- atomic final commit + complete obligation boundary
```

The source service boundary is one service-manager-owned
`agent-watcher --shotcaller <callsign> service-run` process per canonical state
root. It owns one same-user Unix socket, one root lock, and one renewable,
monotonically fenced watcher registration. Hooks are bounded socket clients;
they do not start a foreground supervisor or another model. The repository
contains an inert launchd template with placeholders. Rendering, installing,
loading, or starting it requires a separate exact-source install/cutover
authority and rollback receipt.

The supervisor's semantic recovery port is explicitly injected. It schedules
only quarantined prompts or prompts whose owner has no verified live runtime,
and returns from the hook before recovery finishes. No production recovery
model is selected by this source slice.

Calm mode is the machine policy value `calm` and has two variants. With
supervision on (`supervising`), exact prompt capture and attention-worthy
Champion transitions signal the renewable, fenced Unix socket; the Shotcaller
wait is outside model inference. With supervision off (`paused`), Ashe ends the
model turn, but the non-model monitor, watcher lease, socket, and global hooks
remain active. The same Calm filter applies in both variants: routine events
stay silent. Supervision-on attention uses the watcher socket; supervision-off
attention uses the verified exact-once direct recipient path to start or wake
Ashe. Stop ignores delegated in-flight work only while supervision is off, but
still blocks once for owner-actionable obligations. Resume returns one bounded
page of silent events from the saved cursor. Real owner prompts retain priority.

Normal transition delivery is immediate and event-driven. Runtime exit without
a canonical transition starts one configurable 60-second grace; recovery
cancels it, while expiry performs one CAS-safe reconciliation and may create one
unreachable attention event. A 300-second audit recovers only a lost notification
or service restart and wakes only for a material unresolved attention condition.
The monitor renews its lease silently every 20 seconds, the lease expires after
60 seconds, and launchd's restart throttle is five seconds. The diagnostic
`--poll-seconds 1` foreground loop is not this production boundary.

The timer distinction is material. Owner-source installed 0.2.27 has no
always-running watchdog, launch service, or independent OS timer. Its legacy
foreground `supervise` command keeps an in-memory 30-second runtime snapshot and
requires two matching observations (about 60 seconds) before its stall fallback.
Its separate 300-second liveness deadline currently only resets silently and
performs no health operation. Both vanish when `supervise` exits. The candidate
has no normal one-second poll, 30-second snapshot loop, or self-resetting
liveness deadline. The source launchd/socket service in PR #94 is uninstalled.

## Bounded pre-decision candidate inventory

`request turn` now supplies a deterministic shortlist automatically before the
Shotcaller authors decisions. It uses no transcript search and no model call.

| Property | Contract |
| --- | --- |
| Default bound | 12 candidates and 24,576 encoded bytes |
| Candidate fields | `request_id`, summary bounded to 240 characters, state, version, and an exact project/repository routing key only when already canonical |
| Eligibility | Same current owner and nonterminal request state |
| Shortlist order | Exact routing-key overlap, normalized lexical overlap, update recency, then stable request ID |
| Truth fields | Total count, returned count/bytes, truncation, shortlist digest, and complete active-snapshot digest |
| Expansion | Deterministic request-ID pages off the owner-response critical path |

Twelve concise rows keep routine sideband input in the low tens of kilobytes
while still presenting nearby work. The number is a transport bound, not a
semantic claim. A direct answer or other local bookkeeping proceeds when the
shortlist is truncated. Champion, hidden, or other external dispatch first
requires a complete same-owner inventory and an unchanged full snapshot digest;
truncation or change refuses only that dispatch with a retryable error.

Duplicate, follow-up, and deferred items must name one supplied candidate ID
and version. League validates membership and compares the version again during
the atomic link. A changed candidate fails with `version_conflict`. Exact-event
idempotency remains separate: only the same adapter, session, source-event key,
body, and owner collapse automatically.

## Semantic deduplication and reconciliation

SQLite does not perform fuzzy or paraphrase matching. Semantic duplicate and
follow-up choices are authored by the active Shotcaller against the supplied
candidate inventory.

Stop is omission detection only. It returns bounded unresolved request
summaries and makes no semantic state change. If two requests were created for
one intent, the Shotcaller may explicitly run
`league request reconcile-duplicate` to supersede request B with request A.
That schema-18 transition:

- requires the same current owner and Squad plus both expected versions;
- preserves both prompt/source histories and creates no task, work, or result;
- releases B's claim, prevents later dispatch by terminally cancelling only B,
  and records one immutable reconciliation event;
- is exactly idempotent for the same pair and versions; and
- refuses self-links, chains/cycles, cross-owner or cross-Squad links, stale or
  terminal state, and any B with external dispatch, task, or result evidence.

## Benchmark method

### Immutable Luna diagnostic baseline

The completed baseline remains immutable at source `6354271…`. Batch `N` means
`N` captured prompts classified in one model call, not `N` model calls. Its
120-case corpus is intentionally tiny: 20–77 characters per prompt, average
56.53. Classifier payloads were 2,271 characters at batch 1, 2,713 at batch 6,
and 4,363 at batch 25. The schema required one item per captured prompt and did
not test one long prompt containing several intents.

The OFF arm skipped semantic classification only for diagnosis. The ON arm
started a separate Luna xhigh classifier to author the real JSON batch. Both
arms retained comparable capture, one-process framing, and SQLite work. This
was never a supported production mode.

| Temperature | Batch | OFF total p50/p95 ms | ON total p50/p95 ms | ON semantic-model p50/p95 ms | Added ON-OFF p50/p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cold | 1 | 327.179 / 357.773 | 6,910.948 / 8,999.678 | 5,765.588 / 7,902.503 | 6,553.643 / 8,677.418 |
| Cold | 6 | 346.142 / 503.626 | 15,437.667 / 25,176.811 | 14,120.582 / 24,028.439 | 15,099.197 / 24,828.736 |
| Cold | 25 | 352.106 / 365.433 | 48,538.062 / 60,364.875 | 47,320.820 / 59,230.160 | 48,206.202 / 60,004.733 |
| Warm | 1 | 339.574 / 382.022 | 6,932.626 / 7,380.334 | 5,650.034 / 6,111.169 | 6,575.666 / 7,052.698 |
| Warm | 6 | 376.468 / 387.634 | 15,950.141 / 26,329.296 | 14,782.817 / 25,143.384 | 15,618.070 / 25,976.903 |
| Warm | 25 | 360.785 / 476.913 | 52,075.200 / 58,457.118 | 50,521.584 / 56,974.854 | 51,716.591 / 58,111.815 |

The isolated semantic-model wall time above is not added production latency in
the inline design. It measures the obsolete diagnostic second-classifier
architecture. Source inspection and a focused command test prove that
`request turn` consumes model-authored JSON over its existing stdin and does
not start a classifier subprocess. If an installed ordinary request turn ever
starts a second model process, that is a P0 defect.

### Inline prompt-shape matrix

The new corpus crosses short, medium, and long prompts with 1, 3, and 6 ordered
semantic requests. It uses realistic acknowledgements, corrections, follow-ups,
and repeated work rather than padding. Each cell runs three state arms:

- `cold_empty`: no existing request;
- `preseed_exact`: an equivalent canonical request already exists; and
- `preseed_paraphrase`: a differently worded equivalent request exists.

Ten samples per cell ran on implementation commit `01964ae…`; output SHA-256 is
`02fa117dfd86ae637abd9f5d46bdd34eee3a2858be361c00acd6a441e4cd5aa3`.
The harness uses the corpus's gold ordered sideband so it can measure the actual
capture, candidate-link, validation, SQLite, and one-process boundaries with
zero classifier processes. Therefore produced-item and dedup counts prove local
mechanics, not model split or paraphrase accuracy. Provider token counts are
unavailable because no second provider request exists. Active-Shotcaller
semantic time is part of its normal owner-response turn and is not separately
observable in this source-only harness.

All timings below are p50/p95 milliseconds. `First output` is request-turn
startup through intake. `Local commit` is sideband validation, candidate
membership/version checks, semantic linking, request creation, claims, dispatch
classification, and the atomic SQLite begin commit. `Total` includes final
answer commit and process exit.

| Cell | Arm | Chars/bytes | Mentions → ordered items | Collapsed/linked/created | False merges / missed duplicates | First output | Local commit | Total |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| short-1 | cold | 148/148 | 1 → 1 | 0/0/1 | 0/0 | 188.212/249.497 | 2.240/2.377 | 211.767/273.034 |
| short-1 | exact | 148/148 | 1 → 1 | 0/1/0 | 0/0 | 223.171/593.745 | 1.700/3.177 | 250.215/616.987 |
| short-1 | paraphrase | 148/148 | 1 → 1 | 0/1/0 | 0/0 | 206.891/252.218 | 1.543/4.092 | 228.510/274.373 |
| short-3 | cold | 172/172 | 3 → 2 | 1/0/2 | 0/0 | 203.626/371.891 | 2.824/8.909 | 230.996/395.935 |
| short-3 | exact | 172/172 | 3 → 2 | 1/1/1 | 0/0 | 208.071/277.482 | 2.994/9.568 | 235.023/312.804 |
| short-3 | paraphrase | 172/172 | 3 → 2 | 1/1/1 | 0/0 | 238.606/287.922 | 2.417/4.005 | 263.244/310.618 |
| short-6 | cold | 170/170 | 6 → 6 | 0/0/6 | 0/0 | 276.756/357.599 | 4.028/6.967 | 305.416/382.708 |
| short-6 | exact | 170/170 | 6 → 6 | 0/1/5 | 0/0 | 227.856/281.469 | 3.622/4.620 | 263.173/308.252 |
| short-6 | paraphrase | 170/170 | 6 → 6 | 0/1/5 | 0/0 | 199.539/214.962 | 3.651/8.429 | 226.745/248.069 |
| medium-1 | cold | 472/472 | 1 → 1 | 0/0/1 | 0/0 | 204.522/243.246 | 2.441/3.571 | 228.036/267.052 |
| medium-1 | exact | 472/472 | 1 → 1 | 0/1/0 | 0/0 | 165.457/193.312 | 1.620/2.898 | 187.680/217.907 |
| medium-1 | paraphrase | 472/472 | 1 → 1 | 0/1/0 | 0/0 | 161.377/171.098 | 1.532/2.371 | 183.343/192.979 |
| medium-3 | cold | 590/590 | 3 → 3 | 0/0/3 | 0/0 | 205.095/241.568 | 3.024/4.160 | 230.733/265.264 |
| medium-3 | exact | 590/590 | 3 → 3 | 0/1/2 | 0/0 | 207.091/290.659 | 2.573/3.079 | 231.266/315.885 |
| medium-3 | paraphrase | 590/590 | 3 → 3 | 0/1/2 | 0/0 | 261.752/332.640 | 2.770/4.905 | 296.947/365.905 |
| medium-6 | cold | 601/601 | 6 → 6 | 0/0/6 | 0/0 | 209.217/238.603 | 4.094/7.140 | 236.919/266.144 |
| medium-6 | exact | 601/601 | 6 → 6 | 0/1/5 | 0/0 | 183.116/204.191 | 3.652/4.760 | 208.654/234.418 |
| medium-6 | paraphrase | 601/601 | 6 → 6 | 0/1/5 | 0/0 | 186.451/222.965 | 3.086/3.349 | 210.710/248.031 |
| long-1 | cold | 1,200/1,200 | 1 → 1 | 0/0/1 | 0/0 | 264.260/470.838 | 2.818/19.896 | 292.833/497.437 |
| long-1 | exact | 1,200/1,200 | 1 → 1 | 0/1/0 | 0/0 | 250.166/295.424 | 1.958/3.100 | 271.456/320.897 |
| long-1 | paraphrase | 1,200/1,200 | 1 → 1 | 0/1/0 | 0/0 | 169.915/243.026 | 1.344/2.632 | 190.782/267.252 |
| long-3 | cold | 1,193/1,193 | 3 → 2 | 1/0/2 | 0/0 | 162.447/172.955 | 2.395/2.534 | 186.252/196.942 |
| long-3 | exact | 1,193/1,193 | 3 → 2 | 1/1/1 | 0/0 | 168.274/201.515 | 2.214/3.407 | 191.659/225.431 |
| long-3 | paraphrase | 1,193/1,193 | 3 → 2 | 1/1/1 | 0/0 | 181.247/248.160 | 2.173/3.244 | 204.281/270.761 |
| long-6 | cold | 1,558/1,558 | 6 → 6 | 0/0/6 | 0/0 | 168.101/175.285 | 3.268/3.662 | 193.346/201.317 |
| long-6 | exact | 1,558/1,558 | 6 → 6 | 0/1/5 | 0/0 | 162.030/197.967 | 2.985/3.834 | 185.886/225.656 |
| long-6 | paraphrase | 1,558/1,558 | 6 → 6 | 0/1/5 | 0/0 | 164.201/182.710 | 3.360/4.155 | 191.558/206.552 |

Across all 27 cells, exact-event capture p50 ranged 0.312–0.754 ms,
sideband serialization p50 0.068–0.130 ms, local validate/dedup/commit p50
1.344–4.094 ms, first output p50 161.377–276.756 ms, and whole-turn p50
183.343–305.416 ms. Whole-turn p95 ranged 192.979–616.987 ms. The local
SQLite boundary is not the dominant source of latency.

## What is proved and what remains

### Source-proved

- One request-turn process consumes active-Shotcaller sideband JSON and starts
  zero classifier processes.
- More than 12 candidates never block a direct answer.
- Incomplete inventory refuses only external dispatch, and off-path deterministic
  paging is available.
- Candidate membership and version are checked at semantic link time; a changed
  full candidate snapshot fences external dispatch.
- Exact source-event retry is idempotent; distinct same-turn prompt bodies are
  distinct captures.
- Persistent service startup, renewable/fenced ownership, same-user event
  socket, exact prompt broker, Stop feedback suppression, same-turn rearm,
  Champion wake, asynchronous recovery, stale-socket recovery, and bounded stop
  pass with temporary roots and injected fakes.
- Calm Detached source acceptance proves bounded prompt wake, exactly-once
  attention-transition wake through the watcher while supervision is on and
  through verified direct delivery while it is off, complete routine/attention
  classification, owner-prompt priority, stale-fence refusal, recovery of one
  deliberately omitted notification, 60/300/20/60/5 timer defaults, grace
  cancellation, bounded silent replay, both Stop variants, restart/lease
  recovery, and one canonical runtime-reconciliation event after two exact
  observations and the configured grace.
- Stop alone changes no request semantics; explicit reconciliation closes only
  B, preserves provenance, is idempotent, and removes B from unfinished work.
- Schema 18 migration, backup, rollback, foreign keys, integrity, command
  schemas, deterministic export, and import/export parity pass focused tests.

### Still required under #66 and #23

- Independent source review, hosted exact-head CI, merge authority, and a
  separately authorized exact-source install with verified backup and rollback.
- Render/install/load the service definition and prove `service-status` live at
  the owner source. No service is live from this work.
- Installed exact-once prompt capture, correct Shotcaller binding, a genuine
  same-turn steer, semantic accounting, Stop self-feedback suppression, and
  prompt/Champion wake p50/p95.
- Installed vanilla/retained-legacy/current/candidate cold/warm comparison and
  the full prompt-to-visible-Champion-to-cleanup #23 E2E.
- A bounded Sol xhigh probe of the asynchronous orphan/backlog classifier only.
  It must not block owner responses or alter normal model routing.
- A real active-Shotcaller prompt-shape quality run. The gold-sideband source
  matrix does not prove that a model splits or paraphrase-links every cell.

Normal triage-on behavior remains the release contract. Diagnostic triage-off
and separate synchronous classifiers are not production modes.

## Primary source traceability

| Material claim | Owning source |
| --- | --- |
| Inline turn, candidate digest, candidate version, dispatch fence | [`src/league/cli.py`](../../src/league/cli.py), [`src/league/sqlite_request_ops.py`](../../src/league/sqlite_request_ops.py) |
| Persistent process, socket, lease, wake, recovery boundary | [`src/league/persistent_supervisor.py`](../../src/league/persistent_supervisor.py), [`src/league/sqlite_watcher_ops.py`](../../src/league/sqlite_watcher_ops.py) |
| Source-only service-manager boundary | [`config/league-supervisor.launchd.plist.in`](../../config/league-supervisor.launchd.plist.in) |
| Duplicate reconciliation schema and transition | [`src/league/sqlite_request_reconciliation_schema.py`](../../src/league/sqlite_request_reconciliation_schema.py), [`schema/league-request-reconciliation.schema.json`](../../schema/league-request-reconciliation.schema.json) |
| Prompt-shape corpus and measurement boundaries | [`tests/fixtures/semantic_prompt_shape_matrix.v1.json`](../../tests/fixtures/semantic_prompt_shape_matrix.v1.json), [`scripts/benchmark_inline_triage_prompt_shapes.py`](../../scripts/benchmark_inline_triage_prompt_shapes.py) |
| Focused source acceptance | [`tests/test_request_turn_batch.py`](../../tests/test_request_turn_batch.py), [`tests/test_persistent_supervisor.py`](../../tests/test_persistent_supervisor.py), [`tests/test_calm_supervision.py`](../../tests/test_calm_supervision.py), [`tests/test_request_reconciliation.py`](../../tests/test_request_reconciliation.py), [`tests/test_inline_triage_prompt_shapes.py`](../../tests/test_inline_triage_prompt_shapes.py) |
| SQLite WAL, transaction, and migration behavior | [SQLite WAL documentation](https://sqlite.org/wal.html), [SQLite transaction documentation](https://sqlite.org/lang_transaction.html), [SQLite backup API](https://sqlite.org/backup.html) |
| macOS persistent-service contract | [Apple Daemons and Services Programming Guide](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html) |
