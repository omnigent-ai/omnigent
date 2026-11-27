// List polls slow 4 times once nobody has used the window for 3 minutes; the first input reads
// each slowed poll that's due, once, and puts the rest back on their usual pace.

import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import {
  IDLE_INPUT_EVENTS,
  IDLE_POLL_AFTER_MS,
  IDLE_POLL_FACTOR,
  createGitHubPollIdle,
} from "./githubPollIdle";

function idleHarness() {
  let clock = 1_000_000;
  let onInput = () => {};
  let setEnabled: (enabled: boolean) => void = () => {};
  const timers: { at: number; callback: () => void }[] = [];
  const idle = createGitHubPollIdle({
    now: () => clock,
    listen: (inputListener) => {
      onInput = inputListener;
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
    },
  });
  const advance = (ms: number) => {
    const until = clock + ms;
    for (;;) {
      const next = timers.filter((each) => each.at <= until).sort((a, b) => a.at - b.at)[0];
      if (!next) break;
      clock = next.at;
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
    idle,
    advance,
    poll,
    input: () => onInput(),
    /** The input listener as this start handed it out, to call after a reset. */
    handedOut: () => onInput,
    setEnabled: (enabled: boolean) => setEnabled(enabled),
  };
}

describe("githubPollIdle", () => {
  it("slows polls only once nobody has used the window for 3 minutes", () => {
    const { idle, advance, poll, input } = idleHarness();
    const { query } = poll();
    expect(idle.pollFactor(query, () => true)).toBe(1);
    advance(60_000);
    input();
    advance(IDLE_POLL_AFTER_MS - 1);
    expect(idle.pollFactor(query, () => true)).toBe(1);
    expect(idle.factor()).toBe(1);
    advance(1);
    expect(idle.pollFactor(query, () => true)).toBe(IDLE_POLL_FACTOR);
    expect(idle.factor()).toBe(IDLE_POLL_FACTOR);
  });

  it("counts a person's input through the window, never the app's own scrolling", () => {
    let clock = 1_000_000;
    const idle = createGitHubPollIdle({ now: () => clock, setTimer: () => 0 as never });
    const log = document.createElement("div");
    document.body.append(log);
    try {
      idle.factor();
      const setOptions = vi.fn();
      const query = { observers: [{ options: { refetchInterval: 30_000 }, setOptions }] };
      // Streamed output scrolls the log into view every few seconds; nobody touches the window.
      for (let elapsed = 0; elapsed < IDLE_POLL_AFTER_MS; elapsed += 5_000) {
        clock += 5_000;
        log.dispatchEvent(new Event("scroll"));
      }
      expect(idle.pollFactor(query, () => true)).toBe(IDLE_POLL_FACTOR);
      log.dispatchEvent(new Event("scroll"));
      expect(idle.factor()).toBe(IDLE_POLL_FACTOR);
      expect(setOptions).not.toHaveBeenCalled();
      // Each input a person gives wakes it.
      for (const type of IDLE_INPUT_EVENTS) {
        clock += IDLE_POLL_AFTER_MS;
        expect(idle.factor()).toBe(IDLE_POLL_FACTOR);
        log.dispatchEvent(new Event(type, { bubbles: true }));
        expect(idle.factor(), type).toBe(1);
      }
    } finally {
      log.remove();
      idle.resetForTests();
    }
  });

  it("reads each slowed poll that's due once at the first input, and reschedules the others", () => {
    const { idle, advance, poll, input } = idleHarness();
    const due = poll();
    const notDue = poll();
    idle.factor();
    advance(IDLE_POLL_AFTER_MS);
    idle.pollFactor(due.query, () => true);
    idle.pollFactor(due.query, () => true);
    idle.pollFactor(notDue.query, () => false);
    input();
    expect(due.refetch).toHaveBeenCalledTimes(1);
    expect(due.refetch).toHaveBeenCalledWith({ cancelRefetch: false });
    expect(notDue.refetch).not.toHaveBeenCalled();
    expect(notDue.setOptions).toHaveBeenCalledTimes(1);
    expect(idle.factor()).toBe(1);
    // Later input in use reads nothing more.
    input();
    expect(due.refetch).toHaveBeenCalledTimes(1);
  });

  it("tells work that paces itself when idle starts and ends", () => {
    const { idle, advance, input } = idleHarness();
    const heard = vi.fn();
    const other = vi.fn();
    const stop = idle.subscribe(heard);
    idle.subscribe(other);
    advance(IDLE_POLL_AFTER_MS);
    expect(heard).toHaveBeenCalledTimes(1);
    input();
    expect(heard).toHaveBeenCalledTimes(2);
    stop();
    advance(IDLE_POLL_AFTER_MS);
    input();
    expect(heard).toHaveBeenCalledTimes(2);
    expect(other).toHaveBeenCalledTimes(4);
  });

  it("never slows polls with the flag off, and turning it off puts slowed ones back", () => {
    const { idle, advance, poll, setEnabled } = idleHarness();
    const { query, setOptions } = poll();
    idle.factor();
    advance(IDLE_POLL_AFTER_MS);
    expect(idle.pollFactor(query, () => true)).toBe(IDLE_POLL_FACTOR);
    setEnabled(false);
    expect(setOptions).toHaveBeenCalledTimes(1);
    expect(idle.pollFactor(query, () => true)).toBe(1);
    advance(IDLE_POLL_AFTER_MS);
    expect(idle.factor()).toBe(1);
  });

  it("counts a page as in use when it opens, and starts afresh after a test reset", () => {
    const { idle, advance } = idleHarness();
    advance(10 * IDLE_POLL_AFTER_MS);
    // Nothing asked yet: the count starts with the first poll that asks.
    expect(idle.factor()).toBe(1);
    advance(IDLE_POLL_AFTER_MS);
    expect(idle.factor()).toBe(IDLE_POLL_FACTOR);
    idle.resetForTests();
    expect(idle.factor()).toBe(1);
  });

  it("ignores input and listeners from before a test reset", () => {
    const harness = idleHarness();
    const { idle, advance, input } = harness;
    const heard = vi.fn();
    idle.subscribe(heard);
    const before = harness.handedOut();
    idle.resetForTests();
    expect(idle.factor()).toBe(1);
    advance(IDLE_POLL_AFTER_MS);
    expect(idle.factor()).toBe(IDLE_POLL_FACTOR);
    // The old start's input arrives late: it keeps nothing in use, and old listeners hear nothing.
    before();
    expect(idle.factor()).toBe(IDLE_POLL_FACTOR);
    expect(heard).not.toHaveBeenCalled();
    input();
    expect(idle.factor()).toBe(1);
  });
});

describe("with a real query observer", () => {
  it("slows the poll's interval 4 times at idle, then reads once and keeps its pace at input", async () => {
    vi.useFakeTimers();
    let onInput = () => {};
    const idle = createGitHubPollIdle({
      listen: (inputListener) => {
        onInput = inputListener;
      },
    });
    const client = new QueryClient();
    const queryFn = vi.fn(async () => "list");
    const observer = new QueryObserver(client, {
      queryKey: ["pull-requests"],
      queryFn,
      refetchInterval: (query) => 30_000 * idle.pollFactor(query, () => true),
      refetchIntervalInBackground: true,
    });
    const stop = observer.subscribe(() => undefined);
    try {
      // Input between reads, so idle starts between them and has to slow the interval itself.
      await vi.advanceTimersByTimeAsync(10_000);
      onInput();
      await vi.advanceTimersByTimeAsync(IDLE_POLL_AFTER_MS);
      const atIdle = queryFn.mock.calls.length;
      await vi.advanceTimersByTimeAsync(IDLE_POLL_FACTOR * 30_000 - 1);
      expect(queryFn).toHaveBeenCalledTimes(atIdle);
      await vi.advanceTimersByTimeAsync(1);
      expect(queryFn).toHaveBeenCalledTimes(atIdle + 1);
      onInput();
      await vi.advanceTimersByTimeAsync(0);
      expect(queryFn).toHaveBeenCalledTimes(atIdle + 2);
      await vi.advanceTimersByTimeAsync(30_000);
      expect(queryFn).toHaveBeenCalledTimes(atIdle + 3);
    } finally {
      stop();
      client.clear();
      idle.resetForTests();
      vi.useRealTimers();
    }
  });
});
