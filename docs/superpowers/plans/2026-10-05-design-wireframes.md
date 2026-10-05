# Design page: wireframes

Contract: `### Wireframes` under `## Later phases` in `designs/DESIGN_PAGE.md`.
Branch `polly/design-wireframes`, on the standalone HTML export.

## Findings that shape the plan

- Branding already splits cleanly: `loadDeckBranding` returns `kitStyle`
  (built by `buildDesignKitStyle`) and the kind-agnostic design-system
  `systemStyle` plus rewritten `content` from `injectDesignSystem`. Only the
  kit builder adds section rules (`body>section` background, color, font,
  `position: relative`, the `::after` logo) and loads the logo and the kit
  stylesheet. A `sections: false` option on the builder, threaded through
  `loadDesignKit` and `loadDeckBranding`, gives wireframes `@font-face` and
  the `--kit-*` tokens only, and skips reading the logo and kit CSS they do
  not use. Every sanitizer check and cap stays on the shared path.
- The branding gate (per-session state, the design-system read cache, the
  kit and design-system timeouts) and fullscreen live inline in
  `SlidesViewer`. They move into hooks in `web/src/shell/designViewer.ts`
  that both viewers use; `SlidesViewer` behavior and tests are unchanged.
- The preview's `<base target="_blank">` turns `#id` links into new tabs.
  The wireframe frame script intercepts clicks on `a[href^="#"]` and
  `[data-goto]` in the capture phase: a screen id switches screens (and
  tells the parent), another id scrolls to that element, anything else is a
  no-op. Same postMessage protocol shape as the deck (source tag, parent
  only), so the sandbox is unchanged.
- PR 8's index lists both kinds when `kind` is omitted, and the reconcile
  `PUT` takes `kind`. One workspace search with `q=.html` and both include
  globs (the runner splits comma lists) covers both kinds; the reconcile sends
  one `PUT` per kind so each replaces only its own rows.
- Framework skills ship from `FRAMEWORK_SKILL_DIRS` (load_skill listing and
  native bundle links) and `skills/**/*` package data, so a new dir under
  `omnigent/resources/skills/` ships with one list entry.
- Shared "Using a design system" text: a third framework skill,
  `design-systems`, holds it, and both `slide-decks` and `wireframes` tell
  the agent to load it. A skill is the one place both the runner's
  `load_skill` and native harness bundles can reach. The slide-decks pin for
  that text follows it to the shared skill, plus a check that slide-decks
  points there.

## Tasks (tests first, one commit each)

1. **Skills.** New `design-systems` (moved text) and `wireframes` (format:
   `.wireframe.html`, top-level `<section data-screen data-title>` screens,
   `href="#id"` / `data-goto` links, responsive CSS for 1440, 834, 390 wide
   frames, grayscale first, kit tokens and fonts only; incremental workflow).
   slide-decks points to design-systems. Tests: load_skill listing and
   loading, the design-system pin, orchestrator injection list, sub-agent
   seeded set.
2. **Kit style flag.** `buildDesignKitStyle(..., { sections })`,
   `loadDesignKit(read, { sections })`, `loadDeckBranding(content, deps,
   { sections })`. Tests: no section rules or logo, fonts and tokens kept,
   logo and CSS not read, design system unaffected.
3. **Wireframe document.** `isWireframeFile`, `listWireframeScreens`,
   `prepareWireframeDoc` (show the active screen only, frame script for
   screen switches and links), `WIREFRAME_DEVICES`. Tests: detection, screen
   listing (none means one), injection order, the script's link handling.
4. **Shared viewer hooks.** Move the branding gate and fullscreen out of
   `SlidesViewer` into `designViewer.ts`. SlidesViewer tests stay green.
5. **WireframeViewer + routing.** Device picker (Desktop 1440x900, Tablet
   834x1194, Phone 390x844), frame at device size scaled to fit (never
   above 1), screen picker, messages from its own iframe only, branding
   badge and notice, Source, fullscreen. Lazy route in `CodeViewer`. Tests:
   routing, sizes and scaling, picker and link messages, branding without
   section rules.
6. **Design page kinds.** `DesignDeck.kind`, `designKind`, `designName`,
   combined search, per-kind reconcile, index of both kinds, card badge,
   studio viewer by kind. Tests: helpers, cards and badges, studio routing.
7. **New design toggle.** Slides | Wireframe in the dialog: path
   `wireframes/<slug>.wireframe.html`, the wireframes skill in the first
   message, slug collisions against the landing and the `wireframes/`
   listing (with its loading gate). Tests: toggle, message, collision, gate.
8. **Docs.** Feature map sub-features and manual checks.

## Out of scope

User or org kits, brand-rule warnings, wireframe export or print, scripted
screens counted at runtime.
