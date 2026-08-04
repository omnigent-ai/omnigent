import { afterEach, describe, expect, it } from "vitest";
import {
  createCssFontFamilyPreference,
  FONT_FAMILY_DEFAULT,
  normalizeFontFamily,
  readStoredFontFamily,
  writeStoredFontFamily,
} from "./cssFontFamilyPreference";

const KEY = "omnigent:test-font-family";
const CSS_VAR = "--test-font-family";

afterEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty(CSS_VAR);
});

describe("cssFontFamilyPreference — normalizeFontFamily", () => {
  it("returns the empty default for non-strings", () => {
    expect(normalizeFontFamily(42)).toBe(FONT_FAMILY_DEFAULT);
    expect(normalizeFontFamily(null)).toBe("");
    expect(normalizeFontFamily(undefined)).toBe("");
  });

  it("trims surrounding whitespace", () => {
    expect(normalizeFontFamily("  Georgia  ")).toBe("Georgia");
  });

  it("preserves the punctuation a font stack relies on", () => {
    expect(normalizeFontFamily('"Times New Roman", serif')).toBe('"Times New Roman", serif');
  });

  it("strips characters that could break the CSS declaration", () => {
    expect(normalizeFontFamily("Arial;}body{")).toBe("Arialbody");
  });

  it("collapses an over-long value to the default", () => {
    expect(normalizeFontFamily("x".repeat(200))).toBe(FONT_FAMILY_DEFAULT);
  });
});

describe("cssFontFamilyPreference — readStoredFontFamily / writeStoredFontFamily", () => {
  it("round-trips a valid family name and returns the normalized write value", () => {
    expect(writeStoredFontFamily(KEY, "Inter")).toBe("Inter");
    expect(readStoredFontFamily(KEY)).toBe("Inter");
    expect(localStorage.getItem(KEY)).toBe(JSON.stringify("Inter"));
  });

  it("clears the key when written empty or whitespace-only", () => {
    writeStoredFontFamily(KEY, "Inter");
    expect(localStorage.getItem(KEY)).not.toBeNull();
    expect(writeStoredFontFamily(KEY, "   ")).toBe("");
    expect(localStorage.getItem(KEY)).toBeNull();
    expect(readStoredFontFamily(KEY)).toBe("");
  });

  it("returns the default when nothing is stored", () => {
    expect(readStoredFontFamily(KEY)).toBe(FONT_FAMILY_DEFAULT);
  });

  it("falls back to the default on malformed JSON", () => {
    localStorage.setItem(KEY, "}{not json");
    expect(readStoredFontFamily(KEY)).toBe(FONT_FAMILY_DEFAULT);
  });

  it("falls back to the default on a non-string value", () => {
    localStorage.setItem(KEY, JSON.stringify(42));
    expect(readStoredFontFamily(KEY)).toBe(FONT_FAMILY_DEFAULT);
  });
});

describe("cssFontFamilyPreference — createCssFontFamilyPreference", () => {
  const pref = createCssFontFamilyPreference({
    key: KEY,
    cssVar: CSS_VAR,
    fallback: "var(--font-sans)",
    category: "sans",
  });

  it("read/write round-trip through the configured key", () => {
    expect(pref.write("Inter")).toBe("Inter");
    expect(pref.read()).toBe("Inter");
    expect(localStorage.getItem(KEY)).toBe(JSON.stringify("Inter"));
  });

  it("applies the family with the fallback stack appended", () => {
    pref.apply("Inter");
    expect(document.documentElement.style.getPropertyValue(CSS_VAR)).toBe(
      "Inter, var(--font-sans)",
    );
  });

  it("removes the property when applied empty (default)", () => {
    pref.apply("Inter");
    expect(document.documentElement.style.getPropertyValue(CSS_VAR)).toBe(
      "Inter, var(--font-sans)",
    );
    pref.apply("");
    expect(document.documentElement.style.getPropertyValue(CSS_VAR)).toBe("");
  });

  it("uses each preference's own fallback stack", () => {
    const mono = createCssFontFamilyPreference({
      key: "omnigent:test-mono-family",
      cssVar: "--test-mono-family",
      fallback: "ui-monospace, monospace",
      category: "fixedWidth",
    });
    mono.apply("Roboto Mono");
    expect(document.documentElement.style.getPropertyValue("--test-mono-family")).toBe(
      "Roboto Mono, ui-monospace, monospace",
    );
    document.documentElement.style.removeProperty("--test-mono-family");
  });
});
