---
name: claim-ledger
description: Enumerate configurable claims, subtract judged claims, and maintain an auditable queue.
---
# Claim ledger
Use `git log --format=%H -- <range>` and inspect each commit body and diff. Extract
distinct identifiers with the configured regex (default `D-[0-9]+`), never a
literal hard-coded ID list. Inspect PR bodies when supplied. Load the existing
record, subtract only claims with an explicit judgement and evidence, then
persist queue, source commit, pattern, and timestamp in the record.

For absence claims, do not trust `grep`: `grep -I` can skip a file containing
one NUL byte and hide the whole file. Use `git grep` or `awk` and record the
command, commit, and paths searched. Keep claim, commit, status, evidence,
confidence, and settling-test fields separate; never mark a claim judged from
git status alone.
