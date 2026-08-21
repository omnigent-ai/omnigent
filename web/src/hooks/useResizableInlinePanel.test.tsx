import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { resetWidthStoreForTesting, useResizableInlinePanel } from "./useResizableInlinePanel";

// useResizableInlinePanel keeps its width in a module-level store shared across
// all callers, re-seeded per conversation. resetWidthStoreForTesting clears it
// between tests so cases are fully independent. A 2000px viewport gives a
// 1512px clamp ceiling (2000 - 480 chat minimum - 8 gap); the default width
// there is 600 (0.36 * 2000 = 720, clamped to the [420, 600] band).

const SESSION = "conv_test";
const originalInnerWidth = window.innerWidth;

function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

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
  setInnerWidth(2000);
});

afterEach(() => {
  localStorage.clear();
  resetWidthStoreForTesting();
  setInnerWidth(originalInnerWidth);
});

describe("useResizableInlinePanel persistence", () => {
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

  it("scopes the saved width to its session: a different session uses the default", () => {
    const first = renderHook(() => useResizableInlinePanel(SESSION));
    expect(nudgeWiderOnce(first.result)).toBe(620);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(620);
    first.unmount();

    // A second conversation has no saved width, so it falls back to the
    // viewport-derived default (600) rather than inheriting the first's 620.
    const second = renderHook(() => useResizableInlinePanel("conv_other"));
    expect(second.result.current.panelWidth).toBe(600);
    expect(readSessionWorkspaceState("conv_other").widthPx).toBeUndefined();
    second.unmount();
  });

  it("re-derives from the preference on resize: clamps down on shrink, springs back on widen", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));

    // Establish a persisted preference of 620 (default 600 + one ArrowLeft step).
    expect(nudgeWiderOnce(result)).toBe(620);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(620);

    // Shrinking the viewport clamps the live width to the chat-preserving
    // ceiling (700 - 480 chat - 8 gap = 212). The chat's 480 floor wins over
    // the panel's own 240 comfort minimum, so the panel yields below 240 rather
    // than squeeze the chat. The saved 620 preference is untouched.
    setInnerWidth(700);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.panelWidth).toBe(212);
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
    // Drag the panel out to its sidebar-collapsed ceiling: 1400 - 480 - 8 = 912.
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
    expect(collapsed.result.current.panelWidth).toBe(912);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(912);
    collapsed.unmount();

    // Sidebar open (320px): the ceiling drops to 1400 - 320 - 480 - 8 = 592, so
    // the rendered width is squeezed but the saved preference is untouched.
    const open = renderHook(() => useResizableInlinePanel(SESSION, undefined, 320));
    expect(open.result.current.panelWidth).toBe(592);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(912);
    open.unmount();

    // Collapsing restores the full preferred width.
    const reopened = renderHook(() => useResizableInlinePanel(SESSION, undefined, 0));
    expect(reopened.result.current.panelWidth).toBe(912);
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
    // 1400 - 320 sidebar - 8 gap - panel >= 480 for the chat.
    expect(1400 - 320 - result.current.panelWidth - 8).toBeGreaterThanOrEqual(480);
    unmount();
  });

  it("keeps the chat >= 480px when the viewport shrinks with both sidebars open", () => {
    // The reported bug: with the left sidebar open (reserved) AND the rail wide,
    // shrinking the window let the chat fall under 480 — the panel's own 240
    // comfort minimum was overriding the chat-preserving ceiling, and a resize
    // that didn't move the stored width never re-rendered. The chat floor must
    // win and the recompute must fire on every resize.
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
    // chat = viewport - sidebar - gap - panel.
    expect(1000 - reservedPx - 8 - result.current.panelWidth).toBeGreaterThanOrEqual(480);
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
      // Browser cancellation or capture loss keeps the last applied width,
      // ends the drag, and never persists a half-finished resize.
      const { result } = renderHook(() => useResizableInlinePanel(SESSION));
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

      expect(result.current.panelWidth).toBe(800);
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
    expect(result.current.panelWidth).toBe(800);

    rerender({ enabled: false });

    expect(result.current.panelWidth).toBe(800);
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
    expect(result.current.panelWidth).toBe(800);
  });

  it("returns a 24px fine-pointer target without annexing the transcript", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));

    expect(result.current.handleProps.style).toMatchObject({
      touchAction: "none",
      boxSizing: "content-box",
      paddingLeft: 6,
      paddingRight: 14,
      marginLeft: -6,
      marginRight: -18,
      backgroundClip: "content-box",
    });
  });

  it("exposes separator value semantics that track keyboard resizing", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));

    expect(result.current.handleProps).toMatchObject({
      role: "separator",
      "aria-valuenow": 600,
      "aria-valuemin": 240,
      "aria-valuemax": 1512,
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
        return query === "(pointer: coarse)" ? coarse : false;
      },
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        if (query === "(pointer: coarse)") onChange = listener;
      },
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;

    try {
      const { result } = renderHook(() => useResizableInlinePanel(SESSION));
      coarse = true;
      act(() => onChange?.({ matches: true } as MediaQueryListEvent));

      expect(result.current.handleProps.style).toMatchObject({
        paddingLeft: 6,
        paddingRight: 16,
        marginLeft: -6,
        marginRight: -20,
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
    const targetWidth = 4 + Number(style?.paddingLeft ?? 0) + Number(style?.paddingRight ?? 0);
    const chatOverlap = targetWidth + Number(style?.marginRight ?? 0);
    expect(chatOverlap).toBeLessThanOrEqual(6);
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
