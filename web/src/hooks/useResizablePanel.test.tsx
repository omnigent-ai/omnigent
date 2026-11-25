import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import { resetSharedWidthStoreForTesting, useResizablePanel } from "./useResizablePanel";

const originalInnerWidth = window.innerWidth;

function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

beforeEach(() => {
  setInnerWidth(2000);
});

afterEach(() => {
  localStorage.clear();
  resetSharedWidthStoreForTesting();
  setInnerWidth(originalInnerWidth);
  vi.unstubAllGlobals();
});

describe("useResizablePanel layout", () => {
  it.each([
    ["a browser", false, false],
    ["the desktop app", true, true],
  ])("sizes the panel inline below md only in %s", (_name, desktopApp, inline) => {
    setInnerWidth(900);
    if (desktopApp) vi.stubGlobal("omnigentDesktop", { kind: "electron" });

    const { result } = renderHook(() => useResizablePanel(true));

    expect(result.current.isDesktop).toBe(inline);
    // 50vw of 900px; the mobile layout leaves the width to its drawer classes.
    expect(result.current.panelWidth).toBe(inline ? 450 : undefined);
  });
});

describe("useResizablePanel persistence", () => {
  it("persists explicit keyboard resize and restores it after store reset", () => {
    const { result, unmount } = renderHook(() => useResizablePanel(true));

    // Default at 2000px viewport is 50vw = 1000. ArrowRight narrows by 20px.
    act(() => {
      result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    expect(result.current.panelWidth).toBe(980);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(980);

    unmount();
    resetSharedWidthStoreForTesting();
    const restored = renderHook(() => useResizablePanel(true));

    // A fresh module-level store hydrates from localStorage instead of falling
    // back to 50vw, which is the refresh behavior this hook must preserve.
    expect(restored.result.current.panelWidth).toBe(980);
    restored.unmount();
  });

  it("clamps live on shrink without persisting, then restores the preference on widen", () => {
    const { result } = renderHook(() => useResizablePanel(true));

    act(() => {
      result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(980);

    setInnerWidth(1000);
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });

    // The live width clamps to the new 80vw ceiling, but the saved user
    // preference remains 980 so a later wider viewport can restore it.
    expect(result.current.panelWidth).toBe(800);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(980);

    // Widening the viewport again re-derives from the persisted preference,
    // so the panel springs back to 980 within the same session (no reload).
    setInnerWidth(2000);
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current.panelWidth).toBe(980);
  });

  it("updates live width during a drag but only persists on release", () => {
    const { result } = renderHook(() => useResizablePanel(true));

    act(() => {
      result.current.handleProps.onMouseDown({
        preventDefault: () => {},
      } as React.MouseEvent);
    });
    act(() => {
      // 2000px viewport, cursor at 1200 → width = innerWidth - clientX = 800.
      window.dispatchEvent(new MouseEvent("mousemove", { clientX: 1200 }));
    });

    // Live width tracks the drag, but nothing is written to storage mid-drag —
    // persisting per mousemove would fire a synchronous setItem on every frame.
    expect(result.current.panelWidth).toBe(800);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBeNull();

    act(() => {
      window.dispatchEvent(new MouseEvent("mouseup"));
    });

    // Release snapshots the final width exactly once.
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(800);
  });

  it("notifies multiple mounted subscribers from the shared width store", () => {
    const first = renderHook(() => useResizablePanel(true));
    const second = renderHook(() => useResizablePanel(true));

    act(() => {
      first.result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    // Both hook instances read the same module-level store. If subscription
    // fan-out breaks, only the initiating hook would observe the new width.
    expect(first.result.current.panelWidth).toBe(980);
    expect(second.result.current.panelWidth).toBe(980);

    first.unmount();
    second.unmount();
  });

  it("keeps a panel with its own storage key apart from the push panels", () => {
    const push = renderHook(() => useResizablePanel(true));
    const project = renderHook(() =>
      useResizablePanel(true, 40, 320, "projectSessionPanelWidthPx"),
    );

    act(() => {
      project.result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    expect(project.result.current.panelWidth).toBe(780);
    expect(readPanelSizePreference("projectSessionPanelWidthPx")).toBe(780);
    expect(push.result.current.panelWidth).toBe(1000);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBeNull();

    push.unmount();
    project.unmount();
  });
});
