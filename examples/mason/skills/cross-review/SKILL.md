---
name: cross-review
description: Independently review each package diff against every claim contract.
---
# Cross-vendor review
Select a vendor different from the implementer. Supply ONLY the PR diff and
package contract, never the implementer's worktree or transcript. Review each
claim separately: acceptance criterion, evidence, regression risk, migration
path, and required gates. Report blocking issues, non-blocking issues, and
suggestions with file:line evidence; never edit. Send blocking fixes to the
same implementer conversation, then repeat review until zero blockers. A bundle
must not pass because it is broadly plausible: every constituent claim gets a
checklist result.

Issues inside the contract are BLOCKING and return to the implementer. Issues
outside it are ADVISORY ONLY and go to the parking file; reviewers may not add
work. Allow at most two fix-task rounds per PR; after that escalate to the human.

Create the handoff with `git diff main...HEAD > /tmp/WP-01.diff` or `gh pr diff <number>`, and append the contract only. Require one row per claim: `CLAIM D-1: ACCEPTANCE [pass/fail], EVIDENCE [file:line], REGRESSION [pass/fail], GATES [pass/fail], DECISION [block/clear]`. Finish with `BLOCKERS: ...`, `NON_BLOCKING: ...`, and `SUGGESTIONS: ...`; a missing claim row is blocking.
