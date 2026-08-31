import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
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
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 0 })));
    act(() =>
      collapsed.result.current.handleProps.onMouseDown({
        preventDefault: () => {},
      } as React.MouseEvent),
    );
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 100 })));
    act(() => window.dispatchEvent(new MouseEvent("mouseup")));
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
    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 0 })));
    act(() => window.dispatchEvent(new MouseEvent("mouseup")));

    // Now shrink the viewport hard. Even though the stored (no-reserve) width may
    // still fit its own ceiling, the render-time reserve clamp must re-run.
    setInnerWidth(1000);
    act(() => window.dispatchEvent(new Event("resize")));
    rerender({ reserved: reservedPx });
    // chat = viewport - sidebar - gap - panel.
    expect(1000 - reservedPx - 8 - result.current.panelWidth).toBeGreaterThanOrEqual(480);
  });
});

describe("useResizableInlinePanel drag overlay", () => {
  const overlaySelector = () =>
    [...document.body.children].find(
      (c): c is HTMLElement =>
        c instanceof HTMLElement && c.style.position === "fixed" && c.style.zIndex === "2147483647",
    ) ?? null;

  it("ignores drag input while persistence is disabled for a tentative key", () => {
    const sessionId = "conv_tentative";
    const { result, unmount } = renderHook(() =>
      useResizableInlinePanel(sessionId, undefined, 0, false),
    );
    const initialWidth = result.current.panelWidth;
    expect(result.current.handleProps["aria-disabled"]).toBe(true);
    expect(result.current.handleProps.tabIndex).toBe(-1);

    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    expect(overlaySelector()).toBeNull();

    act(() => {
      window.dispatchEvent(new MouseEvent("mousemove", { clientX: 100 }));
      window.dispatchEvent(new MouseEvent("mouseup"));
    });
    expect(result.current.panelWidth).toBe(initialWidth);
    expect(readSessionWorkspaceState(sessionId).widthPx).toBeUndefined();
    unmount();
  });

  it("freezes and does not persist a drag when its key becomes tentative", async () => {
    const sessionId = "conv_mid_drag";
    const { result, rerender, unmount } = renderHook(
      ({ persistEnabled }) => useResizableInlinePanel(sessionId, undefined, 0, persistEnabled),
      { initialProps: { persistEnabled: true } },
    );

    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 1200 })));
    await waitFor(() => expect(result.current.panelWidth).toBe(800));
    expect(readSessionWorkspaceState(sessionId).widthPx).toBeUndefined();

    rerender({ persistEnabled: false });
    act(() => {
      window.dispatchEvent(new MouseEvent("mousemove", { clientX: 1000 }));
      window.dispatchEvent(new MouseEvent("mouseup"));
    });
    expect(result.current.panelWidth).toBe(800);
    expect(readSessionWorkspaceState(sessionId).widthPx).toBeUndefined();
    unmount();
  });

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
