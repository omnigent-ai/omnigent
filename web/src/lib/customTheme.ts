import {
  isThemePalette,
  type PaletteMeta,
  type PaletteSwatch,
  type ThemePalette,
} from "./themePalette";

const STORAGE_KEY = "omnigent:custom-theme";
const HEX_COLOR = /^#[0-9a-f]{6}$/i;

export interface CustomTheme {
  basePalette: ThemePalette;
  accent: string;
  tint: string;
  contrast: number;
  translucentSidebar: boolean;
}

export const DEFAULT_CUSTOM_THEME: CustomTheme = {
  basePalette: "omni",
  accent: "#df3c85",
  tint: "#f3e9f4",
  contrast: 50,
  translucentSidebar: false,
};

interface Rgb {
  r: number;
  g: number;
  b: number;
}

export interface DerivedThemeVariant {
  background: string;
  foreground: string;
  card: string;
  cardSolid: string;
  primary: string;
  primaryForeground: string;
  secondary: string;
  muted: string;
  mutedForeground: string;
  codeBackground: string;
  accent: string;
  border: string;
  borderStrong: string;
  sidebar: string;
  shellBackground: string;
}

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
    tint: candidate.tint.toLowerCase(),
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
    accent: palette.light.accent.toLowerCase(),
    tint: palette.light.bg.toLowerCase(),
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

function rgba(hex: string, alpha: number): string {
  const color = hexToRgb(hex);
  return `rgba(${color.r}, ${color.g}, ${color.b}, ${alpha})`;
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

export function deriveCustomTheme(theme: CustomTheme): DerivedCustomTheme {
  const normalized = normalizeTheme(theme) ?? DEFAULT_CUSTOM_THEME;
  const contrast = normalized.contrast / 100;
  const lightBackground = mix(normalized.tint, "#fffdff", 0.79 - contrast * 0.15);
  const darkBackground = mix(normalized.tint, "#0b0b13", 0.72 + contrast * 0.1);

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
      sidebar: normalized.translucentSidebar ? rgba(lightSidebar, 0.72) : lightSidebar,
      shellBackground: `linear-gradient(155deg, ${lightCard} 0%, ${lightBackground} 62%, ${mix(lightBackground, normalized.accent, 0.06)} 100%)`,
    },
    dark: {
      background: darkBackground,
      foreground: darkForeground,
      card: darkCard,
      cardSolid: darkCard,
      primary: normalized.accent,
      primaryForeground: readableForeground(normalized.accent),
      secondary: darkMuted,
      muted: darkMuted,
      mutedForeground: darkMutedForeground,
      codeBackground: mix(darkBackground, "#000000", 0.16 + contrast * 0.08),
      accent: mix(darkBackground, normalized.accent, 0.14 + contrast * 0.08),
      border: darkBorder,
      borderStrong: mix(darkBackground, darkForeground, 0.2 + contrast * 0.12),
      sidebar: normalized.translucentSidebar ? rgba(darkSidebar, 0.72) : darkSidebar,
      shellBackground: `radial-gradient(ellipse at 18% 24%, ${rgba(normalized.accent, 0.12)} 0%, transparent 52%), linear-gradient(155deg, ${darkCard} 0%, ${darkBackground} 68%, ${mix(darkBackground, "#000000", 0.16)} 100%)`,
    },
  };
}

export function customThemeSwatches(theme: CustomTheme): {
  light: PaletteSwatch;
  dark: PaletteSwatch;
} {
  const variants = deriveCustomTheme(theme);
  return {
    light: {
      bg: variants.light.background,
      card: variants.light.cardSolid,
      accent: variants.light.primary,
      border: variants.light.border,
      text: variants.light.foreground,
    },
    dark: {
      bg: variants.dark.background,
      card: variants.dark.cardSolid,
      accent: variants.dark.primary,
      border: variants.dark.border,
      text: variants.dark.foreground,
    },
  };
}

const TOKEN_MAP = {
  background: "background",
  foreground: "foreground",
  card: "card",
  cardSolid: "card-solid",
  primary: "primary",
  primaryForeground: "primary-foreground",
  secondary: "secondary",
  muted: "muted",
  mutedForeground: "muted-foreground",
  codeBackground: "code-bg",
  accent: "accent",
  border: "border",
  borderStrong: "border-strong",
  sidebar: "sidebar",
  shellBackground: "shell-background",
} as const;

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
    for (const [key, token] of Object.entries(TOKEN_MAP) as [
      keyof DerivedThemeVariant,
      (typeof TOKEN_MAP)[keyof typeof TOKEN_MAP],
    ][]) {
      style.setProperty(`--custom-${mode}-${token}`, variants[mode][key]);
    }
  }
}
