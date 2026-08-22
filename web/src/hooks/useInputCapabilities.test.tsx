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
  it("reports a mouse desktop when no query matches", () => {
    installMatchMedia({});
    const { result } = renderHook(() => useInputCapabilities());
    expect(result.current).toEqual({
      coarsePrimary: false,
      anyCoarse: false,
      hoverPrimary: false,
      hasTouch: false,
    });
  });

  it("reads each capability from its media query and maxTouchPoints", () => {
    installMatchMedia({
      "(pointer: coarse)": true,
      "(any-pointer: coarse)": true,
      "(hover: hover)": false,
    });
    setMaxTouchPoints(5);
    const { result } = renderHook(() => useInputCapabilities());
    expect(result.current).toEqual({
      coarsePrimary: true,
      anyCoarse: true,
      hoverPrimary: false,
      hasTouch: true,
    });
  });

  it("keeps viewport-independent axes independent: a fine-primary touch laptop", () => {
    // any-pointer coarse (touchscreen present) with a fine hovering primary
    // (trackpad) — TR-2's touch-laptop shape must be representable.
    installMatchMedia({
      "(any-pointer: coarse)": true,
      "(hover: hover)": true,
    });
    setMaxTouchPoints(10);
    const { result } = renderHook(() => useInputCapabilities());
    expect(result.current).toEqual({
      coarsePrimary: false,
      anyCoarse: true,
      hoverPrimary: true,
      hasTouch: true,
    });
  });

  it("updates live when a media query flips (convertible mode change)", () => {
    const media = installMatchMedia({ "(hover: hover)": true });
    const { result } = renderHook(() => useInputCapabilities());
    expect(result.current.coarsePrimary).toBe(false);
    expect(result.current.hoverPrimary).toBe(true);

    act(() => {
      media.set("(pointer: coarse)", true);
      media.set("(hover: hover)", false);
    });
    expect(result.current.coarsePrimary).toBe(true);
    expect(result.current.hoverPrimary).toBe(false);
  });

  it("returns a referentially stable snapshot while values are unchanged", () => {
    installMatchMedia({ "(hover: hover)": true });
    const { result, rerender } = renderHook(() => useInputCapabilities());
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });
});
