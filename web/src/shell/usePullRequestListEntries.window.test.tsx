// A windowed Pull Requests list reads only the rows in its viewport, once they've stayed there,
// and a few the user is working with: never a row it scrolled past or merely holds. Its stats
// read for those rows alone, and wait like its overviews while the app's own reads are held.

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
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { githubCooldownKey } from "@/lib/githubCooldown";
import { githubReadBudgets } from "@/lib/githubReadBudget";
import { PULL_REQUEST_ROWS_FOLLOW_SNAPSHOTS_FLAG } from "@/lib/pullRequestRowSnapshots";

import { usePullRequestListEntries } from "./usePullRequestListEntries";

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
function installShell({ rowSnapshots = true, flag }: { rowSnapshots?: boolean; flag?: boolean }) {
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
      featureFlags: { names: flag === undefined ? [] : [PULL_REQUEST_ROWS_FOLLOW_SNAPSHOTS_FLAG] },
    }),
    featureFlags: {
      subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
        if (name === PULL_REQUEST_ROWS_FOLLOW_SNAPSHOTS_FLAG) {
          setFlag = callback;
          queueMicrotask(() => callback(flag));
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
    /** The PR numbers each stats batch asked for. */
    statsBatches: () =>
      codeHostQuery.mock.calls
        .filter(([request]) => request.operation === "pull_request_list_stats")
        .map(([request]) => (request.params?.pull_requests ?? []).map((pr) => pr.number)),
  };
}

/** A 420-row view, and the 20 rows its viewport holds at the top. */
const ALL = Array.from({ length: 420 }, (_value, index) => index + 1);
const FIRST_SCREEN = ALL.slice(0, 20);

const associationFor = (number: number) =>
  ({
    url: urlOf(number),
    host: "github.com",
    repository: "acme/widgets",
    number,
    relationship: "created",
  }) as unknown as PullRequestAssociation;

function mountList(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MacPullRequestScopeContext.Provider value={SCOPE}>
        <SidebarDataProvider>{children}</SidebarDataProvider>
      </MacPullRequestScopeContext.Provider>
    </QueryClientProvider>
  );
  const view = renderHook(
    ({
      numbers,
      visible,
      exceptions,
    }: {
      numbers: number[];
      visible: number[];
      exceptions: number[];
    }) =>
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
        { readWindow: { visible: visible.map(urlOf), exceptions: exceptions.map(urlOf) } },
      ),
    { wrapper, initialProps: { numbers: ALL, visible: FIRST_SCREEN, exceptions: [] as number[] } },
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

// Each case steps minutes of time over 420 rows, a second at a time.
describe("a windowed list of 420 rows", { timeout: 60_000 }, () => {
  it("reads only the rows in view, once each, and their stats in one batch", async () => {
    const shell = installShell({});
    mountList();
    await advance(10_000);
    expect(shell.overviews()).toBeGreaterThan(0);
    expect(shell.overviews()).toBeLessThanOrEqual(2 * FIRST_SCREEN.length);
    for (const number of ALL.slice(20)) expect(shell.overviews(number)).toBe(0);
    for (const batch of shell.statsBatches()) {
      expect(batch.every((number) => number <= 20)).toBe(true);
    }
  });

  it("observes only its read targets, and a row scrolled away keeps showing what it read", async () => {
    installShell({});
    const view = mountList();
    await advance(10_000);
    const cache = view.client.getQueryCache();
    const observed = () =>
      cache
        .findAll({ queryKey: ["github-info", SCOPE] })
        .filter((query) => query.getObserversCount() > 0);
    // The row's read under its read scope, not the bare key it had before the shell answered.
    const rowQuery = (number: number) =>
      cache
        .findAll({ queryKey: ["github-info", SCOPE, urlOf(number)] })
        .find((query) => query.queryKey.length > 3);
    expect(observed().length).toBeGreaterThan(0);
    expect(observed().length).toBeLessThanOrEqual(FIRST_SCREEN.length);
    const first = () => view.result.current.entries.find((entry) => entry.association.number === 1);
    expect(first()?.status).toBe("ready");

    view.rerender({ numbers: ALL, visible: ALL.slice(200, 220), exceptions: [] });
    await advance(10_000);
    expect(observed().length).toBeLessThanOrEqual(FIRST_SCREEN.length);
    expect(rowQuery(1)?.getObserversCount()).toBe(0);
    expect(first()?.status).toBe("ready");
    expect(first()?.pr?.title).toBe("Followed");
    // A change to its cached answer, as a write's own update, still shows.
    act(() => {
      view.client.setQueryData(rowQuery(1)!.queryKey, {
        ...overview(1),
        pr: { ...overview(1).pr, title: "Renamed" },
      });
    });
    await advance(1_000);
    expect(first()?.pr?.title).toBe("Renamed");
  });

  it("reads each row's overview once, following its first batch rather than reading again", async () => {
    const shell = installShell({});
    const view = mountList();
    await advance(5_000);
    view.rerender({ numbers: ALL, visible: ALL.slice(20, 40), exceptions: [] });
    await advance(10_000);
    const read = ALL.filter((number) => shell.overviews(number) > 0);
    expect(read.length).toBeGreaterThanOrEqual(30);
    expect(read.every((number) => shell.overviews(number) === 1)).toBe(true);
    expect(shell.overviews()).toBeLessThanOrEqual(40);
    // A quiet minute reads the rows still waiting their turn, and none of the others again.
    await advance(60_000);
    expect(ALL.slice(0, 40).every((number) => shell.overviews(number) === 1)).toBe(true);
    expect(shell.overviews()).toBe(40);
  });

  it("shows a row it hasn't read as waiting, not as a failed read", async () => {
    installShell({});
    const { result } = mountList();
    await advance(10_000);
    const unread = result.current.entries.filter((entry) => entry.association.number > 20);
    expect(unread).toHaveLength(400);
    expect(unread.every((entry) => entry.status === "loading")).toBe(true);
  });

  it("reads nothing for rows a fast scroll passes, and only the screen it stops on", async () => {
    const shell = installShell({});
    const view = mountList();
    await advance(5_000);
    const before = shell.overviews();
    // 300 rows pass, a screen every 100 ms, then it stops at rows 301 to 320.
    for (let step = 1; step <= 15; step++) {
      view.rerender({
        numbers: ALL,
        visible: ALL.slice(step * 20, step * 20 + 20),
        exceptions: [],
      });
      // eslint-disable-next-line no-await-in-loop -- one screen at a time.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
    }
    await advance(10_000);
    const read = ALL.filter((number) => shell.overviews(number) > 0 && number > 20);
    expect(read.every((number) => number > 300 && number <= 320)).toBe(true);
    expect(shell.overviews() - before).toBeLessThanOrEqual(2 * 20);
  });

  it("reads the few rows the user works with wherever they are, and no more", async () => {
    const shell = installShell({});
    const view = mountList();
    view.rerender({ numbers: ALL, visible: FIRST_SCREEN, exceptions: [400, 401, 402, 403, 404] });
    await advance(10_000);
    expect([400, 401, 402].every((number) => shell.overviews(number) > 0)).toBe(true);
    expect([403, 404].every((number) => shell.overviews(number) === 0)).toBe(true);
  });

  it("starts no new reads while the reader's budget is spent, and catches up once it isn't", async () => {
    const shell = installShell({});
    const view = mountList();
    await advance(10_000);
    githubReadBudgets.observe(githubCooldownKey("github.com", "mac-octo"), {
      resource: "graphql",
      limit: 5000,
      remaining: 100,
      resetAt: Date.now() + 2 * 60_000,
    });
    const overviews = shell.overviews();
    const batches = shell.batches();
    view.rerender({ numbers: ALL, visible: ALL.slice(200, 220), exceptions: [] });
    await advance(60_000);
    expect(shell.overviews()).toBe(overviews);
    expect(shell.batches()).toBe(batches);
    await advance(2 * 60_000);
    expect(ALL.slice(200, 220).some((number) => shell.overviews(number) > 0)).toBe(true);
    githubReadBudgets.clear();
  });
});
