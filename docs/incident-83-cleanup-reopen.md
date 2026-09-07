# Issue #83 incident analysis: cleanup without exact continuation

## Summary

The accepted issue-#15 policy required two behaviors that the merged runtime
lifecycle did not yet connect: completed disposable Champion cleanup could not
close its exact owning issue, and a later explicit successor could not reopen
that issue while resuming the exact archived provider thread. Cleanup preserved
task evidence and released runtime/Git/callsign resources, but it had no durable
provider-thread lineage or external issue action. Visible launch always created
a fresh provider thread, while the SQLite runtime identity index prohibited any
later runtime row from reusing a historical thread identity.

This was a missing lifecycle coupling, not an operator or provider failure.

## Impact

- A successfully cleaned Champion could leave its owning issue open.
- Closing an issue manually produced no receipt tied to the teardown operation.
- A later assignment could choose a fresh thread, but could not prove that it
  had resumed the one archived thread intended by the user.
- Callsign reuse was safe, but there was no independent provider-thread lineage
  proving that callsign history and conversation continuity were separate.
- A crash between a hosted issue mutation and its local receipt had no dedicated
  reopen recovery fence.

No live state was corrupted by the prior implementation; the missing operation
failed by absence rather than by silently guessing a continuation.

## Root causes

1. The cleanup action vocabulary stopped at callsign release. It had no exact
   owning-issue adapter or receipt.
2. Runtime identity uniqueness covered all historical rows. That correctly
   prevented accidental reuse, but also prevented a policy-authorized new
   incarnation after the predecessor was closed.
3. The assignment launcher only implemented fresh Codex startup and its
   fresh-thread handshake. It had no explicit archived-thread argument or
   exact post-start equality check.
4. Task, issue, provider thread, endpoint/runtime, worktree, and callsign facts
   were durable individually, but no immutable archive linked all six before
   cleanup released them.
5. There was no exclusive continuation claim. Two successors could therefore
   have raced if resume had been added only at the launcher layer.
6. Cleanup planning and production execution encoded task-state/disposition
   compatibility separately. Planning could persist an owner-cancelled
   `ready_to_land` operation at fence zero, while execution's narrower table
   refused that immutable operation before claiming it.

## Corrective design

Schema v16 adds a permanent provider-thread lineage, immutable per-incarnation
archives, runtime incarnations, and fenced continuation operations. Historical
runtime rows remain; only live rows are unique by harness and provider session.
Every runtime carrying an archived thread must be linked to the same lineage,
and all predecessors must be closed before a successor is authorized.

Cleanup planning now accepts an opt-in continuation archive only for a completed
published Champion with complete acceptance and cleanup proof. The archive and
ordered cleanup actions commit in one transaction before any external effect.
The exact owning issue closes last. The archive is not claimable until both the
close-action receipt and final teardown receipt exist.

Continuation requires an explicit operation with one archive, successor
assignment/task/agent, exact repository/issue/branch/worktree binding, current
instruction digest, optional reconciliation digest, and a bounded concrete
benefit. A partial unique index and expected archive version make the claim
exclusive. The issue reopen uses an executor lease plus monotonic version/fence;
an unknown external outcome is recovered by re-inspecting the exact issue.

Assignment remains the normal callsign allocator. For the currently supported
Codex/Herdr path, launch calls exact resume with the archived thread UUID and
refuses unless the new endpoint publishes that same UUID. Activation creates a
new runtime incarnation and never restores the old endpoint, worktree, branch,
agent identity, runtime identity, or callsign reservation.

Planning and execution now consume one exact task-state/disposition matrix.
`ready_to_land` permits `completed`, `rejected`, or `cancelled`; the latter two
represent an explicit owner rejection/cancellation after implementation became
landable. Other historical terminal combinations remain unchanged. Planning
checks the matrix inside its transaction before creating or advancing a cleanup
obligation revision, and execution checks the same matrix before claiming the
operation fence. This makes incompatible new plans fail early while allowing a
compatible fence-zero plan persisted by an older release to recover unchanged.

## Fail-closed matrix

| Observation | Result |
| --- | --- |
| Acceptance or cleanup proof incomplete | Cleanup plan refused; issue remains open |
| Task state and requested disposition are incompatible | Plan refused before a cleanup revision is claimed |
| Existing fence-zero plan is `ready_to_land + cancelled` | Execution proceeds under the shared owner-decision rule |
| Cleanup action fails before issue close | Operation remains retryable; issue remains open |
| Exact issue already closed | Close records `already_applied`; no duplicate mutation |
| Provider lacks durable exact resume or safe rebinding | Continuation refused |
| Archived context is unhealthy | Continuation refused |
| Instructions changed without reconciliation | Continuation refused |
| New worktree binding is stale, default-branch, or already owned | Continuation refused |
| Thread identity appears in an unlinked or live runtime | Continuation refused |
| Another continuation owns the archive | Version/unique claim refused |
| Reopen succeeded but receipt write was interrupted | Expired lease retry observes open and records recovery |
| Resumed endpoint reports another thread | Endpoint is cleaned and activation is refused |

## Rejected alternatives

- Reusing the prior callsign as proof of continuity was rejected because
  callsign allocation and provider-thread history are independent lifecycles.
- Reusing the old worktree/runtime record was rejected because cleanup proof
  must remain truthful and immutable.
- Reopening by repository search or issue title was rejected because only the
  archived repository and issue number are authoritative.
- Treating every new task on the issue as a continuation was rejected; fresh is
  the default and resume requires a concrete benefit plus an exclusive claim.
- Retrying hosted mutations blindly was rejected in favor of observation-based
  idempotency and immutable receipts.
- Adding a JSON sidecar or provider-specific canonical store was rejected; all
  lifecycle writes remain behind the stable SQLite-backed League commands.

## Verification and remaining boundary

Focused synthetic coverage exercises an earlier cleanup failure, issue-close
failure/retry, already-closed reconciliation, exact reopen/resume with a new
callsign and runtime incarnation, successor cleanup, unsupported resume,
incomplete gates, stale binding, instruction/thread ambiguity, exclusive claims,
partial external failure recovery, exact state/disposition compatibility,
plan-time mismatch refusal, and fence-zero post-upgrade cleanup recovery. The
existing migration, cleanup, assignment, and visible-launch tests cover the
neighboring contracts and schema rollback.

Only Codex on Herdr has an operational exact-resume launcher in this slice.
Other providers remain fail-closed until their own exact durable resume and safe
worktree-rebind drivers are implemented and accepted.
