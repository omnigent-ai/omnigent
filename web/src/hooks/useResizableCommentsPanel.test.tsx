import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import {
  resetCommentsWidthStoreForTesting,
  useResizableCommentsPanel,
} from "./useResizableCommentsPanel";

const originalInnerWidth = window.innerWidth;

function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

// jsdom has no pointer capture, so tests drive the returned handlers directly
// with a stub handle element that tracks capture state.
function makeHandleTarget() {
  const captured = new Set<number>();
  return {
    setPointerCapture: vi.fn((id: number) => captured.add(id)),
    releasePointerCapture: vi.fn((id: number) => captured.delete(id)),
    hasPointerCapture: (id: number) => captured.has(id),
  };
}

type HandleTarget = ReturnType<typeof makeHandleTarget>;

function pointerEvent(
  target: HandleTarget,
  overrides: Partial<{ pointerId: number; pointerType: string; button: number; clientX: number }>,
): React.PointerEvent {
  return {
    pointerId: 1,
    pointerType: "touch",
    button: 0,
    clientX: 0,
    preventDefault: () => {},
    currentTarget: target,
    ...overrides,
  } as unknown as React.PointerEvent;
}

const originalMatchMedia = window.matchMedia;

type MediaListener = (e: MediaQueryListEvent) => void;

/** Controllable matchMedia mock: per-query matches plus a change-event firer. */
function mockMatchMedia(matches: Record<string, boolean> = {}) {
  const listeners = new Map<string, Set<MediaListener>>();
  window.matchMedia = ((query: string) => ({
    matches: matches[query] ?? false,
    media: query,
    onchange: null,
    addEventListener: (_: string, cb: MediaListener) => {
      if (!listeners.has(query)) listeners.set(query, new Set());
      listeners.get(query)?.add(cb);
    },
    removeEventListener: (_: string, cb: MediaListener) => listeners.get(query)?.delete(cb),
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  return {
    fire(query: string, value: boolean) {
      for (const cb of listeners.get(query) ?? new Set<MediaListener>()) {
        cb({ matches: value } as MediaQueryListEvent);
      }
    },
  };
}

/** jsdom has no PointerEvent constructor; a plain Event with pointerId works
 * for the hook's document-level fallback listeners. */
function docPointerEvent(type: string, pointerId: number): Event {
  return Object.assign(new Event(type), { pointerId });
}

const overlaySelector = () =>
  [...document.body.children].find(
    (c): c is HTMLElement =>
      c instanceof HTMLElement && c.style.position === "fixed" && c.style.zIndex === "2147483647",
  ) ?? null;

/** Panel root inside the split row; defaults to right edge x=1000, row 2000px. */
function attachContainer(
  ref: React.MutableRefObject<HTMLDivElement | null>,
  { parentWidth = 2000, panelRight = 1000 } = {},
): void {
  const parent = document.createElement("div");
  const panel = document.createElement("div");
  parent.appendChild(panel);
  vi.spyOn(parent, "getBoundingClientRect").mockReturnValue({ width: parentWidth } as DOMRect);
  vi.spyOn(panel, "getBoundingClientRect").mockReturnValue({ right: panelRight } as DOMRect);
  ref.current = panel;
}

beforeEach(() => {
  setInnerWidth(2000);
  mockMatchMedia({ "(min-width: 768px)": true });
});

afterEach(() => {
  localStorage.clear();
  resetCommentsWidthStoreForTesting();
  setInnerWidth(originalInnerWidth);
  window.matchMedia = originalMatchMedia;
});

describe("useResizableCommentsPanel persistence", () => {
  it("persists explicit keyboard resize and restores it after store reset", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());

    // Default comments width is 240. ArrowLeft widens by 20px.
    act(() => {
      result.current.handleProps.onKeyDown({
        key: "ArrowLeft",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    expect(result.current.width).toBe(260);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(260);

    unmount();
    resetCommentsWidthStoreForTesting();
    const restored = renderHook(() => useResizableCommentsPanel());

    // The restored hook must use the saved comments width instead of the fixed
    // 240px default, matching a browser refresh while comments are open.
    expect(restored.result.current.width).toBe(260);
    restored.unmount();
  });
});

describe("useResizableCommentsPanel pointer drag", () => {
  it("captures the pointer on pointerdown and resizes from pointermove", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 7 })));
    // Capture keeps the drag alive when the pointer leaves the 1px handle.
    expect(target.setPointerCapture).toHaveBeenCalledWith(7);

    // Panel right edge is at 1000, so a move to x=700 means a 300px width.
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 7, clientX: 700 }),
      ),
    );
    expect(result.current.width).toBe(300);

    // Live moves must not persist; only the release does.
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBeNull();
    act(() =>
      result.current.handleProps.onPointerUp(pointerEvent(target, { pointerId: 7, clientX: 700 })),
    );
    expect(target.releasePointerCapture).toHaveBeenCalledWith(7);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(300);
    unmount();
  });

  it("ignores a second concurrent pointer — first pointer wins", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 1 })));
    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 2 })));
    // A second finger neither captures nor steals the drag.
    expect(target.setPointerCapture).toHaveBeenCalledTimes(1);

    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 2, clientX: 500 }),
      ),
    );
    expect(result.current.width).toBe(240);

    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 1, clientX: 700 }),
      ),
    );
    expect(result.current.width).toBe(300);
    unmount();
  });

  it("leaves at least 240px for the viewer at maximum width with the gutter present", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    // Full row is [viewer, gutter, panel]: a 500px row with the panel's right
    // edge flush at x=500, so every pixel the panel takes comes out of the
    // viewer+gutter budget.
    attachContainer(result.current.containerRef, { parentWidth: 500, panelRight: 500 });
    const target = makeHandleTarget();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 6 })));
    // Drag all the way left — the clamp, not the pointer, decides the max.
    act(() =>
      result.current.handleProps.onPointerMove(pointerEvent(target, { pointerId: 6, clientX: 0 })),
    );

    // The dynamic max must budget the gutter's footprint (always the coarse
    // 8px) on top of the viewer's 240px minimum: 500 − 240 − 8 = 252.
    const width = result.current.width as number;
    expect(width).toBe(252);
    expect(500 - width - 8).toBeGreaterThanOrEqual(240);

    act(() =>
      result.current.handleProps.onPointerUp(pointerEvent(target, { pointerId: 6, clientX: 0 })),
    );
    unmount();
  });

  it("ends the drag from the document fallback when capture delivery fails", () => {
    // A browser can drop capture without firing the handle's own pointerup
    // (tab switch, node detach). The document-level fallback still ends the
    // drag — a release, so it persists — and the max-z overlay comes down.
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 5 })));
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 5, clientX: 700 }),
      ),
    );
    act(() => void document.dispatchEvent(docPointerEvent("pointerup", 5)));

    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(300);
    unmount();
  });

  it("aborts without persisting from the document pointercancel fallback", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 5 })));
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 5, clientX: 700 }),
      ),
    );
    act(() => void document.dispatchEvent(docPointerEvent("pointercancel", 5)));

    expect(overlaySelector()).toBeNull();
    expect(result.current.width).toBe(300);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBeNull();
    unmount();
  });

  it("aborts the drag when the layout flips below the md breakpoint", () => {
    // Flipping to mobile unmounts the handle, so its up/cancel can never
    // arrive; the drag must end (unpersisted) or the overlay would wedge.
    const mm = mockMatchMedia({ "(min-width: 768px)": true });
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 4 })));
    expect(overlaySelector()).not.toBeNull();

    act(() => mm.fire("(min-width: 768px)", false));
    expect(result.current.isDesktop).toBe(false);
    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBeNull();
    unmount();
  });

  it("stays fully idle when pointer capture throws", () => {
    // If capture fails, publishing drag state anyway would leave a stale
    // activePointerId that a later reused pointerId could match — ending
    // (and persisting) a drag that never started.
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();
    target.setPointerCapture.mockImplementation(() => {
      throw new Error("InvalidPointerId");
    });

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 9 })));
    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("");

    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 9, clientX: 700 }),
      ),
    );
    expect(result.current.width).toBe(240);

    act(() => void document.dispatchEvent(docPointerEvent("pointerup", 9)));
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBeNull();
    unmount();
  });

  it("does not start a drag from a pen barrel button", () => {
    // A pen barrel press dispatches pointerType "pen" with button 2; only
    // the primary button/tip (button 0) may start a drag.
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() =>
      result.current.handleProps.onPointerDown(
        pointerEvent(target, { pointerType: "pen", button: 2 }),
      ),
    );
    expect(target.setPointerCapture).not.toHaveBeenCalled();
    expect(overlaySelector()).toBeNull();
    unmount();
  });

  it("does not start a drag from a secondary mouse button", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() =>
      result.current.handleProps.onPointerDown(
        pointerEvent(target, { pointerType: "mouse", button: 2 }),
      ),
    );
    expect(target.setPointerCapture).not.toHaveBeenCalled();

    act(() => result.current.handleProps.onPointerMove(pointerEvent(target, { clientX: 700 })));
    expect(result.current.width).toBe(240);
    unmount();
  });

  it.each([
    ["pointercancel", "onPointerCancel"],
    ["lostpointercapture", "onLostPointerCapture"],
  ] as const)("aborts cleanly at the last applied width on %s", (_name, handler) => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 3 })));
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 3, clientX: 700 }),
      ),
    );
    act(() => result.current.handleProps[handler](pointerEvent(target, { pointerId: 3 })));

    // Never a half-state: width settles, body styles restore, drag is over so
    // later moves from the same pointer are inert. An abort is not a choice —
    // the last applied width stays on screen but is never persisted.
    expect(result.current.width).toBe(300);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 3, clientX: 500 }),
      ),
    );
    expect(result.current.width).toBe(300);
    unmount();
  });
});

describe("useResizableCommentsPanel touch affordances", () => {
  // The handle is a divider gutter: a real flex child whose layout footprint
  // is the gutter width (4px painted strip + padding + the cancelling
  // negative margins) and whose hit box overhangs each neighbor by only a
  // capped sliver. Derived from the returned style:
  const gutterGeometry = (style: React.CSSProperties) => {
    const padLeft = Number(style.paddingLeft);
    const padRight = Number(style.paddingRight);
    const marginLeft = Number(style.marginLeft);
    const marginRight = Number(style.marginRight);
    return {
      hitTotal: 4 + padLeft + padRight,
      footprint: 4 + padLeft + padRight + marginLeft + marginRight,
      viewerOverhang: -marginLeft,
      inwardOverhang: -marginRight,
    };
  };

  it("declares touch-action none and a capped-sliver gutter hit target", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    const { style } = result.current.handleProps;

    // No scroll/swipe may start from the handle during a potential drag.
    expect(style.touchAction).toBe("none");

    // Fine pointer (the matchMedia stub reports no coarse pointer): >=24px
    // hit total in a 6px-wide gutter. The slivers are capped so a classic
    // viewer scrollbar and the panel's content keep their pointer streams.
    const g = gutterGeometry(style);
    expect(g.hitTotal).toBeGreaterThanOrEqual(24);
    expect(g.footprint).toBe(6);
    expect(g.viewerOverhang).toBeLessThanOrEqual(10);
    expect(g.inwardOverhang).toBeLessThanOrEqual(8);

    // Content-box keeps hover/active backgrounds on the 4px painted strip.
    expect(style.boxSizing).toBe("content-box");
    expect(style.backgroundClip).toBe("content-box");

    // The affordance is pure style — nothing is rendered into the handle.
    expect("children" in result.current.handleProps).toBe(false);
    unmount();
  });

  it("widens the gutter and hit target on coarse-pointer devices", () => {
    mockMatchMedia({ "(pointer: coarse)": true });
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());

    // Coarse: 8px gutter, 26px hit total — TR-7's 24px floor with the same
    // sliver caps (the preferred 44px would need a visually wide gutter).
    const g = gutterGeometry(result.current.handleProps.style);
    expect(g.hitTotal).toBe(26);
    expect(g.footprint).toBe(8);
    expect(g.viewerOverhang).toBeLessThanOrEqual(10);
    expect(g.inwardOverhang).toBeLessThanOrEqual(8);
    unmount();
  });

  it("exposes the width to assistive tech via aria value attributes", () => {
    const { result, rerender, unmount } = renderHook(() => useResizableCommentsPanel());
    expect(result.current.handleProps["aria-valuenow"]).toBe(240);
    expect(result.current.handleProps["aria-valuemin"]).toBe(200);
    expect(result.current.handleProps["aria-valuemax"]).toBe(640);

    // The value tracks live resizes.
    act(() => {
      result.current.handleProps.onKeyDown({
        key: "ArrowLeft",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });
    expect(result.current.handleProps["aria-valuenow"]).toBe(260);

    attachContainer(result.current.containerRef, { parentWidth: 500, panelRight: 500 });
    rerender();
    expect(result.current.handleProps["aria-valuemax"]).toBe(252);
    unmount();
  });

  it("seeds desktop state from the canonical media query, not innerWidth", () => {
    setInnerWidth(2000);
    mockMatchMedia({ "(min-width: 768px)": false });
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());

    expect(result.current.isDesktop).toBe(false);
    expect(result.current.width).toBeUndefined();
    unmount();
  });

  it("keeps the separator contract the consumer's markup relies on", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    expect(result.current.handleProps.role).toBe("separator");
    expect(result.current.handleProps["aria-label"]).toBe("Resize comments panel");
    expect(result.current.handleProps.tabIndex).toBe(0);
    unmount();
  });
});

describe("useResizableCommentsPanel drag overlay", () => {
  it("shields iframes with a full-window overlay for the duration of the drag", () => {
    // The divider sits beside the HTML-preview iframe. If capture is lost,
    // the overlay keeps the pointer stream in the parent document so the
    // release is never swallowed by the frame.
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    const target = makeHandleTarget();
    expect(overlaySelector()).toBeNull();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, {})));
    const overlay = overlaySelector();
    expect(overlay).not.toBeNull();
    expect(overlay?.style.cursor).toBe("col-resize");

    act(() => result.current.handleProps.onPointerUp(pointerEvent(target, {})));
    expect(overlaySelector()).toBeNull();
    unmount();
  });

  it("removes the overlay and restores body styles if unmounted mid-drag", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    const target = makeHandleTarget();
    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, {})));
    expect(overlaySelector()).not.toBeNull();
    expect(document.body.style.cursor).toBe("col-resize");

    unmount();
    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });
});
