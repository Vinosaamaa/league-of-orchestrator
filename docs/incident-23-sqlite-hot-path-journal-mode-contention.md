# Incident Review: SQLite hot-path journal-mode contention

**Issue:** [#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23)  
**Date:** 2026-08-29  
**Severity:** P0  
**Status:** Mitigated in candidate code; installed owner-visible verification pending  
**Scope:** Local League prompt intake, Stop handling, and foreground supervision

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

The candidate correction makes journal mode maintenance-only. Migration may
establish a mode under exclusive authority. Every normal supervisor, prompt,
Stop, transition, delivery, and reporting connection now reads and validates
the established mode without changing it. No timeout was increased.

This report does **not** declare the incident resolved. Resolution requires the
exact installed long-lived-supervisor gate and a fresh owner-visible
Garen-and-Darius gate after the owner re-enables UserPromptSubmit.

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

## Immediate containment

The owner disabled only the League UserPromptSubmit hook group. League Stop
remained enabled. The active supervisor and canonical database were preserved
for diagnosis. The implementation process did not re-enable hooks, stop the
supervisor, edit SQLite directly, or rewrite canonical history.

## Corrective code

The candidate makes four bounded changes:

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

## Acceptance matrix

| Gate | Required evidence | Candidate status |
| --- | --- | --- |
| Established-mode unit | Long-lived WAL connection plus second hot open; mode remains WAL and both connections read canonical state | Passed |
| Actual supervisor regression | Foreground supervisor subprocess remains open while prompt and Stop subprocesses start together | Passed |
| Prompt durability | Hook returns success; exact prompt body hash and byte count exist once | Passed in synthetic focused test |
| Stop safety | Concurrent Stop returns a normal block, never raw `ERROR: busy` | Passed in synthetic focused test |
| Supervisor wake | The same foreground wait exits for user priority | Passed in synthetic focused test |
| Focused affected suites | Watcher, migration, concurrency, request, acceptance, live-cutover, and public-safety gates | Passed locally; public-safety reruns on committed bytes |
| Exact-head CI | Hosted checks bind to the published PR head | Pending |
| Installed release | Tested merge tree installed with prior release retained for rollback | Pending |
| Installed synthetic gate | Installed supervisor, prompt, and Stop use established WAL without mode change | Pending |
| Owner-visible gate | Fresh Darius supervisor; Garen Stop; Darius prompt after owner-controlled hook re-enable | Pending and required for resolution |

## Rollback

The release contains no database schema or data migration. If installed
acceptance fails:

1. keep UserPromptSubmit disabled;
2. atomically restore the prior installed League release pointer;
3. preserve the canonical WAL database, prompt rows, obligations, outbox, and
   supervisor evidence unchanged;
4. do not retry the rejected prompt or fabricate capture;
5. record the first failing gate and keep issue #23 open.

## Remaining risks

- A supervisor process started before installation continues running its already
  loaded code until the owner replaces it for the acceptance gate.
- Unsupported or externally altered journal modes must remain fail-closed.
- WAL safety still depends on the loaded SQLite runtime meeting the pinned
  minimum version.
- Hook success alone still does not prove canonical prompt capture; acceptance
  must inspect the stable League storage surface.
- Owner-visible acceptance remains outstanding, so this incident is not yet
  resolved.

## Action items

| Priority | Action | Owner | Tracking | Status |
| --- | --- | --- | --- | --- |
| P0 | Make normal connections validate rather than change established journal mode | Issue #23 implementer | Issue #23 | Candidate complete |
| P0 | Add long-lived supervisor plus concurrent prompt/Stop regression | Issue #23 implementer | Issue #23 | Candidate complete |
| P0 | Publish Markdown and self-contained HTML incident artifacts | Issue #23 implementer | Issue #23 | Candidate complete |
| P0 | Run focused tests, public-safety scan, and exact-head CI | Issue #23 implementer | Issue #23 | Local tests passed; CI pending |
| P0 | Install the exact tested release with rollback retained | Release owner | Issue #23 | Pending |
| P0 | Run installed synthetic supervisor/prompt/Stop acceptance | Issue #23 implementer | Issue #23 | Pending |
| P0 | Re-enable UserPromptSubmit and run fresh Darius/Garen gate | Owner | Issue #23 | Pending |
| P1 | Keep journal-mode mutation restricted to explicit maintenance commands | League maintainers | Storage contract | Ongoing invariant |

## Resolution criterion

Issue #23 remains open. The incident may be called resolved only after the exact
installed owner-visible gate passes: a freshly started Darius foreground
supervisor remains active, Garen Stop behaves normally, a distinct Darius prompt
returns success, canonical storage contains that prompt exactly once, and no
hot connection attempts to change journal mode.
