// The app's own reads wait while a reader's budget is spent, GitHub has it cooling down, or
// the window has been away; the hook hears each change.

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { COOLDOWN_WAKE_SPREAD_MS } from "@/lib/pullRequestPolling";

import {
  automaticReadsAllowed,
  automaticReadsResumeAt,
  useAutomaticReadsAllowed,
  type AutomaticReadPacers,
} from "./githubAutomaticReads";

function pacers() {
  const listeners = new Set<() => void>();
  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  };
  const state = {
    pace: "normal" as "normal" | "slow" | "stopped",
    held: false,
    paused: false,
    // A cooldown whose deadline, 1000, has passed and that no answer has ended yet.
    recovering: false,
    now: 0,
  };
  const fake = {
    budgets: {
      pace: () =>
        state.pace === "stopped"
          ? { pace: "stopped", until: 1 }
          : state.pace === "slow"
            ? { pace: "slow", factor: 4 }
            : { pace: "normal" },
      subscribe,
    },
    cooldowns: {
      heldUntil: () => (state.held ? 1 : null),
      phase: () => (state.recovering ? { phase: "recovering", until: 1000 } : null),
      subscribe,
    },
    focus: { paused: () => state.paused, subscribe },
    now: () => state.now,
  } as unknown as AutomaticReadPacers;
  const change = (next: Partial<typeof state>) => {
    Object.assign(state, next);
    for (const listener of [...listeners]) listener();
  };
  return { fake, change, listeners, state };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("the app's own GitHub reads", () => {
  it("go ahead at a normal or slow pace, and wait while stopped, cooling down or away", () => {
    const { fake, change } = pacers();
    expect(automaticReadsAllowed("github.com/octo", fake)).toBe(true);
    change({ pace: "slow" });
    expect(automaticReadsAllowed("github.com/octo", fake)).toBe(true);
    change({ pace: "stopped" });
    expect(automaticReadsAllowed("github.com/octo", fake)).toBe(false);
    change({ pace: "normal", held: true });
    expect(automaticReadsAllowed("github.com/octo", fake)).toBe(false);
    // A reader with no login of its own has no cooldown to wait on.
    expect(automaticReadsAllowed(null, fake)).toBe(true);
    change({ held: false, paused: true });
    expect(automaticReadsAllowed("github.com/octo", fake)).toBe(false);
  });

  it("re-renders as each hold starts and ends, and stops listening once unmounted", () => {
    const { fake, change, listeners } = pacers();
    const view = renderHook(() => useAutomaticReadsAllowed("github.com/octo", fake));
    expect(view.result.current).toBe(true);
    act(() => change({ pace: "stopped" }));
    expect(view.result.current).toBe(false);
    act(() => change({ pace: "normal", paused: true }));
    expect(view.result.current).toBe(false);
    act(() => change({ paused: false }));
    expect(view.result.current).toBe(true);
    view.unmount();
    expect(listeners.size).toBe(0);
  });

  it("after a cooldown, wait for the list's own read to end it, or for the polls' wake spread", () => {
    const { fake, change, state } = pacers();
    change({ recovering: true, now: 1000 });
    // Rows read before the list would end the cooldown and leave the list's limit unread.
    expect(automaticReadsAllowed("github.com/octo", fake)).toBe(false);
    expect(automaticReadsResumeAt("github.com/octo", fake)).toBe(1000 + COOLDOWN_WAKE_SPREAD_MS);
    // The list's answer ends the cooldown, and the rows go at once.
    change({ recovering: false });
    expect(automaticReadsAllowed("github.com/octo", fake)).toBe(true);
    expect(automaticReadsResumeAt("github.com/octo", fake)).toBeNull();
    // With no list to read first, the wake spread's end lets them go.
    change({ recovering: true });
    state.now = 1000 + COOLDOWN_WAKE_SPREAD_MS;
    expect(automaticReadsAllowed("github.com/octo", fake)).toBe(true);
  });

  it("re-renders when a hold that nothing announces ends", () => {
    vi.useFakeTimers();
    const { fake, change, state } = pacers();
    change({ recovering: true, now: 1000 });
    const view = renderHook(() => useAutomaticReadsAllowed("github.com/octo", fake));
    expect(view.result.current).toBe(false);
    state.now = 1000 + COOLDOWN_WAKE_SPREAD_MS;
    act(() => {
      vi.advanceTimersByTime(COOLDOWN_WAKE_SPREAD_MS);
    });
    expect(view.result.current).toBe(true);
  });
});
