# Baseline and planned work

The parent plan is
[#1: Build agent-agnostic League of Orchestrator and disposable Shotcaller lifecycle](https://github.com/Vinosaamaa/league-of-orchestrator/issues/1).

Issue #2 imports the proven watcher/Roster/routing/teardown baseline. The items
below remain planned League work even when the toolkit baseline contains a
related partial behavior:

- [#3: Deliver the exact Champion transition without pending-event cross-wiring](https://github.com/Vinosaamaa/league-of-orchestrator/issues/3) — merged repository-local request-lifecycle slice with #4, #5, and #17
- [#4: Enforce explicit direct, hidden, or Champion dispatch before work](https://github.com/Vinosaamaa/league-of-orchestrator/issues/4) — merged repository-local request-lifecycle slice with #3, #5, and #17
- [#5: Add bounded Stop-hook and Shotcaller wake continuity](https://github.com/Vinosaamaa/league-of-orchestrator/issues/5) — merged repository-local request-lifecycle slice with #3, #4, and #17
- [#6: Choose the minimal durable Roster storage contract](https://github.com/Vinosaamaa/league-of-orchestrator/issues/6) — accepted as ADR 0002; implementation and live migration remain separate
- [#7: Make harness and terminal routing adapter-based](https://github.com/Vinosaamaa/league-of-orchestrator/issues/7) — PR #30 repository-local candidate; installed real-runtime canary remains #23-owned
- [#8: Support guarded disposable Shotcaller handoff](https://github.com/Vinosaamaa/league-of-orchestrator/issues/8) — repository-local v6 candidate adds the stable-Squad CAS switch, bounded immutable Champion snapshot, exact successor acknowledgement, intake fencing, and one owner event/outbox; live acceptance remains #23-gated
- [#9: Add an advisory project catalog and project-grouped Roster](https://github.com/Vinosaamaa/league-of-orchestrator/issues/9) — this candidate adds canonical identities, advisory many-to-many Squad mappings, stable commands/schemas, and the bounded read-only snapshot
- [#10: Declare skill provenance and runtime capabilities](https://github.com/Vinosaamaa/league-of-orchestrator/issues/10) — repository-local schema/config/CLI candidate with sanitized current-root audit and synthetic capability/parity tests; global install remains unchanged
- [#11: Generalize guarded teardown across task classes](https://github.com/Vinosaamaa/league-of-orchestrator/issues/11) — PR #30 repository-local candidate with proof-first recoverable teardown
- [#12: Design a terminal-first Roster UI](https://github.com/Vinosaamaa/league-of-orchestrator/issues/12) — this candidate accepts the design-only Project Ledger direction; an interactive renderer remains a later separately authorized slice
- [#13: Add a persistent shuffled callsign allocation queue](https://github.com/Vinosaamaa/league-of-orchestrator/issues/13) — repository-local v6 candidate implements the accepted persisted shuffle, compatibility scan, exact rollback, tail release, immutable history, and concurrent retry safety; live acceptance remains #23-gated
- [#14: Route model and effort by durable evidence](https://github.com/Vinosaamaa/league-of-orchestrator/issues/14) — merged baseline completed by issue #36's versioned provider policy, evidence gate, expiring override, and one safe-boundary escalation
- [#15: Design Champion continuation, retirement, and automatic rollover routing](https://github.com/Vinosaamaa/league-of-orchestrator/issues/15) — design-only accepted-policy candidate; implementation remains with #8 and #13
- [#17: Add a durable request inbox and unresolved-work reconciliation](https://github.com/Vinosaamaa/league-of-orchestrator/issues/17) — merged repository-local request-lifecycle slice with #3, #4, and #5
- [#18: Audit every JSON/JSONL state dependency before SQLite migration](https://github.com/Vinosaamaa/league-of-orchestrator/issues/18) — completed by the sanitized producer-consumer matrix and cutover inventory
- [#19: Implement SQLite storage and atomic canonical cutover](https://github.com/Vinosaamaa/league-of-orchestrator/issues/19) — repository-local store, audited dry-run import, command facade, and focused tests are merged; live cutover remains separate
- [#21: Document the SQLite orchestration design and decision trail](https://github.com/Vinosaamaa/league-of-orchestrator/issues/21) — completed; its accepted resolutions are canonical for lifecycle work
- [#22: Add evidence-backed League activity and end-of-day reports](https://github.com/Vinosaamaa/league-of-orchestrator/issues/22) — this repository-local candidate adds stable JSON, Markdown/HTML derivation, exact scopes/ranges, immutable show/since specs, completion gates, bounded pagination, and the public-command-only League report skill
- [#23: Build an isolated League acceptance sandbox and reversible cutover harness](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23) — this continuation integrates the repository-local lifecycle, read-only explicit-binding shadow, staged-inactive League/watcher bundle, source/staged/current-install manifests, backup/restore rehearsal, isolated runtime-contract canaries, supervision measurements, and one deterministic no-apply mutation manifest; genuine runtime canaries, the separately authorized global switch, and live smoke remain open
- [#25: Add League-specific outbound privacy enforcement and local-only project metadata](https://github.com/Vinosaamaa/league-of-orchestrator/issues/25) — this repository-local candidate adds structured classifications, one exact-byte remote boundary, incident and no-reply regressions, and staged cross-harness guidance without installation
- [#36: Implement research-backed orchestration and model routing policy](https://github.com/Vinosaamaa/league-of-orchestrator/issues/36) — this repository-local v8 candidate separates owner/execution routing, adds acknowledgement-gated Squad routing and safe registration, recorded hidden scientists, parent-request progress, and the completed #14 model policy; live cutover remains #23-owned
- [#40: Require repository-owned durable artifacts before teardown](https://github.com/Vinosaamaa/league-of-orchestrator/issues/40) — repository-local v9 candidate records expected repository artifacts and exact merged-publication receipts, then refuses cleanup while publication is unresolved; final real acceptance remains #23-owned

The merged #3/#4/#5/#17 slice and PR #30's #7/#11/#14 slice extend only the
repository-local store, command facade, and injected-adapter services. They do
not cross #23's install, live import, hook, watcher-replacement, real canary, or
cutover gates. The filesystem baseline remains live until one coherent release
is separately authorized and verified.
