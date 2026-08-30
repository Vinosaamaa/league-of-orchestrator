# Issue #85: Champion launch title overwrite

## Incident

The visible Champion launcher wrote and verified `<Callsign> · <Task>` before
delivering its launch handshake or assignment context. Herdr/Codex could then
auto-title the visible sidebar, thread, and terminal from that prompt. League
recorded context delivery without observing the final display state, so the
canonical routing identity remained correct while the human-facing identity
drifted to prompt text.

## Owning-layer correction

`league assign run` now treats the initial metadata report as a seed, not the
final acceptance gate. The real launch adapter applies an assignment-scoped
title ownership token, delivers the bounded context, restores the exact
callsign/task metadata when provider auto-title changes it, and verifies all
three visible title surfaces before context delivery is recorded.

The final ordering invariant is:

1. verify the generated runtime and exact endpoint;
2. seed launch-owned routing and display metadata;
3. activate and deliver the bounded assignment context;
4. require the same endpoint, thread, routing name, task metadata, and title
   ownership token;
5. restore and verify `<Callsign> · <Task>` on the sidebar, thread, and
   terminal;
6. only then record successful context delivery.

An exact completed retry performs no adapter mutation. If the endpoint or title
ownership changed before restoration, League refuses to overwrite it and uses
the existing exact-runtime cleanup path. Unproven cleanup remains a truthful
cleanup obligation.

## Regression boundary

Focused fake-Herdr coverage makes context delivery auto-title the runtime,
proves the assigned title is restored afterward, proves completed retry is
idempotent, and proves changed ownership metadata refuses restoration. The
fixtures use temporary repositories and synthetic identities only.
