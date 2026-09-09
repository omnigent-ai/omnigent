import { describe, expect, it } from "vitest";
import {
  FONT_CATALOG,
  FONT_CATALOG_BY_CATEGORY,
  type FontCategory,
  getFontByFamily,
  getFontById,
} from "./fontCatalog";

const CATEGORIES: FontCategory[] = ["sans", "fixedWidth", "code"];

describe("fontCatalog — integrity", () => {
  it("has unique ids across the whole catalog", () => {
    const ids = FONT_CATALOG.map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("groups every entry under exactly its declared category", () => {
    for (const category of CATEGORIES) {
      for (const entry of FONT_CATALOG_BY_CATEGORY[category]) {
        expect(entry.category).toBe(category);
      }
    }
  });

  it("the by-category groups partition the flat catalog", () => {
    const grouped = CATEGORIES.flatMap((c) => FONT_CATALOG_BY_CATEGORY[c]);
    expect(grouped.length).toBe(FONT_CATALOG.length);
    expect(new Set(grouped)).toEqual(new Set(FONT_CATALOG));
  });

  it("populates each category the interface uses", () => {
    for (const category of CATEGORIES) {
      expect(FONT_CATALOG_BY_CATEGORY[category].length).toBeGreaterThan(0);
    }
  });

  it("carries valid source metadata for every entry", () => {
    for (const entry of FONT_CATALOG) {
      if (entry.source === "google-css2") {
        // A CSS2 entry must have a fetchable stylesheet href.
        expect(entry.cssUrl).toMatch(/^https:\/\/fonts\.googleapis\.com\/css2\?/);
        expect(entry.faces).toBeUndefined();
      } else if (entry.source === "self-hosted") {
        // A self-hosted entry must carry at least one @font-face with an https URL.
        expect(entry.faces?.length).toBeGreaterThan(0);
        for (const face of entry.faces ?? []) {
          expect(face.url).toMatch(/^https:\/\//);
        }
        expect(entry.cssUrl).toBeUndefined();
      } else {
        // Bundled: nothing to fetch.
        expect(entry.cssUrl).toBeUndefined();
        expect(entry.faces).toBeUndefined();
      }
    }
  });

  it("includes the bundled Geist Mono and a system default with no fetch", () => {
    const geist = getFontById("geist-mono");
    expect(geist?.source).toBe("bundled");
    expect(geist?.family).toBe("Geist Mono Variable");

    const system = getFontById("system-ui");
    expect(system?.source).toBe("bundled");
    // Empty family = "System default" (maps to --font-sans, nothing to load).
    expect(system?.family).toBe("");
  });

  it("includes the expected common families and Nerd Font variants", () => {
    const labels = new Set(FONT_CATALOG.map((e) => e.label));
    for (const expected of [
      "Inter",
      "Roboto",
      "JetBrains Mono",
      "Fira Code",
      "Cascadia Code",
      "JetBrainsMono Nerd Font Mono",
      "CaskaydiaCove Nerd Font Mono",
    ]) {
      expect(labels).toContain(expected);
    }
  });
});

describe("fontCatalog — lookups", () => {
  it("resolves an entry by id", () => {
    expect(getFontById("inter")?.family).toBe("Inter");
    expect(getFontById("nope")).toBeUndefined();
  });

  it("resolves a typed family name case-insensitively", () => {
    expect(getFontByFamily("Fira Code")?.id).toBe("fira-code");
    expect(getFontByFamily("fira code")?.id).toBe("fira-code");
    expect(getFontByFamily("  FIRA CODE  ")?.id).toBe("fira-code");
  });

  it("returns undefined for an empty name or a non-catalog family", () => {
    expect(getFontByFamily("")).toBeUndefined();
    expect(getFontByFamily("   ")).toBeUndefined();
    // A locally-installed font the catalog doesn't know is left to the OS.
    expect(getFontByFamily("Comic Sans MS")).toBeUndefined();
  });

  it("resolves a family shared across categories to a single entry", () => {
    // IBM Plex Mono is offered in both fixedWidth and code; family lookup must
    // still resolve deterministically (first catalog occurrence wins).
    const entry = getFontByFamily("IBM Plex Mono");
    expect(entry).toBeDefined();
    expect(entry?.family).toBe("IBM Plex Mono");
  });
});
