import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import {
  childSessionsQueryKey,
  childSessionsQueryOptions,
  fetchChildSessions,
  patchChildSessionsCache,
  type ChildSessionInfo,
  useChildSessions,
  useDescendantSession,
} from "./useChildSessions";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

function wrapper({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {children}
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
});

function deferredResponse() {
  let resolve!: (response: Response) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Response>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function childPage(data: Partial<ChildSessionInfo>[], hasMore = false): Response {
  return new Response(JSON.stringify({ data, has_more: hasMore, last_id: data.at(-1)?.id }));
}

describe("child-session fetch ownership", () => {
  it.each([false, true])(
    "replays only concurrent SSE fields across pages (refetch=%s)",
    async (refetch) => {
      const client = new QueryClient();
      const options = childSessionsQueryOptions("parent");
      const send = (id: string, patch: Partial<ChildSessionInfo>) =>
        patchChildSessionsCache(client, "parent", id, patch);
      if (refetch)
        send("known", {
          title: "stale",
          tool: "stale",
          own_cost_usd: 2,
          busy: true,
          last_message_preview: "stale",
        });
      const second = deferredResponse();
      const fetch = vi.mocked(authenticatedFetch);
      fetch
        .mockResolvedValueOnce(
          childPage(
            [
              {
                id: "known",
                title: "fetched",
                tool: "fetched",
                own_cost_usd: 1,
                busy: false,
                current_task_status: "queued",
                last_message_preview: "fetched preview",
                last_task_error: { code: "failed", message: "old failure" },
              },
            ],
            true,
          ),
        )
        .mockReturnValueOnce(second.promise);
      const pending = client.fetchQuery(options);
      await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
      // Equal receipts still outrank an older HTTP value; later fields coalesce independently.
      send("known", {
        own_cost_usd: 2,
        busy: true,
        current_task_status: "in_progress",
        last_task_error: null,
      });
      send("known", { busy: false, current_task_status: "completed" });
      send("inserted", { own_cost_usd: 0 });
      send("overlap", { own_cost_usd: 7, last_message_preview: null });
      second.resolve(
        childPage([
          {
            id: "overlap",
            title: "HTTP fills omitted fields",
            own_cost_usd: 1,
            last_message_preview: "old",
          },
        ]),
      );
      await pending;
      const rows = client.getQueryData<ChildSessionInfo[]>(options.queryKey)!;
      expect(rows.find((row) => row.id === "known")).toMatchObject({
        title: "fetched",
        tool: "fetched",
        own_cost_usd: 2,
        busy: false,
        current_task_status: "completed",
        last_message_preview: "fetched preview",
        last_task_error: null,
      });
      expect(rows.find((row) => row.id === "inserted")).toMatchObject({ own_cost_usd: 0 });
      expect(rows.find((row) => row.id === "overlap")).toMatchObject({
        title: "HTTP fills omitted fields",
        own_cost_usd: 7,
        last_message_preview: null,
      });
      fetch.mockResolvedValueOnce(childPage([{ id: "known", title: "clean", own_cost_usd: 0 }]));
      await client.fetchQuery({ ...options, staleTime: 0 });
      expect(client.getQueryData(options.queryKey)).toMatchObject([
        { id: "known", title: "clean", own_cost_usd: 0, last_task_error: null },
      ]);
    },
  );

  it("keeps recording after queryFn resolves until the cache commit", async () => {
    const client = new QueryClient();
    const options = childSessionsQueryOptions("parent");
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      childPage([{ id: "known", own_cost_usd: 1 }]),
    );
    if (typeof options.queryFn !== "function" || typeof options.structuralSharing !== "function")
      throw new Error("Missing shared query callbacks");
    const incoming = await options.queryFn({
      client,
      queryKey: options.queryKey,
      signal: new AbortController().signal,
      meta: undefined,
    });
    patchChildSessionsCache(client, "parent", "known", { own_cost_usd: null });
    const committed = options.structuralSharing(client.getQueryData(options.queryKey), incoming);
    expect(committed).toMatchObject([{ id: "known", own_cost_usd: null }]);
  });

  it.each(["cancelQueries", "unmount"])(
    "preserves live rows immediately after default %s cancellation and late page completion",
    async (cancellation) => {
      const client = new QueryClient();
      const key = childSessionsQueryKey("parent");
      patchChildSessionsCache(client, "parent", "known", {
        own_cost_usd: 1,
        current_task_status: "failed",
        last_task_error: { code: "failed", message: "old failure" },
      });
      const second = deferredResponse();
      const fetch = vi.mocked(authenticatedFetch);
      fetch
        .mockResolvedValueOnce(childPage([{ id: "incomplete-http-page" }], true))
        .mockReturnValueOnce(second.promise);
      const { unmount } = renderHook(() => useChildSessions("parent"), {
        wrapper: ({ children }) => (
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        ),
      });
      await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
      patchChildSessionsCache(client, "parent", "known", {
        own_cost_usd: 2,
        current_task_status: "completed",
        last_task_error: null,
      });
      patchChildSessionsCache(client, "parent", "inserted", { own_cost_usd: 0 });
      const liveRows = client.getQueryData(key);
      expect(liveRows).toMatchObject([
        { id: "inserted", own_cost_usd: 0 },
        { id: "known", own_cost_usd: 2, current_task_status: "completed", last_task_error: null },
      ]);

      if (cancellation === "cancelQueries")
        await client.cancelQueries({ queryKey: key, exact: true });
      else unmount();
      expect(client.getQueryData(key)).toEqual(liveRows);
      expect(client.getQueryState(key)?.fetchStatus).toBe("idle");
      expect(fetch.mock.calls[1][1]?.signal?.aborted).toBe(true);

      second.resolve(childPage([{ id: "stale-late-page" }], true));
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
      expect(client.getQueryData(key)).toEqual(liveRows);
      expect(fetch).toHaveBeenCalledTimes(2);
    },
  );

  it.each(["cancel", "refetch"])(
    "retires only its own request on %s when a late page settles",
    async (replacement) => {
      const client = new QueryClient();
      const options = childSessionsQueryOptions("parent");
      const oldSecond = deferredResponse();
      const newSecond = deferredResponse();
      const fetch = vi.mocked(authenticatedFetch);
      fetch
        .mockResolvedValueOnce(childPage([{ id: "old-page" }], true))
        .mockReturnValueOnce(oldSecond.promise)
        .mockResolvedValueOnce(
          childPage([{ id: "known", title: "new snapshot", own_cost_usd: 1 }], true),
        )
        .mockReturnValueOnce(newSecond.promise);
      const oldRequest = client.fetchQuery(options).catch(() => undefined);
      await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
      patchChildSessionsCache(client, "parent", "known", { own_cost_usd: 2 });
      if (replacement === "cancel")
        await client.cancelQueries({ queryKey: options.queryKey, exact: true });
      const newRequest =
        replacement === "cancel"
          ? client.fetchQuery({ ...options, staleTime: 0 })
          : client.refetchQueries({ queryKey: options.queryKey, exact: true });
      await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
      expect(fetch.mock.calls[1][1]?.signal?.aborted).toBe(true);
      oldSecond.resolve(childPage([{ id: "stale-late-page" }]));
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
      patchChildSessionsCache(client, "parent", "known", { own_cost_usd: 3 });
      newSecond.resolve(childPage([]));
      await newRequest;
      await oldRequest;
      expect(client.getQueryData(options.queryKey)).toMatchObject([
        { id: "known", title: "new snapshot", own_cost_usd: 3 },
      ]);
    },
  );

  it("keeps live data when page two fails, then starts a clean journal", async () => {
    const client = new QueryClient();
    const options = childSessionsQueryOptions("parent");
    const second = deferredResponse();
    const fetch = vi.mocked(authenticatedFetch);
    fetch
      .mockResolvedValueOnce(childPage([{ id: "incomplete-http-page" }], true))
      .mockReturnValueOnce(second.promise);
    const pending = client.fetchQuery(options);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    patchChildSessionsCache(client, "parent", "live", { own_cost_usd: 2 });
    const rejected = expect(pending).rejects.toThrow("page two failed");
    second.reject(new Error("page two failed"));
    await rejected;
    expect(client.getQueryData(options.queryKey)).toMatchObject([{ id: "live", own_cost_usd: 2 }]);
    fetch.mockResolvedValueOnce(childPage([{ id: "live", own_cost_usd: 0 }]));
    await client.fetchQuery(options);
    expect(client.getQueryData(options.queryKey)).toMatchObject([{ id: "live", own_cost_usd: 0 }]);
  });
});

describe("useChildSessions", () => {
  it("does not fetch child sessions for a provisional conversation", () => {
    const { result } = renderHook(() => useChildSessions("temp:pending-create"), { wrapper });

    expect(result.current.children).toEqual([]);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("useDescendantSession", () => {
  it("fetches the direct-child list when it is not cached", async () => {
    // WHY: a mirrored card can mount before anything loaded the ancestor's
    // child list; the lookup must fetch it rather than stay unlabeled.
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            {
              id: "conv_child",
              title: "researcher:auth",
              task_summary: "Investigate auth flow",
              tool: "researcher",
              session_name: "auth",
              current_task_status: "in_progress",
              busy: true,
            },
          ],
        }),
      ),
    );
    const { result } = renderHook(() => useDescendantSession("conv_parent", "conv_child"), {
      wrapper,
    });

    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current?.task_summary).toBe("Investigate auth flow"));
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/conv_parent/child_sessions?limit=100",
      { signal: expect.any(AbortSignal) },
    );
  });
});

it("loads every bounded page and rejects a repeated cursor", async () => {
  const fetch = vi.mocked(authenticatedFetch);
  const page = (id: string, more: boolean) =>
    new Response(JSON.stringify({ data: [{ id, own_cost_usd: 0 }], has_more: more, last_id: id }));
  fetch.mockResolvedValueOnce(page("first", true)).mockResolvedValueOnce(page("last", false));
  const signal = new AbortController().signal;
  expect(
    (await fetchChildSessions("parent", signal)).map((child) => [child.id, child.own_cost_usd]),
  ).toEqual([
    ["first", 0],
    ["last", 0],
  ]);
  expect(fetch).toHaveBeenLastCalledWith(
    "/v1/sessions/parent/child_sessions?limit=100&after=first",
    { signal },
  );
  fetch.mockResolvedValueOnce(page("first", true)).mockResolvedValueOnce(page("first", true));
  await expect(fetchChildSessions("parent")).rejects.toThrow("Repeated child-session cursor");
  fetch.mockResolvedValueOnce(new Response(JSON.stringify({ data: [], has_more: true })));
  await expect(fetchChildSessions("parent")).rejects.toThrow("Missing child-session cursor");
});

it("fetches the complete list when SSE seeded only a partial cold cache", async () => {
  const client = new QueryClient();
  client.setQueryData(childSessionsQueryKey("parent"), [{ id: "partial" }], { updatedAt: 0 });
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(
    new Response(JSON.stringify({ data: [{ id: "first" }, { id: "second" }] })),
  );
  const { result } = renderHook(() => useChildSessions("parent"), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  await waitFor(() =>
    expect(result.current.children.map((child) => child.id)).toEqual(["first", "second"]),
  );
});
