import { afterEach, describe, expect, it, vi } from "vitest";

import {
  isOwnSessionWindow,
  openBrowserSessionWindow,
  sessionWindowPath,
  startingSessionWindowId,
  startsAsSessionWindow,
} from "./sessionWindow";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete window.__OMNIGENT_BASE_PATH__;
  delete document.documentElement.dataset.sessionWindow;
  delete document.documentElement.dataset.sessionWindowFor;
  window.name = "";
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

  it("records the session a pop-out was opened for, under a base path too", () => {
    vi.stubGlobal("opener", window);
    window.history.replaceState(null, "", "/proxy/6767/c/conv%201?window=session");
    expect(startingSessionWindowId()).toBe("conv 1");
    window.history.replaceState(null, "", "/settings?window=session");
    expect(startingSessionWindowId()).toBeNull();
  });

  it.each([
    ["its own session while showing it", "conv_1", "conv_1", true],
    ["its own session after moving to another", "conv_1", "conv_2", false],
    ["another session", "conv_2", "conv_1", false],
  ])("treats a pop-out opening %s as itself: %s", (_name, target, shown, expected) => {
    document.documentElement.dataset.sessionWindow = "true";
    document.documentElement.dataset.sessionWindowFor = "conv_1";
    expect(isOwnSessionWindow(target, shown)).toBe(expected);
    delete document.documentElement.dataset.sessionWindow;
    expect(isOwnSessionWindow("conv_1", "conv_1")).toBe(false);
  });

  it("opens another window from a pop-out that moved away from the requested session", () => {
    window.history.replaceState(null, "", "/c/conv_2?window=session");
    window.name = "omnigent-session-conv_1";
    const popup = { location: { pathname: "blank", href: "about:blank" }, focus: vi.fn() };
    const open = vi
      .spyOn(window, "open")
      .mockImplementation((_url, name) =>
        name === window.name ? window : (popup as unknown as Window),
      );

    expect(openBrowserSessionWindow("conv_1")).toBe(true);
    expect(open).toHaveBeenCalledWith("", "omnigent-session-conv_1", "popup,width=960,height=820");
    expect(popup.location.href).toBe("/c/conv_1?window=session");
    expect(window.location.pathname).toBe("/c/conv_2");
  });

  it("keeps a pop-out's name while it still shows its session", () => {
    window.history.replaceState(null, "", "/c/conv_1?window=session");
    window.name = "omnigent-session-conv_1";
    const focus = vi.spyOn(window, "focus").mockImplementation(() => {});
    vi.spyOn(window, "open").mockImplementation((_url, name) =>
      name === window.name ? window : null,
    );

    expect(openBrowserSessionWindow("conv_1")).toBe(true);
    expect(window.name).toBe("omnigent-session-conv_1");
    expect(focus).toHaveBeenCalled();
    expect(window.location.pathname).toBe("/c/conv_1");
  });

  it("reports a blocked popup", () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    expect(openBrowserSessionWindow("conv_1")).toBe(false);
  });
});
