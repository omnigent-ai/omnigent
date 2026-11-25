// The PR info query's own poll: a linked read that never answered still tries again on the
// failure backoff, instead of resting forever.

import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { rowBackstopAfter } from "@/lib/pullRequestRowSnapshots";

import type { PullRequestChecks, PullRequestInfo } from "./usePullRequests";
import { pullRequestInfoQueryOptions } from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

afterEach(() => {
  vi.useRealTimers();
});

const context = () =>
  ({
    conversationId: "session",
    hostServeable: true,
    readPathKnown: () => true,
    readsOnThisMac: () => false,
    target: () => null,
    scope: () => [],
    readBlocked: () => false,
    hasLocalLinks: false,
    sessionless: false,
    retry: () => false,
  }) as unknown as Parameters<typeof pullRequestInfoQueryOptions>[0];

describe("pullRequestInfoQueryOptions polling", () => {
  it("reads a linked PR again 60 s, then 120 s, after its first reads fail", async () => {
    vi.useFakeTimers();
    const options = pullRequestInfoQueryOptions(context(), { poll: "linked" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const started = Date.now();
    const fetchedAt: number[] = [];
    const observer = new QueryObserver(client, {
      ...options,
      queryFn: async () => {
        fetchedAt.push(Date.now() - started);
        throw new Error("GitHub couldn't answer.");
      },
    });
    const unsubscribe = observer.subscribe(() => {});
    await vi.advanceTimersByTimeAsync(181_000);
    unsubscribe();
    client.clear();
    expect(fetchedAt).toEqual([0, 60_000, 180_000]);
  });
});

describe("the open PR's checks poll", () => {
  const checks = (over: Partial<PullRequestChecks> = {}): PullRequestChecks => ({
    passing: 0,
    failing: 0,
    pending: 0,
    total: 0,
    runs: [],
    ...over,
  });
  const openPullRequest = (
    head: string,
    prChecks: PullRequestChecks,
    tracked = false,
  ): PullRequestInfo => ({
    object: "session.github.info",
    ...(tracked ? { tracking_available: true } : {}),
    available: true,
    gh_available: true,
    authenticated: true,
    branch: "feature",
    repo: { name_with_owner: "o/r" },
    base_ref: "main",
    pr: {
      number: 1,
      title: "t",
      state: "OPEN",
      url: "https://github.com/o/r/pull/1",
      is_draft: false,
      author: null,
      base_ref: "main",
      head_ref: "feature",
      head_sha: head,
      checks: prChecks,
    },
  });

  /** Watch the PR from now, answering each read with `answer()`; returns read times in seconds. */
  function watch(answer: () => PullRequestInfo, paced = true) {
    const started = Date.now();
    const options = pullRequestInfoQueryOptions(context(), {
      poll: true,
      prUrl: "https://github.com/o/r/pull/1",
      ...(paced ? { checksPacing: { openedAt: started } } : {}),
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const readsAt: number[] = [];
    const observer = new QueryObserver(client, {
      ...options,
      queryFn: async () => {
        readsAt.push((Date.now() - started) / 1000);
        return answer();
      },
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

  const gaps = (readsAt: number[], from: number, to: number) =>
    readsAt
      .slice(1)
      .map((at, index) => [readsAt[index]!, at - readsAt[index]!] as const)
      .filter(([at]) => at >= from && at < to)
      .map(([, gap]) => gap);

  it("with no checks yet: 5 s for 2 minutes, then 30 s, then every 2 minutes after 10, never stopping", async () => {
    vi.useFakeTimers();
    const { readsAt, stop } = watch(() => openPullRequest("a1", checks()));
    await vi.advanceTimersByTimeAsync(20 * 60_000);
    stop();
    expect(new Set(gaps(readsAt, 0, 115))).toEqual(new Set([5]));
    expect(new Set(gaps(readsAt, 125, 590))).toEqual(new Set([30]));
    expect(new Set(gaps(readsAt, 610, 1080))).toEqual(new Set([120]));
    // Twenty minutes with no checks: 24 + about 16 + 5 reads, not 240.
    expect(readsAt.length).toBeLessThan(50);
  });

  it("finds checks that first appear after the slowdown, and watches them fast again", async () => {
    vi.useFakeTimers();
    let current = openPullRequest("a1", checks());
    const { readsAt, stop } = watch(() => current);
    await vi.advanceTimersByTimeAsync(11 * 60_000);
    // Checks appear eleven minutes in; a read due at that moment ran before they did.
    current = openPullRequest("a1", checks({ pending: 2, total: 2 }));
    const appeared = 11 * 60;
    await vi.advanceTimersByTimeAsync(3 * 60_000);
    stop();
    const seen = readsAt.find((at) => at > appeared)!;
    // Seen within one discovery interval, then read every 5 s again.
    expect(seen - appeared).toBeLessThanOrEqual(120);
    expect(new Set(gaps(readsAt, seen, seen + 115))).toEqual(new Set([5]));
  });

  it("sees a push after the poll slowed within one slow interval, and reads fast after it", async () => {
    vi.useFakeTimers();
    let current = openPullRequest("a1", checks({ pending: 1, total: 1 }));
    const { readsAt, stop } = watch(() => current);
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    current = openPullRequest("b2", checks({ pending: 1, total: 1 }));
    await vi.advanceTimersByTimeAsync(3 * 60_000);
    stop();
    // A read due at the push ran before it: the first read after it sees the new head.
    const seen = readsAt.find((at) => at > 300)!;
    expect(seen - 300).toBeLessThanOrEqual(30);
    expect(new Set(gaps(readsAt, seen, seen + 115))).toEqual(new Set([5]));
  });

  it("never restarts the fast phase on answers that show the same head and checks", async () => {
    vi.useFakeTimers();
    const { readsAt, stop } = watch(() => openPullRequest("a1", checks({ pending: 1, total: 1 })));
    await vi.advanceTimersByTimeAsync(8 * 60_000);
    stop();
    expect(new Set(gaps(readsAt, 125, 470))).toEqual(new Set([30]));
  });

  it("reads every 5 s as before without pacing, which the rollback flag gives", async () => {
    vi.useFakeTimers();
    const { readsAt, stop } = watch(() => openPullRequest("a1", checks()), false);
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    stop();
    expect(new Set(gaps(readsAt, 0, 295))).toEqual(new Set([5]));
  });

  it("paces a PR whose session tracks its links, which otherwise reads every 10 s", async () => {
    vi.useFakeTimers();
    const tracked = () => openPullRequest("a1", checks({ pending: 1, total: 1 }), true);
    const paced = watch(tracked);
    await vi.advanceTimersByTimeAsync(8 * 60_000);
    paced.stop();
    expect(new Set(gaps(paced.readsAt, 0, 115))).toEqual(new Set([5]));
    expect(new Set(gaps(paced.readsAt, 125, 470))).toEqual(new Set([30]));
    const steady = watch(tracked, false);
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    steady.stop();
    expect(new Set(gaps(steady.readsAt, 0, 290))).toEqual(new Set([10]));
  });

  it("reads fast again when checks that settled run again with the same answer as before", async () => {
    vi.useFakeTimers();
    let current = openPullRequest("a1", checks({ pending: 1, total: 1 }));
    const { readsAt, observer, stop } = watch(() => current);
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    // Settled, nothing polls; a rerun's answer, read on Refresh, matches the first one.
    current = openPullRequest("a1", checks({ passing: 1, total: 1 }));
    await observer.refetch();
    await vi.advanceTimersByTimeAsync(60_000);
    current = openPullRequest("a1", checks({ pending: 1, total: 1 }));
    await observer.refetch();
    const rerunAt = readsAt.at(-1)!;
    await vi.advanceTimersByTimeAsync(60_000);
    stop();
    expect(new Set(gaps(readsAt, rerunAt, rerunAt + 55))).toEqual(new Set([5]));
  });
});

describe("a followed list row's backstop", () => {
  const URL = "https://github.com/o/r/pull/7";
  const followedRow = {
    object: "session.github.info",
    available: true,
    pr: { number: 7, state: "OPEN", url: URL },
    followed_snapshot: "s1",
  } as unknown as PullRequestInfo;

  /** A row that follows its batch's snapshot, read as each observer mounts; read times in s. */
  function rowReads(hourlyBackstop: boolean) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const started = Date.now();
    const readsAt: number[] = [];
    const mount = () => {
      const options = pullRequestInfoQueryOptions(context(), {
        poll: "list",
        prUrl: URL,
        followsSnapshot: true,
        hourlyBackstop,
      });
      const observer = new QueryObserver(client, {
        ...options,
        queryFn: async () => {
          readsAt.push(Math.round((Date.now() - started) / 1000));
          return followedRow;
        },
      });
      return observer.subscribe(() => {});
    };
    return { client, readsAt, mount };
  }

  it("reads once within the hour, at its own place in the last 10 minutes, idle or not", async () => {
    vi.useFakeTimers();
    const { client, readsAt, mount } = rowReads(true);
    const unmount = mount();
    // Nobody touches the window: the deadline isn't stretched by idle pacing.
    await vi.advanceTimersByTimeAsync(20 * 60_000);
    // A remount, as scrolling the row away and back does, reads nothing and keeps its place.
    unmount();
    const remounted = mount();
    await vi.advanceTimersByTimeAsync(55 * 60_000);
    remounted();
    client.clear();
    const backstop = Math.round(rowBackstopAfter(URL, true) / 1000);
    expect(backstop).toBeGreaterThan(50 * 60);
    expect(backstop).toBeLessThanOrEqual(60 * 60);
    expect(readsAt).toEqual([0, backstop]);
  });

  it("reads every 10 minutes with the hourly backstop off", async () => {
    vi.useFakeTimers();
    const { client, readsAt, mount } = rowReads(false);
    const unmount = mount();
    await vi.advanceTimersByTimeAsync(31 * 60_000);
    unmount();
    client.clear();
    expect(readsAt).toEqual([0, 600, 1200, 1800]);
  });
});
