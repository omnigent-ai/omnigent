// A Pull Requests list row read on this Mac reads its overview when the list's batched snapshot
// of the PR changes, and at a 10-minute backstop, instead of every 30 seconds. With the flag
// off, or on a shell without snapshots, it polls as before.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as CodeHostContextModule from "@/lib/codeHostContext";
import type * as IdentityModule from "@/lib/identity";
import type * as LocalLinksModule from "@/lib/localPullRequestLinks";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: () => true,
  useSessionHostOnline: () => true,
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: unknown) =>
    typeof selector === "function"
      ? (selector as (state: { conversationId: null; sessionStatus: "idle" }) => unknown)({
          conversationId: null,
          sessionStatus: "idle",
        })
      : undefined,
}));
vi.mock("@/lib/localPullRequestLinks", async (importOriginal) => ({
  ...(await importOriginal<typeof LocalLinksModule>()),
  listLocalPullRequests: vi.fn(async () => []),
}));
vi.mock("@/lib/codeHostContext", async (importOriginal) => ({
  ...(await importOriginal<typeof CodeHostContextModule>()),
  useCodeHostContext: vi.fn(() => ({ data: undefined })),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));

import { MacPullRequestScopeContext } from "@/hooks/macPullRequestScope";
import { refreshSessionPullRequests } from "@/hooks/refreshSessionPullRequests";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import {
  PULL_REQUEST_ROW_BACKSTOP_60_FLAG,
  PULL_REQUEST_ROWS_FOLLOW_SNAPSHOTS_FLAG,
  rowBackstopAfter,
} from "@/lib/pullRequestRowSnapshots";

import {
  type PullRequestListEntriesOptions,
  usePullRequestListEntries,
} from "./usePullRequestListEntries";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const SCOPE = "pull-requests-page";
const urlOf = (number: number) => `https://github.com/acme/widgets/pull/${number}`;
const HEAD = "a".repeat(40);

/**
 * What GitHub holds for the PR now; a test moves it to stand for a change on GitHub. The
 * `stats` fields stand for a batch that fails, leaves out a PR's snapshot or part of it.
 */
const world = {
  pending: 1,
  overviewFails: false,
  statsFail: false,
  snapshotMissing: false,
  decisionMissing: false,
};

function overview(number: number) {
  return {
    repository: "acme/widgets",
    pr: {
      number,
      title: "Followed",
      state: "OPEN",
      url: urlOf(number),
      is_draft: false,
      author: "author",
      base_ref: "main",
      head_ref: "feature",
      head_sha: HEAD,
      base_sha: "b".repeat(40),
      body: null,
      checks: {
        passing: 0,
        failing: 0,
        pending: world.pending,
        cancelled: 0,
        skipped: 0,
        total: world.pending,
        runs: [],
      },
      comments: [],
      reviewers: [],
      labels: [],
    },
  };
}

function statsEntry(number: number) {
  const snapshot: Record<string, unknown> = {
    check_runs: world.pending ? { IN_PROGRESS: world.pending } : { SUCCESS: 1 },
    status_contexts: {},
    review_decision: null,
    review_requests: 0,
    latest_reviews: 0,
  };
  if (world.decisionMissing) delete snapshot.review_decision;
  return {
    repository: "acme/widgets",
    number,
    additions: 3,
    deletions: 1,
    changed_files: 1,
    updated_at: "2026-11-11T10:00:00Z",
    head_sha: HEAD,
    state: "OPEN",
    is_draft: false,
    ci: world.pending ? "PENDING" : "SUCCESS",
    ci_head_sha: HEAD,
    ...(world.snapshotMissing ? {} : { snapshot }),
  };
}

function listStats(params: { pull_requests?: { number: number }[] } | undefined) {
  return {
    reader: "mac-octo",
    pull_requests: (params?.pull_requests ?? []).map(({ number }) => statsEntry(number)),
  };
}

/** A shell signed in as `mac-octo`; `rowSnapshots` false stands for an older one. */
function installShell({
  rowSnapshots = true,
  flag,
  hourly,
}: {
  rowSnapshots?: boolean;
  flag?: boolean;
  /** The hourly backstop's flag as the shell answers it; unset, the page's default. */
  hourly?: boolean;
}) {
  let setFlag: (value: unknown) => void = () => {};
  const codeHostQuery = vi.fn(
    async (request: {
      operation: string;
      host: string;
      params?: { number?: number; pull_requests?: { number: number }[] };
    }) => {
      const identity = { host: request.host, login: "mac-octo", generation: 1 };
      if (request.operation === "account_list") {
        return { ok: true, data: [{ login: "mac-octo", active: true, state: "success" }] };
      }
      if (request.operation === "pull_request_list_stats") {
        if (world.statsFail) {
          return { ok: false, error: { code: "network", message: "GitHub didn't answer." } };
        }
        return { ok: true, data: listStats(request.params), identity };
      }
      if (world.overviewFails) {
        return { ok: false, error: { code: "network", message: "GitHub didn't answer." } };
      }
      return { ok: true, data: overview(request.params?.number ?? 42), identity };
    },
  );
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery,
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_list_stats"],
          accounts: true,
          provenance: true,
          ...(rowSnapshots ? { rowSnapshots: 1 } : {}),
        },
      ],
      featureFlags: {
        names: [
          ...(flag === undefined ? [] : [PULL_REQUEST_ROWS_FOLLOW_SNAPSHOTS_FLAG]),
          ...(hourly === undefined ? [] : [PULL_REQUEST_ROW_BACKSTOP_60_FLAG]),
        ],
      },
    }),
    featureFlags: {
      subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
        if (name === PULL_REQUEST_ROWS_FOLLOW_SNAPSHOTS_FLAG) {
          setFlag = callback;
          queueMicrotask(() => callback(flag));
        }
        if (name === PULL_REQUEST_ROW_BACKSTOP_60_FLAG && hourly !== undefined) {
          queueMicrotask(() => callback(hourly));
        }
        return () => {};
      },
    },
  };
  const reads = (operation: string, number?: number) =>
    codeHostQuery.mock.calls.filter(
      ([request]) =>
        request.operation === operation &&
        (number === undefined || request.params?.number === number),
    ).length;
  return {
    /** The shell's flag answer changing, as a rollback does. */
    setFlag: (value: boolean) => setFlag(value),
    overviews: (number?: number) => reads("pull_request_overview", number),
    batches: () => reads("pull_request_list_stats"),
  };
}

const associationFor = (number: number) =>
  ({
    url: urlOf(number),
    host: "github.com",
    repository: "acme/widgets",
    number,
    relationship: "created",
  }) as unknown as PullRequestAssociation;

function mountList(
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
  options: PullRequestListEntriesOptions = {},
) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MacPullRequestScopeContext.Provider value={SCOPE}>
        <SidebarDataProvider>{children}</SidebarDataProvider>
      </MacPullRequestScopeContext.Provider>
    </QueryClientProvider>
  );
  const view = renderHook(
    ({ numbers }: { numbers: number[] }) =>
      usePullRequestListEntries(
        {
          conversationId: SCOPE,
          active: true,
          associations: numbers.map(associationFor),
          hostOffline: false,
          sandboxAsleep: false,
          onSelect: () => {},
          unlinkPending: false,
          managesSessionLinks: false,
        },
        options,
      ),
    { wrapper, initialProps: { numbers: [42] } },
  );
  return { client, ...view };
}

/** Advance a second at a time, so React renders and runs effects as the time passes. */
async function advance(ms: number) {
  for (let left = ms; left > 0; left -= 1_000) {
    // eslint-disable-next-line no-await-in-loop -- each step must render before the next.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(Math.min(left, 1_000));
    });
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(document, "hasFocus").mockReturnValue(true);
  Object.assign(world, {
    pending: 1,
    overviewFails: false,
    statsFail: false,
    snapshotMissing: false,
    decisionMissing: false,
  });
  githubMacReadsPreference.write("on");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 500 })),
  );
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  githubMacReadsPreference.write("automatic");
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
});

describe("a row read on this Mac", () => {
  it("catches up with the first batch once, then reads only when the snapshot changes", async () => {
    const shell = installShell({});
    const { result } = mountList();
    await advance(10_000);
    // Its first answer, then one read to follow the batch that came back meanwhile.
    expect(shell.overviews()).toBe(2);
    expect(shell.batches()).toBeGreaterThanOrEqual(1);
    // The row now shows the diff size its batch read.
    expect(result.current.entries[0]?.row.stats?.additions).toBe(3);

    // Nine quiet minutes: the batch polls; the row's overview doesn't.
    await advance(9 * 60_000);
    expect(shell.overviews()).toBe(2);
    expect(shell.batches()).toBeGreaterThan(10);

    // The check finishes on GitHub: the next batch sees it, and the row reads once.
    world.pending = 0;
    await advance(31_000);
    expect(shell.overviews()).toBe(3);
    await advance(60_000);
    expect(shell.overviews()).toBe(3);
  });

  it("reads at the backstop with an unchanged snapshot, counted from its last answer", async () => {
    const shell = installShell({ hourly: false });
    mountList();
    await advance(10_000);
    expect(shell.overviews()).toBe(2);
    await advance(10 * 60_000 - 10_000);
    expect(shell.overviews()).toBe(2);
    await advance(15_000);
    expect(shell.overviews()).toBe(3);
  });

  it("reads within the hour, at the row's own place in it, with the hourly backstop on", async () => {
    const shell = installShell({});
    mountList(undefined, { showsReadAges: true });
    await advance(10_000);
    expect(shell.overviews()).toBe(2);
    const backstop = rowBackstopAfter(urlOf(42), true);
    expect(backstop).toBeGreaterThan(50 * 60_000);
    // Nobody uses the window all the while: the batches slow, the deadline holds.
    await advance(backstop - 20_000);
    expect(shell.overviews()).toBe(2);
    await advance(15_000);
    expect(shell.overviews()).toBe(3);
  });

  it("reads at once on Refresh, then follows quietly again", async () => {
    const shell = installShell({});
    const { client } = mountList();
    await advance(10_000);
    const [overviews, batches] = [shell.overviews(), shell.batches()];
    await act(async () => {
      await refreshSessionPullRequests(client, SCOPE);
    });
    expect(shell.overviews()).toBe(overviews + 1);
    expect(shell.batches()).toBe(batches + 1);
    await advance(2 * 60_000);
    expect(shell.overviews()).toBe(overviews + 1);
  });

  it.each([
    ["31 seconds", 31_000],
    ["four minutes", 4 * 60_000],
    ["six minutes", 6 * 60_000],
  ])("stays quiet when its list mounts again %s later", async (_after, away) => {
    const shell = installShell({});
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const first = mountList(client);
    await advance(10_000);
    expect(shell.overviews()).toBe(2);
    first.unmount();
    await advance(away);
    mountList(client);
    await advance(1_000);
    expect(shell.overviews()).toBe(2);
  });

  it("keeps the 10-minute backstop where the list shows no read age, as Classic's, flag on", async () => {
    const shell = installShell({});
    mountList();
    await advance(10_000);
    expect(shell.overviews()).toBe(2);
    await advance(10 * 60_000);
    expect(shell.overviews()).toBe(3);
  });

  it("keeps a list that shows no read age batching every 30 s while nobody uses the window", async () => {
    const shell = installShell({});
    mountList();
    // Past the 3 quiet minutes, then 4 more: about 8 batches, not the 2 an idle-paced list makes.
    await advance(3 * 60_000 + 30_000);
    const before = shell.batches();
    await advance(4 * 60_000);
    expect(shell.batches() - before).toBeGreaterThanOrEqual(6);
  });

  it("slows the new page's batches 4 times while nobody uses the window, and reads at input", async () => {
    const shell = installShell({});
    mountList(undefined, { showsReadAges: true });
    await advance(3 * 60_000 + 30_000);
    const before = shell.batches();
    await advance(4 * 60_000);
    expect(shell.batches() - before).toBeLessThanOrEqual(3);
    const idle = shell.batches();
    await act(async () => {
      window.dispatchEvent(new Event("pointermove"));
    });
    await advance(2_000);
    expect(shell.batches()).toBe(idle + 1);
  });

  it("reads once when its list mounts again past the backstop", async () => {
    const shell = installShell({ hourly: false });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const first = mountList(client);
    await advance(10_000);
    first.unmount();
    await advance(11 * 60_000);
    mountList(client);
    await advance(5_000);
    expect(shell.overviews()).toBe(3);
  });

  it.each([
    ["its batch leaves out the snapshot", "snapshotMissing"],
    ["its batch fails", "statsFail"],
  ] as const)(
    "polls every 30 seconds again once %s, and follows again once it's back",
    async (_case, loss) => {
      const shell = installShell({});
      mountList();
      await advance(10_000);
      expect(shell.overviews()).toBe(2);
      world[loss] = true;
      world.pending = 0;
      const lost = shell.batches();
      await advance(2 * 60_000);
      // A failing batch backs off (30 s, then a minute); one that answers keeps polling.
      expect(shell.batches() - lost).toBeGreaterThanOrEqual(2);
      const polled = shell.overviews();
      expect(polled - 2).toBeGreaterThanOrEqual(3);
      // The snapshot comes back: once a batch answers, past any backoff, the row follows it
      // again and goes quiet.
      world[loss] = false;
      await advance(4 * 60_000);
      const recovered = shell.overviews();
      await advance(3 * 60_000);
      expect(shell.overviews()).toBe(recovered);
    },
  );

  it("keeps following when a row joins the list and its batch changes", async () => {
    const shell = installShell({});
    const view = mountList();
    await advance(10_000);
    expect(shell.overviews(42)).toBe(2);
    view.rerender({ numbers: [42, 43] });
    await advance(10_000);
    // The new row reads and catches up; the followed one stays quiet through the new batch.
    expect(shell.overviews(43)).toBe(2);
    await advance(3 * 60_000);
    expect(shell.overviews(42)).toBe(2);
    expect(shell.overviews(43)).toBe(2);
  });

  it("polls every 30 seconds when a row joins and the new batch's first read fails", async () => {
    const shell = installShell({});
    const view = mountList();
    await advance(10_000);
    expect(shell.overviews(42)).toBe(2);
    world.statsFail = true;
    world.pending = 0;
    const lost = shell.batches();
    view.rerender({ numbers: [42, 43] });
    await advance(2 * 60_000);
    // The new batch's reads fail and back off.
    expect(shell.batches() - lost).toBeGreaterThanOrEqual(2);
    // The older batch's snapshot no longer holds the row: it polls as before.
    expect(shell.overviews(42) - 2).toBeGreaterThanOrEqual(3);
    world.statsFail = false;
    await advance(4 * 60_000);
    const recovered = shell.overviews(42);
    await advance(3 * 60_000);
    expect(shell.overviews(42)).toBe(recovered);
  });

  it("never follows a snapshot whose review decision the shell left out", async () => {
    world.decisionMissing = true;
    const shell = installShell({});
    mountList();
    await advance(10_000);
    const first = shell.overviews();
    await advance(2 * 60_000);
    expect(shell.overviews() - first).toBeGreaterThanOrEqual(3);
  });

  it("keeps its own failure backoff after a failed read, whatever the snapshot does", async () => {
    const shell = installShell({ hourly: false });
    mountList();
    await advance(10_000);
    world.overviewFails = true;
    // Past the backstop, the row reads and fails.
    await advance(10 * 60_000);
    const failedAt = shell.overviews();
    world.pending = 0;
    // A changed snapshot doesn't read over the failure; its backoff waits a minute.
    await advance(45_000);
    expect(shell.overviews()).toBe(failedAt);
    world.overviewFails = false;
    await advance(30_000);
    expect(shell.overviews()).toBe(failedAt + 1);
  });

  it("polls every 30 seconds again as soon as the flag is rolled back", async () => {
    const shell = installShell({ flag: true });
    mountList();
    await advance(10_000);
    await advance(2 * 60_000);
    const followed = shell.overviews();
    act(() => shell.setFlag(false));
    await advance(2 * 60_000);
    expect(shell.overviews() - followed).toBeGreaterThanOrEqual(3);
  });

  it.each([
    ["the flag is off", { flag: false }],
    ["the shell predates snapshots", { rowSnapshots: false }],
  ])("polls every 30 seconds and batches nothing when %s", async (_case, shellOptions) => {
    const shell = installShell(shellOptions);
    mountList();
    await advance(10_000);
    const first = shell.overviews();
    await advance(5 * 60_000);
    expect(shell.overviews() - first).toBeGreaterThanOrEqual(9);
    expect(shell.batches()).toBe(0);
  });
});
