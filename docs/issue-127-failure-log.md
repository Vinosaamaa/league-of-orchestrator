# Issue #127 failure log

This log records the bounded failures encountered while building the
already-stopped total-retirement path. It contains no machine-local paths,
runtime identifiers, provider transcripts, or live agent data.

| Boundary | Failure | Resolution | Canonical or live mutation before resolution |
| --- | --- | --- | --- |
| Issue selection | `issue_scope_incomplete` because the existing issue body lacked a recognized hard-boundaries heading. | Added only the explicit scope, acceptance, and hard-boundary headings already authorized by the owner, then repeated the same stable selection. | None. |
| First TDD slice | The new public retirement module did not exist. | Added the registry-dispatched service and atomic SQLite operation only after the failing public import/test established the boundary. | Synthetic temporary SQLite only. |
| Stable CLI contract | `runtime retire-stopped-agent` was not a recognized action. | Added the exact-identity command and verified every required option through the public executable. | None. |
| Callsign assertion | The focused test initially assumed a non-existent `available` result key. | Corrected the test to the stable `counts` and ordered `entries` callsign-queue contract; production code was unchanged. | Synthetic temporary SQLite only. |
| Multiplexer capability regression | Existing truthfulness tests did not include the newly implemented Herdr capability. | Added `verify_stopped_agent` to the public multiplexer contract and updated the exact callable-capability expectations. | None. |
| Migration regression | The immutable migration list did not yet include schema 24 and its checksum. | Added the contiguous schema-24 entry and exact checksum expectation. | Synthetic temporary SQLite only. |
| Restart idempotency | The first successful result included `callsign`, but the durable retry receipt did not. | Persisted the callsign inside the immutable receipt so pre- and post-restart results differ only in the `idempotent` flag. | Synthetic temporary SQLite only. |
| Input preflight | An invalid timestamp reached the adapter and transaction boundary. | Added RFC3339-with-offset validation before canonical lookup or adapter inspection. | Synthetic temporary SQLite only; the red fixture was discarded with its temporary directory. |
| Issue-comment publication | The first sanitized comment attempt could not reach the GitHub API. | Repeated the exact same bounded payload through the approved network boundary; one comment was created. | No repository, League, or live-agent mutation. |
| Combined affected gate | Schema 24 changed the deterministic migration-report digest while the acceptance fixture still pinned the schema-23 value. | Updated only the exact derived report digest, then reran the affected gate from its start. | Synthetic temporary acceptance roots only. |
| Pre-cutover schema gate | The pre-cutover and acceptance receipt schemas still required schema version 23. | Advanced the exact version constants and contiguous applied-version list to 24; no runtime or cutover policy changed. | None. |
| Managed test sandbox | The persistent-supervisor acceptance could not bind its synthetic temporary Unix socket and reported `PermissionError`. | Re-ran the unchanged focused test with the required local-socket permission; it passed, proving an execution-sandbox restriction rather than a product failure. | Synthetic temporary state only; no live supervisor or agent endpoint was addressed. |
| Release-head rebase | Current main advanced to League 0.2.50 and both changes appended provenance at the same boundary. | Preserved the reviewed 0.2.50 watcher-release record followed by the schema-24 #127 record; retained all 0.2.50 version assertions while applying the new migration digest and schema constants. | None. |
| Exact-head review | The first PR head proved only agent-inventory absence before opening the settlement transaction; process names were unused, provider aliases were rejected, receipt inputs were unbounded, and two active-ownership queries lacked covering indexes. | Moved exact pane/process proof inside the same bounded immediate transaction as canonical revalidation and settlement; normalized aliases; bounded identity/proof bytes; added covering indexes and bounded lookups. | None; the superseded head was not merged or installed. |
| CLI proof fixture | The synthetic CLI E2E returned an agent-list shape for the newly required pane process inspection. | Updated only the fake Herdr command to return the then-modeled structured absent-pane result; the later live-contract row records its owner-source correction. | Synthetic temporary state only. |
| Live Herdr pane-absence contract | The installed `pane process-info` command reports a closed pane with exit status 1, empty stdout, and a structured `pane_not_found` envelope on stderr; the candidate parsed only stdout and expected `not_found`. | Parse bounded strict JSON from stdout only on success and stderr only on failure, and accept only the installed exit-1 `pane_not_found` contract. Updated both direct runner and CLI E2E fixtures to match the owner-source surface. | Read-only owner probe only; no pane, process, repository, or League state was changed. |
| Strict process envelope | Python's default JSON decoder accepted non-finite constants, and the pane-absence branch accepted an envelope containing both `error` and `result`. | Refuse `NaN`, `Infinity`, and negative `Infinity` through `parse_constant`; require exactly one top-level `result` member on success or `error` member on failure. Adversarial tests cover non-finite constants and both mixed-envelope directions. | Synthetic adapter calls only; no canonical, pane, process, or repository mutation. |

Expected refusal tests also prove `stopped_retirement_endpoint_live`,
`stopped_retirement_identity_ambiguous`,
`stopped_retirement_identity_mismatch`,
`stopped_retirement_provider_mismatch`,
`stopped_retirement_operation_conflict`,
`stopped_retirement_multiplexer_unsupported`, and
`stopped_retirement_work_untransferred`. These are accepted fail-closed outcomes,
not partial failures: every runtime, agent, callsign, Squad membership, and
retained repository byte remains unchanged.
