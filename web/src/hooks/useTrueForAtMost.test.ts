import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTrueForAtMost } from "./useTrueForAtMost";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("useTrueForAtMost", () => {
  it("follows the flag until it has been true for the limit", () => {
    const { result, rerender } = renderHook(({ active }) => useTrueForAtMost(active, 3000), {
      initialProps: { active: false },
    });
    expect(result.current).toBe(false);

    rerender({ active: true });
    expect(result.current).toBe(true);
    act(() => vi.advanceTimersByTime(2999));
    expect(result.current).toBe(true);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe(false);

    // Turning false and true again restarts the window.
    rerender({ active: false });
    rerender({ active: true });
    expect(result.current).toBe(true);
  });

  it("restarts the window when the reset key changes", () => {
    const { result, rerender } = renderHook(({ key }) => useTrueForAtMost(true, 3000, key), {
      initialProps: { key: "a" },
    });
    act(() => vi.advanceTimersByTime(3000));
    expect(result.current).toBe(false);

    rerender({ key: "b" });
    expect(result.current).toBe(true);
    act(() => vi.advanceTimersByTime(1000));

    // Coming back to a key whose window expired earlier starts a fresh one.
    rerender({ key: "a" });
    expect(result.current).toBe(true);
    act(() => vi.advanceTimersByTime(2999));
    expect(result.current).toBe(true);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe(false);
  });
});
