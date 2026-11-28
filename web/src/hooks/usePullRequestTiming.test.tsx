import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePullRequestTiming, type PullRequestTimingInput } from "@/hooks/usePullRequestTiming";

const reportPullRequestTiming = vi.hoisted(() => vi.fn());
vi.mock("@/lib/nativeBridge", () => ({ reportPullRequestTiming }));

const loading: PullRequestTimingInput = {
  sessionId: "conv_1",
  active: true,
  path: "host",
  stateKind: "loading",
  cached: false,
};

function renderTiming(initialProps: PullRequestTimingInput) {
  return renderHook((input: PullRequestTimingInput) => usePullRequestTiming(input), {
    initialProps,
  });
}

describe("usePullRequestTiming", () => {
  let now = 0;
  beforeEach(() => {
    now = 1000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    reportPullRequestTiming.mockClear();
  });
  afterEach(() => vi.restoreAllMocks());

  it("reports a cold open once, when the PR view's diff settles", () => {
    const { result, rerender } = renderTiming(loading);
    now = 1800;
    rerender({ ...loading, stateKind: "ready" });
    act(() => result.current("header"));
    expect(reportPullRequestTiming).not.toHaveBeenCalled();
    now = 2500;
    act(() => result.current("diff"));
    now = 3000;
    act(() => result.current("diff"));
    rerender({ ...loading, stateKind: "ready", cached: true });
    expect(reportPullRequestTiming).toHaveBeenCalledExactlyOnceWith({
      sessionId: "conv_1",
      path: "host",
      outcome: "ready",
      cached: false,
      stateMs: 800,
      headerMs: 800,
      diffMs: 1500,
    });
  });

  it("keeps one clock when the mark function is called before the state settles", () => {
    const { result, rerender } = renderTiming(loading);
    now = 1400;
    act(() => result.current("header"));
    rerender({ ...loading, stateKind: "ready" });
    act(() => result.current("diff"));
    expect(reportPullRequestTiming).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ stateMs: 400, headerMs: 400, diffMs: 400 }),
    );
  });

  it("reports what a session reached when the panel switches to another, then times the new one", () => {
    const { result, rerender } = renderTiming(loading);
    now = 1200;
    rerender({ ...loading, stateKind: "ready" });
    act(() => result.current("header"));
    now = 1300;
    rerender({ ...loading, sessionId: "conv_2", stateKind: "no-pr", cached: true });
    expect(reportPullRequestTiming.mock.calls.map(([report]) => report)).toEqual([
      expect.objectContaining({
        sessionId: "conv_1",
        outcome: "ready",
        stateMs: 200,
        headerMs: 200,
      }),
      expect.objectContaining({ sessionId: "conv_2", outcome: "no-pr", cached: true, stateMs: 0 }),
    ]);
  });

  it("doesn't time a hidden panel, and reports nothing if it never left loading", () => {
    const { result, rerender, unmount } = renderTiming({ ...loading, active: false });
    rerender({ ...loading, active: false, stateKind: "ready" });
    act(() => result.current("diff"));
    rerender(loading);
    unmount();
    expect(reportPullRequestTiming).not.toHaveBeenCalled();
  });

  it("hands out a new mark for each opening, and keeps it while the opening lasts", () => {
    const { result, rerender } = renderTiming(loading);
    const first = result.current;
    rerender({ ...loading, stateKind: "ready" });
    expect(result.current).toBe(first);
    rerender({ ...loading, stateKind: "ready", active: false });
    rerender({ ...loading, stateKind: "ready" });
    expect(result.current).not.toBe(first);
    // A mark kept from the earlier opening can't write into the new clock.
    act(() => first("header"));
    act(() => result.current("diff"));
    rerender({ ...loading, stateKind: "ready", active: false });
    expect(reportPullRequestTiming).toHaveBeenLastCalledWith(
      expect.not.objectContaining({ headerMs: expect.anything() }),
    );
  });
});
