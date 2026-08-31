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

Commands: `git worktree add ../mason-wt-WP-01 -b mason/WP-01 <base>`; `git -C ../mason-wt-WP-01 push -u origin mason/WP-01`; `gh pr create --base <target> --head mason/WP-01 --title 'WP-01: ...' --body-file contract.md`. For integration run `git switch -c mason/integration-<group> <base>`, `git merge --no-ff mason/WP-01 mason/WP-02`, then the exact test, lint, and typecheck gates.
Before review, run `git -C <worktree> diff --name-only main...HEAD` and compare every path with `files_allowed`; any out-of-fence path is an automatic review failure regardless of code quality. Bound the loop: at most two fix-task rounds per PR; after three failed gate runs, stop and ask the human.
