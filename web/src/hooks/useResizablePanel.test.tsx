import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import { setInnerWidth } from "./resizeHookTestHelpers";
import {
  HANDLE_COARSE_GUTTER_PX,
  HANDLE_FINE_GUTTER_PX,
  HANDLE_INWARD_SLIVER_PX,
  HANDLE_OUTWARD_SLIVER_PX,
  resetSharedWidthStoreForTesting,
  useResizablePanel,
} from "./useResizablePanel";

const originalInnerWidth = window.innerWidth;
const originalMatchMedia = window.matchMedia;
let desktopMatches = true;
let coarsePointer = false;
const desktopChangeListeners = new Set<(event: MediaQueryListEvent) => void>();
const coarseChangeListeners = new Set<(event: MediaQueryListEvent) => void>();

function installMatchMedia(): void {
  window.matchMedia = vi.fn((query: string) => ({
    get matches() {
      return query === "(any-pointer: coarse)"
        ? coarsePointer
        : query === "(pointer: coarse)"
          ? false
          : query.includes("min-width")
            ? desktopMatches
            : false;
    },
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
      if (query === "(any-pointer: coarse)") coarseChangeListeners.add(listener);
      else if (query.includes("min-width")) desktopChangeListeners.add(listener);
    },
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
      if (query === "(any-pointer: coarse)") coarseChangeListeners.delete(listener);
      else desktopChangeListeners.delete(listener);
    },
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
}

function setCoarseMatch(matches: boolean): void {
  coarsePointer = matches;
  const event = { matches } as MediaQueryListEvent;
  for (const listener of coarseChangeListeners) listener(event);
}

function setDesktopMatch(matches: boolean): void {
  desktopMatches = matches;
  const event = { matches } as MediaQueryListEvent;
  for (const listener of desktopChangeListeners) listener(event);
}

function createPointerHandle() {
  const element = document.createElement("div");
  const capturedPointers = new Set<number>();
  const setPointerCapture = vi.fn((pointerId: number) => capturedPointers.add(pointerId));
  const releasePointerCapture = vi.fn((pointerId: number) => capturedPointers.delete(pointerId));
  const hasPointerCapture = vi.fn((pointerId: number) => capturedPointers.has(pointerId));
  Object.assign(element, { setPointerCapture, releasePointerCapture, hasPointerCapture });
  return { element, setPointerCapture, releasePointerCapture };
}

function pointerEvent(
  element: HTMLElement,
  {
    pointerId,
    clientX = 0,
    pointerType = "touch",
    button = 0,
    preventDefault = () => {},
  }: {
    pointerId: number;
    clientX?: number;
    pointerType?: string;
    button?: number;
    preventDefault?: () => void;
  },
): React.PointerEvent<HTMLElement> {
  return {
    currentTarget: element,
    pointerId,
    clientX,
    pointerType,
    button,
    preventDefault,
  } as React.PointerEvent<HTMLElement>;
}

beforeEach(() => {
  setInnerWidth(2000);
  desktopMatches = true;
  coarsePointer = false;
  desktopChangeListeners.clear();
  coarseChangeListeners.clear();
  installMatchMedia();
});

afterEach(() => {
  localStorage.clear();
  resetSharedWidthStoreForTesting();
  setInnerWidth(originalInnerWidth);
  window.matchMedia = originalMatchMedia;
  desktopChangeListeners.clear();
  coarseChangeListeners.clear();
});

describe("useResizablePanel persistence", () => {
  it("seeds desktop state from the canonical media query, not innerWidth", () => {
    setInnerWidth(2000);
    desktopMatches = false;
    const { result } = renderHook(() => useResizablePanel(true));

    expect(result.current.isDesktop).toBe(false);
    expect(result.current.panelWidth).toBeUndefined();
  });

  it("persists explicit keyboard resize and restores it after store reset", () => {
    const { result, unmount } = renderHook(() => useResizablePanel(true));

    // Default at 2000px viewport is 50vw = 1000. ArrowRight narrows by 20px.
    act(() => {
      result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    expect(result.current.panelWidth).toBe(980);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(980);

    unmount();
    resetSharedWidthStoreForTesting();
    const restored = renderHook(() => useResizablePanel(true));

    // A fresh module-level store hydrates from localStorage instead of falling
    // back to 50vw, which is the refresh behavior this hook must preserve.
    expect(restored.result.current.panelWidth).toBe(980);
    restored.unmount();
  });

  it("clamps live on shrink without persisting, then restores the preference on widen", () => {
    const { result } = renderHook(() => useResizablePanel(true));

    act(() => {
      result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(980);

    setInnerWidth(1000);
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });

    // The live width clamps to the new 80vw ceiling, but the saved user
    // preference remains 980 so a later wider viewport can restore it.
    expect(result.current.panelWidth).toBe(800);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(980);

    // Widening the viewport again re-derives from the persisted preference,
    // so the panel springs back to 980 within the same session (no reload).
    setInnerWidth(2000);
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current.panelWidth).toBe(980);
  });

  it("recomputes a viewport-derived default and aria maximum on every resize", () => {
    const { result } = renderHook(() => useResizablePanel(true));
    expect(result.current.panelWidth).toBe(1000);
    expect(result.current.handleProps["aria-valuemax"]).toBe(1600);

    setInnerWidth(1000);
    act(() => window.dispatchEvent(new Event("resize")));

    // No preference exists, so the width store remains null; the viewport
    // signal must still force both render-time values to update.
    expect(result.current.panelWidth).toBe(500);
    expect(result.current.handleProps["aria-valuemax"]).toBe(800);
  });

  it("captures the pointer and persists the final width on release", () => {
    const { result } = renderHook(() => useResizablePanel(true));
    const handle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element, { pointerId: 7 }));
    });
    expect(handle.setPointerCapture).toHaveBeenCalledWith(7);

    act(() => {
      // 2000px viewport, cursor at 1200 → width = innerWidth - clientX = 800.
      result.current.handleProps.onPointerMove(
        pointerEvent(handle.element, { pointerId: 7, clientX: 1200 }),
      );
    });

    // Live width tracks the drag, but nothing is written to storage mid-drag —
    // persisting per pointermove would fire a synchronous setItem on every frame.
    expect(result.current.panelWidth).toBe(800);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBeNull();

    act(() => {
      result.current.handleProps.onPointerUp(pointerEvent(handle.element, { pointerId: 7 }));
    });

    // Release snapshots the final width exactly once.
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(800);
    expect(handle.releasePointerCapture).toHaveBeenCalledWith(7);
  });

  it("stays idle when pointer capture throws", () => {
    const { result } = renderHook(() => useResizablePanel(true));
    const failedHandle = createPointerHandle();
    const nextHandle = createPointerHandle();
    failedHandle.setPointerCapture.mockImplementation(() => {
      throw new Error("capture failed");
    });
    const preventDefault = vi.fn();

    act(() => {
      result.current.handleProps.onPointerDown(
        pointerEvent(failedHandle.element, { pointerId: 8, preventDefault }),
      );
      result.current.handleProps.onPointerMove(
        pointerEvent(failedHandle.element, { pointerId: 8, clientX: 1200 }),
      );
    });

    expect(preventDefault).not.toHaveBeenCalled();
    expect(result.current.panelWidth).toBe(1000);
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(nextHandle.element, { pointerId: 9 }));
    });
    expect(nextHandle.setPointerCapture).toHaveBeenCalledWith(9);
  });

  it.each(["onPointerCancel", "onLostPointerCapture"] as const)(
    "aborts cleanly via %s without persisting",
    (abortHandler) => {
      const { result } = renderHook(() => useResizablePanel(true));
      const handle = createPointerHandle();

      act(() => {
        result.current.handleProps.onPointerDown(pointerEvent(handle.element, { pointerId: 11 }));
        result.current.handleProps.onPointerMove(
          pointerEvent(handle.element, { pointerId: 11, clientX: 1200 }),
        );
      });
      expect(result.current.panelWidth).toBe(800);

      act(() => {
        result.current.handleProps[abortHandler](pointerEvent(handle.element, { pointerId: 11 }));
        result.current.handleProps.onPointerMove(
          pointerEvent(handle.element, { pointerId: 11, clientX: 1400 }),
        );
      });

      // The abort restores the pre-drag width (the viewport-derived default).
      expect(result.current.panelWidth).toBe(1000);
      expect(readPanelSizePreference("pushPanelWidthPx")).toBeNull();
      expect(document.body.style.cursor).toBe("");
      expect(document.body.style.userSelect).toBe("");
    },
  );

  it("ignores additional pointers until the active drag ends", () => {
    const { result } = renderHook(() => useResizablePanel(true));
    const firstHandle = createPointerHandle();
    const secondHandle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(firstHandle.element, { pointerId: 1 }));
      result.current.handleProps.onPointerDown(
        pointerEvent(secondHandle.element, { pointerId: 2 }),
      );
      result.current.handleProps.onPointerMove(
        pointerEvent(secondHandle.element, { pointerId: 2, clientX: 1400 }),
      );
    });

    expect(firstHandle.setPointerCapture).toHaveBeenCalledWith(1);
    expect(secondHandle.setPointerCapture).not.toHaveBeenCalled();
    expect(result.current.panelWidth).toBe(1000);

    act(() => {
      result.current.handleProps.onPointerMove(
        pointerEvent(firstHandle.element, { pointerId: 1, clientX: 1200 }),
      );
    });
    expect(result.current.panelWidth).toBe(800);
  });

  it("aborts without persisting when the hook unmounts mid-drag", () => {
    const { result, unmount } = renderHook(() => useResizablePanel(true));
    const handle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element, { pointerId: 4 }));
      result.current.handleProps.onPointerMove(
        pointerEvent(handle.element, { pointerId: 4, clientX: 1200 }),
      );
      unmount();
    });

    expect(readPanelSizePreference("pushPanelWidthPx")).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });

  it("aborts when the desktop media query flips during a drag", () => {
    const { result } = renderHook(() => useResizablePanel(true));
    const handle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element, { pointerId: 6 }));
      result.current.handleProps.onPointerMove(
        pointerEvent(handle.element, { pointerId: 6, clientX: 1200 }),
      );
    });
    act(() => setDesktopMatch(false));

    expect(result.current.isDesktop).toBe(false);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });

  it("ignores non-primary pen buttons", () => {
    const { result } = renderHook(() => useResizablePanel(true));
    const handle = createPointerHandle();
    const preventDefault = vi.fn();

    act(() => {
      result.current.handleProps.onPointerDown(
        pointerEvent(handle.element, {
          pointerId: 3,
          pointerType: "pen",
          button: 2,
          preventDefault,
        }),
      );
      result.current.handleProps.onPointerMove(
        pointerEvent(handle.element, { pointerId: 3, clientX: 1200 }),
      );
    });

    expect(handle.setPointerCapture).not.toHaveBeenCalled();
    expect(preventDefault).not.toHaveBeenCalled();
    expect(result.current.panelWidth).toBe(1000);
  });

  it.each([
    ["fine", false, HANDLE_FINE_GUTTER_PX, 24],
    ["coarse", true, HANDLE_COARSE_GUTTER_PX, 26],
  ] as const)("returns the budgeted %s seam gutter", (_, coarse, gutter, target) => {
    coarsePointer = coarse;
    const { result } = renderHook(() => useResizablePanel(true));
    const style = result.current.handleProps.style;
    const inset = (gutter - 4) / 2;

    expect(style).toMatchObject({
      touchAction: "none",
      boxSizing: "content-box",
      paddingInlineStart: HANDLE_OUTWARD_SLIVER_PX + inset,
      paddingInlineEnd: HANDLE_INWARD_SLIVER_PX + inset,
      marginInlineStart: -HANDLE_OUTWARD_SLIVER_PX,
      marginInlineEnd: -HANDLE_INWARD_SLIVER_PX,
      backgroundClip: "content-box",
    });
    expect(4 + Number(style.paddingInlineStart) + Number(style.paddingInlineEnd)).toBe(target);
    // The transcript scrollbar thumb occupies the 6–12px band from this seam;
    // keep the handle out of that band so touch-scroll starts remain available.
    expect(-Number(style.marginInlineStart)).toBeLessThanOrEqual(6);
    // FilesPanel's toolbar uses px-2 (8px); do not annex beyond that gutter
    // into its search control or other panel-side interactive content.
    expect(-Number(style.marginInlineEnd)).toBeLessThanOrEqual(8);
    expect(
      4 +
        Number(style.paddingInlineStart) +
        Number(style.paddingInlineEnd) +
        Number(style.marginInlineStart) +
        Number(style.marginInlineEnd),
    ).toBe(gutter);
  });

  it("exposes separator value semantics that track keyboard resizing", () => {
    const { result } = renderHook(() => useResizablePanel(true));

    expect(result.current.handleProps).toMatchObject({
      role: "separator",
      "aria-valuenow": 1000,
      "aria-valuemin": 320,
      "aria-valuemax": 1600,
    });

    act(() =>
      result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: vi.fn(),
      } as unknown as React.KeyboardEvent),
    );
    expect(result.current.handleProps["aria-valuenow"]).toBe(980);
  });

  it("updates the gutter when an attached coarse pointer changes", () => {
    const { result } = renderHook(() => useResizablePanel(true));
    expect(result.current.handleProps.style.marginInlineStart).toBe(-HANDLE_OUTWARD_SLIVER_PX);
    expect(result.current.handleProps.style.paddingInlineStart).toBe(9);

    act(() => setCoarseMatch(true));

    expect(result.current.handleProps.style.marginInlineStart).toBe(-HANDLE_OUTWARD_SLIVER_PX);
    expect(result.current.handleProps.style.paddingInlineStart).toBe(10);
  });

  it("notifies multiple mounted subscribers from the shared width store", () => {
    const first = renderHook(() => useResizablePanel(true));
    const second = renderHook(() => useResizablePanel(true));

    act(() => {
      first.result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    // Both hook instances read the same module-level store. If subscription
    // fan-out breaks, only the initiating hook would observe the new width.
    expect(first.result.current.panelWidth).toBe(980);
    expect(second.result.current.panelWidth).toBe(980);

    first.unmount();
    second.unmount();
  });
});
