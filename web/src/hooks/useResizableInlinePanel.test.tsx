import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { setInnerWidth } from "./resizeHookTestHelpers";
import { resetWidthStoreForTesting, useResizableInlinePanel } from "./useResizableInlinePanel";

// useResizableInlinePanel keeps its width in a module-level store shared across
// all callers, re-seeded per storage key. resetWidthStoreForTesting clears it
// between tests so cases are fully independent. A 2000px viewport gives a
// 1508px clamp ceiling (2000 - 480 chat minimum - 12px gutter); the default width
// there is 600 (0.36 * 2000 = 720, clamped to the [420, 600] band).

const SESSION = "conv_test";
const originalInnerWidth = window.innerWidth;

// Simulate a manual resize via the public keyboard handle (ArrowLeft widens by
// 20px). Returns the resulting panelWidth.
function nudgeWiderOnce(result: { current: ReturnType<typeof useResizableInlinePanel> }): number {
  act(() =>
    result.current.handleProps.onKeyDown({
      key: "ArrowLeft",
      preventDefault: () => {},
    } as React.KeyboardEvent),
  );
  return result.current.panelWidth;
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
  overrides: Partial<{
    pointerId: number;
    pointerType: string;
    button: number;
    clientX: number;
    preventDefault: () => void;
  }> = {},
): React.PointerEvent<HTMLElement> {
  return {
    currentTarget: element,
    pointerId: 1,
    pointerType: "touch",
    button: 0,
    clientX: 0,
    preventDefault: () => {},
    ...overrides,
  } as React.PointerEvent<HTMLElement>;
}

function dispatchDocumentPointer(type: "pointerup" | "pointercancel", pointerId: number): void {
  const event = new Event(type, { bubbles: true });
  Object.defineProperty(event, "pointerId", { value: pointerId });
  document.dispatchEvent(event);
}

const overlaySelector = () =>
  [...document.body.children].find(
    (c): c is HTMLElement =>
      c instanceof HTMLElement && c.style.position === "fixed" && c.style.zIndex === "2147483647",
  ) ?? null;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame"] });
  setInnerWidth(2000);
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
  resetWidthStoreForTesting();
  setInnerWidth(originalInnerWidth);
});

function paintResizeFrame() {
  expect(vi.getTimerCount()).toBe(1);
  act(() => vi.advanceTimersToNextFrame());
}

describe("useResizableInlinePanel persistence", () => {
  it("preserves panel handle prop identity across unchanged rerenders", () => {
    const { result, rerender } = renderHook(() => useResizableInlinePanel(SESSION));
    const handlers = result.current.handleProps;

    rerender();
    expect(result.current.handleProps).toBe(handlers);
    rerender();
    expect(result.current.handleProps).toBe(handlers);
  });

  it("coalesces move bursts into one shared publication per frame and flushes before persistence", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));
    const publish = vi.fn();
    const subscriber = renderHook(() => {
      const panel = useResizableInlinePanel(SESSION);
      publish(panel.panelWidth);
      return panel;
    });
    const handle = createPointerHandle();
    act(() => result.current.handleProps.onPointerDown(pointerEvent(handle.element)));
    publish.mockClear();

    for (const clientX of [1200, 1100, 1000]) {
      act(() =>
        result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX })),
      );
    }

    expect(result.current.panelWidth).toBe(600);
    expect(publish).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersToNextFrame());
    expect(publish.mock.calls).toEqual([[1000]]);
    expect(subscriber.result.current.panelWidth).toBe(1000);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBeUndefined();

    act(() =>
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 900 })),
    );
    expect(publish.mock.calls).toEqual([[1000]]);
    act(() => result.current.handleProps.onPointerUp(pointerEvent(handle.element)));
    expect(publish.mock.calls).toEqual([[1000], [1100]]);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(1100);
    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersToNextFrame());
    expect(publish).toHaveBeenCalledTimes(2);
  });

  it("cancels pending frame publication before rolling back a resize", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));
    expect(nudgeWiderOnce(result)).toBe(620);
    const handle = createPointerHandle();
    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 1200 }));
    });
    expect(result.current.panelWidth).toBe(620);
    act(() => vi.advanceTimersToNextFrame());
    expect(result.current.panelWidth).toBe(800);
    act(() =>
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 1100 })),
    );
    expect(vi.getTimerCount()).toBe(1);
    act(() => result.current.handleProps.onPointerCancel(pointerEvent(handle.element)));
    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersToNextFrame());
    expect(result.current.panelWidth).toBe(620);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(620);
  });

  it("persists explicit keyboard resize per session and restores it after store reset", () => {
    const { result, unmount } = renderHook(() => useResizableInlinePanel(SESSION));

    // Default 600 + one ArrowLeft step (20px) = 620, persisted under the
    // session key.
    const afterNudge = nudgeWiderOnce(result);
    expect(afterNudge).toBe(620);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(620);

    unmount();
    resetWidthStoreForTesting();
    const restored = renderHook(() => useResizableInlinePanel(SESSION));

    // The saved manual width wins over the viewport-derived default of 600.
    expect(restored.result.current.panelWidth).toBe(620);
    restored.unmount();
  });

  it("scopes the saved width to its root-tree key: a different tree uses the default", () => {
    const rootTreeKey = "conv_root";
    const first = renderHook(() => useResizableInlinePanel(rootTreeKey));
    expect(nudgeWiderOnce(first.result)).toBe(620);
    expect(readSessionWorkspaceState(rootTreeKey).widthPx).toBe(620);
    first.unmount();

    // A second root tree has no saved width, so it falls back to the
    // viewport-derived default (600) rather than inheriting the first's 620.
    const second = renderHook(() => useResizableInlinePanel("conv_other_root"));
    expect(second.result.current.panelWidth).toBe(600);
    expect(readSessionWorkspaceState("conv_other_root").widthPx).toBeUndefined();
    second.unmount();
  });

  it("re-derives from the preference on resize: clamps down on shrink, springs back on widen", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));

    // Establish a persisted preference of 620 (default 600 + one ArrowLeft step).
    expect(nudgeWiderOnce(result)).toBe(620);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(620);

    // Shrinking the viewport clamps the live width to the chat-preserving
    // ceiling. At 700px the chat cedes below its 480 comfort minimum down
    // toward its hard floor so the rail keeps a drag range: the ceiling is
    // 240 rail floor + 120 travel = 360 (chat keeps 700 - 360 - 12 = 328).
    // The saved 620 preference is untouched.
    setInnerWidth(700);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.panelWidth).toBe(360);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(620);

    // Widening again re-derives from the preference, restoring 620 in-session.
    setInnerWidth(2000);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.panelWidth).toBe(620);
  });
});

describe("useResizableInlinePanel reserved width (sidebar)", () => {
  // `reservedPx` is the open sidebar's width. It must tighten the ceiling
  // (keeping the chat at its 480px minimum) without overwriting the user's
  // preferred width, so collapsing the sidebar gives the width straight back.
  it("caps at the sidebar-aware ceiling and restores the preference when it collapses", () => {
    setInnerWidth(1400);
    // Drag the panel out to its sidebar-collapsed ceiling: 1400 - 480 - 12 = 908.
    const collapsed = renderHook(() =>
      useResizableInlinePanel(SESSION, undefined, /* reservedPx */ 0),
    );
    const handle = createPointerHandle();
    act(() => {
      collapsed.result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      collapsed.result.current.handleProps.onPointerMove(
        pointerEvent(handle.element, { clientX: 100 }),
      );
      collapsed.result.current.handleProps.onPointerUp(pointerEvent(handle.element));
    });
    expect(collapsed.result.current.panelWidth).toBe(908);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(908);
    collapsed.unmount();

    // Sidebar open (320px): the ceiling drops to 1400 - 320 - 480 - 12 = 588, so
    // the rendered width is squeezed but the saved preference is untouched.
    const open = renderHook(() => useResizableInlinePanel(SESSION, undefined, 320));
    expect(open.result.current.panelWidth).toBe(588);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(908);
    open.unmount();

    // Collapsing restores the full preferred width.
    const reopened = renderHook(() => useResizableInlinePanel(SESSION, undefined, 0));
    expect(reopened.result.current.panelWidth).toBe(908);
    reopened.unmount();
  });

  it("leaves the chat its 480px minimum with the sidebar open", () => {
    setInnerWidth(1400);
    // A preference far wider than the sidebar-open ceiling allows.
    const { result, unmount } = renderHook(() => useResizableInlinePanel(SESSION, undefined, 320));
    act(() => {
      for (let i = 0; i < 60; i++) {
        result.current.handleProps.onKeyDown({
          key: "ArrowLeft",
          preventDefault: () => {},
        } as React.KeyboardEvent);
      }
    });
    // 1400 - 320 sidebar - 12px gutter - panel >= 480 for the chat.
    expect(1400 - 320 - result.current.panelWidth - 12).toBeGreaterThanOrEqual(480);
    unmount();
  });

  it("keeps the chat above its hard floor when the viewport shrinks with both sidebars open", () => {
    // The reported bug: with the left sidebar open (reserved) AND the rail wide,
    // shrinking the window let the chat collapse entirely — the panel's own 240
    // comfort minimum was overriding the chat-preserving ceiling, and a resize
    // that didn't move the stored width never re-rendered. The chat floor must
    // win and the recompute must fire on every resize. At 1000px with the
    // sidebar open the row is tablet-cramped, so the chat holds its 240 hard
    // floor (not the 480 comfort minimum) while the rail keeps its drag range.
    setInnerWidth(1400);
    const reservedPx = 320; // open left sidebar
    const { result, rerender } = renderHook(
      ({ reserved }) => useResizableInlinePanel(SESSION, undefined, reserved),
      { initialProps: { reserved: reservedPx } },
    );
    // Drag the rail out to its widest at this viewport.
    const handle = createPointerHandle();
    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      result.current.handleProps.onPointerMove(pointerEvent(handle.element));
      result.current.handleProps.onPointerUp(pointerEvent(handle.element));
    });

    // Now shrink the viewport hard. Even though the stored (no-reserve) width may
    // still fit its own ceiling, the render-time reserve clamp must re-run.
    setInnerWidth(1000);
    act(() => window.dispatchEvent(new Event("resize")));
    rerender({ reserved: reservedPx });
    // chat = viewport - sidebar - gutter - panel.
    expect(1000 - reservedPx - 12 - result.current.panelWidth).toBeGreaterThanOrEqual(240);
  });
});

describe("useResizableInlinePanel tablet-width viewports", () => {
  // On unfolded-foldable / tablet widths (~768–1100px) the sidebar defaults
  // open, and reserving it plus the chat's full 480px comfort minimum used to
  // consume the whole row: the clamp's floor collapsed onto its ceiling, so
  // every drag computed the same width and resize gestures were no-ops. The
  // chat must cede below its comfort minimum (down to a hard floor) before
  // the rail loses its drag range.
  it("keeps a usable drag range at 1024px with the sidebar open", () => {
    setInnerWidth(1024);
    const { result } = renderHook(() => useResizableInlinePanel(SESSION, undefined, 320));
    const handle = createPointerHandle();

    // Shrink toward the rail's floor…
    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 784 }));
    });
    paintResizeFrame();
    const narrow = result.current.panelWidth;
    expect(narrow).toBe(240);

    // …then widen back out. Before the tablet-aware clamp this move was a
    // no-op: floor and ceiling had collapsed onto the same pinned value.
    act(() => {
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 600 }));
      result.current.handleProps.onPointerUp(pointerEvent(handle.element));
    });
    expect(result.current.panelWidth).toBeGreaterThan(narrow + 100);

    // The chat still keeps its hard floor at the rail's widest.
    expect(1024 - 320 - 12 - result.current.panelWidth).toBeGreaterThanOrEqual(240);
  });

  it("keeps the resize handle live at 840px with the sidebar open", () => {
    // 840px is an unfolded Pixel Fold. With a 320px sidebar the old ceiling
    // was 32px: the rail rendered as a crushed sliver with a dead handle.
    setInnerWidth(840);
    const { result } = renderHook(() => useResizableInlinePanel(SESSION, undefined, 320));

    expect(result.current.handleProps["aria-disabled"]).toBe(false);
    expect(result.current.panelWidth).toBeGreaterThanOrEqual(240);

    const handle = createPointerHandle();
    const widest = result.current.panelWidth;
    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 600 }));
      result.current.handleProps.onPointerUp(pointerEvent(handle.element));
    });
    expect(result.current.panelWidth).toBe(240);
    expect(result.current.panelWidth).toBeLessThan(widest);
  });
});

describe("useResizableInlinePanel pointer drag", () => {
  it("captures the pointer and persists the final width on release", () => {
    // Without setPointerCapture, a drag that leaves the 1px handle (or crosses
    // the HTML-preview iframe) loses the pointer stream and the rail sticks.
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));
    const handle = createPointerHandle();

    act(() =>
      result.current.handleProps.onPointerDown(pointerEvent(handle.element, { pointerId: 7 })),
    );
    expect(handle.setPointerCapture).toHaveBeenCalledWith(7);

    // 2000px viewport, cursor at 1200 → width = innerWidth - clientX = 800.
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(handle.element, { pointerId: 7, clientX: 1200 }),
      ),
    );

    // Live width tracks the drag, but nothing is written to storage mid-drag —
    // persisting per pointermove would fire a synchronous write on every frame.
    paintResizeFrame();
    expect(result.current.panelWidth).toBe(800);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBeUndefined();

    act(() =>
      result.current.handleProps.onPointerUp(pointerEvent(handle.element, { pointerId: 7 })),
    );

    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(800);
    expect(handle.releasePointerCapture).toHaveBeenCalledWith(7);
  });

  it("stays idle when pointer capture throws", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));
    const handle = createPointerHandle();
    const preventDefault = vi.fn();
    handle.setPointerCapture.mockImplementationOnce(() => {
      throw new DOMException("capture unavailable");
    });

    act(() =>
      result.current.handleProps.onPointerDown(
        pointerEvent(handle.element, { pointerId: 7, preventDefault }),
      ),
    );

    expect(preventDefault).not.toHaveBeenCalled();
    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(handle.element, { pointerId: 7, clientX: 1200 }),
      ),
    );
    expect(result.current.panelWidth).toBe(600);
  });

  it.each(["onPointerCancel", "onLostPointerCapture"] as const)(
    "aborts cleanly without persisting through %s",
    (abortHandler) => {
      // Browser cancellation or capture loss restores the pre-drag width,
      // ends the drag, and never persists a half-finished resize.
      const { result } = renderHook(() => useResizableInlinePanel(SESSION));
      const handle = createPointerHandle();

      act(() => {
        result.current.handleProps.onPointerDown(pointerEvent(handle.element, { pointerId: 11 }));
        result.current.handleProps.onPointerMove(
          pointerEvent(handle.element, { pointerId: 11, clientX: 1200 }),
        );
      });
      paintResizeFrame();
      expect(result.current.panelWidth).toBe(800);

      act(() => {
        result.current.handleProps[abortHandler](pointerEvent(handle.element, { pointerId: 11 }));
        result.current.handleProps.onPointerMove(
          pointerEvent(handle.element, { pointerId: 11, clientX: 1400 }),
        );
      });

      expect(result.current.panelWidth).toBe(600);
      expect(readSessionWorkspaceState(SESSION).widthPx).toBeUndefined();
      expect(document.body.style.cursor).toBe("");
      expect(document.body.style.userSelect).toBe("");
    },
  );

  it("does not start a drag from a secondary pen button", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));
    const handle = createPointerHandle();

    act(() =>
      result.current.handleProps.onPointerDown(
        pointerEvent(handle.element, { pointerType: "pen", button: 2 }),
      ),
    );
    expect(handle.setPointerCapture).not.toHaveBeenCalled();

    act(() =>
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 1200 })),
    );
    expect(result.current.panelWidth).toBe(600);
  });

  it("finishes through the document fallback if the handle unmounts mid-drag", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));
    const firstHandle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(firstHandle.element, { pointerId: 5 }));
      result.current.handleProps.onPointerMove(
        pointerEvent(firstHandle.element, { pointerId: 5, clientX: 1200 }),
      );
      firstHandle.element.remove();
      dispatchDocumentPointer("pointerup", 5);
    });

    expect(result.current.panelWidth).toBe(800);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(800);
    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    const nextHandle = createPointerHandle();
    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(nextHandle.element, { pointerId: 6 }));
      result.current.handleProps.onPointerMove(
        pointerEvent(nextHandle.element, { pointerId: 6, clientX: 1100 }),
      );
    });
    expect(nextHandle.setPointerCapture).toHaveBeenCalledWith(6);
    paintResizeFrame();
    expect(result.current.panelWidth).toBe(900);
  });

  it("aborts without persisting when the panel-enabled gate flips false", () => {
    const { result, rerender } = renderHook(
      ({ enabled }) => useResizableInlinePanel(SESSION, undefined, 0, enabled),
      { initialProps: { enabled: true } },
    );
    const handle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 1200 }));
    });
    paintResizeFrame();
    expect(result.current.panelWidth).toBe(800);

    rerender({ enabled: false });

    expect(result.current.panelWidth).toBe(600);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBeUndefined();
    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });

  it("aborts without persisting when its root-tree key becomes tentative", () => {
    const { result, rerender } = renderHook(
      ({ persistEnabled }) => useResizableInlinePanel(SESSION, undefined, 0, true, persistEnabled),
      { initialProps: { persistEnabled: true } },
    );
    const handle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 1200 }));
    });
    paintResizeFrame();
    expect(result.current.panelWidth).toBe(800);

    rerender({ persistEnabled: false });

    expect(result.current.panelWidth).toBe(600);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBeUndefined();
    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });

  it("does not start pointer or keyboard resize while disabled", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION, undefined, 0, false));
    const handle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 1200 }));
      result.current.handleProps.onPointerUp(pointerEvent(handle.element));
      result.current.handleProps.onKeyDown({
        key: "ArrowLeft",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    expect(handle.setPointerCapture).not.toHaveBeenCalled();
    expect(result.current.handleProps["aria-disabled"]).toBe(true);
    expect(result.current.handleProps.hidden).toBe(true);
    expect(result.current.handleProps.tabIndex).toBe(-1);
    expect(result.current.panelWidth).toBe(600);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBeUndefined();
    expect(overlaySelector()).toBeNull();
  });

  it("keeps a tentative root-tree gutter visible but blocks resize input", () => {
    const { result } = renderHook(() =>
      useResizableInlinePanel(SESSION, undefined, 0, true, false),
    );
    const handle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 1200 }));
      result.current.handleProps.onPointerUp(pointerEvent(handle.element));
      result.current.handleProps.onKeyDown({
        key: "ArrowLeft",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    expect(handle.setPointerCapture).not.toHaveBeenCalled();
    expect(result.current.handleProps["aria-disabled"]).toBe(true);
    expect(result.current.handleProps.hidden).toBe(false);
    expect(result.current.handleProps.tabIndex).toBe(-1);
    expect(result.current.panelWidth).toBe(600);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBeUndefined();
    expect(overlaySelector()).toBeNull();
  });

  it("does not start pointer or keyboard resize at a zero-width clamp", () => {
    setInnerWidth(0);
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));
    const handle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      result.current.handleProps.onKeyDown({
        key: "ArrowLeft",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    expect(result.current.panelWidth).toBe(0);
    expect(result.current.handleProps["aria-disabled"]).toBe(true);
    expect(handle.setPointerCapture).not.toHaveBeenCalled();
    expect(readSessionWorkspaceState(SESSION).widthPx).toBeUndefined();
    expect(overlaySelector()).toBeNull();
  });

  it("aborts the old drag before loading a new session", () => {
    const { result, rerender } = renderHook(({ sessionId }) => useResizableInlinePanel(sessionId), {
      initialProps: { sessionId: "conv_old" },
    });
    const handle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(handle.element));
      result.current.handleProps.onPointerMove(pointerEvent(handle.element, { clientX: 1200 }));
    });
    paintResizeFrame();
    expect(result.current.panelWidth).toBe(800);

    rerender({ sessionId: "conv_new" });

    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    act(() => dispatchDocumentPointer("pointerup", 1));

    expect(readSessionWorkspaceState("conv_old").widthPx).toBeUndefined();
    expect(readSessionWorkspaceState("conv_new").widthPx).toBeUndefined();
  });

  it("ignores additional pointers until the active drag ends", () => {
    // A second finger joining a live resize must not steal the stream —
    // first pointer wins until that drag ends.
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));
    const firstHandle = createPointerHandle();
    const secondHandle = createPointerHandle();

    act(() => {
      result.current.handleProps.onPointerDown(pointerEvent(firstHandle.element));
      result.current.handleProps.onPointerDown(
        pointerEvent(secondHandle.element, { pointerId: 2 }),
      );
      result.current.handleProps.onPointerMove(
        pointerEvent(secondHandle.element, { pointerId: 2, clientX: 1400 }),
      );
    });

    expect(firstHandle.setPointerCapture).toHaveBeenCalledWith(1);
    expect(secondHandle.setPointerCapture).not.toHaveBeenCalled();
    expect(result.current.panelWidth).toBe(600);

    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(firstHandle.element, { clientX: 1200 }),
      ),
    );
    paintResizeFrame();
    expect(result.current.panelWidth).toBe(800);
  });

  it("returns a 24px fine-pointer target without annexing the transcript", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));

    expect(result.current.handleProps.style).toMatchObject({
      touchAction: "none",
      boxSizing: "content-box",
      paddingLeft: 9,
      paddingRight: 11,
      marginLeft: -6,
      marginRight: -8,
      backgroundClip: "content-box",
    });
  });

  it("exposes separator value semantics that track keyboard resizing", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));

    expect(result.current.handleProps).toMatchObject({
      role: "separator",
      "aria-valuenow": 600,
      "aria-valuemin": 240,
      "aria-valuemax": 1508,
    });

    expect(nudgeWiderOnce(result)).toBe(620);
    expect(result.current.handleProps["aria-valuenow"]).toBe(620);
  });

  it("reacts to coarse-pointer changes with a tightly bounded 26px target", () => {
    const originalMatchMedia = window.matchMedia;
    let coarse = false;
    let onChange: ((event: MediaQueryListEvent) => void) | undefined;
    window.matchMedia = ((query: string) => ({
      get matches() {
        return query === "(any-pointer: coarse)" ? coarse : false;
      },
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        if (query === "(any-pointer: coarse)") onChange = listener;
      },
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;

    try {
      const { result } = renderHook(() => useResizableInlinePanel(SESSION));
      coarse = true;
      act(() => onChange?.({ matches: true } as MediaQueryListEvent));

      expect(result.current.handleProps.style).toMatchObject({
        paddingLeft: 10,
        paddingRight: 12,
        marginLeft: -6,
        marginRight: -8,
      });
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it("caps the chat-side sliver before the transcript scrollbar thumb", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));

    // TranscriptScrollbar's resting thumb occupies the 6–12px band from the
    // chat edge, so the resize target must stop at or before 6px.
    const style = result.current.handleProps.style;
    expect(-Number(style?.marginLeft ?? 0)).toBeLessThanOrEqual(6);
  });
});

describe("useResizableInlinePanel drag overlay", () => {
  it("mounts a full-window overlay during a drag so moves aren't lost to an iframe", () => {
    // The panel sits beside the sandboxed HTML-preview iframe. Capture plus
    // a shielding overlay keeps the parent receiving the pointer stream when
    // the drag crosses the frame.
    const { result, unmount } = renderHook(() => useResizableInlinePanel(SESSION));
    expect(overlaySelector()).toBeNull();

    const handle = createPointerHandle();
    act(() => result.current.handleProps.onPointerDown(pointerEvent(handle.element)));
    const overlay = overlaySelector();
    expect(overlay).not.toBeNull();
    expect(overlay?.style.cursor).toBe("col-resize");

    act(() => result.current.handleProps.onPointerUp(pointerEvent(handle.element)));
    expect(overlaySelector()).toBeNull();
    unmount();
  });

  it("removes the overlay if unmounted mid-drag", () => {
    const { result, unmount } = renderHook(() => useResizableInlinePanel(SESSION));
    const handle = createPointerHandle();
    act(() => result.current.handleProps.onPointerDown(pointerEvent(handle.element)));
    expect(overlaySelector()).not.toBeNull();

    // Panel closes (e.g. tab switch) while still dragging — cleanup must not
    // leave the transparent overlay swallowing every click on the page.
    unmount();
    expect(overlaySelector()).toBeNull();
  });
});
