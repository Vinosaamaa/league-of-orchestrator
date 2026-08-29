# Isolated acceptance and reversible cutover foundation

Issue [#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23)
owns this repository-local harness. It creates one disposable League home only
beneath an explicit existing temporary root. It does not discover or use a home
directory, global Roster, installed watcher, callsign pool, hook file, browser
profile, live delivery endpoint, or canonical writer.

## Post-cutover Stop and supervision compatibility

After the writer pointer selects SQLite, the stable `agent-watcher` dispatcher
resolves both an explicit Shotcaller and the hook payload session identity from
canonical agent instances. Stop obligations include active imported Champions
through their preserved Shotcaller ownership even when legacy task rows have no
canonical coordinator fields. An ordinary prompt rearms the single Stop block;
it does not permit an unresolved Shotcaller turn to end.

`agent-watcher --shotcaller <Callsign> supervise` is a SQLite reader/waiter in
this mode. It wakes on one observed Champion state change, obligation-count
change, or newer user-message generation. Legacy mutation commands remain
fenced after cutover. Focused acceptance exercises the installed-shape symlink
dispatcher, explicit and session-ID Stop paths, one supervision wake, user
priority, and final allow only after the synthetic Champion is settled.

Codex `UserPromptSubmit` and Cursor `beforeSubmitPrompt` hooks capture the exact
local prompt once under the verified Shotcaller runtime. Adapter/session/source
identity determines the prompt ID; the stored body hash and UTF-8 byte count
bind the retained bytes. The same transaction rearms supervision. The model
then uses `league request triage` to commit its semantic split into ordered
items and uses the existing claim, dispatch, route, explicit-state, answer, and
unresolved commands. An untriaged captured prompt is itself an unresolved Stop
obligation; hooks never infer a split or mine a transcript.

Champion transitions create one recipient outbox row in the same transaction.
An active SQLite supervisor owns delivery; without one, the dispatcher uses one
verified Herdr endpoint. Receipt uniqueness makes a duplicate retry inert, and
an unavailable endpoint leaves the row pending.

Prompt submission is availability-critical. When a valid hook payload has no
exact verified runtime identity, the hook stores its complete bytes once in the
canonical quarantine with a `runtime_unverified` obligation and returns success
so the local prompt proceeds. `league request bind-prompt` later requires one
exact actor/runtime/session match, promotes the same prompt without changing its
identity or bytes, and wakes the bound Shotcaller for model-authored triage.

Run the complete foundation with one command after creating task-owned sentinel
fixtures outside the requested namespace:

```sh
./bin/league acceptance run \
  --temporary-root /absolute/task-owned/temporary-root \
  --namespace issue-23-review \
  --sentinel-path /absolute/caller-specified/live-byte-sentinel \
  --config-sentinel /absolute/caller-specified/config-sentinel.json \
  --process-sentinel /absolute/caller-specified/process-sentinel.json
```

The process sentinel is synthetic and has this exact outer shape:

```json
{"processes":[],"schema":"league.synthetic-process-sentinel.v1"}
```

## Complete no-apply pre-cutover gate

After the legacy sources and every possible live destination have been listed
in a strict `league.pre-cutover-plan.v1` file, this one command runs the complete
repository-local gate:

```sh
./bin/league acceptance preflight \
  --temporary-root /absolute/task-owned/temporary-root \
  --namespace issue-23-precutover \
  --plan /absolute/task-owned/precutover-plan.json \
  --sentinel-path /absolute/caller-specified/live-byte-sentinel \
  --config-sentinel /absolute/caller-specified/config-sentinel.json \
  --process-sentinel /absolute/caller-specified/process-sentinel.json
```

The plan never authorizes a write. It binds each manifest-relative legacy file
to one explicit absolute regular-file source, inventories bounded current
targets (including absent proposed destinations), and names the future backup,
release, League launcher, watcher launcher, SQLite state, writer pointer,
archive, and hook targets. Every proposed destination must have one exact
current-target precondition. The command refuses a temporary root that overlaps
any planned target and never scans a home directory.

The live-target `unchanged` result is explicitly scoped to exact before/after
snapshot parity plus zero preflight writes. It does not claim continuous
external stability; a separately authorized cutover must quiesce and lock live
writers before relying on those preconditions.

### Issue 23 legacy initialization reconciliation

Ordinary importer parity remains fail-closed. If a Shotcaller status snapshot
and its sole initialization transition disagree, `snapshot_event_mismatch`
still blocks the preflight unless the plan contains one
`league.legacy-roster-reconciliation.v1` object. That object must bind one exact
manifest artifact ID and status/updates pair, both current source SHA-256
hashes, a bounded reason, and either the authoritative status snapshot, the
authoritative latest transition, or one exact normalized
`status`/`at`/`update` triple.

For the bounded case where more than one independent initialization pair needs
the same treatment, `legacy.reconciliations` accepts an ordered list of 1–16 of
those exact objects. The singular `legacy.reconciliation` form remains
supported, and a plan may use only one form. Artifact IDs and both artifact
paths must be unique across the list; duplicates or any path overlap refuse the
plan before a snapshot is created. Each list item retains its own hashes,
resolution, and reason, and is applied in declared order.

The exception is initialization-only and snapshot-only. The gate refuses
missing, stale, duplicate, broad, ambiguous, non-Shotcaller, already-matching,
or multi-transition authorizations before creating the temporary SQLite state.
It rewrites only the copied pair beneath the explicit temporary root, rechecks
the original source hashes after import, and never edits the declared legacy
files. A successful normalization creates one owner-only, create-once
`legacy-reconciliation-receipt.json` containing the artifact pair, original
hashes, normalized hash, reason, authoritative triple, and a
`temporary_snapshot_only` result. The same sanitized inputs yield the same
receipt and pre-cutover operation history in independent attempts.
An ordered-list success instead creates one owner-only, create-once
`legacy-reconciliation-receipts.json` envelope whose ordered entries contain
the same complete per-pair proofs. No singular or list receipt is emitted when
any authorization or a later preflight stage refuses.

### Issue 23 archived watcher-cursor classification

An archived or otherwise non-active Roster referenced by a watcher cursor is
not an active agent and must not be imported as one. The default remains
`unknown_consumer`. A migration manifest may classify a cursor only when all
of these checks pass:

- **Binding:** exact watcher source hash, cursor source path, byte offset, and
  one non-overlapping retained status/history artifact pair with both current
  SHA-256 hashes;
- **Roster identity:** one inactive Champion or worker whose callsign, record
  path, and active Shotcaller owner agree exactly;
- **History:** a matching status/latest-transition triple, an exact cursor
  line boundary, every cursor-prefix digest already seen, and no retained event
  pending or ambiguously claimed; post-cursor archive events must not appear in
  watcher seen, delivery, pending, or current-event state;
- **Unbound receipts:** a separate exact watcher-hash-bound enumeration for
  legacy receipt IDs with no recoverable event payload; and
- **Effect:** preserve the source pair in the backup inventory and store only
  restricted classification metadata—never create an agent, event, cursor,
  seen row, or delivery from retired evidence.

Unbound receipt evidence is retained only as counts and a classification
digest, never reconstructed as delivery history. Missing, stale, duplicate,
overlapping, active, foreign-owner, unseen, pending, current-last-event, broad,
or ambiguous classifications fail closed.
Two watcher scopes may cite the same retired source. The same source and offset
must bind the same artifact pair and hashes; distinct offsets may bind distinct
archived incarnations. Divergent evidence for one source/offset refuses.

### Issue 23 pending-launch field aliases

Pending-launch import normalizes only these bounded source fields:

- `created_at` aliases canonical `started_at`, which is required and validated;
- `resume_thread_id` aliases canonical `resume_thread`; a non-null value must
  be an exact UUID, while explicit null means no resume identity;
- `observed_runtime_generation` aliases canonical `runtime_generation`; and
- legacy `task` text becomes the task summary while `task_id` remains the sole
  task identity.

If both forms of an alias are present, their strings must match exactly or
import refuses.

Alias normalization changes only the temporary import plan. The original
pending-launch bytes and SHA-256 remain in artifact evidence. The importer does
not synthesize a timestamp, task identity, resume thread, phase, or launch
outcome, and it continues to reject unknown fields and unsupported phases.

The receipt `league.pre-cutover-receipt.v1` adds four grouped proofs.

### Migration and rollback proof

- a consistent explicit-binding copy of the caller's legacy state, strict
  dry-run, isolated import, exact legacy-field and row-count parity, source
  recheck, verified SQLite backup, and restricted rollback export;
- optional exact-hash Shotcaller initialization reconciliation, singular or as
  one bounded duplicate-free ordered list, with immutable snapshot-only
  receipts; missing or partial authorization leaves parity fail-closed;
- sandbox-only backup and restore of every current target, including exact
  absent-target rollback instructions;

### Staged-install proof

- a staged-inactive League and compatibility-watcher bundle with exact
  source/release/staged bytes, version and manifest parity, executable
  permissions, launcher/help/schema/hook checks, and pointer rollback.

### Lifecycle proof

- one fully integrated synthetic request, assignment, transition delivery,
  Stop block/allow, exact fake-resource teardown, answer, and safe-finish gate;
- Codex/Herdr, Pi/Herdr, and attached Codex/tmux contract canaries using only
  deterministic doubles. Cursor and genuine Herdr/tmux execution remain
  explicitly unverified; these receipts are not real-runtime claims.

### Cutover-model proof

- the crash-restart writer-pointer matrix, exact fake canary cleanup, live-path
  and caller sentinel parity, and source/staged/current-installed manifest
  checks;
- one deterministic `league.cutover-mutation-manifest.v1` containing every
  proposed backup, inactive install/import, launcher/hook/pointer switch,
  writer activation, live smoke, intake reopen, and exact rollback target. Every
  operation has `applied:false`, and the operation receipt stops at
  `awaiting_authority`.

## Normative supervision compatibility policy

Supervision evidence keeps normal transitions and user prompts on an immediate
event-driven registered-listener path. The isolated benchmark records wake
latency, silent idle CPU/RSS observation, runtime snapshot cost at 1/8/32
Champions, and the simulated missed-wake bound. It prints no periodic unchanged
message and creates no daemon or transcript poller. The proposed installed
compatibility defaults remain exactly 30 seconds and two identical mismatch
observations (earliest simulated fallback: 60 seconds); there is no separate
15-second policy. The mutation manifest records the exact default command and
the two configurable arguments. A different cadence requires measured evidence
and explicit cutover authority.

## Real disposable cleanup canary

The cleanup gate has one deliberately real runtime command. It fetches only the
two required report commits into a new repository beneath the explicit
temporary root, creates an isolated worktree at the exact tested head,
opens one no-focus Herdr pane, launches one Codex Champion routed as
`gpt-5.6-sol high`, and stores the lifecycle in the canary's real SQLite state:

```sh
./bin/league acceptance cleanup-canary \
  --temporary-root /absolute/task-owned/temporary-root \
  --namespace issue-23-cleanup-canary \
  --source-root /absolute/path/to/league-of-orchestrator
```

The command declares that report before the terminal transition, refuses
cleanup while publication is pending, and then records the actual PR #41
tested head and squash-merge receipt. Exact tested/merge tree and report-byte
parity make that isolated branch eligible for cleanup without performing any
hosted mutation. The command also proves that the terminal transition creates
exactly one durable cleanup obligation, event, and outbox row; the same
Shotcaller Stop decision blocks both wait and end while cleanup alone is
pending; and complete stored
authority starts adapter-backed cleanup without a reminder. Cleanup archives
identity/evidence first, exits only the exact Codex session, closes only its
Herdr pane and runtime binding, removes only the clean registered worktree and
eligible local branch, and releases the callsign last. It writes immutable
per-action and final teardown receipts. Fault injection stops after the archive
external effect, then a separate League CLI process reopens the SQLite store and
resumes the same operation idempotently. Only `cleanup_completed` clears Stop.

Readiness uses the normative supervision compatibility policy above. The
command never uses the user's home directory, canonical League state, or a global install. It is a real
Codex/Herdr canary only; it is not evidence for Cursor, Pi, another harness,
tmux, a live repository, or the final cutover.

Issue #40's repository-artifact lifecycle is merged and integrated here. A
green receipt from this command is the final disposable E2E evidence for the
pre-cutover candidate; it remains neither authority nor a command for global
installation, canonical import, hook/watcher mutation, live delivery, or the
live writer switch. Those exact proposed mutations remain separately gated by
the no-apply preflight receipt and explicit cutover authority.

The authorized cutover archives the complete replaced legacy system beneath
the plan's owner-only archive root, keyed by the new SQLite writer generation.
The archive contains exact hashed copies of the old watcher bundle and launcher,
hook configurations, JSON/JSONL records, watcher state, pools, routing, and
resources. Its `RESTORE.md` records the guarded restoration order, and
`league acceptance archive-verify --archive <generation-directory>` verifies
every archived node before any separately authorized rollback. The archive is
never an active writer; stable hooks retain their existing command paths and
switch together by resolving the new SQLite-backed launcher.

The command refuses missing, relative, symbolic-link, or malformed sentinels
and refuses an existing namespace. It accepts at most 16 byte sentinels so a
caller cannot create an unbounded preflight workload. The global
`--state-root` option remains mandatory for storage and domain commands, while
`acceptance run` uses only its separately named temporary root and refuses a
supplied state root. It leaves an owner-only
`acceptance-receipt.json` in the new home. The receipt conforms to
`schema/league-acceptance-receipt.schema.json` and records:

- a durable planned/executing/completed operation history; a failed attempt is
  recorded as resumable `blocked`, and the same command resumes it in a new
  isolated attempt only when the namespace and sentinel fingerprint still
  match;
- deterministic IDs and a fixed fake clock;
- fake harness, terminal, Git/GitHub, process/resource, notification,
  deployment, and hook adapters;
- byte, parsed-config, and synthetic-process sentinel parity;
- transactional schema migration, strict dry-run import, isolated apply, and
  exact fixture-row parity, with source/report/parity digests pinned to a fixed
  synthetic runtime root so receipts are independent of the temporary root;
- source/release/staged byte and version parity, launcher/help/JSON-Schema
  checks, a staged-runtime schema migration and integrity check, synthetic hook
  fixtures, permissions, path-leak refusal, and tested pointer rollback beneath
  a task-owned prefix;
- one sandbox-only generation-bound writer pointer and exclusive cutover lock;
- every pointer-switch fault stage, with a real child process stopped by
  `SIGKILL` and a separate recovery process reconstructing state only from the
  durable journal under the exclusive lock, resumable operation histories,
  coherent old/new recovery, and the invariant that no scenario activates two
  writers;
- exact fake canary registration and identity-bound cleanup.

The original `acceptance run` receipt retains machine-readable `pending`
entries for request, assignment, watcher, Stop, and teardown so its v1 contract
does not retroactively claim broader coverage. The separate pre-cutover receipt
is the integrated contract described above. Neither receipt reports a double or
synthetic hook payload as real-runtime support.

Focused and combined affected verification are:

```sh
make test-acceptance
make test-request-lifecycle
make test-runtime-lifecycle
make test-skill-contracts
make test-handoff-callsigns
make test-reporting-privacy
```

The staged migration assertion and strict receipt schema follow
`CURRENT_SCHEMA_VERSION`; the current contiguous sequence is
`[1,2,3,4,5,6,7,8,9]`. Version 8 adds the provider-neutral routing and
orchestration policy. Version 9 adds repository-owned artifact declarations and
exact merged-publication receipts without changing the acceptance operation or
sentinel contract.

The skill-contract suite uses only synthetic temporary custom roots and fake
capability profiles. The current machine inventory was audited separately in a
read-only command and reduced to the path-free, body-free receipt in
`docs/research/custom-skill-audit.json`. It does not install, synchronize, or
rewrite a skill. Release-to-installed parity and a real runtime remain #23
gates.

The staged release manifest also proves exact source/release/staged parity for
the portable report HTML template, League report skill, and shared guidance
source. The guidance adapter tests stage only beneath disposable explicit roots;
the acceptance harness does not install or cut over global Codex, Cursor, or Pi
instructions.

The generation switch in these harnesses is a model exercised beneath the
disposable namespace. It is not a global cutover command. Canonical cutover,
live import, real hook mutation, watcher replacement, installation, delivery,
and any live-runtime smoke still require the separately authorized cutover.
