# Wireframes

A file named `*.wireframe.html` (a self-contained, responsive HTML document
with one top-level `<section data-screen>` per screen) opens in a wireframe
viewer instead of the plain HTML preview. The page runs in the same sandboxed
preview as other HTML files at a chosen device size, scaled to fit the panel,
and offers a device picker, a screen picker, in-frame screen links,
fullscreen, and a Source toggle back to the code view. Agents write them with
the `wireframes` framework skill.

## Sub-features

- `wireframe-open`: opening a `*.wireframe.html` file shows its first screen
  at Desktop size. Other `.html` files keep the plain HTML preview and
  `*.slides.html` keeps the deck viewer.
- `wireframe-device`: the Device buttons (Desktop 1440x900, Tablet 834x1194,
  Phone 390x844) set the frame size. The frame scales down to fit the panel
  (never up) and the page scrolls inside the frame, so media queries see the
  device width.
- `wireframe-screens`: each top-level `<section data-screen="id"
  data-title="Title">` is a screen. With more than one, a Screen select lists
  them by title (the id when there is no title). A file with no such sections
  renders as one screen.
- `wireframe-links`: inside the frame, a click on an `<a href="#id">` or an
  element with `data-goto="id"` that names a screen switches to it, scrolls to
  the top, and updates the Screen select; a `#id` that names another element
  scrolls to it. Other links open in a new tab.
- `wireframe-branding`: a design kit at `.omnigent/design-kit/kit.json` or a
  design system pointer at `.omnigent/design-system.json` applies only its
  fonts and tokens (`:root` variables); the kit logo, kit stylesheet, and
  slide base rules are skipped. The toolbar shows the kit or design system
  name, and the deck's notices ("Design kit not applied: reason", "Design
  system not applied: reason", and the owner and sandbox notices) apply the
  same way. See `deck-design-kit` and `deck-design-system` in
  `feature-map/slide-decks.md`.
- `wireframe-fullscreen`: the fullscreen toggle fills the screen with the
  frame; it is hidden when the browser has no Fullscreen API.
- `wireframe-source`: "View wireframe source" switches to the code view; the
  toolbar's "View preview" returns to the wireframe.
- `wireframe-skill`: the `wireframes` skill (listed by `load_skill` and
  shipped into native harness bundles like `slide-decks`) asks for responsive
  CSS, grayscale first, kit fonts and tokens only, and a complete document
  after every edit. Both it and `slide-decks` point to the shared
  `design-systems` skill for design-system pointers.

## How to get to it (user POV)

**Desktop web and desktop app:** open a `*.wireframe.html` file from the Files
panel or a file link; it opens in the right-rail file viewer. On the Design
page, a Wireframe card opens it in the studio.

**Phone (web or mobile app):** open the same file from the Files panel; it
opens in the full-screen file viewer, scaled to the screen width.

**New wireframe:** on the Design page choose New design, then Wireframe.

## Driving it with the repro environment

Preconditions: a running instance (`verify-env start`, then `verify-env
doctor`) and the built web UI. There is no E2E test for the wireframe viewer
yet.

- Screen listing, the srcdoc (screen style, frame script, branding order),
  link and `data-goto` handling, scaling, the device and screen pickers, and
  the Source button: `cd web && pnpm exec vitest run src/shell/WireframeViewer.test.tsx`;
  routing from the file viewer: `pnpm exec vitest run src/shell/CodeViewer.test.tsx`.
- Fonts-and-tokens-only branding: `cd web && pnpm exec vitest run src/shell/deckBranding.test.ts src/shell/SlidesViewer.test.tsx`.
- Skill listing and bundling: `uv run --frozen --group test python -m pytest -p no:xdist tests/tools/builtins/test_load_skill.py tests/runner/test_orchestrator_skill_injection.py`.
- By hand: write a file `flow.wireframe.html` in a workspace with two
  sections, `<section data-screen="home" data-title="Home"><a href="#signup">Sign up</a></section>`
  and `<section data-screen="signup" data-title="Sign up"><button data-goto="home">Back</button></section>`,
  plus a `@media (max-width: 500px)` rule. Open it, choose Sign up in the
  frame and Back, check the Screen select follows, switch to Phone and check
  the media query applies and the frame stays inside the panel, then try
  fullscreen and "View wireframe source".
- Branding by hand: copy `examples/design-kits/sample` to
  `.omnigent/design-kit`, reopen the wireframe, and check its fonts and
  `var(--...)` colors apply while no kit logo appears.

## Gotchas

- Only sections that are direct children of the body count as screens;
  screens added by the page's own script after load are not listed.
- Hidden screens are hidden by an injected `@media screen` rule, so printing
  shows every screen.
- A literal `</body>` in page text or `<noscript>` can shift where the
  viewer's script is injected.
- Branding loads when the wireframe opens or its content refreshes; editing
  only the kit or design system needs the wireframe reopened.
- There is no HTML export for wireframes yet.
