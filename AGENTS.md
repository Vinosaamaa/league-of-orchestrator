# Agent Instructions

## Scope

- Repository work → use one issue-owned worktree and branch; keep unrelated
  checkouts and live agent state unchanged.
- Baseline change → preserve proven behavior or name the deliberate difference
  in `docs/PROVENANCE.md` and cover it with a focused test.
- Planned feature → route to its owning issue in `docs/ROADMAP.md`; do not fold
  it into the bootstrap.

## Safety

- Tests → use synthetic temporary records and fake adapters only; never point
  them at `~/.agents`, a live multiplexer, or a real repository worktree.
- Public egress → audit candidate bytes and reachable Git objects first; stop on
  credentials, tokens, private endpoints, personal data, transcripts, generated
  artifacts, or machine state.
- Sensitive finding → report only its repository path and class; never echo the
  value.
- Launch preflight → reconcile the Roster, callsign-pool release, and live
  endpoint; record routing name and displayed backend kind only after the
  launched endpoint verifies, rolling back a partial reservation on failure.
- Install or migration → require explicit authority, exact source/installed
  parity, backups, and a verified rollback; issue #2 performs neither.

## Verification

- Local baseline → run `make test`.
- Record contract → keep `status.json` and the latest `updates.jsonl` event exact
  on status, timestamp, and update text.
- Teardown → fail closed unless every identity, Git, publication, deployment,
  smoke, resource, and archive gate required by the current schema is proven.
