import { afterEach, describe, expect, it } from "vitest";
import { setEmbedRoot, setEmbedScopeRoot } from "./host";
import {
  applyThemePalette,
  DEFAULT_PALETTE,
  isThemeSelection,
  isThemePalette,
  PALETTES,
  readThemePalette,
  themeSelections,
  themePalettes,
  writeThemePalette,
} from "./themePalette";

const STORAGE_KEY = "omnigent:ui-theme-palette";

type Rgb = [number, number, number];

/** Parses the `#rrggbb` and `rgba(r, g, b, a)` forms the palettes use. */
function parseColor(value: string): { rgb: Rgb; alpha: number } {
  const hex = /^#([0-9a-f]{6})$/i.exec(value);
  if (hex) {
    const rgb = [0, 2, 4].map((offset) => Number.parseInt(hex[1].slice(offset, offset + 2), 16));
    return { rgb: rgb as Rgb, alpha: 1 };
  }
  const rgba = /^rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)$/.exec(value);
  if (!rgba) throw new Error(`unsupported color: ${value}`);
  return {
    rgb: [Number(rgba[1]), Number(rgba[2]), Number(rgba[3])],
    alpha: rgba[4] === undefined ? 1 : Number(rgba[4]),
  };
}

/** The colour actually painted when `top` is drawn over an opaque `under`. */
function composite(top: { rgb: Rgb; alpha: number }, under: Rgb): Rgb {
  return under.map((channel, i) => channel + (top.rgb[i] - channel) * top.alpha) as Rgb;
}

const linear = (channel: number) => {
  const value = channel / 255;
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
};

function luminance([r, g, b]: Rgb): number {
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function contrast(first: Rgb, second: Rgb): number {
  const [high, low] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (high + 0.05) / (low + 0.05);
}

function lab([r, g, b]: Rgb): [number, number, number] {
  const [lr, lg, lb] = [r, g, b].map(linear);
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  const x = f((lr * 0.4124 + lg * 0.3576 + lb * 0.1805) / 0.95047);
  const y = f(lr * 0.2126 + lg * 0.7152 + lb * 0.0722);
  const z = f((lr * 0.0193 + lg * 0.1192 + lb * 0.9505) / 1.08883);
  return [116 * y - 16, 500 * (x - y), 200 * (y - z)];
}

/** CIE76 colour difference: ~2 is just noticeable, 8+ is clearly distinct. */
function deltaE(first: Rgb, second: Rgb): number {
  const [l1, a1, b1] = lab(first);
  const [l2, a2, b2] = lab(second);
  return Math.hypot(l1 - l2, a1 - a2, b1 - b2);
}

afterEach(() => {
  localStorage.clear();
  setEmbedScopeRoot(null);
  setEmbedRoot(null);
  document.documentElement.removeAttribute("data-theme");
});

describe("themePalette", () => {
  it("returns the default palette when nothing is stored", () => {
    expect(readThemePalette()).toBe(DEFAULT_PALETTE);
    expect(DEFAULT_PALETTE).toBe("omni");
  });

  it("round-trips a valid palette", () => {
    writeThemePalette("github");
    expect(readThemePalette()).toBe("github");
    expect(localStorage.getItem(STORAGE_KEY)).toBe(JSON.stringify("github"));
  });

  it("round-trips the custom theme selection", () => {
    writeThemePalette("custom");
    expect(readThemePalette()).toBe("custom");
    expect(localStorage.getItem(STORAGE_KEY)).toBe(JSON.stringify("custom"));
  });

  it("clears the key when the default is written (nothing to persist)", () => {
    writeThemePalette("dracula");
    expect(localStorage.getItem(STORAGE_KEY)).not.toBeNull();
    writeThemePalette(DEFAULT_PALETTE);
    // Storing the default just reverts to base tokens, so drop the key.
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(readThemePalette()).toBe(DEFAULT_PALETTE);
  });

  it("falls back to the default on an unknown stored id", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify("not-a-theme"));
    expect(readThemePalette()).toBe(DEFAULT_PALETTE);
  });

  it("falls back to the default on malformed JSON", () => {
    // Corrupt localStorage should not break app boot.
    localStorage.setItem(STORAGE_KEY, "}{not json");
    expect(readThemePalette()).toBe(DEFAULT_PALETTE);
  });

  it("guards known vs unknown palette ids", () => {
    expect(isThemePalette("github")).toBe(true);
    expect(isThemePalette("omni")).toBe(true);
    expect(isThemePalette("nord")).toBe(true);
    expect(isThemePalette("solarized")).toBe(true);
    expect(isThemePalette("nope")).toBe(false);
    expect(isThemePalette(undefined)).toBe(false);
    expect(isThemePalette(42)).toBe(false);
    expect(isThemePalette("custom")).toBe(false);
    expect(isThemeSelection("custom")).toBe(true);
    expect(isThemeSelection("github")).toBe(true);
    expect(isThemeSelection("nope")).toBe(false);
  });

  it("sets data-theme on the document root for a non-default palette", () => {
    applyThemePalette("catppuccin");
    expect(document.documentElement.getAttribute("data-theme")).toBe("catppuccin");
  });

  it("sets the custom data-theme when the custom configuration is selected", () => {
    applyThemePalette("custom");
    expect(document.documentElement.getAttribute("data-theme")).toBe("custom");
  });

  it("removes data-theme for the default palette so the base tokens take over", () => {
    applyThemePalette("github");
    expect(document.documentElement.getAttribute("data-theme")).toBe("github");
    applyThemePalette(DEFAULT_PALETTE);
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("stamps data-theme on both embed roots when embedded (light + dark selectors)", () => {
    // The light `:root[data-theme]` selectors match the scope root; the dark
    // `.dark[data-theme]` selectors match the inner `.dark` root — both need it.
    const scope = document.createElement("div");
    const inner = document.createElement("div");
    setEmbedScopeRoot(scope);
    setEmbedRoot(inner);
    applyThemePalette("dracula");
    expect(scope.getAttribute("data-theme")).toBe("dracula");
    expect(inner.getAttribute("data-theme")).toBe("dracula");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    applyThemePalette(DEFAULT_PALETTE);
    expect(scope.hasAttribute("data-theme")).toBe(false);
    expect(inner.hasAttribute("data-theme")).toBe(false);
  });

  it("exposes swatch metadata for every selectable palette, default first", () => {
    // The picker renders one card per palette, so the metadata list and the id
    // union must stay in lockstep.
    expect(PALETTES.map((p) => p.id)).toEqual([...themePalettes]);
    expect(themeSelections).toEqual([...themePalettes, "custom"]);
    expect(PALETTES[0].id).toBe(DEFAULT_PALETTE);
    for (const palette of PALETTES) {
      expect(palette.label.length).toBeGreaterThan(0);
      expect(palette.light.bg).toMatch(/^#|rgb/);
      expect(palette.dark.bg).toMatch(/^#|rgb/);
      expect(palette.tokens.light.shellBackground.length).toBeGreaterThan(0);
      expect(palette.tokens.dark.shellBackground.length).toBeGreaterThan(0);
    }
  });

  it("uses the canonical Solarized backgrounds", () => {
    const solarized = PALETTES.find((palette) => palette.id === "solarized");
    expect(solarized?.tokens.light.background).toBe("#fdf6e3");
    expect(solarized?.tokens.dark.background).toBe("#002b36");
    expect(solarized?.tokens.dark.shellBackground).toBe("#002b36");
  });

  it("keeps Omnigent's text selection on the brand tint, not the neutral primary", () => {
    const omni = PALETTES.find((palette) => palette.id === "omni")!;
    expect(omni.tokens.light.selectionBackground).toBe("rgba(240, 1, 150, 0.1)");
    expect(omni.tokens.light.selectionForeground).toBe("#651249");
    expect(omni.tokens.dark.selectionBackground).toBe("rgba(240, 1, 150, 0.15)");
    expect(omni.tokens.dark.selectionForeground).toBe("#f9a8d4");
  });

  it.each(PALETTES)("keeps $label selected text readable and its highlight visible", (palette) => {
    for (const mode of ["light", "dark"] as const) {
      const { background, selectionBackground, selectionForeground } = palette.tokens[mode];
      const page = parseColor(background).rgb;
      // Measure the highlight as painted (a tint composited over the page),
      // whether the palette uses an opaque pair or a translucent wash.
      const highlight = composite(parseColor(selectionBackground), page);
      const label = `${palette.label} ${mode}`;
      expect(
        contrast(parseColor(selectionForeground).rgb, highlight),
        label,
      ).toBeGreaterThanOrEqual(4.5);
      expect(deltaE(highlight, page), label).toBeGreaterThanOrEqual(8);
    }
  });
});
