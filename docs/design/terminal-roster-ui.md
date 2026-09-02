# Terminal-first Roster UI

> **Decision for issue #12: use Layout A, Project Ledger, for the later
> implementation slice.** This issue produces design artifacts only. It does
> not add an interactive TUI, controller, command key, daemon, scheduler, or
> durable UI cache.

## Purpose and guardrails

The terminal view lets a Summoner or Shotcaller answer three questions quickly:

1. What needs attention now?
2. Which project, Squad, Shotcaller, and Champion own the visible work?
3. Which exact canonical record supports the displayed claim?

It reads one `league.roster-snapshot.v1` value. It never scans live files,
opens a second SQLite connection per panel, calls a runtime adapter while the
snapshot transaction is open, or infers success from process existence. Any
later evidence drill-down is read-only.

## Minimum reliable field audit

| Source | Reliable fields | Used in the v1 snapshot | Explicit non-inference |
| --- | --- | --- | --- |
| Project catalog | Project ID, summary, aliases, code, state, version, exact local root/repository, ordered suggested Squads | Identity, label, state, suggestions; exact root/repository only in local visibility | A suggestion is not ownership or permission to route |
| Squads and agents | Squad state; Shotcaller/Champion ID, callsign, role, status, version, update time, update, blocker, next action, retirement | Complete current visible identities, availability, status and prose | A live-looking agent is not proof that its task succeeded |
| Tasks and assignments | Task ID, project, request, owner, coordinator, Champion, state, version, result summary, assignment state | Project grouping, work state, owner, task-to-agent nesting | Task completion does not answer a request or authorize cleanup |
| Requests | Request ID, owner, return target, execution mode, state, version, update time | Unattached unresolved work and task context | A routed or completed task does not imply an answered request |
| Events and task transitions | Stable event/transition ID, state, update, blocker, next action, ordered time | Recent transitions and exact evidence links | Delivery state is not recipient acknowledgement unless its receipt exists |
| Watcher tables | Registration lease, scope generation, reconciliation condition/count, outbox state | Future detail view only; current snapshot uses committed material events | Watcher silence is not runtime health |
| Runtime tables and adapters | Persisted binding state, verified flag, capabilities, last observation; capability matrix | Future detail view may label verified observation age | Adapter capability is not current liveness; unavailable adapters yield unknown |

Private roots, repository identities, session locators, endpoints, model names,
prompt text, and update prose are redacted in outbound mode. The design samples
below are synthetic and contain no live names, paths, repositories, or records.

### Provider presentation role boundary

Provider-facing display metadata may carry one canonical League role token:
`orchestrator_role=shotcaller` or `orchestrator_role=champion`. League derives
it only from exact canonical role ownership and omits it for missing or unknown
roles. It is independent of `sidebar_name`, thread title, terminal title, and
task label, and provider-generated title refreshes do not own it. The token is
receipt-bound to League's existing presentation overlay; a changed unowned
value refuses or an exact owned retry restores it.

This document assigns no rendering to that token. In particular it defines no
marker, glyph, color, ANSI, conditional style, pane decoration, or additional
sidebar text; any source-managed terminal renderer remains an owning-layer
decision outside League.

### Canonical naming boundary

League renders names from explicit role, callsign, project-code, and two-word
task metadata. Shotcaller sidebar/thread/terminal names are the callsign.
Champion sidebar is the callsign; Champion thread and terminal title are
`<Callsign> · <PROJECT>` when an exact project code is supplied and
`<Callsign> · <Two Word Task>` otherwise. The fallback task is always exactly
two words.

Provider/runtime names are neither display inputs nor title-parsing hints.
Codex, Cursor, and Pi-backed endpoints consume the same metadata contract;
provider launch and Herdr rendering remain outside this issue's boundary.
Prompt/context/OSC/restart refreshes may be repaired only under exact League
ownership. Token-only icon/status refreshes leave correct names untouched, and
a newer user-owned source is never overwritten.

## Layout options

### Layout A — Project Ledger (selected)

One vertical reading order works at narrow and wide terminal widths. A compact
summary is followed by collapsible project sections; the selected row owns a
detail/evidence pane below it.

```text
LEAGUE  as of 12:00:00Z   ! 2 NEED ACTION   > 4 UNDERWAY   R 1 READY   ~ 1 STALE
Filter: all projects                                      snapshot: current

v PROJECT ALPHA  [ALPHA]  suggested: SQUAD NORTH, SQUAD WEST
  ! BLOCKED  SQUAD NORTH > CHAMPION A  task:compile-contract   age 08m
    blocker: waiting for synthetic fixture
    next: verify the bounded fixture
  R READY    SQUAD WEST  > CHAMPION B  task:review-candidate   age 03m
> PROJECT BETA   [BETA]
  > ACTIVE   SQUAD NORTH > CHAMPION C  task:write-tests        age 01m

DETAIL · task:compile-contract · version 4
Evidence: tasks/task:compile-contract@4  events/event:synthetic-42
```

Benefits: one semantic order, clear project context, good 80-column behavior,
and a direct League → project/Squad → Champion → evidence path. Cost: comparing
all blocked rows across many projects takes a filter or search.

### Layout B — Status Columns

```text
NEEDS ACTION             UNDERWAY                 RECENTLY FINISHED
PROJECT ALPHA            PROJECT BETA             PROJECT ALPHA
! task:compile-contract  > task:write-tests       R task:review-candidate
~ task:stale-review
```

Benefits: strongest status comparison on a wide screen. Costs: project context
repeats, 80-column terminals collapse awkwardly, horizontal movement is harder
for keyboard and screen-reader users, and large lanes hide one another.

### Layout C — Plain Hierarchical Report

```text
PROJECT ALPHA
  NEEDS ACTION
    ! CHAMPION A · task:compile-contract · blocked · age 08m
  RECENTLY FINISHED
    R CHAMPION B · task:review-candidate · ready_to_land · age 03m
```

Benefits: lowest implementation cost, best pipe/print behavior, and no focus
model. Costs: weak repeated scanning, no persistent detail context, and no
efficient drill-down in a large Roster. This remains the non-interactive
fallback renderer, not the selected TUI layout.

## Why Project Ledger wins

Project grouping is the unique requirement that changes the information
architecture. Layout A preserves that hierarchy instead of turning project
names into repeated labels. Its single vertical order also supports color-free
status prefixes, terminal resize, text export, and a later detail pane without
adding command authority.

## Representative states

The status word and leading symbol always appear together; color is optional.

| State | Row form | Required explanation |
| --- | --- | --- |
| Empty | `- EMPTY · No current Roster entries` | Show snapshot time and active filters; do not invite a write action |
| Active | `> ACTIVE · CHAMPION A · task:write-tests · age 01m` | Show project and Squad ancestry |
| Blocked | `! BLOCKED · CHAMPION A · task:compile-contract · age 08m` | Show blocker and next action or the literal `not recorded` |
| Ready | `R READY · CHAMPION B · task:review-candidate · age 03m` | Spell out `ready_to_land` in details; never imply merged or deployed |
| Stale | `~ STALE · CHAMPION C · task:verify-contract · age 2h` | Show the stale threshold and last canonical update; never infer a dead runtime |
| Unavailable | `X UNAVAILABLE · SQUAD WEST · Shotcaller unavailable` | Preserve the named suggestion; never substitute another Squad |
| Large | `+ 198 MORE · bounded snapshot truncated at 200 items` | Keep visible counts tied to the loaded page; name the bound explicitly |
| Failed read | `! SNAPSHOT FAILED · storage_busy · retryable` | Show error code and retryability; never render an empty Roster as success |

If a refresh fails after one successful in-memory view, the old view may remain
onscreen with `STALE VIEW — refresh failed` and its original timestamp. That
copy is process memory only and disappears on exit; it is not persisted.

## Keyboard and navigation contract

- `Up`/`Down` and `k`/`j`: previous or next visible row.
- `Left`/`Right` and `h`/`l`: collapse or expand the selected project or row.
- `Home`/`End` and `g`/`G`: first or last visible row.
- `PageUp`/`PageDown`: move by one viewport without changing the snapshot.
- `Enter`: open or close the read-only detail/evidence pane.
- `/`: open a transient local filter; `n`/`N` moves between matches.
- `r`: request one new bounded snapshot. It performs no other action.
- `?`: show the key legend; `Esc` closes filter, detail, or help; `q` exits.
- `Tab`/`Shift-Tab`: move among header filter, Roster, detail, and help
  landmarks. The header exposes focusable Filter, Previous match, Next match,
  Refresh, Help, and Exit controls activated with `Enter`; these are the exact
  non-letter alternatives for `/`, `N`, `n`, `r`, `?`, and `q`. Arrow keys,
  `Enter`, `Tab`, and `Escape` cover row movement, expansion, activation, focus
  movement, and dismissal without requiring letter keys.

There are deliberately no assign, route, send, merge, deploy, stop, teardown,
or retry-work keys. A read failure can be retried only by the ordinary refresh
key.

## Refresh, focus, and failure semantics

The later TUI reads once at startup and on explicit `r`. Automatic polling is
out of the smallest slice. Each successful response displays `as_of`, recent
and stale boundaries, visibility, item limit, and truncation flags. Refresh
replaces the whole view only after a valid schema-complete response. Selection
is restored by exact entity ID; if that ID disappears, focus moves to the next
row in the same project, then the project header.

`as_of` is an inclusive read boundary, not a promise to reconstruct earlier
versions of mutable rows. Records and evidence with later timestamps are
omitted; the view never presents future-dated state as part of the snapshot.

`busy`, `store_missing`, `migration_required`, schema mismatch, and integrity
failure remain distinct error codes. Runtime observation unavailable is a row
detail of `unknown`; it does not fail a valid canonical snapshot. A malformed
or partial response is never rendered.

## Accessibility and truncation

- Respect `NO_COLOR`, offer `--no-color`, and use the symbol plus full status
  word even when color is enabled. ASCII mode maps the symbols to `-`, `>`,
  `!`, `R`, `~`, and `X`.
- Use one logical top-to-bottom reading order. Columns are visual alignment,
  not the only carrier of relationships.
- Announce refresh completion, count changes, focus relocation, and failures in
  one reserved status line; do not stream routine liveness noise.
- At widths below 80 columns, hide aliases first, then Squad repetition, then
  age. Never hide the status word, selected callsign, task ID, blocker marker,
  or truncation marker.
- Truncate on grapheme boundaries with a visible ellipsis. The detail pane
  shows the full selected value and wraps it; it never creates horizontal
  scrolling.
- At large bounds, render only the returned rows and the explicit `+ N MORE`
  marker. Do not silently fetch, persist, or merge another snapshot.
- With `TERM=dumb`, emit Layout C once as plain text and exit successfully.

## Smallest later implementation slice

Build a separate read-only `league-roster-view` process that:

1. invokes the stable `roster snapshot` facade for one local-only snapshot and
   never logs or exports its private fields;
2. validates `league.roster-snapshot.v1` completely;
3. renders Layout A with project collapse, row navigation, details, help, and
   manual refresh;
4. supports `NO_COLOR`, ASCII fallback, 80-column resize, empty/failure/large
   states, and deterministic text-mode output; and
5. has synthetic golden tests for all representative states.

No runtime adapter call, root/repository display, filter persistence, auto-refresh,
pagination, action command, or controller belongs in that slice. Those require
separate authorization after the read-only design is accepted.
