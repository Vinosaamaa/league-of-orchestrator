# League outbound privacy boundary

> **Status: repository-local contract for issue #25.** It adds no network
> transport, installation, global guidance mutation, live import, or cutover.

## Classification and projection

League stores exact project roots, private repository identities, and full
canonical JSON local evidence plus its reference and hash as `local_only`. A project also stores an explicit
repository visibility and export policy:

- `deny` publishes no project metadata;
- `metadata_only` permits only validated summary, code, and aliases;
- `public_repository` additionally permits one validated HTTPS repository URL
  only when repository visibility is explicitly public.

Outbound project values use `null` for withheld fields and include structured
classifications. Repository-relative paths, approved public HTTPS URLs, opaque
League IDs, SHA-256 values, and explicit placeholders are allowed.

## One final rendered-payload validator

Every League remote adapter calls
`validate_final_rendered_payload` on the exact UTF-8 bytes immediately before
its transport. Public and private destinations use identical checks. The
guarded adapter family covers GitHub issue, pull request, and comment payloads;
shared or published Lavish; deployment notes; report export; and any future
League remote adapter. A local-diagnostic payload cannot target this boundary.

The validator decodes HTML, percent, JSON, Unicode, slash, and hexadecimal
escapes before scanning. It refuses:

- absolute, home, worktree, archive, profile, temporary, and Application
  Support paths, plus `file://`;
- usernames, hostnames, PID, pane/tab/thread/session UUID, socket, loopback,
  private address, and private or unapproved endpoints;
- credentials, tokens, secret assignments, and private-key material;
- employer, applicant, personal, email, phone, or address data;
- binary, screenshot, log, or stack-trace payloads containing any refused
  class.

A refusal returns only category and field locally. It never includes the unsafe
value and the transport is not called. The regression fixture assembles a
GitHub issue body from live-like task state containing a worktree, proves zero
transport calls, then proves a placeholder-only body succeeds.

Successful transport returns a bounded redacted receipt with the adapter kind,
destination visibility, exact payload hash and byte count, and a hash of the
transport receipt identity. It never returns the body, local evidence, or raw
remote receipt identity.

## Publication metadata and staged guidance

`scripts/public_safety.py` checks every unpublished commit in an explicit Git
range. Both author and committer emails must be GitHub no-reply identities; a
failure prints only the commit hash and identity category. This prevents the
metadata class that affected PR #34 without embedding the exposed value in the
repository or failure output.

`global-agent-instructions/shared-AGENTS.md` is the small source-managed
cross-harness rule. `src/league/guidance.py` can stage exact bytes only beneath
an explicit isolated Codex, Cursor, or Pi root with backup, atomic replacement,
parity proof, and rollback. It has no home-directory default and no CLI. Issue
#23 owns any later installed guidance, migration, or cutover; this change does
not perform one.
