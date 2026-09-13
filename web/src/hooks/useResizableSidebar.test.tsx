import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
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

// Simulate a drag: press the handle, move the cursor to clientX, release.
// For a left panel the live width tracks the cursor's distance from the
// viewport's left edge (clientX).
function dragTo(
  result: { current: ReturnType<typeof useResizableSidebar> },
  clientX: number,
): void {
  act(() =>
    result.current.handleProps.onMouseDown({
      preventDefault: () => {},
    } as React.MouseEvent),
  );
  act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX })));
  act(() => window.dispatchEvent(new MouseEvent("mouseup")));
}

beforeEach(() => {
  setInnerWidth(2000);
});

afterEach(() => {
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
});
