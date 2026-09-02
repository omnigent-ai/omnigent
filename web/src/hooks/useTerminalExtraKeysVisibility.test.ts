import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { writeTerminalExtraKeysMode } from "@/lib/terminalExtraKeysPreferences";
import { useTerminalExtraKeysVisibility } from "./useTerminalExtraKeysVisibility";

type ChangeListener = (ev: { matches: boolean }) => void;

/**
 * Per-query matchMedia stub: `(pointer: coarse)` reads `coarse.matches` and
 * can fire `change`; every other query stays false.
 */
function stubMatchMedia(initialCoarse: boolean) {
  const coarse = { matches: initialCoarse, listeners: new Set<ChangeListener>() };
  vi.spyOn(window, "matchMedia").mockImplementation((query: string) => {
    const isCoarse = query.includes("pointer: coarse");
    return {
      get matches() {
        return isCoarse ? coarse.matches : false;
      },
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: (_type: string, fn: ChangeListener) => {
        if (isCoarse) coarse.listeners.add(fn);
      },
      removeEventListener: (_type: string, fn: ChangeListener) => {
        if (isCoarse) coarse.listeners.delete(fn);
      },
      dispatchEvent: () => false,
    } as unknown as MediaQueryList;
  });
  return {
    setCoarse(next: boolean) {
      coarse.matches = next;
      for (const fn of coarse.listeners) fn({ matches: next });
    },
    get listenerCount() {
      return coarse.listeners.size;
    },
  };
}

const win = window as unknown as Record<string, unknown>;

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  delete win.omnigentNative;
  vi.restoreAllMocks();
  // Also clears any write kept in memory after a refused storage write.
  writeTerminalExtraKeysMode("auto");
  localStorage.clear();
});

describe("useTerminalExtraKeysVisibility", () => {
  it("shows on a coarse primary pointer and hides on a fine one (auto)", () => {
    const mq = stubMatchMedia(true);
    const { result, rerender } = renderHook(() => useTerminalExtraKeysVisibility(false));
    expect(result.current).toBe(true);

    // Live change (a Fold docking to a mouse) hides the row without a remount.
    act(() => mq.setCoarse(false));
    rerender();
    expect(result.current).toBe(false);

    act(() => mq.setCoarse(true));
    rerender();
    expect(result.current).toBe(true);
  });

  it("stays hidden on a fine pointer with no shell and no override", () => {
    stubMatchMedia(false);
    const { result } = renderHook(() => useTerminalExtraKeysVisibility(false));
    expect(result.current).toBe(false);
  });

  it("shows on a fine pointer inside the iOS or Android native shell", () => {
    // WHY: an iPad with a trackpad reports pointer: fine, but the shell is a
    // touch device by construction and its keyboards often lack Esc.
    stubMatchMedia(false);
    win.omnigentNative = { kind: "ios" };
    expect(renderHook(() => useTerminalExtraKeysVisibility(false)).result.current).toBe(true);

    win.omnigentNative = { kind: "android" };
    expect(renderHook(() => useTerminalExtraKeysVisibility(false)).result.current).toBe(true);
  });

  it("never shows for a read-only attach, whatever the device or preference", () => {
    stubMatchMedia(true);
    win.omnigentNative = { kind: "ios" };
    writeTerminalExtraKeysMode("on");
    const { result } = renderHook(() => useTerminalExtraKeysVisibility(true));
    expect(result.current).toBe(false);
  });

  it("honors the preference: on forces the row on desktop, off hides it on touch", () => {
    const mq = stubMatchMedia(false);
    const { result, rerender } = renderHook(() => useTerminalExtraKeysVisibility(false));
    expect(result.current).toBe(false);

    act(() => writeTerminalExtraKeysMode("on"));
    rerender();
    expect(result.current).toBe(true);

    act(() => mq.setCoarse(true));
    act(() => writeTerminalExtraKeysMode("off"));
    rerender();
    expect(result.current).toBe(false);

    act(() => writeTerminalExtraKeysMode("auto"));
    rerender();
    expect(result.current).toBe(true);
  });

  it("reflects a preference write even when storage refuses it", () => {
    // WHY: the snapshot re-reads the store; if the write only lived in
    // localStorage a quota error would leave the row's visibility stale.
    stubMatchMedia(false);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    const { result, rerender } = renderHook(() => useTerminalExtraKeysVisibility(false));
    expect(result.current).toBe(false);

    act(() => writeTerminalExtraKeysMode("on"));
    rerender();
    expect(result.current).toBe(true);
    expect(localStorage.getItem("omnigent:terminal-extra-keys")).toBeNull();
  });

  it("never consults viewport width", () => {
    // WHY: the product constraint is touch-based visibility; a tablet at
    // desktop width must still get the row, so no width query may be issued.
    const spy = stubMatchMedia(true);
    renderHook(() => useTerminalExtraKeysVisibility(false));
    const queries = (window.matchMedia as unknown as ReturnType<typeof vi.fn>).mock.calls.map(
      (call: unknown[]) => String(call[0]),
    );
    expect(queries.length).toBeGreaterThan(0);
    for (const q of queries) expect(q).not.toMatch(/width/);
    expect(spy.listenerCount).toBe(1);
  });
});
