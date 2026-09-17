import { QueryClient, QueryClientProvider, focusManager } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/sessionsApi";
import { sessionRowsPage, type ScopeCacheData } from "@/lib/sidebarData";
import {
  fetchConversationsPage,
  type Conversation,
  type ConversationsPage,
} from "./useConversations";
import { useScopeCache } from "./useScopeCache";
import type * as ConversationsModule from "./useConversations";
import type * as IdentityModule from "@/lib/identity";

vi.mock("./useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof ConversationsModule>()),
  fetchConversationsPage: vi.fn(),
}));
const identity = vi.hoisted(() => ({ viewerId: "alice" as string | null }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => identity.viewerId,
}));
const fetchPage = vi.mocked(fetchConversationsPage);
const key = ["conversations", "", false, null, "mine"];
const rows = (start: number, count: number, shared = false): Conversation[] =>
  Array.from({ length: count }, (_, i) => ({
    id: `s${start + i}`,
    updated_at: 10000 - start - i,
    created_at: 1,
    object: "conversation",
    title: `Session ${start + i}`,
    labels: {},
    owner: shared ? "bob" : "alice",
    permission_level: shared ? 1 : 4,
  }));
let client: QueryClient;
beforeEach(() => {
  identity.viewerId = "alice";
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  fetchPage
    .mockReset()
    .mockImplementation(async ({ after, limit = 30, visibility }) =>
      sessionRowsPage(
        rows(after ? Number(after.slice(1)) + 1 : 1, limit, visibility === "shared"),
        true,
      ),
    );
});
afterEach(() => {
  cleanup();
  client.clear();
  vi.useRealTimers();
  focusManager.setFocused(undefined);
});
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>{children}</QueryClientProvider>
);
const cached = () => client.getQueryData<ScopeCacheData>(key)!;

it.each(["mine", "shared"] as const)(
  "refreshes the complete loaded %s window in one request",
  async (scope) => {
    const { result } = renderHook(() => useScopeCache(scope, 60_000), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await act(async () => {
      await result.current.fetchNextPage();
    });
    const replacement = rows(2, 60, scope === "shared").map((row) => ({
      ...row,
      title: "Refreshed",
    }));
    fetchPage.mockClear().mockResolvedValueOnce(sessionRowsPage(replacement, true));
    await act(async () => {
      await result.current.refetch();
    });
    await waitFor(() => expect(result.current.data?.pages[0].data).toEqual(replacement));
    expect(fetchPage).toHaveBeenCalledTimes(1);
    expect(fetchPage.mock.calls[0][0]).toMatchObject({ limit: 60, visibility: scope });
    expect(fetchPage.mock.calls[0][0].after).toBeUndefined();
    await act(async () => {
      await result.current.fetchNextPage();
    });
    expect(fetchPage.mock.calls[1][0].after).toBe("s61");
  },
);

it("caps refresh at 100 while retaining older history and refreshing the entire prefix", async () => {
  const { result } = renderHook(() => useScopeCache("mine", 60_000), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  // Each page needs the cursor returned by its predecessor.
  for (let i = 0; i < 7; i++) {
    // oxlint-disable-next-line no-await-in-loop
    await act(async () => {
      await result.current.fetchNextPage();
    });
  }
  expect(cached().pages[0].data).toHaveLength(240);
  const replacement = [{ ...rows(1, 1)[0], id: "new", updated_at: 20000 }, ...rows(2, 99)];
  fetchPage.mockClear().mockResolvedValueOnce(sessionRowsPage(replacement, true));
  await act(async () => {
    await result.current.refetch();
  });
  expect(fetchPage.mock.calls[0][0].limit).toBe(100);
  expect(cached().pages[0].data.map((row) => row.id)).toEqual(
    [...replacement, ...rows(101, 140)].map((row) => row.id),
  );
  expect(cached().pages[0].last_id).toBe("s240");
});

it.each([null, 4])(
  "filters mixed scopes with permission level %s, retaining cursor and window size",
  async (permission_level) => {
    fetchPage.mockResolvedValue(
      sessionRowsPage(
        [
          ...rows(1, 1).map((row) => ({ ...row, permission_level })),
          ...rows(2, 1, true).map((row) => ({ ...row, permission_level })),
        ],
        true,
      ),
    );
    const { result } = renderHook(
      () => ({ mine: useScopeCache("mine", 60_000), shared: useScopeCache("shared", 180_000) }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.shared.isSuccess).toBe(true));
    expect(result.current.mine.data?.pages[0].data.map((row) => row.id)).toEqual(["s1"]);
    expect(result.current.shared.data?.pages[0].data.map((row) => row.id)).toEqual(["s2"]);
    expect(result.current.mine.data?.pages[0].last_id).toBe("s2");
    await act(async () => {
      await result.current.mine.fetchNextPage();
    });
    await act(async () => {
      await result.current.mine.refetch();
    });
    expect(fetchPage.mock.calls.at(-1)?.[0].limit).toBe(60);
  },
);

it("keeps polling, focus and invalidation to one request after loading more rows", async () => {
  vi.useFakeTimers();
  const { result } = renderHook(() => useScopeCache("mine", 60_000), { wrapper });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  await act(async () => {
    await result.current.fetchNextPage();
  });
  fetchPage.mockClear();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  await act(async () => {
    await client.invalidateQueries({ queryKey: key });
  });
  client.setQueryData(key, cached(), { updatedAt: Date.now() - 40_000 });
  await act(async () => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(fetchPage).toHaveBeenCalledTimes(3);
  expect(
    fetchPage.mock.calls.every(([args]) => args.after === undefined && args.limit === 60),
  ).toBe(true);
});

it("waits for pagination before refreshing the expanded window", async () => {
  const { result } = renderHook(() => useScopeCache("mine", 60_000), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  let finish!: (page: ConversationsPage) => void;
  fetchPage.mockClear().mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  let next!: Promise<unknown>;
  act(() => {
    next = result.current.fetchNextPage();
  });
  await waitFor(() => expect(finish).toBeDefined());
  let refresh!: Promise<unknown>;
  act(() => {
    refresh = result.current.refetch();
  });
  expect(fetchPage).toHaveBeenCalledTimes(1);
  await act(async () => {
    finish(sessionRowsPage(rows(31, 30), true));
    await next;
    await refresh;
  });
  expect(fetchPage.mock.calls[1][0].limit).toBe(60);
  expect(cached().pages[0].data).toHaveLength(60);
});

it("deduplicates load-more clicks and discards a pending page when disabled", async () => {
  let enabled = true;
  const { result, rerender } = renderHook(() => useScopeCache("mine", 60_000, enabled), {
    wrapper,
  });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  let finish!: (page: ConversationsPage) => void;
  fetchPage.mockClear().mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  let next!: Promise<unknown>;
  act(() => {
    next = result.current.fetchNextPage();
    void result.current.fetchNextPage();
  });
  await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(1));
  enabled = false;
  rerender();
  expect(fetchPage.mock.calls[0][0].signal?.aborted).toBe(true);
  await act(async () => {
    finish(sessionRowsPage(rows(31, 30), true));
    await next;
  });
  expect(cached().pages[0].data).toHaveLength(30);
});

it.each(["mine", "shared"] as const)(
  "queues one %s page behind refresh and uses the refreshed cursor",
  async (scope) => {
    const { result } = renderHook(() => ({ ...useScopeCache(scope, 60_000) }), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    let finish!: (page: ConversationsPage) => void;
    fetchPage.mockClear().mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    let refresh!: Promise<unknown>;
    act(() => {
      refresh = result.current.refetch();
    });
    await waitFor(() => expect(finish).toBeDefined());
    expect(result.current.isFetchingNextPage).toBe(false);
    let next!: Promise<unknown>;
    act(() => {
      next = result.current.fetchNextPage();
      void result.current.fetchNextPage();
    });
    await waitFor(() => expect(result.current.isFetchingNextPage).toBe(true));
    expect(fetchPage).toHaveBeenCalledTimes(1);
    const replacement = rows(2, 30, scope === "shared");
    await act(async () => {
      finish(sessionRowsPage(replacement, true));
      await refresh;
      await next;
    });
    expect(fetchPage).toHaveBeenCalledTimes(2);
    expect(fetchPage.mock.calls[1][0]).toMatchObject({ after: "s31", visibility: scope });
    await waitFor(() =>
      expect(result.current.data?.pages[0].data).toEqual([
        ...replacement,
        ...rows(32, 30, scope === "shared"),
      ]),
    );
  },
);

it.each(["disabled", "exhausted", "failed", "replaced"])(
  "handles a %s refresh while load more is queued",
  async (scenario) => {
    let enabled = true;
    const { result, rerender } = renderHook(() => ({ ...useScopeCache("mine", 60_000, enabled) }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    let finish!: (page: ConversationsPage) => void;
    let fail!: (error: Error) => void;
    fetchPage.mockClear().mockImplementationOnce(
      () =>
        new Promise((resolve, reject) => {
          finish = resolve;
          fail = reject;
        }),
    );
    let refresh!: Promise<unknown>;
    let next!: Promise<unknown>;
    act(() => {
      refresh = result.current.refetch();
    });
    await waitFor(() => expect(finish).toBeDefined());
    act(() => {
      next = result.current.fetchNextPage();
    });
    await waitFor(() => expect(result.current.isFetchingNextPage).toBe(true));
    if (scenario === "disabled") {
      enabled = false;
      rerender();
    }
    await act(async () => {
      if (scenario === "replaced") await result.current.refetch();
      if (scenario === "failed") fail(new Error("offline"));
      else finish(sessionRowsPage(rows(2, 30), scenario !== "exhausted"));
      await refresh;
      await next;
    });
    const shouldAppend = scenario === "failed" || scenario === "replaced";
    expect(fetchPage).toHaveBeenCalledTimes(scenario === "replaced" ? 3 : shouldAppend ? 2 : 1);
    expect(cached().pages[0].data).toHaveLength(shouldAppend ? 60 : 30);
    if (shouldAppend) expect(fetchPage.mock.calls.at(-1)?.[0].after).toBe("s30");
  },
);

it("preserves loaded rows after a failed refresh and recovers on retry", async () => {
  const { result } = renderHook(() => useScopeCache("mine", 60_000), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  await act(async () => {
    await result.current.fetchNextPage();
  });
  fetchPage.mockRejectedValueOnce(new Error("offline"));
  await act(async () => {
    await result.current.refetch();
  });
  await waitFor(() => expect(result.current.isError).toBe(true));
  expect(cached().pages[0].data).toHaveLength(60);
  await act(async () => {
    await result.current.refetch();
  });
  await waitFor(() => expect(result.current.isError).toBe(false));
});

it("recovers a stale pagination cursor by restarting the window", async () => {
  const { result } = renderHook(() => useScopeCache("mine", 60_000), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  fetchPage.mockRejectedValueOnce(new ApiError("Cursor deleted", 400, "stale_cursor"));
  await act(async () => {
    await result.current.fetchNextPage().catch(() => {});
  });
  await waitFor(() => expect(result.current.isError).toBe(false));
  expect(fetchPage.mock.calls.at(-1)?.[0].limit).toBe(30);
});

it("honors configured refresh timing and a smaller refresh cap", async () => {
  vi.useFakeTimers();
  const { result } = renderHook(() => useScopeCache("mine", 20_000, true, 45), { wrapper });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  await act(async () => {
    await result.current.fetchNextPage();
  });
  fetchPage.mockClear();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(20_000);
  });
  expect(fetchPage).toHaveBeenCalledTimes(1);
  expect(fetchPage.mock.calls[0][0].limit).toBe(45);
  expect(cached().pages[0].data).toHaveLength(60);
});

it("rechecks ownership when viewer identity resolves without refetching the rows", async () => {
  identity.viewerId = null;
  fetchPage.mockResolvedValue(
    sessionRowsPage(
      [...rows(1, 1), ...rows(2, 1, true)].map((row) => ({ ...row, permission_level: null })),
    ),
  );
  const { result, rerender } = renderHook(
    () => ({ mine: useScopeCache("mine", 60000), shared: useScopeCache("shared", 180000) }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.shared.isSuccess).toBe(true));
  identity.viewerId = "alice";
  rerender();
  expect(result.current.mine.data?.pages[0].data.map((row) => row.id)).toEqual(["s1"]);
  expect(result.current.shared.data?.pages[0].data.map((row) => row.id)).toEqual(["s2"]);
  expect(fetchPage).toHaveBeenCalledTimes(2);
});

it.each(["mine", "shared"] as const)(
  "uses configured page sizes and refresh growth for %s",
  async (scope) => {
    const { result, rerender } = renderHook(
      ({ size, cap }) => useScopeCache(scope, false, true, cap, null, size),
      { wrapper, initialProps: { size: 50, cap: 75 } },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchPage.mock.calls[0][0].limit).toBe(50);
    await act(async () => {
      await result.current.fetchNextPage();
    });
    expect(fetchPage.mock.calls[1][0]).toMatchObject({ limit: 50, after: "s50" });
    await waitFor(() => expect(result.current.data?.windowSize).toBe(100));
    await act(async () => {
      await result.current.refetch();
    });
    expect(fetchPage.mock.calls[2][0].limit).toBe(75);
    expect(result.current.data?.pages[0].data).toHaveLength(100);
    rerender({ size: 25, cap: 90 });
    await act(async () => {
      await result.current.fetchNextPage();
    });
    expect(fetchPage.mock.calls[3][0]).toMatchObject({ limit: 25, after: "s100" });
    await waitFor(() => expect(result.current.data?.windowSize).toBe(125));
    await act(async () => {
      await result.current.refetch();
    });
    expect(fetchPage.mock.calls[4][0].limit).toBe(90);
    await waitFor(() => expect(result.current.data?.pages[0].data).toHaveLength(125));
  },
);

it("applies the refresh cap only after the initial configured page", async () => {
  const { result } = renderHook(() => useScopeCache("mine", false, true, 20, null, 50), {
    wrapper,
  });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(fetchPage.mock.calls[0][0].limit).toBe(50);
  await act(async () => {
    await result.current.refetch();
  });
  expect(fetchPage.mock.calls[1][0].limit).toBe(20);
  expect(result.current.data?.windowSize).toBe(50);
});
