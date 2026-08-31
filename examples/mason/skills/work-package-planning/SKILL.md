---
name: work-package-planning
description: Synthesize all audit findings into coherent, dependency-ordered repair contracts.
---
# Work-package planning
Reason over the complete findings set. Roll up root causes and list every claim
closed by each package. Derive touched files from evidence plus each claiming
commit's diff; overlapping sets share a package or acquire a strict DAG edge.
Separate migration/schema and application layers unless inseparable, and give
migrations an existing-data/additive lens. Emit edges (helper→callers,
migration→dependent code), parallel groups, and a clear per-claim acceptance
criterion. Split unrelated subsystems or contracts a reviewer cannot hold in
mind; size for coherence, not finding count.

Read commit subjects before auditing: `git log --format='%h %s%n%b' <range> |
grep -inE 'not fixed|not started|piece [0-9]|step[s]? [0-9]|partial|out of scope'`.
For each finding apply the destination test: compare `git show --stat
<claiming-commit>` with the directory named by the finding's evidence. If the
commit never entered the consumer directory, classify it as MISSING LAYER
TRAVERSAL and cluster on that traversal, not the symptom. Then refute the
rollup: findings roll up ONLY when they share a symbol, file, helper, or test.
Different destinations with nothing shared means the rollup is fake.

Classify every finding before planning: CODE (worktree and PR), TEMPLATE-DATA
(live template/config content; cannot be repaired from a worktree and must not
become a PR; route to a deploy-pass runbook), or PROCESS (repair with a gate,
never a patch). TEMPLATE-DATA as CODE manufactures a fake green; PROCESS as
CODE does the same at a deeper level.

Worked evidence: eleven of twenty-two confirmed findings shared “capability
built, nothing reaches it”. Verdict: FOUR rolled up into TWO packages and SEVEN
stayed independent. There was no systemic gap in the CODE; there was one in the
PROCESS. Conflating a process cause with a code cause produces one plausible-
sounding grand package that cannot be implemented, cannot be reviewed, and
burns the whole budget before anyone notices.

Accepted Rollup A is `D-211 + D-361 + D-681 + D-805`, one file: the authoring
surface. `packages/contracts/src/portal-scope-operators.ts:31` declares the
operators and `:43-48` requires the typed authored form; `packages/rf-contracts/
src/portal/scope-compiler.ts:67-68` rejects the prefix and `:251-267` evaluates
correctly; `apps/rf-platform-portal/.../ActorTypesEditor.tsx:42` offers none and
`:49-55` emits the rejected prefix. None of those claiming commits touched the
portal app, and the register says “D-805 ... Blocks D-681”. Shared file, symbol,
and test make this genuine.

Accepted Rollup B is `D-506 + D-703 + D-725`, but it is not a code package:
author lines on the live `portfolio-manager` template. `git ls-files` confirms
no template file exists. Route it to a deploy-pass runbook, never a PR.

The seven independent destinations include `apps/rf-app` (three),
`apps/rf-platform-portal` (two), `apps/query-builder`, `services/rf-explorer-api`
(two), `packages/workflows`, and live template data. They share no symbol,
file, helper, or test. `D-288` is a second implementation: the fix is in
`packages/rf-query` and `rf-ui-components`, while `apps/query-builder/queryTransform.ts:109`
downgrades `joinType === 'full'` to `left` and
`tests/queryTransform.test.ts:124` asserts `left`—a test asserting the defect.
`D-242` is a type mismatch: `asset_record_type` is varchar but the resolver
needs a sibling enum of real entity keys; the picker already exists. Trace the
mechanism, not the symptom.

Contract template: package/root cause; claim IDs; files; dependencies; change
boundary; acceptance criterion and settling evidence per claim; tests/lint/
typecheck gates; migration/backfill obligations; reviewer checklist.

Derive blast radius with `git show --stat --oneline <claiming-commit>` and `git show <claiming-commit> --`; union those paths with evidence paths. Fill: `PACKAGE: WP-01\nROOT_CAUSE: ...\nCLAIMS: [D-1, D-2]\nFILES: [...]\nfiles_allowed: [src/foo.py, tests/test_foo.py]\nfiles_forbidden: [...]\nDEPENDS_ON: [...]\n`, then add `- D-1: ACCEPTANCE=... EVIDENCE=...` for every claim and finish with `GATES: [pytest ..., ruff ..., pyrefly ...]` and `REVIEW_CHECKLIST: ...`.

The plan reviewer receives exactly one plan, findings, and rubric. Its contract is:
### What the plan reviewer RECEIVES
- the plan artifact (packages, claim->package mapping, files, DAG edges, rationale)
- the consolidated findings list the plan was built from
- the audit rubric
It does NOT get an open brief to explore the repository for new problems.
### What it MAY do
- object to how findings are grouped (wrong rollup, or a rollup that should be split)
- object to package sizing (too big to review honestly / too small to be coherent)
- object to missing or wrong DAG edges (ordering, collisions)
- object to layer mixing (migration bundled with application logic)
- object to a package whose contract has no clear acceptance criterion per finding
### What it MAY NOT do
- add new findings or defects
- propose new fixes, refactors, or adjacent improvements
- widen any package's acceptance criteria
- recommend work not already traceable to an existing confirmed finding
If it believes a finding is missing, that is recorded as a NOTE FOR THE NEXT AUDIT
CYCLE. It never becomes a plan change in this pass.
### Bounding rules
- Exactly ONE pass. No iterate-until-happy loop.
- Verdict is one of: ACCEPT / ACCEPT-WITH-EDITS / REJECT, with each objection tied to
  a specific package id.
- Objections are ranked and capped (top N blocking, top N advisory) so the output
  cannot become an unbounded backlog.
- NET-SCOPE RULE: a plan revision may reduce, merge, split, or reorder scope. It may
  NOT increase the total set of claim ids under repair. Increasing total scope
  requires explicit human approval, not reviewer say-so.
- On REJECT, mason revises the artifact and may re-submit ONCE. A second REJECT
  escalates to the human rather than triggering a third pass.
