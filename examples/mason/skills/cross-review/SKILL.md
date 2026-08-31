---
name: cross-review
description: Review a work-item diff against its contract from another vendor.
---
# Cross-vendor review

Select a vendor different from the implementer's. Supply ONLY the work-item
diff and contract, never the implementer's worktree or transcript. Check every
acceptance item, evidence requirement, regression risk, and required gate.
Report BLOCKING issues, NON-BLOCKING issues, and SUGGESTIONS separately, each
with file:line evidence; never edit.

Issues inside the contract are BLOCKING and return to the same implementer.
Problems outside the contract are reported as `DISCOVERED — OUT OF SCOPE` and
do not add work. Allow at most two fix-task rounds per work item; after three
failed gate runs, escalate to the human.

Create the handoff with `git diff <base>...HEAD` and append the contract only.
Require an explicit result for every acceptance item and finish with
`BLOCKERS: ...`, `NON_BLOCKING: ...`, and `SUGGESTIONS: ...`.
