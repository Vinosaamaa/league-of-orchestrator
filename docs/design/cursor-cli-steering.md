# Cursor CLI steering

Issue [#84](https://github.com/Vinosaamaa/league-of-orchestrator/issues/84)
adds a provider-faithful Cursor direct-delivery effect without changing the
canonical outbox or granting new live authority.

## Problem and root cause

League's generic direct adapter sent every transition with an idle-style
prompt operation. Cursor has two distinct input contracts:

- idle or done: literal prompt text followed by one Enter submits work;
- working: literal prompt text followed by two Enter keys interrupts and steers.

Applying the second sequence to the wrong pane, a replaced session, or a state
that changed after observation can interrupt unrelated work. Retrying after an
uncertain command can also duplicate the text or interrupt twice. Finally, a
routed Cursor Shotcaller previously had to infer an internal request-accept
command, including claim details it does not own.

## Decision

`InstalledDeliveryAdapter` selects `HerdrCursorSteeringAdapter` only when the
canonical direct target declares Cursor on Herdr. Before any input, the adapter
requires all of the following to agree:

- canonical runtime instance and generation;
- exact Herdr pane and Cursor session;
- exact routing name when canonical state provides one;
- `interactive_ready=true` and an understood Cursor status;
- non-negative Herdr revision and state-change sequence;
- exactly one foreground `cursor-agent` with a valid PID and foreground process
  group.

The observations come from direct supported `herdr agent get` and
`herdr pane process-info` commands. Transcript contents are not identity
evidence.

For idle/done, League records an intent and invokes one `herdr agent prompt`,
which supplies literal text and one Enter. For working, League records the
intent, re-proves the unchanged binding, invokes `herdr pane send-text`, records
the text phase, re-proves the unchanged pane/session/status/revision/sequence/
process binding, and only then invokes `herdr pane send-keys ... enter enter`.
Both paths require a post-effect observation of the same pane, session, and
Cursor PID with an increased state-change sequence before recording the effect.

## Durable retry fence

Schema migration 21 adds one `cursor_steering_effects` row per exact
outbox/event/recipient effect. It binds runtime generation, pane, session,
action, observed status, and the prompt digest. Prompt text is not persisted in
this table.

The states are:

1. `intent_recorded`
2. `text_sent` for working delivery only
3. `effect_applied`
4. `acknowledged` when the canonical outbox acknowledgement is committed

`refused` is terminal for a proved safety refusal. An exact retry of an applied
or acknowledged effect returns its existing receipt with no provider command.
An exact retry of a refusal returns the same refusal. An incomplete intent or
text phase is outcome-ambiguous and refuses, because replay could duplicate
input.

## Structured routed delivery

Cursor receives one `league.routed-delivery.v1` JSON envelope prefixed by
`LEAGUE ROUTED DELIVERY`. A `request_routed` envelope contains one
`league.routed-delivery-action.v1` action whose `argv` is the complete stable
command:

```text
league request accept-routed \
  --event-id EVENT \
  --recipient-agent-id AGENT \
  --runtime-instance-id RUNTIME
```

The command verifies that exact event, recipient, current runtime, request, and
routing receipt, then derives the current lease and a deterministic claim token
inside League. Exact retries are idempotent. The stable
`league delivery dispatch` command routes one exact outbox/event/recipient
through the installed provider selector.

## Refusals and rejected alternatives

League refuses before input for a wrong pane, replaced session, provider or
route mismatch, unavailable interactive input, unsupported status, malformed
identity, or missing/ambiguous Cursor process. A changed observation before an
effect or before working-state Enter keys records `cursor_state_changed`.
Missing post-effect state advance records `cursor_steering_ack_unverified`.

Rejected alternatives:

- always use one Enter: it cannot steer a working Cursor CLI;
- always use two Enter keys: it can interrupt the wrong or newly idle session;
- trust pane title, transcript text, or command exit status: none proves the
  exact current provider session and accepted state change;
- replay after a crash: input may already have reached Cursor;
- expose claim timestamps/tokens in the prompt: the receiving agent should not
  reconstruct canonical acceptance internals.

## Verification and rollout boundary

Provider-faithful fake Herdr tests exercise the exact command arrays for idle
submit, working steer, pre-interrupt race, duplicate retry, wrong pane/session,
unavailable input, ambiguous process, and post-steer acknowledgement. Storage
migration tests exercise schema-20-to-21 crash rollback and idempotent retry.

This change is repository-local. No installation, live steering, event replay,
Champion replacement, cleanup, teardown, merge, or owner-visible acceptance is
performed. Those remain separately reviewed and authorized gates.
