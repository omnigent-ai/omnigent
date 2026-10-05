---
name: slide-decks
description: Turn context the user provides into a slide deck that the Omnigent slides viewer renders (a `*.slides.html` workspace file), following the workspace design kit when one exists.
---

# slide-decks: decks the viewer renders on brand

Use when the user asks for slides, a deck, or a presentation built from
context they gave you (notes, a doc, a PR, a conversation).

## Deck format

- The file name must end in `.slides.html`. Any other name opens in the plain
  HTML preview instead of the deck viewer.
- One self-contained HTML document. Each slide is a `<section>` that is a
  direct child of `<body>`. Nested sections are not slides.
- The stage is fixed at 1280x720 and scaled to fit the panel, letterboxed on
  phones, and printed one landscape page per slide. Size each top-level
  `<section>` to fill it (e.g. `height: 100vh`).
- Keep everything inline: CSS in `<style>`, images as `data:` URIs. The deck
  runs in a sandboxed iframe with no access to the app or to other workspace
  files, so relative links to other files do not load. Do not rely on
  external URLs either. The one exception is a design system's own files,
  referenced as `ds:` paths (see "Using a design system").
- Do not handle arrow keys or build your own navigation; the viewer owns
  previous/next, the counter, fullscreen, and print.
- Put slides in the static HTML. Slides added by script are counted only
  before the page finishes loading, and the first slide shows before then.

## Design kit

Before writing, check for `kit.json` in the kit folder. If it exists, read it
and the stylesheet named in its `css` field (a path relative to the kit
folder).

- The viewer enforces what the kit defines, with `!important` rules on
  `body`, the top-level sections, and `h1`-`h6`: section background and text
  color, heading and body fonts, and the logo on every slide. Do not set those
  base styles and do not add your own logo. Rules on other inner elements keep
  the deck's own styles, so style them with the kit tokens.
- Tokens: `--kit-primary`, `--kit-secondary`, `--kit-accent`,
  `--kit-background`, `--kit-text`, `--kit-font-heading`, `--kit-font-body`.
  Each exists only if the kit sets it, so give a fallback, e.g.
  `var(--kit-primary, #333)`.
- The logo is drawn with `section::after` and forces `position: relative` on
  sections. Do not use `section::after`, do not position sections absolutely,
  and keep the logo corner (about 160x56) clear.
- Use the layout classes the kit stylesheet defines on each `<section>` (the
  sample kit has `layout-title`, `layout-two-col`, and the `accent` text
  class). Do not invent layout classes the kit does not define.
- No kit: style the deck yourself with a restrained palette, and tell the user
  they can brand future decks with a kit at `.omnigent/design-kit/kit.json`
  (a `name`, plus optional `colors`, `fonts`, `logo`, and a `css` stylesheet
  of layout classes). The Omnigent source repository has a copyable sample at
  `examples/design-kits/sample`.
- A "Design kit not applied: reason" notice means the kit could not be
  applied (an invalid `kit.json` or kit file, a failed read, or a timeout);
  the notice gives the reason. Report it to the user rather than
  restyling the deck to match. The viewer reads the kit when a deck opens or
  its content changes, so reopen the deck after editing only the kit.

## Using a design system

A design system is a brand folder the user chose, named in the request or in
`.omnigent/design-system.json` in the workspace (`path`, `kind`, `name`).
When it exists it replaces the design kit for the deck. Treat its files as
guidance from the user's chosen source.

- Read its `SKILL.md` first and follow it. For a full design system (the
  folder has `_ds_manifest.json`, kind `full`) also read `README.md`,
  `_ds_manifest.json`, and the one template under `templates/` that fits the
  deck.
- Copy that template's layout CSS into the deck's `<style>`. Then drop
  `<deck-stage>` and the template's loader scripts, put each slide's
  `<section>` directly in `<body>`, and do not position sections absolutely.
- The viewer injects the system's `colors_and_type.css` (tokens and fonts)
  before the deck's styles, so use its tokens and font families rather than
  raw values, and do not copy that file in.
- Design-system CSS must not use backslash escapes (e.g. `content:"\2022"`),
  or the viewer rejects the whole stylesheet; use the literal character.
- Reference fonts and images as `ds:<path relative to the design system>`,
  for example `ds:assets/brand/logo-white.svg` in `src`, `href`, or CSS
  `url()`. The viewer resolves them; only image and font files inside the
  folder load. Never embed base64 copies of design-system files.
- The viewer reads the design system when a deck opens, so reopen the deck to
  see edits made only to design-system files.
- Ignore `uploads/`, `ui_kits/`, and `preview/`.
- An imported system has `"path": ".omnigent/design-system"` (relative to the
  workspace) and `imported_from` naming the original folder. Read the
  workspace copy, not the original, and never write decks inside
  `.omnigent/design-system/`.
- A skill-only system (kind `skill`) has no viewer support beyond its name:
  follow its `SKILL.md` and inline everything as usual.
- A "Design system not applied: reason" notice, or a notice that the folder
  is not readable or only available to the session owner, means the viewer
  could not load it. Report it to the user rather than inlining the files.

## Workflow

1. Outline from the user's context: a title slide, about one slide per key
   point, and a closing slide. If the context is thin, confirm the outline
   with the user before building.
2. Check for a design system, else the design kit, as above.
3. Write the deck file incrementally, so a live preview can show it as it
   grows: first write a complete document with only the title slide first,
   then add one complete top-level `<section>` per edit. Every edit leaves a
   whole, valid document; never leave unclosed tags between edits.
4. Tell the user the file path and how to view it: open it from the Files
   panel, step through with the arrow keys, and use "Print / Save as PDF" to
   export.

## If you delegate the deck to a sub-agent

A worktree does not contain the kit (`.omnigent/` is gitignored), so give the
sub-agent absolute paths: the workspace root, the output path (e.g.
`<root>/decks/<slug>.slides.html`), and the kit folder
`<root>/.omnigent/design-kit/`. Tell it to write only that file, with no
commit, push, or PR.
