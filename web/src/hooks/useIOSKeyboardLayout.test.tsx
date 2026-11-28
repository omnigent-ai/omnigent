import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useIOSNativeKeyboardInset } from "./useIOSNativeKeyboardInset";
import { useIOSViewportLock } from "./useIOSViewportLock";

const HEIGHT_VAR = "--omnigent-viewport-height";
let viewport: EventTarget & { height: number; offsetTop: number };
let nativeViewport: { width: number; height: number } | null;
let nativeListeners: Set<() => void>;
let setDocumentScrollEnabled: ReturnType<typeof vi.fn>;

function useKeyboardLayout() {
  useIOSViewportLock();
  return useIOSNativeKeyboardInset();
}

function shellHeight() {
  return document.documentElement.style.getPropertyValue(HEIGHT_VAR);
}

function changeKeyboard(webHeight: number, nativeHeight: number) {
  act(() => {
    viewport.height = webHeight;
    nativeViewport = { width: window.innerWidth, height: nativeHeight };
    viewport.dispatchEvent(new Event("resize"));
    for (const listener of nativeListeners) listener();
    vi.advanceTimersByTime(20);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("innerWidth", 1210);
  vi.stubGlobal("innerHeight", 834);
  vi.stubGlobal("scrollTo", vi.fn());
  viewport = Object.assign(new EventTarget(), { height: 834, offsetTop: 0 });
  vi.stubGlobal("visualViewport", viewport);
  nativeViewport = { width: 1210, height: 834 };
  nativeListeners = new Set();
  setDocumentScrollEnabled = vi.fn();
  vi.stubGlobal("omnigentNative", {
    kind: "ios",
    setDocumentScrollEnabled,
    getKeyboardViewport: () => nativeViewport,
    onKeyboardViewportChanged: (callback: () => void) => {
      nativeListeners.add(callback);
      return () => nativeListeners.delete(callback);
    },
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("iOS keyboard layout", () => {
  it("disables native root scrolling only while the shell controls keyboard layout", () => {
    const { unmount } = renderHook(useKeyboardLayout);
    expect(setDocumentScrollEnabled).toHaveBeenCalledExactlyOnceWith(false);
    changeKeyboard(765.5, 834);
    expect(shellHeight()).toBe("834px");
    expect(setDocumentScrollEnabled).toHaveBeenCalledTimes(1);
    unmount();
    expect(setDocumentScrollEnabled).toHaveBeenLastCalledWith(true);
  });

  it("keeps the shell and overlays full-height for floating hardware-keyboard controls", () => {
    const { result } = renderHook(useKeyboardLayout);
    changeKeyboard(685, 834);
    expect(shellHeight()).toBe("834px");
    expect(result.current).toBe(0);
  });

  it("keeps content above a docked keyboard and restores it when dismissed", () => {
    const { result } = renderHook(useKeyboardLayout);
    changeKeyboard(464, 464);
    expect(shellHeight()).toBe("464px");
    expect(result.current).toBe(370);
    // Native geometry can settle before WebKit restores its viewport.
    changeKeyboard(464, 834);
    expect(shellHeight()).toBe("834px");
    expect(result.current).toBe(0);
  });

  it("keeps native height when WebKit briefly shrinks innerHeight for the toolbar", () => {
    const { result } = renderHook(useKeyboardLayout);
    vi.stubGlobal("innerHeight", 766);
    changeKeyboard(766, 834);
    expect(shellHeight()).toBe("834px");
    expect(result.current).toBe(0);
    vi.stubGlobal("innerHeight", 834);
    changeKeyboard(765.5, 834);
    expect(shellHeight()).toBe("834px");
  });

  it("reserves short native docked insets for both the shell and fixed overlays", () => {
    const { result } = renderHook(useKeyboardLayout);
    changeKeyboard(779, 779);
    expect(shellHeight()).toBe("779px");
    expect(result.current).toBe(55);
    changeKeyboard(834, 834);
    expect(result.current).toBe(0);
  });

  it("keeps filtering small visual viewport changes on older shells", () => {
    vi.stubGlobal("omnigentNative", { kind: "ios" });
    const { result } = renderHook(useKeyboardLayout);
    changeKeyboard(779, 834);
    expect(result.current).toBe(0);
  });

  it("accepts fractional native dimensions without expanding the document", () => {
    nativeViewport = { width: 1210.33, height: 834.33 };
    viewport.height = 685;
    const { result } = renderHook(useKeyboardLayout);
    expect(shellHeight()).toBe("834px");
    expect(result.current).toBe(0);
  });

  it("uses the visual viewport on older shells", () => {
    vi.stubGlobal("omnigentNative", { kind: "ios" });
    const { result } = renderHook(useKeyboardLayout);
    changeKeyboard(464, 834);
    expect(shellHeight()).toBe("464px");
    expect(result.current).toBe(370);
  });

  it("ignores stale native dimensions during rotation", () => {
    const { result } = renderHook(useKeyboardLayout);
    act(() => {
      vi.stubGlobal("innerWidth", 834);
      vi.stubGlobal("innerHeight", 1210);
      viewport.height = 1210;
      window.dispatchEvent(new Event("resize"));
      vi.advanceTimersByTime(20);
    });
    expect(shellHeight()).toBe("1210px");
    expect(result.current).toBe(0);
    changeKeyboard(840, 840);
    expect(shellHeight()).toBe("840px");
    expect(result.current).toBe(370);
  });

  it.each([
    null,
    { width: 1210, height: NaN },
    { width: 1210, height: -1 },
    { width: NaN, height: 834 },
  ])("falls back until native geometry is valid: %j", (value) => {
    nativeViewport = value;
    viewport.height = 464;
    const { result } = renderHook(useKeyboardLayout);
    expect(shellHeight()).toBe("464px");
    expect(result.current).toBe(370);
  });

  it("removes native listeners and pending layout work on unmount", () => {
    const { unmount } = renderHook(useKeyboardLayout);
    act(() => viewport.dispatchEvent(new Event("resize")));
    unmount();
    expect(nativeListeners.size).toBe(0);
    act(() => vi.advanceTimersByTime(20));
    expect(shellHeight()).toBe("");
  });

  it.each(["electron", "android", undefined])("leaves %s layout alone", (kind) => {
    vi.stubGlobal("omnigentNative", kind ? { kind } : undefined);
    const { result } = renderHook(useKeyboardLayout);
    changeKeyboard(464, 464);
    expect(shellHeight()).toBe("");
    expect(result.current).toBe(0);
  });
});
