import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type * as Sonner from "sonner";
import type { PullRequestInfo } from "./usePullRequests";
import {
  fetchSessionPullRequests,
  sessionPullRequestsKey,
  startSessionUnlinkRun,
  startTreeUnlinkRun,
  stateAsOf,
  unlinkRunSummary,
  useSessionPullRequests,
  useSessionUnlinkPending,
  useUnlinkSessionPr,
} from "./useSessionPullRequests";
import { runningBulkRuns, stopBulkRun } from "@/pullRequestList/bulkRuns";
import { resetPullRequestIntentsForTests } from "@/store/sessionPullRequestIntents";

import { SessionFreePullRequestScopeContext } from "../../hooks/sessionFreePullRequestScope";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

// What each bulk run's toast said as it finished.
const toasts = vi.hoisted(() => [] as { title: string; detail: unknown; needsLook: boolean }[]);
vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof Sonner>();
  const toast = Object.assign((title: string, options?: Parameters<typeof actual.toast>[1]) => {
    toasts.push({
      title,
      detail: options?.description ?? null,
      needsLook: options?.duration === Infinity,
    });
    return actual.toast(title, options);
  }, actual.toast);
  return { ...actual, toast };
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetPullRequestIntentsForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

const one = "https://github.com/example/one/pull/42";
const two = {
  url: "https://github.com/example/two/pull/7",
  host: "github.com",
  repository: "example/two",
  number: 7,
  relationship: "attached",
};

function page(data: unknown[], overrides: Record<string, unknown> = {}): Response {
  return new Response(
    JSON.stringify({
      object: "list",
      data,
      last_id: null,
      has_more: false,
      synced: true,
      ...overrides,
    }),
    { status: 200 },
  );
}

function wrapper(client: QueryClient, info: ServerInfo | "loading") {
  return ({ children }: { children: ReactNode }) => (
    <CapabilitiesProvider info={info}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </CapabilitiesProvider>
  );
}

const serverInfo = (stored: boolean): ServerInfo => ({
  ...FALLBACK_SERVER_INFO,
  session_pull_requests: stored,
});

it.each([
  {
    server: "current",
    stored: true,
    request: "DELETE /v1/sessions/conv_1/pull-requests",
    listed: { prs: [two], synced: true },
  },
  {
    server: "older",
    stored: false,
    request: "POST /v1/sessions/conv_1/resources/github/prs",
    listed: undefined,
  },
])("unlinks on the $server server and lists only what it stores", async (scenario) => {
  const requests: string[] = [];
  let unlinked = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new URL(String(input), "http://localhost");
      if (!init?.method) return page(unlinked ? [two] : [{ ...two, url: one, number: 42 }, two]);
      requests.push(`${init.method} ${request.pathname}`);
      unlinked = true;
      if (init.method === "DELETE") {
        expect(request.searchParams.get("url")).toBe(one);
        // Only the list's own read writes it, so the reply's list is never shown.
        return page([two, { ...two, url: "https://github.com/example/stale/pull/9" }]);
      }
      expect(JSON.parse(String(init.body))).toEqual({ url: one, action: "remove" });
      return new Response(
        JSON.stringify({ object: "session.github.info", available: true, prs: [two] }),
        { status: 200 },
      );
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(
    () => ({ list: useSessionPullRequests("conv_1"), unlink: useUnlinkSessionPr("conv_1") }),
    { wrapper: wrapper(client, serverInfo(scenario.stored)) },
  );

  result.current.unlink.mutate(one);

  await waitFor(() => expect(result.current.unlink.isSuccess).toBe(true));
  // An older server stores no list, so its consumers keep following live info.
  expect(result.current.list.data).toEqual(scenario.listed);
  expect(requests).toEqual([scenario.request]);
});

it("reads the whole list again when an unlink answers with only its first page", async () => {
  const first = { ...two, url: one, number: 42 };
  const third = { ...two, url: "https://github.com/example/two/pull/8", number: 8 };
  let unlinked = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        unlinked = true;
        return page([two], { last_id: two.url, has_more: true });
      }
      if (!unlinked) return page([first, two, third]);
      const after = new URL(String(input), "http://localhost").searchParams.get("after");
      return after ? page([third]) : page([two], { last_id: two.url, has_more: true });
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(
    () => ({ list: useSessionPullRequests("conv_1"), unlink: useUnlinkSessionPr("conv_1") }),
    { wrapper: wrapper(client, serverInfo(true)) },
  );
  await waitFor(() => expect(result.current.list.data?.prs).toEqual([first, two, third]));

  result.current.unlink.mutate(one);

  await waitFor(() => expect(result.current.list.data?.prs).toEqual([two, third]));
});

it("keeps an unlink to its own session when the panel switches sessions", async () => {
  let answer: (response: Response) => void = () => {};
  const fetchMock = vi.fn(
    () =>
      new Promise<Response>((resolve) => {
        answer = resolve;
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient();
  const other = { prs: [{ ...two, url: "https://github.com/example/three/pull/3" }], synced: true };
  client.setQueryData(sessionPullRequestsKey("conv_2"), other);
  const { result, rerender } = renderHook(({ id }) => useUnlinkSessionPr(id), {
    initialProps: { id: "conv_1" },
    wrapper: wrapper(client, serverInfo(true)),
  });
  result.current.mutate(one);
  await waitFor(() => expect(result.current.isPending).toBe(true));

  rerender({ id: "conv_2" });
  // The next session shows neither the unlink nor, once it answers, its outcome.
  expect(result.current.isIdle).toBe(true);
  answer(page([two]));
  // The unlink's session reads its list again; that read, not the reply, shows the outcome.
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  answer(page([two]));

  await waitFor(() =>
    expect(client.getQueryData(sessionPullRequestsKey("conv_1"))).toEqual({
      prs: [two],
      synced: true,
    }),
  );
  expect(client.getQueryData(sessionPullRequestsKey("conv_2"))).toEqual(other);
  expect(result.current.isIdle).toBe(true);
});

it("unlinks a batch in its own session, three at a time, outside any view", async () => {
  const three = "https://github.com/example/three/pull/3";
  const four = "https://github.com/example/four/pull/4";
  const requests: string[] = [];
  const answers: ((response: Response) => void)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = new URL(String(input), "http://localhost");
      requests.push(`${init?.method} ${request.pathname} ${request.searchParams.get("url")}`);
      return new Promise<Response>((resolve) => {
        answers.push(resolve);
      });
    }),
  );
  const client = new QueryClient();
  const linked = [{ ...two, url: one }, two, { ...two, url: three }, { ...two, url: four }];
  client.setQueryData(sessionPullRequestsKey("conv_1"), { prs: linked, synced: true });
  // The same PR is linked in another session; that link stays.
  const otherStored = { prs: [{ ...two, url: three }], synced: true };
  client.setQueryData(sessionPullRequestsKey("conv_2"), otherStored);

  const remaining = startSessionUnlinkRun(client, serverInfo(true), "conv_1", [one, three, four]);
  await waitFor(() => expect(requests).toHaveLength(3));
  // Each answer's own list is stale beside the others: the run never caches one.
  answers[1]!(page([{ ...two, url: one }, two, { ...two, url: four }]));
  answers[0]!(page([two, { ...two, url: three }, { ...two, url: four }]));
  answers[2]!(page([two, { ...two, url: three }]));
  expect(client.getQueryData(sessionPullRequestsKey("conv_1"))).toEqual({
    prs: linked,
    synced: true,
  });
  // One read of the session's list settles the run.
  await waitFor(() => expect(requests).toHaveLength(4));
  expect(requests[3]).toBe("undefined /v1/sessions/conv_1/pull-requests null");
  answers[3]!(page([two]));

  await expect(remaining).resolves.toEqual([two]);
  expect(client.getQueryData(sessionPullRequestsKey("conv_1"))).toEqual({
    prs: [two],
    synced: true,
  });
  expect(requests.every((request) => request.includes("/conv_1/"))).toBe(true);
  expect(client.getQueryData(sessionPullRequestsKey("conv_2"))).toBe(otherStored);
});

it("sends no more of a batch once it's stopped, and lets those out settle", async () => {
  const urls = [1, 2, 3, 4, 5].map((number) => `https://github.com/example/r/pull/${number}`);
  const answers: ((response: Response) => void)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      // The run's read of the list once it ends answers at once.
      init?.method === "DELETE"
        ? new Promise<Response>((resolve) => {
            answers.push(resolve);
          })
        : page([]),
    ),
  );
  const client = new QueryClient();
  const remaining = startSessionUnlinkRun(client, serverInfo(true), "conv_1", urls);
  await waitFor(() => expect(answers).toHaveLength(3));
  stopBulkRun(runningBulkRuns().at(-1)!.id);
  for (const answer of answers) answer(page([]));
  await remaining;
  expect(answers).toHaveLength(3);
});

it("queues a batch in the same session until an unlink before it answers", async () => {
  const three = "https://github.com/example/three/pull/3";
  const requests: (string | null)[] = [];
  const answers: ((response: Response) => void)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      requests.push(new URL(String(input), "http://localhost").searchParams.get("url"));
      return new Promise<Response>((resolve) => {
        answers.push(resolve);
      });
    }),
  );
  const client = new QueryClient();
  const { result } = renderHook(() => useUnlinkSessionPr("conv_1"), {
    wrapper: wrapper(client, serverInfo(true)),
  });
  result.current.mutate(one);
  const batch = startSessionUnlinkRun(client, serverInfo(true), "conv_1", [three, two.url]);
  await waitFor(() => expect(requests).toEqual([one]));
  // A stale first answer can't land after the batch, so the list ends as the batch left it.
  await new Promise((resolve) => {
    setTimeout(resolve, 20);
  });
  expect(requests).toEqual([one]);
  answers[0]!(page([two, { ...two, url: three }]));
  // The first unlink's read of the list (no `url`) ends it before the batch starts.
  await waitFor(() => expect(requests).toEqual([one, null]));
  answers[1]!(page([two, { ...two, url: three }]));
  await waitFor(() => expect(requests).toEqual([one, null, three, two.url]));
  answers[2]!(page([two]));
  answers[3]!(page([{ ...two, url: three }]));
  await waitFor(() => expect(requests).toEqual([one, null, three, two.url, null]));
  answers[4]!(page([]));
  await batch;
  expect(client.getQueryData(sessionPullRequestsKey("conv_1"))).toEqual({
    prs: [],
    synced: true,
  });
});

it("unlinks every one it can in a batch, despite a failure, and rereads once", async () => {
  const three = "https://github.com/example/three/pull/3";
  const deletes: string[] = [];
  let reads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost").searchParams.get("url");
      if (init?.method !== "DELETE") {
        reads += 1;
        return page(reads === 1 ? [{ ...two, url: one }, two, { ...two, url: three }] : [two]);
      }
      deletes.push(url!);
      return url === two.url
        ? new Response(JSON.stringify({ error: { message: "Server unavailable" } }), {
            status: 403,
          })
        : page([]);
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(() => useSessionPullRequests("conv_1"), {
    wrapper: wrapper(client, serverInfo(true)),
  });
  await waitFor(() => expect(result.current.data?.prs).toHaveLength(3));

  await startSessionUnlinkRun(client, serverInfo(true), "conv_1", [one, two.url, three]);

  expect(deletes.sort()).toEqual([one, two.url, three].sort());
  // One reread settles the list, the PR it couldn't unlink included.
  await waitFor(() => expect(result.current.data?.prs).toEqual([two]));
  expect(reads).toBe(2);
});

it("keeps an unlink when a read that started before it answers afterwards", async () => {
  const answerRead: ((response: Response) => void)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "DELETE"
        ? page([two])
        : new Promise<Response>((resolve) => {
            answerRead.push(resolve);
          }),
    ),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(
    () => ({ list: useSessionPullRequests("conv_1"), unlink: useUnlinkSessionPr("conv_1") }),
    { wrapper: wrapper(client, serverInfo(true)) },
  );
  await waitFor(() => expect(result.current.list.isFetching).toBe(true));

  result.current.unlink.mutate(one);
  // The unlink reads the list again, after its answer; that read replaces the older one.
  await waitFor(() => expect(answerRead).toHaveLength(2));
  answerRead[1]!(page([two]));
  await waitFor(() => expect(result.current.unlink.isSuccess).toBe(true));
  answerRead[0]!(page([{ ...two, url: one, number: 42 }, two]));
  await new Promise<void>((resolve) => {
    setTimeout(resolve, 10);
  });

  await waitFor(() => expect(result.current.list.isFetching).toBe(false));
  expect(result.current.list.data?.prs).toEqual([two]);
});

it("refetches when a live read lists different PRs, and only then", async () => {
  const livePayload = (prs: unknown[]) =>
    ({ tracking_available: true, prs }) as Pick<PullRequestInfo, "tracking_available" | "prs">;
  const fetchMock = vi.fn(async () => page([two]));
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result, rerender } = renderHook(({ live }) => useSessionPullRequests("conv_1", live), {
    wrapper: wrapper(client, serverInfo(true)),
    initialProps: { live: livePayload([two]) },
  });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));

  rerender({ live: livePayload([{ ...two }]) });
  await new Promise<void>((resolve) => {
    setTimeout(resolve, 10);
  });
  expect(fetchMock).toHaveBeenCalledTimes(1);

  rerender({ live: livePayload([{ ...two, url: one }, two]) });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
});

it("refuses to unlink before the server's capabilities load", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient();
  const { result } = renderHook(() => useUnlinkSessionPr("conv_1"), {
    wrapper: wrapper(client, "loading"),
  });

  result.current.mutate(one);

  await waitFor(() => expect(result.current.isError).toBe(true));
  expect(fetchMock).not.toHaveBeenCalled();
});

it("reads every page of a current server's list and nothing from an older one", async () => {
  const first = { ...two, url: one, number: 42 };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const after = new URL(String(input), "http://localhost").searchParams.get("after");
    return after ? page([two], { synced: false }) : page([first], { last_id: one, has_more: true });
  });
  vi.stubGlobal("fetch", fetchMock);

  const older = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderHook(() => useSessionPullRequests("conv_1"), {
    wrapper: wrapper(older, serverInfo(false)),
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(() => useSessionPullRequests("conv_1"), {
    wrapper: wrapper(client, serverInfo(true)),
  });

  await waitFor(() => expect(result.current.data).toEqual({ prs: [first, two], synced: false }));
  expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
    "/v1/sessions/conv_1/pull-requests?limit=100",
    `/v1/sessions/conv_1/pull-requests?limit=100&after=${encodeURIComponent(one)}`,
  ]);
});

const SESSION_FREE_SCOPE = { id: "pull-requests-page" };

it("lists no links for the ID a session-free pull request view reads under", async () => {
  const fetchMock = vi.fn(async () => page([two]));
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const Stored = wrapper(client, serverInfo(true));
  const { result } = renderHook(() => useSessionPullRequests("pull-requests-page"), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <Stored>
        <SessionFreePullRequestScopeContext.Provider value={SESSION_FREE_SCOPE}>
          {children}
        </SessionFreePullRequestScopeContext.Provider>
      </Stored>
    ),
  });
  await new Promise((resolve) => {
    setTimeout(resolve, 20);
  });
  expect(result.current.fetchStatus).toBe("idle");
  expect(fetchMock).not.toHaveBeenCalled();
});

it.each([
  { cursor: "missing", last_id: null },
  { cursor: "repeated", last_id: two.url },
])("stops at a $cursor cursor instead of looping", async ({ last_id }) => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => page([two], { has_more: true, last_id })),
  );
  await expect(fetchSessionPullRequests("conv_1")).rejects.toThrow("invalid page");
});

it("labels a stored state with its age only once it's stale", () => {
  const now = Date.UTC(2026, 9, 27, 12);
  expect(stateAsOf(null, now)).toBeNull();
  expect(stateAsOf(now / 1000 - 60, now)).toBeNull();
  expect(stateAsOf(now / 1000 - 2 * 3600, now)).toBe("2h ago");
});

const AUTH = { id: "conv_auth", name: "auth-fix" };
const DOCS = { id: "conv_docs", name: "docs" };

/** A removal of `url` from each of `sessions`, as the list plans it. */
function removalFrom(url: string, sessions: string[], kept: { id: string; name: string }[] = []) {
  const named = [AUTH, DOCS];
  return {
    url,
    targets: sessions.map((sessionId) => ({ sessionId, url })),
    fromSubAgents: named.filter((session) => sessions.includes(session.id)),
    kept,
    ownershipPending: false,
  };
}

/** A server answering each unlink, `refuse` naming the sessions whose unlinks it refuses. */
function unlinkServer({ refuse = [] }: { refuse?: string[] } = {}) {
  const deletes: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new URL(String(input), "http://localhost");
      const session = request.pathname.split("/")[3]!;
      if (init?.method !== "DELETE") return page([]);
      deletes.push(`${session} ${request.searchParams.get("url")}`);
      return refuse.includes(session)
        ? new Response(JSON.stringify({ error: { message: "Server unavailable" } }), {
            status: 403,
          })
        : page([]);
    }),
  );
  return deletes;
}

it("removes a PR from each session in the tree that links it, as each stores it", async () => {
  toasts.length = 0;
  const deletes = unlinkServer();
  const client = new QueryClient();
  client.setQueryData(sessionPullRequestsKey("conv_1"), {
    prs: [{ ...two, url: one }],
    synced: true,
  });
  // The sub-agent stores the same PR under a differently written URL.
  client.setQueryData(sessionPullRequestsKey(AUTH.id), {
    prs: [{ ...two, url: `${one}/` }, two],
    synced: true,
  });

  const remaining = await startTreeUnlinkRun(client, serverInfo(true), "conv_1", [
    removalFrom(one, ["conv_1", AUTH.id]),
    removalFrom(two.url, [AUTH.id]),
  ]);

  expect(deletes.sort()).toEqual(
    [`conv_1 ${one}`, `${AUTH.id} ${one}/`, `${AUTH.id} ${two.url}`].sort(),
  );
  expect(remaining).toEqual([]);
  expect(client.getQueryData(sessionPullRequestsKey(AUTH.id))).toMatchObject({ prs: [] });
  expect(toasts.at(-1)).toEqual({
    title: "Removed 2 pull requests",
    detail: null,
    needsLook: false,
  });
});

it("says a PR was only partly removed when one of its sessions refused", async () => {
  toasts.length = 0;
  unlinkServer({ refuse: [DOCS.id] });
  const client = new QueryClient();

  await startTreeUnlinkRun(client, serverInfo(true), "conv_1", [
    removalFrom(one, [AUTH.id, DOCS.id]),
  ]);

  expect(toasts.at(-1)).toMatchObject({
    title: "Partly removed 1 pull request",
    needsLook: true,
  });
  expect(String(toasts.at(-1)!.detail)).toBe(
    "Still linked by docs. A link couldn’t be removed: Server unavailable",
  );
});

it("counts a PR none of whose sessions let go of it once, not once per refused link", async () => {
  toasts.length = 0;
  unlinkServer({ refuse: [AUTH.id, DOCS.id] });
  const client = new QueryClient();

  await startTreeUnlinkRun(client, serverInfo(true), "conv_1", [
    removalFrom(one, [AUTH.id, DOCS.id]),
  ]);

  expect(toasts.at(-1)).toEqual({
    title: "No pull requests were removed",
    detail: "1 pull request couldn’t be removed: Server unavailable",
    needsLook: true,
  });
});

it("sends none of a PR's other links once Stopped, though the one out was refused", async () => {
  toasts.length = 0;
  const deletes: string[] = [];
  const answers: (() => void)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      deletes.push(new URL(String(input), "http://localhost").pathname.split("/")[3]!);
      return new Promise<Response>((resolve) => {
        answers.push(() =>
          resolve(
            new Response(JSON.stringify({ error: { message: "Server unavailable" } }), {
              status: 403,
            }),
          ),
        );
      });
    }),
  );
  const client = new QueryClient();
  const run = startTreeUnlinkRun(client, serverInfo(true), "conv_1", [
    removalFrom(one, [AUTH.id, DOCS.id]),
  ]);
  await waitFor(() => expect(deletes).toEqual([AUTH.id]));
  stopBulkRun(runningBulkRuns().at(-1)!.id);
  answers[0]!();
  await run;

  expect(deletes).toEqual([AUTH.id]);
  expect(toasts.at(-1)).toEqual({
    title: "No pull requests were removed",
    detail: "1 pull request couldn’t be removed: Server unavailable",
    needsLook: true,
  });
});

it("sums up removed, partly removed, refused and unsent PRs each by PR", () => {
  const [removed, partial, refused, unsent] = [1, 2, 3, 4].map((number) =>
    removalFrom(`https://github.com/acme/app/pull/${number}`, [AUTH.id, DOCS.id]),
  );
  const rows = [removed!, partial!, refused!, unsent!];
  const summary = unlinkRunSummary(
    rows,
    new Map([
      [removed!, "removed"],
      [partial!, "partial"],
      [refused!, "not_removed"],
    ]),
    new Map([[partial!, ["docs"]]]),
    new Map([
      [partial!, ["Server unavailable"]],
      [refused!, ["Not allowed", "Not allowed"]],
    ]),
  );
  expect(summary).toEqual({
    title: "Removed 1 pull request",
    detail:
      "1 partly removed: still linked by docs. 1 pull request couldn’t be removed: Not allowed Stopped before 1 more.",
    needsLook: true,
  });
  // Every PR removed needs no look; one Stop left unsent still does.
  expect(
    unlinkRunSummary([removed!], new Map([[removed!, "removed"]]), new Map(), new Map()),
  ).toMatchObject({ needsLook: false });
  expect(unlinkRunSummary([removed!], new Map(), new Map(), new Map())).toEqual({
    title: "No pull requests were removed",
    detail: "Stopped before 1 more.",
    needsLook: true,
  });
});

it("shows a sub-agent's removal pending while its parent's run changes it, and queues its own", async () => {
  const deletes: string[] = [];
  const answers: (() => void)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== "DELETE") return Promise.resolve(page([]));
      deletes.push(new URL(String(input), "http://localhost").pathname.split("/")[3]!);
      return new Promise<Response>((resolve) => {
        answers.push(() => resolve(page([])));
      });
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(
    () => ({
      parent: useSessionUnlinkPending("conv_1"),
      child: useSessionUnlinkPending(AUTH.id),
      other: useSessionUnlinkPending("conv_other"),
      childUnlink: useUnlinkSessionPr(AUTH.id),
    }),
    { wrapper: wrapper(client, serverInfo(true)) },
  );
  const run = startTreeUnlinkRun(client, serverInfo(true), "conv_1", [removalFrom(one, [AUTH.id])]);
  await waitFor(() => expect(deletes).toEqual([AUTH.id]));
  await waitFor(() => expect(result.current).toMatchObject({ parent: true, child: true }));
  expect(result.current.other).toBe(false);

  // The sub-agent's own panel unlinks after the run, not over it.
  result.current.childUnlink.mutate(two.url);
  await new Promise((resolve) => {
    setTimeout(resolve, 20);
  });
  expect(deletes).toEqual([AUTH.id]);
  answers[0]!();
  await run;
  await waitFor(() => expect(deletes).toEqual([AUTH.id, AUTH.id]));
  answers[1]!();
  await waitFor(() => expect(result.current).toMatchObject({ parent: false, child: false }));
});

it("leaves a PR partly removed when Stop comes between its sessions", async () => {
  toasts.length = 0;
  const deletes: string[] = [];
  const answers: (() => void)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // The run's read of the list once it ends answers at once.
      if (init?.method !== "DELETE") return page([]);
      deletes.push(new URL(String(input), "http://localhost").pathname.split("/")[3]!);
      return new Promise<Response>((resolve) => {
        answers.push(() => resolve(page([])));
      });
    }),
  );
  const client = new QueryClient();
  const run = startTreeUnlinkRun(client, serverInfo(true), "conv_1", [
    removalFrom(one, [AUTH.id, DOCS.id]),
  ]);
  await waitFor(() => expect(deletes).toEqual([AUTH.id]));
  stopBulkRun(runningBulkRuns().at(-1)!.id);
  answers[0]!();
  await run;

  expect(deletes).toEqual([AUTH.id]);
  expect(toasts.at(-1)).toMatchObject({ title: "Partly removed 1 pull request", needsLook: true });
  expect(String(toasts.at(-1)!.detail)).toBe("Still linked by docs.");
});
