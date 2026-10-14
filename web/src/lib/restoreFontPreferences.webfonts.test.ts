import { waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { applyWebFontCapability, restoreFontPreferences } from "./restoreFontPreferences";
import { resetFontLoaderForTests } from "./webFontLoader";

afterEach(() => {
  resetFontLoaderForTests();
  localStorage.clear();
  document.documentElement.style.removeProperty("--ui-font-family");
  document.documentElement.style.removeProperty("--ui-mono-font-family");
});

describe("restoreFontPreferences webfonts gate", () => {
  it("restores saved CSS without contacting font CDNs while the capability is off", () => {
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Inter"));
    localStorage.setItem("omnigent:fixed-width-font-family", JSON.stringify("IBM Plex Mono"));
    localStorage.setItem("omnigent:code-font-family", JSON.stringify("JetBrains Mono"));
    applyWebFontCapability(false);
    restoreFontPreferences();

    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe(
      "Inter, var(--font-sans)",
    );
    expect(document.documentElement.style.getPropertyValue("--ui-mono-font-family")).toBe(
      "IBM Plex Mono, var(--font-mono)",
    );
    expect(document.querySelector("link[data-omnigent-font]")).toBeNull();
    expect(document.querySelector("style[data-omnigent-font]")).toBeNull();
  });

  it("loads a saved catalog font when the capability is enabled", async () => {
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Inter"));
    applyWebFontCapability(true);

    await waitFor(() => {
      expect(
        document.querySelector('link[data-omnigent-font][href*="fonts.googleapis.com/css2"]'),
      ).not.toBeNull();
    });
  });

  it("loads saved catalog fonts after an off-mode restore is enabled", async () => {
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Roboto"));
    applyWebFontCapability(false);
    restoreFontPreferences();
    expect(document.querySelector("link[data-omnigent-font]")).toBeNull();

    applyWebFontCapability(true);

    await waitFor(() => {
      expect(
        document.querySelector('link[data-omnigent-font][href*="family=Roboto"]'),
      ).not.toBeNull();
    });
  });
});
