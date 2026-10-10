import { afterEach, describe, expect, it } from "vitest";
import {
  applyFixedWidthFontFamily,
  FIXED_WIDTH_FONT_FAMILY_DEFAULT,
  FIXED_WIDTH_FONT_FAMILY_FALLBACK,
  readFixedWidthFontFamily,
  writeFixedWidthFontFamily,
} from "./fixedWidthFontPreferences";

const FAMILY_STORAGE_KEY = "omnigent:fixed-width-font-family";

afterEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty("--ui-mono-font-family");
});

describe("fixedWidthFontPreferences", () => {
  it("returns the empty default when nothing is stored", () => {
    expect(readFixedWidthFontFamily()).toBe(FIXED_WIDTH_FONT_FAMILY_DEFAULT);
    expect(readFixedWidthFontFamily()).toBe("");
  });

  it("round-trips a valid family under the dedicated key", () => {
    writeFixedWidthFontFamily("IBM Plex Mono");
    expect(readFixedWidthFontFamily()).toBe("IBM Plex Mono");
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).toBe(JSON.stringify("IBM Plex Mono"));
  });

  it("trims surrounding whitespace", () => {
    writeFixedWidthFontFamily("  Roboto Mono  ");
    expect(readFixedWidthFontFamily()).toBe("Roboto Mono");
  });

  it("clears the preference when written empty or whitespace-only", () => {
    writeFixedWidthFontFamily("IBM Plex Mono");
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).not.toBeNull();
    writeFixedWidthFontFamily("   ");
    // Empty input removes the key rather than storing a blank string.
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).toBeNull();
    expect(readFixedWidthFontFamily()).toBe("");
  });

  it("strips characters that could break the CSS declaration", () => {
    writeFixedWidthFontFamily("Roboto Mono;}body{");
    expect(readFixedWidthFontFamily()).toBe("Roboto Monobody");
  });

  it("falls back to the default on a value longer than the cap", () => {
    writeFixedWidthFontFamily("x".repeat(200));
    expect(readFixedWidthFontFamily()).toBe(FIXED_WIDTH_FONT_FAMILY_DEFAULT);
    expect(localStorage.getItem(FAMILY_STORAGE_KEY)).toBeNull();
  });

  it("falls back to the default on malformed JSON", () => {
    // Corrupt localStorage should not break app boot.
    localStorage.setItem(FAMILY_STORAGE_KEY, "}{not json");
    expect(readFixedWidthFontFamily()).toBe(FIXED_WIDTH_FONT_FAMILY_DEFAULT);
  });

  it("applies the family with the mono stack appended as a fallback", () => {
    // The mono stack is appended so an uninstalled/partial name degrades to the
    // app's default mono, not a browser default.
    applyFixedWidthFontFamily("IBM Plex Mono");
    expect(document.documentElement.style.getPropertyValue("--ui-mono-font-family")).toBe(
      `IBM Plex Mono, ${FIXED_WIDTH_FONT_FAMILY_FALLBACK}`,
    );
  });

  it("removes the custom property when applied empty (Default)", () => {
    applyFixedWidthFontFamily("IBM Plex Mono");
    expect(document.documentElement.style.getPropertyValue("--ui-mono-font-family")).toBe(
      `IBM Plex Mono, ${FIXED_WIDTH_FONT_FAMILY_FALLBACK}`,
    );
    applyFixedWidthFontFamily("");
    // Removing the property lets the .font-mono rule fall back to var(--font-mono).
    expect(document.documentElement.style.getPropertyValue("--ui-mono-font-family")).toBe("");
  });
});
