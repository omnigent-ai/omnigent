import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import {
  resetCommentsWidthStoreForTesting,
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
  resetCommentsWidthStoreForTesting();
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
    resetCommentsWidthStoreForTesting();
    const restored = renderHook(() => useResizableCommentsPanel());

    // The restored hook must use the saved comments width instead of the fixed
    // 240px default, matching a browser refresh while comments are open.
    expect(restored.result.current.width).toBe(260);
    restored.unmount();
  });
});
