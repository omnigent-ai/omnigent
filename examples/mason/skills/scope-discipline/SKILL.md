---
name: scope-discipline
description: Freeze, fence, and report repair scope so discovery cannot become unauthorized work.
---
# Scope discipline
At acceptance, append a scope ledger and never rewrite it:
`scope_ledger: {task_id: <id>, approved_at: <sha>, approved_by: human,
in_scope: {claim_ids: [D-1], files_expected: [...]}, out_of_scope: [...],
budgets: {max_packages: N, max_claim_ids: M}, revisions: []}`. Claim IDs are
an enumerated set; every plan, package, contract, PR, and review cites it.

Scope may SHRINK freely. Scope may GROW only with explicit human approval.
Never add a claim, file, or acceptance criterion without a ledger revision.
Discovery is not a mandate: write out-of-contract problems to
`~/.mason/<task_id>/discovered.md` with claim id (if any), file:line, and evidence;
never fix them in the current worktree.

Every contract must contain `files_allowed: [...]` and optional
`files_forbidden: [...]`. Mechanically fence a worker with
`git -C <worktree> diff --name-only main...HEAD`, compare every path to the
allowed globs, and fail review automatically on any breach regardless of code
quality. Do not rename, restructure, clean up, upgrade dependencies, or format
untouched code. Inside-contract review issues block; outside-contract issues
are advisory and go to the parking file.

Bound convergence: one plan-review pass plus one resubmit; at most two
fix-task rounds per PR; after three failed gate runs, stop and ask the human.
Lead every human report with the scope delta (`SCOPE: approved ... current ...
parked ...`). Before any repair dispatch, verify every plan claim is in the
ledger, no package has an unapproved claim, and package count is within budget;
otherwise STOP and ask.
