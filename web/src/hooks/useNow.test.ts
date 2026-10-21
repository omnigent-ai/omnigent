// Unit tests for useNow — the shared, slowly-ticking wall clock that keeps
// relative time labels ("Next run in 3 hours") fresh. Exercised with fake timers
// so the ticking is deterministic. Each assertion is chosen so that removing the
// interval (a frozen clock) or the unmount cleanup (a leaked timer) turns it red.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TICK_MS, useNow, useNowSelector } from "./useNow";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useNow", () => {
  it("advances `now` after each tick interval", () => {
    const { result } = renderHook(() => useNow());
    const first = result.current.getTime();

    act(() => {
      vi.advanceTimersByTime(TICK_MS);
    });
    const second = result.current.getTime();
    // Without the interval firing setNow, `now` would be frozen at `first` and
    // the relative label would never count down.
    expect(second).toBeGreaterThanOrEqual(first + TICK_MS);

    act(() => {
      vi.advanceTimersByTime(TICK_MS);
    });
    expect(result.current.getTime()).toBeGreaterThanOrEqual(second + TICK_MS);
  });

  it("does not advance before a full interval has elapsed", () => {
    const { result } = renderHook(() => useNow());
    const first = result.current;

    act(() => {
      vi.advanceTimersByTime(TICK_MS - 1);
    });
    // Same Date reference between ticks — the snapshot is stable so React does
    // not re-render on every timer granularity, only once per TICK_MS.
    expect(result.current).toBe(first);
  });

  it("shares ONE timer across multiple subscribers", () => {
    const setSpy = vi.spyOn(globalThis, "setInterval");
    const a = renderHook(() => useNow());
    const b = renderHook(() => useNow());
    const c = renderHook(() => useNow());
    // A single module-level interval drives every subscriber — not one per row.
    expect(setSpy).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(TICK_MS);
    });
    // All subscribers observe the same advanced instant.
    expect(a.result.current.getTime()).toBe(b.result.current.getTime());
    expect(b.result.current.getTime()).toBe(c.result.current.getTime());

    a.unmount();
    b.unmount();
    c.unmount();
    setSpy.mockRestore();
  });

  it("clears the shared timer once the last subscriber unmounts", () => {
    const clearSpy = vi.spyOn(globalThis, "clearInterval");
    const a = renderHook(() => useNow());
    const b = renderHook(() => useNow());

    // First unmount still leaves a live subscriber → timer must stay running.
    a.unmount();
    expect(clearSpy).not.toHaveBeenCalled();

    // Last subscriber leaves → the interval is torn down (no leak, no
    // state-update-after-unmount).
    b.unmount();
    expect(clearSpy).toHaveBeenCalledTimes(1);
    clearSpy.mockRestore();
  });
});

describe("useNowSelector", () => {
  it("does not subscribe or re-render while disabled", () => {
    const interval = vi.spyOn(globalThis, "setInterval");
    let renders = 0;
    const view = renderHook(() => {
      renders += 1;
      return useNowSelector((now) => now.getHours() >= 17, { enabled: false });
    });

    act(() => vi.advanceTimersByTime(TICK_MS * 2));
    expect(renders).toBe(1);
    expect(interval).not.toHaveBeenCalled();
    view.unmount();
    interval.mockRestore();
  });

  it("does not re-render on a tick with the same selected value", () => {
    vi.setSystemTime(new Date(2026, 9, 5, 17, 0));
    let renders = 0;
    const view = renderHook(() => {
      renders += 1;
      return useNowSelector((now) => now.getHours() >= 17, { enabled: true });
    });
    expect(view.result.current).toBe(true);
    const rendersAfterMount = renders;

    act(() => vi.advanceTimersByTime(TICK_MS * 2));
    expect(renders).toBe(rendersAfterMount);
    view.unmount();
  });

  it("re-renders when the selected warning window opens", () => {
    vi.setSystemTime(new Date(2026, 9, 5, 16, 59, 30));
    let renders = 0;
    const view = renderHook(() => {
      renders += 1;
      return useNowSelector((now) => now.getHours() >= 17, { enabled: true });
    });
    expect(view.result.current).toBe(false);
    const rendersAfterMount = renders;

    act(() => vi.advanceTimersByTime(TICK_MS));
    expect(view.result.current).toBe(true);
    expect(renders).toBe(rendersAfterMount + 1);
    view.unmount();
  });
});
