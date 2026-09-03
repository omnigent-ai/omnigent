import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useInputCapabilities } from "./useInputCapabilities";

// Controllable matchMedia: per-query matches plus manually fired change
// events, so the hook's reactivity can be exercised (a convertible flipping
// modes, a mouse attaching).
function installMatchMedia(state: Record<string, boolean>) {
  const listeners = new Map<string, Set<() => void>>();
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: vi.fn((query: string) => ({
      get matches() {
        return state[query] ?? false;
      },
      media: query,
      addEventListener: (_: string, cb: () => void) => {
        if (!listeners.has(query)) listeners.set(query, new Set());
        listeners.get(query)!.add(cb);
      },
      removeEventListener: (_: string, cb: () => void) => {
        listeners.get(query)?.delete(cb);
      },
    })),
  });
  return {
    set(query: string, matches: boolean) {
      state[query] = matches;
      for (const cb of listeners.get(query) ?? []) cb();
    },
  };
}

function setMaxTouchPoints(value: number) {
  Object.defineProperty(navigator, "maxTouchPoints", {
    configurable: true,
    value,
  });
}

afterEach(() => {
  setMaxTouchPoints(0);
});

describe("useInputCapabilities", () => {
  it("reports no coarse pointer when the query does not match", () => {
    installMatchMedia({});
    const { result } = renderHook(() => useInputCapabilities());
    expect(result.current).toEqual({ anyCoarse: false, hasTouch: false });
  });

  it("reports a coarse pointer when the query matches", () => {
    installMatchMedia({ "(any-pointer: coarse)": true });
    const { result } = renderHook(() => useInputCapabilities());
    expect(result.current).toEqual({ anyCoarse: true, hasTouch: false });
  });

  it("reports an attached touch digitizer", () => {
    installMatchMedia({});
    setMaxTouchPoints(5);
    const { result } = renderHook(() => useInputCapabilities());
    expect(result.current).toEqual({ anyCoarse: false, hasTouch: true });
  });

  it("updates live when the coarse-pointer query flips", () => {
    const media = installMatchMedia({});
    const { result } = renderHook(() => useInputCapabilities());
    expect(result.current.anyCoarse).toBe(false);

    act(() => {
      media.set("(any-pointer: coarse)", true);
    });
    expect(result.current.anyCoarse).toBe(true);
  });

  it("returns a referentially stable snapshot while values are unchanged", () => {
    installMatchMedia({ "(any-pointer: coarse)": true });
    const { result, rerender } = renderHook(() => useInputCapabilities());
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });

  it("shares one media-query subscription across concurrent consumers and rerenders", () => {
    installMatchMedia({ "(hover: hover)": true });
    const matchMedia = vi.mocked(window.matchMedia);
    const { rerender } = renderHook(() => {
      useInputCapabilities();
      useInputCapabilities();
    });

    expect(matchMedia).toHaveBeenCalledTimes(1);
    rerender();
    expect(matchMedia).toHaveBeenCalledTimes(1);
  });
});
