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
import { setEmbedRoot, setEmbedScopeRoot } from "./host";

const STORAGE_KEY = "omnigent:custom-theme";

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

afterEach(() => {
  localStorage.clear();
  setEmbedScopeRoot(null);
  setEmbedRoot(null);
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

  it("tints the sidebar active highlight with a custom accent", () => {
    const theme = createCustomThemeFromPalette(PALETTES[0]);
    const variants = deriveCustomTheme({
      ...theme,
      accent: "#2563eb",
      darkAccent: "#f59e0b",
    });

    // Background tracks the accent at low alpha, in both modes.
    expect(variants.light.sidebarActive).toBe("rgba(37, 99, 235, 0.12)");
    expect(variants.dark.sidebarActive).toBe("rgba(245, 158, 11, 0.12)");

    // Foreground reuses the rebased sidebar foreground (the default token
    // model's `var(--sidebar-foreground)`), so it stays legible whatever
    // format the base sidebar uses — not a hex-only-parser white fallback.
    expect(variants.light.sidebarActiveForeground).toBe(variants.light.sidebarForeground);
    expect(variants.dark.sidebarActiveForeground).toBe(variants.dark.sidebarForeground);
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

  it("uses each mode's accent without changing the stored accent", () => {
    const theme = { ...DEFAULT_CUSTOM_THEME, accent: "#2563eb", darkAccent: "#ffcc00" };
    writeCustomTheme(theme);
    const variants = deriveCustomTheme(theme);
    expect(variants.light.selectionBackground).toBe(theme.accent);
    expect(variants.dark.selectionBackground).toBe(theme.darkAccent);
    expect(readCustomTheme()).toEqual(theme);
    expect(variants.light.primary).toBe(theme.accent);
    expect(variants.dark.primary).toBe(theme.darkAccent);
  });

  it("recalculates selection against a changed dark tint even with an unchanged accent", () => {
    const theme = createCustomThemeFromPalette(PALETTES[0]);
    const original = deriveCustomTheme(theme).dark;
    const adjusted = deriveCustomTheme({ ...theme, darkTint: "#eeeeee" }).dark;
    expect(adjusted.primary).toBe(original.primary);
    expect(adjusted.background).toBe("#eeeeee");
    expect(adjusted.selectionBackground).not.toBe(original.selectionBackground);
    expect(adjusted.selectionBackground).not.toBe(theme.darkAccent);
    expect(adjusted.selectionForeground).toMatch(/^#(?:000000|ffffff)$/);
  });

  it("adjusts extreme accents for the final custom surfaces at every contrast setting", () => {
    const palette = PALETTES.find((candidate) => candidate.id === "github")!;
    for (const contrast of [0, 50, 100]) {
      for (const darkTint of ["#000000", "#777777", "#ffffff"]) {
        for (const accent of ["#000000", "#ffffff", "#ffff00", "#0000ff"]) {
          const variants = deriveCustomTheme({
            ...createCustomThemeFromPalette(palette),
            accent,
            darkAccent: accent,
            darkTint,
            contrast,
          });
          for (const variant of [variants.light, variants.dark]) {
            expect(
              ratio(variant.selectionBackground, variant.selectionForeground),
            ).toBeGreaterThanOrEqual(4.5);
            const canvas =
              variant === variants.dark ? ["#0d1117", "#0a0e14"] : ["#fbfcfd", "#f6f8fa"];
            const surfaces = [
              variant.background,
              variant.cardSolid,
              variant.codeBackground,
              ...canvas,
            ];
            const minimum = (color: string) =>
              Math.min(...surfaces.map((surface) => ratio(color, surface)));
            expect(minimum(variant.selectionBackground)).toBeGreaterThanOrEqual(minimum(accent));
          }
        }
      }
    }
  });

  it("balances a gray custom dark tint against the unchanged dark transcript canvas", () => {
    const palette = PALETTES.find((candidate) => candidate.id === "github")!;
    const theme = { ...createCustomThemeFromPalette(palette), darkTint: "#777777" };
    const variant = deriveCustomTheme(theme).dark;
    const surfaces = [
      variant.background,
      variant.cardSolid,
      variant.codeBackground,
      "#0d1117",
      "#0a0e14",
    ];
    const minimum = (color: string) =>
      Math.min(...surfaces.map((surface) => ratio(color, surface)));
    expect(variant.shellBackground).toBe(palette.tokens.dark.shellBackground);
    expect(minimum(variant.selectionBackground)).toBeGreaterThanOrEqual(minimum(theme.darkAccent));
    expect(ratio(variant.selectionBackground, "#0d1117")).toBeGreaterThan(2);
    expect(ratio(variant.selectionBackground, variant.selectionForeground)).toBeGreaterThanOrEqual(
      4.5,
    );
    expect(minimum(variant.selectionBackground)).toBeGreaterThanOrEqual(3);
  });

  it("keeps muted helper text at WCAG AA contrast for every allowed contrast setting", () => {
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

  it("targets the embed roots when embedded (vars on scope root, attr on both)", () => {
    // Embedded, the `--custom-*` vars go on the scope root (inherited by the
    // inner `.dark` root); the translucent attribute goes on BOTH roots so the
    // light `:root[...]` and dark `.dark[...]` selectors each match.
    const scope = document.createElement("div");
    const inner = document.createElement("div");
    setEmbedScopeRoot(scope);
    setEmbedRoot(inner);
    applyCustomTheme({
      basePalette: "omni",
      accent: "#2563eb",
      darkAccent: "#2563eb",
      tint: "#dbeafe",
      darkTint: "#160e24",
      contrast: 60,
      translucentSidebar: true,
    });
    expect(scope.style.getPropertyValue("--custom-dark-background")).toBe("#160e24");
    expect(scope).toHaveAttribute("data-custom-translucent-sidebar");
    expect(inner).toHaveAttribute("data-custom-translucent-sidebar");
    expect(document.documentElement.style.getPropertyValue("--custom-dark-background")).toBe("");
    expect(document.documentElement).not.toHaveAttribute("data-custom-translucent-sidebar");
  });
});
