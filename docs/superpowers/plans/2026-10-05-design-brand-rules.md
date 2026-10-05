# Design page: brand-rule warnings

Contract: `### Brand-rule warnings` under `## Later phases` in
`designs/DESIGN_PAGE.md`. Branch `polly/design-brand-rules`, on wireframes.

## Findings that shape the plan

- `loadDeckBranding` already has the full-system branch with injected reads
  (`deps.read`, per-file cap through `kitText`), the owner gate for absolute
  paths, and a design-system timeout. Brand rules load there, after
  injection succeeds, and never turn into a notice: any problem with the
  adherence file or templates means no badge.
- Templates need a directory listing. The session filesystem listing
  (`fetchWorkspaceDirectory` in `useWorkspaceChangedFiles.ts`) handles both
  relative (imported) and absolute folders with the same auth split as reads;
  it is exported and injected as an optional `deps.list`.
- PR 9's import copies a fixed file list that lacks `_adherence.oxlintrc.json`,
  so an imported system would never show warnings. The file joins
  `IMPORT_FILES` (it is `.json`, already an allowed extension).
- Wireframes start grayscale, so brand-color warnings would be noise there.
  Decision: decks only. The loader computes warnings only when kit
  `sections` is on (decks); wireframes pass `sections: false` and do not read
  the adherence file at all.
- The oxlint selectors are AST selectors for JS, e.g.
  `Literal[value=/#[0-9a-f]{3,8}/i]`. Only the `/regex/flags` inside each
  selector is reused, tested against each token of a CSS declaration value,
  so both anchored and unanchored patterns behave. A pattern that mentions
  `font` is not a value pattern; fonts are checked against `fontFamilies`
  (plus token names and CSS generic families) on `font-family`.

## Safety (untrusted `_adherence.oxlintrc.json`)

- Read through `kitText` (PR 7's 2 MB per-file cap); `JSON.parse` in a try;
  every field shape-checked.
- At most 8 patterns, each at most 200 characters; `new RegExp` in a try;
  patterns with nested quantifiers (`(a+)+`) or backreferences are dropped.
- Input bound: tokens over 64 characters are skipped and at most 5000
  declarations are scanned per document, so even a slow pattern runs on
  small, bounded input.
- Templates: top-level `.css` and `.html` under `templates/`, at most 20
  files, each through `kitText`.

## Tasks

1. `web/src/lib/brandRules.ts` (pure): `parseAdherence`, `scanBrandWarnings`
   (DOMParser, `<style>` text and `style` attributes only), noise rules,
   template baseline (`templatePairs`), dedupe by value. Tests first: each
   rule, each noise rule, baseline, slide text ignored, hostile configs
   (invalid regex, oversized file, pathological pattern, bad JSON).
2. Loader: `deckBranding` gains `brandWarnings` (null means no badge) and the
   optional `list` dep; `designViewer` injects the listing; the import
   copies the adherence file; the fixture gains an adherence file and one
   template. Tests: full system yields warnings, template baseline applies,
   kit, skill-only, and wireframe (`sections: false`) yield none.
3. `SlidesViewer`: toolbar badge "N brand warnings" opening a popover list
   (value, property, where). Tests: badge and list in full mode, none for
   kit and skill-only, export unchanged.
4. Skill: the shared "Using a design system" text names the adherence file's
   tokens and fonts; `test_load_skill` pins it.

## Gates

`pnpm type-check`, `pnpm lint`, targeted vitest (shell, lib, pages/design),
one full `pnpm test`, `test_load_skill`, `pre-commit run --files`.
