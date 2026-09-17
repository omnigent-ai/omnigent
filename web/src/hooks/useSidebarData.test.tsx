import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { sidebarConfig } from "@/lib/sidebarConfig";
import { PINNED_LABEL_KEY } from "@/lib/sessionListCache";
import { sessionRowsPage } from "@/lib/sidebarData";
import type { Conversation } from "./useConversations";
import { SidebarDataProvider, useLoadedConversations, useSidebarData } from "./useSidebarData";
import { useArchivedSessions } from "./useScopeCache";

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
