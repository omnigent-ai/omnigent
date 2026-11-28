// A windowed Pull Requests list reads only the rows in its viewport, once they've stayed there,
// and a few the user is working with: never a row it scrolled past or merely holds. Its stats
// read for those rows alone, and wait like its overviews while the app's own reads are held.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as CodeHostContextModule from "@/lib/codeHostContext";
import type * as IdentityModule from "@/lib/identity";
import type * as PullRequestRpcModule from "@/lib/pullRequestRpc";

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
vi.mock("@/lib/codeHostContext", async (importOriginal) => ({
  ...(await importOriginal<typeof CodeHostContextModule>()),
  useCodeHostContext: vi.fn(() => ({ data: undefined })),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));
// The session's host serves the list's stats.
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (_sessionId: string, method: string) =>
    method === "getPullRequestListStats",
}));

/** Every host read the list sends: its method and request. */
const host = vi.hoisted(() => ({
  reads: [] as { method: string; request: Record<string, unknown> }[],
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => {
  const actual = await importOriginal<typeof PullRequestRpcModule>();
  const readPullRequestRpc = async (
    _sessionId: string,
    method: string,
    call: PullRequestRpcModule.PullRequestRpc["call"],
  ) => {
    let request: Record<string, unknown> = {};
    const client = new Proxy(
      {},
      {
        get: () => (sent: Record<string, unknown>) => {
          request = sent;
          return Promise.resolve({ payload: {} });
        },
      },
    );
    await call(client as Parameters<typeof call>[0]);
    host.reads.push({ method, request });
    return hostAnswer(method, request);
  };
  return { ...actual, readPullRequestRpc };
});

import { SidebarDataProvider } from "@/hooks/useSidebarData";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";

import { usePullRequestListEntries } from "./usePullRequestListEntries";

const SESSION = "conv-1";
const urlOf = (number: number) => `https://github.com/acme/widgets/pull/${number}`;
const numberOf = (url: unknown) => Number(String(url).split("/").at(-1));
const HEAD = "a".repeat(40);

function info(number: number) {
  return {
    object: "session.github.info",
    available: true,
    provider: "github",
    tracking_available: true,
    selected_pr_url: urlOf(number),
    auth: { authenticated: true, hint: null, cli: null, accounts: [], selected_account: null },
    capabilities: {},
    repo: { name_with_owner: "acme/widgets" },
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
      checks: { passing: 0, failing: 0, pending: 1, total: 1, runs: [] },
      comments: [],
      reviewers: [],
      labels: [],
    },
  };
}

function statsEntry(number: number) {
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
    ci: "pending",
    ci_head_sha: HEAD,
  };
}

function hostAnswer(method: string, request: Record<string, unknown>): unknown {
  if (method === "getPullRequest") return info(numberOf(request.prUrl));
  if (method === "getPullRequestListStats") {
    const urls = (request.prUrls as string[] | undefined) ?? [];
    return { reader: "host-octo", data: urls.map((url) => statsEntry(numberOf(url))) };
  }
  throw new Error(`No answer for ${method}`);
}

/** How many times the host read `number`'s overview, or every row's. */
const overviews = (number?: number) =>
  host.reads.filter(
    ({ method, request }) =>
      method === "getPullRequest" && (number === undefined || numberOf(request.prUrl) === number),
  ).length;

/** The PR numbers each stats batch asked for. */
const statsBatches = () =>
  host.reads
    .filter(({ method }) => method === "getPullRequestListStats")
    .map(({ request }) => ((request.prUrls as string[]) ?? []).map(numberOf));

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
      <SidebarDataProvider>{children}</SidebarDataProvider>
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
          conversationId: SESSION,
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
  host.reads = [];
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// Each case steps minutes of time over 420 rows, a second at a time.
describe("a windowed list of 420 rows", { timeout: 60_000 }, () => {
  it("reads only the rows in view, once each, and their stats in one batch", async () => {
    mountList();
    await advance(10_000);
    expect(overviews()).toBeGreaterThan(0);
    expect(overviews()).toBeLessThanOrEqual(2 * FIRST_SCREEN.length);
    for (const number of ALL.slice(20)) expect(overviews(number)).toBe(0);
    expect(statsBatches().length).toBeGreaterThan(0);
    for (const batch of statsBatches()) {
      expect(batch.every((number) => number <= 20)).toBe(true);
    }
  });

  it("observes only its read targets, and a row scrolled away keeps showing what it read", async () => {
    const view = mountList();
    await advance(10_000);
    const cache = view.client.getQueryCache();
    const rowQueries = () =>
      cache
        .findAll({ queryKey: ["pr-page-github-info"] })
        .filter((query) => query.queryKey.includes(urlOf(numberOf(query.queryKey.at(-1)))));
    const observed = () => rowQueries().filter((query) => query.getObserversCount() > 0);
    const rowQuery = (number: number) =>
      rowQueries().find((query) => query.queryKey.includes(urlOf(number)));
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
        ...info(1),
        pr: { ...info(1).pr, title: "Renamed" },
      });
    });
    await advance(1_000);
    expect(first()?.pr?.title).toBe("Renamed");
  });

  it("shows a row it hasn't read as waiting, not as a failed read", async () => {
    const { result } = mountList();
    await advance(10_000);
    const unread = result.current.entries.filter((entry) => entry.association.number > 20);
    expect(unread).toHaveLength(400);
    expect(unread.every((entry) => entry.status === "loading")).toBe(true);
  });

  it("reads nothing for rows a fast scroll passes, and only the screen it stops on", async () => {
    const view = mountList();
    await advance(5_000);
    const before = overviews();
    expect(before).toBeGreaterThan(0);
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
    const read = ALL.filter((number) => overviews(number) > 0 && number > 20);
    expect(read.length).toBeGreaterThan(0);
    expect(read.every((number) => number > 300 && number <= 320)).toBe(true);
    expect(overviews() - before).toBeLessThanOrEqual(2 * 20);
  });

  it("reads the few rows the user works with wherever they are, and no more", async () => {
    const view = mountList();
    view.rerender({ numbers: ALL, visible: FIRST_SCREEN, exceptions: [400, 401, 402, 403, 404] });
    await advance(10_000);
    expect([400, 401, 402].every((number) => overviews(number) > 0)).toBe(true);
    expect([403, 404].every((number) => overviews(number) === 0)).toBe(true);
  });

  it("reads stats for the screen it scrolls to, never for the rows it only holds", async () => {
    const view = mountList();
    await advance(10_000);
    const before = statsBatches().length;
    view.rerender({ numbers: ALL, visible: ALL.slice(200, 220), exceptions: [] });
    await advance(10_000);
    const after = statsBatches().slice(before);
    expect(after.length).toBeGreaterThan(0);
    for (const batch of after) {
      expect(batch.every((number) => number > 200 && number <= 220)).toBe(true);
    }
  });
});
