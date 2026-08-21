import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import { resetSidebarWidthStoreForTesting, useResizableSidebar } from "./useResizableSidebar";

// useResizableSidebar keeps its width in a module-level store shared across all
// callers. resetSidebarWidthStoreForTesting resets it between tests so cases
// are independent. A 2000px viewport gives a 1000px ceiling (50vw).

const originalInnerWidth = window.innerWidth;

function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

// Simulate one keyboard step on the public handle. ArrowRight widens by 20px
// (right edge of a left panel), ArrowLeft narrows. Returns the resulting width.
function nudge(
  result: { current: ReturnType<typeof useResizableSidebar> },
  key: "ArrowRight" | "ArrowLeft",
): number {
  act(() =>
    result.current.handleProps.onKeyDown({
      key,
      preventDefault: () => {},
    } as React.KeyboardEvent),
  );
  return result.current.width;
}

function createHandle() {
  const handle = document.createElement("div");
  handle.dataset.testResizeHandle = "true";
  document.body.appendChild(handle);
  const capturedPointers = new Set<number>();
  handle.setPointerCapture = vi.fn((pointerId: number) => capturedPointers.add(pointerId));
  handle.hasPointerCapture = vi.fn((pointerId: number) => capturedPointers.has(pointerId));
  handle.releasePointerCapture = vi.fn((pointerId: number) => capturedPointers.delete(pointerId));
  return handle;
}

function pointerEvent(
  handle: HTMLDivElement,
  overrides: Partial<{
    pointerId: number;
    pointerType: string;
    button: number;
    clientX: number;
  }> = {},
): React.PointerEvent<HTMLElement> {
  return {
    pointerId: 1,
    pointerType: "touch",
    button: 0,
    clientX: 0,
    currentTarget: handle,
    preventDefault: () => {},
    ...overrides,
  } as unknown as React.PointerEvent<HTMLElement>;
}

function documentPointerEvent(
  type: "pointerup" | "pointercancel",
  pointerId: number,
): PointerEvent {
  const event = new Event(type) as PointerEvent;
  Object.defineProperty(event, "pointerId", { value: pointerId });
  return event;
}

function startDrag(
  result: { current: ReturnType<typeof useResizableSidebar> },
  handle: HTMLDivElement,
  overrides: Parameters<typeof pointerEvent>[1] = {},
): void {
  act(() => result.current.handleProps.onPointerDown(pointerEvent(handle, overrides)));
}

// Simulate a drag: press the handle, move the captured pointer, then release.
// For a left panel the live width tracks the cursor's distance from the
// viewport's left edge (clientX).
function dragTo(
  result: { current: ReturnType<typeof useResizableSidebar> },
  clientX: number,
): void {
  const handle = createHandle();
  startDrag(result, handle);
  act(() => result.current.handleProps.onPointerMove(pointerEvent(handle, { clientX })));
  act(() => result.current.handleProps.onPointerUp(pointerEvent(handle)));
}

beforeEach(() => {
  setInnerWidth(2000);
});

afterEach(() => {
  document.querySelectorAll("[data-test-resize-handle]").forEach((handle) => handle.remove());
  localStorage.clear();
  resetSidebarWidthStoreForTesting();
  setInnerWidth(originalInnerWidth);
});

describe("useResizableSidebar", () => {
  it("defaults to 320px with no saved preference", () => {
    const { result } = renderHook(() => useResizableSidebar());
    expect(result.current.width).toBe(320);
    // A pristine default is not a user choice, so nothing is persisted.
    expect(readPanelSizePreference("sidebarWidthPx")).toBeNull();
  });

  it("exposes separator value semantics that track keyboard resizing", () => {
    const { result } = renderHook(() => useResizableSidebar());

    expect(result.current.handleProps).toMatchObject({
      role: "separator",
      "aria-valuenow": 320,
      "aria-valuemin": 220,
      "aria-valuemax": 1000,
    });

    expect(nudge(result, "ArrowRight")).toBe(340);
    expect(result.current.handleProps["aria-valuenow"]).toBe(340);
  });

  it("widens on ArrowRight and narrows on ArrowLeft, persisting each step", () => {
    const { result } = renderHook(() => useResizableSidebar());

    expect(nudge(result, "ArrowRight")).toBe(340); // 320 + 20
    expect(readPanelSizePreference("sidebarWidthPx")).toBe(340);

    expect(nudge(result, "ArrowLeft")).toBe(320); // back down 20
    expect(readPanelSizePreference("sidebarWidthPx")).toBe(320);
  });

  it("clamps between 220px and half the viewport", () => {
    const { result } = renderHook(() => useResizableSidebar());

    // Drag far past the right edge — capped at half of the 2000px viewport.
    dragTo(result, 1500);
    expect(result.current.width).toBe(1000);

    // Drag below the floor — held at 220, not 50.
    dragTo(result, 50);
    expect(result.current.width).toBe(220);
  });

  it("persists a drag and restores it after a store reset (reload)", () => {
    const { result, unmount } = renderHook(() => useResizableSidebar());

    dragTo(result, 400);
    expect(result.current.width).toBe(400);
    expect(readPanelSizePreference("sidebarWidthPx")).toBe(400);

    unmount();
    resetSidebarWidthStoreForTesting();
    const restored = renderHook(() => useResizableSidebar());
    expect(restored.result.current.width).toBe(400);
    restored.unmount();
  });

  it("clamps down on viewport shrink and springs back to the saved width on widen", () => {
    const { result } = renderHook(() => useResizableSidebar());

    // Establish a 900px preference (under the 1000px ceiling at 2000px).
    dragTo(result, 900);
    expect(result.current.width).toBe(900);
    expect(readPanelSizePreference("sidebarWidthPx")).toBe(900);

    // Shrink the viewport: ceiling = 700*0.5 = 350. Live width clamps down to
    // 350 but the saved 900 preference is untouched.
    setInnerWidth(700);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.width).toBe(350);
    expect(readPanelSizePreference("sidebarWidthPx")).toBe(900);

    // Widen again: re-derives from the preference, restoring 900 in-session.
    setInnerWidth(2000);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.width).toBe(900);
  });

  it("captures pointer drags on the handle and exposes touch-safe affordances", () => {
    const { result } = renderHook(() => useResizableSidebar());
    const handle = createHandle();

    startDrag(result, handle, { pointerId: 7 });
    expect(handle.setPointerCapture).toHaveBeenCalledWith(7);
    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");
    expect(result.current.handleProps.style).toEqual({
      touchAction: "none",
      boxSizing: "content-box",
      paddingInlineStart: 9,
      paddingInlineEnd: 11,
      marginInlineStart: -8,
      marginInlineEnd: -10,
      backgroundClip: "content-box",
    });

    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(handle, { pointerId: 7, clientX: 480 }),
      ),
    );
    expect(result.current.width).toBe(480);
    act(() => result.current.handleProps.onPointerUp(pointerEvent(handle, { pointerId: 7 })));

    expect(readPanelSizePreference("sidebarWidthPx")).toBe(480);
    expect(handle.releasePointerCapture).toHaveBeenCalledWith(7);
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });

  it("reacts to primary-pointer coarseness with asymmetric hit padding", () => {
    let coarse = false;
    const listeners = new Set<() => void>();
    const query = {
      get matches() {
        return coarse;
      },
      media: "(pointer: coarse)",
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn((_type: string, listener: () => void) => listeners.add(listener)),
      removeEventListener: vi.fn((_type: string, listener: () => void) =>
        listeners.delete(listener),
      ),
      dispatchEvent: vi.fn(() => false),
    } as MediaQueryList;
    const matchMedia = vi.spyOn(window, "matchMedia").mockReturnValue(query);
    const { result, unmount } = renderHook(() => useResizableSidebar());

    expect(result.current.handleProps.style.paddingInlineStart).toBe(9);
    expect(result.current.handleProps.style.paddingInlineEnd).toBe(11);

    coarse = true;
    act(() => listeners.forEach((listener) => listener()));
    expect(result.current.handleProps.style.paddingInlineStart).toBe(10);
    expect(result.current.handleProps.style.paddingInlineEnd).toBe(12);

    unmount();
    expect(query.removeEventListener).toHaveBeenCalledWith("change", expect.any(Function));
    matchMedia.mockRestore();
  });

  it("stays idle when pointer capture throws", () => {
    const { result } = renderHook(() => useResizableSidebar());
    const handle = createHandle();
    handle.setPointerCapture = vi.fn(() => {
      throw new DOMException("capture failed");
    });
    const preventDefault = vi.fn();

    act(() =>
      result.current.handleProps.onPointerDown({
        ...pointerEvent(handle),
        preventDefault,
      } as React.PointerEvent<HTMLElement>),
    );

    expect(preventDefault).not.toHaveBeenCalled();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    act(() => result.current.handleProps.onPointerMove(pointerEvent(handle, { clientX: 500 })));
    expect(result.current.width).toBe(320);
  });

  it("ignores concurrent pointers until the captured pointer ends", () => {
    const { result } = renderHook(() => useResizableSidebar());
    const handle = createHandle();

    startDrag(result, handle, { pointerId: 1 });
    startDrag(result, handle, { pointerId: 2 });
    expect(handle.setPointerCapture).toHaveBeenCalledTimes(1);

    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(handle, { pointerId: 2, clientX: 700 }),
      ),
    );
    expect(result.current.width).toBe(320);
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(handle, { pointerId: 1, clientX: 450 }),
      ),
    );
    expect(result.current.width).toBe(450);
  });

  it("does not start a drag from a pen barrel button", () => {
    const { result } = renderHook(() => useResizableSidebar());
    const handle = createHandle();

    startDrag(result, handle, { pointerType: "pen", button: 2 });
    expect(handle.setPointerCapture).not.toHaveBeenCalled();
    expect(document.body.style.cursor).toBe("");

    act(() => result.current.handleProps.onPointerMove(pointerEvent(handle, { clientX: 500 })));
    expect(result.current.width).toBe(320);
  });

  it.each(["onPointerCancel", "onLostPointerCapture"] as const)(
    "keeps the last applied width without persisting on %s",
    (abortHandler) => {
      const { result } = renderHook(() => useResizableSidebar());
      const handle = createHandle();

      startDrag(result, handle, { pointerId: 3 });
      act(() =>
        result.current.handleProps.onPointerMove(
          pointerEvent(handle, { pointerId: 3, clientX: 500 }),
        ),
      );
      expect(result.current.width).toBe(500);

      act(() => result.current.handleProps[abortHandler](pointerEvent(handle, { pointerId: 3 })));
      expect(result.current.width).toBe(500);
      expect(readPanelSizePreference("sidebarWidthPx")).toBeNull();
      expect(document.body.style.cursor).toBe("");
      expect(document.body.style.userSelect).toBe("");
    },
  );

  it("keeps the last applied width without persisting on unmount", () => {
    const { result, unmount } = renderHook(() => useResizableSidebar());
    const handle = createHandle();

    startDrag(result, handle, { pointerId: 9 });
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(handle, { pointerId: 9, clientX: 560 }),
      ),
    );
    expect(result.current.width).toBe(560);

    unmount();
    expect(readPanelSizePreference("sidebarWidthPx")).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    const remounted = renderHook(() => useResizableSidebar());
    expect(remounted.result.current.width).toBe(560);
    remounted.unmount();
  });

  it("aborts when the handle unmounts mid-drag and allows the next drag", async () => {
    const { result } = renderHook(() => useResizableSidebar());
    const firstHandle = createHandle();

    startDrag(result, firstHandle, { pointerId: 11 });
    expect(document.body.style.cursor).toBe("col-resize");
    firstHandle.remove();

    await waitFor(() => expect(document.body.style.cursor).toBe(""));
    expect(document.body.style.userSelect).toBe("");
    expect(readPanelSizePreference("sidebarWidthPx")).toBeNull();

    const nextHandle = createHandle();
    startDrag(result, nextHandle, { pointerId: 12 });
    expect(nextHandle.setPointerCapture).toHaveBeenCalledWith(12);
    expect(document.body.style.cursor).toBe("col-resize");
    act(() =>
      result.current.handleProps.onPointerCancel(pointerEvent(nextHandle, { pointerId: 12 })),
    );
  });

  it("uses document fallbacks when the captured handle misses the terminal event", () => {
    const { result } = renderHook(() => useResizableSidebar());
    const handle = createHandle();

    startDrag(result, handle, { pointerId: 13 });
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(handle, { pointerId: 13, clientX: 620 }),
      ),
    );
    act(() => document.dispatchEvent(documentPointerEvent("pointerup", 13)));

    expect(readPanelSizePreference("sidebarWidthPx")).toBe(620);
    expect(document.body.style.cursor).toBe("");
  });
});
