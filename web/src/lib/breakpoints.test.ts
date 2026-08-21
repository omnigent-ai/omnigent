import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";

import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { MD_BREAKPOINT_PX, MD_MIN_WIDTH_QUERY, isMobileViewport } from "./breakpoints";

// Evaluate min-/max-width queries against a simulated viewport width, so
// boundary behavior at fractional widths can be exercised.
function stubViewportWidth(width: number) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: (() => {
        const min = query.match(/^\(min-width: ([\d.]+)px\)$/);
        if (min) return width >= parseFloat(min[1]);
        const max = query.match(/^\(max-width: ([\d.]+)px\)$/);
        if (max) return width <= parseFloat(max[1]);
        return false;
      })(),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("breakpoints", () => {
  it("encodes Tailwind's md breakpoint exactly once", () => {
    expect(MD_BREAKPOINT_PX).toBe(768);
    // md: variant (inclusive lower bound) — the one canonical query.
    expect(MD_MIN_WIDTH_QUERY).toBe("(min-width: 768px)");
  });

  it("isMobileViewport is true below md and false at md+", () => {
    stubViewportWidth(MD_BREAKPOINT_PX);
    expect(isMobileViewport()).toBe(false);

    stubViewportWidth(MD_BREAKPOINT_PX - 1);
    expect(isMobileViewport()).toBe(true);
  });

  // The hook, the imperative helper, and the native-shell signal must give
  // the SAME answer at every width. The pole is "mobile unless provably
  // md+": in the fractional sliver (767.98, 768) — reachable under browser
  // zoom — Tailwind's md: overrides don't apply, the page renders its mobile
  // base styles, and the predicate must say mobile too.
  it.each([
    [767, true],
    [767.5, true],
    [767.98, true],
    [767.99, true],
    [768, false],
  ])("hook, helper, and published signal agree at %spx", (width, mobile) => {
    stubViewportWidth(width);
    expect(isMobileViewport()).toBe(mobile);
    // eslint-disable-next-line no-underscore-dangle -- bridge-global naming
    expect(window.__omnigentIsMobileViewport?.()).toBe(mobile);
    const { result } = renderHook(() => useIsMobileViewport());
    expect(result.current).toBe(mobile);
  });

  it("publishes the signal native shells consume for their back handler", () => {
    // eslint-disable-next-line no-underscore-dangle -- bridge-global naming
    expect(window.__omnigentIsMobileViewport).toBe(isMobileViewport);
  });
});
