import type { ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EmbeddedProvider } from "@/lib/embedded";
import { canOpenSessionWindow, useOpenSessionWindow } from "./useOpenSessionWindow";

const mocks = vi.hoisted(() => ({ isMobile: false, toastError: vi.fn() }));

vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => mocks.isMobile,
}));

vi.mock("sonner", () => ({ toast: { error: mocks.toastError } }));

const globals = window as unknown as Record<string, unknown>;

beforeEach(() => {
  mocks.isMobile = false;
  vi.clearAllMocks();
});

afterEach(() => {
  delete globals.omnigentDesktop;
  delete globals.omnigentNative;
  vi.restoreAllMocks();
});

describe("canOpenSessionWindow", () => {
  it.each([
    ["a browser", {}, true],
    [
      "a desktop shell with the window bridge",
      { omnigentDesktop: { kind: "electron", openSessionWindow: vi.fn() } },
      true,
    ],
    ["an older desktop shell", { omnigentDesktop: { kind: "electron" } }, false],
    ["the iOS app", { omnigentNative: { kind: "ios" } }, false],
    ["the Android app", { omnigentNative: { kind: "android" } }, false],
  ])("is %s → %s", (_name, bridges, expected) => {
    Object.assign(globals, bridges);
    expect(canOpenSessionWindow()).toBe(expected);
  });
});

describe("useOpenSessionWindow", () => {
  it("is unavailable on a mobile layout and inside an embedding host", () => {
    mocks.isMobile = true;
    expect(renderHook(() => useOpenSessionWindow()).result.current).toBeNull();

    mocks.isMobile = false;
    const wrapper = ({ children }: { children: ReactNode }) => (
      <EmbeddedProvider>{children}</EmbeddedProvider>
    );
    expect(renderHook(() => useOpenSessionWindow(), { wrapper }).result.current).toBeNull();
  });

  it("opens a desktop window through the shell instead of window.open", async () => {
    const openSessionWindow = vi.fn().mockResolvedValue(true);
    globals.omnigentDesktop = { kind: "electron", openSessionWindow };
    const open = vi.spyOn(window, "open");

    renderHook(() => useOpenSessionWindow()).result.current?.("conv_1");

    expect(openSessionWindow).toHaveBeenCalledWith("conv_1");
    expect(open).not.toHaveBeenCalled();
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(mocks.toastError).not.toHaveBeenCalled();
  });

  it.each([
    ["opens a popup quietly", { location: { pathname: "", href: "" }, focus: vi.fn() }, false],
    ["explains a blocked popup", null, true],
  ])("in a browser, %s", (_name, popup, warns) => {
    const open = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window | null);

    renderHook(() => useOpenSessionWindow()).result.current?.("conv_1");

    expect(open).toHaveBeenCalledWith("", "omnigent-session-conv_1", expect.any(String));
    if (warns) {
      expect(mocks.toastError).toHaveBeenCalledWith(
        "Your browser blocked the new window. Allow pop-ups for this site.",
      );
    } else {
      expect(mocks.toastError).not.toHaveBeenCalled();
    }
  });

  it("tells the user when the desktop shell can't open the window", async () => {
    globals.omnigentDesktop = { kind: "electron", openSessionWindow: () => Promise.resolve(false) };

    renderHook(() => useOpenSessionWindow()).result.current?.("conv_1");

    await waitFor(() =>
      expect(mocks.toastError).toHaveBeenCalledWith("Couldn't open a new window"),
    );
  });
});
