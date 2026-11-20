import { afterEach, describe, expect, it, vi } from "vitest";

import {
  openBrowserSessionWindow,
  sessionWindowPath,
  startsAsSessionWindow,
} from "./sessionWindow";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete window.__OMNIGENT_BASE_PATH__;
  window.history.replaceState(null, "", "/");
});

describe("session window", () => {
  it("routes to the session with the pop-out marker", () => {
    expect(sessionWindowPath("conv_1")).toBe("/c/conv_1?window=session");
    expect(sessionWindowPath("a b")).toBe("/c/a%20b?window=session");
  });

  it.each([
    ["a browser popup", "/c/conv_1?window=session", { opener: window }, true],
    [
      "the desktop app",
      "/c/conv_1?window=session",
      { omnigentDesktop: { kind: "electron" } },
      true,
    ],
    ["a copied link with no opener", "/c/conv_1?window=session", { opener: null }, false],
    ["an ordinary tab opened from another page", "/c/conv_1", { opener: window }, false],
  ])("starts as a session window only for %s", (_name, url, globals, expected) => {
    window.history.replaceState(null, "", url);
    for (const [name, value] of Object.entries(globals)) vi.stubGlobal(name, value);

    expect(startsAsSessionWindow()).toBe(expected);
  });

  it("opens a named, sized popup on the session under the deployment base path", () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    const popup = { location: { pathname: "blank", href: "about:blank" }, focus: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    expect(openBrowserSessionWindow("conv_1")).toBe(true);
    expect(open).toHaveBeenCalledWith("", "omnigent-session-conv_1", "popup,width=960,height=820");
    expect(popup.location.href).toBe("/proxy/6767/c/conv_1?window=session");
    expect(popup.focus).toHaveBeenCalled();
  });

  it("brings forward a popup already showing the session without reloading it", () => {
    const href = "http://localhost:3000/c/conv_1?window=session";
    const popup = { location: { pathname: "/c/conv_1", href }, focus: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    expect(openBrowserSessionWindow("conv_1")).toBe(true);
    expect(popup.location.href).toBe(href);
    expect(popup.focus).toHaveBeenCalled();
  });

  it("sends a popup that moved to another session back to this one", () => {
    const popup = { location: { pathname: "/c/conv_2", href: "/c/conv_2" }, focus: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    openBrowserSessionWindow("conv_1");
    expect(popup.location.href).toBe("/c/conv_1?window=session");
  });

  it("sends a popup that left for another site back to the session", () => {
    const location = {
      href: "",
      get pathname(): string {
        throw new DOMException("Blocked a frame with origin", "SecurityError");
      },
    };
    const popup = { location, focus: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    expect(openBrowserSessionWindow("conv_1")).toBe(true);
    expect(location.href).toBe("/c/conv_1?window=session");
    expect(popup.focus).toHaveBeenCalled();
  });

  it("reports a blocked popup", () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    expect(openBrowserSessionWindow("conv_1")).toBe(false);
  });
});
