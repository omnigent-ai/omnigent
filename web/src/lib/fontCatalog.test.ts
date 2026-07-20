import { describe, expect, it } from "vitest";
import {
  FONT_CATALOG,
  FONT_CATALOG_BY_CATEGORY,
  type FontCategory,
  fontLoadKey,
  getFontByFamily,
  getFontById,
  getFontsByFamily,
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

  it("resolves a family shared across categories deterministically", () => {
    // IBM Plex Mono is offered in both fixedWidth and code; the single-arg
    // lookup resolves to the FIRST catalog occurrence (backward-compatible).
    const entry = getFontByFamily("IBM Plex Mono");
    expect(entry).toBeDefined();
    expect(entry?.family).toBe("IBM Plex Mono");
    expect(entry?.category).toBe("fixedWidth");
  });

  it("returns every entry for a shared family via getFontsByFamily", () => {
    const matches = getFontsByFamily("IBM Plex Mono");
    expect(matches.length).toBe(2);
    expect(new Set(matches.map((e) => e.category))).toEqual(new Set(["fixedWidth", "code"]));
    // Distinct ids, so PR 2 can address each independently.
    expect(new Set(matches.map((e) => e.id)).size).toBe(2);
    expect(getFontsByFamily("Comic Sans MS")).toEqual([]);
    expect(getFontsByFamily("")).toEqual([]);
  });

  it("resolves a shared family to the requested category", () => {
    // The whole point of the category arg: the code control must get the code
    // entry, the mono control the fixedWidth one — not always the first match.
    expect(getFontByFamily("IBM Plex Mono", "code")?.category).toBe("code");
    expect(getFontByFamily("IBM Plex Mono", "code")?.id).toBe("ibm-plex-mono-code");
    expect(getFontByFamily("IBM Plex Mono", "fixedWidth")?.category).toBe("fixedWidth");
    expect(getFontByFamily("IBM Plex Mono", "fixedWidth")?.id).toBe("ibm-plex-mono");
  });

  it("falls back to the first match when the family isn't in the asked category", () => {
    // Fira Code is code-only; asking for it as sans still resolves (delivery
    // metadata is identical across a family's entries, so it still loads).
    const entry = getFontByFamily("Fira Code", "sans");
    expect(entry?.id).toBe("fira-code");
  });
});

describe("fontCatalog — fontLoadKey (resource identity)", () => {
  it("keys google-css2 entries by their stylesheet URL", () => {
    const inter = getFontById("inter");
    expect(fontLoadKey(inter!)).toBe(inter!.cssUrl);
  });

  it("gives the two IBM Plex Mono entries the SAME load key", () => {
    // Different ids, identical Google CSS2 URL → identical resource → one key,
    // so the loader dedupes them (see webFontLoader dedup test).
    const fixed = getFontById("ibm-plex-mono")!;
    const code = getFontById("ibm-plex-mono-code")!;
    expect(fixed.id).not.toBe(code.id);
    expect(fontLoadKey(fixed)).toBe(fontLoadKey(code));
  });

  it("keys self-hosted entries by their face URLs", () => {
    const nerd = getFontById("jetbrainsmono-nerd-font-mono")!;
    expect(fontLoadKey(nerd)).toContain(nerd.faces![0].url);
  });

  it("gives distinct resources distinct keys", () => {
    const keys = FONT_CATALOG.filter((e) => e.source !== "bundled").map(fontLoadKey);
    // The only intentional collision is the shared IBM Plex Mono URL.
    const dupes = keys.filter((k, i) => keys.indexOf(k) !== i);
    expect(new Set(dupes).size).toBe(1);
  });
});
