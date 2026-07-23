// Unit tests for the conversation-mutation HTTP helpers, plus the
// query-invalidation contract of the stop mutation hook.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConversationsInfiniteData } from "@/lib/sessionListCache";
import type { Session } from "@/lib/types";
import { useSessionUpdatesConnected } from "./useSessionUpdatesConnected";
import {
  deleteConversation,
  fetchAllArchivedProjectNames,
  renameConversation,
  useArchiveConversation,
  useBulkArchiveConversations,
  useBulkDeleteConversations,
  useBulkStopSessions,
  useConversations,
  useDeleteProject,
  useProjects,
  useNewestProjectSession,
  useProjectSessions,
  useMoveToProject,
  useRenameConversation,
  useStopAndDeleteConversation,
  useStopSession,
  type Conversation,
} from "./useConversations";

vi.mock("./useSessionUpdatesConnected", () => ({ useSessionUpdatesConnected: vi.fn() }));

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.mocked(useSessionUpdatesConnected).mockReturnValue(false);
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("renameConversation", () => {
  it("PATCHes /v1/sessions/{id} with the new title", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        id: "conv_abc",
        object: "conversation",
        title: "New name",
        created_at: 0,
        updated_at: 1,
        labels: {},
      }),
    );

    const result = await renameConversation("conv_abc", "New name");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc");
    expect(init.method).toBe("PATCH");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ title: "New name" });
    expect(result.title).toBe("New name");
  });

  it("url-encodes the conversation id", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        id: "x",
        object: "conversation",
        title: "t",
        created_at: 0,
        updated_at: 0,
        labels: {},
      }),
    );
    await renameConversation("conv with space", "t");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/conv%20with%20space");
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 404 }));
    await expect(renameConversation("missing", "x")).rejects.toThrow(/404/);
  });
});

describe("useConversations refetch interval", () => {
  function renderConversationsHook(options?: Parameters<typeof useConversations>[2]) {
    fetchMock.mockResolvedValue(
      mockResponse({
        data: [],
        first_id: null,
        last_id: null,
        has_more: false,
      }),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);

    renderHook(() => useConversations("", false, options), { wrapper });
    const query = queryClient.getQueryCache().find({
      queryKey: ["conversations", "", false],
    });
    return (query?.options as { refetchInterval?: unknown } | undefined)?.refetchInterval;
  }

  it("does not poll by default while the updates stream is connected", () => {
    vi.mocked(useSessionUpdatesConnected).mockReturnValue(true);

    const interval = renderConversationsHook();

    // Non-sidebar consumers should not add steady `/v1/sessions` traffic
    // while the WebSocket is healthy.
    expect(interval).toBe(false);
  });

  it("keeps a low-rate HTTP reconciliation when explicitly requested", () => {
    vi.mocked(useSessionUpdatesConnected).mockReturnValue(true);

    const interval = renderConversationsHook({ reconcileWhileConnected: true });

    // The visible sidebar list opts in because the WebSocket only watches
    // ids already in the cache; without this, sessions created in another
    // tab/CLI never appear.
    expect(interval).toBe(60_000);
  });

  it("uses the disconnected fallback interval when the updates stream is down", () => {
    vi.mocked(useSessionUpdatesConnected).mockReturnValue(false);

    const interval = renderConversationsHook();

    // The disconnected path keeps the prior safety-poll cadence.
    expect(interval).toBe(45_000);
  });
});

describe("useConversations project filter", () => {
  function renderWithProject(project?: string) {
    fetchMock.mockResolvedValue(
      mockResponse({ data: [], first_id: null, last_id: null, has_more: false }),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    renderHook(() => useConversations("", true, {}, project), { wrapper });
    return queryClient;
  }

  it("sends project= alongside include_archived=true when a project is set", async () => {
    renderWithProject("Design");
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // The archived list must scope server-side, so both params reach the request.
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("include_archived=true");
    expect(url).toContain("project=Design");
  });

  it("url-encodes a project name with spaces", async () => {
    renderWithProject("My Project");
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("project=My+Project");
  });

  it("omits project= when no project is set (all projects)", async () => {
    renderWithProject(undefined);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // "All projects" must not send an empty project= (the server would read
    // that as "unfiled sessions only").
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("include_archived=true");
    expect(url).not.toContain("project=");
  });

  it("coalesces an empty-string project into 'all projects' (base key, no project= param)", async () => {
    const queryClient = renderWithProject("");

    // No distinct four-element "" variant: it shares the base three-element key,
    // so key, request, and cache-membership all agree "all projects".
    const keys = queryClient
      .getQueryCache()
      .getAll()
      .map((q) => q.queryKey as unknown[]);
    expect(keys).toContainEqual(["conversations", "", true]);
    expect(keys.every((k) => k.length === 3)).toBe(true);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls[0][0] as string).not.toContain("project=");
  });

  it("forwards a project literally named __all__ as project=__all__", async () => {
    renderWithProject("__all__");
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("project=__all__");
  });
});

describe("fetchAllArchivedProjectNames", () => {
  it("pages through all archived sessions and returns distinct sorted project names", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          data: [
            { id: "a", archived: true, labels: { omni_project: "Beta" } },
            // Active row — include_archived returns it, but it's not filterable here.
            { id: "b", archived: false, labels: { omni_project: "Zeta" } },
            // Archived but unfiled — no project label to collect.
            { id: "c", archived: true, labels: {} },
          ],
          first_id: "a",
          last_id: "c",
          has_more: true,
        }),
      )
      .mockResolvedValueOnce(
        mockResponse({
          data: [
            { id: "d", archived: true, labels: { omni_project: "Alpha" } },
            // Duplicate project across pages collapses to one entry.
            { id: "e", archived: true, labels: { omni_project: "Beta" } },
          ],
          first_id: "d",
          last_id: "e",
          has_more: false,
        }),
      );

    const names = await fetchAllArchivedProjectNames();

    // Distinct + sorted; active and unfiled rows contribute nothing.
    expect(names).toEqual(["Alpha", "Beta"]);
    // Page 1: archived, large page size, no project filter, no cursor.
    const url1 = fetchMock.mock.calls[0][0] as string;
    expect(url1).toContain("include_archived=true");
    expect(url1).toContain("limit=100");
    expect(url1).not.toContain("project=");
    expect(url1).not.toContain("after=");
    // Page 2 follows the previous page's last_id cursor.
    expect(fetchMock.mock.calls[1][0]).toContain("after=c");
  });

  it("stops after one request when the first page has no more", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ data: [], first_id: null, last_id: null, has_more: false }),
    );

    const names = await fetchAllArchivedProjectNames();

    expect(names).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("deleteConversation", () => {
  it("DELETEs /v1/sessions/{id}", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));

    await deleteConversation("conv_abc");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc");
    expect(init.method).toBe("DELETE");
  });

  it("appends ?delete_branch=true when deleteBranch is set", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));

    await deleteConversation("conv_abc", true);

    // The opt-in branch-cleanup flag must reach the server as a query
    // param; without it the worktree/branch would never be removed.
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc?delete_branch=true");
    expect(init.method).toBe("DELETE");
  });

  it("omits the query param when deleteBranch is false (default)", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));

    await deleteConversation("conv_abc");

    // Default delete must NOT carry the flag, so a plain delete never
    // triggers irreversible branch cleanup.
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/conv_abc");
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 404 }));
    await expect(deleteConversation("missing")).rejects.toThrow(/404/);
  });
});

describe("useStopAndDeleteConversation stops the running session first", () => {
  function renderDeleteHook() {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    return renderHook(() => useStopAndDeleteConversation(), { wrapper });
  }

  it("POSTs stop_session, THEN DELETEs the session", async () => {
    // Call 1: stop_session → {queued:false}. Call 2: DELETE → {deleted:true}.
    fetchMock.mockResolvedValueOnce(mockResponse({ queued: false }));
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));

    const { result } = renderDeleteHook();
    result.current.mutate({ id: "conv_x" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Exactly two requests, in order: stop first, delete second. If the
    // stop call is missing, the running agent (claude-native tmux pane /
    // host-spawned runner) keeps executing orphaned after the delete —
    // the bug this hook closes.
    expect(fetchMock).toHaveBeenCalledTimes(2);

    const [stopUrl, stopInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(stopUrl).toBe("/v1/sessions/conv_x/events");
    expect(stopInit.method).toBe("POST");
    expect(JSON.parse(stopInit.body as string)).toEqual({ type: "stop_session", data: {} });

    const [delUrl, delInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(delUrl).toBe("/v1/sessions/conv_x");
    expect(delInit.method).toBe("DELETE");
  });

  it("still DELETEs when the stop fails (best-effort)", async () => {
    // Stop returns a non-2xx (offline/wedged runner). The delete must
    // still go out and the mutation must succeed.
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 503 }));
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));

    const { result } = renderDeleteHook();
    result.current.mutate({ id: "conv_x", deleteBranch: true });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Two calls means the mutation attempted stop and still issued DELETE
    // after the stop failed; one would skip either step, while three or more
    // would duplicate network work.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // A swallowed stop failure must not abort the delete: the row has to
    // disappear from the UI regardless. The deleteBranch flag still rides
    // through to the DELETE query string.
    const [delUrl, delInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(delUrl).toBe("/v1/sessions/conv_x?delete_branch=true");
    expect(delInit.method).toBe("DELETE");
  });
});

// Shared cache-seeding helpers for the delete-eviction and rename-patching
// suites below. The default title is what the rename tests overwrite; the
// delete tests never read it.

/** Minimal sidebar row for seeding list caches. */
function conversation(overrides: Partial<Conversation> & { id: string }): Conversation {
  return {
    object: "conversation",
    title: "Old name",
    created_at: 0,
    updated_at: 100,
    labels: {},
    permission_level: null,
    ...overrides,
  };
}

/** Single-page infinite-query cache value holding the given rows. */
function infinitePage(rows: Conversation[]): ConversationsInfiniteData {
  return {
    pages: [
      {
        data: rows,
        first_id: rows[0]?.id ?? null,
        last_id: rows[rows.length - 1]?.id ?? null,
        has_more: false,
      },
    ],
    pageParams: [undefined],
  };
}

describe("useStopAndDeleteConversation cache eviction", () => {
  function seedAndDelete() {
    // Call 1: stop_session → {queued:false}. Call 2: DELETE → {deleted:true}.
    fetchMock.mockResolvedValueOnce(mockResponse({ queued: false }));
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    // Two list variants (default sidebar + archived view), a project folder's
    // own paginated list, plus the two long-lived per-session caches that can
    // resurrect a deleted row.
    queryClient.setQueryData(
      ["conversations", "", false],
      infinitePage([conversation({ id: "conv_x" }), conversation({ id: "conv_other" })]),
    );
    queryClient.setQueryData(
      ["conversations", "", true],
      infinitePage([conversation({ id: "conv_x" })]),
    );
    queryClient.setQueryData(
      ["project-sessions", "Sprint 42"],
      infinitePage([conversation({ id: "conv_x" }), conversation({ id: "conv_sibling" })]),
    );
    queryClient.setQueryData(["conversation-backfill", "conv_x"], conversation({ id: "conv_x" }));
    queryClient.setQueryData(["session", "conv_x"], {
      id: "conv_x",
      agentId: "ag_1",
      agentName: null,
      status: "idle",
      createdAt: 0,
      title: "A session",
      items: [],
      permissionLevel: null,
      parentSessionId: null,
      subAgentName: null,
    } satisfies Session);
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const rendered = renderHook(() => useStopAndDeleteConversation(), { wrapper });
    return { queryClient, rendered };
  }

  it("removes the deleted row from every cached list variant in place", async () => {
    const { queryClient, rendered } = seedAndDelete();

    rendered.result.current.mutate({ id: "conv_x" });
    await waitFor(() => expect(rendered.result.current.isSuccess).toBe(true));

    for (const includeArchived of [false, true]) {
      const data = queryClient.getQueryData<ConversationsInfiniteData>([
        "conversations",
        "",
        includeArchived,
      ]);
      // The deleted row must be gone from the cached pages themselves —
      // this splice is what makes the sidebar row disappear, since the
      // hook deliberately never refetches the list (see below).
      expect(data!.pages[0].data.find((c) => c.id === "conv_x")).toBeUndefined();
    }
    // Unrelated rows must survive the splice untouched.
    const base = queryClient.getQueryData<ConversationsInfiniteData>(["conversations", "", false]);
    expect(base!.pages[0].data.map((c) => c.id)).toEqual(["conv_other"]);

    // The project folder's own list is patched too, so a filed session
    // disappears from its folder without a refresh — its sibling stays.
    const folder = queryClient.getQueryData<ConversationsInfiniteData>([
      "project-sessions",
      "Sprint 42",
    ]);
    expect(folder!.pages[0].data.map((c) => c.id)).toEqual(["conv_sibling"]);
  });

  it("drops the backfill and session snapshot caches", async () => {
    const { queryClient, rendered } = seedAndDelete();

    rendered.result.current.mutate({ id: "conv_x" });
    await waitFor(() => expect(rendered.result.current.isSuccess).toBe(true));

    // The pinned-row backfill query remounts the moment the id leaves
    // the paginated pages; a still-fresh (staleTime 60s) cached entry
    // here would re-add the deleted session to the Pinned section until
    // a full page reload — the bug this eviction fixes.
    expect(queryClient.getQueryData(["conversation-backfill", "conv_x"])).toBeUndefined();
    // The open-chat snapshot must go too so a later visit to /c/{id}
    // can't render the deleted session from cache.
    expect(queryClient.getQueryData(["session", "conv_x"])).toBeUndefined();
  });

  it("does not refetch the conversations list, but does refresh the project list", async () => {
    const { queryClient, rendered } = seedAndDelete();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    rendered.result.current.mutate({ id: "conv_x" });
    await waitFor(() => expect(rendered.result.current.isSuccess).toBe(true));

    // An immediate conversations refetch races the server's async search-index
    // reindex of the delete and can resurrect the just-deleted row (the bug
    // this hook shape fixes) — so the list is patched in place, never
    // invalidated.
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: ["conversations"] });
    // The project list IS refreshed (DB-direct, no reindex race) so a project
    // emptied by the delete drops its now-empty folder without a reload.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["projects"] });
  });
});

describe("useRenameConversation cache patching", () => {
  function seedAndRename() {
    // The PATCH response carries the server-confirmed new title and
    // bumped updated_at.
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        id: "conv_x",
        object: "conversation",
        title: "New name",
        created_at: 0,
        updated_at: 200,
        labels: {},
      }),
    );
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    // Two list variants (default sidebar + archived view) plus the two
    // long-lived per-session caches the list patch doesn't cover.
    queryClient.setQueryData(
      ["conversations", "", false],
      infinitePage([conversation({ id: "conv_x" }), conversation({ id: "conv_other" })]),
    );
    queryClient.setQueryData(
      ["conversations", "", true],
      infinitePage([conversation({ id: "conv_x" })]),
    );
    queryClient.setQueryData(["conversation-backfill", "conv_x"], conversation({ id: "conv_x" }));
    queryClient.setQueryData(["session", "conv_x"], {
      id: "conv_x",
      agentId: "ag_1",
      agentName: null,
      status: "idle",
      createdAt: 0,
      title: "Old name",
      items: [],
      permissionLevel: null,
      parentSessionId: null,
      subAgentName: null,
    } satisfies Session);
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const rendered = renderHook(() => useRenameConversation(), { wrapper });
    return { queryClient, rendered };
  }

  it("patches the new title into every cached list variant in place", async () => {
    const { queryClient, rendered } = seedAndRename();

    rendered.result.current.mutate({ id: "conv_x", title: "New name" });
    await waitFor(() => expect(rendered.result.current.isSuccess).toBe(true));

    for (const includeArchived of [false, true]) {
      const data = queryClient.getQueryData<ConversationsInfiniteData>([
        "conversations",
        "",
        includeArchived,
      ]);
      const row = data!.pages[0].data.find((c) => c.id === "conv_x")!;
      // Title AND updated_at must both land: the title is what the user
      // sees; updated_at drives the sidebar's client-side sort and the
      // unseen tracker's baseline comparison.
      expect(row.title).toBe("New name");
      expect(row.updated_at).toBe(200);
    }
    // Unrelated rows must survive the patch untouched.
    const base = queryClient.getQueryData<ConversationsInfiniteData>(["conversations", "", false]);
    expect(base!.pages[0].data.find((c) => c.id === "conv_other")!.title).toBe("Old name");
  });

  it("patches the backfill and session snapshot caches", async () => {
    const { queryClient, rendered } = seedAndRename();

    rendered.result.current.mutate({ id: "conv_x", title: "New name" });
    await waitFor(() => expect(rendered.result.current.isSuccess).toBe(true));

    // staleTime 60s — without the patch a pinned row keeps the old
    // title for up to a minute.
    const backfill = queryClient.getQueryData<Conversation>(["conversation-backfill", "conv_x"]);
    expect(backfill!.title).toBe("New name");
    // staleTime Infinity — without the patch the open-chat header keeps
    // the old title until the next stream bind.
    const snapshot = queryClient.getQueryData<Session>(["session", "conv_x"]);
    expect(snapshot!.title).toBe("New name");
  });

  it("does not refetch the list (no invalidation)", async () => {
    const { queryClient, rendered } = seedAndRename();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    rendered.result.current.mutate({ id: "conv_x", title: "New name" });
    await waitFor(() => expect(rendered.result.current.isSuccess).toBe(true));

    // An immediate refetch races the server's search-index reindex of
    // the rename and can resurrect the old title (the bug this hook
    // shape fixes) — the only network call allowed is the PATCH itself.
    expect(invalidateSpy).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect((fetchMock.mock.calls[0] as [string, RequestInit])[1].method).toBe("PATCH");
  });
});

describe("useStopSession invalidation", () => {
  it("invalidates the conversations list AND the per-session snapshot", async () => {
    // The endpoint answers POST /v1/sessions/{id}/events → {queued:false}.
    fetchMock.mockResolvedValueOnce(mockResponse({ queued: false }));
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);

    const { result } = renderHook(() => useStopSession(), { wrapper });
    result.current.mutate("conv_x");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The list refresh keeps the sidebar badge current.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    // The snapshot refresh is what keeps the header's Stop gate correct:
    // the header merges snapshot fields OVER the list row, so a snapshot
    // left stale at the pre-stop state would clobber the now-stopped
    // state. Dropping this invalidation reintroduces the bug where the
    // header lagged (Stop lingering).
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["session", "conv_x"] });
  });
});

describe("useBulkArchiveConversations", () => {
  function renderBulkArchiveHook() {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const rendered = renderHook(() => useBulkArchiveConversations(), { wrapper });
    return { queryClient, invalidateSpy, rendered };
  }

  it("PATCHes each session and invalidates the list on success", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          id: "conv_a",
          object: "conversation",
          title: "A",
          created_at: 0,
          updated_at: 10,
          labels: {},
        }),
      )
      .mockResolvedValueOnce(
        mockResponse({
          id: "conv_b",
          object: "conversation",
          title: "B",
          created_at: 0,
          updated_at: 11,
          labels: {},
        }),
      );

    const { invalidateSpy, rendered } = renderBulkArchiveHook();
    rendered.result.current.mutate({ ids: ["conv_a", "conv_b"], archived: true });
    await waitFor(() => expect(rendered.result.current.isSuccess).toBe(true));

    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [, init] of fetchMock.mock.calls as [string, RequestInit][]) {
      expect(init.method).toBe("PATCH");
      expect(JSON.parse(init.body as string)).toEqual({ archived: true });
    }
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
  });

  it("throws with failed ids when some archives fail", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          id: "conv_a",
          object: "conversation",
          title: "A",
          created_at: 0,
          updated_at: 10,
          labels: {},
        }),
      )
      .mockResolvedValueOnce(mockResponse({}, { ok: false, status: 500 }));

    const { rendered } = renderBulkArchiveHook();
    rendered.result.current.mutate({ ids: ["conv_a", "conv_b"], archived: true });
    await waitFor(() => expect(rendered.result.current.isError).toBe(true));

    expect((rendered.result.current.error as any).failed).toEqual(["conv_b"]);
  });
});

describe("useBulkDeleteConversations", () => {
  function renderBulkDeleteHook() {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    queryClient.setQueryData(
      ["conversations", "", false],
      infinitePage([
        conversation({ id: "conv_a" }),
        conversation({ id: "conv_b" }),
        conversation({ id: "conv_keep" }),
      ]),
    );
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const rendered = renderHook(() => useBulkDeleteConversations(), { wrapper });
    return { queryClient, rendered };
  }

  it("stops and deletes each session, then removes them from cache", async () => {
    // For each id: stop (POST) then delete (DELETE) = 4 calls for 2 ids.
    fetchMock
      .mockResolvedValueOnce(mockResponse({ queued: false })) // stop conv_a
      .mockResolvedValueOnce(mockResponse({ deleted: true })) // delete conv_a
      .mockResolvedValueOnce(mockResponse({ queued: false })) // stop conv_b
      .mockResolvedValueOnce(mockResponse({ deleted: true })); // delete conv_b

    const { queryClient, rendered } = renderBulkDeleteHook();
    rendered.result.current.mutate(["conv_a", "conv_b"]);
    await waitFor(() => expect(rendered.result.current.isSuccess).toBe(true));

    const data = queryClient.getQueryData<ConversationsInfiniteData>(["conversations", "", false]);
    expect(data!.pages[0].data.map((c) => c.id)).toEqual(["conv_keep"]);
  });

  it("evicts succeeded ids from cache even when some deletes fail", async () => {
    // conv_a succeeds (stop+delete), conv_b fails on delete.
    fetchMock
      .mockResolvedValueOnce(mockResponse({ queued: false })) // stop conv_a
      .mockResolvedValueOnce(mockResponse({ deleted: true })) // delete conv_a
      .mockResolvedValueOnce(mockResponse({ queued: false })) // stop conv_b
      .mockResolvedValueOnce(mockResponse({}, { ok: false, status: 500 })); // delete conv_b fails

    const { queryClient, rendered } = renderBulkDeleteHook();
    rendered.result.current.mutate(["conv_a", "conv_b"]);
    await waitFor(() => expect(rendered.result.current.isError).toBe(true));

    // conv_a was successfully deleted and should be evicted; conv_b stays.
    const data = queryClient.getQueryData<ConversationsInfiniteData>(["conversations", "", false]);
    const ids = data!.pages[0].data.map((c) => c.id);
    expect(ids).not.toContain("conv_a");
    expect(ids).toContain("conv_b");
    expect(ids).toContain("conv_keep");
  });
});

describe("useBulkStopSessions", () => {
  function renderBulkStopHook() {
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const rendered = renderHook(() => useBulkStopSessions(), { wrapper });
    return { invalidateSpy, rendered };
  }

  it("POSTs stop_session for each id and invalidates the list", async () => {
    fetchMock
      .mockResolvedValueOnce(mockResponse({ queued: false }))
      .mockResolvedValueOnce(mockResponse({ queued: false }));

    const { invalidateSpy, rendered } = renderBulkStopHook();
    rendered.result.current.mutate(["conv_a", "conv_b"]);
    await waitFor(() => expect(rendered.result.current.isSuccess).toBe(true));

    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [url, init] of fetchMock.mock.calls as [string, RequestInit][]) {
      expect(url).toMatch(/\/v1\/sessions\/conv_[ab]\/events$/);
      expect(init.method).toBe("POST");
      expect(JSON.parse(init.body as string)).toEqual({ type: "stop_session", data: {} });
    }
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
  });

  it("throws with failed ids when some stops fail", async () => {
    fetchMock
      .mockResolvedValueOnce(mockResponse({ queued: false }))
      .mockResolvedValueOnce(mockResponse({}, { ok: false, status: 503 }));

    const { rendered } = renderBulkStopHook();
    rendered.result.current.mutate(["conv_a", "conv_b"]);
    await waitFor(() => expect(rendered.result.current.isError).toBe(true));

    const err = rendered.result.current.error as any;
    expect(err.succeeded).toEqual(["conv_a"]);
    expect(err.failed).toEqual(["conv_b"]);
  });
});

describe("useProjects", () => {
  it("GETs /v1/sessions/projects and returns the {id, name} project list", async () => {
    const projects = [
      { id: "p_x", name: "Customer X" },
      { id: null, name: "Sprint 42" },
    ];
    fetchMock.mockResolvedValueOnce(mockResponse(projects));

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useProjects(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/projects");
    expect(result.current.data).toEqual(projects);
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 500 }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useProjects(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("useProjectSessions", () => {
  it("does not fetch while disabled (collapsed folder)", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    renderHook(() => useProjectSessions("Sprint 42", false), { wrapper });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches the project's non-archived sessions, newest-first, when enabled", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        data: [{ id: "conv_a", object: "conversation", title: "A", created_at: 0, updated_at: 9 }],
        first_id: "conv_a",
        last_id: "conv_a",
        has_more: false,
      }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useProjectSessions("Sprint 42", true), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("/v1/sessions?");
    expect(url).toContain("project=Sprint+42");
    expect(url).toContain("order=desc");
    expect(url).toContain("sort_by=updated_at");
    expect(url).toContain("limit=20");
    // Folders show active sessions only — archived ones leave the sidebar.
    expect(url).not.toContain("include_archived");
    expect(result.current.data?.pages[0]?.data[0]?.id).toBe("conv_a");
  });
});

describe("useNewestProjectSession", () => {
  it("does not fetch without a project", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    renderHook(() => useNewestProjectSession(null), { wrapper });
    renderHook(() => useNewestProjectSession(""), { wrapper });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches one newest session and unwraps it from the page", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        data: [{ id: "conv_a", object: "conversation", title: "A", created_at: 0, updated_at: 9 }],
        first_id: "conv_a",
        last_id: "conv_a",
        has_more: true,
      }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useNewestProjectSession("Sprint 42"), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("/v1/sessions?");
    expect(url).toContain("project=Sprint+42");
    expect(url).toContain("order=desc");
    expect(url).toContain("sort_by=updated_at");
    expect(url).toContain("limit=1");
    expect(result.current.data?.id).toBe("conv_a");
  });

  it("resolves null for a project with no sessions", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ data: [], first_id: null, last_id: null, has_more: false }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useNewestProjectSession("Empty"), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
  });
});

describe("useMoveToProject", () => {
  it("resolves the project name to an id, then PATCHes project_id", async () => {
    // Filing by name first lists projects to resolve the id, then PATCHes.
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          object: "list",
          data: [{ id: "p_sprint", name: "Sprint 42" }],
        }),
      )
      .mockResolvedValueOnce(
        mockResponse({
          id: "conv_move",
          object: "conversation",
          title: "t",
          created_at: 0,
          updated_at: 1,
          project_id: "p_sprint",
        }),
      );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useMoveToProject(), { wrapper });

    result.current.mutate({ id: "conv_move", project: "Sprint 42" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock.mock.calls[0][0]).toBe("/v1/projects");
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_move");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({
      project_id: "p_sprint",
      labels: { omni_project: "" },
    });
  });

  it("creates the project on demand when the name has no first-class row", async () => {
    fetchMock
      // No project of this name exists yet → list is empty …
      .mockResolvedValueOnce(mockResponse({ object: "list", data: [] }))
      // … so it's created …
      .mockResolvedValueOnce(mockResponse({ id: "p_new", object: "project", name: "Fresh" }))
      // … then the session is filed under the new id.
      .mockResolvedValueOnce(
        mockResponse({
          id: "conv_move",
          object: "conversation",
          title: "t",
          created_at: 0,
          updated_at: 1,
          project_id: "p_new",
        }),
      );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useMoveToProject(), { wrapper });

    result.current.mutate({ id: "conv_move", project: "Fresh" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [createUrl, createInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(createUrl).toBe("/v1/projects");
    expect(createInit.method).toBe("POST");
    expect(JSON.parse(createInit.body as string)).toEqual({ name: "Fresh" });
    const [patchUrl, patchInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(patchUrl).toBe("/v1/sessions/conv_move");
    expect(JSON.parse(patchInit.body as string)).toEqual({
      project_id: "p_new",
      labels: { omni_project: "" },
    });
  });

  it("unfiles with project_id='' (no id resolution) and invalidates the lists", async () => {
    // Unfiling clears membership directly — no project lookup needed.
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        id: "conv_move",
        object: "conversation",
        title: "t",
        created_at: 0,
        updated_at: 1,
        project_id: null,
      }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useMoveToProject(), { wrapper });

    result.current.mutate({ id: "conv_move", project: "" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_move");
    expect(JSON.parse(init.body as string)).toEqual({
      project_id: "",
      labels: { omni_project: "" },
    });

    // Both keys must refresh: conversations so the row re-groups into its new
    // section, projects so the sidebar list updates.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["projects"] });
  });
});

describe("useArchiveConversation", () => {
  it("PATCHes archived and invalidates both the conversations and projects queries", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        id: "conv_a",
        object: "conversation",
        title: "A",
        created_at: 0,
        updated_at: 10,
        labels: { omni_project: "Sprint 42" },
      }),
    );
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useArchiveConversation(), { wrapper });

    result.current.mutate({ id: "conv_a", archived: true });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_a");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ archived: true });
    // Projects must refresh too: archiving the last live member of a project
    // removes its folder; unarchiving restores it.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["projects"] });
  });
});

describe("useDeleteProject", () => {
  function archivedConv(id: string) {
    return mockResponse({
      id,
      object: "conversation",
      title: id,
      created_at: 0,
      updated_at: 10,
      archived: true,
      labels: { omni_project: "Sprint 42" },
    });
  }

  it("archives + unfiles every member, then deletes the first-class project", async () => {
    // 1st call: page of project members. Then one PATCH per member, then the
    // DELETE of the first-class container.
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          data: [
            { id: "conv_a", object: "conversation", title: "A", created_at: 0, updated_at: 1 },
            { id: "conv_b", object: "conversation", title: "B", created_at: 0, updated_at: 2 },
          ],
          first_id: "conv_a",
          last_id: "conv_b",
          has_more: false,
        }),
      )
      .mockResolvedValueOnce(archivedConv("conv_a"))
      .mockResolvedValueOnce(archivedConv("conv_b"))
      .mockResolvedValueOnce(mockResponse({ id: "p_1", object: "project.deleted", deleted: true }));

    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useDeleteProject(), { wrapper });

    result.current.mutate({ id: "p_1", name: "Sprint 42" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The list fetch is filtered by project and includes archived members.
    const listUrl = fetchMock.mock.calls[0][0] as string;
    expect(listUrl).toContain("project=Sprint+42");
    expect(listUrl).toContain("include_archived=true");

    // Each member is archived AND detached (project_id cleared + label removed)
    // via PATCH — never deleted.
    const patches = (fetchMock.mock.calls.slice(1, 3) as [string, RequestInit][]).map(
      ([url, init]) => ({ url, init }),
    );
    expect(patches.map((p) => p.url).sort()).toEqual([
      "/v1/sessions/conv_a",
      "/v1/sessions/conv_b",
    ]);
    for (const { init } of patches) {
      expect(init.method).toBe("PATCH");
      expect(JSON.parse(init.body as string)).toEqual({
        archived: true,
        project_id: "",
        labels: { omni_project: "" },
      });
    }

    // Finally the first-class container is removed.
    const [delUrl, delInit] = fetchMock.mock.calls[3] as [string, RequestInit];
    expect(delUrl).toBe("/v1/projects/p_1");
    expect(delInit.method).toBe("DELETE");

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["projects"] });
  });

  it("throws with succeeded/failed split when some archives fail", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          data: [
            { id: "conv_a", object: "conversation", title: "A", created_at: 0, updated_at: 1 },
            { id: "conv_b", object: "conversation", title: "B", created_at: 0, updated_at: 2 },
          ],
          first_id: "conv_a",
          last_id: "conv_b",
          has_more: false,
        }),
      )
      .mockResolvedValueOnce(archivedConv("conv_a"))
      .mockResolvedValueOnce(mockResponse({}, { ok: false, status: 403 }));

    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useDeleteProject(), { wrapper });

    result.current.mutate({ id: "p_1", name: "Sprint 42" });
    await waitFor(() => expect(result.current.isError).toBe(true));

    const err = result.current.error as unknown as {
      failed: string[];
      succeeded: string[];
      total: number;
    };
    expect(err.failed).toEqual(["conv_b"]);
    expect(err.succeeded).toEqual(["conv_a"]);
    expect(err.total).toBe(2);
  });
});
