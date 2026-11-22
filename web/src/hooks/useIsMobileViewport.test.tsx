import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isDesktopLayout, useIsMobileViewport } from "./useIsMobileViewport";
import {
  isSideBySideSidebarViewport,
  useIsSideBySideSidebarViewport,
} from "./useIsSideBySideSidebarViewport";

/** Answer the md width queries as a viewport of `width` CSS pixels, squarish enough to dock. */
function mockViewportWidth(width: number) {
  const base = window.matchMedia;
  vi.spyOn(window, "matchMedia").mockImplementation((query: string) => {
    const min = /\(min-width: ([\d.]+)px\)/.exec(query);
    const max = /\(max-width: ([\d.]+)px\)/.exec(query);
    return {
      ...base(query),
      media: query,
      matches: (!min || width >= Number(min[1])) && (!max || width <= Number(max[1])),
    };
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("layout by viewport width", () => {
  it.each([
    ["a narrow browser window", 740, false, true],
    ["a wide browser window", 1280, false, false],
    ["a narrow desktop app window", 740, true, false],
    ["a wide desktop app window", 1280, true, false],
  ])("picks the layout for %s", (_name, width, desktopApp, mobile) => {
    mockViewportWidth(width);
    if (desktopApp) vi.stubGlobal("omnigentDesktop", { kind: "electron" });

    expect(renderHook(() => useIsMobileViewport()).result.current).toBe(mobile);
    expect(isDesktopLayout()).toBe(!mobile);
    // 740px is wide enough for the mobile layout's side-by-side sidebar.
    expect(isSideBySideSidebarViewport()).toBe(mobile);
    expect(renderHook(() => useIsSideBySideSidebarViewport()).result.current).toBe(mobile);
  });
});
