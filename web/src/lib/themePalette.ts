// Persisted, app-global color-palette preference.
//
// The web UI has two independent appearance axes:
//
//   1. MODE  — light / dark / system, owned by next-themes (toggles the
//      `.dark` class on <html>; see components/theme/ThemeProvider.tsx).
//   2. PALETTE — the color scheme (Omni pink, GitHub, Vercel, …), owned here.
//
// A palette is applied as a `data-theme` attribute on <html>, so it composes
// with the mode class: `:root:not(.dark)[data-theme="github"]` is GitHub-light
// and `.dark[data-theme="github"]` is GitHub-dark (see the palette blocks in
// index.css). The default "omni" palette carries no CSS overrides — it falls
// through to the base `:root` / `.dark` tokens — so selecting it just restores
// the brand look. Everything is expressed through the existing CSS custom
// properties (--background, --primary, --sidebar, …), so a palette re-skins the
// whole app without any component knowing a theme changed.
//
// Mirrors lib/uiFontPreferences.ts: a read/write pair backed by localStorage
// plus a single `apply*` function that owns the DOM side-effect, called at boot
// (main.tsx) before first paint and on every change (Appearance settings).

const STORAGE_KEY = "omnigent:ui-theme-palette";

/** Selectable color palettes. The first entry is the default (brand) look. */
export const themePalettes = [
  "omni",
  "dracula",
  "github",
  "catppuccin",
  "gruvbox",
  "nord",
] as const;

export type ThemePalette = (typeof themePalettes)[number];

/** Built-in palettes plus the user's derived custom configuration. */
export const themeSelections = [...themePalettes, "custom"] as const;
export type ThemeSelection = (typeof themeSelections)[number];

/** Default palette: the Omni brand tokens already defined in `:root` / `.dark`. */
export const DEFAULT_PALETTE: ThemePalette = "omni";

/** A few representative colors used to render a palette's preview swatch. */
export interface PaletteSwatch {
  /** Page canvas (behind the cards). */
  bg: string;
  /** Card / panel surface floating on the canvas. */
  card: string;
  /** Primary action / brand accent for this palette. */
  accent: string;
  /** Card border / divider. */
  border: string;
  /** Body text on the card. */
  text: string;
}

export interface PaletteTokens {
  background: string;
  foreground: string;
  card: string;
  cardSolid: string;
  cardForeground: string;
  tray: string;
  popover: string;
  popoverForeground: string;
  primary: string;
  primaryForeground: string;
  secondary: string;
  secondaryForeground: string;
  muted: string;
  mutedForeground: string;
  codeBackground: string;
  accent: string;
  accentForeground: string;
  border: string;
  borderStrong: string;
  buttonBorder: string;
  input: string;
  ring: string;
  brandAccent: string;
  sidebar: string;
  sidebarForeground: string;
  sidebarPrimary: string;
  sidebarPrimaryForeground: string;
  sidebarAccent: string;
  sidebarAccentForeground: string;
  sidebarBorder: string;
  sidebarRing: string;
  sidebarActive: string;
  sidebarActiveForeground: string;
  sidebarBackground: string;
  shellBackground: string;
}

type PaletteTokenInput = Pick<
  PaletteTokens,
  | "background"
  | "foreground"
  | "card"
  | "cardSolid"
  | "primary"
  | "primaryForeground"
  | "secondary"
  | "muted"
  | "mutedForeground"
  | "codeBackground"
  | "accent"
  | "accentForeground"
  | "border"
  | "borderStrong"
  | "ring"
  | "sidebar"
  | "shellBackground"
> &
  Partial<PaletteTokens>;

function paletteTokens(tokens: PaletteTokenInput): PaletteTokens {
  return {
    cardForeground: tokens.foreground,
    tray: tokens.card,
    popover: tokens.cardSolid,
    popoverForeground: tokens.foreground,
    secondaryForeground: tokens.foreground,
    buttonBorder: tokens.border,
    input: tokens.border,
    brandAccent: tokens.ring,
    sidebarForeground: tokens.foreground,
    sidebarPrimary: tokens.ring,
    sidebarPrimaryForeground: tokens.primaryForeground,
    sidebarAccent: tokens.accent,
    sidebarAccentForeground: tokens.accentForeground,
    sidebarBorder: tokens.border,
    sidebarRing: tokens.ring,
    sidebarActive: "color-mix(in srgb, var(--sidebar-foreground) 7%, var(--sidebar))",
    sidebarActiveForeground: "var(--sidebar-foreground)",
    sidebarBackground: "var(--sidebar)",
    ...tokens,
  };
}

export interface PaletteMeta {
  id: ThemePalette;
  /** Display name shown under the swatch. */
  label: string;
  /** One-line description of the palette's character. */
  blurb: string;
  /** Swatch colors for the light rendering of this palette. */
  light: PaletteSwatch;
  /** Swatch colors for the dark rendering of this palette. */
  dark: PaletteSwatch;
  tokens: {
    light: PaletteTokens;
    dark: PaletteTokens;
  };
}

// Swatch colors are a hand-picked summary of each palette's CSS block — enough
// to render a faithful mini-preview without parsing the stylesheet. Keep these
// in sync with the matching `[data-theme]` block in index.css.
export const PALETTES: readonly PaletteMeta[] = [
  {
    id: "omni",
    label: "Omnigent",
    blurb: "The signature pink brand look.",
    light: {
      bg: "#fdf7fb",
      card: "#ffffff",
      accent: "#df3c85",
      border: "#e8ecf0",
      text: "#11171c",
    },
    dark: { bg: "#160e24", card: "#28223a", accent: "#df3c85", border: "#2a2440", text: "#f4f5f7" },
    tokens: {
      light: paletteTokens({
        background: "#ffffff",
        foreground: "#27272a",
        card: "#ffffff",
        cardSolid: "#ffffff",
        popoverForeground: "#11171c",
        primary: "#11171c",
        primaryForeground: "#ffffff",
        secondary: "#eceef1",
        muted: "#0000000f",
        mutedForeground: "#71717a",
        codeBackground: "#0000000f",
        accent: "#d7edfe",
        accentForeground: "#04355d",
        border: "#e4e4e7",
        borderStrong: "#a1a1aa",
        buttonBorder: "#d6d6d6",
        ring: "#11171c",
        brandAccent: "#df3c85",
        sidebar: "#fdfafa",
        sidebarPrimary: "#2272b4",
        sidebarRing: "#2272b4",
        sidebarActive: "rgba(240, 1, 150, 0.1)",
        sidebarActiveForeground: "#651249",
        sidebarBackground: "linear-gradient(90deg, #fffefe, #fcf6fa)",
        shellBackground: "var(--background)",
      }),
      dark: paletteTokens({
        background: "#0e1013",
        foreground: "oklch(0.965 0.003 240)",
        card: "rgba(31, 39, 45, 0.6)",
        cardSolid: "#181f25",
        popover: "rgba(26, 33, 41, 0.8)",
        primary: "#e8ecf0",
        primaryForeground: "#11171c",
        secondary: "#1f272d",
        secondaryForeground: "#e8ecf0",
        muted: "color-mix(in srgb, #92a4b3 15%, #262f36)",
        mutedForeground: "#92a4b3",
        codeBackground: "oklch(0.38 0.005 240)",
        accent: "#04355d",
        accentForeground: "#4299e0",
        border: "oklch(0.28 0.005 240)",
        borderStrong: "#a1a1aa",
        buttonBorder: "#d6d6d6",
        input: "oklch(0.28 0.005 240)",
        ring: "#e8ecf0",
        brandAccent: "#df3c85",
        sidebar: "rgba(17, 23, 28, 0.75)",
        sidebarPrimary: "oklch(0.64 0.22 254)",
        sidebarPrimaryForeground: "oklch(0.12 0.004 240)",
        sidebarAccent: "oklch(0.245 0.005 240)",
        sidebarAccentForeground: "oklch(0.965 0.003 240)",
        sidebarBorder: "oklch(0.28 0.005 240)",
        sidebarRing: "oklch(0.92 0.003 240 / 0.4)",
        sidebarActive: "rgba(240, 1, 150, 0.15)",
        sidebarActiveForeground: "#f472b6",
        sidebarBackground:
          "linear-gradient(transparent 35%, rgba(92, 48, 108, 0.2)), linear-gradient(135deg, rgba(255, 255, 255, 0.04), transparent 60%)",
        shellBackground:
          "radial-gradient(rgba(100, 40, 180, 0.12), transparent 50%), radial-gradient(at 80% 20%, rgba(80, 30, 140, 0.08), transparent 45%), linear-gradient(145deg, #1a0e2d, #0e1418, #130e1b)",
      }),
    },
  },
  {
    id: "dracula",
    label: "Dracula",
    blurb: "Moody purple with a pink pop.",
    light: {
      bg: "#f7f5fd",
      card: "#ffffff",
      accent: "#7c3aed",
      border: "#e6e0f2",
      text: "#1e1a2b",
    },
    dark: { bg: "#282a36", card: "#343746", accent: "#bd93f9", border: "#44475a", text: "#f8f8f2" },
    tokens: {
      light: paletteTokens({
        background: "#f7f5fd",
        foreground: "#1e1a2b",
        card: "#ffffff",
        cardSolid: "#ffffff",
        primary: "#7c3aed",
        primaryForeground: "#ffffff",
        secondary: "#efeaf9",
        muted: "#efeaf9",
        mutedForeground: "#6b6786",
        codeBackground: "#efeaf9",
        accent: "#f3e8ff",
        accentForeground: "#6b21a8",
        border: "#e6e0f2",
        borderStrong: "#b6abd6",
        ring: "#7c3aed",
        brandAccent: "#d6409f",
        sidebar: "#f3f0fa",
        shellBackground: "linear-gradient(160deg, #faf8ff 0%, #f5f1fd 50%, #f3eefb 100%)",
      }),
      dark: paletteTokens({
        background: "#282a36",
        foreground: "#f8f8f2",
        card: "rgba(68, 71, 90, 0.5)",
        cardSolid: "#343746",
        popover: "rgba(33, 34, 44, 0.92)",
        primary: "#bd93f9",
        primaryForeground: "#282a36",
        secondary: "#343746",
        muted: "#3b3d4d",
        mutedForeground: "#b3b8d4",
        codeBackground: "#21222c",
        accent: "#3b304f",
        accentForeground: "#ff79c6",
        border: "#44475a",
        borderStrong: "#5a5d75",
        ring: "#bd93f9",
        brandAccent: "#ff79c6",
        sidebar: "rgba(33, 34, 44, 0.8)",
        shellBackground:
          "radial-gradient(ellipse at 20% 40%, rgba(189, 147, 249, 0.14) 0%, transparent 50%), radial-gradient(ellipse at 80% 20%, rgba(255, 121, 198, 0.1) 0%, transparent 45%), linear-gradient(160deg, #2a2c3a 0%, #282a36 55%, #21222c 100%)",
      }),
    },
  },
  {
    id: "github",
    label: "GitHub",
    blurb: "Clean neutrals with a signal blue.",
    light: {
      bg: "#f6f8fa",
      card: "#ffffff",
      accent: "#0969da",
      border: "#d1d9e0",
      text: "#1f2328",
    },
    dark: { bg: "#0d1117", card: "#161b22", accent: "#58a6ff", border: "#30363d", text: "#e6edf3" },
    tokens: {
      light: paletteTokens({
        background: "#f6f8fa",
        foreground: "#1f2328",
        card: "#ffffff",
        cardSolid: "#ffffff",
        primary: "#1f883d",
        primaryForeground: "#ffffff",
        secondary: "#eaeef2",
        muted: "#eaeef2",
        mutedForeground: "#59636e",
        codeBackground: "#eff1f3",
        accent: "#ddf4ff",
        accentForeground: "#0550ae",
        border: "#d1d9e0",
        borderStrong: "#afb8c1",
        ring: "#0969da",
        sidebar: "#f6f8fa",
        shellBackground: "linear-gradient(180deg, #fbfcfd 0%, #f6f8fa 100%)",
      }),
      dark: paletteTokens({
        background: "#0d1117",
        foreground: "#e6edf3",
        card: "rgba(22, 27, 34, 0.72)",
        cardSolid: "#161b22",
        popover: "rgba(22, 27, 34, 0.9)",
        primary: "#238636",
        primaryForeground: "#ffffff",
        secondary: "#21262d",
        muted: "#21262d",
        mutedForeground: "#8b949e",
        codeBackground: "#1c2128",
        accent: "#121d2f",
        accentForeground: "#58a6ff",
        border: "#30363d",
        borderStrong: "#444c56",
        ring: "#58a6ff",
        sidebar: "rgba(13, 17, 23, 0.75)",
        sidebarPrimaryForeground: "#0d1117",
        shellBackground: "linear-gradient(160deg, #0d1117 0%, #0a0e14 100%)",
      }),
    },
  },
  {
    id: "catppuccin",
    label: "Catppuccin",
    blurb: "Soft pastels — Latte & Mocha.",
    light: {
      bg: "#eff1f5",
      card: "#ffffff",
      accent: "#8839ef",
      border: "#ccd0da",
      text: "#4c4f69",
    },
    dark: { bg: "#1e1e2e", card: "#313244", accent: "#cba6f7", border: "#45475a", text: "#cdd6f4" },
    tokens: {
      light: paletteTokens({
        background: "#eff1f5",
        foreground: "#4c4f69",
        card: "#ffffff",
        cardSolid: "#ffffff",
        primary: "#8839ef",
        primaryForeground: "#ffffff",
        secondary: "#e6e9ef",
        muted: "#e6e9ef",
        mutedForeground: "#6c6f85",
        codeBackground: "#e6e9ef",
        accent: "#ccd0da",
        accentForeground: "#8839ef",
        border: "#ccd0da",
        borderStrong: "#acb0be",
        ring: "#8839ef",
        brandAccent: "#ea76cb",
        sidebar: "#e6e9ef",
        shellBackground: "linear-gradient(160deg, #f2f3f7 0%, #eff1f5 60%, #e9ecf2 100%)",
      }),
      dark: paletteTokens({
        background: "#1e1e2e",
        foreground: "#cdd6f4",
        card: "rgba(49, 50, 68, 0.6)",
        cardSolid: "#282938",
        popover: "rgba(24, 24, 37, 0.92)",
        primary: "#cba6f7",
        primaryForeground: "#1e1e2e",
        secondary: "#313244",
        muted: "#313244",
        mutedForeground: "#a6adc8",
        codeBackground: "#181825",
        accent: "#45475a",
        accentForeground: "#cba6f7",
        border: "#45475a",
        borderStrong: "#585b70",
        ring: "#cba6f7",
        sidebar: "rgba(24, 24, 37, 0.8)",
        shellBackground:
          "radial-gradient(ellipse at 20% 30%, rgba(203, 166, 247, 0.12) 0%, transparent 55%), radial-gradient(ellipse at 80% 20%, rgba(245, 194, 231, 0.08) 0%, transparent 45%), linear-gradient(160deg, #232336 0%, #1e1e2e 60%, #181825 100%)",
      }),
    },
  },
  {
    id: "gruvbox",
    label: "Gruvbox",
    blurb: "Warm retro earth tones.",
    light: {
      bg: "#fbf1c7",
      card: "#fffdf2",
      accent: "#d65d0e",
      border: "#e6d5a8",
      text: "#3c3836",
    },
    dark: { bg: "#282828", card: "#3c3836", accent: "#fe8019", border: "#504945", text: "#ebdbb2" },
    tokens: {
      light: paletteTokens({
        background: "#fbf1c7",
        foreground: "#3c3836",
        card: "#fffdf2",
        cardSolid: "#fffdf2",
        primary: "#d65d0e",
        primaryForeground: "#ffffff",
        secondary: "#ebdbb2",
        muted: "#ebdbb2",
        mutedForeground: "#7c6f64",
        codeBackground: "#ebdbb2",
        accent: "#f2e5bc",
        accentForeground: "#af3a03",
        border: "#e6d5a8",
        borderStrong: "#bdae93",
        ring: "#d65d0e",
        sidebar: "#f4e8bc",
        shellBackground: "linear-gradient(160deg, #fbf3cd 0%, #fbf1c7 55%, #f7ecbb 100%)",
      }),
      dark: paletteTokens({
        background: "#282828",
        foreground: "#ebdbb2",
        card: "rgba(60, 56, 54, 0.6)",
        cardSolid: "#32302f",
        popover: "rgba(29, 32, 33, 0.92)",
        primary: "#fe8019",
        primaryForeground: "#282828",
        secondary: "#3c3836",
        muted: "#3c3836",
        mutedForeground: "#a89984",
        codeBackground: "#1d2021",
        accent: "#504945",
        accentForeground: "#fe8019",
        border: "#504945",
        borderStrong: "#665c54",
        ring: "#fe8019",
        sidebar: "rgba(29, 32, 33, 0.8)",
        shellBackground:
          "radial-gradient(ellipse at 20% 30%, rgba(254, 128, 25, 0.1) 0%, transparent 55%), radial-gradient(ellipse at 80% 20%, rgba(250, 189, 47, 0.07) 0%, transparent 45%), linear-gradient(160deg, #32302f 0%, #282828 60%, #1d2021 100%)",
      }),
    },
  },
  {
    id: "nord",
    label: "Nord",
    blurb: "Arctic frost blues over polar-night neutrals.",
    light: {
      bg: "#eceff4",
      card: "#e5e9f0",
      accent: "#5e81ac",
      border: "#d8dee9",
      text: "#2e3440",
    },
    dark: { bg: "#2e3440", card: "#3b4252", accent: "#88c0d0", border: "#4c566a", text: "#eceff4" },
    tokens: {
      light: paletteTokens({
        background: "#eceff4",
        foreground: "#2e3440",
        card: "#e5e9f0",
        cardSolid: "#e5e9f0",
        primary: "#5e81ac",
        primaryForeground: "#eceff4",
        secondary: "#d8dee9",
        muted: "#d8dee9",
        mutedForeground: "#4c566a",
        codeBackground: "#d8dee9",
        accent: "#e5e9f0",
        accentForeground: "#5e81ac",
        border: "#d8dee9",
        borderStrong: "#81a1c1",
        ring: "#5e81ac",
        sidebar: "#e5e9f0",
        sidebarAccent: "#d8dee9",
        shellBackground: "linear-gradient(160deg, #eceff4 0%, #e5e9f0 55%, #d8dee9 100%)",
      }),
      dark: paletteTokens({
        background: "#2e3440",
        foreground: "#eceff4",
        card: "rgba(59, 66, 82, 0.6)",
        cardSolid: "#3b4252",
        popover: "rgba(46, 52, 64, 0.92)",
        primary: "#88c0d0",
        primaryForeground: "#2e3440",
        secondary: "#3b4252",
        muted: "#434c5e",
        mutedForeground: "#d8dee9",
        codeBackground: "#3b4252",
        accent: "#434c5e",
        accentForeground: "#88c0d0",
        border: "#4c566a",
        borderStrong: "#81a1c1",
        ring: "#88c0d0",
        sidebar: "rgba(46, 52, 64, 0.8)",
        shellBackground:
          "radial-gradient(ellipse at 20% 30%, rgba(136, 192, 208, 0.12) 0%, transparent 55%), radial-gradient(ellipse at 80% 20%, rgba(94, 129, 172, 0.08) 0%, transparent 45%), linear-gradient(160deg, #434c5e 0%, #2e3440 60%, #2e3440 100%)",
      }),
    },
  },
] as const;

/**
 * Return whether a string is a supported palette id.
 *
 * localStorage can hold any stale or hand-edited value, so this type guard
 * lets call sites reject unknown ids before handing them to the UI or the DOM.
 *
 * @param value Palette string to validate, e.g. `"github"`.
 * @returns Whether the value is a supported palette id.
 */
export function isThemePalette(value: unknown): value is ThemePalette {
  return typeof value === "string" && (themePalettes as readonly string[]).includes(value);
}

export function isThemeSelection(value: unknown): value is ThemeSelection {
  return typeof value === "string" && (themeSelections as readonly string[]).includes(value);
}

/**
 * Read the persisted palette.
 *
 * Returns the default when nothing is stored, on a server render (no `window`),
 * or when the stored value is missing/malformed — never throws, so a corrupt
 * entry can't break app boot.
 */
export function readThemePalette(): ThemeSelection {
  if (typeof window === "undefined") return DEFAULT_PALETTE;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_PALETTE;
    const parsed: unknown = JSON.parse(raw);
    return isThemeSelection(parsed) ? parsed : DEFAULT_PALETTE;
  } catch {
    return DEFAULT_PALETTE;
  }
}

/**
 * Persist the palette. An unknown id clears the preference (reverting to the
 * default) rather than storing garbage. Swallows quota/access errors so a
 * failed write can't break the app.
 */
export function writeThemePalette(palette: ThemeSelection): void {
  if (typeof window === "undefined") return;
  try {
    if (!isThemeSelection(palette) || palette === DEFAULT_PALETTE) {
      window.localStorage.removeItem(STORAGE_KEY);
      return;
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(palette));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}

/**
 * Apply the palette to the DOM by setting `data-theme` on the document root.
 * The `[data-theme]` blocks in index.css re-point the color tokens; the default
 * "omni" palette has no block, so it removes the attribute and the base
 * `:root` / `.dark` tokens take over. This is the single source of the DOM
 * side-effect and composes with next-themes' `.dark` class untouched.
 */
export function applyThemePalette(palette: ThemeSelection): void {
  if (typeof document === "undefined") return;
  const next = isThemeSelection(palette) ? palette : DEFAULT_PALETTE;
  if (next === DEFAULT_PALETTE) {
    document.documentElement.removeAttribute("data-theme");
    return;
  }
  document.documentElement.setAttribute("data-theme", next);
}
