import type { ReactNode } from "react";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryCache, QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/sessionsApi";
import { STALE_CURSOR_MAX_RESTARTS, useRestartOnStaleCursor } from "./staleCursor";

const staleCursor = () => new ApiError("Cursor deleted", 400, "stale_cursor");

function harness() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

function useWatchedList(queryFn: () => Promise<string>, scope = "all") {
  const queryKey = ["list", { scope }];
  useRestartOnStaleCursor(queryKey);
  return useQuery({ queryKey, queryFn });
}

async function settle(client: QueryClient) {
  await waitFor(() => expect(client.isFetching()).toBe(0));
  await new Promise((resolve) => {
    setTimeout(resolve, 20);
  });
  await waitFor(() => expect(client.isFetching()).toBe(0));
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("useRestartOnStaleCursor", () => {
  it("restarts a query whose cursor went stale and stops after the restart budget", async () => {
    const queryFn = vi.fn().mockRejectedValue(staleCursor());
    const { client, wrapper } = harness();
    const hook = renderHook(() => useWatchedList(queryFn), { wrapper });
    await waitFor(() => expect(hook.result.current.isError).toBe(true));
    await settle(client);
    expect(queryFn).toHaveBeenCalledTimes(STALE_CURSOR_MAX_RESTARTS + 1);
  });

  it("resets the restart budget once the query succeeds again", async () => {
    const queryFn = vi
      .fn()
      .mockRejectedValueOnce(staleCursor())
      .mockResolvedValueOnce("page 1")
      .mockRejectedValue(staleCursor());
    const { client, wrapper } = harness();
    const hook = renderHook(() => useWatchedList(queryFn), { wrapper });
    await waitFor(() => expect(hook.result.current.data).toBe("page 1"));
    await client.refetchQueries({ queryKey: ["list"] });
    await waitFor(() => expect(hook.result.current.isError).toBe(true));
    await settle(client);
    expect(queryFn).toHaveBeenCalledTimes(2 + STALE_CURSOR_MAX_RESTARTS + 1);
  });

  it("follows the new key when the watched list changes", async () => {
    const queryFn = vi.fn().mockResolvedValueOnce("page 1").mockRejectedValue(staleCursor());
    const { client, wrapper } = harness();
    const hook = renderHook(({ scope }) => useWatchedList(queryFn, scope), {
      wrapper,
      initialProps: { scope: "all" },
    });
    await waitFor(() => expect(hook.result.current.data).toBe("page 1"));
    hook.rerender({ scope: "mine" });
    await waitFor(() => expect(hook.result.current.isError).toBe(true));
    await settle(client);
    expect(queryFn).toHaveBeenCalledTimes(1 + STALE_CURSOR_MAX_RESTARTS + 1);
  });

  it("leaves other queries with a stale cursor alone", async () => {
    const watched = vi.fn().mockResolvedValue("page 1");
    const { client, wrapper } = harness();
    renderHook(() => useWatchedList(watched), { wrapper });
    await waitFor(() => expect(watched).toHaveBeenCalledTimes(1));
    const other = vi.fn().mockRejectedValue(staleCursor());
    await client.prefetchQuery({ queryKey: ["other"], queryFn: other });
    await settle(client);
    expect(other).toHaveBeenCalledTimes(1);
    expect(watched).toHaveBeenCalledTimes(1);
  });

  it("does not scan the cache for events about other queries", async () => {
    const { client, wrapper } = harness();
    renderHook(() => useWatchedList(() => Promise.resolve("page 1")), { wrapper });
    const find = vi.spyOn(QueryCache.prototype, "find");
    for (let i = 0; i < 20; i++) {
      client.setQueryData(["unrelated", i], i);
    }
    expect(find).not.toHaveBeenCalled();
  });
});
