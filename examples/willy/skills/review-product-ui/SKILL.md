---
name: review-product-ui
description: Perform a source-level product UI preflight before publishing, then apply rendered feedback from the Artifacts workspace in later turns.
---

# Review product UI

Use this skill after the artifact exists and before its first publication. Willy
does not have a browser tool before publication; do not claim rendered or
interaction testing that did not occur.

## Review procedure

1. Read the complete HTML, CSS, and JavaScript artifact source. Trace primary
   navigation, interactive handlers, responsive rules, focus styles, and state
   transitions directly from the files.
2. Perform a source-level preflight in this order:
   - task clarity and information architecture
   - primary-action visibility and interaction feedback
   - dashboard/data comprehension and realistic states
   - alignment, spacing rhythm, typography, color, and component consistency
   - responsive behavior, keyboard access, focus visibility, and contrast
   - broken resource references and likely overflow, clipping, or layout shifts
3. Classify findings as blocking or non-blocking. A broken task flow, missing
   state, inaccessible control, unreadable data, console error, or visual defect
   in the primary viewport is blocking.
4. Fix every blocking issue found in source and repeat the preflight until no
   blocking source findings remain.
5. Call `publish_design_artifact` with the exact entry path, a concise title,
   and `operation="created"` or `operation="updated"`.

Do not publish a design merely because the files exist. Publishing signals that
the source preflight is complete and makes the artifact available for rendered
inspection in Omnigent's Artifacts workspace. When the user later supplies
element feedback, screenshots, or review diagnostics, treat that rendered
evidence as the next review round, update the same files, and republish.
