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

Concrete check: for 11 findings all saying “capability built, nothing reaches
it”, search whether the same missing registration/router/wiring edge explains
all 11 and whether one systemic change reaches every user path. If yes, make
one package with 11 checklist entries; if evidence shows independent wiring
layers or conflicting blast radii, split by root cause and order shared
prerequisites. Settle the choice by symbol/reference searches, call-graph
traces, original diffs, and targeted end-to-end tests.

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
