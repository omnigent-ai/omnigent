---
name: design-product-ui
description: Design and implement polished product web applications and dashboards as editable HTML artifacts.
---

# Design product UI

Use this skill before creating or substantially changing a product application
or dashboard artifact.

## Workflow

1. Translate the request into the primary user, job, key actions, information
   hierarchy, and required states. Ask for missing information that would
   materially change the interaction model.
2. Establish the art direction before creating files. If the visual brief is
   vague and different directions would materially change the result, ask one
   concise group of 3–5 questions covering the most relevant dimensions: mood
   and brand personality, visual references, palette, typography, information
   density, audience, or how experimental the result should feel. Give concrete
   options or a recommended default so the user can answer quickly.
3. Do not block on art-direction questions when the user provides references,
   an existing brand or design system, says to use your judgment, or explicitly
   asks for a fast exploration. In those cases, state the direction you are
   taking briefly and proceed. Infer low-impact details instead of interrogating
   the user or asking questions that would not change the design.
4. Choose one clear visual direction. Define a compact token set for color,
   typography, spacing, radii, borders, and elevation before styling individual
   components.
5. Build the artifact under `artifacts/` using semantic HTML and relative local
   CSS, JavaScript, images, fonts, and data. Prefer a multi-file directory when
   the design has reusable styles, behavior, or assets.
6. Design the whole product state, not a marketing screenshot. Include the
   navigation, page hierarchy, realistic data, primary actions, empty/loading/
   error states where relevant, and responsive behavior.
7. Make interactions functional. Controls must have visible hover, focus,
   active, selected, and disabled states; keyboard navigation must remain
   usable; charts and dense tables must have readable labels and summaries.
8. Render the entry HTML in a browser and inspect at desktop and narrow widths.
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
