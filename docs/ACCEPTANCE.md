# Isolated acceptance and reversible cutover foundation

Issue [#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23)
owns this repository-local harness. It creates one disposable League home only
beneath an explicit existing temporary root. It does not discover or use a home
directory, global Roster, installed watcher, callsign pool, hook file, browser
profile, live delivery endpoint, or canonical writer.

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

The receipt `league.pre-cutover-receipt.v1` adds four grouped proofs.

### Migration and rollback proof

- a consistent explicit-binding copy of the caller's legacy state, strict
  dry-run, isolated import, exact legacy-field and row-count parity, source
  recheck, verified SQLite backup, and restricted rollback export;
- optional exact-hash Shotcaller initialization reconciliation with an
  immutable snapshot-only receipt; no authorization leaves parity fail-closed;
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
