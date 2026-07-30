import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createPresenceIdleTracker } from "./presenceIdle";

// Fake timers also fake Date.now, so the tracker's dwell arithmetic
// (hiddenAt vs now) advances in lockstep with its debounce timer.
beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

const IDLE_AFTER_MS = 30_000;

function tracker() {
  const onFlip = vi.fn();
  const t = createPresenceIdleTracker({ onFlip, idleAfterMs: IDLE_AFTER_MS });
  return { t, onFlip };
}

describe("createPresenceIdleTracker", () => {
  it("never reports idle before the hidden debounce elapses", () => {
    const { t, onFlip } = tracker();
    t.noteReported(false);
    t.handleVisibilityChange(true);
    vi.advanceTimersByTime(IDLE_AFTER_MS - 1);
    // A flip (= a stream reconnect) before the debounce means quick
    // alt-tabs churn the connection and flicker co-viewers' headers.
    expect(onFlip).not.toHaveBeenCalled();
    expect(t.idleNow()).toBe(false);
  });

  it("flips to idle after the debounce while reported active", () => {
    const { t, onFlip } = tracker();
    t.noteReported(false);
    t.handleVisibilityChange(true);
    vi.advanceTimersByTime(IDLE_AFTER_MS);
    // One reconnect request, and the next connect must carry idle=true.
    expect(onFlip).toHaveBeenCalledTimes(1);
    expect(t.idleNow()).toBe(true);
  });

  it("returning to the tab before the debounce cancels the pending flip", () => {
    const { t, onFlip } = tracker();
    t.noteReported(false);
    t.handleVisibilityChange(true);
    vi.advanceTimersByTime(5_000);
    t.handleVisibilityChange(false);
    vi.advanceTimersByTime(IDLE_AFTER_MS * 2);
    // The cancelled timer must never fire — a late flip here would
    // reconnect (and grey the user) while they are actively looking.
    expect(onFlip).not.toHaveBeenCalled();
    expect(t.idleNow()).toBe(false);
  });

  it("flips back to active immediately when a reported-idle tab becomes visible", () => {
    const { t, onFlip } = tracker();
    t.handleVisibilityChange(true);
    vi.advanceTimersByTime(IDLE_AFTER_MS);
    expect(onFlip).toHaveBeenCalledTimes(1);
    t.noteReported(true); // the reconnect carried idle=true
    t.handleVisibilityChange(false);
    // No debounce on un-greying: the user is looking NOW.
    expect(onFlip).toHaveBeenCalledTimes(2);
    expect(t.idleNow()).toBe(false);
  });

  it("does not flip on visible when the stream already reported active", () => {
    const { t, onFlip } = tracker();
    t.noteReported(false);
    t.handleVisibilityChange(true);
    t.handleVisibilityChange(false);
    // Nothing diverged (stream says active, tab is visible) — a flip
    // here would pointlessly recycle the stream on every tab focus.
    expect(onFlip).not.toHaveBeenCalled();
  });

  it("does not flip after the debounce when the stream already reported idle", () => {
    const { t, onFlip } = tracker();
    t.noteReported(true); // e.g. a reconnect already carried idle=true
    t.handleVisibilityChange(true);
    vi.advanceTimersByTime(IDLE_AFTER_MS * 2);
    expect(onFlip).not.toHaveBeenCalled();
  });

  it("keeps the original hiddenAt across repeated hidden events", () => {
    const { t, onFlip } = tracker();
    t.noteReported(false);
    t.handleVisibilityChange(true);
    vi.advanceTimersByTime(20_000);
    // A second hidden notification (some browsers re-fire) must not
    // restart the dwell clock or arm a duplicate timer.
    t.handleVisibilityChange(true);
    vi.advanceTimersByTime(10_000);
    expect(t.idleNow()).toBe(true);
    expect(onFlip).toHaveBeenCalledTimes(1);
  });

  it("idleNow reflects dwell even if the debounce timer never fired", () => {
    // Background tabs throttle timers; the ingress-forced reconnect
    // recomputing idleNow() is what keeps the flag eventually-correct.
    const onFlip = vi.fn();
    const t = createPresenceIdleTracker({ onFlip, idleAfterMs: IDLE_AFTER_MS });
    t.noteReported(true); // pretend the flip already happened elsewhere
    t.handleVisibilityChange(true);
    vi.advanceTimersByTime(IDLE_AFTER_MS * 3);
    expect(t.idleNow()).toBe(true);
  });
});
