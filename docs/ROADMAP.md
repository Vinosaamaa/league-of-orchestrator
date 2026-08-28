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
- [#8: Support guarded disposable Shotcaller handoff](https://github.com/Vinosaamaa/league-of-orchestrator/issues/8)
- [#9: Add an advisory project catalog and project-grouped Roster](https://github.com/Vinosaamaa/league-of-orchestrator/issues/9)
- [#10: Declare skill provenance and runtime capabilities](https://github.com/Vinosaamaa/league-of-orchestrator/issues/10)
- [#11: Generalize guarded teardown across task classes](https://github.com/Vinosaamaa/league-of-orchestrator/issues/11) — PR #30 repository-local candidate with proof-first recoverable teardown
- [#12: Design a terminal-first Roster UI](https://github.com/Vinosaamaa/league-of-orchestrator/issues/12)
- [#13: Add a persistent shuffled callsign allocation queue](https://github.com/Vinosaamaa/league-of-orchestrator/issues/13) — accepted queue policy: release appends to the tail, recency ranks rather than bans, and the sole compatible candidate remains allocatable
- [#14: Route model and effort by durable evidence](https://github.com/Vinosaamaa/league-of-orchestrator/issues/14) — PR #30 repository-local assignment-neutral routing candidate
- [#15: Design Champion continuation, retirement, and automatic rollover routing](https://github.com/Vinosaamaa/league-of-orchestrator/issues/15) — design-only accepted-policy candidate; implementation remains with #8 and #13
- [#17: Add a durable request inbox and unresolved-work reconciliation](https://github.com/Vinosaamaa/league-of-orchestrator/issues/17) — merged repository-local request-lifecycle slice with #3, #4, and #5
- [#18: Audit every JSON/JSONL state dependency before SQLite migration](https://github.com/Vinosaamaa/league-of-orchestrator/issues/18) — completed by the sanitized producer-consumer matrix and cutover inventory
- [#19: Implement SQLite storage and atomic canonical cutover](https://github.com/Vinosaamaa/league-of-orchestrator/issues/19) — repository-local store, audited dry-run import, command facade, and focused tests are merged; live cutover remains separate
- [#21: Document the SQLite orchestration design and decision trail](https://github.com/Vinosaamaa/league-of-orchestrator/issues/21) — completed; its accepted resolutions are canonical for lifecycle work
- [#22: Add evidence-backed League activity and end-of-day reports](https://github.com/Vinosaamaa/league-of-orchestrator/issues/22)
- [#23: Build an isolated League acceptance sandbox and reversible cutover harness](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23) — the repository-local disposable sandbox, fixture shadow, staged rollback, fake canary, and generation-switch fault foundation are merged; lifecycle receipt integration, real-runtime canaries, authorized global switch, and live smoke remain open

The merged #3/#4/#5/#17 slice and PR #30's #7/#11/#14 slice extend only the
repository-local store, command facade, and injected-adapter services. They do
not cross #23's install, live import, hook, watcher-replacement, real canary, or
cutover gates. The filesystem baseline remains live until one coherent release
is separately authorized and verified.
