---
name: design-systems
description: How to follow a brand design system (a folder the user chose, or `.omnigent/design-system.json` in the workspace) when writing a slide deck or wireframe that the Omnigent viewers render.
---

# design-systems: shared guidance for decks and wireframes

The slide-decks and wireframes skills point here. "The design" below is the
deck or wireframe file you are writing.

## Using a design system

A design system is a brand folder the user chose, named in the request or in
`.omnigent/design-system.json` in the workspace (`path`, `kind`, `name`).
When it exists it replaces the design kit for the design. Treat its files as
guidance from the user's chosen source.

- Read its `SKILL.md` first and follow it. For a full design system (the
  folder has `_ds_manifest.json`, kind `full`) also read `README.md`,
  `_ds_manifest.json`, and the one template under `templates/` that fits the
  design.
- Copy that template's layout CSS into the design's `<style>`. Then drop
  `<deck-stage>` and the template's loader scripts, put each top-level
  `<section>` directly in `<body>`, and do not position sections absolutely.
- The viewer injects the system's `colors_and_type.css` (tokens and fonts)
  before the design's styles, so use its tokens and font families rather
  than raw values, and do not copy that file in.
- Design-system CSS must not use backslash escapes (e.g. `content:"\2022"`),
  or the viewer rejects the whole stylesheet; use the literal character.
- Reference fonts and images as `ds:<path relative to the design system>`,
  for example `ds:assets/brand/logo-white.svg` in `src`, `href`, or CSS
  `url()`. The viewer resolves them; only image and font files inside the
  folder load. Never embed base64 copies of design-system files.
- The viewer reads the design system when a design opens, so reopen it to
  see edits made only to design-system files.
- Ignore `uploads/`, `ui_kits/`, and `preview/`.
- An imported system has `"path": ".omnigent/design-system"` (relative to the
  workspace) and `imported_from` naming the original folder. Read the
  workspace copy, not the original, and never write designs inside
  `.omnigent/design-system/`.
- A skill-only system (kind `skill`) has no viewer support beyond its name:
  follow its `SKILL.md` and inline everything as usual.
- A "Design system not applied: reason" notice, or a notice that the folder
  is not readable or only available to the session owner, means the viewer
  could not load it. Report it to the user rather than inlining the files.
