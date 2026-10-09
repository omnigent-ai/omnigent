import { describe, expect, it } from "vitest";
import { createCustomThemeFromPalette, deriveCustomTheme } from "./customTheme";
import {
  DEFAULT_TERMINAL_PALETTE,
  resolveTerminalPalette,
  TERMINAL_PALETTES,
  type TerminalColors,
} from "./terminalPalettes";
import { PALETTES, themePalettes } from "./themePalette";

const ANSI_SLOTS = [
  "black",
  "red",
  "green",
  "yellow",
  "blue",
  "magenta",
  "cyan",
  "white",
  "brightBlack",
  "brightRed",
  "brightGreen",
  "brightYellow",
  "brightBlue",
  "brightMagenta",
  "brightCyan",
  "brightWhite",
] as const satisfies readonly (keyof TerminalColors)[];

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((at) => {
    const value = Number.parseInt(hex.slice(at, at + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(first: string, second: string): number {
  const [lighter, darker] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

const variants = themePalettes.flatMap((palette) =>
  (["light", "dark"] as const).map((mode) => ({
    palette,
    mode,
    colors: TERMINAL_PALETTES[palette][mode],
  })),
);

describe("TERMINAL_PALETTES", () => {
  it.each(variants)("$palette $mode defines every slot as a solid hex color", ({ colors }) => {
    for (const slot of [...ANSI_SLOTS, "background", "foreground", "cursor"] as const) {
      expect(colors[slot], slot).toMatch(/^#[0-9a-f]{6}$/);
    }
    expect(colors.cursorAccent).toBe(colors.background);
  });

  it.each(variants)("$palette $mode keeps body text readable", ({ colors }) => {
    expect(contrast(colors.foreground, colors.background)).toBeGreaterThanOrEqual(4);
  });

  it.each(variants.filter(({ mode }) => mode === "light"))(
    "$palette light paints ANSI white as dark text",
    ({ colors }) => {
      // Dark-assuming CLIs print primary text in white / bright-white; on a
      // light canvas those must stay readable, not vanish into the background.
      expect(contrast(colors.white, colors.background)).toBeGreaterThanOrEqual(4);
      expect(contrast(colors.brightWhite, colors.background)).toBeGreaterThanOrEqual(4);
      expect(luminance(colors.brightWhite)).toBeLessThan(luminance(colors.background));
    },
  );

  it("follows each palette's canvas in dark mode", () => {
    // Every built-in dark palette's upstream terminal background is the app's
    // own page background, so the terminal reads as part of the theme.
    for (const palette of PALETTES.filter(({ id }) => id !== "omni")) {
      expect(TERMINAL_PALETTES[palette.id].dark.background).toBe(palette.tokens.dark.background);
    }
  });

  it("reuses the app's selection wash for upstream palettes", () => {
    for (const palette of PALETTES.filter(({ id }) => id !== "omni")) {
      expect(TERMINAL_PALETTES[palette.id].light.selectionBackground).toBe(
        palette.tokens.light.selectionBackground,
      );
      expect(TERMINAL_PALETTES[palette.id].dark.selectionBackground).toBe(
        palette.tokens.dark.selectionBackground,
      );
    }
  });
});

describe("resolveTerminalPalette", () => {
  it("returns the built-in palette for a named selection", () => {
    expect(resolveTerminalPalette("nord")).toBe(TERMINAL_PALETTES.nord);
    expect(resolveTerminalPalette("omni")).toBe(DEFAULT_TERMINAL_PALETTE);
  });

  it.each(PALETTES)("matches $label exactly for an unmodified custom theme", (palette) => {
    expect(resolveTerminalPalette("custom", createCustomThemeFromPalette(palette))).toEqual(
      TERMINAL_PALETTES[palette.id],
    );
  });

  it("shifts the canvas with a custom tint and keeps the base ANSI table", () => {
    const base = createCustomThemeFromPalette(
      PALETTES.find((palette) => palette.id === "gruvbox") ?? PALETTES[0],
    );
    const custom = { ...base, tint: "#e0f0ff", darkTint: "#102030" };
    const resolved = resolveTerminalPalette("custom", custom);
    const derived = deriveCustomTheme(custom);

    for (const mode of ["light", "dark"] as const) {
      const colors = resolved[mode];
      expect(colors.background).not.toBe(TERMINAL_PALETTES.gruvbox[mode].background);
      expect(colors.background).toMatch(/^#[0-9a-f]{6}$/);
      expect(colors.cursorAccent).toBe(colors.background);
      for (const slot of ANSI_SLOTS) {
        expect(colors[slot]).toBe(TERMINAL_PALETTES.gruvbox[mode][slot]);
      }
    }
    expect(resolved.dark.background).toBe(derived.dark.background);
  });

  it("uses a custom accent's selection wash", () => {
    const base = createCustomThemeFromPalette(PALETTES[0]);
    const custom = { ...base, accent: "#2563eb", darkAccent: "#2563eb" };
    const resolved = resolveTerminalPalette("custom", custom);
    const derived = deriveCustomTheme(custom);

    expect(resolved.light.selectionBackground).toBe(derived.light.selectionBackground);
    expect(resolved.dark.selectionBackground).toBe(derived.dark.selectionBackground);
  });
});
