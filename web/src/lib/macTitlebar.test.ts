import { afterEach, describe, expect, it, vi } from "vitest";

import { withMacTitlebarClearance } from "./macTitlebar";

const MAC_ELECTRON_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Electron/40.0.0";

function useShell({ electron, userAgent }: { electron: boolean; userAgent: string }): void {
  if (electron) {
    (window as unknown as Record<string, unknown>).omnigentDesktop = { kind: "electron" };
  }
  vi.spyOn(window.navigator, "userAgent", "get").mockReturnValue(userAgent);
}

describe("withMacTitlebarClearance", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    document.documentElement.style.removeProperty("--omnigent-electron-zoom");
  });

  it("passes padding through outside the macOS shell", () => {
    useShell({ electron: true, userAgent: "Mozilla/5.0 (Windows NT 10.0) Electron/40.0.0" });
    expect(withMacTitlebarClearance(8)).toBe(8);
    expect(withMacTitlebarClearance()).toBe(0);
  });

  it("passes padding through in a plain macOS browser", () => {
    useShell({ electron: false, userAgent: MAC_ELECTRON_USER_AGENT });
    expect(withMacTitlebarClearance({ left: 4 })).toEqual({ left: 4 });
  });

  it("adds the title-bar height to the top edge in the macOS shell", () => {
    useShell({ electron: true, userAgent: MAC_ELECTRON_USER_AGENT });
    expect(withMacTitlebarClearance()).toEqual({ top: 48, right: 0, bottom: 0, left: 0 });
    expect(withMacTitlebarClearance(8)).toEqual({ top: 56, right: 8, bottom: 8, left: 8 });
    expect(withMacTitlebarClearance({ left: 4 })).toEqual({ left: 4, top: 48 });
  });

  it("scales the clearance with the page zoom", () => {
    useShell({ electron: true, userAgent: MAC_ELECTRON_USER_AGENT });
    document.documentElement.style.setProperty("--omnigent-electron-zoom", "1.5");
    expect(withMacTitlebarClearance()).toEqual({ top: 32, right: 0, bottom: 0, left: 0 });
  });
});
