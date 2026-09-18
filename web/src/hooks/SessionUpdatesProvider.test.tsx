// Focused coverage for SessionUpdatesProvider's watch-set computation:
// the open session must always be unioned into the set pushed to the
// socket, even when it's an off-sidebar child absent from the
// conversations cache. The cache-merge / frame-application logic is
// covered separately in sessionListCache.test.ts; here we mock the socket
// and assert exactly which ids reach `setWatched`.

import { act, cleanup, render, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, QueryObserver } from "@tanstack/react-query";
import { MemoryRouter, useNavigate } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearSessionTombstones,
  useArchiveConversation,
  type Conversation,
  type ConversationsPage,
} from "@/hooks/useConversations";
import type { ConversationsInfiniteData } from "@/lib/sessionListCache";

// Mock the socket transport so setWatched is observable and start/stop are
// inert. subscribe/subscribeStatus return no-op unsubscribers.
const setWatched = vi.fn();
const subscribe = vi.fn((_fn: () => void) => () => {});
vi.mock("@/lib/sessionUpdatesSocket", () => ({
  sessionUpdatesSocket: {
    start: vi.fn(),
    stop: vi.fn(),
    setWatched: (...args: unknown[]) => setWatched(...args),
    subscribe: (fn: () => void) => subscribe(fn),
  },
}));

import { SidebarDataProvider, useSidebarView } from "./useSidebarData";
import { useCanEdit } from "./usePermissions";
import { sidebarConfig } from "@/lib/sidebarConfig";
import { SessionUpdatesProvider } from "./SessionUpdatesProvider";
import { useSaveProjectOrder } from "./useProjectOrder";
import * as projectsApi from "@/lib/projectsApi";

function conv(id: string): Conversation {
  return {
    id,
    object: "conversation",
    title: null,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: 100,
  };
}

function seedConversations(client: QueryClient, ids: string[]): void {
  const page: ConversationsPage = {
    data: ids.map(conv),
    first_id: ids[0] ?? null,
    last_id: ids.at(-1) ?? null,
    has_more: false,
  };
  const data: ConversationsInfiniteData = {
    pages: [page],
    pageParams: [undefined],
  };
  // The canonical key shape SessionUpdatesProvider reads from.
  client.setQueryData(["conversations", "", false], data);
}

function seedProjectFolder(client: QueryClient, project: string, ids: string[]): void {
  const page: ConversationsPage = {
    data: ids.map(conv),
    first_id: ids[0] ?? null,
    last_id: ids.at(-1) ?? null,
    has_more: false,
  };
  client.setQueryData(["project-sessions", project], {
    pages: [page],
    pageParams: [undefined],
  } satisfies ConversationsInfiniteData);
}

function renderProvider(client: QueryClient, initialEntries: string[]) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={initialEntries}>
        <SessionUpdatesProvider>{null}</SessionUpdatesProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// The latest id-set pushed to the socket, sorted for stable comparison.
function lastWatched(): string[] {
  const call = setWatched.mock.calls.at(-1);
  return [...((call?.[0] ?? []) as string[])].sort();
}

beforeEach(() => {
  setWatched.mockClear();
  subscribe.mockClear();
});

afterEach(() => {
  cleanup();
  clearSessionTombstones();
});

describe("SessionUpdatesProvider watch-set", () => {
  it("watches the cached sidebar ids when no session is open", () => {
    const client = new QueryClient();
    seedConversations(client, ["conv_a", "conv_b"]);
    renderProvider(client, ["/"]);
    expect(lastWatched()).toEqual(["conv_a", "conv_b"]);
  });

  it("unions an off-sidebar open child into the watch-set", () => {
    // The child is NOT in the conversations cache (children are filtered
    // out of the sidebar list), yet it must be watched so the open-session
    // view gets streamed liveness for it.
    const client = new QueryClient();
    seedConversations(client, ["conv_a"]);
    renderProvider(client, ["/c/conv_child"]);
    expect(lastWatched()).toEqual(["conv_a", "conv_child"]);
  });

  it("does not duplicate the open session when it's already a sidebar row", () => {
    const client = new QueryClient();
    seedConversations(client, ["conv_open", "conv_b"]);
    renderProvider(client, ["/c/conv_open"]);
    expect(lastWatched()).toEqual(["conv_b", "conv_open"]);
  });

  it("does not send client-only temp ids in the watch-set", () => {
    const client = new QueryClient();
    seedConversations(client, ["conv_real", "temp:12345678"]);
    renderProvider(client, ["/c/temp:12345678"]);
    expect(lastWatched()).toEqual(["conv_real"]);
  });

  it("re-pushes the watch-set with the new open id on navigation", () => {
    // Navigating between off-sidebar children doesn't touch the
    // conversations cache, so the watch-set must re-push from the activeId
    // effect — otherwise the newly-opened child would never be watched.
    const client = new QueryClient();
    seedConversations(client, ["conv_a"]);

    let navigate: ReturnType<typeof useNavigate> | null = null;
    function CaptureNavigate() {
      navigate = useNavigate();
      return null;
    }

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/c/conv_child1"]}>
          <CaptureNavigate />
          <SessionUpdatesProvider>{null}</SessionUpdatesProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(lastWatched()).toEqual(["conv_a", "conv_child1"]);

    setWatched.mockClear();
    // Navigate within the same router (no cache change) to a different
    // off-sidebar child.
    act(() => navigate?.("/c/conv_child2"));
    expect(lastWatched()).toEqual(["conv_a", "conv_child2"]);
  });
});

// The frame handler the provider registered on the mocked socket. Feeding
// frames through it exercises the real provider logic (fingerprint tracking,
// cache application) without a live WebSocket.
function frameHandler(): (frame: unknown) => void {
  const call = subscribe.mock.calls.at(-1);
  if (!call) throw new Error("provider never subscribed to the socket");
  return call[0] as unknown as (frame: unknown) => void;
}

// Wire-shaped session row carrying a comments fingerprint. Frames send full
// rows with explicit nulls (comments_updated_at: null when no comments).
function wireItem(id: string, commentsCount: number, commentsUpdatedAt: number | null) {
  return { ...conv(id), comments_count: commentsCount, comments_updated_at: commentsUpdatedAt };
}

describe("SessionUpdatesProvider host changes", () => {
  it("invalidates cached session agents so shell inventories refresh", () => {
    const client = new QueryClient();
    seedConversations(client, ["conv_old"]);
    renderProvider(client, ["/c/conv_old"]);
    const invalidate = vi.spyOn(client, "invalidateQueries");

    act(() => frameHandler()({ type: "hosts_changed" }));

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["hosts"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["session-agent"] });
  });
});

describe("SessionUpdatesProvider comments fingerprint", () => {
  it("invalidates the comments cache when a changed frame moves the fingerprint", () => {
    const client = new QueryClient();
    seedConversations(client, ["conv_a"]);
    renderProvider(client, ["/"]);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const handler = frameHandler();

    // First sight also invalidates: a mutation between the comments
    // query's fetch and this first frame would otherwise be swallowed as
    // baseline (no-op when no comments query is mounted, so it's free).
    act(() => handler({ type: "snapshot", items: [wireItem("conv_a", 0, null)] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["comments", "conv_a"] });
    invalidate.mockClear();

    // Another user added a comment: count 0 → 1. The stale cached comment
    // list must be invalidated (prefix key also covers per-file variants).
    act(() => handler({ type: "changed", items: [wireItem("conv_a", 1, 1_000)] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["comments", "conv_a"] });
  });

  it("invalidates on an in-place edit (timestamp moves, count unchanged)", () => {
    // The agent marking a comment addressed changes no row count — the
    // updated_at bump alone must trigger the refresh.
    const client = new QueryClient();
    seedConversations(client, ["conv_a"]);
    renderProvider(client, ["/"]);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const handler = frameHandler();

    act(() => handler({ type: "snapshot", items: [wireItem("conv_a", 1, 1_000)] }));
    invalidate.mockClear(); // drop the first-sight invalidation
    act(() => handler({ type: "changed", items: [wireItem("conv_a", 1, 2_000)] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["comments", "conv_a"] });
  });

  it("does not invalidate when a changed frame leaves the fingerprint alone", () => {
    // Unrelated row changes (title, status, liveness) must not churn the
    // comments cache — only fingerprint movement may invalidate.
    const client = new QueryClient();
    seedConversations(client, ["conv_a"]);
    renderProvider(client, ["/"]);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const handler = frameHandler();

    act(() => handler({ type: "snapshot", items: [wireItem("conv_a", 1, 1_000)] }));
    invalidate.mockClear(); // drop the first-sight invalidation
    act(() =>
      handler({
        type: "changed",
        items: [{ ...wireItem("conv_a", 1, 1_000), title: "renamed" }],
      }),
    );
    const commentsCalls = invalidate.mock.calls.filter(
      ([args]) => Array.isArray(args?.queryKey) && args.queryKey[0] === "comments",
    );
    // Zero comments-key invalidations proves an unchanged fingerprint is
    // inert; a call here means every row change would refetch comments.
    expect(commentsCalls).toEqual([]);
  });
});

describe("SessionUpdatesProvider project folders", () => {
  it("watches sessions that live only in a project folder's cache", () => {
    // A project folder fetches its members into ["project-sessions", <name>],
    // separate from the global list. Those ids must still be watched so the
    // stream delivers liveness (e.g. the "Needs response" elicitation count).
    const client = new QueryClient();
    seedConversations(client, ["conv_a"]);
    seedProjectFolder(client, "Sprint 42", ["conv_filed"]);
    renderProvider(client, ["/"]);
    expect(lastWatched()).toEqual(["conv_a", "conv_filed"]);
  });

  it("patches a project folder row in place from a changed frame", () => {
    const client = new QueryClient();
    seedProjectFolder(client, "Sprint 42", ["conv_filed"]);
    renderProvider(client, ["/"]);
    const handler = frameHandler();

    // A pending-elicitation bump must reach the folder's own cache so the row
    // flips to "Needs response" without a refetch.
    act(() =>
      handler({
        type: "changed",
        items: [{ ...conv("conv_filed"), pending_elicitations_count: 1 }],
      }),
    );

    const folder = client.getQueryData<ConversationsInfiniteData>([
      "project-sessions",
      "Sprint 42",
    ]);
    expect(folder!.pages[0].data[0]!.pending_elicitations_count).toBe(1);
  });

  it("evicts a removed session from a project folder's cache", () => {
    const client = new QueryClient();
    seedProjectFolder(client, "Sprint 42", ["conv_filed", "conv_other"]);
    renderProvider(client, ["/"]);
    const handler = frameHandler();

    act(() => handler({ type: "removed", ids: ["conv_filed"] }));

    const folder = client.getQueryData<ConversationsInfiniteData>([
      "project-sessions",
      "Sprint 42",
    ]);
    expect(folder!.pages[0].data.map((c) => c.id)).toEqual(["conv_other"]);
  });
});

describe("SessionUpdatesProvider fingerprint pruning", () => {
  it("prunes de-watched sessions on snapshot so they re-baseline on return", () => {
    const client = new QueryClient();
    seedConversations(client, ["conv_a", "conv_b"]);
    renderProvider(client, ["/"]);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const handler = frameHandler();

    // Both sessions watched, fingerprints recorded.
    act(() =>
      handler({
        type: "snapshot",
        items: [wireItem("conv_a", 1, 1_000), wireItem("conv_b", 1, 1_000)],
      }),
    );
    // Watch-set narrowed (e.g. a search filter): the snapshot restates
    // only conv_a, so conv_b's fingerprint entry must be dropped.
    act(() => handler({ type: "snapshot", items: [wireItem("conv_a", 1, 1_000)] }));
    invalidate.mockClear();

    // conv_b returns with the SAME fingerprint as before. If pruning
    // regressed, the retained entry would match and skip invalidation —
    // missing any mutation that happened while conv_b was unwatched and
    // got reverted, and unbounding the map. First sight must re-fire.
    act(() => handler({ type: "changed", items: [wireItem("conv_b", 1, 1_000)] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["comments", "conv_b"] });
  });
});

describe("SessionUpdatesProvider archive tombstone", () => {
  it("does not let a stale changed frame resurrect an archiving row", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ ...conv("conv_a"), archived: true, updated_at: 10 }),
      }),
    );
    const client = new QueryClient();
    seedConversations(client, ["conv_a", "conv_b"]);
    client.setQueryData<ConversationsInfiniteData>(["conversations", "", true], {
      pages: [
        {
          data: [{ ...conv("conv_c"), archived: true }],
          first_id: "conv_c",
          last_id: "conv_c",
          has_more: false,
        },
      ],
      pageParams: [undefined],
    });
    renderProvider(client, ["/"]);
    const handler = frameHandler();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const archive = renderHook(() => useArchiveConversation(), { wrapper });

    archive.result.current.mutate({ id: "conv_a", archived: true });
    await waitFor(() => {
      expect(
        client
          .getQueryData<ConversationsInfiniteData>(["conversations", "", false])!
          .pages[0].data.map((row) => row.id),
      ).toEqual(["conv_b"]);
    });

    act(() =>
      handler({
        type: "changed",
        items: [{ ...conv("conv_a"), archived: false, title: "Late edit" }],
      }),
    );
    expect(
      client
        .getQueryData<ConversationsInfiniteData>(["conversations", "", false])!
        .pages[0].data.map((row) => row.id),
    ).toEqual(["conv_b"]);
    expect(
      client
        .getQueryData<ConversationsInfiniteData>(["conversations", "", true])!
        .pages[0].data.find((row) => row.id === "conv_a"),
    ).toMatchObject({ title: "Late edit", archived: true });
    await waitFor(() => expect(archive.result.current.isSuccess).toBe(true));
  });
});

describe("SessionUpdatesProvider list invalidation", () => {
  it("invalidates the archived-project-names scan on a remote removal", () => {
    // Another client archiving/relabeling/deleting must refresh the Archived
    // view's project picker — only local mutations invalidate it otherwise.
    vi.useFakeTimers();
    try {
      const client = new QueryClient();
      seedConversations(client, ["conv_a"]);
      renderProvider(client, ["/"]);
      const invalidate = vi.spyOn(client, "invalidateQueries");
      const handler = frameHandler();

      act(() => handler({ type: "removed", ids: ["conv_a"] }));
      act(() => vi.advanceTimersByTime(300)); // past the debounce

      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["archived-project-names"] });
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("SessionUpdatesProvider projects_changed frames", () => {
  it("invalidates the project-row caches when another client changes a project", () => {
    // A project rename/create/delete in another client arrives only as a
    // `projects_changed` frame; nothing else refreshes ["projects"] (staleTime
    // keeps it cached), so the handler must invalidate it — and the per-project
    // config cache — for the sidebar to converge without a reload.
    const client = new QueryClient();
    seedConversations(client, ["conv_a"]);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderProvider(client, ["/"]);
    const frameListener = subscribe.mock.calls.at(-1)?.[0] as unknown as (frame: unknown) => void;
    expect(frameListener).toBeTypeOf("function");
    act(() => frameListener({ type: "projects_changed" }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["projects"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["project-order"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["project-config"] });
  });

  it("refreshes both caches when a change arrives during the save's settlement refetch", async () => {
    const client = new QueryClient();
    const oldProjects = [{ id: "a", name: "A" }];
    const newProjects = [{ id: "b", name: "B" }, ...oldProjects];
    const oldOrder = { sort_mode: "manual" as const, ordered_project_ids: ["a"] };
    const newOrder = { sort_mode: "manual" as const, ordered_project_ids: ["b", "a"] };
    let finishProjects!: (value: typeof oldProjects) => void;
    let finishOrder!: (value: typeof oldOrder) => void;
    const fetchProjects = vi
      .fn()
      .mockReturnValueOnce(
        new Promise<typeof oldProjects>((resolve) => {
          finishProjects = resolve;
        }),
      )
      .mockResolvedValue(newProjects);
    const fetchOrder = vi
      .fn()
      .mockReturnValueOnce(
        new Promise<typeof oldOrder>((resolve) => {
          finishOrder = resolve;
        }),
      )
      .mockResolvedValue(newOrder);
    const stopProjects = new QueryObserver(client, {
      queryKey: ["projects"],
      queryFn: fetchProjects,
      initialData: oldProjects,
      staleTime: Infinity,
    }).subscribe(() => {});
    const stopOrder = new QueryObserver(client, {
      queryKey: ["project-order"],
      queryFn: fetchOrder,
      initialData: oldOrder,
      staleTime: Infinity,
    }).subscribe(() => {});
    const save = vi.spyOn(projectsApi, "saveProjectOrder").mockResolvedValue(oldOrder);
    renderProvider(client, ["/"]);
    const { result } = renderHook(() => useSaveProjectOrder(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    try {
      act(() => result.current.mutate(oldProjects));
      await waitFor(() => expect(fetchOrder).toHaveBeenCalledTimes(1));
      expect(fetchProjects).toHaveBeenCalledTimes(1);
      expect(client.isMutating({ mutationKey: ["project-order"] })).toBe(1);
      const frameListener = subscribe.mock.calls.at(-1)?.[0] as unknown as (frame: unknown) => void;
      act(() => frameListener({ type: "projects_changed" }));
      expect(fetchProjects).toHaveBeenCalledTimes(1);
      expect(fetchOrder).toHaveBeenCalledTimes(1);

      // The in-flight reads captured the snapshot before the other tab's change.
      await act(async () => {
        finishProjects(oldProjects);
        finishOrder(oldOrder);
      });
      await waitFor(() => {
        expect(result.current.isSuccess).toBe(true);
        expect(client.getQueryData(["projects"])).toEqual(newProjects);
        expect(client.getQueryData(["project-order"])).toEqual(newOrder);
      });
      expect(fetchProjects).toHaveBeenCalledTimes(2);
      expect(fetchOrder).toHaveBeenCalledTimes(2);
    } finally {
      stopProjects();
      stopOrder();
      save.mockRestore();
    }
  });

  it("coalesces changes until all order saves finish, including a failed save", async () => {
    const client = new QueryClient();
    renderProvider(client, ["/"]);
    let finish!: () => void;
    let fail!: (error: Error) => void;
    const first = client.getMutationCache().build(client, {
      mutationKey: ["project-order"],
      mutationFn: () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    });
    const second = client.getMutationCache().build(client, {
      mutationKey: ["project-order"],
      mutationFn: () =>
        new Promise<void>((_resolve, reject) => {
          fail = reject;
        }),
    });
    const firstSave = first.execute(undefined);
    const secondSave = second.execute(undefined).catch(() => {});
    await waitFor(() => expect(fail).toBeTypeOf("function"));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const frameListener = subscribe.mock.calls.at(-1)?.[0] as unknown as (frame: unknown) => void;
    act(() => {
      frameListener({ type: "projects_changed" });
      frameListener({ type: "projects_changed" });
    });
    await act(async () => {
      finish();
      await firstSave;
    });
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["projects"] });
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["project-order"] });
    invalidate.mockClear();
    await act(async () => {
      fail(new Error("offline"));
      await secondSave;
    });
    expect(invalidate.mock.calls).toEqual([
      [{ queryKey: ["projects"] }],
      [{ queryKey: ["project-order"] }],
    ]);
  });
});

it("updates pending counts on a pinned row outside the scope windows", () => {
  const client = new QueryClient();
  client.setQueryData(["pinned-conversations"], {
    conversations: [
      { ...conv("old-pin"), labels: { "omnigent.pinned": "123" }, pending_elicitations_count: 0 },
    ],
    filterHonored: true,
  });
  renderProvider(client, ["/"]);
  act(() =>
    frameHandler()({
      type: "changed",
      items: [{ ...wireItem("old-pin", 2, 100), pending_elicitations_count: 3 }],
    }),
  );
  const data = client.getQueryData<{ conversations: Conversation[] }>(["pinned-conversations"]);
  expect(data?.conversations[0]).toMatchObject({
    pending_elicitations_count: 3,
    comments_count: 2,
    labels: { "omnigent.pinned": "123" },
  });
});

it("keeps a directly opened shared session watched and permission-aware with the Shared list inactive", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const mine = conv("mine");
  const sharedKey = ["conversations", "", false, null, "shared"];
  client.setQueryData(sharedKey, {
    pages: [{ data: [{ ...conv("retained-shared"), permission_level: 1 }] }],
    pageParams: [undefined],
  });
  client.setQueryData(["session", "direct-shared"], { id: "direct-shared", permissionLevel: 1 });
  const fetch = vi.fn(async (url: string) => ({
    ok: true,
    json: async () => ({ data: url.includes("pinned=true") ? [] : [mine], has_more: false }),
  }));
  vi.stubGlobal("fetch", fetch);
  function Selection() {
    useSidebarView("mine");
    return null;
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/c/direct-shared"]}>
        <SidebarDataProvider
          config={{ ...sidebarConfig, inboxIncludesShared: false, pinsIncludeShared: false }}
        >
          <Selection />
          <SessionUpdatesProvider>{children}</SessionUpdatesProvider>
        </SidebarDataProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  try {
    const { result, unmount } = renderHook(() => useCanEdit("direct-shared"), { wrapper });
    await waitFor(() => expect(lastWatched()).toEqual(["direct-shared", "mine"]));
    expect(result.current).toBe(false);
    act(() =>
      client.setQueryData(["session", "direct-shared"], {
        id: "direct-shared",
        permissionLevel: 2,
      }),
    );
    await waitFor(() => expect(result.current).toBe(true));
    expect(fetch.mock.calls.some(([url]) => url.includes("visibility=shared"))).toBe(false);
    expect(client.getQueryData(sharedKey)).toBeDefined();
    unmount();
  } finally {
    client.clear();
    vi.unstubAllGlobals();
  }
});
