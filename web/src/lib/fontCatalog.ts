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
// Settings renders one control for each category.

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

/**
 * A Google Fonts CSS2 catalog entry: label mirrors the family, `id` is the
 * stable slug, and the stylesheet href is built from the family + weights.
 */
function googleFont(
  category: FontCategory,
  id: string,
  family: string,
  weights: readonly number[],
): FontCatalogEntry {
  return {
    id,
    label: family,
    family,
    category,
    source: "google-css2",
    cssUrl: googleCss2Url(family, weights),
  };
}

// 25 eligible Sans Serif families in ascending Google popularity;
// base catalog families not in the ranked list follow.
const SANS_GOOGLE_FONTS: readonly [string, string, readonly number[]][] = [
  ["open-sans", "Open Sans", [400, 500, 600, 700]],
  ["roboto", "Roboto", [400, 500, 600, 700]],
  ["google-sans", "Google Sans", [400, 500, 600, 700]],
  ["inter", "Inter", [400, 500, 600, 700]],
  ["montserrat", "Montserrat", [400, 500, 600, 700]],
  ["rubik", "Rubik", [400, 500, 600, 700]],
  ["arimo", "Arimo", [400, 500, 600, 700]],
  ["ubuntu", "Ubuntu", [400, 500, 700]],
  ["lato", "Lato", [400, 700]],
  ["noto-sans", "Noto Sans", [400, 500, 600, 700]],
  ["raleway", "Raleway", [400, 500, 600, 700]],
  ["nunito", "Nunito", [400, 500, 600, 700]],
  ["nunito-sans", "Nunito Sans", [400, 500, 600, 700]],
  ["public-sans", "Public Sans", [400, 500, 600, 700]],
  ["dm-sans", "DM Sans", [400, 500, 600, 700]],
  ["manrope", "Manrope", [400, 500, 600, 700]],
  ["outfit", "Outfit", [400, 500, 600, 700]],
  ["plus-jakarta-sans", "Plus Jakarta Sans", [400, 500, 600, 700]],
  ["work-sans", "Work Sans", [400, 500, 600, 700]],
  ["quicksand", "Quicksand", [400, 500, 600, 700]],
  ["figtree", "Figtree", [400, 500, 600, 700]],
  ["pt-sans", "PT Sans", [400, 700]],
  ["mulish", "Mulish", [400, 500, 600, 700]],
  ["source-sans-3", "Source Sans 3", [400, 600, 700]],
  ["barlow", "Barlow", [400, 500, 600, 700]],
  ["geist", "Geist", [400, 500, 600, 700]],
];

const SANS_FONTS: readonly FontCatalogEntry[] = [
  // System default has no face to load; it maps to the bundled system stack.
  { id: "system-ui", label: "System default", family: "", category: "sans", source: "bundled" },
  ...SANS_GOOGLE_FONTS.map(([id, family, weights]) => googleFont("sans", id, family, weights)),
];

// 25 eligible Monospace families in ascending Google popularity, shared by both roles.
const MONOSPACE_GOOGLE_FONTS: readonly [string, string, string, readonly number[]][] = [
  ["roboto-mono", "roboto-mono-code", "Roboto Mono", [400, 500, 600, 700]],
  ["jetbrains-mono-fixed-width", "jetbrains-mono", "JetBrains Mono", [400, 500, 600, 700]],
  ["ibm-plex-mono", "ibm-plex-mono-code", "IBM Plex Mono", [400, 500, 600, 700]],
  ["inconsolata", "inconsolata-code", "Inconsolata", [400, 500, 600, 700]],
  ["source-code-pro-fixed-width", "source-code-pro", "Source Code Pro", [400, 500, 600, 700]],
  ["space-mono", "space-mono-code", "Space Mono", [400, 700]],
  ["dm-mono", "dm-mono-code", "DM Mono", [400, 500]],
  ["fira-code-fixed-width", "fira-code", "Fira Code", [400, 500, 600, 700]],
  ["courier-prime", "courier-prime-code", "Courier Prime", [400, 700]],
  ["google-sans-code", "google-sans-code-code", "Google Sans Code", [400, 500, 600, 700]],
  ["share-tech-mono", "share-tech-mono-code", "Share Tech Mono", [400]],
  ["pt-mono", "pt-mono-code", "PT Mono", [400]],
  ["ubuntu-mono", "ubuntu-mono-code", "Ubuntu Mono", [400, 700]],
  ["fira-mono", "fira-mono-code", "Fira Mono", [400, 500, 700]],
  ["cousine", "cousine-code", "Cousine", [400, 700]],
  ["cutive-mono", "cutive-mono-code", "Cutive Mono", [400]],
  ["red-hat-mono", "red-hat-mono-code", "Red Hat Mono", [400, 500, 600, 700]],
  ["azeret-mono", "azeret-mono-code", "Azeret Mono", [400, 500, 600, 700]],
  ["anonymous-pro", "anonymous-pro-code", "Anonymous Pro", [400, 700]],
  ["fragment-mono", "fragment-mono-code", "Fragment Mono", [400]],
  ["chivo-mono", "chivo-mono-code", "Chivo Mono", [400, 500, 600, 700]],
  ["spline-sans-mono", "spline-sans-mono-code", "Spline Sans Mono", [400, 500, 600, 700]],
  ["martian-mono", "martian-mono-code", "Martian Mono", [400, 500, 600, 700]],
  ["overpass-mono", "overpass-mono-code", "Overpass Mono", [400, 500, 600, 700]],
  ["b612-mono", "b612-mono-code", "B612 Mono", [400, 700]],
];

function monospaceFonts(category: "fixedWidth" | "code"): readonly FontCatalogEntry[] {
  return MONOSPACE_GOOGLE_FONTS.map(([fixedWidthId, codeId, family, weights]) =>
    googleFont(category, category === "fixedWidth" ? fixedWidthId : codeId, family, weights),
  );
}

const NERD_FONT_VARIANTS = [
  // Nerd Fonts stay pinned; fetched when webfonts is enabled and a dropdown
  // previews them or the user selects one.
  [
    "jetbrainsmono-nerd-font-mono",
    "JetBrainsMono Nerd Font Mono",
    "JetBrainsMono/Ligatures/Regular/JetBrainsMonoNerdFontMono-Regular.ttf",
  ],
  [
    "firacode-nerd-font-mono",
    "FiraCode Nerd Font Mono",
    "FiraCode/Regular/FiraCodeNerdFontMono-Regular.ttf",
  ],
  [
    "saucecodepro-nerd-font-mono",
    "SauceCodePro Nerd Font Mono",
    "SourceCodePro/SauceCodeProNerdFontMono-Regular.ttf",
  ],
  [
    "caskaydiacove-nerd-font-mono",
    "CaskaydiaCove Nerd Font Mono",
    "CascadiaCode/CaskaydiaCoveNerdFontMono-Regular.ttf",
  ],
] as const;

function nerdFonts(category: "fixedWidth" | "code"): readonly FontCatalogEntry[] {
  return NERD_FONT_VARIANTS.map(([id, family, path]): FontCatalogEntry => ({
    id: category === "code" ? id : `${id}-fixed-width`,
    label: family,
    family,
    category,
    source: "self-hosted",
    faces: nerdFace(path),
  }));
}

// ---- Fixed width (general monospace UI) -----------------------------------

const FIXED_WIDTH_FONTS: readonly FontCatalogEntry[] = [
  // Bundled by Fontsource in index.css, so selecting it never needs a fetch.
  {
    id: "geist-mono",
    label: "Geist Mono",
    family: "Geist Mono Variable",
    category: "fixedWidth",
    source: "bundled",
  },
  ...monospaceFonts("fixedWidth"),
  ...nerdFonts("fixedWidth"),
];

// ---- Code (editor + terminal) ---------------------------------------------

const CODE_FONTS: readonly FontCatalogEntry[] = [
  ...monospaceFonts("code"),
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
  ...nerdFonts("code"),
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
