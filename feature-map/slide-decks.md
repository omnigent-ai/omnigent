# Slide decks

A file named `*.slides.html` (a self-contained HTML document with one top-level
section per slide) opens in a slide-deck viewer instead of the plain HTML
preview. The deck runs in the same sandboxed preview as other HTML files, shows
one slide at a time on a 16:9 stage scaled to the panel, and offers previous and
next controls, a slide counter, keyboard navigation, fullscreen, printing every
slide to PDF, and a Source toggle back to the code view.

## Sub-features

- `deck-open`: opening a `*.slides.html` file shows the first slide and a
  `1 / total` counter. Other `.html` files keep the plain HTML preview.
- `deck-navigate`: previous and next buttons, and ArrowLeft, ArrowRight, PageUp,
  and PageDown while the viewer is focused, step through slides. Previous is
  disabled on the first slide and next on the last.
- `deck-fullscreen`: the fullscreen toggle fills the screen with the deck. It is
  hidden when the browser has no Fullscreen API.
- `deck-print`: "Print / Save as PDF" opens the print dialog with every slide on
  its own landscape page.
- `deck-source`: the Source button switches to the existing code view; the
  toolbar's "View preview" returns to the deck.
- `deck-empty`: a deck with no top-level sections shows a "No slides yet" empty
  state.
- `deck-design-kit`: a brand kit at `.omnigent/design-kit/kit.json` in the
  workspace (colors, fonts, logo, layout stylesheet) brands every deck, even one
  whose author ignored it. The toolbar shows the kit name; an invalid kit shows
  a "Design kit not applied: reason" notice and the deck renders unbranded.
  Format and a copyable sample: `examples/design-kits/sample/README.md`.

## How to get to it (user POV)

**Desktop web and desktop app:** open a `*.slides.html` file from the Files
panel or a file link; it opens in the right-rail file viewer.

**Phone (web or mobile app):** open the same file from the Files panel; it
opens in the full-screen file viewer.

**Design kit (any surface):** copy `examples/design-kits/sample` to
`.omnigent/design-kit` at the workspace root (or have the agent write a kit
there), then open or reopen a deck.

## Driving it with the repro environment

Preconditions: a running instance (`verify-env start`, then `verify-env
doctor`) and the built web UI.

- Desktop deck open, navigation, and Source:
  `tests/e2e_ui/files/test_slides_viewer.py::test_slides_viewer_steps_through_deck`
  seeds a 3-slide deck and steps through it.
- Component states (counter, keys, empty deck, Source, fullscreen visibility,
  print message): `cd web && pnpm exec vitest run src/shell/SlidesViewer.test.tsx`.
- Phone: no test. With a phone-sized viewport, open a deck from the Files panel
  and check that the stage is letterboxed to the screen width and the
  controls step through slides.
- Print: no test (the print dialog is native). Choose "Print / Save as PDF" and
  check the preview shows one landscape page per slide.
- Design kit applied over a deck's own colors, fonts, and layout class:
  `tests/e2e_ui/files/test_slides_viewer.py::test_slides_viewer_applies_design_kit`
  seeds the sample kit and checks computed styles inside the deck iframe.
  Parsing, validation, injection, and the toolbar name and notice:
  `cd web && pnpm exec vitest run src/shell/SlidesViewer.test.tsx`.
- Design kit by hand: with the sample kit copied in, a deck shows the kit's
  background, fonts, and logo in the bottom-right corner of every slide, in
  fullscreen, and in the print preview. Change a color in `kit.json` to
  `"red;}"` and reopen the deck to see the notice.

## Gotchas

- Only `*.slides.html` routes to the deck viewer; `slides.html` or `.htm`
  files use the plain HTML preview.
- Only sections that are direct children of the body count as slides.
- A literal `</body>` in page text or `<noscript>` can shift where the viewer's
  script is injected, and the counter may change once the deck loads.
- The toolbar's "View source" and the deck's "View deck source" both reach the
  same code view.
- Text-selection comments are available in the plain HTML preview, not in the
  deck viewer.
- The kit loads when a deck opens or its content refreshes; editing only the
  kit needs the deck reopened.
- Kit base rules use `!important` on the body, top-level sections, and
  headings. Deck rules aimed at other inner elements keep their own font, and
  kit layout classes that change a section's background or color need
  `!important` too (see the sample `layouts.css`).
- The kit forces `position: relative` on sections when it has a logo, and the
  logo uses the section's `::after`.
- Asset paths must be plain relative paths inside the kit folder (letters,
  digits, `_`, `-`, `.`, `/`; no `..`, spaces, or URLs), each file at most 2 MB.
  Fonts without `src` must be installed on the viewer's machine.
- The deck iframe has an opaque origin, so assets are inlined as data URIs.
