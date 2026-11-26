import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useMenuCloseHandOff } from "./useMenuCloseHandOff";

afterEach(() => {
  vi.useRealTimers();
});

describe("useMenuCloseHandOff", () => {
  it("runs a held action in place of the menu's focus restoration", () => {
    const { result } = renderHook(() => useMenuCloseHandOff());
    const run = vi.fn();
    result.current.runAfterClose(run);
    const event = new Event("focusout", { cancelable: true });

    result.current.onCloseAutoFocus(event);

    expect(event.defaultPrevented).toBe(true);
    expect(run).toHaveBeenCalledOnce();
  });

  it("lets focus return to the trigger first, then runs the held action", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useMenuCloseHandOff());
    const run = vi.fn();
    result.current.runAfterClose(run, { afterFocusReturns: true });
    const event = new Event("focusout", { cancelable: true });

    result.current.onCloseAutoFocus(event);

    expect(event.defaultPrevented).toBe(false);
    expect(run).not.toHaveBeenCalled();
    vi.runAllTimers();
    expect(run).toHaveBeenCalledOnce();
  });

  it("leaves a menu closed without a held action alone, and runs an action once", () => {
    const { result } = renderHook(() => useMenuCloseHandOff());
    const untouched = new Event("focusout", { cancelable: true });
    result.current.onCloseAutoFocus(untouched);
    expect(untouched.defaultPrevented).toBe(false);

    const run = vi.fn();
    result.current.runAfterClose(run);
    result.current.onCloseAutoFocus(new Event("focusout", { cancelable: true }));
    result.current.onCloseAutoFocus(new Event("focusout", { cancelable: true }));
    expect(run).toHaveBeenCalledOnce();
  });
});
