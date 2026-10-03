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

## How to get to it (user POV)

**Desktop web and desktop app:** open a `*.slides.html` file from the Files
panel or a file link; it opens in the right-rail file viewer.

**Phone (web or mobile app):** open the same file from the Files panel; it
opens in the full-screen file viewer.

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
