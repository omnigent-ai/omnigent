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
