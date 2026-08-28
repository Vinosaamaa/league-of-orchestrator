# Isolated acceptance and reversible cutover foundation

Issue [#23](https://github.com/Vinosaamaa/league-of-orchestrator/issues/23)
owns this repository-local harness. It creates one disposable League home only
beneath an explicit existing temporary root. It does not discover or use a home
directory, global Roster, installed watcher, callsign pool, hook file, browser
profile, live delivery endpoint, or canonical writer.

Run the complete foundation with one command after creating task-owned sentinel
fixtures outside the requested namespace:

```sh
./bin/league acceptance run \
  --temporary-root /absolute/task-owned/temporary-root \
  --namespace issue-23-review \
  --sentinel-path /absolute/caller-specified/live-byte-sentinel \
  --config-sentinel /absolute/caller-specified/config-sentinel.json \
  --process-sentinel /absolute/caller-specified/process-sentinel.json
```

The process sentinel is synthetic and has this exact outer shape:

```json
{"processes":[],"schema":"league.synthetic-process-sentinel.v1"}
```

The command refuses missing, relative, symbolic-link, or malformed sentinels
and refuses an existing namespace. It accepts at most 16 byte sentinels so a
caller cannot create an unbounded preflight workload. The global
`--state-root` option remains mandatory for storage and domain commands, while
`acceptance run` uses only its separately named temporary root and refuses a
supplied state root. It leaves an owner-only
`acceptance-receipt.json` in the new home. The receipt conforms to
`schema/league-acceptance-receipt.schema.json` and records:

- a durable planned/executing/completed operation history; a failed attempt is
  recorded as resumable `blocked`, and the same command resumes it in a new
  isolated attempt only when the namespace and sentinel fingerprint still
  match;
- deterministic IDs and a fixed fake clock;
- fake harness, terminal, Git/GitHub, process/resource, notification,
  deployment, and hook adapters;
- byte, parsed-config, and synthetic-process sentinel parity;
- transactional schema migration, strict dry-run import, isolated apply, and
  exact fixture-row parity, with source/report/parity digests pinned to a fixed
  synthetic runtime root so receipts are independent of the temporary root;
- source/release/staged byte and version parity, launcher/help/JSON-Schema
  checks, a staged-runtime schema migration and integrity check, synthetic hook
  fixtures, permissions, path-leak refusal, and tested pointer rollback beneath
  a task-owned prefix;
- one sandbox-only generation-bound writer pointer and exclusive cutover lock;
- every pointer-switch fault stage, with a real child process stopped by
  `SIGKILL` and a separate recovery process reconstructing state only from the
  durable journal under the exclusive lock, resumable operation histories,
  coherent old/new recovery, and the invariant that no scenario activates two
  writers;
- exact fake canary registration and identity-bound cleanup.

The request, assignment, watcher, Stop, and teardown acceptance-receipt
extensions remain machine-readable `pending` entries. Request, assignment,
watcher delivery, Stop, and teardown now have repository-local implementations
and focused deterministic tests, but these slices deliberately do not rewrite
the already isolated acceptance receipt or claim a cutover canary. Codex,
Cursor, Pi, Herdr, and tmux remain `unverified`; consuming a synthetic payload
or fake endpoint is never reported as real-runtime support.

Focused and combined affected verification are:

```sh
make test-acceptance
make test-request-lifecycle
make test-runtime-lifecycle
make test-skill-contracts
make test-affected
```

The staged migration assertion follows `CURRENT_SCHEMA_VERSION`; after PR #31,
canonical request migrations are `[1,2,3]` and runtime lifecycle appends
checksummed v4 without changing the acceptance operation or sentinel contract.

The skill-contract suite uses only synthetic temporary custom roots and fake
capability profiles. The current machine inventory was audited separately in a
read-only command and reduced to the path-free, body-free receipt in
`docs/research/custom-skill-audit.json`. It does not install, synchronize, or
rewrite a skill. Release-to-installed parity and a real runtime remain #23
gates.

The generation switch in this harness is a model exercised beneath the
disposable namespace. It is not a global cutover command. Canonical cutover,
live import, real hook mutation, watcher replacement, installation, delivery,
and real-runtime canaries still require merged prerequisites plus separate
explicit authority.
