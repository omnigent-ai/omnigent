// A windowed list's stats: its read targets are the rows in view, whatever their order, so a
// batch reads only when a row in it has no answer or a stale one, one batch at a time, and a
// row keeps the newest stats any batch found after it scrolls away.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  sameListStatsResult,
  WINDOW_BATCH_SPACING_MS,
  usePullRequestListStats,
} from "@/hooks/usePullRequestListStats";
import { githubCooldownKey, githubCooldowns } from "@/lib/githubCooldown";
import { githubReadBudgets } from "@/lib/githubReadBudget";
import type { PullRequestListStatsTarget } from "@/lib/pullRequestListStats";

const urlOf = (number: number) => `https://github.com/acme/app/pull/${number}`;

function target(number: number): PullRequestListStatsTarget {
  return {
    url: urlOf(number),
    source: { kind: "github-fast-track", account: { host: "github.com", login: "alice" } },
    repository: "acme/app",
    number,
    headSha: undefined,
    open: true,
  };
}

/** A shell answering every list-stats batch, counting what it asks and how many PRs each time. */
function countingShell({ fails = false, latencyMs = 0, changing = false } = {}) {
  const batches: number[][] = [];
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    getDesktopFeatures: async () => ({
      githubFastTrack: true,
      githubFastTrackOperations: ["account_status", "pull_request_list_stats"],
    }),
    githubQuery: vi.fn(async (operation: string, _account: unknown, params: unknown) => {
      if (operation !== "pull_request_list_stats")
        return { ok: false, error: { code: "unsupported" } };
      const asked = (params as { pull_requests: { number: number }[] }).pull_requests;
      batches.push(asked.map((pr) => pr.number));
      // GitHub takes a moment to answer, so an answer lands after its batch was sent.
      if (latencyMs > 0) {
        await new Promise((resolve) => {
          setTimeout(resolve, latencyMs);
        });
      }
      if (fails) return { ok: false, error: { code: "query_failed", message: "GitHub failed." } };
      return {
        ok: true,
        data: {
          pull_requests: asked.map(({ number }) => ({
            repository: "acme/app",
            number,
            // A changing PR answers each batch with new stats: 100 more lines for each batch so far.
            additions: number + (changing ? 100 * batches.length : 0),
            deletions: 0,
            changed_files: 1,
            updated_at: null,
            head_sha: "head",
            state: "OPEN",
            is_draft: false,
            ci: null,
            ci_head_sha: "head",
          })),
        },
      };
    }),
  };
  return batches;
}

/** A list of `rows` whose read window is `reads`; every row shows what its batches found. */
function windowed(rows: PullRequestListStatsTarget[], reads = rows) {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(
    ({
      targets,
      window,
    }: {
      targets: PullRequestListStatsTarget[];
      window: PullRequestListStatsTarget[];
    }) =>
      usePullRequestListStats(targets, {
        enabled: true,
        fastTrackSettled: true,
        followsWindow: true,
        readUrls: new Set(window.map((each) => each.url)),
      }),
    { wrapper, initialProps: { targets: rows, window: reads } },
  );
}

// A second at a time, each in its own act, so the hook renders as a page would between them.
async function advance(ms: number) {
  for (let left = ms; left > 0; left -= 1_000) {
    // eslint-disable-next-line no-await-in-loop -- time passes in order.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(Math.min(1_000, left));
    });
  }
}

// A list grouped by author: the 20 rows in view come from 17 search pages of 25.
const SCATTERED = Array.from({ length: 20 }, (_value, index) =>
  target(1 + ((index * 25) % 420) + index),
);

describe("a windowed list's stats", () => {
  afterEach(() => {
    githubReadBudgets.clear();
    githubCooldowns.reset();
    vi.useRealTimers();
  });

  it("reads one batch of the rows in view every 30 s, however scattered they are", async () => {
    vi.useFakeTimers();
    const batches = countingShell();
    windowed(SCATTERED);
    await advance(1_000);
    expect(batches).toHaveLength(1);
    expect(batches[0]).toHaveLength(20);
    await advance(10 * 60_000);
    // One read a 30 s for 10 minutes: about 120 an hour, not 17 batches each time.
    expect(batches.length).toBeGreaterThanOrEqual(20);
    expect(batches.length).toBeLessThanOrEqual(22);
    expect(batches.every((batch) => batch.length === 20)).toBe(true);
  });

  it("keeps reading every 30 s when GitHub takes a moment to answer each batch", async () => {
    vi.useFakeTimers();
    const batches = countingShell({ latencyMs: 800 });
    windowed(SCATTERED);
    await advance(2_000);
    expect(batches).toHaveLength(1);
    await advance(5 * 60_000);
    expect(batches.length).toBeGreaterThanOrEqual(10);
  });

  it("reads once for a row scrolled into view, not again for every row still there", async () => {
    vi.useFakeTimers();
    const batches = countingShell();
    const rows = [...SCATTERED, target(999)];
    const view = windowed(rows, SCATTERED);
    await advance(1_000);
    expect(batches).toHaveLength(1);
    // Rows outside the window aren't asked about.
    expect(batches[0]).not.toContain(999);
    // One row scrolls in: one read of the new window, after the spacing.
    view.rerender({ targets: rows, window: [...SCATTERED.slice(1, 20), target(999)] });
    await advance(WINDOW_BATCH_SPACING_MS);
    expect(batches).toHaveLength(2);
    expect(batches[1]).toContain(999);
    // Back where it was within 30 s: nothing new to read, and the row that left keeps its stats.
    view.rerender({ targets: rows, window: SCATTERED });
    await advance(WINDOW_BATCH_SPACING_MS);
    expect(batches).toHaveLength(2);
    expect(view.result.current.byUrl.get(urlOf(999))?.additions).toBe(999);
    expect(view.result.current.byUrl.get(SCATTERED[0]!.url)).toBeDefined();
  });

  it("sends at most one batch per spacing while the window keeps moving", async () => {
    vi.useFakeTimers();
    const batches = countingShell();
    const rows = [target(1), ...Array.from({ length: 20 }, (_value, step) => target(1000 + step))];
    const view = windowed(rows, [target(1)]);
    await advance(1_000);
    // A new row every 500 ms for 10 s.
    for (let step = 0; step < 20; step++) {
      view.rerender({ targets: rows, window: [target(1000 + step)] });
      // eslint-disable-next-line no-await-in-loop -- one step at a time.
      await advance(500);
    }
    expect(batches.length).toBeLessThanOrEqual(1 + 10_000 / WINDOW_BATCH_SPACING_MS + 1);
  });

  it("reads nothing while the reader's budget is spent, and reads once it isn't", async () => {
    vi.useFakeTimers();
    const batches = countingShell();
    githubReadBudgets.observe(githubCooldownKey("github.com", "alice"), {
      resource: "graphql",
      limit: 5000,
      remaining: 100,
      resetAt: Date.now() + 60_000,
    });
    windowed(SCATTERED);
    await advance(30_000);
    expect(batches).toHaveLength(0);
    await advance(40_000);
    expect(batches.length).toBeGreaterThanOrEqual(1);
  });

  it("waits a minute after a failed batch, instead of every spacing", async () => {
    vi.useFakeTimers();
    const batches = countingShell({ fails: true });
    windowed(SCATTERED);
    await advance(1_000);
    expect(batches).toHaveLength(1);
    await advance(50_000);
    expect(batches).toHaveLength(1);
    await advance(15_000);
    expect(batches).toHaveLength(2);
  });

  it("keeps a row's newest stats when it scrolls back into an older batch", async () => {
    vi.useFakeTimers();
    const batches = countingShell({ changing: true });
    const rows = [target(1), target(2), target(3)];
    const view = windowed(rows, rows.slice(0, 2));
    await advance(1_000);
    expect(view.result.current.byUrl.get(urlOf(2))?.additions).toBe(102);
    view.rerender({ targets: rows, window: rows.slice(1) });
    await advance(WINDOW_BATCH_SPACING_MS);
    expect(batches).toHaveLength(2);
    expect(view.result.current.byUrl.get(urlOf(2))?.additions).toBe(202);
    const newer = view.result.current.snapshots?.get(urlOf(2))?.dispatchedAt;
    // Back to the first batch, still fresh: nothing reads, and PR 2 keeps the second answer.
    view.rerender({ targets: rows, window: rows.slice(0, 2) });
    await advance(1_000);
    expect(batches).toHaveLength(2);
    expect(view.result.current.byUrl.get(urlOf(2))?.additions).toBe(202);
    expect(view.result.current.snapshots?.get(urlOf(2))?.dispatchedAt).toBe(newer);
    expect(view.result.current.byUrl.get(urlOf(1))?.additions).toBe(101);
  });

  it("keeps its result between renders while nothing new is read", async () => {
    vi.useFakeTimers();
    const batches = countingShell();
    const rows = [target(1), target(2), target(3)];
    const view = windowed(rows);
    await advance(1_000);
    expect(batches).toHaveLength(1);
    const shown = view.result.current;
    expect(shown.byUrl.get(urlOf(2))?.additions).toBe(2);
    // A list re-renders for its own reasons, a selection or a removal; its rows' stats stay put,
    // or every row is derived again on every render and the list's updates never settle.
    view.rerender({ targets: rows, window: rows });
    view.rerender({ targets: [...rows], window: rows });
    expect(view.result.current).toBe(shown);
  });

  it("keeps one batch in flight while the rows in view change, then reads the latest", async () => {
    vi.useFakeTimers();
    const batches = countingShell({ latencyMs: 60_000 });
    const rows = [target(1), target(2), target(3)];
    const view = windowed(rows, rows.slice(0, 1));
    await advance(1_000);
    expect(batches).toEqual([[1]]);
    view.rerender({ targets: rows, window: rows.slice(1, 2) });
    await advance(10_000);
    view.rerender({ targets: rows, window: rows.slice(2) });
    await advance(10_000);
    expect(batches).toEqual([[1]]);
    // The first answer lands; the rows in view now read, not those it passed on the way.
    await advance(41_000);
    expect(batches).toEqual([[1], [3]]);
  });

  it("reads a tall viewport's two batches one after the other, not together", async () => {
    vi.useFakeTimers();
    const batches = countingShell({ latencyMs: 60_000 });
    const rows = Array.from({ length: 26 }, (_value, index) => target(index + 1));
    windowed(rows);
    await advance(1_000);
    // 26 rows make a batch of 25 and one of 1; only the first is in flight.
    expect(batches.map((batch) => batch.length)).toEqual([25]);
    await advance(30_000);
    expect(batches).toHaveLength(1);
    // Once it answers, the waiting batch goes, after the spacing.
    await advance(31_000 + WINDOW_BATCH_SPACING_MS);
    expect(batches.map((batch) => batch.length)).toEqual([25, 1]);
  });

  it("re-reads a moved head's batch within the same bound", async () => {
    vi.useFakeTimers();
    const batches = countingShell({ latencyMs: 60_000 });
    const rows = Array.from({ length: 26 }, (_value, index) => target(index + 1));
    const view = windowed(rows);
    await advance(61_000 + WINDOW_BATCH_SPACING_MS);
    expect(batches).toHaveLength(2);
    // The second batch is in flight when a push moves PR 1's head.
    const moved = [{ ...rows[0]!, headSha: "pushed" }, ...rows.slice(1)];
    view.rerender({ targets: moved, window: moved });
    await advance(30_000);
    expect(batches).toHaveLength(2);
    await advance(31_000 + WINDOW_BATCH_SPACING_MS);
    expect(batches).toHaveLength(3);
    expect(batches[2]).toContain(1);
  });

  it("waits out a failure's backoff whichever rows the next batch holds", async () => {
    vi.useFakeTimers();
    const batches = countingShell({ fails: true });
    const rows = [target(1), target(2), target(3)];
    const view = windowed(rows, rows.slice(0, 1));
    await advance(1_000);
    expect(batches).toHaveLength(1);
    view.rerender({ targets: rows, window: rows.slice(1, 2) });
    await advance(20_000);
    // The held batch's row says why it has no stats, as the failed one's does.
    expect(view.result.current.status).toBe("failed");
    expect(view.result.current.gaps.get(urlOf(2))).toMatchObject({ kind: "read-failed" });
    view.rerender({ targets: rows, window: rows.slice(2) });
    await advance(30_000);
    expect(batches).toHaveLength(1);
    await advance(15_000);
    expect(batches).toEqual([[1], [3]]);
  });
});

describe("sameListStatsResult", () => {
  const stats = {
    additions: 1,
    deletions: 0,
    changedFiles: 1,
    updatedAt: undefined,
    headSha: "head",
    dispatchedAt: 5,
    extended: false,
  };
  const result = (additions = 1, gap = "not-reported" as const) => ({
    status: "ready" as const,
    byUrl: new Map([[urlOf(1), { ...stats, additions }]]),
    gaps: new Map([[urlOf(2), { kind: gap, service: "github" as const }]]),
    snapshots: new Map([[urlOf(1), { fingerprint: "f", reader: "alice", dispatchedAt: 5 }]]),
  });

  it("matches results that show the same, however they were built", () => {
    expect(sameListStatsResult(result(), result())).toBe(true);
  });

  it("tells apart a changed status, stat, gap, or row", () => {
    expect(sameListStatsResult(result(), { ...result(), status: "loading" })).toBe(false);
    expect(sameListStatsResult(result(), result(2))).toBe(false);
    expect(sameListStatsResult(result(), { ...result(), gaps: new Map() })).toBe(false);
    expect(
      sameListStatsResult(result(), {
        ...result(),
        byUrl: new Map([[urlOf(3), stats]]),
      }),
    ).toBe(false);
    expect(sameListStatsResult(result(), { ...result(), snapshots: undefined })).toBe(false);
  });
});
