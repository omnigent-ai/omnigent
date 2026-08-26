import {
  isThemePalette,
  PALETTE_TOKEN_CSS_NAMES,
  PALETTES,
  type PaletteMeta,
  type PaletteSwatch,
  type PaletteTokens,
  type ThemePalette,
} from "./themePalette";

const STORAGE_KEY = "omnigent:custom-theme";
const HEX_COLOR = /^#[0-9a-f]{6}$/i;

export interface CustomTheme {
  basePalette: ThemePalette;
  accent: string;
  darkAccent: string;
  tint: string;
  darkTint: string;
  contrast: number;
  translucentSidebar: boolean;
}

export const DEFAULT_CUSTOM_THEME: CustomTheme = {
  basePalette: "omni",
  accent: "#11171c",
  darkAccent: "#e8ecf0",
  tint: "#ffffff",
  darkTint: "#0e1013",
  contrast: 50,
  translucentSidebar: false,
};

interface Rgb {
  r: number;
  g: number;
  b: number;
}

export type DerivedThemeVariant = PaletteTokens;

export interface DerivedCustomTheme {
  light: DerivedThemeVariant;
  dark: DerivedThemeVariant;
}

export function isHexColor(value: unknown): value is string {
  return typeof value === "string" && HEX_COLOR.test(value);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function normalizeTheme(value: unknown): CustomTheme | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<CustomTheme>;
  if (
    !isThemePalette(candidate.basePalette) ||
    !isHexColor(candidate.accent) ||
    !isHexColor(candidate.tint) ||
    typeof candidate.contrast !== "number" ||
    !Number.isFinite(candidate.contrast) ||
    typeof candidate.translucentSidebar !== "boolean"
  ) {
    return null;
  }
  return {
    basePalette: candidate.basePalette,
    accent: candidate.accent.toLowerCase(),
    darkAccent: isHexColor(candidate.darkAccent)
      ? candidate.darkAccent.toLowerCase()
      : (PALETTES.find((palette) => palette.id === candidate.basePalette)?.tokens.dark.primary ??
        DEFAULT_CUSTOM_THEME.darkAccent),
    tint: candidate.tint.toLowerCase(),
    darkTint: isHexColor(candidate.darkTint)
      ? candidate.darkTint.toLowerCase()
      : (PALETTES.find((palette) => palette.id === candidate.basePalette)?.tokens.dark.background ??
        DEFAULT_CUSTOM_THEME.darkTint),
    contrast: Math.round(clamp(candidate.contrast, 0, 100)),
    translucentSidebar: candidate.translucentSidebar,
  };
}

export function readCustomTheme(): CustomTheme {
  if (typeof window === "undefined") return DEFAULT_CUSTOM_THEME;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_CUSTOM_THEME;
    return normalizeTheme(JSON.parse(raw)) ?? DEFAULT_CUSTOM_THEME;
  } catch {
    return DEFAULT_CUSTOM_THEME;
  }
}

export function writeCustomTheme(theme: CustomTheme): void {
  if (typeof window === "undefined") return;
  const normalized = normalizeTheme(theme);
  if (!normalized) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // A failed preference write should not interrupt live theme updates.
  }
}

export function createCustomThemeFromPalette(palette: PaletteMeta): CustomTheme {
  return {
    basePalette: palette.id,
    accent: palette.tokens.light.primary.toLowerCase(),
    darkAccent: palette.tokens.dark.primary.toLowerCase(),
    tint: palette.tokens.light.background.toLowerCase(),
    darkTint: palette.tokens.dark.background.toLowerCase(),
    contrast: 50,
    translucentSidebar: false,
  };
}

function hexToRgb(hex: string): Rgb {
  return {
    r: Number.parseInt(hex.slice(1, 3), 16),
    g: Number.parseInt(hex.slice(3, 5), 16),
    b: Number.parseInt(hex.slice(5, 7), 16),
  };
}

function rgbToHex({ r, g, b }: Rgb): string {
  const channel = (value: number) =>
    Math.round(clamp(value, 0, 255))
      .toString(16)
      .padStart(2, "0");
  return `#${channel(r)}${channel(g)}${channel(b)}`;
}

function mix(first: string, second: string, secondWeight: number): string {
  const firstRgb = hexToRgb(first);
  const secondRgb = hexToRgb(second);
  const weight = clamp(secondWeight, 0, 1);
  return rgbToHex({
    r: firstRgb.r * (1 - weight) + secondRgb.r * weight,
    g: firstRgb.g * (1 - weight) + secondRgb.g * weight,
    b: firstRgb.b * (1 - weight) + secondRgb.b * weight,
  });
}

function luminance(hex: string): number {
  const color = hexToRgb(hex);
  const linear = (channel: number) => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return linear(color.r) * 0.2126 + linear(color.g) * 0.7152 + linear(color.b) * 0.0722;
}

function contrastRatio(first: string, second: string): number {
  const firstLuminance = luminance(first);
  const secondLuminance = luminance(second);
  const lighter = Math.max(firstLuminance, secondLuminance);
  const darker = Math.min(firstLuminance, secondLuminance);
  return (lighter + 0.05) / (darker + 0.05);
}

function ensureContrast(color: string, backgrounds: string[], toward: string): string {
  const passes = (candidate: string) =>
    backgrounds.every((background) => contrastRatio(candidate, background) >= 4.5);
  if (passes(color)) return color;
  let lower = 0;
  let upper = 1;
  let result = toward;
  for (let index = 0; index < 12; index += 1) {
    const weight = (lower + upper) / 2;
    const candidate = mix(color, toward, weight);
    if (passes(candidate)) {
      result = candidate;
      upper = weight;
    } else {
      lower = weight;
    }
  }
  return passes(result) ? result : toward;
}

function readableForeground(background: string): "#111318" | "#ffffff" {
  const value = luminance(background);
  const darkContrast = (value + 0.05) / (luminance("#111318") + 0.05);
  const lightContrast = (luminance("#ffffff") + 0.05) / (value + 0.05);
  return darkContrast >= lightContrast ? "#111318" : "#ffffff";
}

type GeneratedThemeVariant = Pick<
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
  | "border"
  | "borderStrong"
  | "sidebar"
>;

interface GeneratedCustomTheme {
  light: GeneratedThemeVariant;
  dark: GeneratedThemeVariant;
}

interface CssColor extends Rgb {
  alpha: number;
}

function parseCssColor(value: string): CssColor | null {
  const hex = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(value);
  if (hex) {
    return {
      ...hexToRgb(`#${hex[1]}`),
      alpha: hex[2] ? Number.parseInt(hex[2], 16) / 255 : 1,
    };
  }
  const rgb = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)$/i.exec(
    value,
  );
  if (!rgb) return null;
  return {
    r: Number(rgb[1]),
    g: Number(rgb[2]),
    b: Number(rgb[3]),
    alpha: rgb[4] ? Number(rgb[4]) : 1,
  };
}

function formatCssColor(color: CssColor, template: string): string {
  const rgb = rgbToHex(color);
  if (template.startsWith("#") && color.alpha === 1) return rgb;
  if (template.length === 9) {
    const alpha = Math.round(clamp(color.alpha, 0, 1) * 255)
      .toString(16)
      .padStart(2, "0");
    return `${rgb}${alpha}`;
  }
  return `rgba(${Math.round(clamp(color.r, 0, 255))}, ${Math.round(clamp(color.g, 0, 255))}, ${Math.round(clamp(color.b, 0, 255))}, ${Math.round(clamp(color.alpha, 0, 1) * 1000) / 1000})`;
}

function rebaseColor(base: string, reference: string, current: string): string {
  if (current === reference) return base;
  const baseColor = parseCssColor(base);
  const referenceColor = parseCssColor(reference);
  const currentColor = parseCssColor(current);
  if (!baseColor || !referenceColor || !currentColor) return base;
  return formatCssColor(
    {
      r: baseColor.r + currentColor.r - referenceColor.r,
      g: baseColor.g + currentColor.g - referenceColor.g,
      b: baseColor.b + currentColor.b - referenceColor.b,
      alpha: baseColor.alpha,
    },
    base,
  );
}

function setAlpha(color: string, alpha: number): string {
  const parsed = parseCssColor(color);
  return parsed ? formatCssColor({ ...parsed, alpha }, "rgba") : color;
}

function generateCustomTheme(theme: CustomTheme): GeneratedCustomTheme {
  const normalized = normalizeTheme(theme) ?? DEFAULT_CUSTOM_THEME;
  const contrast = normalized.contrast / 100;
  const lightBackground = mix(normalized.tint, "#fffdff", 0.79 - contrast * 0.15);
  const darkBackground = normalized.darkTint;

  const lightCard = mix(lightBackground, "#ffffff", 0.72 + contrast * 0.16);
  const darkCard = mix(darkBackground, "#ffffff", 0.05 + contrast * 0.08);
  const lightForeground = mix(normalized.tint, "#111318", 0.91 + contrast * 0.05);
  const darkForeground = mix(normalized.tint, "#ffffff", 0.86 + contrast * 0.08);
  const lightBorder = mix(lightBackground, lightForeground, 0.08 + contrast * 0.08);
  const darkBorder = mix(darkBackground, darkForeground, 0.1 + contrast * 0.08);
  const lightMuted = mix(lightBackground, lightForeground, 0.04 + contrast * 0.05);
  const darkMuted = mix(darkBackground, darkForeground, 0.07 + contrast * 0.06);
  const lightSidebar = mix(lightBackground, lightCard, 0.42);
  const darkSidebar = mix(darkBackground, darkCard, 0.34);
  const lightMutedForeground = ensureContrast(
    mix(lightForeground, lightBackground, 0.45 - contrast * 0.12),
    [lightBackground, lightCard, lightMuted],
    lightForeground,
  );
  const darkMutedForeground = ensureContrast(
    mix(darkForeground, darkBackground, 0.34 - contrast * 0.08),
    [darkBackground, darkCard, darkMuted],
    darkForeground,
  );

  return {
    light: {
      background: lightBackground,
      foreground: lightForeground,
      card: lightCard,
      cardSolid: lightCard,
      primary: normalized.accent,
      primaryForeground: readableForeground(normalized.accent),
      secondary: lightMuted,
      muted: lightMuted,
      mutedForeground: lightMutedForeground,
      codeBackground: mix(lightBackground, lightForeground, 0.05 + contrast * 0.05),
      accent: mix(lightBackground, normalized.accent, 0.09 + contrast * 0.06),
      border: lightBorder,
      borderStrong: mix(lightBackground, lightForeground, 0.18 + contrast * 0.12),
      sidebar: lightSidebar,
    },
    dark: {
      background: darkBackground,
      foreground: darkForeground,
      card: darkCard,
      cardSolid: darkCard,
      primary: normalized.darkAccent,
      primaryForeground: readableForeground(normalized.darkAccent),
      secondary: darkMuted,
      muted: darkMuted,
      mutedForeground: darkMutedForeground,
      codeBackground: mix(darkBackground, "#000000", 0.16 + contrast * 0.08),
      accent: mix(darkBackground, normalized.accent, 0.14 + contrast * 0.08),
      border: darkBorder,
      borderStrong: mix(darkBackground, darkForeground, 0.2 + contrast * 0.12),
      sidebar: darkSidebar,
    },
  };
}

function rebaseVariant(
  base: PaletteTokens,
  reference: GeneratedThemeVariant,
  current: GeneratedThemeVariant,
  primary: string,
  translucentSidebar: boolean,
): DerivedThemeVariant {
  const primaryChanged = primary !== base.primary.toLowerCase();
  const foreground = rebaseColor(base.foreground, reference.foreground, current.foreground);
  const card = rebaseColor(base.card, reference.card, current.card);
  const cardSolid = rebaseColor(base.cardSolid, reference.cardSolid, current.cardSolid);
  const accent = rebaseColor(base.accent, reference.accent, current.accent);
  const accentForeground = rebaseColor(
    base.accentForeground,
    reference.foreground,
    current.foreground,
  );
  const border = rebaseColor(base.border, reference.border, current.border);
  const sidebar = rebaseColor(base.sidebar, reference.sidebar, current.sidebar);

  return {
    background: rebaseColor(base.background, reference.background, current.background),
    foreground,
    card,
    cardSolid,
    cardForeground: rebaseColor(base.cardForeground, reference.foreground, current.foreground),
    tray: rebaseColor(base.tray, reference.card, current.card),
    popover: rebaseColor(base.popover, reference.cardSolid, current.cardSolid),
    popoverForeground: rebaseColor(
      base.popoverForeground,
      reference.foreground,
      current.foreground,
    ),
    primary: primaryChanged ? primary : base.primary,
    primaryForeground: primaryChanged ? readableForeground(primary) : base.primaryForeground,
    secondary: rebaseColor(base.secondary, reference.secondary, current.secondary),
    secondaryForeground: rebaseColor(
      base.secondaryForeground,
      reference.foreground,
      current.foreground,
    ),
    muted: rebaseColor(base.muted, reference.muted, current.muted),
    mutedForeground: rebaseColor(
      base.mutedForeground,
      reference.mutedForeground,
      current.mutedForeground,
    ),
    codeBackground: rebaseColor(
      base.codeBackground,
      reference.codeBackground,
      current.codeBackground,
    ),
    accent,
    accentForeground,
    border,
    borderStrong: rebaseColor(base.borderStrong, reference.borderStrong, current.borderStrong),
    buttonBorder: rebaseColor(base.buttonBorder, reference.border, current.border),
    input: rebaseColor(base.input, reference.border, current.border),
    ring: primaryChanged ? primary : base.ring,
    brandAccent: primaryChanged ? primary : base.brandAccent,
    sidebar: translucentSidebar ? setAlpha(sidebar, 0.72) : sidebar,
    sidebarForeground: rebaseColor(
      base.sidebarForeground,
      reference.foreground,
      current.foreground,
    ),
    sidebarPrimary: primaryChanged ? primary : base.sidebarPrimary,
    sidebarPrimaryForeground: primaryChanged
      ? readableForeground(primary)
      : base.sidebarPrimaryForeground,
    sidebarAccent: rebaseColor(base.sidebarAccent, reference.accent, current.accent),
    sidebarAccentForeground: rebaseColor(
      base.sidebarAccentForeground,
      reference.foreground,
      current.foreground,
    ),
    sidebarBorder: rebaseColor(base.sidebarBorder, reference.border, current.border),
    sidebarRing: primaryChanged ? primary : base.sidebarRing,
    sidebarActive: base.sidebarActive,
    sidebarActiveForeground: base.sidebarActiveForeground,
    sidebarBackground: base.sidebarBackground,
    shellBackground: base.shellBackground,
  };
}

export function deriveCustomTheme(theme: CustomTheme): DerivedCustomTheme {
  const normalized = normalizeTheme(theme) ?? DEFAULT_CUSTOM_THEME;
  const palette =
    PALETTES.find((candidate) => candidate.id === normalized.basePalette) ?? PALETTES[0];
  const referenceTheme = createCustomThemeFromPalette(palette);
  const reference = generateCustomTheme(referenceTheme);
  const current = generateCustomTheme({ ...normalized, translucentSidebar: false });

  return {
    light: rebaseVariant(
      palette.tokens.light,
      reference.light,
      current.light,
      normalized.accent,
      normalized.translucentSidebar,
    ),
    dark: rebaseVariant(
      palette.tokens.dark,
      reference.dark,
      current.dark,
      normalized.darkAccent,
      normalized.translucentSidebar,
    ),
  };
}

export function customThemeSwatches(theme: CustomTheme): {
  light: PaletteSwatch;
  dark: PaletteSwatch;
} {
  const normalized = normalizeTheme(theme) ?? DEFAULT_CUSTOM_THEME;
  const palette =
    PALETTES.find((candidate) => candidate.id === normalized.basePalette) ?? PALETTES[0];
  const reference = generateCustomTheme(createCustomThemeFromPalette(palette));
  const current = generateCustomTheme({ ...normalized, translucentSidebar: false });

  const swatch = (
    base: PaletteSwatch,
    referenceVariant: GeneratedThemeVariant,
    currentVariant: GeneratedThemeVariant,
  ): PaletteSwatch => ({
    bg: rebaseColor(base.bg, referenceVariant.background, currentVariant.background),
    card: rebaseColor(base.card, referenceVariant.cardSolid, currentVariant.cardSolid),
    accent:
      currentVariant.primary === referenceVariant.primary ? base.accent : currentVariant.primary,
    border: rebaseColor(base.border, referenceVariant.border, currentVariant.border),
    text: rebaseColor(base.text, referenceVariant.foreground, currentVariant.foreground),
  });

  return {
    light: swatch(palette.light, reference.light, current.light),
    dark: swatch(palette.dark, reference.dark, current.dark),
  };
}

export function applyCustomTheme(theme: CustomTheme): void {
  if (typeof document === "undefined") return;
  const normalized = normalizeTheme(theme) ?? DEFAULT_CUSTOM_THEME;
  const variants = deriveCustomTheme(normalized);
  const style = document.documentElement.style;
  document.documentElement.toggleAttribute(
    "data-custom-translucent-sidebar",
    normalized.translucentSidebar,
  );
  for (const mode of ["light", "dark"] as const) {
    for (const [key, token] of Object.entries(PALETTE_TOKEN_CSS_NAMES) as [
      keyof DerivedThemeVariant,
      (typeof PALETTE_TOKEN_CSS_NAMES)[keyof typeof PALETTE_TOKEN_CSS_NAMES],
    ][]) {
      style.setProperty(`--custom-${mode}-${token}`, variants[mode][key]);
    }
  }
}
