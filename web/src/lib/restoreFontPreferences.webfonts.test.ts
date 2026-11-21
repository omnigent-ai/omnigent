import { waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FALLBACK_SERVER_INFO } from "./capabilities";
import {
  applyWebFontCapability,
  applyWebFontCapabilityFromServerInfo,
  restoreFontPreferences,
} from "./restoreFontPreferences";
import { resetFontLoaderForTests } from "./webFontLoader";

afterEach(() => {
  resetFontLoaderForTests();
  localStorage.clear();
  document.documentElement.style.removeProperty("--ui-font-family");
  document.documentElement.style.removeProperty("--ui-mono-font-family");
});

describe("restoreFontPreferences webfonts gate", () => {
  it("does not load saved catalog fonts before capabilities are known", async () => {
    vi.resetModules();
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Inter"));
    const { restoreFontPreferences: restore } = await import("./restoreFontPreferences");

    restore();

    expect(document.querySelector("link[data-omnigent-font]")).toBeNull();
    expect(document.querySelector("style[data-omnigent-font]")).toBeNull();
  });

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

  it("does not load a saved catalog font when settled info disables webfonts", () => {
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Inter"));
    applyWebFontCapabilityFromServerInfo({
      ...FALLBACK_SERVER_INFO,
      features: { webfonts: false },
    });

    expect(document.querySelector("link[data-omnigent-font]")).toBeNull();
  });

  it("loads a saved catalog font when settled info enables webfonts", async () => {
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Inter"));
    applyWebFontCapabilityFromServerInfo({
      ...FALLBACK_SERVER_INFO,
      features: { webfonts: true },
    });

    await waitFor(() => {
      expect(
        document.querySelector('link[data-omnigent-font][href*="fonts.googleapis.com/css2"]'),
      ).not.toBeNull();
    });
  });

  it("loads saved catalog fonts after an off-mode restore is enabled", async () => {
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Roboto"));
    applyWebFontCapabilityFromServerInfo({
      ...FALLBACK_SERVER_INFO,
      features: { webfonts: false },
    });
    restoreFontPreferences();
    expect(document.querySelector("link[data-omnigent-font]")).toBeNull();

    applyWebFontCapabilityFromServerInfo({
      ...FALLBACK_SERVER_INFO,
      features: { webfonts: true },
    });

    await waitFor(() => {
      expect(
        document.querySelector('link[data-omnigent-font][href*="family=Roboto"]'),
      ).not.toBeNull();
    });
  });
});
