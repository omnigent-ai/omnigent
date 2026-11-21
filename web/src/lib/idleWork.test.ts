import { afterEach, describe, expect, it, vi } from "vitest";

import { runInIdleSlices } from "./idleWork";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("runInIdleSlices", () => {
  it("runs a slice per idle callback until the work reports done", () => {
    const callbacks: IdleRequestCallback[] = [];
    vi.stubGlobal("requestIdleCallback", (callback: IdleRequestCallback) => {
      callbacks.push(callback);
      return callbacks.length;
    });
    vi.stubGlobal("cancelIdleCallback", vi.fn());
    const remaining: number[] = [];
    let slices = 0;
    runInIdleSlices((timeLeft) => {
      remaining.push(timeLeft());
      slices++;
      return slices === 3;
    });
    const deadline = { didTimeout: false, timeRemaining: () => 12 };
    while (callbacks.length > 0) callbacks.shift()!(deadline);
    expect(slices).toBe(3);
    expect(remaining).toEqual([12, 12, 12]);
  });

  it("stops scheduling once cancelled", () => {
    const callbacks: IdleRequestCallback[] = [];
    const cancelIdleCallback = vi.fn();
    vi.stubGlobal("requestIdleCallback", (callback: IdleRequestCallback) => {
      callbacks.push(callback);
      return 7;
    });
    vi.stubGlobal("cancelIdleCallback", cancelIdleCallback);
    const step = vi.fn(() => false);
    const cancel = runInIdleSlices(step);
    cancel();
    expect(cancelIdleCallback).toHaveBeenCalledWith(7);
    callbacks.shift()?.({ didTimeout: false, timeRemaining: () => 12 });
    expect(step).not.toHaveBeenCalled();
  });

  it("falls back to short timed slices where idle callbacks are missing", () => {
    vi.useFakeTimers();
    vi.stubGlobal("requestIdleCallback", undefined);
    let slices = 0;
    let budget = 0;
    runInIdleSlices((timeLeft) => {
      budget = Math.max(budget, timeLeft());
      slices++;
      return slices === 2;
    });
    vi.runAllTimers();
    expect(slices).toBe(2);
    expect(budget).toBeGreaterThan(0);
    expect(budget).toBeLessThanOrEqual(5);
  });
});
