import { cleanup, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useScheduleEscape } from "./useScheduleEscape";

afterEach(cleanup);

it("uses the latest callback without changing listener order on rerender", () => {
  const order: string[] = [];
  const first = () => {
    order.push("old");
    return false;
  };
  const second = () => {
    order.push("new");
    return false;
  };
  const { rerender } = renderHook(({ dismiss }) => useScheduleEscape(true, dismiss), {
    initialProps: { dismiss: first },
  });
  const later = () => order.push("later");
  window.addEventListener("keydown", later, true);
  try {
    rerender({ dismiss: second });
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(order).toEqual(["new", "later"]);
  } finally {
    window.removeEventListener("keydown", later, true);
  }
});

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

it("allows later handlers when dismissal declines and ignores other keys", () => {
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

it("ignores Escape already handled by an earlier listener", () => {
  const prevent = (event: KeyboardEvent) => event.preventDefault();
  window.addEventListener("keydown", prevent, true);
  const dismiss = vi.fn(() => true);
  try {
    renderHook(() => useScheduleEscape(true, dismiss));
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(dismiss).not.toHaveBeenCalled();
  } finally {
    window.removeEventListener("keydown", prevent, true);
  }
});

it.each([{ isComposing: true }, { keyCode: 229 }])(
  "ignores composition Escape %j",
  (composition) => {
    const dismiss = vi.fn(() => true);
    renderHook(() => useScheduleEscape(true, dismiss));
    const event = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
      ...composition,
    });
    document.body.dispatchEvent(event);
    expect(dismiss).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  },
);
