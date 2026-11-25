// Page history replays from the shell's URL changes and view probes: a shell with
// the embedded browser, and one without it.
import { afterEach, describe, expect, it, vi } from "vitest";

import { browserUrlEventsSupported, onBrowserUrlChanged, probeBrowserView } from "./nativeBridge";

function setBridge(bridge: Record<string, unknown> | undefined) {
  if (bridge)
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      ...bridge,
    };
  else delete (window as unknown as Record<string, unknown>).omnigentDesktop;
}

afterEach(() => {
  setBridge(undefined);
  vi.restoreAllMocks();
});

describe("probing a view", () => {
  const bridgeWith = (view: unknown) => ({ browserHasView: vi.fn().mockResolvedValue(view) });

  it("reads the URL the shell reports, and nothing else", async () => {
    setBridge(bridgeWith({ exists: true, url: "https://x.test/b", entryIndex: 1, entryCount: 3 }));
    await expect(probeBrowserView("conv_1")).resolves.toEqual({
      exists: true,
      url: "https://x.test/b",
    });
  });

  it("says a view is missing", async () => {
    setBridge(bridgeWith({ exists: false }));
    await expect(probeBrowserView("conv_1")).resolves.toEqual({ exists: false });
  });

  it("can't say for a live view without a URL", async () => {
    setBridge(bridgeWith({ exists: true }));
    await expect(probeBrowserView("conv_1")).resolves.toBeNull();
  });

  it("can't say on a shell without views, or when the read fails", async () => {
    setBridge({});
    await expect(probeBrowserView("conv_1")).resolves.toBeNull();
    vi.spyOn(console, "warn").mockImplementation(() => {});
    setBridge({ browserHasView: vi.fn().mockRejectedValue(new Error("gone")) });
    await expect(probeBrowserView("conv_1")).resolves.toBeNull();
  });
});

describe("URL changes", () => {
  it("reports URL changes only on shells that send them", () => {
    setBridge({});
    expect(browserUrlEventsSupported()).toBe(false);
    expect(() => onBrowserUrlChanged(vi.fn())()).not.toThrow();
    setBridge({ onBrowserUrlChanged: vi.fn(() => () => {}) });
    expect(browserUrlEventsSupported()).toBe(true);
  });
});
