---
name: design-product-ui
description: Design and implement polished product web applications and dashboards as editable HTML artifacts.
---

# Design product UI

Use this skill before creating or substantially changing a product application
or dashboard artifact.

## Workflow

1. Translate the request into the primary user, job, key actions, information
   hierarchy, and required states. Ask only for missing information that would
   materially change the interaction model.
2. Choose one clear visual direction. Define a compact token set for color,
   typography, spacing, radii, borders, and elevation before styling individual
   components.
3. Build the artifact under `artifacts/` using semantic HTML and relative local
   CSS, JavaScript, images, fonts, and data. Prefer a multi-file directory when
   the design has reusable styles, behavior, or assets.
4. Design the whole product state, not a marketing screenshot. Include the
   navigation, page hierarchy, realistic data, primary actions, empty/loading/
   error states where relevant, and responsive behavior.
5. Make interactions functional. Controls must have visible hover, focus,
   active, selected, and disabled states; keyboard navigation must remain
   usable; charts and dense tables must have readable labels and summaries.
6. Render the entry HTML in a browser and inspect at desktop and narrow widths.
   Fix overflow, clipping, weak contrast, inconsistent alignment, and broken
   relative resources before review.

## Product UI principles

- Lead with the user's task and current state, not decorative hero copy.
- Use hierarchy and whitespace before borders and shadows.
- Keep navigation, filters, actions, and content in predictable regions.
- Prefer a small set of composable components over one-off card treatments.
- Make dashboards explain what changed, why it matters, and what action follows.
- Use restrained motion only when it clarifies state or spatial continuity.
- Avoid generic AI styling: excessive gradients, glass effects, floating cards,
  pill-shaped everything, oversized headings, and ornamental charts.

## Output

Leave the artifact as working files under `artifacts/`. Do not publish it yet;
the `review-product-ui` skill must run first.
