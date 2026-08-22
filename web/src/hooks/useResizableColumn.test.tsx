import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useResizableColumn } from "./useResizableColumn";

const defaultMatchMedia = window.matchMedia;

function installMatchMedia(state: Record<string, boolean>) {
  const listeners = new Map<string, Set<() => void>>();
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: vi.fn((query: string) => ({
      get matches() {
        return state[query] ?? false;
      },
      media: query,
      addEventListener: (_: string, callback: () => void) => {
        if (!listeners.has(query)) listeners.set(query, new Set());
        listeners.get(query)!.add(callback);
      },
      removeEventListener: (_: string, callback: () => void) => {
        listeners.get(query)?.delete(callback);
      },
    })),
  });
  return {
    set(query: string, matches: boolean) {
      state[query] = matches;
      for (const callback of listeners.get(query) ?? []) callback();
    },
  };
}

function pointerEvent(
  pointerId: number,
  clientX = 0,
  setPointerCapture = vi.fn(),
  { button = 0, pointerType = "touch" } = {},
): React.PointerEvent & { setPointerCapture: ReturnType<typeof vi.fn> } {
  return {
    pointerId,
    clientX,
    button,
    pointerType,
    preventDefault: vi.fn(),
    currentTarget: { setPointerCapture },
    setPointerCapture,
  } as unknown as React.PointerEvent & { setPointerCapture: ReturnType<typeof vi.fn> };
}

function keyEvent(key: string) {
  return { key, preventDefault: vi.fn() } as unknown as React.KeyboardEvent & {
    preventDefault: ReturnType<typeof vi.fn>;
  };
}

/** Render the hook with a container anchored at the given viewport left edge. */
function renderColumn(containerLeft = 0) {
  const rendered = renderHook(() => useResizableColumn());
  rendered.result.current.containerRef.current = {
    getBoundingClientRect: () => ({ left: containerLeft }),
  } as HTMLElement;
  return rendered;
}

afterEach(() => {
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
  window.matchMedia = defaultMatchMedia;
});

describe("useResizableColumn pointer dragging", () => {
  it("captures the pointer on pointerdown and tracks moves on the captured element", () => {
    const { result } = renderColumn(100);
    const down = pointerEvent(7);

    act(() => result.current.handleProps.onPointerDown(down));
    expect(down.setPointerCapture).toHaveBeenCalledWith(7);
    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");

    // Width follows the pointer, measured from the container's left edge.
    act(() => result.current.handleProps.onPointerMove(pointerEvent(7, 400)));
    expect(result.current.width).toBe(300);

    // Drag clamps to [minWidth, maxWidth] (defaults 100..480).
    act(() => result.current.handleProps.onPointerMove(pointerEvent(7, 100)));
    expect(result.current.width).toBe(100);
    act(() => result.current.handleProps.onPointerMove(pointerEvent(7, 5000)));
    expect(result.current.width).toBe(480);

    act(() => result.current.handleProps.onPointerUp(pointerEvent(7)));
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    // Moves after release no longer resize.
    act(() => result.current.handleProps.onPointerMove(pointerEvent(7, 350)));
    expect(result.current.width).toBe(480);
  });

  it("ignores a second concurrent pointer (first pointer wins)", () => {
    const { result } = renderColumn(0);

    act(() => result.current.handleProps.onPointerDown(pointerEvent(1)));

    // A second finger going down mid-drag must not capture or steal the drag.
    const second = pointerEvent(2);
    act(() => result.current.handleProps.onPointerDown(second));
    expect(second.setPointerCapture).not.toHaveBeenCalled();

    act(() => result.current.handleProps.onPointerMove(pointerEvent(2, 999)));
    expect(result.current.width).toBe(176);

    // The second pointer lifting must not end the first pointer's drag.
    act(() => result.current.handleProps.onPointerUp(pointerEvent(2)));
    act(() => result.current.handleProps.onPointerMove(pointerEvent(1, 300)));
    expect(result.current.width).toBe(300);
  });

  it.each(["onPointerCancel", "onLostPointerCapture"] as const)(
    "aborts cleanly on %s, keeping the last applied width",
    (name) => {
      const { result } = renderColumn(0);

      act(() => result.current.handleProps.onPointerDown(pointerEvent(3)));
      act(() => result.current.handleProps.onPointerMove(pointerEvent(3, 250)));
      expect(result.current.width).toBe(250);

      act(() => result.current.handleProps[name](pointerEvent(3)));
      expect(document.body.style.cursor).toBe("");
      expect(document.body.style.userSelect).toBe("");

      // The aborted pointer is dead: further moves must not resize.
      act(() => result.current.handleProps.onPointerMove(pointerEvent(3, 400)));
      expect(result.current.width).toBe(250);
    },
  );

  it("resets body cursor/selection when unmounted mid-drag", () => {
    const { result, unmount } = renderColumn(0);

    act(() => result.current.handleProps.onPointerDown(pointerEvent(4)));
    expect(document.body.style.cursor).toBe("col-resize");

    unmount();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });

  it("ends the drag via the document fallback when the handle element is gone", () => {
    const { result } = renderColumn(0);

    act(() => result.current.handleProps.onPointerDown(pointerEvent(5)));
    expect(document.body.style.cursor).toBe("col-resize");

    // If the handle unmounts mid-drag (breakpoint flip, terminal exit) its
    // React handlers never fire — the document-level pointerup fallback must
    // still end the drag instead of wedging body styles + activePointerId.
    act(() => {
      const up = new Event("pointerup");
      Object.defineProperty(up, "pointerId", { value: 5 });
      document.dispatchEvent(up);
    });
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    // The drag state is fully released: a fresh pointer can start a new drag.
    act(() => result.current.handleProps.onPointerDown(pointerEvent(6)));
    act(() => result.current.handleProps.onPointerMove(pointerEvent(6, 200)));
    expect(result.current.width).toBe(200);
    act(() => result.current.handleProps.onPointerUp(pointerEvent(6)));
  });

  it.each(["mouse", "pen"] as const)(
    "ignores a secondary-button (%s) pointerdown entirely",
    (pointerType) => {
      const { result } = renderColumn(0);

      // Right-click / pen barrel button (button 2) must not arm a drag,
      // capture the pointer, or flip body styles.
      const down = pointerEvent(11, 0, vi.fn(), { button: 2, pointerType });
      act(() => result.current.handleProps.onPointerDown(down));
      expect(down.setPointerCapture).not.toHaveBeenCalled();
      expect(down.preventDefault).not.toHaveBeenCalled();
      expect(document.body.style.cursor).toBe("");
      expect(document.body.style.userSelect).toBe("");

      // No activePointerId was published: moves from that pointer are inert.
      act(() => result.current.handleProps.onPointerMove(pointerEvent(11, 300)));
      expect(result.current.width).toBe(176);
    },
  );

  it("stays idle when pointer capture fails", () => {
    const { result } = renderColumn(0);
    const failing = pointerEvent(
      9,
      0,
      vi.fn(() => {
        throw new DOMException("InvalidPointerId");
      }),
    );

    act(() => result.current.handleProps.onPointerDown(failing));
    expect(failing.preventDefault).not.toHaveBeenCalled();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    // No drag was armed by the failed capture...
    act(() => result.current.handleProps.onPointerMove(pointerEvent(9, 300)));
    expect(result.current.width).toBe(176);

    // ...and a later pointer can still start one.
    act(() => result.current.handleProps.onPointerDown(pointerEvent(10)));
    act(() => result.current.handleProps.onPointerMove(pointerEvent(10, 300)));
    expect(result.current.width).toBe(300);
    act(() => result.current.handleProps.onPointerUp(pointerEvent(10)));
  });

  it("aborts when the handle render gate closes mid-drag", () => {
    const rendered = renderHook(
      ({ enabled }) => useResizableColumn(undefined, undefined, undefined, enabled),
      {
        initialProps: { enabled: true },
      },
    );
    rendered.result.current.containerRef.current = {
      getBoundingClientRect: () => ({ left: 0 }),
    } as HTMLElement;

    act(() => rendered.result.current.handleProps.onPointerDown(pointerEvent(12)));
    act(() => rendered.result.current.handleProps.onPointerMove(pointerEvent(12, 260)));
    expect(document.body.style.cursor).toBe("col-resize");

    rendered.rerender({ enabled: false });
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    act(() => rendered.result.current.handleProps.onPointerMove(pointerEvent(12, 400)));
    expect(rendered.result.current.width).toBe(260);
  });
});

describe("useResizableColumn keyboard resizing", () => {
  it("resizes with arrow keys using the same clamps as dragging", () => {
    const { result } = renderColumn(0);

    const right = keyEvent("ArrowRight");
    act(() => result.current.handleProps.onKeyDown(right));
    expect(result.current.width).toBe(196);
    expect(right.preventDefault).toHaveBeenCalled();

    act(() => result.current.handleProps.onKeyDown(keyEvent("ArrowLeft")));
    expect(result.current.width).toBe(176);

    // Repeated ArrowLeft stops at minWidth (100).
    for (let i = 0; i < 10; i++) {
      act(() => result.current.handleProps.onKeyDown(keyEvent("ArrowLeft")));
    }
    expect(result.current.width).toBe(100);

    // Repeated ArrowRight stops at maxWidth (480).
    for (let i = 0; i < 30; i++) {
      act(() => result.current.handleProps.onKeyDown(keyEvent("ArrowRight")));
    }
    expect(result.current.width).toBe(480);

    // Unrelated keys neither resize nor swallow the event.
    const other = keyEvent("Enter");
    act(() => result.current.handleProps.onKeyDown(other));
    expect(result.current.width).toBe(480);
    expect(other.preventDefault).not.toHaveBeenCalled();
  });

  it("exposes a focusable separator with value semantics that track the width", () => {
    const { result } = renderColumn(0);
    const props = result.current.handleProps;

    expect(props.role).toBe("separator");
    expect(props.tabIndex).toBe(0);
    expect(props["aria-orientation"]).toBe("vertical");
    expect(props["aria-valuenow"]).toBe(176);
    expect(props["aria-valuemin"]).toBe(100);
    expect(props["aria-valuemax"]).toBe(480);

    act(() => result.current.handleProps.onKeyDown(keyEvent("ArrowRight")));
    expect(result.current.handleProps["aria-valuenow"]).toBe(196);
  });
});

describe("useResizableColumn touch affordances", () => {
  // The painted strip the consumer renders is 4px wide (`w-1`); the hit box
  // is that strip plus the invisible padding on each side.
  const PAINTED = 4;

  it("disables touch-action and keeps a >=24px hit target on fine pointers", () => {
    // jsdom's matchMedia never matches "(pointer: coarse)" → fine-pointer pad.
    const { result } = renderColumn(0);
    const style = result.current.handleProps.style;

    expect(style.touchAction).toBe("none");
    expect(style.paddingLeft + PAINTED + style.paddingRight).toBeGreaterThanOrEqual(24);

    // The negative margin anchors the painted strip's right edge to the
    // consumer-provided `left` boundary; background-clip keeps the pad
    // unpainted so the visual weight stays a 4px line.
    expect(style.marginLeft).toBe(-(style.paddingLeft + PAINTED));
    expect(style.backgroundClip).toBe("content-box");
    expect(style.boxSizing).toBe("content-box");
  });

  it("widens the hit target to >=44px on coarse pointers", () => {
    installMatchMedia({ "(pointer: coarse)": true });
    const { result } = renderColumn(0);
    const style = result.current.handleProps.style;

    expect(style.paddingLeft + PAINTED + style.paddingRight).toBeGreaterThanOrEqual(44);
    // Pad biased toward the terminal pane so the list rows' trailing status
    // badges keep more of their tappable area.
    expect(style.paddingRight).toBeGreaterThan(style.paddingLeft);
  });

  it("updates the hit target when the primary pointer capability changes at runtime", () => {
    const media = installMatchMedia({ "(pointer: coarse)": false });
    const { result } = renderColumn(0);

    expect(result.current.handleProps.style).toMatchObject({
      paddingLeft: 6,
      paddingRight: 14,
      marginLeft: -10,
    });

    act(() => media.set("(pointer: coarse)", true));

    expect(result.current.handleProps.style).toMatchObject({
      paddingLeft: 12,
      paddingRight: 28,
      marginLeft: -16,
    });
  });
});
