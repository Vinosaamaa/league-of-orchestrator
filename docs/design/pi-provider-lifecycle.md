# Pi runtime and provider lifecycle

Issue #84 models Pi as the runtime and Cursor/Codex as providers. Both
providers share Pi's standard home and session inventory. League never selects
a provider by setting a Pi home, and no wrapper participates in launch or
resume.

## Durable descriptor

Each launch records a checksummed descriptor before external effects. It binds
the provider, model, effort, exact cwd, Shotcaller/Champion role, sibling-pane
or new-tab placement, callsign, project code, two-word task label, routing
name, release root, and create/fork/resume session inputs. Champions create one
new tab. Shotcallers create one sibling pane from the exact creator pane.

Create uses a deterministic session ID. A project-bound fork uses the exact
parent JSONL path and can occur once for a parent/cwd pair. Resume and restart
use the exact bound child JSONL path. The stored parent path remains lineage
evidence; restart never forks again.

## Trust and activation

Pi may otherwise stop at an interactive project-trust gate. The launcher first
verifies that the assignment cwd is the exact issue worktree and repository
root. Only that launch receives `pi --approve`; no parent directory, second
worktree, or global trust entry is authorized.

Trust and startup are pre-activation. For fresh create/fork, the Pi extension
waits for native `session_start`, reads the session manager's exact ID, absolute
JSONL path, and optional parent path, then publishes canonical Herdr metadata.
Exact resume already owns a byte-verified bound path and publishes a path digest
from the launcher; Herdr's native path is used when available but is not
fabricated when a legacy pane does not surface it. League requires one exact Pi
foreground process in the assigned cwd, the descriptor digest, provider and
role metadata, the canonical pane label, and two unchanged readbacks. Raw Pi
terminal-title changes are part of the stability fingerprint but cannot replace
the canonical pane label.

## Unified-inventory migration

Migration of an existing isolated session occurs only at a controlled restart
boundary where Herdr proves the exact pane has returned to its shell and has no
foreground process. A strict manifest binds the legacy inventory root, unified
inventory root, relative JSONL path, source digest, descriptor, and endpoint.

League reads only the first JSONL record for identity/cwd/parent validation,
and the first parent record when lineage exists. It hashes the complete source,
rejects a duplicate session ID at any other unified path or digest, copies with
exclusive mode-0600 creation, fsyncs, and verifies the same digest. Source
bytes and the embedded parent path are unchanged. Durable intent, copy, bind,
and restart receipts make interruption and retry effect-safe.

After migration, plain `pi --resume` with the All tab is the inventory
acceptance boundary. Herdr then starts `pi --session <exact-child-path>` in the
stored cwd. All launch metadata, including pane identity, is passed as explicit
Pi arguments rather than inherited environment. A restart ID can apply that
effect once; retry returns the original receipt without new terminal input or
another process.

## Refusals

Launch or migration fails closed for an unverified worktree, missing release
integration, ambiguous/multiple process, wrong cwd or pane, missing session
path/ID, changed descriptor metadata, unstable/native title, active migration
pane, invalid or changed JSONL, missing parent, duplicate session identity,
changed source digest, existing different destination, or repeated fork.

This issue does not implement the Herdr renderer or issue #95's glyph work.
