---
name: slide-decks
description: Turn context the user provides into a slide deck that the Omnigent slides viewer renders (a `*.slides.html` workspace file), following the workspace design kit when one exists.
---

# slide-decks: decks the viewer renders on brand

Use when the user asks for slides, a deck, or a presentation built from
context they gave you (notes, a doc, a PR, a conversation).

Writing a deck is writing HTML and CSS, so polly delegates it: dispatch an
`implement` sub-agent and paste the "Deck format" and "Design kit" sections
below into its contract. The deliverable is the workspace file, not a PR,
unless the user asks to commit the deck to a repo.

## Deck format

- The file name must end in `.slides.html` (for example
  `decks/q3-review.slides.html`). Any other name opens in the plain HTML
  preview instead of the deck viewer.
- One self-contained HTML document. Each slide is a `<section>` that is a
  direct child of `<body>`. Nested sections are not slides.
- Design each slide for a 16:9 stage (1280x720 works well). The viewer scales
  it to the panel, letterboxes it on phones, and prints one landscape page per
  slide.
- Keep everything inline: CSS in `<style>`, images as `data:` URIs. The deck
  runs in a sandboxed iframe with no access to the app or to other workspace
  files, so relative links to other files do not load. Do not rely on
  external URLs either.
- Do not handle arrow keys or build your own navigation; the viewer owns
  previous/next, the counter, fullscreen, and print.
- Slides may be added by script, but only before the page finishes loading;
  the counter does not pick up slides added later.

## Design kit

Before writing, check for `.omnigent/design-kit/kit.json` at the workspace
root. If it exists, read it and the stylesheet named in its `css` field.

- The viewer enforces the kit: section background and text color, heading and
  body fonts, and the logo on every slide come from the kit, whatever the deck
  says. Do not set those base styles and do not add your own logo.
- Use the kit tokens for everything else: `var(--kit-primary)`,
  `var(--kit-secondary)`, `var(--kit-accent)`, `var(--kit-background)`,
  `var(--kit-text)`, `var(--kit-font-heading)`, `var(--kit-font-body)`.
- Use the layout classes the kit stylesheet defines on each `<section>` (the
  sample kit has `layout-title`, `layout-two-col`, and the `accent` text
  class). Do not invent layout classes the kit does not define.
- No kit: style the deck yourself with a restrained palette, and tell the user
  they can brand future decks by copying `examples/design-kits/sample` to
  `.omnigent/design-kit/` and editing it.
- A "Design kit not applied: reason" notice means `kit.json` is invalid. Fix
  the kit, not the deck. The viewer reads the kit only when a deck opens, so
  reopen the deck after editing the kit.

## Workflow

1. Outline from the user's context: a title slide, about one slide per key
   point, and a closing slide. If the context is thin, confirm the outline
   with the user before building.
2. Check for the design kit as above.
3. Write the deck file.
4. Tell the user the file path and how to view it: open it from the Files
   panel, step through with the arrow keys, and use "Print / Save as PDF" to
   export.
