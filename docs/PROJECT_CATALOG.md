# Advisory project catalog and project-grouped Roster

> **Status: repository-local v2 contract for issues #9, #12, and #25.** It does not
> install, import live records, change routing authority, or implement the
> terminal UI designed in [terminal Roster UI](design/terminal-roster-ui.md).

## Authority boundary

The catalog helps a Shotcaller recognize a project. It never owns a task and it
never selects a worker. An explicit user route remains authoritative even when
the project has suggested Squads. A mapping edit changes only the catalog row
and its suggestion rows; it does not update tasks, assignments, requests,
events, task transitions, Roster records, or project instructions.

The Roster snapshot is also advisory. It is assembled from the canonical
SQLite facade in one bounded read transaction and is marked `canonical: false`
and `read_only: true`. It is not cached to disk and cannot become a second
state store.

## Canonical identities

- Project IDs use `project:<slug>` and remain stable when a root, alias, code,
  summary, or suggestion changes.
- Repository input is normalized to host plus remote path. HTTPS, SSH URL, and
  SCP-like Git spellings for the same remote resolve to one key. Credentials,
  query strings, fragments, `.` segments, and `..` segments are refused.
- Roots must be exact absolute paths. Normalization removes redundant trailing
  separators without resolving symlinks or consulting the filesystem.
- Codes and aliases use exact Unicode-normalized, case-insensitive keys. Codes,
  roots, and repository keys cannot be assigned to two projects. An alias may
  describe several projects; resolving that alias returns
  `ambiguous_project` instead of choosing one.
- Automatically imported legacy projects derive their canonical ID from the
  normalized repository key rather than the original URL spelling. Migration
  v5 indexes existing repository identities once through the same canonical
  normalizer, so later resolution and writes use the index instead of rescans.

## Catalog data

Each project stores one concise summary, zero to sixteen aliases, an optional
code, one exact local root, one exact remote repository identity, active or
retired state, a monotonic compare-and-swap version, and an update timestamp.
`project_aliases` and `project_squad_suggestions` are normalized many-to-many
tables. One Squad may be suggested for several projects and one project may
suggest several Squads in a deterministic order.

A suggestion is available only while its Squad is active and its Shotcaller is
not retired or terminal. Retired and unavailable suggestions remain visible
with a reason; they do not cause a task to move to another Squad. `project
advise --explicit-squad-id ...` reports the explicit route separately and never
substitutes a suggestion.

## Stable commands

All commands require an explicit, already migrated state root. Examples use
placeholder identities only.

```sh
./bin/league --state-root <state-root> project put \
  --project-id project:alpha --expected-version 0 \
  --summary 'Example coordination project' \
  --repository https://example.invalid/team/alpha.git \
  --root <exact-local-root> --code ALPHA --alias alpha \
  --repository-visibility unknown --export-policy metadata_only \
  --state active --at 2026-01-01T00:00:00Z

./bin/league --state-root <state-root> project resolve --code ALPHA
./bin/league --state-root <state-root> project list --visibility outbound

./bin/league --state-root <state-root> project suggest-squads \
  --project-id project:alpha --expected-version 1 \
  --squad-id squad:north --squad-id squad:west \
  --at 2026-01-01T00:01:00Z

./bin/league --state-root <state-root> project advise \
  --project-id project:alpha --explicit-squad-id squad:user-choice
```

Create uses expected version zero. Update and suggestion replacement require
the exact version read by the caller. An exact retry returns `idempotent: true`;
a competing non-identical writer returns `version_conflict` after the bounded
SQLite wait.

## Visibility and deterministic transfer

Local catalog reads include the exact repository and root. Outbound v2 reads
always return a `local_only` root classification and a null root. `deny` also
withholds summary, aliases, code, and repository; `metadata_only` permits only
the validated descriptive fields; `public_repository` permits those fields and
one validated public HTTPS repository only when visibility is explicitly
`public`. The default Roster command visibility is outbound;
local terminal software must request local visibility deliberately.
Legacy imports use the generic summary `Imported project`; they never derive an
outbound-visible label from a private repository path.

Migration v5 adds the catalog fields, normalized aliases, suggestions, and
Roster lookup indexes automatically through `league storage migrate`. Existing
database upgrades still require the verified backup gate. The issue-#18
importer deterministically includes every new project column and both new tables
in its digest-bound plan. Inspection and restricted rollback exports include
the same tables in stable key order; inspection redacts repository keys, roots,
and task or transition prose.

## Roster snapshot contract

`roster snapshot` requires explicit snapshot, recent, and stale timestamps:

```sh
./bin/league --state-root <state-root> roster snapshot \
  --as-of 2026-01-01T12:00:00Z \
  --recent-since 2026-01-01T06:00:00Z \
  --stale-before 2026-01-01T10:00:00Z \
  --limit 200 --visibility outbound
```

The response uses `league.roster-snapshot.v1`. It contains global Squads,
project groups, an unresolved-project group when needed, recent material
transitions, source bounds, and explicit truncation flags. Work is classified
once, in this precedence order:

1. `needs_action`: blocked, failed, rejected, awaiting-user,
   awaiting-requester, or non-terminal work older than the stale boundary.
2. `recently_finished`: terminal work at or after the recent boundary.
3. `unresolved`: work without a canonical project, plus unattached unresolved
   requests or Champions.
4. `underway`: other current non-terminal work.

Older terminal work is omitted from the current Roster rather than mislabeled
as recent. Each item includes exact table/key/version references and, when
available, the latest event and task-transition references. Locators use the
non-network `league://` namespace; they identify canonical rows but are not
authority to mutate them.

`as_of` is an inclusive upper bound for every source and evidence query. Rows
with later timestamps are omitted; because mutable rows do not retain every
historical version, the snapshot does not claim to reconstruct a past state.

The output schemas are
[project catalog](../schema/league-project-catalog.schema.json) and
[Roster snapshot](../schema/league-roster-snapshot.schema.json).
