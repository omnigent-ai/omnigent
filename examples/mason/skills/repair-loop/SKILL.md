---
name: repair-loop
description: Execute package-scoped repairs with worktrees, review loops, and integration checks.
---
# Repair loop
After human approval, create exactly one worktree and branch per package and
send its contract to one implementer. Run packages only when their DAG edges
permit parallelism. The implementer runs gates and opens its own PR; scope
creep is forbidden. If the real cause escapes the package, stop it and re-plan.

Give the diff and contract to a different vendor, route blocking findings back
to the original implementer, and repeat until clean. Once every PR in a
dependency group is green alone, merge them only into a scratch integration
branch and run the full gates. Mason never merges the target branch.
