# League of Orchestrator

League of Orchestrator is a small, local-first lifecycle layer for coordinating
Summoners, Shotcallers, and Champions across agent harnesses and terminal
backends. It preserves exact identity, durable progress, narrow authority, and
proof-gated cleanup without introducing a service or control plane.

The repository baseline was established by
[issue #2](https://github.com/Vinosaamaa/league-of-orchestrator/issues/2). The
repository-local SQLite store and command facade are tracked by
[issue #19](https://github.com/Vinosaamaa/league-of-orchestrator/issues/19),
under the [project plan in #1](https://github.com/Vinosaamaa/league-of-orchestrator/issues/1).
Nothing here installs files, changes hooks, or connects to live Roster state.

## Baseline in this candidate

- A byte-preserved watcher core proven in `terminal-environment-toolkit`.
- Strict JSON/JSONL Roster validation and matching snapshot/event semantics.
- Durable watcher offsets, event deduplication, scoped delivery, and bounded
  runtime reconciliation.
- Atomic Herdr launch preflight with routing/display identity verification and
  reservation rollback on a failed launch.
- Current thin Herdr and tmux adapters, with their portability limits explicit.
- Semantic model/effort routing with explicit user overrides preserved.
- Fail-closed schema-2 teardown verification, including local-install byte
  parity and later-main squash proof, plus archive boundaries.
- Synthetic examples, authoring schemas, and focused local regression tests.
- One standard-library SQLite implementation behind a `Storage` protocol and
  stable `league` command facade.
- Eighteen contiguous checksummed schema migrations, a loaded-runtime WAL gate,
  verified backups, integrity checks, expected-version writes, and bounded contention.
- A strict manifest importer covering every canonical issue-#18 artifact
  family, with dry-run digest confirmation before apply.
- Deterministic bounded inspection and restricted rollback exports with
  machine-readable schemas.
- Prompt-once intake, complete bounded triage, request claims and states,
  explicit direct/hidden/Champion dispatch, and unresolved reconciliation.
- Recoverable visible-Champion assignment with exact acceptance receipts,
  settled post-context callsign/task title restoration bound to the exact
  metadata source and sequence, deterministic two-word display-task defaults,
  and one owner-authorized sequence-fenced League-overlay reconciliation for
  exact active pre-fix Champions without modern title receipts,
  source-bound transition outbox delivery, unique recipient effects, and fair
  backlog draining.
- One role-aware bounded Shotcaller Stop decision with ordinary-message
  priority and separate request, dispatch, and watcher leases, plus in-place
  Shotcaller bootstrap that waits for a stable source-owned callsign title
  without creating layout/process state or overwriting a newer user title,
  source, or unrelated token. Read-only identity queries retry bounded
  transient malformed output; persistent malformed bytes still refuse before
  callsign reservation, runtime registration, or Squad state. A new exact
  create may rebind one version-2 retired unbound bootstrap residue only when
  its prior assignment is rolled back, its current terminal/thread intent is
  exact, and no runtime, Squad, offer, lease, or active callsign remains. A
  baseline-less legacy residue is eligible only when its metadata is exactly
  empty and two exact current observations prove an unbound, callsign-free
  presentation; League atomically upgrades that row with the observed source,
  title, token, generation, and sequence baseline before publication. One
  frozen older two-key profile is also recognized: `scope_kind=squad` and its
  `scope_id` must exactly equal the sole rolled-back assignment's historical
  Squad scope, while the verified thread must equal the retired agent ID.
  League captures the same v2 baseline and normalizes the durable scope to
  `shotcaller` plus that exact agent/thread ID before publication. Extra keys,
  changed scope/history, or any owned resource refuse. Herdr
  provider-generated presentation tokens are never treated as a routing bind:
  only consistent top-level route fields bind the pane, while a source-less
  provider presentation is accepted only as one complete thread-bound envelope.
  Legacy recovery records an assignment-bound publication attempt before the
  first Herdr rename. An exact reserved retry may resume a route-only partial
  publication despite unrelated global state-sequence advances only when the
  endpoint identity and every provider presentation byte still match that
  attempt. League's title overlay carries explicit owner/source tokens and uses
  Herdr's provider-source authority without borrowing its source-local sequence;
  a later provider or user presentation refuses and is preserved. Rollback
  either proves the exact external restoration before releasing the reservation
  or leaves that reservation as the durable retry obligation.
- One active-turn semantic sideband with a 12-row/24 KiB same-owner candidate
  shortlist, version-fenced duplicate/follow-up links, and external-dispatch
  refusal when the complete candidate inventory is unavailable or changed.
- One source-only persistent event supervisor with renewable/fenced ownership,
  exact prompt and Champion-event wake, asynchronous orphan recovery, and a
  service-manager template that is not installed by this repository work.
- One explicit same-owner duplicate-request reconciliation command; Stop remains
  omission detection and never performs semantic cleanup.
- Opaque capability-based harness/backend bindings, typed task resources, and
  canonical SQLite-backed recoverable teardown with immutable per-action/final
  receipts, exact shared-lease release, and persistent-resource retention.
- Assignment-neutral semantic model/effort routing that preserves explicit
  choices and permits one evidence-triggered safe-boundary escalation.
- A sanitized custom-skill provenance inventory, bounded install-parity audit,
  and provider/model-neutral runtime capability matrix with explicit shared
  inline fallback and specialist refusal.
- A canonical-ID project catalog with exact local/repository identities,
  aliases/codes, and many-to-many advisory Squad suggestions that never move
  work or override explicit routing.
- One bounded read-only project-grouped Roster snapshot with exact evidence
  references, outbound redaction, and an accepted terminal-first design.
- One persisted seeded callsign queue with compatibility-first allocation,
  exact reservation rollback, tail release, and immutable assignment history.
- One guarded disposable Shotcaller rollover for a stable Squad with a bounded
  immutable Champion snapshot, exact successor acknowledgement, and one atomic
  owner/event/outbox switch.
- One switched-rollover-only snapshot refresh that replaces an expired
  descendant snapshot with a new immutable revision after exact canonical CAS
  and two identical complete live Herdr identity observations. Runtime
  generations are derived from the observed terminal and exact thread/session,
  and must also match an existing canonical generation; stale rows are never
  reused. The final observation runs in the consistent deferred transaction
  immediately before pointer CAS without reserving the SQLite writer lock. A
  partially reconciled switched rollover may refresh only when every
  successor-owned descendant has one exact immutable reconciliation receipt;
  the new receipt retains the full original set and exposes proved terminal
  markers so already-transferred rows are not reconciled twice. An imported
  legacy descendant created by an older reconciler may use only that release's
  exact historical receipt profile: both runtime and assignment must have been
  created atomically, the assignment must retain the byte-equivalent acceptance
  receipt, and current task/callsign/runtime/capability/outbox state must pass
  the same checks. Incomplete modern receipts never fall back to this profile.
  During that same switched refresh, an exact predecessor-owned imported row
  whose canonical route and display identity are both null may adopt only the
  unique live Herdr top-level name equal to its normalized callsign. League
  also accepts an exact frozen no-runtime binding when the only current change
  is one verified active/idle runtime matching the same agent and live endpoint.
  League records both binding digests plus the full runtime evidence in the
  agent/runtime/callsign/snapshot CAS receipt and builds the new snapshot from
  the incremented canonical agent version; modern clears, successor rows,
  title-only guesses, overlaps, capability gaps, and identity drift refuse.
- Deterministic bounded activity reports with stable JSON, exact range/timezone
  and scope, immutable show/since specifications, completion gates, indexed
  pagination, and JSON-derived Markdown/portable HTML.
- Structured local-only project/evidence classification and one fail-closed
  final-rendered-payload validator shared by every League remote adapter.
- One provider-neutral v1 routing slice with explicit/continuation/unique-strong
  Squad ownership, exact direct-tiny bounds, recorded hidden scientists,
  acknowledgement-gated transfer, safe Squad registration, parent-request
  progress coalescing, versioned provider routing, and one evidence-triggered
  model escalation.
- One immutable scoped `autonomous_delivery` grant lifecycle (owner alias:
  **YOLO mode**) with exact goal/action receipts, expiry, revocation, configured
  usage limits, bounded repair, backup/export coverage, and Shotcaller-only
  external-action ownership. Exact protected command gates can consume and
  settle that accepted authority without asking the owner again.
- Issue-first visible delegation with duplicate preflight across open and
  closed GitHub issues, durable reuse/reopen/create selection receipts, and an
  exact binding before assignment or tab mutation; durable work kinds cannot
  route direct or hide implementation ownership.
- Issue-coupled Champion cleanup that archives the exact provider thread and
  owning task/repository/issue binding before release, closes the issue through
  the recoverable cleanup executor, and permits an explicit successor to reopen
  that same issue and resume only the uniquely archived healthy thread.

The proven watcher remains one deep module. The new storage slice is a small
modular monolith: one composite `Storage` interface with cohesive administrative,
lifecycle, delivery, and transfer subprotocols; one `SQLiteStorage` facade over
a shared connection/transaction core and focused SQL operation modules; one
import planner; and one CLI. The watcher does not import it yet, so this feature
branch cannot become a second live canonical writer.

## Local development

Requirements: Python 3, Git, and a POSIX shell.

```sh
make test
make test-storage
make test-project-roster
make test-request-lifecycle
make test-turn-benchmark
make test-runtime-lifecycle
make test-routing-policy
make test-skill-contracts
make test-handoff-callsigns
make test-acceptance
make test-reporting-privacy
make test-affected
make test-all
```

`make test` runs the inherited baseline once, `make test-storage` runs the
storage slice once, `make test-project-roster` runs the focused issues #9/#12
contract, `make test-request-lifecycle` runs the grouped lifecycle
suite, `make test-runtime-lifecycle` runs issues #7/#11/#14/#83, and
`make test-turn-benchmark` runs the focused one-process, semantic-ablation, and
inline prompt-shape harness contracts without contacting a model provider, and
`make test-routing-policy` runs issue #36's deterministic owner/execution,
Squad registration, parent-progress, and hidden-scientist contract, while
`make test-skill-contracts` runs issue #10's synthetic provenance, privacy,
duplicate-parity, CLI, and runtime-fallback contract, while
`make test-handoff-callsigns` runs issues #8/#13, and `make test-acceptance`
runs both the isolated issue-#23 foundation and the complete no-apply
pre-cutover command. `make test-affected` composes storage,
acceptance, request lifecycle, runtime/skill/routing lifecycle, handoff/callsign,
reporting/privacy, and public-safety
coverage, and `make test-reporting-privacy` runs issues #22/#25 report, privacy, staged-guide,
metadata, incident, renderer, pagination, and latency contracts.
`make test-all` adds the inherited baseline once. Every
target uses temporary fixtures only. It does not install files, contact GitHub,
mutate global agent state, or operate live Herdr/tmux sessions.

Issue #83 adds three stable continuation commands. `continuation prepare`
verifies a new non-default Git worktree and atomically claims one available
archive; `continuation reopen` performs the fenced, receipt-bearing owning-issue
reopen; and `continuation status` returns the exact operation, archive, and
thread lineage. The claimed assignment is then launched through the ordinary
`assign run` command. A Codex continuation uses `codex resume` with the archived
thread UUID, verifies that the new endpoint published that exact UUID, assigns
a normal current callsign, and records a new runtime incarnation. Unsupported,
unhealthy, reused, live, stale, conflicting, or instruction-drifted candidates
refuse before a successful resume.

The cleanup manifest opts into issue coupling with one
`continuation_archive` object and a final `issue_close` action. It must contain
the exact task, provider thread, runtime, repository, issue, branch, worktree,
instruction/policy digests, completed acceptance, cleanup evidence, and declared
resume capabilities. Planning stores that archive in the same transaction as
the ordered cleanup actions, before any external resource release. The issue is
closed last and the archive becomes available only after the exact close action
and final teardown receipts exist. See [runtime lifecycle](docs/runtime-lifecycle.md)
and the [issue #83 incident analysis](docs/incident-83-cleanup-reopen.md).

The repository now proves a staged-inactive release, exact read-only shadow,
backup/restore rehearsal, and deterministic proposed-mutation manifest through
`league acceptance preflight`; that command writes only beneath its explicit
temporary root and stops at `awaiting_authority`. The repository does not yet
own or perform live installation. The currently installed
watcher and rollback process remain owned by `terminal-environment-toolkit`
until a later migration proves source/installed parity and rollback from this
repository. See [migration](docs/MIGRATION.md) and
[provenance](docs/PROVENANCE.md).

Inspect the stable command inventory without creating state:

```sh
./bin/league --help
./bin/league help inventory
./bin/league --state-root /absolute/isolated/state-root storage --help
```

Every state operation requires an explicit existing absolute state root;
machine-readable help is read-only and needs no root. The database
filename, SQL, pragmas, and transaction details remain internal. The output
contracts are [command](schema/league-command-output.schema.json),
[import report](schema/league-import-report.schema.json), and
[export](schema/league-export.schema.json) JSON Schemas. The advisory surfaces
add [project catalog](schema/league-project-catalog.schema.json) and
[Roster snapshot](schema/league-roster-snapshot.schema.json) contracts.

The grouped request-lifecycle command and transaction map is documented in
[request lifecycle](docs/REQUEST_LIFECYCLE.md). Its implementation is inert
until issue #23 separately proves installation and cutover.
Issue #66's inline-triage, candidate-inventory, persistent-supervision, and
measured source-only boundaries are in the
[issue #66 benchmark report](docs/research/issue-66-inline-triage-supervision-benchmark.md).
The normal Shotcaller path opens one `request turn` process; the same active
model authors its semantic JSON. `agent-watcher service-run` is an external
service-manager surface, never an active-turn command. The inert launchd
template is not an install receipt.

The repository-local supervisor supports `all_material` and Calm (`calm`) wake
policies. Calm commits every transition but suppresses routine Champion
progress. With supervision on, the Shotcaller remains in an event-driven wait
outside model inference and attention arrives through the fenced Unix socket.
`agent-watcher service-pause` turns Calm supervision off: the model turn ends,
the non-model monitor, watcher lease, socket, and global hooks stay active, and
attention uses the verified exact-once direct recipient path. `service-resume`
restores the attached wait and returns one bounded silent-transition
reconciliation. Real owner prompts retain priority in both variants.

Normal delivery is immediate IPC, not polling. Missing runtime truth gets one
configurable 60-second grace before CAS-safe reconciliation. A 300-second
SQLite audit is recovery-only for lost notifications or service restart and
never invokes a model when healthy. The service renews its silent lease every
20 seconds, the lease expires after 60 seconds, and the inert launchd template
uses a five-second restart throttle.

Installed 0.2.28 has no always-running watchdog or OS-owned supervision timer.
Its legacy foreground `supervise` loop keeps a 30-second runtime snapshot and
requires two matching observations (about 60 seconds) before a stall fallback;
its separate 300-second liveness deadline only resets silently and performs no
health operation. Both timers disappear when that foreground command exits.
These legacy timers are not the candidate design. The source launchd/socket
service described here is not installed.
The adapter, resource, cleanup, and routing contracts are documented in
[runtime lifecycle](docs/runtime-lifecycle.md) and remain equally repository-local.
The custom-root provenance and capability boundary is documented in
[skill capabilities](docs/skill-capabilities.md). Its validation, audit, and
matrix commands require explicit config/root/profile inputs and create no state.
The project catalog and bounded Roster are documented in
[project catalog](docs/PROJECT_CATALOG.md); the chosen design-only terminal
direction is [Project Ledger](docs/design/terminal-roster-ui.md).
The report source, commands, completion gates, renderers, and report skill are
documented in [reporting](docs/REPORTING.md). The classification, final-byte
remote boundary, publication metadata gate, and staged League supplement are
in [privacy](docs/PRIVACY.md).

The [privacy contract](docs/PRIVACY.md) records the guide ownership split:
terminal-environment-toolkit alone owns the universal guide, while League may
install only its orchestration supplement. Toolkit issue #45 owns the universal
trigger; League issue #90 changes no toolkit or installed guide.
The owner/operator model, grant state machine, issue-first boundary, migration,
and remaining limits are documented in
[scoped autonomous delivery](docs/design/scoped-autonomous-delivery.md).

## Scoped autonomous delivery

Manual remains the default. A Summoner authorizes one exact goal from a strict
grant document, and the Shotcaller checks status before each irreversible step:

```sh
./bin/league --state-root /absolute/state mode authorize \
  --grant /absolute/grant.json --expected-goal-version 0 \
  --at 2026-01-01T00:00:00Z
./bin/league --state-root /absolute/state mode status \
  --goal-id goal:example --at 2026-01-01T00:00:01Z
./bin/league --state-root /absolute/state mode use \
  --action /absolute/action.json --expected-goal-version 2 \
  --at 2026-01-01T00:00:02Z
```

`mode settle`, `mode transition`, and `mode revoke` finish the checked flow.
Grant and action input schemas are
[authorization](schema/league-autonomous-grant.schema.json) and
[action use](schema/league-autonomous-action.schema.json); status, action, and
protected-gate receipts have separate public schemas. A protected command such
as `assign reconcile-runtime` accepts `--mode-action` with
`--expected-mode-goal-version`; League binds the exact command scope to that
action, runs the command, and durably settles both receipts. The grant must
explicitly include the command's category (`live_reconcile`, `retire`,
`shotcaller_create`, `squad_register`, or `teardown`). Manual authority remains
available, but a caller cannot combine it with mode authority for the same
gate. Stale, revoked, expired, sensitive, excluded, out-of-scope, over-limit,
or safety-bypass actions still refuse before the protected operation.

An autonomous grant never
makes repository implementation direct: `request dispatch` still requires a
visible Champion, and `assign run` verifies the issue against GitHub before any
canonical assignment or terminal mutation.

Before `assign run`, select the issue through the duplicate-preflight command:

```sh
./bin/league --state-root /absolute/state issue select \
  --task-id task:example --task-summary "Implement the exact task" \
  --coordinator-agent-id <shotcaller-agent-id> \
  --repository https://github.com/owner/repository.git \
  --issue-title "Implement the exact issue" --issue-body /absolute/issue.md \
  --at 2026-01-01T00:00:00Z
```

The command searches all open and closed issues by normalized title and the
normalized Objective/Scope section. It reuses an open equivalent, recognizes a
closed recurrence only after the supported `issue_reopen` action is settled and
the owner API reports it open on exact retry, and creates only distinct scope.
Pass its `receipt_digest` to `assign run` as
`--issue-selection-receipt-digest`; an absent, stale, or mismatched receipt
refuses before assignment or terminal mutation.

## Repository-local SQLite implementation; cutover still gated

Issue [#6](https://github.com/Vinosaamaa/league-of-orchestrator/issues/6)
accepts one embedded SQLite canonical store, using the complete dependency audit
from [#18](https://github.com/Vinosaamaa/league-of-orchestrator/issues/18).
Agents use stable `league` commands and never SQL; there is no server, ORM, or
permanent dual canonical store. Issue #19 implements that boundary for explicit
isolated roots. The dry-run importer reports exact artifact and row counts,
ordering, retained files, the complete audit disposition, and a digest that is
required before apply. Unknown consumers, duplicate keys or identities,
malformed/truncated records, snapshot/event skew, and target collisions refuse.
Import reads are bound to validated file descriptors, retained-file and
audit-coverage reports are strict schemas, roster offsets are indexed, and
bounded JSONL export emits rows without retaining a second complete row graph.

See [ADR 0002](docs/adr/0002-sqlite-canonical-store.md), the
[dependency audit](docs/research/json-jsonl-state-dependency-audit.md), and the
[prototype benchmark](docs/research/sqlite-storage-prototype-benchmark.md).

The implementation enables WAL only when the SQLite library loaded by the
executing Python runtime is version **3.51.3 or newer**. SQLite's
[WAL-reset documentation](https://www.sqlite.org/wal.html#the_wal_reset_bug)
identifies the affected range through 3.51.2 and the fix in 3.51.3. An older or
unverifiable loaded runtime selects rollback-journal mode and reports the
refusal; a nearby `sqlite3` executable never substitutes for this check.

Issue [#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23)
owns the isolated acceptance harness, staged installation, read-only live-state
shadow, reversible pointer switch, and separately authorized cutover. Until
those gates pass, the filesystem watcher remains the only live authority.
The repository-local foundation and its one explicit-root command are
documented in [isolated acceptance](docs/ACCEPTANCE.md). It records the later
acceptance-receipt extensions for request, assignment, watcher, Stop, and
teardown as pending. All seven now have repository-local implementations and
focused deterministic tests, but have not been folded into a cutover receipt;
the harness does not claim real Codex, Cursor, Pi, Herdr, or tmux support from
fake adapters.

## Project map

- [Architecture and authority boundaries](docs/ARCHITECTURE.md)
- [Filesystem Roster baseline decision](docs/adr/0001-filesystem-roster-baseline.md)
- [Accepted SQLite canonical-store decision](docs/adr/0002-sqlite-canonical-store.md)
- [JSON/JSONL dependency audit](docs/research/json-jsonl-state-dependency-audit.md)
- [SQLite prototype and benchmark](docs/research/sqlite-storage-prototype-benchmark.md)
- [Reversible migration and install boundary](docs/MIGRATION.md)
- [Guarded rollover and shuffled callsign queue](docs/HANDOFF_CALLSIGNS.md)
- [Isolated acceptance and reversible cutover foundation](docs/ACCEPTANCE.md)
- [Champion launch title incident and ordering
  invariant](docs/incident-85-champion-launch-title.md)
- [Exact source provenance](docs/PROVENANCE.md)
- [Repository-local request lifecycle](docs/REQUEST_LIFECYCLE.md)
- [Repository-local runtime lifecycle](docs/runtime-lifecycle.md)
- [Issue-coupled cleanup and exact-thread reopen incident analysis](docs/incident-83-cleanup-reopen.md)
- [Skill provenance and runtime capability contract](docs/skill-capabilities.md)
- [Advisory project catalog and project-grouped Roster](docs/PROJECT_CATALOG.md)
- [Deterministic activity reports](docs/REPORTING.md)
- [Outbound privacy boundary](docs/PRIVACY.md)
- [Research-backed orchestration and model routing policy](docs/research/orchestration-model-routing-policy-evidence.md)
- [Terminal-first Project Ledger design](docs/design/terminal-roster-ui.md)
- [Scoped autonomous delivery and issue-first delegation](docs/design/scoped-autonomous-delivery.md)
- [Baseline versus planned issues](docs/ROADMAP.md)

## Non-goals for this implementation PR

No interactive Roster UI or controller, adapter cutover, global install, hook mutation, live import,
watcher replacement, daemon, merge, release, deployment, or live teardown is
performed here.
