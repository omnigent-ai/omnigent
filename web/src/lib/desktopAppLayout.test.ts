import { afterEach, describe, expect, it, vi } from "vitest";

import { applyDesktopAppAttribute, keepsDesktopLayout } from "./desktopAppLayout";

afterEach(() => {
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.desktopApp;
  delete document.documentElement.dataset.sessionWindow;
  delete document.documentElement.dataset.sessionWindowFor;
  window.history.replaceState(null, "", "/");
});

describe("desktop app layout", () => {
  it.each([
    ["the desktop app", { omnigentDesktop: { kind: "electron" } }, true],
    ["a browser", {}, false],
    ["the iOS app", { omnigentNative: { kind: "ios" } }, false],
    ["the Android app", { omnigentNative: { kind: "android" } }, false],
  ])("keeps the desktop layout only in %s", (_name, bridges, expected) => {
    for (const [name, bridge] of Object.entries(bridges)) vi.stubGlobal(name, bridge);

    expect(keepsDesktopLayout()).toBe(expected);
    applyDesktopAppAttribute();
    expect(document.documentElement.hasAttribute("data-desktop-app")).toBe(expected);
  });

  it.each([
    ["a browser popup", { opener: window }, true],
    ["a copied link with no opener", { opener: null }, false],
  ])("keeps the desktop layout in a popped-out session for %s", (_name, globals, expected) => {
    window.history.replaceState(null, "", "/c/conv_1?window=session");
    for (const [name, value] of Object.entries(globals)) vi.stubGlobal(name, value);

    applyDesktopAppAttribute();
    expect(document.documentElement.dataset.sessionWindow === "true").toBe(expected);
    expect(document.documentElement.dataset.sessionWindowFor).toBe(expected ? "conv_1" : undefined);
    expect(keepsDesktopLayout()).toBe(expected);
    expect(document.documentElement.hasAttribute("data-desktop-app")).toBe(expected);
  });
});
