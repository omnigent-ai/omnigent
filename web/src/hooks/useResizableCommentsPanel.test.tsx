import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import {
  resetCommentsSizeStoreForTesting,
  useResizableCommentsPanel,
} from "./useResizableCommentsPanel";

const originalInnerWidth = window.innerWidth;

function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

beforeEach(() => {
  setInnerWidth(2000);
});

afterEach(() => {
  localStorage.clear();
  resetCommentsSizeStoreForTesting();
  setInnerWidth(originalInnerWidth);
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
    resetCommentsSizeStoreForTesting();
    const restored = renderHook(() => useResizableCommentsPanel());

    // The restored hook must use the saved comments width instead of the fixed
    // 240px default, matching a browser refresh while comments are open.
    expect(restored.result.current.width).toBe(260);
    restored.unmount();
  });

  it("persists the stacked height separately from the width", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());

    // Default stacked height is 256. ArrowUp grows it by 20px.
    act(() => {
      result.current.handleProps.onKeyDown({
        key: "ArrowUp",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    expect(result.current.height).toBe(276);
    expect(result.current.width).toBe(240);
    expect(readPanelSizePreference("commentsPanelHeightPx")).toBe(276);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBeNull();

    unmount();
    resetCommentsSizeStoreForTesting();
    const restored = renderHook(() => useResizableCommentsPanel());
    expect(restored.result.current.height).toBe(276);
    restored.unmount();
  });
});

describe("useResizableCommentsPanel drag overlay", () => {
  const overlaySelector = () =>
    [...document.body.children].find(
      (c): c is HTMLElement =>
        c instanceof HTMLElement && c.style.position === "fixed" && c.style.zIndex === "2147483647",
    ) ?? null;

  it("mounts a full-window overlay during a drag so mouseup isn't lost to the preview iframe", () => {
    // The divider sits between the HTML-preview iframe and this panel. Without
    // an overlay, dragging over the frame routes mousemove/mouseup into it and
    // the parent never sees the release, so the drag sticks to the cursor.
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    expect(overlaySelector()).toBeNull();

    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    const overlay = overlaySelector();
    expect(overlay).not.toBeNull();
    // With no measured row the panel stacks, so the drag is a height drag.
    expect(result.current.handleProps["aria-orientation"]).toBe("horizontal");
    expect(overlay?.style.cursor).toBe("row-resize");

    act(() => window.dispatchEvent(new MouseEvent("mouseup")));
    expect(overlaySelector()).toBeNull();
    unmount();
  });

  it("removes the overlay if unmounted mid-drag", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    expect(overlaySelector()).not.toBeNull();

    unmount();
    expect(overlaySelector()).toBeNull();
  });
});
