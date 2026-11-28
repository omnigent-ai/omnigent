// GitHub polls stop once the window has been unfocused for 2 minutes, read once each when it's
// focused again, and keep running when the rollback flag is off.

import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import {
  FRAME_FOCUS_CHECK_MS,
  UNFOCUSED_POLL_PAUSE_MS,
  createGitHubPollFocus,
} from "./githubPollFocus";

function focusHarness({ focused = true } = {}) {
  let clock = 1_000_000;
  let windowFocused = focused;
  let onFocus = () => {};
  let onBlur = () => {};
  let setEnabled: (enabled: boolean) => void = () => {};
  // What the view has asked for and not yet stopped, so a reset can be seen to stop it.
  const live = { listeners: 0, watches: 0, starts: 0 };
  const timers: { at: number; callback: () => void }[] = [];
  const focus = createGitHubPollFocus({
    now: () => clock,
    focused: () => windowFocused,
    listen: (focusListener, blurListener) => {
      onFocus = focusListener;
      onBlur = blurListener;
      live.listeners += 1;
      live.starts += 1;
      return () => {
        live.listeners -= 1;
      };
    },
    setTimer: (callback, ms) => {
      const timer = { at: clock + ms, callback };
      timers.push(timer);
      return timer as unknown as ReturnType<typeof setTimeout>;
    },
    clearTimer: (timer) => {
      const index = timers.indexOf(timer as unknown as (typeof timers)[number]);
      if (index >= 0) timers.splice(index, 1);
    },
    watchEnabled: (set) => {
      setEnabled = set;
      live.watches += 1;
      return () => {
        live.watches -= 1;
      };
    },
  });
  const advance = (ms: number) => {
    const until = clock + ms;
    for (;;) {
      const next = timers.filter((each) => each.at <= until).sort((a, b) => a.at - b.at)[0];
      if (!next) break;
      clock = Math.max(clock, next.at);
      timers.splice(timers.indexOf(next), 1);
      next.callback();
    }
    clock = until;
  };
  const poll = () => {
    const setOptions = vi.fn();
    const refetch = vi.fn(async () => undefined);
    return {
      query: {
        observers: [{ options: { queryKey: ["q"], refetchInterval: 30_000 }, setOptions, refetch }],
      },
      setOptions,
      refetch,
    };
  };
  return {
    focus,
    advance,
    /** Move the clock on by `ms` without firing timers, as a suspended renderer does. */
    suspend: (ms: number) => {
      clock += ms;
    },
    /** Set the wall clock back by `ms`, as a time zone or NTP correction can. */
    rewind: (ms: number) => {
      clock -= ms;
    },
    poll,
    live,
    timers,
    /** The app loses the keyboard: the window blurs and the page has no focus. */
    blur: () => {
      windowFocused = false;
      onBlur();
    },
    /** A frame in the page takes the keyboard: the window blurs, but the page keeps focus. */
    focusFrame: () => {
      windowFocused = true;
      onBlur();
    },
    refocus: () => {
      windowFocused = true;
      onFocus();
    },
    setEnabled: (enabled: boolean) => setEnabled(enabled),
    /** The listeners and flag setter as this start handed them out, to call after a reset. */
    handedOut: () => ({ onFocus, onBlur, setEnabled }),
    setWindowFocused: (value: boolean) => {
      windowFocused = value;
    },
  };
}

describe("githubPollFocus", () => {
  it("stops polls only once the window has been unfocused for 2 minutes", () => {
    const { focus, advance, poll, blur } = focusHarness();
    const { query } = poll();
    expect(focus.pollPaused(query)).toBe(false);
    blur();
    advance(UNFOCUSED_POLL_PAUSE_MS - 1);
    expect(focus.pollPaused(query)).toBe(false);
    expect(focus.paused()).toBe(false);
    advance(1);
    expect(focus.pollPaused(query)).toBe(true);
    expect(focus.paused()).toBe(true);
  });

  it("keeps polling while a frame in the page has the keyboard, and pauses once it leaves the app", () => {
    const { focus, advance, poll, focusFrame, setWindowFocused, refocus } = focusHarness();
    const { query, refetch } = poll();
    focus.pollPaused(query);
    focusFrame();
    advance(10 * UNFOCUSED_POLL_PAUSE_MS);
    expect(focus.pollPaused(query)).toBe(false);
    // The keyboard leaves the app from the frame: the window hears nothing, the check notices.
    setWindowFocused(false);
    advance(FRAME_FOCUS_CHECK_MS);
    advance(UNFOCUSED_POLL_PAUSE_MS - 1);
    expect(focus.paused()).toBe(false);
    advance(1);
    expect(focus.pollPaused(query)).toBe(true);
    // Coming back straight into the frame ends the pause too, without a window focus event.
    setWindowFocused(true);
    advance(FRAME_FOCUS_CHECK_MS);
    expect(focus.paused()).toBe(false);
    expect(refetch).toHaveBeenCalledTimes(1);
    refocus();
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("releases stopped polls at focus even after the wall clock moved back", () => {
    const { focus, advance, rewind, poll, blur, refocus } = focusHarness();
    const stopped = poll();
    const heard = vi.fn();
    const stop = focus.subscribe(heard);
    blur();
    advance(UNFOCUSED_POLL_PAUSE_MS);
    expect(focus.pollPaused(stopped.query)).toBe(true);
    heard.mockClear();
    // The clock moves back to before the pause would have started, while the polls stay stopped.
    rewind(UNFOCUSED_POLL_PAUSE_MS);
    refocus();
    expect(stopped.refetch).toHaveBeenCalledTimes(1);
    expect(heard).toHaveBeenCalled();
    expect(focus.paused()).toBe(false);
    stop();
  });

  it("reads each stopped poll once when focused again, and nothing after a short blur", () => {
    const { focus, advance, poll, blur, refocus } = focusHarness();
    const stopped = poll();
    // Listening starts with the first poll that asks.
    focus.paused();
    blur();
    advance(UNFOCUSED_POLL_PAUSE_MS);
    focus.pollPaused(stopped.query);
    focus.pollPaused(stopped.query);
    refocus();
    expect(stopped.refetch).toHaveBeenCalledTimes(1);
    expect(stopped.refetch).toHaveBeenCalledWith({ cancelRefetch: false });
    expect(focus.paused()).toBe(false);

    const brief = poll();
    blur();
    advance(UNFOCUSED_POLL_PAUSE_MS / 2);
    focus.pollPaused(brief.query);
    refocus();
    expect(brief.refetch).not.toHaveBeenCalled();
  });

  it("recovers polls at focus after a suspended renderer passed the pause without its timer", () => {
    const { focus, poll, blur, refocus, suspend } = focusHarness();
    const eligible = poll();
    const ineligible = poll();
    expect(focus.pollPaused(eligible.query)).toBe(false);
    expect(focus.pollPaused(ineligible.query, () => false)).toBe(false);
    blur();
    suspend(UNFOCUSED_POLL_PAUSE_MS + 1);
    refocus();
    expect(eligible.refetch).toHaveBeenCalledTimes(1);
    expect(ineligible.refetch).not.toHaveBeenCalled();
    expect(ineligible.setOptions).toHaveBeenCalledTimes(1);
  });

  it("reads through an observer that still polls, never one that's off or stopped polling", () => {
    const { focus, advance, blur, refocus } = focusHarness();
    const refetch = vi.fn(async () => undefined);
    const off = vi.fn(async () => undefined);
    const query = {
      observers: [
        { options: { enabled: false, refetchInterval: 30_000 }, setOptions: vi.fn(), refetch: off },
        { options: { refetchInterval: false }, setOptions: vi.fn(), refetch: off },
        { options: { refetchInterval: () => 30_000 }, setOptions: vi.fn(), refetch },
      ],
    };
    focus.paused();
    blur();
    advance(UNFOCUSED_POLL_PAUSE_MS);
    focus.pollPaused(query);
    refocus();
    expect(refetch).toHaveBeenCalledTimes(1);
    expect(off).not.toHaveBeenCalled();
  });

  it("reschedules instead of reading when its pacer would hold the read", () => {
    const { focus, advance, poll, blur, refocus } = focusHarness();
    const { query, setOptions, refetch } = poll();
    focus.paused();
    blur();
    advance(UNFOCUSED_POLL_PAUSE_MS);
    focus.pollPaused(query, () => false);
    refocus();
    expect(refetch).not.toHaveBeenCalled();
    expect(setOptions).toHaveBeenCalledTimes(1);
  });

  it("counts from the start for a window that opens unfocused", () => {
    const { focus, advance } = focusHarness({ focused: false });
    expect(focus.paused()).toBe(false);
    advance(UNFOCUSED_POLL_PAUSE_MS);
    expect(focus.paused()).toBe(true);
  });

  it("never stops polls with the flag off, and turning it off resumes stopped ones", () => {
    const { focus, advance, poll, blur, setEnabled } = focusHarness();
    const { query, setOptions } = poll();
    focus.paused();
    blur();
    advance(UNFOCUSED_POLL_PAUSE_MS);
    expect(focus.pollPaused(query)).toBe(true);
    setEnabled(false);
    expect(setOptions).toHaveBeenCalledTimes(1);
    expect(focus.pollPaused(query)).toBe(false);
    setEnabled(true);
    expect(focus.pollPaused(query)).toBe(true);
  });
});

describe("a test reset", () => {
  it("stops the timers, listeners and flag watch, and starts them again only at the next use", () => {
    const { focus, live, timers } = focusHarness({ focused: false });
    for (let start = 1; start <= 3; start += 1) {
      focus.paused();
      expect(live).toEqual({ listeners: 1, watches: 1, starts: start });
      // The pause and the frame focus check.
      expect(timers).toHaveLength(2);
      // A repeated reset stacks nothing and stops nothing twice.
      focus.resetForTests();
      focus.resetForTests();
      expect(live).toMatchObject({ listeners: 0, watches: 0 });
      expect(timers).toHaveLength(0);
    }
  });

  it.each([
    ["while the pause is still pending", UNFOCUSED_POLL_PAUSE_MS - 1],
    ["after it took effect", UNFOCUSED_POLL_PAUSE_MS],
  ])("counts unfocused time afresh %s", (_case, elapsed) => {
    const { focus, advance } = focusHarness({ focused: false });
    focus.paused();
    advance(elapsed);
    focus.resetForTests();
    // Still unfocused: the count begins again at the next use, as on a fresh page.
    expect(focus.paused()).toBe(false);
    advance(UNFOCUSED_POLL_PAUSE_MS - 1);
    expect(focus.paused()).toBe(false);
    advance(1);
    expect(focus.paused()).toBe(true);
  });

  it("restarts focused or unfocused as the window is at the next use", () => {
    const { focus, advance, setWindowFocused } = focusHarness({ focused: false });
    focus.paused();
    focus.resetForTests();
    setWindowFocused(true);
    expect(focus.paused()).toBe(false);
    advance(10 * UNFOCUSED_POLL_PAUSE_MS);
    expect(focus.paused()).toBe(false);
    focus.resetForTests();
    setWindowFocused(false);
    expect(focus.paused()).toBe(false);
    advance(UNFOCUSED_POLL_PAUSE_MS);
    expect(focus.paused()).toBe(true);
  });

  it("doesn't carry a flag turned off into the next test", () => {
    const { focus, advance, setEnabled } = focusHarness({ focused: false });
    focus.paused();
    setEnabled(false);
    advance(UNFOCUSED_POLL_PAUSE_MS);
    expect(focus.paused()).toBe(false);
    focus.resetForTests();
    // The pause is on again until the new start's flag watch says otherwise.
    focus.paused();
    advance(UNFOCUSED_POLL_PAUSE_MS);
    expect(focus.paused()).toBe(true);
  });

  it("ignores listeners and flag answers handed out before the reset", () => {
    const harness = focusHarness({ focused: false });
    const { focus, advance } = harness;
    focus.paused();
    const before = harness.handedOut();
    focus.resetForTests();
    focus.paused();
    advance(UNFOCUSED_POLL_PAUSE_MS);
    expect(focus.paused()).toBe(true);
    // The old start's focus and flag answers arrive late: neither may resume this start's polls.
    before.onFocus();
    before.setEnabled(false);
    expect(focus.paused()).toBe(true);
    // This start's own focus still does.
    harness.refocus();
    expect(focus.paused()).toBe(false);
  });

  it("forgets the polls held before it, so focus never reads or wakes them", () => {
    const { focus, advance, poll, refocus } = focusHarness({ focused: false });
    const held = poll();
    focus.paused();
    advance(UNFOCUSED_POLL_PAUSE_MS);
    expect(focus.pollPaused(held.query)).toBe(true);
    focus.resetForTests();
    focus.paused();
    advance(UNFOCUSED_POLL_PAUSE_MS);
    refocus();
    expect(held.refetch).not.toHaveBeenCalled();
    expect(held.setOptions).not.toHaveBeenCalled();
  });
});

describe("hearing the pause", () => {
  it("tells listeners when the pause starts, when focus ends it, and when the flag turns it off", () => {
    const harness = focusHarness();
    const heard = vi.fn();
    const stop = harness.focus.subscribe(heard);
    expect(harness.focus.paused()).toBe(false);
    harness.blur();
    harness.advance(UNFOCUSED_POLL_PAUSE_MS - 1);
    expect(heard).not.toHaveBeenCalled();
    harness.advance(1);
    expect(harness.focus.paused()).toBe(true);
    expect(heard).toHaveBeenCalledTimes(1);
    harness.refocus();
    expect(harness.focus.paused()).toBe(false);
    expect(heard).toHaveBeenCalledTimes(2);
    harness.setEnabled(false);
    expect(heard).toHaveBeenCalledTimes(3);
    stop();
    harness.setEnabled(true);
    expect(heard).toHaveBeenCalledTimes(3);
  });

  it("hears a pause that starts before any poll asks", () => {
    const harness = focusHarness();
    const heard = vi.fn();
    harness.focus.subscribe(heard);
    harness.blur();
    harness.advance(UNFOCUSED_POLL_PAUSE_MS);
    expect(heard).toHaveBeenCalledTimes(1);
  });

  it("forgets its listeners at a reset", () => {
    const harness = focusHarness();
    const heard = vi.fn();
    harness.focus.subscribe(heard);
    harness.focus.resetForTests();
    harness.focus.paused();
    harness.blur();
    harness.advance(UNFOCUSED_POLL_PAUSE_MS);
    expect(harness.focus.paused()).toBe(true);
    expect(heard).not.toHaveBeenCalled();
  });
});

describe("with a real query observer", () => {
  it("stops the poll's interval at the pause, then reads once and polls again at focus", async () => {
    vi.useFakeTimers();
    let windowFocused = true;
    let onFocus = () => {};
    let onBlur = () => {};
    const focus = createGitHubPollFocus({
      focused: () => windowFocused,
      listen: (focusListener, blurListener) => {
        onFocus = focusListener;
        onBlur = blurListener;
      },
    });
    const client = new QueryClient();
    const queryFn = vi.fn(async () => "list");
    const observer = new QueryObserver(client, {
      queryKey: ["pull-requests"],
      queryFn,
      refetchInterval: (query) => (focus.pollPaused(query) ? false : 30_000),
      refetchIntervalInBackground: true,
    });
    const stop = observer.subscribe(() => undefined);
    try {
      // Blur between reads, so the pause itself has to stop the interval.
      await vi.advanceTimersByTimeAsync(40_000);
      expect(queryFn).toHaveBeenCalledTimes(2);
      windowFocused = false;
      onBlur();
      await vi.advanceTimersByTimeAsync(UNFOCUSED_POLL_PAUSE_MS);
      const atPause = queryFn.mock.calls.length;
      await vi.advanceTimersByTimeAsync(10 * UNFOCUSED_POLL_PAUSE_MS);
      expect(queryFn).toHaveBeenCalledTimes(atPause);
      windowFocused = true;
      onFocus();
      await vi.advanceTimersByTimeAsync(0);
      expect(queryFn).toHaveBeenCalledTimes(atPause + 1);
      await vi.advanceTimersByTimeAsync(30_000);
      expect(queryFn).toHaveBeenCalledTimes(atPause + 2);
    } finally {
      stop();
      client.clear();
      focus.resetForTests();
      vi.useRealTimers();
    }
  });
});
