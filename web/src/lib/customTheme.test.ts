import { afterEach, describe, expect, it } from "vitest";
import {
  applyCustomTheme,
  createCustomThemeFromPalette,
  customThemeSwatches,
  DEFAULT_CUSTOM_THEME,
  deriveCustomTheme,
  readCustomTheme,
  writeCustomTheme,
} from "./customTheme";
import { PALETTES } from "./themePalette";

const STORAGE_KEY = "omnigent:custom-theme";

afterEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-custom-translucent-sidebar");
  for (const property of Array.from(document.documentElement.style)) {
    if (property.startsWith("--custom-")) {
      document.documentElement.style.removeProperty(property);
    }
  }
});

describe("customTheme", () => {
  it("returns a safe default when no valid preference is stored", () => {
    expect(readCustomTheme()).toEqual(DEFAULT_CUSTOM_THEME);

    localStorage.setItem(STORAGE_KEY, JSON.stringify({ accent: "red" }));
    expect(readCustomTheme()).toEqual(DEFAULT_CUSTOM_THEME);
  });

  it("round-trips a valid shared custom-theme configuration", () => {
    const theme = {
      basePalette: "github" as const,
      accent: "#1267d6",
      darkAccent: "#238636",
      tint: "#dce8f7",
      darkTint: "#0d1117",
      contrast: 72,
      translucentSidebar: true,
    };

    writeCustomTheme(theme);

    expect(readCustomTheme()).toEqual(theme);
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null")).toEqual(theme);
  });

  it("restores the dark tint for legacy saved themes", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        basePalette: "github",
        accent: "#1f883d",
        tint: "#f6f8fa",
        contrast: 51,
        translucentSidebar: false,
      }),
    );

    expect(readCustomTheme().darkTint).toBe("#0d1117");
  });

  it("restores the dark accent for legacy saved themes", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        basePalette: "github",
        accent: "#1f883d",
        tint: "#f6f8fa",
        darkTint: "#0d1117",
        contrast: 51,
        translucentSidebar: false,
      }),
    );

    expect(readCustomTheme().darkAccent).toBe("#238636");
  });

  it("creates one editable configuration from a built-in palette", () => {
    const github = PALETTES.find((palette) => palette.id === "github");
    expect(github).toBeDefined();

    expect(createCustomThemeFromPalette(github!)).toEqual({
      basePalette: "github",
      accent: "#1f883d",
      darkAccent: "#238636",
      tint: "#f6f8fa",
      darkTint: "#0d1117",
      contrast: 50,
      translucentSidebar: false,
    });
  });

  it.each(PALETTES)("uses the exact $label tokens at contrast 50", (palette) => {
    const theme = createCustomThemeFromPalette(palette);
    const variants = deriveCustomTheme(theme);

    expect(variants.light).toEqual(palette.tokens.light);
    expect(variants.dark).toEqual(palette.tokens.dark);
  });

  it.each(PALETTES)("restores the exact $label tokens after changing contrast", (palette) => {
    const theme = createCustomThemeFromPalette(palette);

    const adjusted = deriveCustomTheme({ ...theme, contrast: 68 });

    expect(adjusted.light).not.toEqual(palette.tokens.light);
    expect(adjusted.dark).not.toEqual(palette.tokens.dark);
    expect(deriveCustomTheme({ ...theme, contrast: 50 })).toEqual(palette.tokens);
  });

  it("keeps Omnigent's selected-session colors after contrast changes", () => {
    const theme = createCustomThemeFromPalette(PALETTES[0]);
    const variants = deriveCustomTheme({ ...theme, contrast: 53 });

    expect(variants.light.sidebarActive).toBe("rgba(240, 1, 150, 0.1)");
    expect(variants.light.sidebarActiveForeground).toBe("#651249");
    expect(variants.dark.sidebarActive).toBe("rgba(240, 1, 150, 0.15)");
    expect(variants.dark.sidebarActiveForeground).toBe("#f472b6");
  });

  it.each(PALETTES)("keeps the exact $label preview at contrast 50", (palette) => {
    const swatches = customThemeSwatches(createCustomThemeFromPalette(palette));

    expect(swatches).toEqual({ light: palette.light, dark: palette.dark });
  });

  it("shows explicitly customized accents in the preview", () => {
    const palette = PALETTES.find((candidate) => candidate.id === "omni")!;
    const theme = createCustomThemeFromPalette(palette);
    const swatches = customThemeSwatches({
      ...theme,
      accent: "#2563eb",
      darkAccent: "#2563eb",
    });

    expect(swatches.light.accent).toBe("#2563eb");
    expect(swatches.dark.accent).toBe("#2563eb");
  });

  it("derives readable light and dark variants from the same configuration", () => {
    const variants = deriveCustomTheme({
      basePalette: "omni",
      accent: "#2563eb",
      darkAccent: "#2563eb",
      tint: "#dbeafe",
      darkTint: "#160e24",
      contrast: 60,
      translucentSidebar: false,
    });

    expect(variants.light.background).not.toBe(PALETTES[0].tokens.light.background);
    expect(variants.dark.background).toBe("#160e24");
    expect(variants.light.primary).toBe("#2563eb");
    expect(variants.dark.primary).toBe("#2563eb");
    expect(variants.light.primaryForeground).toBe("#ffffff");
    expect(variants.dark.primaryForeground).toBe("#ffffff");
    expect(variants.light.foreground).not.toBe(variants.dark.foreground);
    expect(variants.light.shellBackground).toBe(PALETTES[0].tokens.light.shellBackground);
    expect(variants.dark.shellBackground).toBe(PALETTES[0].tokens.dark.shellBackground);
  });

  it("keeps muted helper text at WCAG AA contrast for every allowed contrast setting", () => {
    const channel = (hex: string, offset: number) => {
      const value = Number.parseInt(hex.slice(offset, offset + 2), 16) / 255;
      return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    };
    const luminance = (hex: string) =>
      channel(hex, 1) * 0.2126 + channel(hex, 3) * 0.7152 + channel(hex, 5) * 0.0722;
    const ratio = (first: string, second: string) => {
      const lighter = Math.max(luminance(first), luminance(second));
      const darker = Math.min(luminance(first), luminance(second));
      return (lighter + 0.05) / (darker + 0.05);
    };

    for (const contrast of [0, 50, 100]) {
      const variants = deriveCustomTheme({
        basePalette: "github",
        accent: "#777777",
        darkAccent: "#777777",
        tint: "#ffffff",
        darkTint: "#0d1117",
        contrast,
        translucentSidebar: false,
      });
      for (const surface of [
        variants.light.background,
        variants.light.cardSolid,
        variants.light.muted,
      ]) {
        expect(ratio(variants.light.mutedForeground, surface)).toBeGreaterThanOrEqual(4.5);
      }
      for (const surface of [
        variants.dark.background,
        variants.dark.cardSolid,
        variants.dark.muted,
      ]) {
        expect(ratio(variants.dark.mutedForeground, surface)).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("applies both mode variants as document-level custom properties", () => {
    applyCustomTheme({
      basePalette: "omni",
      accent: "#2563eb",
      darkAccent: "#2563eb",
      tint: "#dbeafe",
      darkTint: "#160e24",
      contrast: 60,
      translucentSidebar: true,
    });

    const style = document.documentElement.style;
    expect(style.getPropertyValue("--custom-light-background")).not.toBe("");
    expect(style.getPropertyValue("--custom-dark-background")).toBe("#160e24");
    expect(style.getPropertyValue("--custom-light-sidebar")).toMatch(/^rgba\(/);
    expect(style.getPropertyValue("--custom-dark-sidebar")).toMatch(/^rgba\(/);
    expect(document.documentElement).toHaveAttribute("data-custom-translucent-sidebar");
  });
});
