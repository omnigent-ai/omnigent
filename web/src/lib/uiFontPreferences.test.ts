import { afterEach, describe, expect, it } from "vitest";
import {
  applyUiFontFamily,
  applyUiFontScale,
  readUiFontFamily,
  readUiFontSizePx,
  UI_FONT_FAMILY_DEFAULT,
  UI_FONT_SIZE_DEFAULT,
  UI_FONT_SIZE_MAX,
  UI_FONT_SIZE_MIN,
  writeUiFontFamily,
  writeUiFontSizePx,
} from "./uiFontPreferences";

const STORAGE_KEY = "omnigent:ui-font-size";
const FAMILY_STORAGE_KEY = "omnigent:ui-font-family";

afterEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty("--ui-font-scale");
  document.documentElement.style.removeProperty("--ui-font-family");
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

describe("uiFontPreferences — family", () => {
  it("returns the empty default when nothing is stored", () => {
    expect(readUiFontFamily()).toBe(UI_FONT_FAMILY_DEFAULT);
    expect(readUiFontFamily()).toBe("");
  });

  it("round-trips a valid family name", () => {
    writeUiFontFamily("Inter");
    expect(readUiFontFamily()).toBe("Inter");
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).toBe(JSON.stringify("Inter"));
  });

  it("preserves spaces, commas and quotes in a font stack", () => {
    // A multi-family stack must survive normalization intact (the guard only
    // strips declaration-breaking chars, not the punctuation stacks rely on).
    writeUiFontFamily('"Times New Roman", serif');
    expect(readUiFontFamily()).toBe('"Times New Roman", serif');
  });

  it("trims surrounding whitespace", () => {
    writeUiFontFamily("  Georgia  ");
    expect(readUiFontFamily()).toBe("Georgia");
  });

  it("clears the preference when written empty or whitespace-only", () => {
    writeUiFontFamily("Inter");
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).not.toBeNull();
    writeUiFontFamily("   ");
    // Empty input removes the key rather than storing a blank string.
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).toBeNull();
    expect(readUiFontFamily()).toBe("");
  });

  it("strips characters that could break the CSS declaration", () => {
    // `;{}` and control chars can't be allowed to escape the custom-property
    // value; everything else about the name (here the leading font) is kept.
    writeUiFontFamily("Arial;}body{");
    expect(readUiFontFamily()).toBe("Arialbody");
  });

  it("falls back to the default on a value longer than the cap", () => {
    writeUiFontFamily("x".repeat(200));
    expect(readUiFontFamily()).toBe(UI_FONT_FAMILY_DEFAULT);
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).toBeNull();
  });

  it("falls back to the default on malformed JSON", () => {
    // Corrupt localStorage should not break app boot.
    localStorage.setItem(FAMILY_STORAGE_KEY, "}{not json");
    expect(readUiFontFamily()).toBe(UI_FONT_FAMILY_DEFAULT);
  });

  it("falls back to the default on a non-string value", () => {
    localStorage.setItem(FAMILY_STORAGE_KEY, JSON.stringify(42));
    expect(readUiFontFamily()).toBe(UI_FONT_FAMILY_DEFAULT);
  });

  it("applies the family with the system stack appended as a fallback", () => {
    // The system stack is appended so an uninstalled/partial name degrades to
    // the app's default sans, not the browser's default serif.
    applyUiFontFamily("Inter");
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe(
      "Inter, var(--font-sans)",
    );
  });

  it("removes the custom property when applied empty (System default)", () => {
    applyUiFontFamily("Inter");
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe(
      "Inter, var(--font-sans)",
    );
    applyUiFontFamily("");
    // Removing the property lets the html rule fall back to var(--font-sans).
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe("");
  });
});
