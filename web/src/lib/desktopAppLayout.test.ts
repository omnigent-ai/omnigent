import { afterEach, describe, expect, it, vi } from "vitest";

import { applyDesktopAppAttribute, keepsDesktopLayout } from "./desktopAppLayout";

afterEach(() => {
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.desktopApp;
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
});
