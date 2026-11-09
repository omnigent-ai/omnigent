import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { sidebarConfig } from "@/lib/sidebarConfig";
import { PINNED_LABEL_KEY } from "@/lib/sessionListCache";
import { sessionRowsPage, type ScopeCacheData } from "@/lib/sidebarData";
import {
  acknowledgeLocalElicitationResolution,
  beginLocalElicitationResolution,
  cancelLocalElicitationResolution,
  resetLocalElicitationResolutionsForTests,
  useLocalElicitationResolutions,
} from "@/lib/localElicitationResolutions";
import type { Conversation } from "./useConversations";
import { SidebarDataProvider, useLoadedConversations, useSidebarData } from "./useSidebarData";
import { useArchivedSessions } from "./useScopeCache";

vi.mock("./useViewerId", () => ({ useViewerId: () => null }));
vi.mock("./useSessionUpdatesConnected", () => ({ useSessionUpdatesConnected: () => true }));
const fetchMock = vi.fn();
const row = (id: string, permission_level = 4): Conversation => ({
  id,
  permission_level,
  object: "conversation",
  title: id,
  labels: {},
  created_at: 1,
  updated_at: 1,
  pending_elicitations_count: 1,
});
let client: QueryClient;
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  fetchMock.mockReset().mockImplementation((url: string) => {
    const params = new URL(url, "http://localhost").searchParams;
    const shared = params.get("visibility") === "shared";
    const pinned = params.get("pinned") === "true";
    const rows = [row(shared ? "shared" : pinned ? "old-pin" : "mine", shared ? 1 : 4)];
    if (pinned) rows[0]!.labels[PINNED_LABEL_KEY] = "1";
    return Promise.resolve({ ok: true, json: async () => sessionRowsPage(rows) });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  client.clear();
  resetLocalElicitationResolutionsForTests();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <SidebarDataProvider>{children}</SidebarDataProvider>
    </QueryClientProvider>
  );
}

it("shares the two scope requests across consumers and includes old pins once", async () => {
  const { result } = renderHook(
    () => ({
      data: useSidebarData(),
      first: useLoadedConversations(),
      second: useLoadedConversations(),
    }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.data.inboxCount).toBe(3));
  expect(fetchMock).toHaveBeenCalledTimes(4);
  const urls = fetchMock.mock.calls.map(([url]) => String(url));
  expect(
    urls.every((url) => new URL(url, "http://localhost").searchParams.get("limit") === "30"),
  ).toBe(true);
  expect(result.current.data.watchedIds.sort()).toEqual(["mine", "old-pin", "shared"]);
  expect(result.current.first.data).toBe(result.current.second.data);
  await act(async () =>
    result.current.data.registerFolder("project", [row("folder"), row("mine")]),
  );
  expect(result.current.data.inboxCount).toBe(4);
  await act(async () => result.current.data.registerFolder("project", null));
  expect(result.current.data.inboxCount).toBe(3);
});

it("excludes cached shared rows when disabled and keeps inbox policy separate", async () => {
  let enabled = true;
  const wrap = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <SidebarDataProvider
        config={{ ...sidebarConfig, sharedAvailable: enabled, inboxIncludesShared: false }}
      >
        {children}
      </SidebarDataProvider>
    </QueryClientProvider>
  );
  const { result, rerender } = renderHook(useSidebarData, { wrapper: wrap });
  act(() => result.current.registerView("all"));
  await waitFor(() => expect(result.current.watchedIds).toContain("shared"));
  expect(result.current.inboxCount).toBe(2);
  enabled = false;
  rerender();
  expect(result.current.watchedIds).not.toContain("shared");
  expect(result.current.all.data?.pages[0]?.data.map((r) => r.id)).not.toContain("shared");
});

it("keeps the archived snapshot out of polling and invalidation refetches", async () => {
  const wrap = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useArchivedSessions(true), { wrapper: wrap });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  await act(async () => client.invalidateQueries({ queryKey: ["conversations"] }));
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("refreshes mine every 60 seconds and shared every 180 seconds", async () => {
  vi.useFakeTimers();
  const { result } = renderHook(useSidebarData, { wrapper });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(result.current.inboxCount).toBe(3);
  fetchMock.mockClear();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  const scopeCalls = (scope: string) =>
    fetchMock.mock.calls.filter(([url]) => {
      const params = new URL(String(url), "http://localhost").searchParams;
      return params.get("visibility") === scope && !params.has("pinned");
    });
  expect(scopeCalls("mine")).toHaveLength(1);
  expect(scopeCalls("shared")).toHaveLength(0);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(120_000);
  });
  expect(scopeCalls("mine")).toHaveLength(3);
  expect(scopeCalls("shared")).toHaveLength(1);
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes("pinned=true"))).toBe(false);
});

const MINE_QUERY_KEY = ["conversations", "", false, null, "mine"];

function setCachedMineCount(count: number) {
  client.setQueryData<ScopeCacheData>(
    MINE_QUERY_KEY,
    (data) =>
      data && {
        ...data,
        pages: data.pages.map((page) => ({
          ...page,
          data: page.data.map((item) =>
            item.id === "mine" ? { ...item, pending_elicitations_count: count } : item,
          ),
        })),
      },
  );
}

it("subtracts an answer in flight from the derived rows until the server count drops", async () => {
  const { result } = renderHook(
    () => ({
      sidebar: useSidebarData(),
      resolutions: useLocalElicitationResolutions(),
    }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.sidebar.inboxCount).toBe(3));

  let holdToken = 0;
  act(() => {
    holdToken = beginLocalElicitationResolution("elicit_1", ["mine"], { nativeDelivery: false });
  });
  expect(result.current.sidebar.inboxCount).toBe(2);
  expect(
    result.current.sidebar.mine.data?.pages[0]?.data.find((item) => item.id === "mine")
      ?.pending_elicitations_count,
  ).toBe(0);
  // The cached page keeps the server's count; the hold lives only in the derived rows.
  expect(
    client
      .getQueryData<ScopeCacheData>(MINE_QUERY_KEY)
      ?.pages[0]?.data.find((item) => item.id === "mine")?.pending_elicitations_count,
  ).toBe(1);

  act(() => acknowledgeLocalElicitationResolution("elicit_1", holdToken));
  act(() => setCachedMineCount(0));
  await waitFor(() => expect(result.current.resolutions.answeredElicitationIds.size).toBe(0));
  expect(result.current.sidebar.inboxCount).toBe(2);
});

it.each([
  ["was pending at the answer", 2, null],
  ["arrived after the answer", 1, 2],
] as const)(
  "never shows the other prompt as answered when it %s",
  async (_case, atAnswer, arrival) => {
    const counts: number[] = [];
    const { result } = renderHook(
      () => {
        const sidebar = useSidebarData();
        const count = sidebar.loadedRows.find(
          (item) => item.id === "mine",
        )?.pending_elicitations_count;
        if (count !== undefined) counts.push(count);
        return sidebar;
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.inboxCount).toBe(3));
    act(() => setCachedMineCount(atAnswer));
    await waitFor(() => expect(counts.at(-1)).toBe(atAnswer));

    let holdToken = 0;
    act(() => {
      holdToken = beginLocalElicitationResolution("elicit_1", ["mine"], { nativeDelivery: false });
    });
    act(() => acknowledgeLocalElicitationResolution("elicit_1", holdToken));
    if (arrival !== null) {
      act(() => setCachedMineCount(arrival));
      await waitFor(() => expect(counts.at(-1)).toBe(arrival - 1));
    }
    const bothCountedAt = counts.length;
    act(() => setCachedMineCount(1));
    await waitFor(() => expect(result.current.inboxCount).toBe(3));

    expect(counts.slice(bothCountedAt)).not.toContain(0);
    expect(counts.at(-1)).toBe(1);
  },
);

it("shows the server count of a folder row loaded after its prompt was answered", async () => {
  const { result } = renderHook(useSidebarData, { wrapper });
  await waitFor(() => expect(result.current.inboxCount).toBe(3));

  act(() => {
    beginLocalElicitationResolution("elicit_1", ["folder"], { nativeDelivery: false });
  });
  // The server already removed the answered prompt; another one is still pending.
  await act(async () => result.current.registerFolder("project", [row("folder")]));

  expect(result.current.inboxCount).toBe(4);
});

it("restores the count when the POST for an answer in flight fails", async () => {
  const { result } = renderHook(useSidebarData, { wrapper });
  await waitFor(() => expect(result.current.inboxCount).toBe(3));

  let holdToken = 0;
  act(() => {
    holdToken = beginLocalElicitationResolution("elicit_1", ["mine"], { nativeDelivery: false });
  });
  expect(result.current.inboxCount).toBe(2);
  act(() => cancelLocalElicitationResolution("elicit_1", holdToken));
  expect(result.current.inboxCount).toBe(3);
});
