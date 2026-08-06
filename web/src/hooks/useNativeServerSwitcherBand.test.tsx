import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useNativeServerSwitcherBand } from "./useNativeServerSwitcherBand";

const VIEWPORT_WIDTH = 1000;

function domRect(left: number, right: number): DOMRect {
  return {
    x: left,
    y: 0,
    left,
    right,
    top: 0,
    bottom: 600,
    width: right - left,
    height: 600,
    toJSON: () => ({}),
  } as DOMRect;
}

function makeColumn(left: number, right: number): HTMLElement {
  const column = document.createElement("main");
  column.getBoundingClientRect = () => domRect(left, right);
  return column;
}

// jsdom doesn't implement elementFromPoint; frontmost tracking probes it.
function stubTopElement(resolve: () => Element | null) {
  (
    document as unknown as { elementFromPoint: (x: number, y: number) => Element | null }
  ).elementFromPoint = resolve;
}

function installAndroidBridge() {
  const setServerSwitcherBand = vi.fn();
  const setServerSwitcherHidden = vi.fn();
  (window as unknown as Record<string, unknown>).omnigentNative = {
    kind: "android",
    setBadgeCount: vi.fn(),
    notify: vi.fn().mockResolvedValue(true),
    setServerSwitcherBand,
    setServerSwitcherHidden,
  };
  return { setServerSwitcherBand, setServerSwitcherHidden };
}

beforeEach(() => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: VIEWPORT_WIDTH });
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentNative;
  delete (document as unknown as Record<string, unknown>).elementFromPoint;
  document.documentElement.removeAttribute("dir");
  document.documentElement.style.removeProperty("--omnigent-top-bar-visible");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useNativeServerSwitcherBand", () => {
  it("clamps transformed bounds to the viewport", () => {
    const { setServerSwitcherBand } = installAndroidBridge();
    const column = makeColumn(-0.5, 1000.5);
    stubTopElement(() => column);

    renderHook(() => useNativeServerSwitcherBand(column));

    expect(setServerSwitcherBand).toHaveBeenCalledWith(0, 1);
  });

  it("publishes physical bounds unchanged in RTL", () => {
    document.documentElement.dir = "rtl";
    const { setServerSwitcherBand } = installAndroidBridge();
    const column = makeColumn(100, 700);
    stubTopElement(() => column);

    renderHook(() => useNativeServerSwitcherBand(column));

    expect(setServerSwitcherBand).toHaveBeenCalledWith(0.1, 0.7);
  });

  it("hides instead of publishing the content beneath a covering overlay", () => {
    const { setServerSwitcherBand, setServerSwitcherHidden } = installAndroidBridge();
    const column = makeColumn(0, 1000);
    // A drawer owns the probe point, so the column is never frontmost.
    const overlay = document.createElement("div");
    document.body.appendChild(overlay);
    stubTopElement(() => overlay);

    renderHook(() => useNativeServerSwitcherBand(column));

    expect(setServerSwitcherBand).not.toHaveBeenCalled();
    expect(setServerSwitcherHidden).toHaveBeenLastCalledWith(true);
  });

  it("hides instead of borrowing an adjacent region for a collapsed chat column", () => {
    const { setServerSwitcherBand, setServerSwitcherHidden } = installAndroidBridge();
    const column = makeColumn(320, 320);
    stubTopElement(() => column);

    renderHook(() => useNativeServerSwitcherBand(column));

    expect(setServerSwitcherBand).not.toHaveBeenCalled();
    expect(setServerSwitcherHidden).toHaveBeenLastCalledWith(true);
  });

  it("publishes a narrow band as-is; native owns the too-small-to-fit policy", () => {
    const { setServerSwitcherBand } = installAndroidBridge();
    const column = makeColumn(320, 383);
    stubTopElement(() => column);

    renderHook(() => useNativeServerSwitcherBand(column));

    expect(setServerSwitcherBand).toHaveBeenCalledWith(0.32, 0.383);
  });

  it("clears the native placement when the tracked UI unmounts", () => {
    const { setServerSwitcherHidden } = installAndroidBridge();
    const column = makeColumn(100, 700);
    stubTopElement(() => column);
    const { unmount } = renderHook(() => useNativeServerSwitcherBand(column));
    setServerSwitcherHidden.mockClear();

    unmount();

    expect(setServerSwitcherHidden).toHaveBeenCalledOnce();
    expect(setServerSwitcherHidden).toHaveBeenCalledWith(true);
  });

  it("publishes a neutral hidden state when no tracked UI is mounted", () => {
    const { setServerSwitcherBand, setServerSwitcherHidden } = installAndroidBridge();

    renderHook(() => useNativeServerSwitcherBand(null));

    expect(setServerSwitcherBand).not.toHaveBeenCalled();
    expect(setServerSwitcherHidden).toHaveBeenLastCalledWith(true);
  });

  it("does not drive the switcher in the iOS shell, which owns its own visibility", () => {
    const setServerSwitcherBand = vi.fn();
    const setServerSwitcherHidden = vi.fn();
    (window as unknown as Record<string, unknown>).omnigentNative = {
      kind: "ios",
      setBadgeCount: vi.fn(),
      notify: vi.fn().mockResolvedValue(true),
      setServerSwitcherBand,
      setServerSwitcherHidden,
    };
    const column = makeColumn(100, 700);
    stubTopElement(() => column);

    const { unmount } = renderHook(() => useNativeServerSwitcherBand(column));
    unmount();

    expect(setServerSwitcherBand).not.toHaveBeenCalled();
    expect(setServerSwitcherHidden).not.toHaveBeenCalled();
  });
});
