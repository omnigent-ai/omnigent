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
