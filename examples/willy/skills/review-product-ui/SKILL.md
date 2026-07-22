---
name: review-product-ui
description: Review a product web application or dashboard artifact and fix usability, hierarchy, accessibility, and visual-quality issues before publishing.
---

# Review product UI

Use this skill after the artifact exists and before publishing it.

## Review procedure

1. Open the artifact in a browser at desktop and narrow widths. Exercise its
   primary navigation and interactions rather than judging source code alone.
2. Review the result in this order:
   - task clarity and information architecture
   - primary-action visibility and interaction feedback
   - dashboard/data comprehension and realistic states
   - alignment, spacing rhythm, typography, color, and component consistency
   - responsive behavior, keyboard access, focus visibility, and contrast
   - broken resources, console errors, overflow, clipping, and layout shifts
3. Classify findings as blocking or non-blocking. A broken task flow, missing
   state, inaccessible control, unreadable data, console error, or visual defect
   in the primary viewport is blocking.
4. Fix every blocking issue in the artifact files. Re-render and repeat the
   review until no blocking findings remain.
5. Call `publish_design_artifact` with the exact entry path, a concise title,
   and `operation="created"` or `operation="updated"`.

Do not publish a design merely because the files exist. Publishing is the
explicit signal that browser review and required fixes are complete.

