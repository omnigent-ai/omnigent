// A reader's hourly budget, as answers report it: polls slow below a quarter of the reader's
// own limit, pause below a tenth until the reset, and run as usual once it passes.

import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createGitHubReadBudgets,
  GITHUB_READ_BUDGET_FLAG,
  githubReadBudgets,
  type GitHubReadBudgets,
} from "./githubReadBudget";

const KEY = "github.com\namy";
const START = 1_000_000;
const RESET = START + 30 * 60_000;

// Each test's stores, cleared after it so no reset timer or flag watch outlives it.
const stores: GitHubReadBudgets[] = [];
afterEach(() => {
  for (const store of stores.splice(0)) store.clear();
  githubReadBudgets.clear();
});

function budgets() {
  let clock = START;
  let setEnabled: (enabled: boolean) => void = () => {};
  const store = createGitHubReadBudgets({
    now: () => clock,
    watchEnabled: (set) => {
      setEnabled = set;
    },
  });
  stores.push(store);
  return {
    store,
    setEnabled: (enabled: boolean) => setEnabled(enabled),
    advance: (ms: number) => {
      clock += ms;
    },
  };
}

const graphql = (remaining: number, limit = 5000, resetAt = RESET) => ({
  resource: "graphql" as const,
  limit,
  remaining,
  resetAt,
});

describe("githubReadBudgets", () => {
  it("slows below a quarter and stops below a tenth of the reader's own limit", () => {
    const cases: [number, number, string][] = [
      [5000, 1250, "normal"],
      [5000, 1249, "slow"],
      [5000, 500, "slow"],
      [5000, 499, "stopped"],
      [15000, 3749, "slow"],
      [15000, 1500, "slow"],
      [15000, 1499, "stopped"],
      [15000, 4000, "normal"],
    ];
    for (const [limit, remaining, pace] of cases) {
      const { store } = budgets();
      store.observe(KEY, graphql(remaining, limit));
      expect(store.pace(KEY).pace, `${remaining}/${limit}`).toBe(pace);
    }
    const slowed = budgets().store;
    slowed.observe(KEY, graphql(1000));
    expect(slowed.pace(KEY)).toEqual({ pace: "slow", factor: 4 });
    const { store } = budgets();
    store.observe(KEY, graphql(10));
    expect(store.pace(KEY)).toEqual({ pace: "stopped", until: RESET });
    store.observe(KEY, graphql(1000));
    expect(store.pace(KEY)).toEqual({ pace: "stopped", until: RESET });
    // Two resources low enough to stop: the stop lasts until the later of their resets.
    const both = budgets().store;
    both.observe(KEY, graphql(50, 5000, START + 60_000));
    both.observe(KEY, { ...graphql(100, 5000, START + 120_000), resource: "core" });
    expect(both.pace(KEY)).toEqual({ pace: "stopped", until: START + 120_000 });
  });

  it("lifts the stop at the reset with no read in between", () => {
    const { store, advance } = budgets();
    store.observe(KEY, graphql(0));
    advance(RESET - START - 1);
    expect(store.pace(KEY).pace).toBe("stopped");
    advance(1);
    expect(store.pace(KEY)).toEqual({ pace: "normal" });
  });

  it("never lets a late answer from the same or an earlier window restore spent budget", () => {
    const { store } = budgets();
    store.observe(KEY, graphql(100));
    store.observe(KEY, graphql(4900));
    store.observe(KEY, graphql(4900, 5000, RESET - 60 * 60_000));
    expect(store.pace(KEY).pace).toBe("stopped");
    // A later reset is a new window.
    store.observe(KEY, graphql(4900, 5000, RESET + 60 * 60_000));
    expect(store.pace(KEY).pace).toBe("normal");
  });

  it("paces nothing it has no budget for, and ignores a malformed one", () => {
    const { store } = budgets();
    store.observe(KEY, { ...graphql(0), limit: 0 });
    store.observe(KEY, undefined);
    expect(store.pace(KEY).pace).toBe("normal");
    expect(store.pace(null).pace).toBe("normal");
    expect(store.pace("github.com\nother").pace).toBe("normal");
  });
});

describe("githubReadBudgets for reads nobody asked for yet", () => {
  const core = (remaining: number, resetAt = RESET) => ({
    resource: "core" as const,
    limit: 5000,
    remaining,
    resetAt,
  });
  const GRAPHQL = ["graphql"] as const;

  it("allows them only on a known budget with half or more of every resource left", () => {
    const { store } = budgets();
    // Nothing seen yet, or no reader: no proof the budget is healthy.
    expect(store.speculativeAllowed(KEY, GRAPHQL)).toBe(false);
    expect(store.speculativeAllowed(null, GRAPHQL)).toBe(false);
    store.observe(KEY, graphql(2500));
    expect(store.speculativeAllowed(KEY, GRAPHQL)).toBe(true);
    // A pace of normal isn't enough: 40% left still paces normally.
    store.observe(KEY, core(2000));
    expect(store.pace(KEY).pace).toBe("normal");
    expect(store.speculativeAllowed(KEY, GRAPHQL)).toBe(false);
    expect(store.speculativeAllowed("github.com\nsomeone-else", GRAPHQL)).toBe(false);
  });

  it("allows none once every budget it knew has reset, until an answer reports one again", () => {
    const { store, advance } = budgets();
    store.observe(KEY, graphql(4000));
    expect(store.speculativeAllowed(KEY, GRAPHQL)).toBe(true);
    advance(RESET - START);
    expect(store.speculativeAllowed(KEY, GRAPHQL)).toBe(false);
    store.observe(KEY, graphql(4999, 5000, RESET + 60 * 60_000));
    expect(store.speculativeAllowed(KEY, GRAPHQL)).toBe(true);
  });

  it("allows none while pacing is off", () => {
    const { store, setEnabled } = budgets();
    store.observe(KEY, graphql(4000));
    setEnabled(false);
    expect(store.speculativeAllowed(KEY, GRAPHQL)).toBe(false);
    store.observe(KEY, graphql(4000));
    expect(store.speculativeAllowed(KEY, GRAPHQL)).toBe(false);
  });

  it("allows a read only on a known budget for each resource it spends", () => {
    const { store, advance } = budgets();
    store.observe(KEY, graphql(4500));
    // A healthy GraphQL budget says nothing about REST's, which a patch spends.
    expect(store.speculativeAllowed(KEY, ["graphql"])).toBe(true);
    expect(store.speculativeAllowed(KEY, ["core"])).toBe(false);
    expect(store.speculativeAllowed(KEY, ["graphql", "core"])).toBe(false);
    expect(store.speculativeAllowed(KEY, [])).toBe(false);
    store.observe(KEY, core(4000, START + 60_000));
    expect(store.speculativeAllowed(KEY, ["core"])).toBe(true);
    expect(store.speculativeAllowed(KEY, ["graphql", "core"])).toBe(true);
    // REST's window resets before GraphQL's: until an answer reports it again, it isn't known.
    advance(60_000);
    expect(store.speculativeAllowed(KEY, ["graphql"])).toBe(true);
    expect(store.speculativeAllowed(KEY, ["core"])).toBe(false);
  });
});

describe("githubReadBudgets across resources", () => {
  const core = (remaining: number, resetAt: number) => ({
    resource: "core" as const,
    limit: 5000,
    remaining,
    resetAt,
  });

  it("keeps each resource's own window, so an older answer for another can't erase a tighter one", () => {
    const { store } = budgets();
    store.observe(KEY, graphql(100, 5000, START + 50 * 60_000));
    store.observe(KEY, core(4000, START + 30 * 60_000));
    expect(store.pace(KEY)).toEqual({ pace: "stopped", until: START + 50 * 60_000 });
  });

  it("paces on the tightest resource whose window is still open", () => {
    const { store, advance } = budgets();
    store.observe(KEY, core(100, START + 10 * 60_000));
    store.observe(KEY, graphql(1000, 5000, START + 50 * 60_000));
    expect(store.pace(KEY)).toEqual({ pace: "stopped", until: START + 10 * 60_000 });
    advance(10 * 60_000);
    expect(store.pace(KEY).pace).toBe("slow");
  });
});

describe("githubReadBudgets and the shell's rollback flag", () => {
  afterEach(() => {
    delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
    vi.resetModules();
  });

  it("drops every budget the moment the flag turns off, and keeps none while it's off", () => {
    const { store, setEnabled } = budgets();
    store.observe(KEY, graphql(10));
    expect(store.pace(KEY).pace).toBe("stopped");
    setEnabled(false);
    expect(store.pace(KEY).pace).toBe("normal");
    store.observe(KEY, graphql(10));
    expect(store.pace(KEY).pace).toBe("normal");
    setEnabled(true);
    store.observe(KEY, graphql(10));
    expect(store.pace(KEY).pace).toBe("stopped");
  });

  it("wakes the polls it held when the flag turns off or a resource's window resets", () => {
    const { store, setEnabled } = budgets();
    const setOptions = vi.fn();
    const query = { observers: [{ options: { queryKey: ["q"] }, setOptions }] };
    store.observe(KEY, graphql(10));
    store.hold(query);
    store.observe(KEY, graphql(5));
    expect(setOptions).not.toHaveBeenCalled();
    store.observe(KEY, graphql(4900, 5000, RESET + 60 * 60_000));
    expect(setOptions).toHaveBeenCalledWith({ queryKey: ["q"] });
    store.hold(query);
    setEnabled(false);
    expect(setOptions).toHaveBeenCalledTimes(2);
  });

  it("tells work paced by hand at the reset, with no answer in between, and leaves held polls be", () => {
    vi.useFakeTimers();
    try {
      const { store, advance } = budgets();
      const setOptions = vi.fn();
      const eased = vi.fn();
      store.subscribe(eased);
      store.observe(KEY, graphql(10));
      store.hold({ observers: [{ options: { queryKey: ["q"] }, setOptions }] });
      const wait = RESET - START;
      advance(wait - 1);
      vi.advanceTimersByTime(wait - 1);
      expect(eased).not.toHaveBeenCalled();
      advance(1);
      vi.advanceTimersByTime(1);
      // A held poll's own interval already ends at the reset; rescheduling it would delay it.
      expect(setOptions).not.toHaveBeenCalled();
      expect(eased).toHaveBeenCalledTimes(1);
      expect(store.pace(KEY)).toEqual({ pace: "normal" });
      vi.advanceTimersByTime(2 * 60 * 60_000);
      expect(eased).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("holds a poll slowed and then stopped only as stopped, so the reset leaves its read due", () => {
    vi.useFakeTimers();
    try {
      const { store, advance } = budgets();
      const setOptions = vi.fn();
      const query = { observers: [{ options: { queryKey: ["q"] }, setOptions }] };
      store.observe(KEY, graphql(1000));
      store.hold(query, store.pace(KEY));
      store.observe(KEY, graphql(10));
      store.hold(query, store.pace(KEY));
      const wait = RESET - START;
      advance(wait);
      vi.advanceTimersByTime(wait);
      expect(setOptions).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("forgets its listeners and flag watch on clear, and pacing turned off with them", () => {
    let setEnabled: (enabled: boolean) => void = () => {};
    const stopWatching = vi.fn();
    const store = createGitHubReadBudgets({
      now: () => START,
      watchEnabled: (set) => {
        setEnabled = set;
        return stopWatching;
      },
    });
    stores.push(store);
    const eased = vi.fn();
    const changed = vi.fn();
    store.subscribe(eased);
    store.subscribeChanges(changed);
    store.observe(KEY, graphql(10));
    setEnabled(false);
    expect(store.pace(KEY)).toEqual({ pace: "normal" });
    store.clear();
    expect(stopWatching).toHaveBeenCalledTimes(1);
    eased.mockClear();
    changed.mockClear();
    const before = setEnabled;
    store.observe(KEY, graphql(10));
    expect(store.pace(KEY)).toMatchObject({ pace: "stopped" });
    expect(changed).not.toHaveBeenCalled();
    // An answer for the watch from before the clear turns nothing off.
    before(false);
    expect(store.pace(KEY)).toMatchObject({ pace: "stopped" });
  });

  it("tells work paced by hand when a window resets or the flag turns off, until it stops", () => {
    const { store, setEnabled } = budgets();
    const eased = vi.fn();
    const stop = store.subscribe(eased);
    store.observe(KEY, graphql(10));
    store.observe(KEY, graphql(5));
    expect(eased).not.toHaveBeenCalled();
    store.observe(KEY, graphql(4900, 5000, RESET + 60 * 60_000));
    expect(eased).toHaveBeenCalledTimes(1);
    setEnabled(false);
    expect(eased).toHaveBeenCalledTimes(2);
    stop();
    setEnabled(true);
    store.observe(KEY, graphql(10));
    store.observe(KEY, graphql(4900, 5000, RESET + 2 * 60 * 60_000));
    expect(eased).toHaveBeenCalledTimes(2);
  });

  it("watches the shell's own flag, live, once a budget first arrives", async () => {
    let deliver: (value: unknown) => void = () => {};
    const subscribe = vi.fn(
      (_name: string, _options: unknown, callback: (value: unknown) => void) => {
        deliver = callback;
        return () => {};
      },
    );
    (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
      kind: "electron",
      featureFlags: { subscribe },
      getDesktopFeatures: async () => ({ featureFlags: { names: [GITHUB_READ_BUDGET_FLAG] } }),
    };
    vi.resetModules();
    // A fresh module's singleton, which watches the shell's flag: cleared after the test.
    const fresh = (await import("./githubReadBudget")).githubReadBudgets;
    stores.push(fresh);
    const budget = { ...graphql(10), resetAt: Date.now() + 30 * 60_000 };
    fresh.observe(KEY, budget);
    await vi.waitFor(() => expect(subscribe).toHaveBeenCalledTimes(1));
    expect(subscribe.mock.calls[0]![0]).toBe(GITHUB_READ_BUDGET_FLAG);
    deliver(true);
    fresh.observe(KEY, budget);
    expect(fresh.pace(KEY).pace).toBe("stopped");
    deliver(false);
    expect(fresh.pace(KEY).pace).toBe("normal");
  });
});

describe("subscribeChanges", () => {
  it("tells each listener about every new budget and the reset, until it stops listening", () => {
    vi.useFakeTimers();
    vi.setSystemTime(START);
    try {
      const store = createGitHubReadBudgets();
      stores.push(store);
      const heard = vi.fn();
      const other = vi.fn();
      const stop = store.subscribeChanges(heard);
      store.subscribeChanges(other);
      store.observe(KEY, graphql(1000));
      expect(heard).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(RESET - START);
      expect(heard).toHaveBeenCalledTimes(2);
      stop();
      store.observe(KEY, graphql(4000, 5000, RESET + 60 * 60_000));
      expect(heard).toHaveBeenCalledTimes(2);
      expect(other).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("with real query observers", () => {
  it("polls a slowed list at its usual pace from the reset, and reads a stopped one at it", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(START);
    const store = createGitHubReadBudgets();
    stores.push(store);
    const client = new QueryClient();
    // As a list's poll paces: slower while slowed, and only until the reset while stopped.
    const poll = (key: string) => {
      const queryFn = vi.fn(async () => key);
      const observer = new QueryObserver(client, {
        queryKey: [key],
        queryFn,
        refetchInterval: (query) => {
          const pace = store.pace(key);
          if (pace.pace === "normal") return 30_000;
          store.hold(query, pace);
          return pace.pace === "slow" ? 30_000 * pace.factor : Math.max(pace.until - Date.now(), 1);
        },
        refetchIntervalInBackground: true,
      });
      return { queryFn, stop: observer.subscribe(() => undefined) };
    };
    store.observe("slowed", graphql(1000));
    store.observe("stopped", graphql(10));
    // Start between slowed reads, so the reset has to restore the interval itself.
    const late = 10_000;
    await vi.advanceTimersByTimeAsync(late);
    const slowed = poll("slowed");
    const stopped = poll("stopped");
    try {
      await vi.advanceTimersByTimeAsync(RESET - START - late - 1);
      expect(stopped.queryFn).toHaveBeenCalledTimes(1);
      const beforeReset = slowed.queryFn.mock.calls.length;
      expect(beforeReset).toBe(1 + Math.floor((RESET - START - late - 1) / 120_000));
      await vi.advanceTimersByTimeAsync(1);
      expect(stopped.queryFn).toHaveBeenCalledTimes(2);
      // Its next read follows the usual interval from the reset, not the slowed one before it.
      const atReset = slowed.queryFn.mock.calls.length;
      await vi.advanceTimersByTimeAsync(30_000 - 1);
      expect(slowed.queryFn).toHaveBeenCalledTimes(atReset);
      await vi.advanceTimersByTimeAsync(1);
      expect(slowed.queryFn).toHaveBeenCalledTimes(atReset + 1);
      expect(stopped.queryFn).toHaveBeenCalledTimes(3);
    } finally {
      slowed.stop();
      stopped.stop();
      client.clear();
      vi.useRealTimers();
    }
  });

  it("keeps a stopped list stopped until every stopped resource has reset", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(START);
    const store = createGitHubReadBudgets();
    stores.push(store);
    const client = new QueryClient();
    // GraphQL is the tighter share and resets first; core is still below a tenth until later.
    store.observe(KEY, graphql(50, 5000, START + 60_000));
    store.observe(KEY, { ...graphql(100, 5000, START + 120_000), resource: "core" });
    const queryFn = vi.fn(async () => "list");
    const observer = new QueryObserver(client, {
      queryKey: ["stopped"],
      queryFn,
      refetchInterval: (query) => {
        const pace = store.pace(KEY);
        if (pace.pace === "normal") return 30_000;
        store.hold(query, pace);
        return pace.pace === "slow" ? 30_000 * pace.factor : Math.max(pace.until - Date.now(), 1);
      },
      refetchIntervalInBackground: true,
    });
    const stop = observer.subscribe(() => undefined);
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(queryFn).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(120_000 - 1);
      expect(queryFn).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(queryFn).toHaveBeenCalledTimes(2);
    } finally {
      stop();
      client.clear();
      vi.useRealTimers();
    }
  });
});
