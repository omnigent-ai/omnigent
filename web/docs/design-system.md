# Omnigent Web Design System

Omnigent should feel calm, precise, and native while retaining Otto's warmth. The system is built from semantic tokens and shared primitives so light, dark, and custom palettes remain coherent.

## Brand roles

- **Otto pink** is the primary brand and action color. Use `brand-accent` for interaction and `otto-pink` only for mascot ornamentation.
- **Otto green** communicates success, availability, and Otto's companion. Do not use it as a generic action color.
- **System blue** is reserved for focus, selection, links, information, and browser-native interaction cues.
- Preserve Otto's source SVG. Integrate it through scale, spacing, local aura, and motion rather than redrawing it.

## Surface ladder

Use the semantic surface tokens in `src/index.css`: canvas (`background`), navigation (`sidebar`), content (`card`), opaque overlays (`card-solid`), trays (`tray`), and subdued regions (`muted`). Do not hardcode theme-specific backgrounds in components.

The greeting can use a restrained local aura around Otto. Chat canvases stay flat so transcript content remains the focus.

## Shape and elevation

- Compact transcript and status cards: `--radius-otto-sm` (10px).
- Small controls and code chips: `--radius-otto-xs`.
- User messages: 14px through the user-bubble primitive.
- Pills, status dots, and avatars: fully rounded.
- Use `--elevation-otto-*` and `--border-otto-*`; do not introduce literal radii or arbitrary shadows.

## Typography

- Standard UI chrome: `text-13`.
- Transcript card title: `text-card-title` (13/20).
- Transcript card body: `text-card-body` (12/18).
- Transcript metadata: `text-card-meta` (11/16).
- File references: regular-weight mono with `text-file-reference`; never a badge or bold treatment.
- Avoid arbitrary `text-[Npx]` utilities. Add a named step only when the scale genuinely lacks one.

## Transcript primitives

Tool calls, approvals, errors, routing cards, and status cards share:

- the transcript typography scale;
- Otto card radii and borders;
- `TRANSCRIPT_RAIL_CLASS` for nested details;
- semantic status foregrounds (`success-foreground`, `warning-foreground`, `info-foreground`);
- 44px minimum mobile touch targets, with compact desktop sizing where appropriate.

Prefer extending a canonical card or helper in `src/components/blocks` over creating a one-off container.

## Accessibility

- Every icon-only action needs an accessible name.
- Never nest interactive elements.
- Inputs need visible labels or `aria-label` and a visible `focus-visible` ring.
- Use foreground status tokens for text; base status hues are for decoration and surfaces.
- Preserve reduced-motion behavior and keyboard access.

## Theme verification

Every visual change must be checked in:

1. default light;
2. default dark;
3. at least one custom palette when tokens or broad surfaces change;
4. desktop and narrow/mobile layouts;
5. keyboard focus and reduced-motion mode when interaction or animation changes.

## Enforcement

Run `npm run lint:design` from `web/`. The checker examines added source lines relative to the main branch and rejects raw colors, arbitrary font sizes, literal radii, arbitrary shadows, and important modifiers in feature code. Theme definitions, syntax themes, icon assets, and tests are intentionally exempt.

When an exception is truly necessary, centralize it in a theme or primitive file rather than weakening the checker or scattering allowlists.
