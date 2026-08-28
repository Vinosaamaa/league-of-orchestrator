# Baseline and planned work

The parent plan is
[#1: Build agent-agnostic League of Orchestrator and disposable Shotcaller lifecycle](https://github.com/Vinosaamaa/league-of-orchestrator/issues/1).

Issue #2 imports the proven watcher/Roster/routing/teardown baseline. The items
below remain planned League work even when the toolkit baseline contains a
related partial behavior:

- [#3: Deliver the exact Champion transition without pending-event cross-wiring](https://github.com/Vinosaamaa/league-of-orchestrator/issues/3)
- [#4: Enforce explicit direct, hidden, or Champion dispatch before work](https://github.com/Vinosaamaa/league-of-orchestrator/issues/4)
- [#5: Add bounded Stop-hook and Shotcaller wake continuity](https://github.com/Vinosaamaa/league-of-orchestrator/issues/5)
- [#6: Choose the minimal durable Roster storage contract](https://github.com/Vinosaamaa/league-of-orchestrator/issues/6) — accepted as ADR 0002; implementation and live migration remain separate
- [#7: Make harness and terminal routing adapter-based](https://github.com/Vinosaamaa/league-of-orchestrator/issues/7)
- [#8: Support guarded disposable Shotcaller handoff](https://github.com/Vinosaamaa/league-of-orchestrator/issues/8)
- [#9: Add an advisory project catalog and project-grouped Roster](https://github.com/Vinosaamaa/league-of-orchestrator/issues/9)
- [#10: Declare skill provenance and runtime capabilities](https://github.com/Vinosaamaa/league-of-orchestrator/issues/10)
- [#11: Generalize guarded teardown across task classes](https://github.com/Vinosaamaa/league-of-orchestrator/issues/11)
- [#12: Design a terminal-first Roster UI](https://github.com/Vinosaamaa/league-of-orchestrator/issues/12)
- [#13: Add history-aware callsign allocation and reuse cooldown](https://github.com/Vinosaamaa/league-of-orchestrator/issues/13)
- [#17: Add a durable request inbox and unresolved-work reconciliation](https://github.com/Vinosaamaa/league-of-orchestrator/issues/17)
- [#18: Audit every JSON/JSONL state dependency before SQLite migration](https://github.com/Vinosaamaa/league-of-orchestrator/issues/18) — completed by the sanitized producer-consumer matrix and cutover inventory
- [#19: Implement SQLite storage and atomic canonical cutover](https://github.com/Vinosaamaa/league-of-orchestrator/issues/19) — this candidate implements the repository-local store, audited dry-run import, command facade, and focused tests; the issue remains open through live cutover
- [#21: Document the SQLite orchestration design and decision trail](https://github.com/Vinosaamaa/league-of-orchestrator/issues/21)
- [#22: Add evidence-backed League activity and end-of-day reports](https://github.com/Vinosaamaa/league-of-orchestrator/issues/22)
- [#23: Build an isolated League acceptance sandbox and reversible cutover harness](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23) — owns staged install, read-only shadow, atomic global switch, rollback, and live smoke

Issue #19 does not pre-build sibling lifecycle policy or cross the #23 install
and cutover gates. The filesystem baseline remains live until one coherent
release is separately authorized and verified.
