import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { resetWidthStoreForTesting, useResizableInlinePanel } from "./useResizableInlinePanel";

// useResizableInlinePanel keeps its width in a module-level store shared across
// all callers, re-seeded per conversation. resetWidthStoreForTesting clears it
// between tests so cases are fully independent. A 2000px viewport leaves the
// 400px default unclamped.

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

    // Default 400 + one ArrowLeft step (20px) = 420, persisted under the
    // session key.
    const afterNudge = nudgeWiderOnce(result);
    expect(afterNudge).toBe(420);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(420);

    unmount();
    resetWidthStoreForTesting();
    const restored = renderHook(() => useResizableInlinePanel(SESSION));

    // The saved manual width wins over the 400px default.
    expect(restored.result.current.panelWidth).toBe(420);
    restored.unmount();
  });

  it("scopes the saved width to its session: a different session uses the default", () => {
    const first = renderHook(() => useResizableInlinePanel(SESSION));
    expect(nudgeWiderOnce(first.result)).toBe(420);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(420);
    first.unmount();

    // A second conversation has no saved width, so it falls back to the
    // 400px default rather than inheriting the first's 420px width.
    const second = renderHook(() => useResizableInlinePanel("conv_other"));
    expect(second.result.current.panelWidth).toBe(400);
    expect(readSessionWorkspaceState("conv_other").widthPx).toBeUndefined();
    second.unmount();
  });

  it("re-derives from the preference on resize: clamps down on shrink, springs back on widen", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));

    // Establish a persisted preference of 420 (default 400 + one ArrowLeft step).
    expect(nudgeWiderOnce(result)).toBe(420);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(420);

    // Shrinking the viewport clamps the live width to the 0.6 ceiling
    // (600 * 0.6 = 360) without disturbing the saved 420 preference.
    setInnerWidth(600);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.panelWidth).toBe(360);
    expect(readSessionWorkspaceState(SESSION).widthPx).toBe(420);

    // Widening again re-derives from the preference, restoring 420 in-session.
    setInnerWidth(2000);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.panelWidth).toBe(420);
  });
});

describe("useResizableInlinePanel drag overlay", () => {
  const overlaySelector = () =>
    [...document.body.children].find(
      (c): c is HTMLElement =>
        c instanceof HTMLElement && c.style.position === "fixed" && c.style.zIndex === "2147483647",
    ) ?? null;

  it("mounts a full-window overlay during a drag so mouseup isn't lost to an iframe", () => {
    // The panel sits beside the sandboxed HTML-preview iframe. Without an
    // overlay, dragging over the frame routes mousemove/mouseup into it and the
    // parent never sees the release, so the drag sticks to the cursor.
    const { result, unmount } = renderHook(() => useResizableInlinePanel(SESSION));
    expect(overlaySelector()).toBeNull();

    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    const overlay = overlaySelector();
    expect(overlay).not.toBeNull();
    expect(overlay?.style.cursor).toBe("col-resize");

    act(() => window.dispatchEvent(new MouseEvent("mouseup")));
    expect(overlaySelector()).toBeNull();
    unmount();
  });

  it("removes the overlay if unmounted mid-drag", () => {
    const { result, unmount } = renderHook(() => useResizableInlinePanel(SESSION));
    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    expect(overlaySelector()).not.toBeNull();

    // Panel closes (e.g. tab switch) while still dragging — cleanup must not
    // leave the transparent overlay swallowing every click on the page.
    unmount();
    expect(overlaySelector()).toBeNull();
  });
});
