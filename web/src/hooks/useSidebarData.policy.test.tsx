import {
  QueryClient,
  QueryClientProvider,
  focusManager,
  onlineManager,
} from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { sidebarConfig, type SidebarConfig } from "@/lib/sidebarConfig";
import type { SessionFilter } from "@/lib/sessionFilterPreferences";
import { PINNED_LABEL_KEY } from "@/lib/sessionListCache";
import { sessionRowsPage, type ScopeCacheData } from "@/lib/sidebarData";
import type { Session } from "@/lib/types";
import {
  PINNED_CONVERSATIONS_KEY,
  useTogglePinnedConversation,
  type Conversation,
} from "./useConversations";
import { SidebarDataProvider, useSidebarData, useSidebarView } from "./useSidebarData";
import { useDirectorySessions } from "./useDirectorySessions";

vi.mock("./useSessionUpdatesConnected", () => ({ useSessionUpdatesConnected: () => true }));
const fetchMock = vi.fn();
const row = (id: string, shared = false): Conversation => ({
  id,
  object: "conversation",
  title: id,
  labels: {},
  created_at: 1,
  updated_at: 100,
  permission_level: shared ? 1 : 4,
  pending_elicitations_count: 1,
});
const sharedKey = ["conversations", "", false, null, "shared"];
let client: QueryClient;
let config: SidebarConfig;
let view: SessionFilter;
let mounted: boolean;
function View() {
  useSidebarView(view);
  return null;
}
function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <SidebarDataProvider config={config}>
        {mounted && <View />}
        {children}
      </SidebarDataProvider>
    </QueryClientProvider>
  );
}
const params = (url: string) => new URL(url, "http://localhost").searchParams;
const listCalls = (scope: string) =>
  fetchMock.mock.calls.filter(
    ([url]) => params(url).get("visibility") === scope && !params(url).has("pinned"),
  );
const response = (rows: Conversation[], more = false) => ({
  ok: true,
  json: async () => sessionRowsPage(rows, more),
});
beforeEach(() => {
  view = "mine";
  mounted = true;
  config = { ...sidebarConfig, inboxIncludesShared: false, pinsIncludeShared: false };
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  fetchMock.mockReset().mockImplementation(async (url: string) => {
    const p = params(url);
    const shared = p.get("visibility") === "shared";
    const pinned = p.has("pinned");
    const next = row(
      pinned
        ? "pin"
        : p.has("after")
          ? `${shared ? "shared" : "mine"}-older`
          : shared
            ? "shared"
            : "mine",
      shared,
    );
    if (pinned) next.labels[PINNED_LABEL_KEY] = "1";
    return response([next], !pinned && !p.has("after"));
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  client.clear();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  focusManager.setFocused(undefined);
  onlineManager.setOnline(true);
});

it("keeps managed startup, Inbox paging, focus, reconnect and all-query invalidation owned-only", async () => {
  const { result } = renderHook(useSidebarData, { wrapper });
  await waitFor(() => expect(result.current.inboxCount).toBe(2));
  expect(listCalls("shared")).toHaveLength(0);
  expect(result.current.inbox).toBe(result.current.mine);
  await act(async () => {
    await result.current.inbox.fetchNextPage();
  });
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["conversations"], refetchType: "all" });
  });
  await act(async () => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    onlineManager.setOnline(false);
    onlineManager.setOnline(true);
    await result.current.inbox.refetch?.();
    await result.current.shared.refetch();
  });
  expect(listCalls("shared")).toHaveLength(0);
  expect(
    fetchMock.mock.calls
      .filter(([url]) => params(url).has("pinned"))
      .map(([url]) => params(url).get("visibility")),
  ).toEqual(["mine"]);
  expect(result.current.inboxRows.map((r) => r.id)).toContain("mine");
});

it.each(["all", "shared"] as const)(
  "activates once for persisted %s selection and refreshes retained rows on re-entry",
  async (initial) => {
    view = initial;
    const { result, rerender } = renderHook(useSidebarData, { wrapper });
    await waitFor(() => expect(result.current.shared.isSuccess).toBe(true));
    expect(listCalls("shared")).toHaveLength(1);
    expect(result.current.all.data?.pages[0].data.map((r) => r.id)).toContain("shared");
    view = "mine";
    rerender();
    expect(result.current.sharedActive).toBe(false);
    expect(result.current.watchedIds).not.toContain("shared");
    expect(client.getQueryData<ScopeCacheData>(sharedKey)?.pages[0].data[0].id).toBe("shared");
    await act(async () => {
      await client.invalidateQueries({ queryKey: sharedKey, refetchType: "all" });
    });
    expect(listCalls("shared")).toHaveLength(1);
    view = initial;
    rerender();
    await waitFor(() => expect(listCalls("shared")).toHaveLength(2));
    await waitFor(() => expect(result.current.shared.isFetching).toBe(false));
    expect(listCalls("shared")).toHaveLength(2);
    expect(result.current.pinned.data?.conversations.map((r) => r.id)).toEqual(["pin"]);
  },
);

it("stops polling on unmount and separates polling disablement from entry and explicit loading", async () => {
  vi.useFakeTimers();
  config = { ...config, mineRefreshMs: false, sharedRefreshMs: 1000 };
  view = "all";
  const { result, rerender } = renderHook(useSidebarData, { wrapper });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(listCalls("shared")).toHaveLength(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  expect(listCalls("shared")).toHaveLength(2);
  expect(listCalls("mine")).toHaveLength(1);
  mounted = false;
  rerender();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(listCalls("shared")).toHaveLength(2);
  config = { ...config, sharedRefreshMs: false };
  mounted = true;
  rerender();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(listCalls("shared")).toHaveLength(3);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(180_000);
  });
  expect(listCalls("shared")).toHaveLength(3);
  await act(async () => {
    await result.current.shared.refetch();
  });
  expect(listCalls("shared")).toHaveLength(4);
  config = { ...config, mineRefreshMs: 2000 };
  rerender();
  const before = listCalls("mine").length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(listCalls("mine")).toHaveLength(before + 1);
});

it("cancels a Shared refresh and its queued page when leaving All", async () => {
  view = "all";
  const { result, rerender } = renderHook(useSidebarData, { wrapper });
  await waitFor(() => expect(result.current.shared.isSuccess).toBe(true));
  let finish!: (value: ReturnType<typeof response>) => void;
  const original = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation((url: string, ...args: unknown[]) =>
    params(url).get("visibility") === "shared"
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : original(url, ...args),
  );
  let refresh!: Promise<unknown>, next!: Promise<unknown>;
  act(() => {
    refresh = result.current.shared.refetch();
  });
  await waitFor(() => expect(finish).toBeDefined());
  act(() => {
    next = result.current.shared.fetchNextPage();
  });
  await waitFor(() => expect(result.current.shared.isFetchingNextPage).toBe(true));
  view = "mine";
  rerender();
  await act(async () => {
    finish(response([row("late-shared", true)], true));
    await refresh;
    await next;
  });
  expect(listCalls("shared")).toHaveLength(2);
  expect(result.current.watchedIds).not.toContain("shared");
  expect(result.current.shared.isFetchingNextPage).toBe(false);
  expect(client.getQueryData<ScopeCacheData>(sharedKey)?.pages[0].data[0].id).toBe("shared");
});

it("isolates owned Inbox rows, errors, comments and folder/pin coverage from Shared", async () => {
  view = "shared";
  const original = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation((url: string, ...args: unknown[]) =>
    params(url).get("visibility") === "shared"
      ? Promise.resolve({ ok: false, status: 503, statusText: "Unavailable" })
      : original(url, ...args),
  );
  const { result } = renderHook(useSidebarData, { wrapper });
  await waitFor(() => expect(result.current.shared.isError).toBe(true));
  await act(async () => {
    result.current.registerFolder("project", [row("owned-folder"), row("shared-folder", true)]);
  });
  expect(result.current.inbox.isError).toBe(false);
  expect(result.current.inbox.isLoading).toBe(false);
  expect(result.current.inboxRows.map((r) => r.id).sort()).toEqual(["mine", "owned-folder", "pin"]);
  expect(result.current.inboxCount).toBe(3);
  const before = listCalls("shared").length;
  await act(async () => {
    await result.current.inbox.fetchNextPage();
    await result.current.inbox.refetch?.();
  });
  expect(listCalls("shared")).toHaveLength(before);
});

it("allows OSS Inbox demand but never enables the general Shared cache for pins alone", async () => {
  config = { ...config, pinsIncludeShared: true };
  const { result, rerender } = renderHook(useSidebarData, { wrapper });
  await waitFor(() => expect(result.current.pinned.isSuccess).toBe(true));
  expect(listCalls("shared")).toHaveLength(0);
  expect(
    fetchMock.mock.calls.some(
      ([url]) => params(url).get("visibility") === "shared" && params(url).has("pinned"),
    ),
  ).toBe(true);
  config = { ...config, inboxIncludesShared: true };
  rerender();
  await waitFor(() => expect(result.current.shared.isSuccess).toBe(true));
  expect(listCalls("shared")).toHaveLength(1);
  expect(result.current.inbox).toBe(result.current.all);
});

it("filters previously cached shared pins and rejects their optimistic insertion", async () => {
  const shared = { ...row("shared-pin", true), labels: { [PINNED_LABEL_KEY]: "1" } };
  client.setQueryData(PINNED_CONVERSATIONS_KEY, { conversations: [shared], filterHonored: true });
  client.setQueryData(sharedKey, {
    pages: [sessionRowsPage([shared])],
    pageParams: [undefined],
    windowSize: 30,
  });
  const { result } = renderHook(
    () => ({ data: useSidebarData(), toggle: useTogglePinnedConversation() }),
    { wrapper },
  );
  expect(result.current.data.pinned.data?.conversations).toEqual([]);
  await act(async () => {
    await expect(
      result.current.toggle.mutateAsync({ id: shared.id, pinned: true }),
    ).rejects.toThrow("Only your sessions");
  });
  expect(fetchMock.mock.calls.some(([, options]) => options?.method === "PATCH")).toBe(false);
  expect(result.current.data.pinned.data?.conversations).toEqual([]);
});

it("can pin an owned conversation opened directly outside the loaded lists", async () => {
  client.setQueryData<Partial<Session>>(["session", "direct-owned"], {
    id: "direct-owned",
    title: "Direct owned",
    createdAt: 123,
    labels: {},
    permissionLevel: 4,
  });
  const { result } = renderHook(
    () => ({ data: useSidebarData(), toggle: useTogglePinnedConversation() }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.data.pinned.isSuccess).toBe(true));
  fetchMock.mockImplementationOnce(async () => ({
    ok: true,
    json: async () => ({ ...row("direct-owned"), labels: { [PINNED_LABEL_KEY]: "123" } }),
  }));
  await act(async () => {
    await result.current.toggle.mutateAsync({ id: "direct-owned", pinned: true });
  });
  await waitFor(() =>
    expect(result.current.data.pinned.data?.conversations).toContainEqual(
      expect.objectContaining({ id: "direct-owned", title: "Direct owned", permission_level: 4 }),
    ),
  );
  expect(listCalls("shared")).toHaveLength(0);
});

it("directory warnings reuse Mine pagination and cache updates without additional requests", async () => {
  const { result, rerender } = renderHook(
    ({ enabled }) => ({ sidebar: useSidebarData(), directory: useDirectorySessions(enabled) }),
    { wrapper, initialProps: { enabled: false } },
  );
  await waitFor(() => expect(result.current.sidebar.pinned.isSuccess).toBe(true));
  await waitFor(() => expect(result.current.sidebar.mine.isSuccess).toBe(true));
  expect(result.current.directory.data).toBeUndefined();
  const startupCalls = fetchMock.mock.calls.length;
  rerender({ enabled: true });
  expect(result.current.directory.data?.map((r) => r.id)).toEqual(["mine"]);
  expect(fetchMock.mock.calls).toHaveLength(startupCalls);

  await act(async () => {
    await result.current.sidebar.mine.fetchNextPage();
  });
  await waitFor(() =>
    expect(result.current.directory.data?.map((r) => r.id).sort()).toEqual(["mine", "mine-older"]),
  );
  expect(fetchMock.mock.calls).toHaveLength(startupCalls + 1);
  act(() => {
    client.setQueryData<ScopeCacheData>(
      ["conversations", "", false, null, "mine"],
      (data) =>
        data && {
          ...data,
          pages: data.pages.map((page) => ({
            ...page,
            data: page.data.map((r) => ({ ...r, workspace: "/updated" })),
          })),
        },
    );
  });
  await waitFor(() => expect(result.current.directory.data?.[1].workspace).toBe("/updated"));
  rerender({ enabled: false });
  expect(result.current.directory.data).toBeUndefined();
  rerender({ enabled: true });
  expect(result.current.directory.data).toHaveLength(2);
  expect(fetchMock.mock.calls).toHaveLength(startupCalls + 1);
  expect(listCalls("shared")).toHaveLength(0);
  expect(fetchMock.mock.calls.every(([url]) => params(url).has("visibility"))).toBe(true);
  expect(client.getQueryCache().find({ queryKey: ["directory-sessions"] })).toBeUndefined();
});

it("directory warnings exclude Shared rows even when its cache is active or retained", async () => {
  view = "shared";
  const { result, rerender } = renderHook(
    () => ({ sidebar: useSidebarData(), directory: useDirectorySessions(true) }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.sidebar.shared.isSuccess).toBe(true));
  expect(result.current.directory.data?.map((r) => r.id)).toEqual(["mine"]);
  view = "mine";
  rerender();
  expect(result.current.directory.data?.map((r) => r.id)).toEqual(["mine"]);
  expect(result.current.sidebar.sharedActive).toBe(false);
});

it("passes API page size and refresh cap from provider configuration to both scopes", async () => {
  config = { ...config, sessionPageSize: 50, maxRefreshSessions: 75 };
  view = "all";
  const { result } = renderHook(useSidebarData, { wrapper });
  await waitFor(() => expect(result.current.shared.isSuccess).toBe(true));
  await act(async () => {
    await result.current.all.fetchNextPage();
  });
  await act(async () => {
    await result.current.all.refetch?.();
  });
  for (const scope of ["mine", "shared"]) {
    expect(listCalls(scope).map(([url]) => params(url).get("limit"))).toEqual(["50", "50", "75"]);
  }
  expect(
    fetchMock.mock.calls
      .filter(([url]) => params(url).has("pinned"))
      .map(([url]) => params(url).get("limit")),
  ).toEqual(["30"]);
});
