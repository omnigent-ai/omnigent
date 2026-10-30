import { renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { usePreventDragClick } from "./usePreventDragClick";

it("prevents the native link click after dropping a dragged row, then allows normal clicks", () => {
  vi.useFakeTimers();
  const view = renderHook(({ dragging }) => usePreventDragClick(dragging), {
    initialProps: { dragging: true },
  });
  view.rerender({ dragging: false });
  const dropClick = new MouseEvent("click", { bubbles: true, cancelable: true });
  window.dispatchEvent(dropClick);
  expect(dropClick.defaultPrevented).toBe(true);
  vi.runAllTimers();
  const nextClick = new MouseEvent("click", { bubbles: true, cancelable: true });
  window.dispatchEvent(nextClick);
  expect(nextClick.defaultPrevented).toBe(false);
  vi.useRealTimers();
});
