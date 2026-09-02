# League restored-agent reconciliation plugin

This is a supported one-shot asynchronous Herdr startup plugin. Herdr restores
its existing panes and processes first. After its API socket is ready, the hook
reads League's canonical SQLite state and reconciles display metadata onto each
exact native agent session. Before presentation it restores the canonical
routing name, CAS-rebinds the new terminal/runtime generation, and verifies the
existing Shotcaller watcher and wake locator. The hook never creates or closes
a pane, launches or resumes an agent, or prompts a model.

Link or install the plugin once and enable it through Herdr's normal plugin
lifecycle. Disabling it leaves ordinary Herdr startup unchanged. A short native
fallback title may be visible before the asynchronous hook converges. Hook
failure is recorded in Herdr's normal plugin command log and the reconciler
refuses a missing, replaced, duplicated, or ambiguous session rather than
targeting a best guess.

`LEAGUE_STATE_ROOT` may bind an explicit absolute canonical state root. Without
it, the hook accepts only the standard SQLite writer pointer and its sibling
`league` directory. `LEAGUE_COMMAND` may bind an installed League executable;
otherwise `league` is resolved through the plugin process `PATH`.
