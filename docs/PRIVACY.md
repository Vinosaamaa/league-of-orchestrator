# League outbound privacy boundary

> **Status: repository-local contract for issue #25.** It adds no network
> transport, installation, global guidance mutation, live import, or cutover.

## Classification and projection

League stores exact project roots, private repository identities, and full
canonical JSON local evidence plus its reference and hash as `local_only`.
Project visibility, export-policy, withheld-value, and classification semantics
have one canonical definition in the
[Project Catalog visibility contract](PROJECT_CATALOG.md#visibility-and-deterministic-transfer).
Repository-relative paths, approved public HTTPS URLs, opaque League IDs,
SHA-256 values, and explicit placeholders are allowed by this outbound boundary.

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
remote receipt identity. Each call also carries a deterministic idempotency
key; a transport error or invalid receipt is an explicit unknown outcome whose
retry must reuse that key.

## Publication metadata and staged guidance

`scripts/public_safety.py` checks every unpublished commit in an explicit Git
range. Both author and committer emails must be GitHub no-reply identities; a
failure prints only the commit hash and identity category. This prevents the
metadata class that affected PR #34 without embedding the exposed value in the
repository or failure output.

`global-agent-instructions/league/AGENTS.md` is the bounded source-managed
League orchestration supplement. The universal `~/.agents/AGENTS.md` remains
owned and installed solely by terminal-environment-toolkit. `src/league/guidance.py`
accepts only the relative `league/AGENTS.md` target beneath an explicit isolated
Codex, Cursor, or Pi agent root. It rejects universal or alternate targets
before mutation, backs up and atomically stages only the supplement, and proves
the universal hash unchanged before and after install and rollback. It has no
home-directory default and no CLI. Toolkit issue #45 owns universal-guide
reconciliation; League issue #90 owns this refusal boundary. Repository
publication contains no installed path, hook payload, runtime identity, raw
prompt, or machine state.
