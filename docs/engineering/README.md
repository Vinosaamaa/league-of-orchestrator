# Engineering records

Every pull request selects one Engineering impact classification and commits
its matching `docs/engineering/changes/pr-<number>.md` receipt. Material changes
also reference an exact authored record revision in `docs/engineering/records`.

Open a draft PR to obtain its number, then run
`python3 scripts/new-engineering-receipt.py --help` and author the receipt.
Choose `None` with a concrete reason only for changes that do not warrant a
rich Engineering record. The gate verifies repository, PR number, title,
classification, public eligibility and exact record references.

`Engineering impact` runs on source and PR metadata updates as an independent
Ubuntu check. It must be required on `main`; workflow presence alone does not
enforce merges. Existing runtime tests remain owned by `make test`.

The canonical schemas are shared with Interview Arc. Interview Arc consumes
reviewed League records through an exact trusted-source commit pin. Adding a
record here does not itself refresh that pin or publish historical content.
Historical backfills retain the shared explicit publication contract.
