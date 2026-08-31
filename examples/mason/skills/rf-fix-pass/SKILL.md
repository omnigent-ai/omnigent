---
name: rf-fix-pass
description: Point mason at the RecordFlow quarantine fix-pass branch. Repo coordinates, the real gate commands, the project-name trap, the proof-of-fix protocol, and the local rules that override mason's generic defaults. Load this before any ledger, audit, or repair work in the rf repo.
---

# Pointing mason at the rf quarantine fix pass

This skill carries everything rf-specific. Mason's generic skills
(`claim-ledger`, `audit-rubric`, `work-package-planning`, `repair-loop`,
`cross-review`, `scope-discipline`) stay product-neutral. When they disagree with
this file about an rf detail, this file wins.

## Coordinates

    repo:   /home/dev/repos/rf
    branch: quarantine/fix-pass-20260831
    range:  ef93d951d..HEAD          (354 commits, 33 PRs, ~200 D-<number> claims)
    base:   ef93d951d is the last commit before the fix pass began

The branch was cut out of `integrate/platform` because the fixes proved
unreliable. **Assume no claim is true until you have read the evidence.**

Prior art you MUST read before starting, in this order:

1. `REVIEW-PROMPT.md` (root) — the human's review brief.
2. `FIX-PASS-RECORD.md` (root) — completed audit: 133 sampled claim instances,
   103 reachable, 27 that do NOT hold, 3 indeterminate, 22 confirmed findings
   with file, line, confidence and a "settle with" line.
3. `.claude/orchestrator/FIX-BRIEF-TEMPLATE.md` (440 lines) — the fix contract
   the original pass was supposed to follow. The richest source of prior art.
4. `.specify/memory/constitution.md` (1124 lines, 20 principles) — the authority.

Reuse prior judgements ONLY when the row names evidence AND nothing in the current
pass contradicts it. Reopen any known-wrong, uncertain, or unnamed-evidence row;
D-725 demonstrates why. The gap — roughly 175
unexamined claims — is where unknown failures live.

## Two rows are NOT defects — never "fix" them

- `D-461` — the one-member `recurrence` enum is a dated safety guard against a
  skipped billing period.
- `D-253` — the 39-versus-33 grant count gap is by design.

Reporting either as a problem is an error.

## LANDMINE: worktrees

Mason's default is one git worktree per package. In this repo that collides with
two local facts:

1. **`scripts/run-checks.sh --changed` exits 2 inside a nested worktree.** Inside
   a worktree, pass explicit project names instead:
   `scripts/run-checks.sh @rf/data @rf/records`.
2. **Never `git stash` in this repo.** The stash stack is shared across
   worktrees, so a stash in one worktree surfaces in another. Use a branch or a
   commit.

Verify worktree placement before dispatching any repair, and put the explicit
project list in every contract rather than relying on `--changed`.

## The project-name trap

Nx project names are INVERTED against directory names. `scripts/run-checks.sh`
carries a translation shim (lines 88-92) precisely because of this. A contract
that names the wrong one silently gates nothing:

    packages/contracts        ->  @rf/workflow-contracts     INVERTED PAIR
    packages/rf-contracts     ->  @rf/contracts              INVERTED PAIR
    packages/rf-lib           ->  @rf/records
    packages/rf-validation    ->  @rf/records-validation
    packages/rf-query         ->  @rf/records-query
    packages/workflows        ->  @rf/workflow-core
    packages/rf-workflow-sdk  ->  @rf/workflow
    packages/database         ->  @rf/platform-db
    packages/rf-data          ->  @rf/data
    packages/portal           ->  @rf/portal          NOT services/rf-portal
    services/rf-portal        ->  rf-portal  (pkg @rf/portal-service)
    services/rf-explorer-api  ->  @rf/records-api
    services/api-gateway      ->  @rf/workflow-api-gateway

The two-portal hazard (`packages/portal` vs `services/rf-portal`) is real and is
recorded in `FIX-PASS-RECORD.md`. Always state BOTH the directory and the project
name in a contract.

## Gates

Scoped, and mandatory after every repaired row:

    scripts/run-checks.sh <project>...     # explicit names (use inside worktrees)
    scripts/run-checks.sh --changed        # top-level checkout ONLY

Per project this runs build -> lint -> typecheck (two passes, including strict
`config/tsconfig.test.json`) -> test, and asserts that every project printed a
TEST COUNT. **No count line means the tests did not run** — treat a missing count
as a failed gate, not a pass.

Why the scoped gate is not optional: `FIX-BRIEF-TEMPLATE.md:201-225` records a
worker that ran per-file `vitest` plus a production build and believed itself
covered. `scripts/run-checks.sh rf-app` then found FIVE real failures including a
runtime crash. **A per-file vitest run does not typecheck at all.**

Root targets, if you need them (`package.json:13-38`):

    lint        oxlint --config oxlintrc.json packages/ apps/ services/ schemas/
    typecheck   bash scripts/typecheck.sh
    test        bash scripts/test.sh

Measured runtimes on this box: duplicate-types 2-3 s; monorepo lint 3-4 s (oxlint
is Rust); arch-tests ~35-41 s (326 tests); `pnpm run build` 216-466 s; CI's full
unit-test gate ~20 minutes. **Never put the full suite in a single-row contract.**

## Proof of fix — required, and stronger than mason's default

`FIX-BRIEF-TEMPLATE.md:320` requires more than a green gate:

1. Write a test that FAILS first.
2. Revert the fix, run with `--skip-nx-cache`, watch it go RED.
3. Restore the fix, run again, watch it go GREEN.
4. **Report both exit codes.**
5. Prove genericity — name a second product that exercises the path, or cite the
   architecture test that covers it.

A contract that does not demand the red-then-green transcript has not proven
anything. Mason must require it in every rf repair contract.

## Local rules that override mason's generic defaults

- **Fix the generic layer** (`FIX-BRIEF-TEMPLATE.md:13`). Repairs belong in
  `packages/` or `services/`. Never fix a per-product code path, and never patch
  the `portfolio-manager` template to hide a platform defect.
- **No backward compatibility, no defensive fallback**
  (`FIX-BRIEF-TEMPLATE.md:31`). No compatibility shim, no fallback branch, no
  guard that hides a wrong value. Fix the cause and let a wrong call fail.
  Constitution §XI (Zero Defensive Programming, lines 214-262) is the authority;
  its anti-pattern list at 930-948 names fallback values for impossible missing
  data, silent error recovery, log-and-continue on contract violations, optional
  chaining on required properties, and nullish coalescing on non-nullable types.
- **BUT — §XIV Boundary Validation (331-355) permits validation at boundaries**:
  DB rows, `JSON.parse`, third-party returns. Do NOT report a legitimate boundary
  check as a forbidden guard. This distinction is the most likely source of false
  positives against failure pattern 5; require the auditor to state which side of
  the boundary the code sits on.
- **§XVI Schema-Agnostic Rendering (365-372)** is the constitutional basis for
  failure pattern 4: product features MUST derive every entity, field and
  relationship name from the runtime template or metadata. A feature that names a
  specific entity or field is product-coupled and wrong.
- Edit files with Edit/Write only, never via a shell script (Constitution §XII).
- `integrate/platform` is PROTECTED. Topic branch plus PR, never a direct push.
- Selector priority in any UI evidence is accessible role/name/label, then a
  stable domain-semantic identifier. **Never DOM position** — no `first`, no
  `nth`, no positional CSS or XPath, no ancestor traversal.
- Network evidence must never capture headers, Authorization values, tokens or
  cookies.
- Keep flows UI-driven. No JSON, API, DB or debug bypass to create or prove
  product state.
- Reports use ASD-STE100 Simplified Technical English (`CLAUDE.md`). Exact
  strings — commands, paths, identifiers, log lines — are exempt.

## Searching this repo

`grep` runs with `-I` here, and a single NUL byte hides an entire file. **But
`git grep` alone is NOT sufficient: it also skips binary files by default.** More
than 20 tracked files under `.claude/` carry NUL bytes, including every
`speckit.*` command and several agent definitions — which is why the
orchestrator's own worker logs were nearly invisible to the first audit, and
those logs hold decisive evidence.

**Always pass `-a`. Any claim that something does not exist must be settled with
`git grep -a` or `awk`, never plain `grep` and never bare `git grep`.**

    git grep -an '<symbol>' -- 'packages/**' 'services/**' 'apps/**'
    git grep -an '<symbol>' -- '.claude/**'      # the logs: NUL-bearing
    git log --format='%H %s%n%b' ef93d951d..HEAD | awk '/D-[0-9]+/ {print}'

`.claude/orchestrator/logs/` (162 files) is prior art from the original fix pass
and repeatedly records what a worker could NOT reach. Search it before concluding
anything is unreachable.

## Where a user path lives

For failure pattern 1 (capability built, nothing reaches it), a "user path" in
this repo terminates in one of:

- a route registered in `config/route-ownership.mjs` — the canonical
  route -> owner -> auth map; start every reachability trace here
- a surface under `apps/` — `rf-app` (tenant admin, :5180),
  `rf-platform-portal` (template authoring, :5179), `rf-explorer`
- a product template under `testing/fixtures/products/`

A new symbol that no route, surface or template reaches is unreachable no matter
how green its unit test is.

## Known errors in FIX-PASS-RECORD.md — the record is evidence, not authority

A second audit found the record wrong in at least one place that would cause a
WRONG CLOSE. Treat every row as a claim to verify, not a fact.

- **`D-725` — the record mis-states the layer.** It says `contract_event` and
  `contract_document` have not enabled history. At the SYSTEM-ENTITY layer they
  HAVE: `CONTRACT_EVENT_TYPE` carries `history: { enabled: true }` at
  `packages/rf-data/src/system-entities/financial-definitions.ts:625`, and
  `CONTRACT_DOCUMENT_TYPE` at `:659`. There are TWO `history` flags in TWO
  layers; the one that is off is the **template** opt-in (see
  `FIX-PASS-PLAN.md:34-36` on `D-687`, `schemas.ts:3866-3891`). The finding
  survives, but a repairer sent to `financial-definitions.ts` would find the flag
  already `true`, conclude the finding is false, and close it wrongly.

When a finding names a flag, a column or a field, **confirm which LAYER it lives
in** before repairing. This platform has parallel declarations at the
system-entity layer and the template layer, and they disagree.

## Some findings are NOT code packages

`D-506`, `D-703` and `D-725` all resolve by authoring lines on the LIVE
`portfolio-manager` template, which is DATABASE content — no template file exists
in this repository (`.claude/orchestrator/logs/TASK-0153.md:116-118`, confirmed
via `git ls-files`). The code halves are already merged.

**These cannot be repaired from a git worktree and must not be packaged as a PR.**
They belong in a deploy-pass runbook. Packaging template-data work as a code PR is
how you produce a fake green: the PR merges, the gates pass, and the defect is
exactly as broken as before.

Mason must classify every finding as CODE, TEMPLATE-DATA, or PROCESS before
planning, and route the non-code ones out of the PR pipeline.

## The three indeterminate findings

`D-252`, `D-782`, `D-803` need live or template evidence. Route them to a worker
that can reach a running environment or the fixture templates, and state in the
contract exactly what evidence would settle each. Do not let them be closed on
static reasoning alone.
