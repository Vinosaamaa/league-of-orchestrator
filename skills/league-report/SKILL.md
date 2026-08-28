---
name: league-report
description: Answer League activity, completion, recurring-repair, and end-of-day questions from bounded evidence-backed League reports. Use for requests such as what happened today, whether everything is finished, or what each Champion did.
---

# League Report

Use the repository's public `league report` command as the only source of
League facts. Never query SQLite, scan harness transcripts, inspect live
multiplexer state, or infer missing lifecycle evidence.

Choose one exact timezone and one scope: owner, Squad, project, or all. Use an
explicit from/to range when supplied; otherwise use `--today`. Use
`--since-report` for an incremental update and `league report show` to
reproduce an immutable stored specification.

Lead with `completion.everything_finished`. Explain every false or unknown
gate, then summarize chronological activity, owner groups, recurring repairs,
and explicit evidence gaps. Preserve `unknown` and `unverified`; a terminal
task state, green check, merge, installation, deployment, smoke, resource
release, or teardown never proves another stage.

Keep pagination visible. Follow `pagination.next_cursor` until the requested
scope is covered or state clearly that the answer is truncated. Do not group
repairs by wording; use only the stable repair identifiers in the report.

Default to JSON or Markdown. Generate HTML and open it in a new Agent Chrome
tab only when the user explicitly asks for a visual report. The report data
must still come only from `league report`; the browser step only displays the
portable HTML and must preserve every existing review tab. Never share or
publish the HTML without separate authority.
