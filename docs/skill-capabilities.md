# Skill provenance and runtime capabilities

Issue [#10](https://github.com/Vinosaamaa/league-of-orchestrator/issues/10)
adds one bounded repository-local contract for custom-root skills. It is an
inventory and validation surface, not a plugin loader, package manager, skill
installer, or source synchronizer. It never mutates a skill or chooses a model
or provider.

## Contract

[`config/custom-skills.json`](../config/custom-skills.json) records:

- one public label for each explicit custom root; exact machine paths are
  supplied only to `skill audit` at runtime;
- one identity, `shared` or `specialist` scope, recorded canonical source owner
  or explicit `unrecorded` classification, and declared or unrecorded version
  classification for every skill;
- required and optional `harness`, `tool`, `platform`, `browser`, `forge`,
  `delegation`, and `multiplexer` capability identifiers;
- an inline or refuse fallback and the execution mode used when the declared
  optional capability is available;
- one directory/symlink kind, bounded content-tree SHA-256, and source-parity
  classification for every installed copy.

The runtime validator requires sorted unique records, exact fields, one
definition per identity, at least one installation per definition, and one
definition-owned provenance classification across duplicates. A recorded owner
is a public `owner/repository` identifier only; no source checkout or local
path is retained. `unrecorded` and `unverified` are deliberate evidence states,
not inferred failures or synthetic provenance.

## Commands

Validate the public contract without creating League state:

```sh
./bin/league skill validate --config config/custom-skills.json
```

Audit an installation by binding every declared label to an exact local root:

```sh
./bin/league skill audit \
  --config config/custom-skills.json \
  --root agents-custom=/absolute/custom-root-a \
  --root codex-custom=/absolute/custom-root-b
```

The audit considers every direct directory or symlink with a regular
`SKILL.md`; containers and metadata entries without that file are outside the
custom-skill inventory. It hashes bounded regular files, records top-level
symlink installs as symlinks, and refuses nested links, non-regular content,
excessive content, missing labels, inventory drift, entry-kind drift, and hash drift. Results
contain labels, identities, hashes, counts, and parity only. They explicitly
assert that local paths and skill bodies are absent.

Resolve the contract against an explicit capability profile:

```sh
./bin/league skill matrix \
  --config config/custom-skills.json \
  --profile config/skill-runtime.example.json
```

The profile selects one pair from the existing harness/backend adapter matrix
and supplies only provider- and model-neutral capability identifiers. The
result embeds that registered pair's evidence and driver availability, then
reports each skill as `available`, `available_inline`, or `unavailable` with
the exact missing capability identifiers.

The checked-in profile is illustrative and intentionally grants neither a
browser, forge, delegation, nor multiplexer capability. Selecting a named
contract-only adapter pair is not evidence that its runtime driver exists.

## Delegation and specialist boundaries

`research` is shared. `delegation.background-visible-agents` is optional: when
present its execution is `delegate`; when absent its deterministic execution is
`inline`. A background or hidden worker that is not visible to the caller does
not satisfy this declaration.

Herdr, Lavish transcript rendering, terminal browser control, hosted review
loops, Draw.io, Spring Boot, issue-tracker publication, and local design-database
work remain specialist declarations with explicit requirements. Missing a
specialist requirement returns `unavailable`/`refuse`; the matrix never labels
the skill portable merely because a harness or backend name exists.

## Sanitized current inventory

The read-only 2026-08-28 audit is stored as
[`docs/research/custom-skill-audit.json`](research/custom-skill-audit.json). It
contains 25 copies across two labeled roots and 23 unique skills: 10 recorded
source owners and 13 explicit unrecorded classifications; 10 shared and 13
specialist definitions. `frontend-design` has equal duplicate content hashes.
The two top-level `terminal-browser` symlink copies have different content
hashes, so duplicate install parity is deterministically `mismatched`; both
copies still match their individually declared audit hashes. No installation
was changed to manufacture parity.

All 25 source-parity fields remain `unverified` because the audit did not fetch
or select source checkout bytes, and every version without an authoritative
version record remains `unrecorded`. This honest metadata satisfies the
provenance contract without claiming source/install equality. Issue
[#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23) still owns
release-to-installed parity, staged installation, real-runtime canaries,
cutover, rollback, and smoke.
