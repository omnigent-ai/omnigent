// Unit tests for useThrottledValue — the trailing-edge throttle that bounds how
// often the live assistant bubble re-parses its markdown. Exercised in isolation
// with fake timers so the timing invariants are deterministic. Each assertion is
// chosen so that removing the throttle (passing the value straight through) or
// removing the trailing flush turns the test red.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useThrottledValue } from "./useThrottledValue";

const INTERVAL = 100;

function renderThrottle(initial: string) {
  return renderHook(({ value }: { value: string }) => useThrottledValue(value, INTERVAL), {
    initialProps: { value: initial },
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useThrottledValue", () => {
  it("returns the initial value immediately on mount", () => {
    const { result } = renderThrottle("a");
    // No streaming yet — the first paint must not be delayed by the throttle.
    expect(result.current).toBe("a");
  });

  it("emits the first change immediately", () => {
    const { result, rerender } = renderThrottle("a");
    act(() => {
      rerender({ value: "ab" });
    });
    // First token after an idle gap shows at once (snappy first-token paint);
    // a failure here means the immediate-emit branch regressed to always-defer.
    expect(result.current).toBe("ab");
  });

  it("coalesces rapid changes within the interval into a single trailing update", () => {
    const { result, rerender } = renderThrottle("a");
    // First change emits immediately and opens the throttle window.
    act(() => {
      rerender({ value: "ab" });
    });
    expect(result.current).toBe("ab");

    // Three more changes, all within one INTERVAL window (60ms total).
    act(() => {
      rerender({ value: "abc" });
      vi.advanceTimersByTime(20);
    });
    act(() => {
      rerender({ value: "abcd" });
      vi.advanceTimersByTime(20);
    });
    act(() => {
      rerender({ value: "abcde" });
      vi.advanceTimersByTime(20);
    });
    // Still the immediate-emit value — intermediate frames were throttled. If
    // this reads "abcde", the throttle is not coalescing (re-parse per frame).
    expect(result.current).toBe("ab");

    // Cross the interval boundary → one trailing flush shows the LATEST value,
    // skipping the intermediate "abc"/"abcd" entirely (only the newest matters).
    act(() => {
      vi.advanceTimersByTime(INTERVAL);
    });
    expect(result.current).toBe("abcde");
  });

  it("emits immediately again once the interval has elapsed since the last emit", () => {
    const { result, rerender } = renderThrottle("a");
    act(() => {
      rerender({ value: "ab" });
    });
    expect(result.current).toBe("ab");

    // Idle past the interval, then a single change: no waiting, emits at once.
    act(() => {
      vi.advanceTimersByTime(INTERVAL);
    });
    act(() => {
      rerender({ value: "abc" });
    });
    // A failure here means the throttle keeps deferring even when the channel
    // was quiet — it should only rate-limit bursts, not steady-state updates.
    expect(result.current).toBe("abc");
  });

  it("always converges to the final value via the trailing edge", () => {
    const { result, rerender } = renderThrottle("a");
    act(() => {
      rerender({ value: "ab" });
    });
    act(() => {
      rerender({ value: "final" });
    });
    // "final" arrived inside the window, so it is deferred for now.
    expect(result.current).toBe("ab");
    act(() => {
      vi.advanceTimersByTime(INTERVAL);
    });
    // The trailing flush must land the final value; without it the bubble would
    // be stuck showing truncated mid-stream text after the agent finishes.
    expect(result.current).toBe("final");
  });

  it("clears the pending flush timer on unmount", () => {
    const { rerender, unmount } = renderThrottle("a");
    act(() => {
      rerender({ value: "ab" });
    }); // immediate emit — no pending timer
    // The next change schedules the trailing-flush timer; capture its id. This
    // rerender is the only setTimeout, so the last recorded result is that id.
    const setSpy = vi.spyOn(window, "setTimeout");
    act(() => {
      rerender({ value: "abc" });
    });
    const pendingTimerId = setSpy.mock.results.at(-1)?.value;
    setSpy.mockRestore();
    expect(pendingTimerId).toBeDefined();

    const clearSpy = vi.spyOn(window, "clearTimeout");
    unmount();
    // The unmount cleanup must clear that exact pending timer. Without the
    // cleanup effect the timer survives teardown and fires setShown on an
    // unmounted component (React 18 silently drops it, so a value check can't
    // catch the leak — only asserting the clear does).
    expect(clearSpy).toHaveBeenCalledWith(pendingTimerId);
    clearSpy.mockRestore();
  });
});
