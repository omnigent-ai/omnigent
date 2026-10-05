# Design page: standalone HTML export

Contract: `### Standalone HTML export` under `## Later phases` in
`designs/DESIGN_PAGE.md`. Branch `polly/design-export`, on the design-system
import.

## Findings that shape the plan

- `SlidesViewer` already holds exactly what it rendered: `deckContent` (the
  deck with `ds:` references rewritten) plus `kitStyle` and `systemStyle` from
  `loadDeckBranding`. The export takes those three values; nothing is loaded
  or sanitized again.
- The injected styles are already `@import`-free (`processDesignSystemCss`
  strips them, kit CSS is allowlisted) and the viewer never injects
  design-system scripts, so "left out" reduces to: no frame script and no
  message protocol in the output. The deck's own markup is kept verbatim, as
  in the preview.
- `branding.notice` is set only on failure (kit error, design-system error,
  timeout, owner-only, unreadable folder), so "did not load" is
  `branding === null || branding.notice !== null`.
- `triggerBrowserDownload` in `@/hooks/useFileContent` is the existing
  download helper.
- `SlidesViewer` has no file path; both callers (`CodeViewer`,
  `DesignStudio`) know it and pass a new optional `path` for `<deck>.html`.

## Tasks (tests first, one commit each)

1. **Export builder.** `prepareSlidesExport(html, kitStyle, systemStyle)`
   next to `prepareSlidesDoc`, sharing its placement logic. It injects the
   print rules, the kit and system styles, and a standalone script. The
   "show one slide" and full-viewport rules are scoped to
   `html[data-omnigent-deck]`, which only the script sets. The script handles
   ArrowLeft/ArrowRight, PageUp/PageDown, Space, click (not on links or
   controls), `#n` on load and on `hashchange`, and ignores modified keys and
   keys in form fields. Tests: no frame script or message source, styles in
   the right places, stacked fallback without the script, keys, form-field
   ignore, click, `#n`.
2. **Download HTML button.** In the deck toolbar next to Print; saves
   `<deck>.html` (`deck.html` without a path) with
   `triggerBrowserDownload`. Disabled while branding loads, when truncated,
   or when branding shows a notice. `CodeViewer` and `DesignStudio` pass
   `path`. Tests: download contents and name, each disabled state, the button
   in the file viewer, the studio passing the path.
3. **Docs.** Feature map `deck-export` sub-feature and manual check.

## Out of scope

Wireframes, user and org kits, brand rules, stripping `@import` or scripts
the deck itself contains, scaling the exported slide to a 16:9 stage.
