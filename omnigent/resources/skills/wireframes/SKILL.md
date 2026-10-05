---
name: wireframes
description: Turn a product idea or flow the user describes into a clickable, responsive wireframe that the Omnigent wireframe viewer renders (a `*.wireframe.html` workspace file), using the workspace design kit or design system for fonts and tokens only.
---

# wireframes: clickable screens the viewer renders in device frames

Use when the user asks for a wireframe, mockup, screen flow, or UI sketch of
an app or site, rather than slides.

## Wireframe format

- The file name must end in `.wireframe.html`. Any other name opens in the
  plain HTML preview instead of the wireframe viewer.
- One self-contained HTML document. Each screen is a
  `<section data-screen="id" data-title="...">` that is a direct child of
  `<body>`: `id` is a short kebab-case name (`home`, `sign-in`), and
  `data-title` is what the screen picker shows. A file with no such sections
  is a single screen.
- The viewer shows one screen at a time, with a screen picker, inside a
  device frame: Desktop 1440x900, Tablet 834x1194, or Phone 390x844. The
  frame renders at that size and scrolls, so a screen may be taller than the
  frame. Do not fix the body to the viewport height or hide overflow.
- Link screens with `<a href="#id">` or a `data-goto` attribute
  (`data-goto="id"`) on any element: buttons, cards, list rows. The viewer switches to that screen inside the
  frame; a `#id` that is not a screen scrolls to that element. Do not write
  your own screen-switching script or handle navigation keys.
- Keep everything inline: CSS in `<style>`, images as `data:` URIs or simple
  placeholder boxes. The wireframe runs in a sandboxed iframe with no access
  to the app or to other workspace files, so relative links to other files
  do not load. Do not rely on external URLs either. The one exception is a
  design system's own files, referenced as `ds:` paths (see "Using a design
  system").
- Put screens in the static HTML; the picker lists the sections in the file.

## Responsive CSS

- Write mobile-first CSS that works at all three frame widths, with
  `@media (min-width: 768px)` and `@media (min-width: 1200px)` (or similar)
  for the tablet and desktop layouts. The frame's width drives the media
  queries, so switch devices to check each layout.
- Use flexible layouts (`flex`, `grid`, `minmax`, `max-width`) and relative
  units; avoid fixed pixel widths wider than 390px outside media queries.
- Include the viewport meta tag:
  `<meta name="viewport" content="width=device-width, initial-scale=1">`.

## Grayscale first

- Start in grayscale: neutral grays for surfaces, borders, and text, plain
  boxes for images and icons, and one system font stack. Wireframes are about
  structure, hierarchy, and flow, not visual polish.
- Use realistic but short placeholder copy and labels, not lorem ipsum, so
  the flow reads.
- Add brand color only where the user asks for it, or from a kit or design
  system as below, and then only for emphasis (primary actions, the active
  state).

## Design kit

Before writing, check for `kit.json` in `.omnigent/design-kit/`.

- The wireframe viewer applies only the kit's fonts and tokens; it does not
  enforce section backgrounds, text colors, or a logo on every screen, and it
  does not load the kit's `css` stylesheet. Style the screens yourself.
- Tokens: `--kit-primary`, `--kit-secondary`, `--kit-accent`,
  `--kit-background`, `--kit-text`, `--kit-font-heading`, `--kit-font-body`.
  Each exists only if the kit sets it, so give a grayscale fallback, e.g.
  `var(--kit-font-body, system-ui, sans-serif)` or `var(--kit-primary, #444)`.
- A "Design kit not applied: reason" notice means the kit could not be
  applied; report the reason to the user rather than restyling to match.

## Using a design system

When the request names a design system or the workspace has
`.omnigent/design-system.json`, load the `design-systems` skill and follow
it before writing; it replaces the design kit for the wireframe. Its
templates are often slide layouts: take tokens, fonts, and components from
the system, but keep the wireframe's own screen layout.

## Workflow

1. List the screens and the links between them from the user's description.
   If the flow is unclear, confirm the screen list with the user first.
2. Check for a design system, else the design kit, as above.
3. Write the file incrementally, so a live preview can show it as it grows:
   first write a complete document with only the first screen, then add one
   complete top-level screen `<section>` per edit. Every edit leaves a whole,
   valid document; never leave unclosed tags between edits.
4. Tell the user the file path and how to view it: open it from the Files
   panel, switch screens with the picker or by clicking links, and switch
   devices to check each layout.

## If you delegate the wireframe to a sub-agent

A worktree does not contain the kit (`.omnigent/` is gitignored), so give the
sub-agent absolute paths: the workspace root, the output path (e.g.
`<root>/wireframes/<slug>.wireframe.html`), and the kit folder
`<root>/.omnigent/design-kit/`. Tell it to write only that file, with no
commit, push, or PR.
