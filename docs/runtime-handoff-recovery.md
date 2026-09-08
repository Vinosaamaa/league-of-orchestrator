# Process-preserving handoff recovery (#66)

A native multiplexer handoff can preserve running agent processes and provider
sessions while rotating terminal IDs. Canonical generations then become stale;
process existence alone is not sufficient evidence to rebind them.

`league runtime reconcile-handoff --before-agent-list <local-json>
--owner-authorized --at <RFC3339> --check-only` verifies a saved native inventory
against current discovery and two native process observations. Omit `--check-only`
only after the exact recovery is authorized. This command never starts, prompts,
closes, renames, or completes an agent or task.

Only matching pane, route, provider session, workspace, tab, and old canonical
generation are eligible. Unrelated or already-stale bindings remain explicitly
skipped. Changed live identity refuses before canonical writes. Each eligible
runtime uses a compare-and-swap; failed observation state may become verified
active again without changing task status. Shotcaller watcher bindings use the
existing fenced rebind and verification operations. Partial watcher failure
retains a recovery obligation; retry uses the original inventory.

Focused synthetic coverage: active/idle/failed bindings, same-generation failed
retry, process/route mismatch refusal, check-only database immutability, stale
generation preservation, watcher failure/retry, unchanged task/request/delivery
and cleanup records. Installed recovery is a separate receipt, not established
by those tests.
