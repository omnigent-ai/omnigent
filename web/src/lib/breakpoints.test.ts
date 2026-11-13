import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";

import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { MD_BREAKPOINT_PX, MD_MIN_WIDTH_QUERY, isMobileViewport } from "./breakpoints";
import { type MediaEnvironment, stubMatchMedia } from "@/test-helpers/matchMedia";

// Stubs restore in reverse order so no test inherits the previous environment.
const restoreMatchMedia: (() => void)[] = [];
function stub(env: MediaEnvironment): void {
  restoreMatchMedia.push(stubMatchMedia(env));
}

afterEach(() => {
  while (restoreMatchMedia.length > 0) restoreMatchMedia.pop()?.();
  vi.restoreAllMocks();
});

describe("breakpoints", () => {
  it("encodes Tailwind's md breakpoint exactly once", () => {
    expect(MD_BREAKPOINT_PX).toBe(768);
    // md: variant (inclusive lower bound) — the one canonical query.
    expect(MD_MIN_WIDTH_QUERY).toBe("(min-width: 768px)");
  });

  it("isMobileViewport is true below md and false at md+", () => {
    stub({ width: MD_BREAKPOINT_PX });
    expect(isMobileViewport()).toBe(false);

    stub({ width: MD_BREAKPOINT_PX - 1 });
    expect(isMobileViewport()).toBe(true);
  });

  // Hook, helper, and native-shell signal must agree at every width; in the
  // fractional sliver under 768 (browser zoom) Tailwind renders mobile base
  // styles, so the predicate must say mobile too.
  it.each([
    [767, true],
    [767.5, true],
    [767.98, true],
    [767.99, true],
    [768, false],
  ])("hook, helper, and published signal agree at %spx", (width, mobile) => {
    stub({ width });
    expect(isMobileViewport()).toBe(mobile);
    expect(window["__omnigentIsMobileViewport"]?.()).toBe(mobile);
    const { result } = renderHook(() => useIsMobileViewport());
    expect(result.current).toBe(mobile);
  });

  it("publishes the signal native shells consume for their back handler", () => {
    expect(window["__omnigentIsMobileViewport"]).toBe(isMobileViewport);
  });
});
