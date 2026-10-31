import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";
import {
  announceTurnEnded,
  markTurnEnded,
  RunnerSampleTimeContext,
  useRunnerAsleepConfirmed,
  useTurnEndedAt,
} from "./runnerHealthFreshness";

function withSample(samples: Map<string, number>) {
  return ({ children }: { children: ReactNode }) =>
    createElement(RunnerSampleTimeContext.Provider, { value: samples }, children);
}

describe("useRunnerAsleepConfirmed", () => {
  it("is true when no turn has ended for the session", () => {
    const { result } = renderHook(() => useRunnerAsleepConfirmed("fresh_never_ended"));
    expect(result.current).toBe(true);
  });

  it("is always true without a session id", () => {
    markTurnEnded("whatever", Date.now());
    const { result } = renderHook(() => useRunnerAsleepConfirmed(undefined));
    expect(result.current).toBe(true);
  });

  it("is false while the latest sample predates the turn end, true once a later one arrives", () => {
    const endedAt = 10_000;
    markTurnEnded("sess_gate", endedAt);
    let samples = new Map([["sess_gate", endedAt - 1]]);
    const { result, rerender } = renderHook(() => useRunnerAsleepConfirmed("sess_gate"), {
      wrapper: ({ children }: { children: ReactNode }) => withSample(samples)({ children }),
    });
    expect(result.current).toBe(false);

    samples = new Map([["sess_gate", endedAt + 1]]);
    rerender();
    expect(result.current).toBe(true);
  });

  it("is false when the session has no poll sample at all after a turn end", () => {
    markTurnEnded("sess_nosample", 5);
    const { result } = renderHook(() => useRunnerAsleepConfirmed("sess_nosample"));
    expect(result.current).toBe(false);
  });

  it("re-evaluates when a turn end is announced for a mounted reader", () => {
    const samples = new Map([["sess_live", 100]]);
    const { result } = renderHook(() => useRunnerAsleepConfirmed("sess_live"), {
      wrapper: withSample(samples),
    });
    expect(result.current).toBe(true);
    act(() => {
      markTurnEnded("sess_live", 200);
      announceTurnEnded();
    });
    expect(result.current).toBe(false);
  });
});

describe("useRunnerAsleepConfirmed with a caller-local turn end", () => {
  it("is false when the local turn end is newer than the sample, true once a later sample lands", () => {
    let samples = new Map([["sess_local", 100]]);
    const { result, rerender } = renderHook(
      () => useRunnerAsleepConfirmed("sess_local", { turnEndedAt: 200 }),
      { wrapper: ({ children }: { children: ReactNode }) => withSample(samples)({ children }) },
    );
    expect(result.current).toBe(false);
    samples = new Map([["sess_local", 300]]);
    rerender();
    expect(result.current).toBe(true);
  });
});

describe("useTurnEndedAt", () => {
  it("reports the turn end in the render where active flips to false, and resets per session", () => {
    const { result, rerender } = renderHook(
      ({ id, active }: { id: string; active: boolean }) => useTurnEndedAt(id, active),
      { initialProps: { id: "a", active: true } },
    );
    expect(result.current).toBeUndefined();
    rerender({ id: "a", active: false });
    expect(result.current).toBeGreaterThan(0);
    rerender({ id: "b", active: false });
    expect(result.current).toBeUndefined();
  });
});
