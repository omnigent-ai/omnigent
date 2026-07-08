import { afterEach, describe, expect, it } from "vitest";
import {
  applyThemePalette,
  DEFAULT_PALETTE,
  isThemePalette,
  PALETTES,
  readThemePalette,
  themePalettes,
  writeThemePalette,
} from "./themePalette";

const STORAGE_KEY = "omnigent:ui-theme-palette";

afterEach(() => {
  localStorage.clear();
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
    expect(isThemePalette("nope")).toBe(false);
    expect(isThemePalette(undefined)).toBe(false);
    expect(isThemePalette(42)).toBe(false);
  });

  it("sets data-theme on the document root for a non-default palette", () => {
    applyThemePalette("catppuccin");
    expect(document.documentElement.getAttribute("data-theme")).toBe("catppuccin");
  });

  it("removes data-theme for the default palette so the base tokens take over", () => {
    applyThemePalette("github");
    expect(document.documentElement.getAttribute("data-theme")).toBe("github");
    applyThemePalette(DEFAULT_PALETTE);
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("exposes swatch metadata for every selectable palette, default first", () => {
    // The picker renders one card per palette, so the metadata list and the id
    // union must stay in lockstep.
    expect(PALETTES.map((p) => p.id)).toEqual([...themePalettes]);
    expect(PALETTES[0].id).toBe(DEFAULT_PALETTE);
    for (const palette of PALETTES) {
      expect(palette.label.length).toBeGreaterThan(0);
      expect(palette.light.bg).toMatch(/^#|rgb/);
      expect(palette.dark.bg).toMatch(/^#|rgb/);
    }
  });
});
