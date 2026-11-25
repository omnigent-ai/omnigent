// Held polls wake when what held them lets go, and holding one never keeps a query alive after
// TanStack removed it.

import { setFlagsFromString } from "node:v8";
import { runInNewContext } from "node:vm";

import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { createGitHubCooldowns } from "./githubCooldown";
import { createGitHubReadBudgets } from "./githubReadBudget";
import { createHeldPolls } from "./heldPolls";
import { pacedPollInterval } from "./pullRequestPolling";

const poll = (observerCount = 1) => {
  const setOptions = vi.fn();
  const observers = Array.from({ length: observerCount }, () => ({
    options: { queryKey: ["q"] },
    setOptions,
  }));
  return { query: { observers }, setOptions };
};

describe("createHeldPolls", () => {
  it("reschedules each held poll once, by every observer, then holds none", () => {
    const held = createHeldPolls();
    const first = poll(2);
    const second = poll();
    held.hold(first.query);
    held.hold(first.query);
    held.hold(second.query);
    expect(held.size()).toBe(2);
    held.reschedule();
    expect(first.setOptions).toHaveBeenCalledTimes(2);
    expect(second.setOptions).toHaveBeenCalledWith({ queryKey: ["q"] });
    held.reschedule();
    expect(first.setOptions).toHaveBeenCalledTimes(2);
    expect(held.size()).toBe(0);
  });

  it("lets go of a poll whose observers all left, and of one an observer can't reschedule", () => {
    const held = createHeldPolls();
    const left = { observers: [] as { options: unknown; setOptions: () => void }[] };
    const broken = {
      observers: [
        {
          options: {},
          setOptions: () => {
            throw new Error("gone");
          },
        },
      ],
    };
    held.hold(left);
    held.hold(broken);
    expect(held.size()).toBe(1);
    expect(() => held.reschedule()).not.toThrow();
    expect(held.size()).toBe(0);
  });
});

/** Collect garbage over a few event turns, so finished promises let go of what they held. */
async function collectGarbage(gc: () => void, turns = 6): Promise<void> {
  await new Promise<void>((resolve) => {
    setTimeout(resolve, 15);
  });
  gc();
  if (turns > 1) await collectGarbage(gc, turns - 1);
}

describe("a poll a budget held, once TanStack removes it", () => {
  it("can be collected, while a live observer's poll still wakes", async () => {
    setFlagsFromString("--expose_gc");
    const gc = runInNewContext("typeof gc === 'function' ? gc : undefined") as
      (() => void) | undefined;
    expect(typeof gc).toBe("function");
    let clock = 1_000_000;
    let setEnabled: (enabled: boolean) => void = () => {};
    const budgets = createGitHubReadBudgets({
      now: () => clock,
      watchEnabled: (set) => {
        setEnabled = set;
      },
    });
    budgets.observe("k", {
      resource: "graphql",
      limit: 5000,
      remaining: 100,
      resetAt: clock + 3_600_000,
    });
    const options = (key: string) => ({
      queryKey: [key],
      queryFn: async () => ({ payload: key }),
      refetchInterval: (query: Parameters<typeof pacedPollInterval>[0]) =>
        pacedPollInterval(query, 30_000, "k", {
          budgets,
          now: () => clock,
          cooldowns: createGitHubCooldowns(),
        }),
    });

    async function removedQuery() {
      const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0, retry: false } } });
      const observer = new QueryObserver(client, options("removed"));
      const unsubscribe = observer.subscribe(() => {});
      await observer.refetch();
      const ref = new WeakRef(client.getQueryCache().getAll()[0]!);
      unsubscribe();
      client.clear();
      return ref;
    }
    const removed = await removedQuery();

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const live = new QueryObserver(client, options("live"));
    const unsubscribe = live.subscribe(() => {});
    await live.refetch();
    const setOptions = vi.spyOn(live, "setOptions");

    await collectGarbage(gc!);
    expect(removed.deref()).toBeUndefined();
    setEnabled(false);
    expect(setOptions).toHaveBeenCalledTimes(1);
    unsubscribe();
    client.clear();
  });
});
