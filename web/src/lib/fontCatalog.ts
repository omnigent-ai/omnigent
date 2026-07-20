// Curated registry of fonts the app can load on demand.
//
// The Settings font controls only set a font NAME; nothing loads the font, so
// picking a family the OS doesn't have installed renders the fallback stack
// instead. This catalog pairs each offered family with the metadata a loader
// (see lib/webFontLoader.ts) needs to actually fetch it, so a selected family
// works without a local install.
//
// Fonts are grouped into three roles the interface uses distinctly:
//   - `sans`       — UI/chrome text (the --ui-font-family variable).
//   - `fixedWidth` — general monospace UI usage.
//   - `code`       — the code editor (Monaco) and terminal (xterm).
// PR 2's Settings dropdowns render one control per category off this shape.

/** The three distinct font roles the interface exposes. */
export type FontCategory = "sans" | "fixedWidth" | "code";

/**
 * How a catalog entry's face data is delivered:
 *   - `bundled`      — already shipped in the app bundle (Fontsource import in
 *     index.css); no network fetch, the loader no-ops.
 *   - `google-css2`  — a keyless Google Fonts CSS2 stylesheet (`<link>`); Google
 *     serves the right woff2 per browser. Used for common web families.
 *   - `self-hosted`  — explicit `@font-face` rules the loader injects, pointing
 *     at a CDN/asset woff2/ttf. Used for Cascadia Code + the Nerd Font variants,
 *     which aren't on Google Fonts (or whose glyph coverage we pin explicitly).
 */
export type FontSource = "bundled" | "google-css2" | "self-hosted";

/** A single `@font-face` the self-hosted loader path injects. */
export interface FontFaceAsset {
  /** Absolute URL to the font file (woff2/woff/ttf). */
  readonly url: string;
  /** CSS `font-weight` for this face, e.g. `"400"` or `"400 700"`. */
  readonly weight?: string;
  /** CSS `font-style` for this face; defaults to `"normal"`. */
  readonly style?: string;
  /** `format(...)` hint, e.g. `"woff2"` or `"truetype"`. */
  readonly format?: string;
}

/** One offered font, with everything needed to display AND load it. */
export interface FontCatalogEntry {
  /** Stable, URL/attribute-safe id. Never reused or renamed. */
  readonly id: string;
  /** Human-facing label for the Settings dropdown, e.g. `"JetBrains Mono"`. */
  readonly label: string;
  /**
   * The CSS `font-family` name to apply and to await via `document.fonts.load`.
   * Must match the family name the delivered face registers under.
   */
  readonly family: string;
  /** Which of the three interface roles this font is offered for. */
  readonly category: FontCategory;
  /** How the face data is delivered (see {@link FontSource}). */
  readonly source: FontSource;
  /**
   * `google-css2`: the stylesheet href to inject.
   * Ignored for other sources.
   */
  readonly cssUrl?: string;
  /**
   * `self-hosted`: the `@font-face` faces to inject.
   * Ignored for other sources.
   */
  readonly faces?: readonly FontFaceAsset[];
}

// Pinned Nerd Fonts release — a tag, never `@master`, so the patched-font paths
// and glyph coverage stay stable across deploys.
const NERD_FONTS_TAG = "v3.4.0";
const NERD_FONTS_BASE = `https://cdn.jsdelivr.net/gh/ryanoasis/nerd-fonts@${NERD_FONTS_TAG}/patched-fonts`;

// Fontsource CDN version for Cascadia Code (not on Google Fonts). Pinned so the
// woff2 URLs don't drift.
const CASCADIA_FONTSOURCE = "https://cdn.jsdelivr.net/fontsource/fonts/cascadia-code@5.2.3";

/**
 * Build a keyless Google Fonts CSS2 href for `family` at the given weights.
 * Spaces become `+`; the `:wght@…;…` axis spec uses the literal `:@;` Google's
 * CSS2 endpoint expects (all URL-safe here — no user input reaches this).
 */
function googleCss2Url(family: string, weights: readonly number[]): string {
  const spec = `${family.replace(/ /g, "+")}:wght@${weights.join(";")}`;
  return `https://fonts.googleapis.com/css2?family=${spec}&display=swap`;
}

/** A single-file Nerd Font Mono face (regular weight) from the pinned CDN. */
function nerdFace(path: string): readonly FontFaceAsset[] {
  return [{ url: `${NERD_FONTS_BASE}/${path}`, weight: "400", format: "truetype" }];
}

// ---- Sans (UI/chrome) -----------------------------------------------------

const SANS_FONTS: readonly FontCatalogEntry[] = [
  // System default: no face to load — the empty family maps to --font-sans.
  { id: "system-ui", label: "System default", family: "", category: "sans", source: "bundled" },
  {
    id: "inter",
    label: "Inter",
    family: "Inter",
    category: "sans",
    source: "google-css2",
    cssUrl: googleCss2Url("Inter", [400, 500, 600, 700]),
  },
  {
    id: "roboto",
    label: "Roboto",
    family: "Roboto",
    category: "sans",
    source: "google-css2",
    cssUrl: googleCss2Url("Roboto", [400, 500, 700]),
  },
  {
    id: "open-sans",
    label: "Open Sans",
    family: "Open Sans",
    category: "sans",
    source: "google-css2",
    cssUrl: googleCss2Url("Open Sans", [400, 600, 700]),
  },
  {
    id: "lato",
    label: "Lato",
    family: "Lato",
    category: "sans",
    source: "google-css2",
    cssUrl: googleCss2Url("Lato", [400, 700]),
  },
  {
    id: "source-sans-3",
    label: "Source Sans 3",
    family: "Source Sans 3",
    category: "sans",
    source: "google-css2",
    cssUrl: googleCss2Url("Source Sans 3", [400, 600, 700]),
  },
  {
    id: "geist",
    label: "Geist",
    family: "Geist",
    category: "sans",
    source: "google-css2",
    cssUrl: googleCss2Url("Geist", [400, 500, 600, 700]),
  },
];

// ---- Fixed width (general monospace UI) -----------------------------------

const FIXED_WIDTH_FONTS: readonly FontCatalogEntry[] = [
  // Bundled via Fontsource (@fontsource-variable/geist-mono in index.css) — the
  // loader no-ops for this one.
  {
    id: "geist-mono",
    label: "Geist Mono",
    family: "Geist Mono Variable",
    category: "fixedWidth",
    source: "bundled",
  },
  {
    id: "ibm-plex-mono",
    label: "IBM Plex Mono",
    family: "IBM Plex Mono",
    category: "fixedWidth",
    source: "google-css2",
    cssUrl: googleCss2Url("IBM Plex Mono", [400, 500, 600, 700]),
  },
  {
    id: "roboto-mono",
    label: "Roboto Mono",
    family: "Roboto Mono",
    category: "fixedWidth",
    source: "google-css2",
    cssUrl: googleCss2Url("Roboto Mono", [400, 500, 700]),
  },
  {
    id: "space-mono",
    label: "Space Mono",
    family: "Space Mono",
    category: "fixedWidth",
    source: "google-css2",
    cssUrl: googleCss2Url("Space Mono", [400, 700]),
  },
];

// ---- Code (editor + terminal) ---------------------------------------------

const CODE_FONTS: readonly FontCatalogEntry[] = [
  {
    id: "jetbrains-mono",
    label: "JetBrains Mono",
    family: "JetBrains Mono",
    category: "code",
    source: "google-css2",
    cssUrl: googleCss2Url("JetBrains Mono", [400, 500, 700]),
  },
  {
    id: "fira-code",
    label: "Fira Code",
    family: "Fira Code",
    category: "code",
    source: "google-css2",
    cssUrl: googleCss2Url("Fira Code", [400, 500, 700]),
  },
  {
    id: "source-code-pro",
    label: "Source Code Pro",
    family: "Source Code Pro",
    category: "code",
    source: "google-css2",
    cssUrl: googleCss2Url("Source Code Pro", [400, 500, 700]),
  },
  {
    id: "ibm-plex-mono-code",
    label: "IBM Plex Mono",
    family: "IBM Plex Mono",
    category: "code",
    source: "google-css2",
    cssUrl: googleCss2Url("IBM Plex Mono", [400, 500, 600, 700]),
  },
  // Cascadia Code isn't a Google Fonts family we bundle; deliver its @font-face
  // from the Fontsource CDN (latin subset, regular + bold).
  {
    id: "cascadia-code",
    label: "Cascadia Code",
    family: "Cascadia Code",
    category: "code",
    source: "self-hosted",
    faces: [
      { url: `${CASCADIA_FONTSOURCE}/latin-400-normal.woff2`, weight: "400", format: "woff2" },
      { url: `${CASCADIA_FONTSOURCE}/latin-700-normal.woff2`, weight: "700", format: "woff2" },
    ],
  },
  // Nerd Font variants: patched glyphs (powerline, icons) IDE/terminal users
  // expect. Delivered as single-file Mono faces from the pinned Nerd Fonts CDN,
  // lazily — never eagerly imported.
  {
    id: "jetbrainsmono-nerd-font-mono",
    label: "JetBrainsMono Nerd Font Mono",
    family: "JetBrainsMono Nerd Font Mono",
    category: "code",
    source: "self-hosted",
    faces: nerdFace("JetBrainsMono/Ligatures/Regular/JetBrainsMonoNerdFontMono-Regular.ttf"),
  },
  {
    id: "firacode-nerd-font-mono",
    label: "FiraCode Nerd Font Mono",
    family: "FiraCode Nerd Font Mono",
    category: "code",
    source: "self-hosted",
    faces: nerdFace("FiraCode/Regular/FiraCodeNerdFontMono-Regular.ttf"),
  },
  {
    id: "saucecodepro-nerd-font-mono",
    label: "SauceCodePro Nerd Font Mono",
    family: "SauceCodePro Nerd Font Mono",
    category: "code",
    source: "self-hosted",
    faces: nerdFace("SourceCodePro/SauceCodeProNerdFontMono-Regular.ttf"),
  },
  {
    id: "caskaydiacove-nerd-font-mono",
    label: "CaskaydiaCove Nerd Font Mono",
    family: "CaskaydiaCove Nerd Font Mono",
    category: "code",
    source: "self-hosted",
    faces: nerdFace("CascadiaCode/CaskaydiaCoveNerdFontMono-Regular.ttf"),
  },
];

/** Every catalog entry, in category then display order. */
export const FONT_CATALOG: readonly FontCatalogEntry[] = [
  ...SANS_FONTS,
  ...FIXED_WIDTH_FONTS,
  ...CODE_FONTS,
];

/** The catalog grouped by role, for the three Settings controls. */
export const FONT_CATALOG_BY_CATEGORY: Readonly<Record<FontCategory, readonly FontCatalogEntry[]>> =
  {
    sans: SANS_FONTS,
    fixedWidth: FIXED_WIDTH_FONTS,
    code: CODE_FONTS,
  };

// id → entry, built once. Ids are unique across the catalog (asserted in tests).
const BY_ID = new Map<string, FontCatalogEntry>(FONT_CATALOG.map((e) => [e.id, e]));

/** Look up a catalog entry by its stable id. */
export function getFontById(id: string): FontCatalogEntry | undefined {
  return BY_ID.get(id);
}

// Lower-cased family → ALL matching entries, in catalog order. A family (e.g.
// "IBM Plex Mono") can appear in more than one category, so we keep every match
// rather than the first — a category-aware lookup needs the right one.
const BY_FAMILY = new Map<string, FontCatalogEntry[]>();
for (const entry of FONT_CATALOG) {
  const key = entry.family.trim().toLowerCase();
  if (!key) continue;
  const list = BY_FAMILY.get(key);
  if (list) list.push(entry);
  else BY_FAMILY.set(key, [entry]);
}

/**
 * All catalog entries offered under a family NAME, case-insensitively (empty
 * for a non-catalog family or the empty/System-default name). A shared family
 * like "IBM Plex Mono" returns one entry per category it appears in.
 */
export function getFontsByFamily(family: string): readonly FontCatalogEntry[] {
  const key = family.trim().toLowerCase();
  if (!key) return [];
  return BY_FAMILY.get(key) ?? [];
}

/**
 * Resolve a typed/stored family NAME to a catalog entry, case-insensitively.
 *
 * The bridge from the free-text font inputs (which store a bare family string)
 * to the loader: a typed name matching a catalog family resolves to an entry to
 * load; anything else (a locally-installed font, a partial name, the empty
 * System-default name) resolves to `undefined` and is left to the OS.
 *
 * When `category` is given (e.g. PR 2's per-role dropdowns), the entry from that
 * category wins — so a shared family like "IBM Plex Mono" resolves to its `code`
 * entry for the code control and its `fixedWidth` entry for the mono control. If
 * the family isn't offered in that category, the first match is returned anyway:
 * the delivery metadata is identical across a family's entries, so the font
 * still loads. With no `category`, the first catalog occurrence wins
 * (backward-compatible).
 */
export function getFontByFamily(
  family: string,
  category?: FontCategory,
): FontCatalogEntry | undefined {
  const matches = getFontsByFamily(family);
  if (matches.length === 0) return undefined;
  if (category) return matches.find((e) => e.category === category) ?? matches[0];
  return matches[0];
}

/**
 * The canonical load key for an entry: its underlying resource identity, NOT its
 * catalog id. Two entries with different ids but the same delivery resource (e.g.
 * the `fixedWidth` and `code` IBM Plex Mono entries share one Google CSS2 URL)
 * produce the same key, so the loader injects the stylesheet/faces once and
 * shares a single in-flight load across them.
 */
export function fontLoadKey(entry: FontCatalogEntry): string {
  if (entry.source === "google-css2") return entry.cssUrl ?? `id:${entry.id}`;
  if (entry.source === "self-hosted") {
    const urls = (entry.faces ?? []).map((f) => f.url).join("|");
    return urls || `id:${entry.id}`;
  }
  return `bundled:${entry.id}`;
}
