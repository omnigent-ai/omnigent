// List polls slow 4 times once nobody has used the window for 3 minutes; the first input reads
// each slowed poll that's due, once, and puts the rest back on their usual pace.

import { describe, expect, it, vi } from "vitest";

import { IDLE_POLL_AFTER_MS, IDLE_POLL_FACTOR, createGitHubPollIdle } from "./githubPollIdle";

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

  it("reschedules the polls asked while in use as it turns idle, so none waits out its old timer", () => {
    const { idle, advance, poll } = idleHarness();
    const { query, setOptions } = poll();
    idle.pollFactor(query, () => true);
    advance(IDLE_POLL_AFTER_MS);
    expect(setOptions).toHaveBeenCalledWith({ queryKey: ["q"], refetchInterval: 30_000 });
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
    idle.subscribe(heard);
    advance(IDLE_POLL_AFTER_MS);
    expect(heard).toHaveBeenCalledTimes(1);
    input();
    expect(heard).toHaveBeenCalledTimes(2);
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
});
