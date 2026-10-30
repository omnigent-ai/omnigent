import { cleanup, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useScheduleEscape } from "./useScheduleEscape";

afterEach(cleanup);

it("closes the active picker without sending Escape to the parent dialog", () => {
  const dismiss = vi.fn(() => true);
  const parent = vi.fn();
  document.addEventListener("keydown", parent, true);
  const { rerender } = renderHook(({ open }) => useScheduleEscape(open, dismiss), {
    initialProps: { open: true },
  });
  try {
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(dismiss).toHaveBeenCalledOnce();
    expect(parent).not.toHaveBeenCalled();
    rerender({ open: false });
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(parent).toHaveBeenCalledOnce();
    expect(dismiss).toHaveBeenCalledOnce();
  } finally {
    document.removeEventListener("keydown", parent, true);
  }
});

it("defers to a nested picker and ignores other keys", () => {
  const outer = vi.fn(() => false);
  const inner = vi.fn(() => true);
  renderHook(() => {
    useScheduleEscape(true, outer);
    useScheduleEscape(true, inner);
  });
  fireEvent.keyDown(document.body, { key: "Enter" });
  expect(outer).not.toHaveBeenCalled();
  expect(inner).not.toHaveBeenCalled();
  fireEvent.keyDown(document.body, { key: "Escape" });
  expect(outer).toHaveBeenCalledOnce();
  expect(inner).toHaveBeenCalledOnce();
});
