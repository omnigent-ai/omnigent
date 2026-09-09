import { afterEach, describe, expect, it } from "vitest";
import { restoreFontPreferences } from "./restoreFontPreferences";

afterEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty("--desktop-ui-font-size");
  document.documentElement.style.removeProperty("--ui-font-family");
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

  it("applies defaults when nothing is stored (no throw)", () => {
    expect(() => restoreFontPreferences()).not.toThrow();
    // Default size 13px; family unset → no override.
    expect(document.documentElement.style.getPropertyValue("--desktop-ui-font-size")).toBe(
      "13px",
    );
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe("");
  });
});
