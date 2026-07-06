import { afterEach, describe, expect, it } from "vitest";
import {
  applyUiFontScale,
  readUiFontSizePx,
  UI_FONT_SIZE_DEFAULT,
  UI_FONT_SIZE_MAX,
  UI_FONT_SIZE_MIN,
  writeUiFontSizePx,
} from "./uiFontPreferences";

const STORAGE_KEY = "omnigent:ui-font-size";

afterEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty("--ui-font-scale");
});

describe("uiFontPreferences", () => {
  it("returns the default when nothing is stored", () => {
    expect(readUiFontSizePx()).toBe(UI_FONT_SIZE_DEFAULT);
  });

  it("round-trips a valid size", () => {
    writeUiFontSizePx(18);
    expect(readUiFontSizePx()).toBe(18);
  });

  it("clamps a stored value above the range", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(99));
    expect(readUiFontSizePx()).toBe(UI_FONT_SIZE_MAX);
  });

  it("clamps a stored value below the range", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(4));
    expect(readUiFontSizePx()).toBe(UI_FONT_SIZE_MIN);
  });

  it("clamps out-of-range values on write", () => {
    writeUiFontSizePx(40);
    expect(readUiFontSizePx()).toBe(UI_FONT_SIZE_MAX);
    writeUiFontSizePx(2);
    expect(readUiFontSizePx()).toBe(UI_FONT_SIZE_MIN);
  });

  it("falls back to the default on malformed JSON", () => {
    // Corrupt localStorage should not break app boot.
    localStorage.setItem(STORAGE_KEY, "}{not json");
    expect(readUiFontSizePx()).toBe(UI_FONT_SIZE_DEFAULT);
  });

  it("falls back to the default on a non-numeric value", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify("large"));
    expect(readUiFontSizePx()).toBe(UI_FONT_SIZE_DEFAULT);
  });

  it("applies the size as a scale multiplier on the document root", () => {
    applyUiFontScale(20);
    // 20 / 16 base = 1.25.
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("1.25");
  });

  it("clamps before applying the scale", () => {
    applyUiFontScale(99);
    // Clamped to the 20px max → 20 / 16 = 1.25.
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("1.25");
  });
});
