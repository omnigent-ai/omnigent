import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { PullRequestInfo } from "./usePullRequests";
import {
  fetchSessionPullRequests,
  sessionPullRequestsKey,
  stateAsOf,
  useSessionPullRequests,
  useUnlinkSessionPr,
} from "./useSessionPullRequests";

afterEach(() => vi.unstubAllGlobals());

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
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new URL(String(input), "http://localhost");
      if (!init?.method) return page([{ ...two, url: one, number: 42 }, two]);
      requests.push(`${init.method} ${request.pathname}`);
      if (init.method === "DELETE") {
        expect(request.searchParams.get("url")).toBe(one);
        return page([two]);
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
  expect(result.current.unlink.data?.prs).toEqual([two]);
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
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    ),
  );
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

  await waitFor(() =>
    expect(client.getQueryData(sessionPullRequestsKey("conv_1"))).toEqual({
      prs: [two],
      synced: true,
    }),
  );
  expect(client.getQueryData(sessionPullRequestsKey("conv_2"))).toEqual(other);
  expect(result.current.isIdle).toBe(true);
});

it("keeps an unlink when a read that started before it answers afterwards", async () => {
  let answerRead: (response: Response) => void = () => {};
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "DELETE"
        ? page([two])
        : new Promise<Response>((resolve) => {
            answerRead = resolve;
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
  await waitFor(() => expect(result.current.unlink.isSuccess).toBe(true));
  answerRead(page([{ ...two, url: one, number: 42 }, two]));
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
