import { afterEach, describe, expect, it } from "vitest";
import {
  applyCustomTheme,
  createCustomThemeFromPalette,
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
      tint: "#dce8f7",
      contrast: 72,
      translucentSidebar: true,
    };

    writeCustomTheme(theme);

    expect(readCustomTheme()).toEqual(theme);
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null")).toEqual(theme);
  });

  it("creates one editable configuration from a built-in palette", () => {
    const github = PALETTES.find((palette) => palette.id === "github");
    expect(github).toBeDefined();

    expect(createCustomThemeFromPalette(github!)).toEqual({
      basePalette: "github",
      accent: "#0969da",
      tint: "#f6f8fa",
      contrast: 50,
      translucentSidebar: false,
    });
  });

  it("derives readable light and dark variants from the same configuration", () => {
    const variants = deriveCustomTheme({
      basePalette: "omni",
      accent: "#2563eb",
      tint: "#dbeafe",
      contrast: 60,
      translucentSidebar: false,
    });

    expect(variants.light.background).toBe("#f4f7ff");
    expect(variants.dark.background).toBe("#393c47");
    expect(variants.light.primary).toBe("#2563eb");
    expect(variants.dark.primary).toBe("#2563eb");
    expect(variants.light.primaryForeground).toBe("#ffffff");
    expect(variants.dark.primaryForeground).toBe("#ffffff");
    expect(variants.light.foreground).not.toBe(variants.dark.foreground);
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
        basePalette: "omni",
        accent: "#777777",
        tint: "#ffffff",
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
      tint: "#dbeafe",
      contrast: 60,
      translucentSidebar: true,
    });

    const style = document.documentElement.style;
    expect(style.getPropertyValue("--custom-light-background")).toBe("#f4f7ff");
    expect(style.getPropertyValue("--custom-dark-background")).toBe("#393c47");
    expect(style.getPropertyValue("--custom-light-sidebar")).toMatch(/^rgba\(/);
    expect(style.getPropertyValue("--custom-dark-sidebar")).toMatch(/^rgba\(/);
    expect(document.documentElement).toHaveAttribute("data-custom-translucent-sidebar");
  });
});
