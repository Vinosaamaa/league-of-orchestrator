# Issue #66: bounded native Stop continuation

The installed hook repeatedly returned the same unresolved-work reminder at an
unchanged wait generation. Codex turns each blocking Stop response into another
model continuation. The agent repeatedly ended those continuations, including
empty replies after sleeps, so the same hook reactivated indefinitely. A prior
foreground wait also held the scope lock, preventing a second wait. Ending again
was not recovery and amplified the user-visible spam.

## Deliberate policy correction

The previous every-Stop-blocks policy is replaced for native Codex Shotcaller
continuations only. After native input validation, the hook reads the exact bound
Shotcaller scope. When `stop_hook_active` is true and the current wait generation
was already reported, it returns empty native output before contacting the
supervisor. It neither claims successful handoff nor writes canonical state.

The initial reminder remains. Transport retries with `stop_hook_active=false`
still return that reminder. A newly captured user prompt advances the canonical
generation and rearms it, even if Codex retains the same native turn ID. Missing,
busy, unbound or mismatched state does not receive this exemption. Champion Stop,
other providers, explicit owner-stop controls, completion evidence, pending
delivery and cleanup checks remain separate and unchanged.

This is a narrow correction, not a general solution for a first Stop failure
that has no saved reminder, native wait cancellation, or idle-only wake races.
It does not disable hooks or declare the orchestrator complete.

## Verification

`tests/test_stop_continuation.py` exercises the native command shape with a
synthetic database: initial block, unchanged original-hook retry, five quiet
continuations while the broker is deliberately unavailable, byte-identical
logical database contents, fresh steering, and no exemption for unbound or
malformed input. The existing real-payload generation test adopts the deliberate
new continuation expectation while retaining prompt capture and rearm checks.

Official native contract: <https://learn.chatgpt.com/docs/hooks#stop>.
Source/test results alone do not prove the final live Stop boundary.
