---
name: rf-repo
description: Gate and repository guidance for work in the RecordFlow repository.
---
# RecordFlow repository guidance

Use this skill for work in the regular RecordFlow repository. It does not
apply to a quarantine branch.

## Gates

Run the relevant project gates explicitly:

    scripts/run-checks.sh <project>
    scripts/run-checks.sh --changed

`--changed` is for the top-level checkout only. The script runs build, lint,
typecheck (including strict `config/tsconfig.test.json`), and tests. Every
project must print a TEST COUNT. No count line means the tests did not run and
the gate failed. A per-file vitest run does not typecheck at all.

## Worktrees and project names

Inside a nested worktree, `--changed` exits 2. Name projects explicitly.
Never use `git stash`: the stash stack is shared across worktrees.

Nx project names are inverted against directory names:

    packages/contracts       -> @rf/workflow-contracts
    packages/rf-contracts    -> @rf/contracts
    packages/rf-lib          -> @rf/records
    packages/rf-validation   -> @rf/records-validation
    packages/rf-query        -> @rf/records-query
    packages/workflows       -> @rf/workflow-core
    packages/rf-workflow-sdk -> @rf/workflow
    packages/database        -> @rf/platform-db
    packages/rf-data         -> @rf/data
    packages/portal          -> @rf/portal
    services/rf-portal       -> rf-portal
    services/rf-explorer-api -> @rf/records-api
    services/api-gateway     -> @rf/workflow-api-gateway

Always name both the directory and project in a contract, especially for the
two portal locations.

## Repository rules

- Fix the generic layer under `packages/` or `services/`, never a per-product
  path.
- `integrate/platform` is protected: use a topic branch and PR.
- Constitution §XI Zero Defensive Programming forbids a fallback, swallowed
  error, or guard that hides a wrong value.
- Constitution §XIV permits validation at boundaries such as DB rows,
  `JSON.parse`, and third-party returns. Do not mistake a valid boundary check
  for a forbidden guard.
- Constitution §XVI requires schema-agnostic rendering: derive entity and
  field names from the runtime template; never hard-code them.

## Proof of fix

1. Write a failing test first.
2. Revert the fix and run it with `--skip-nx-cache`; watch it go RED and
   record the exit code.
3. Restore the fix and run it again; watch it go GREEN and record the exit
   code.
4. Report BOTH exit codes.

For every absence claim, `git grep -a` is mandatory; plain `grep` uses `-I`,
and NUL bytes can hide whole files. Edit files with Edit/Write, never through a
shell script.
