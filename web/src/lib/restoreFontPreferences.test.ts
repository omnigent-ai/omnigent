import { afterEach, describe, expect, it, vi } from "vitest";
import { restoreFontPreferences } from "./restoreFontPreferences";
import { loadFontByFamily } from "./webFontLoader";

// Spy on the loader so we can assert a saved family's webfont load is kicked off
// on boot, per role, without depending on jsdom's font machinery.
vi.mock("./webFontLoader", () => ({
  loadFontByFamily: vi.fn(() => ({ entry: undefined, ready: Promise.resolve(false) })),
}));

const mockLoadFontByFamily = vi.mocked(loadFontByFamily);

afterEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty("--desktop-ui-font-size");
  document.documentElement.style.removeProperty("--ui-font-family");
  document.documentElement.style.removeProperty("--ui-mono-font-family");
  vi.clearAllMocks();
});

describe("restoreFontPreferences", () => {
  it("applies the saved UI size + family to the document root on boot", () => {
    localStorage.setItem("omnigent:ui-font-size", JSON.stringify(15));
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Inter"));

    restoreFontPreferences();

    expect(document.documentElement.style.getPropertyValue("--desktop-ui-font-size")).toBe(
      "15px",
    );
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe(
      "Inter, var(--font-sans)",
    );
  });

  it("restores the saved fixed-width family on boot: applies its CSS var and loads it", () => {
    localStorage.setItem("omnigent:fixed-width-font-family", JSON.stringify("IBM Plex Mono"));

    restoreFontPreferences();

    // The fixed-width family lands on its own CSS var with the mono fallback
    // appended — not cross-wired to the UI var.
    expect(document.documentElement.style.getPropertyValue("--ui-mono-font-family")).toBe(
      "IBM Plex Mono, var(--font-mono)",
    );
    // And its webfont load is kicked off under the fixedWidth category so a
    // catalog family is fetched on boot, not only on the next Settings change.
    expect(mockLoadFontByFamily).toHaveBeenCalledWith("IBM Plex Mono", "fixedWidth");
  });

  it("applies defaults when nothing is stored (no throw)", () => {
    expect(() => restoreFontPreferences()).not.toThrow();
    // Default size 13px; families unset → no overrides.
    expect(document.documentElement.style.getPropertyValue("--desktop-ui-font-size")).toBe(
      "13px",
    );
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe("");
    expect(document.documentElement.style.getPropertyValue("--ui-mono-font-family")).toBe("");
  });
});
