// A failing read backs off on its own, counted from real TanStack fetches, and every read
// waits out its reader's GitHub cooldown.

import { QueryClient, QueryObserver, focusManager } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createGitHubCooldowns, githubCooldownKey } from "./githubCooldown";
import { UNFOCUSED_POLL_PAUSE_MS, createGitHubPollFocus } from "./githubPollFocus";
import { IDLE_POLL_AFTER_MS, createGitHubPollIdle } from "./githubPollIdle";
import { createGitHubReadBudgets } from "./githubReadBudget";
import {
  FAILED_READ_POLL_MS,
  MAX_FAILED_READ_POLL_MS,
  PULL_REQUEST_LIST_POLL_MS,
  consecutiveReadFailures,
  pacedPollInterval,
} from "./pullRequestPolling";

const KEY = githubCooldownKey("github.com", "amy");

/** A real query whose next fetch answers or fails as the test says. */
function realQuery() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let next: () => unknown = () => ({ ok: true });
  const queryKey = ["pr-page-github-info", "session", "https://github.com/acme/app/pull/1"];
  const fetch = async (answer: () => unknown) => {
    next = answer;
    await client
      .fetchQuery({ queryKey, queryFn: async () => next(), staleTime: 0 })
      .catch(() => undefined);
  };
  const query = () => client.getQueryCache().find({ queryKey, exact: true })!;
  return { fetch, query };
}

const fail = () => {
  throw new Error("GitHub couldn't answer.");
};
const answer = () => ({ pr: { state: "OPEN" } });
const localRefusal = () => {
  throw Object.assign(new Error("limited"), { code: "rate_limited", local: true });
};

describe("pacedPollInterval", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("spaces a real observer's polls 60, 120, 240 s apart while they fail, and 30 s once answered", async () => {
    vi.useFakeTimers();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const answers = [fail, fail, fail, answer, answer, fail, answer];
    const fetchedAt: number[] = [];
    const started = Date.now();
    const observer = new QueryObserver(client, {
      queryKey: ["pr-page-github-info", "session", "https://github.com/acme/app/pull/2"],
      queryFn: async () => {
        fetchedAt.push(Date.now() - started);
        return (answers.shift() ?? answer)();
      },
      refetchInterval: (query) => pacedPollInterval(query, PULL_REQUEST_LIST_POLL_MS, null),
    });
    const unsubscribe = observer.subscribe(() => {});
    await vi.advanceTimersByTimeAsync(541_000);
    unsubscribe();
    expect(fetchedAt).toEqual([0, 60_000, 180_000, 420_000, 450_000, 480_000, 540_000]);
  });

  it("wakes a row GitHub limited within the spread after a short deadline, even after earlier failures", async () => {
    vi.useFakeTimers();
    const cooldowns = createGitHubCooldowns({ now: () => Date.now(), random: () => 0 });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const started = Date.now();
    const fetchedAt: number[] = [];
    // Two ordinary failures, then GitHub's limit for 4 s, then answers.
    const script = [
      fail,
      fail,
      () => {
        cooldowns.rateLimited(KEY, Date.now() + 4_000);
        throw Object.assign(new Error("limited"), { code: "rate_limited" });
      },
    ];
    const observer = new QueryObserver(client, {
      queryKey: ["pr-page-github-info", "session", "https://github.com/acme/app/pull/3"],
      queryFn: async () => {
        fetchedAt.push(Date.now() - started);
        return (script.shift() ?? answer)();
      },
      refetchInterval: (query) =>
        pacedPollInterval(query, PULL_REQUEST_LIST_POLL_MS, KEY, {
          cooldowns,
          spreadOf: () => 0.5,
          wakeSpreadMs: 30_000,
        }),
    });
    const unsubscribe = observer.subscribe(() => {});
    await vi.advanceTimersByTimeAsync(180_000 + 4_000);
    // The list's own read clears the cooldown at its deadline; the row follows within 30 s.
    cooldowns.answered(KEY);
    await vi.advanceTimersByTimeAsync(30_000);
    unsubscribe();
    // 0 and 60 s fail, 180 s is limited until 184 s, and the row reads again at 199 s.
    expect(fetchedAt).toEqual([0, 60_000, 180_000, 199_000]);
  });

  it("keeps the list's recovery read and a row's wake in place however often it's asked", async () => {
    const { fetch, query } = realQuery();
    let clock = 5_000_000;
    const cooldowns = createGitHubCooldowns({ now: () => clock, random: () => 0 });
    const limited = () => {
      cooldowns.rateLimited(KEY, clock + 4_000);
      throw Object.assign(new Error("limited"), { code: "rate_limited" });
    };
    await fetch(limited);
    const options = { cooldowns, now: () => clock };
    const rowWake = () => clock + (pacedPollInterval(query(), 30_000, KEY, options) as number);
    const listWake = () =>
      clock + (pacedPollInterval(query(), 120_000, KEY, { ...options, recovers: true }) as number);
    const firstRow = rowWake();
    expect(listWake()).toBe(clock + 4_000);
    // A countdown re-renders every second; each re-render asks again.
    for (let second = 0; second < 4; second += 1) {
      clock += 1_000;
      expect(rowWake()).toBe(firstRow);
    }
    expect(firstRow).toBeGreaterThanOrEqual(clock);
    expect(firstRow).toBeLessThanOrEqual(clock + 30_000);
    // Asked just after the wait ended, before anything read: the list reads now, not in 2 min.
    clock += 1;
    expect(pacedPollInterval(query(), 120_000, KEY, { ...options, recovers: true })).toBe(1);
    // And after the list's answer cleared the cooldown, the row still wakes where it would.
    cooldowns.answered(KEY);
    expect(rowWake()).toBe(firstRow);
  });

  it("backs a new limit off instead of waking on an earlier cooldown's passed deadline", async () => {
    const { fetch, query } = realQuery();
    let clock = 5_000_000;
    const cooldowns = createGitHubCooldowns({ now: () => clock, random: () => 0 });
    cooldowns.rateLimited(KEY, clock + 1_000);
    clock += 60_000;
    cooldowns.answered(KEY);
    await fetch(() => {
      throw Object.assign(new Error("limited"), { code: "rate_limited" });
    });
    const options = { cooldowns, now: () => clock, spreadOf: () => 0 };
    expect(pacedPollInterval(query(), 30_000, KEY, options)).toBe(FAILED_READ_POLL_MS);
    expect(pacedPollInterval(query(), 30_000, KEY, { ...options, recovers: true })).toBe(
      FAILED_READ_POLL_MS,
    );
  });

  it("starts no timer while a slow recovery read is in flight, so held rows never spin", async () => {
    vi.useFakeTimers();
    const cooldowns = createGitHubCooldowns({ now: () => Date.now(), random: () => 0 });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const queryKey = ["pr-page-github-info", "session", "https://github.com/acme/app/pull/4"];
    client.setQueryData(queryKey, "cached");
    let reads = 0;
    let finish: (value: string) => void = () => {};
    const observer = new QueryObserver(client, {
      queryKey,
      queryFn: async () => {
        reads += 1;
        if (reads === 1) {
          cooldowns.rateLimited(KEY, Date.now() + 4_000);
          throw Object.assign(new Error("limited"), { code: "rate_limited" });
        }
        return new Promise<string>((resolve) => {
          finish = resolve;
        });
      },
      refetchInterval: (query) =>
        pacedPollInterval(query, PULL_REQUEST_LIST_POLL_MS, KEY, { cooldowns, spreadOf: () => 0 }),
    });
    const unsubscribe = observer.subscribe(() => {});
    await vi.advanceTimersByTimeAsync(1);
    const query = client.getQueryCache().find({ queryKey, exact: true })!;
    const fetches = vi.spyOn(query, "fetch");
    await vi.advanceTimersByTimeAsync(4_100);
    expect(fetches.mock.calls.length).toBeLessThanOrEqual(1);
    finish("answered");
    await vi.advanceTimersByTimeAsync(1);
    unsubscribe();
    client.clear();
  });

  it("gives an unanswered read whose poll would rest the failure backoff", async () => {
    const { fetch, query } = realQuery();
    await fetch(fail);
    expect(pacedPollInterval(query(), false, null)).toBe(FAILED_READ_POLL_MS);
    await fetch(fail);
    expect(pacedPollInterval(query(), false, null)).toBe(2 * FAILED_READ_POLL_MS);
    await fetch(answer);
    expect(pacedPollInterval(query(), false, null)).toBe(false);
  });

  it("backs a failing read off 60, 120, 240 s, resets on an answer, then starts at 60 s", async () => {
    const { fetch, query } = realQuery();
    const paces: (number | false)[] = [];
    for (const read of [fail, fail, fail, answer, fail]) {
      // eslint-disable-next-line no-await-in-loop -- each poll follows the last one's answer.
      await fetch(read);
      paces.push(pacedPollInterval(query(), PULL_REQUEST_LIST_POLL_MS, null));
    }
    expect(paces).toEqual([
      FAILED_READ_POLL_MS,
      2 * FAILED_READ_POLL_MS,
      4 * FAILED_READ_POLL_MS,
      PULL_REQUEST_LIST_POLL_MS,
      FAILED_READ_POLL_MS,
    ]);
  });

  it("stops doubling at 15 minutes", async () => {
    const { fetch, query } = realQuery();
    for (let read = 0; read < 12; read += 1) {
      // eslint-disable-next-line no-await-in-loop -- each failure follows the last.
      await fetch(fail);
      pacedPollInterval(query(), PULL_REQUEST_LIST_POLL_MS, null);
    }
    expect(pacedPollInterval(query(), PULL_REQUEST_LIST_POLL_MS, null)).toBe(
      MAX_FAILED_READ_POLL_MS,
    );
  });

  it("reads the same streak however often it's asked between fetches", async () => {
    const { fetch, query } = realQuery();
    // TanStack asks for the pace after every settled fetch, often more than once.
    await fetch(fail);
    for (let ask = 0; ask < 3; ask += 1) consecutiveReadFailures(query());
    await fetch(fail);
    for (let ask = 0; ask < 5; ask += 1) consecutiveReadFailures(query());
    expect(pacedPollInterval(query(), PULL_REQUEST_LIST_POLL_MS, null)).toBe(
      2 * FAILED_READ_POLL_MS,
    );
  });

  it("neither counts nor resets on the page's own refusals within a cooldown", async () => {
    const { fetch, query } = realQuery();
    await fetch(fail);
    pacedPollInterval(query(), PULL_REQUEST_LIST_POLL_MS, null);
    await fetch(localRefusal);
    consecutiveReadFailures(query());
    await fetch(localRefusal);
    expect(consecutiveReadFailures(query())).toBe(1);
    await fetch(fail);
    expect(pacedPollInterval(query(), PULL_REQUEST_LIST_POLL_MS, null)).toBe(
      2 * FAILED_READ_POLL_MS,
    );
  });

  it("keeps a resting poll at rest once it has an answer, even if a refresh failed", async () => {
    const { fetch, query } = realQuery();
    await fetch(answer);
    await fetch(fail);
    expect(pacedPollInterval(query(), false, null)).toBe(false);
  });

  it("holds a read until its reader's cooldown ends, waking within the spread after it", async () => {
    const { fetch, query } = realQuery();
    await fetch(answer);
    let clock = 5_000_000;
    const cooldowns = createGitHubCooldowns({ now: () => clock, random: () => 0 });
    cooldowns.rateLimited(KEY, clock + 300_000);
    const options = { cooldowns, now: () => clock, spreadOf: () => 0.5, wakeSpreadMs: 30_000 };
    expect(pacedPollInterval(query(), PULL_REQUEST_LIST_POLL_MS, KEY, options)).toBe(315_000);
    // Another reader's poll isn't held.
    expect(
      pacedPollInterval(
        query(),
        PULL_REQUEST_LIST_POLL_MS,
        githubCooldownKey("github.com", "b"),
        options,
      ),
    ).toBe(PULL_REQUEST_LIST_POLL_MS);
    // The recovering poll reads exactly when the wait ends.
    expect(
      pacedPollInterval(query(), PULL_REQUEST_LIST_POLL_MS, KEY, { ...options, recovers: true }),
    ).toBe(300_000);
    clock += 300_000;
    expect(pacedPollInterval(query(), PULL_REQUEST_LIST_POLL_MS, KEY, options)).toBe(
      PULL_REQUEST_LIST_POLL_MS,
    );
  });
});

describe("pacedPollInterval on the reader's hourly budget", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /** A real observer polling every 30 s as `KEY`; returns when each read went out, in s. */
  function poll(budgets: ReturnType<typeof createGitHubReadBudgets>) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const started = Date.now();
    const readsAt: number[] = [];
    const observer = new QueryObserver(client, {
      queryKey: ["pr-page-github-info", "session", "https://github.com/acme/app/pull/3"],
      queryFn: async () => {
        readsAt.push((Date.now() - started) / 1000);
        return answer();
      },
      refetchInterval: (query) =>
        pacedPollInterval(query, PULL_REQUEST_LIST_POLL_MS, KEY, {
          budgets,
          cooldowns: createGitHubCooldowns(),
          spreadOf: () => 0,
        }),
    });
    const unsubscribe = observer.subscribe(() => {});
    return {
      readsAt,
      observer,
      stop: () => {
        unsubscribe();
        client.clear();
      },
    };
  }

  const budget = (remaining: number, resetInMs: number) => ({
    resource: "graphql" as const,
    limit: 5000,
    remaining,
    resetAt: Date.now() + resetInMs,
  });

  it("polls 4 times slower below a quarter left", async () => {
    vi.useFakeTimers();
    const budgets = createGitHubReadBudgets();
    budgets.observe(KEY, budget(1000, 60 * 60_000));
    const { readsAt, stop } = poll(budgets);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    stop();
    expect(readsAt).toEqual([0, 120, 240, 360, 480, 600]);
  });

  it("returns a slowed poll to its usual pace at the reset, without waiting out the slowed interval", async () => {
    vi.useFakeTimers();
    const budgets = createGitHubReadBudgets();
    budgets.observe(KEY, budget(1000, 3 * 60_000));
    const { readsAt, stop } = poll(budgets);
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    stop();
    // Slowed to 2 minutes until the reset at 180 s, then every 30 s from it.
    expect(readsAt).toEqual([0, 120, 210, 240, 270, 300]);
  });

  it("pauses below a tenth until the reset, then reads with no read needed to lift it", async () => {
    vi.useFakeTimers();
    const budgets = createGitHubReadBudgets();
    budgets.observe(KEY, budget(100, 15 * 60_000));
    const { readsAt, stop } = poll(budgets);
    await vi.advanceTimersByTimeAsync(16 * 60_000);
    stop();
    expect(readsAt).toEqual([0, 900, 930, 960]);
  });

  it("reads again within one poll once the rollback flag turns off, on the same observer", async () => {
    vi.useFakeTimers();
    let setEnabled: (enabled: boolean) => void = () => {};
    const budgets = createGitHubReadBudgets({
      watchEnabled: (set) => {
        setEnabled = set;
      },
    });
    budgets.observe(KEY, budget(100, 50 * 60_000));
    const { readsAt, stop } = poll(budgets);
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    expect(readsAt).toEqual([0]);
    setEnabled(false);
    await vi.advanceTimersByTimeAsync(65_000);
    stop();
    expect(readsAt).toEqual([0, 330, 360]);
  });

  it("still reads on Refresh while paused, and keeps the pause after it", async () => {
    vi.useFakeTimers();
    const budgets = createGitHubReadBudgets();
    budgets.observe(KEY, budget(100, 15 * 60_000));
    const { readsAt, observer, stop } = poll(budgets);
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    await observer.refetch();
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    stop();
    expect(readsAt).toEqual([0, 300]);
  });
});

describe("pacedPollInterval in an unfocused window", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("stops a real observer's poll 2 minutes after the window loses focus, and reads once on focus", async () => {
    vi.useFakeTimers();
    // The window's focus as the test sets it, which the focus check also reads.
    let hasFocus = true;
    let onFocus = () => {};
    let onBlur = () => {};
    const focus = createGitHubPollFocus({
      focused: () => hasFocus,
      listen: (focusListener, blurListener) => {
        onFocus = () => {
          hasFocus = true;
          focusListener();
        };
        onBlur = () => {
          hasFocus = false;
          blurListener();
        };
      },
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const started = Date.now();
    const readsAt: number[] = [];
    const observer = new QueryObserver(client, {
      queryKey: ["pr-page-github-info", "session", "https://github.com/acme/app/pull/4"],
      queryFn: async () => {
        readsAt.push((Date.now() - started) / 1000);
        return answer();
      },
      refetchInterval: (query) =>
        pacedPollInterval(query, PULL_REQUEST_LIST_POLL_MS, KEY, {
          focus,
          budgets: createGitHubReadBudgets(),
          cooldowns: createGitHubCooldowns(),
        }),
    });
    const unsubscribe = observer.subscribe(() => {});
    await vi.advanceTimersByTimeAsync(10_000);
    onBlur();
    await vi.advanceTimersByTimeAsync(UNFOCUSED_POLL_PAUSE_MS + 3 * 60_000);
    // Polls ran until the pause, at 130 s, and none after it.
    expect(readsAt).toEqual([0, 30, 60, 90, 120]);
    onFocus();
    await vi.advanceTimersByTimeAsync(65_000);
    unsubscribe();
    client.clear();
    expect(readsAt.slice(5)).toEqual([310, 340, 370]);
  });
});

describe("pacedPollInterval when focus returns", () => {
  afterEach(() => {
    focusManager.setFocused(undefined);
    vi.useRealTimers();
  });

  /** A mounted observer polling every `interval` as `KEY`, with the app's no-refetch-on-focus. */
  function watch(interval = PULL_REQUEST_LIST_POLL_MS) {
    const started = Date.now();
    const readsAt: number[] = [];
    // The window's focus as the test sets it, which the focus check also reads.
    let hasFocus = true;
    let onFocus = () => {};
    let onBlur = () => {};
    const focus = createGitHubPollFocus({
      focused: () => hasFocus,
      listen: (focusListener, blurListener) => {
        onFocus = () => {
          hasFocus = true;
          focusListener();
        };
        onBlur = () => {
          hasFocus = false;
          blurListener();
        };
      },
    });
    const budgets = createGitHubReadBudgets();
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
    });
    client.mount();
    const observer = new QueryObserver(client, {
      queryKey: ["pr-page-github-info", "session", "https://github.com/acme/app/pull/5"],
      queryFn: async () => {
        readsAt.push((Date.now() - started) / 1000);
        return answer();
      },
      refetchInterval: (query) =>
        pacedPollInterval(query, interval, KEY, {
          focus,
          budgets,
          cooldowns: createGitHubCooldowns(),
          spreadOf: () => 0,
        }),
      refetchIntervalInBackground: false,
    });
    const unsubscribe = observer.subscribe(() => {});
    return {
      readsAt,
      observer,
      budgets,
      blur: () => onBlur(),
      refocus: () => onFocus(),
      stop: () => {
        unsubscribe();
        client.clear();
        client.unmount();
      },
    };
  }

  it("stops a poll already scheduled past the pause, without a read during the grace period", async () => {
    vi.useFakeTimers();
    const watched = watch(5 * 60_000);
    await vi.advanceTimersByTimeAsync(10_000);
    watched.blur();
    await vi.advanceTimersByTimeAsync(350_000);
    watched.stop();
    expect(watched.readsAt).toEqual([0]);
  });

  it("reads at once on focus after a hidden window skipped every poll in the grace period", async () => {
    vi.useFakeTimers();
    const watched = watch();
    await vi.advanceTimersByTimeAsync(10_000);
    watched.blur();
    focusManager.setFocused(false);
    await vi.advanceTimersByTimeAsync(170_000);
    expect(watched.readsAt).toEqual([0]);
    focusManager.setFocused(true);
    watched.refocus();
    await vi.advanceTimersByTimeAsync(0);
    watched.stop();
    expect(watched.readsAt).toEqual([0, 180]);
  });

  it("reads on focus when a suspended window passed the pause before its timer could run", async () => {
    vi.useFakeTimers();
    const watched = watch(5 * 60_000);
    await vi.advanceTimersByTimeAsync(10_000);
    watched.blur();
    // A suspended renderer gains wall time without running its timers.
    vi.setSystemTime(Date.now() + 180_000);
    watched.refocus();
    await vi.advanceTimersByTimeAsync(0);
    watched.stop();
    expect(watched.readsAt).toEqual([0, 190]);
  });

  it("keeps the focus read behind a budget hold learned while stopped", async () => {
    vi.useFakeTimers();
    const watched = watch();
    await vi.advanceTimersByTimeAsync(10_000);
    watched.blur();
    await vi.advanceTimersByTimeAsync(150_000);
    watched.budgets.observe(KEY, {
      resource: "graphql",
      limit: 5000,
      remaining: 100,
      resetAt: Date.now() + 60 * 60_000,
    });
    const before = watched.readsAt.length;
    watched.refocus();
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    watched.stop();
    expect(watched.readsAt.length).toBe(before);
  });

  it("reads nothing on focus for a query whose observer stopped polling meanwhile", async () => {
    vi.useFakeTimers();
    const watched = watch();
    await vi.advanceTimersByTimeAsync(10_000);
    watched.blur();
    await vi.advanceTimersByTimeAsync(150_000);
    watched.observer.setOptions({ ...watched.observer.options, refetchInterval: false });
    const before = watched.readsAt.length;
    watched.refocus();
    await vi.advanceTimersByTimeAsync(60_000);
    watched.stop();
    expect(watched.readsAt.length).toBe(before);
  });
});

describe("pacedPollInterval in a window nobody uses", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  const budget = (remaining: number, resetInMs: number) => ({
    resource: "graphql" as const,
    limit: 5000,
    remaining,
    resetAt: Date.now() + resetInMs,
  });

  /**
   * A mounted observer polling every `interval` as `KEY`, idle-paced or as a deadline, with its
   * own idle view; returns read times in seconds and the window's input.
   */
  function watch({
    interval = PULL_REQUEST_LIST_POLL_MS,
    kind = "poll" as "poll" | "deadline",
    idlePaced = true,
    budgets = createGitHubReadBudgets(),
    cooldowns = createGitHubCooldowns(),
  } = {}) {
    let onInput = () => {};
    const idle = createGitHubPollIdle({
      listen: (inputListener) => {
        onInput = inputListener;
      },
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const started = Date.now();
    const readsAt: number[] = [];
    const observer = new QueryObserver(client, {
      queryKey: ["pr-page-github-info", "session", "https://github.com/acme/app/pull/9"],
      queryFn: async () => {
        readsAt.push((Date.now() - started) / 1000);
        return answer();
      },
      refetchInterval: (query) =>
        pacedPollInterval(
          query,
          kind === "deadline"
            ? Math.max(query.state.dataUpdatedAt + interval - Date.now(), 1)
            : interval,
          KEY,
          {
            idle,
            budgets,
            cooldowns,
            focus: createGitHubPollFocus({ focused: () => true, listen: () => {} }),
            spreadOf: () => 0,
            kind,
            idlePaced,
          },
        ),
    });
    const unsubscribe = observer.subscribe(() => {});
    return {
      readsAt,
      input: () => onInput(),
      stop: () => {
        unsubscribe();
        client.clear();
      },
    };
  }

  it("polls 4 times slower 3 minutes after the last input, and reads what's due at the next", async () => {
    vi.useFakeTimers();
    const watched = watch();
    await vi.advanceTimersByTimeAsync(5_000);
    watched.input();
    // Idle from 185 s: the poll that read at 180 s waits 2 minutes from the slowdown.
    await vi.advanceTimersByTimeAsync(IDLE_POLL_AFTER_MS + 315_000);
    expect(watched.readsAt).toEqual([0, 30, 60, 90, 120, 150, 180, 305, 425]);
    // At 500 s the user moves: it's been 75 s since the last read, so it reads once, then every 30 s.
    watched.input();
    await vi.advanceTimersByTimeAsync(65_000);
    watched.stop();
    expect(watched.readsAt.slice(9)).toEqual([500, 530, 560]);
  });

  it("never slows a poll that isn't idle-paced, such as the open PR's own", async () => {
    vi.useFakeTimers();
    const watched = watch({ idlePaced: false });
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    watched.stop();
    expect(watched.readsAt).toHaveLength(21);
  });

  it("slows 4 times, not 16, when a low budget slows it too: the longest hold wins", async () => {
    vi.useFakeTimers();
    const budgets = createGitHubReadBudgets();
    budgets.observe(KEY, budget(1000, 2 * 60 * 60_000));
    const watched = watch({ budgets });
    await vi.advanceTimersByTimeAsync(20 * 60_000);
    watched.stop();
    expect(
      new Set(watched.readsAt.slice(1).map((at, index) => at - watched.readsAt[index]!)),
    ).toEqual(new Set([120]));
  });

  it("keeps a deadline where it is when idle or low on budget", async () => {
    vi.useFakeTimers();
    const budgets = createGitHubReadBudgets();
    budgets.observe(KEY, budget(1000, 3 * 60 * 60_000));
    const watched = watch({ interval: 55 * 60_000, kind: "deadline", budgets });
    await vi.advanceTimersByTimeAsync(2 * 60 * 60_000);
    watched.stop();
    expect(watched.readsAt).toEqual([0, 3300, 6600]);
  });

  it("holds a deadline while the budget is nearly out, and reads it once the budget resets", async () => {
    vi.useFakeTimers();
    const budgets = createGitHubReadBudgets();
    budgets.observe(KEY, budget(100, 70 * 60_000));
    const watched = watch({ interval: 55 * 60_000, kind: "deadline", budgets });
    await vi.advanceTimersByTimeAsync(80 * 60_000);
    watched.stop();
    // Due at 55 minutes, held until the reset at 70, then read.
    expect(watched.readsAt).toEqual([0, 4200]);
  });

  it("holds a deadline through its reader's cooldown, and reads within the spread after it", async () => {
    vi.useFakeTimers();
    const cooldowns = createGitHubCooldowns();
    cooldowns.rateLimited(KEY, Date.now() + 10 * 60_000);
    const watched = watch({ interval: 5 * 60_000, kind: "deadline", cooldowns });
    await vi.advanceTimersByTimeAsync(16 * 60_000);
    watched.stop();
    // Due at 5 minutes, held until the cooldown ends at 10, then on its own deadline again.
    expect(watched.readsAt).toEqual([0, 600, 900]);
  });

  it("puts a poll back on its pace at input without a read while its reader is held", async () => {
    vi.useFakeTimers();
    const cooldowns = createGitHubCooldowns();
    const watched = watch({ cooldowns });
    await vi.advanceTimersByTimeAsync(IDLE_POLL_AFTER_MS + 60_000);
    const before = watched.readsAt.length;
    cooldowns.rateLimited(KEY, Date.now() + 5 * 60_000);
    watched.input();
    await vi.advanceTimersByTimeAsync(1_000);
    watched.stop();
    expect(watched.readsAt).toHaveLength(before);
  });
});
