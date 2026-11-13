import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { getSessionHost, getSessionParent } from "@/lib/sessionHost";
import { findCachedListRow } from "@/lib/sessionListCache";
import { conversation } from "@/test/sidebarMockHelpers";
import {
  fetchConversationById,
  rememberOpenSessionRow,
  useOpenSessionRow,
} from "./useOpenSessionRow";

function mockResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 404 ? "Not Found" : "OK",
    json: async () => body,
  } as unknown as Response;
}

function listPages(rows: Conversation[]) {
  return {
    pages: [{ data: rows, first_id: null, last_id: null, has_more: false }],
    pageParams: [],
  };
}

// Sidebar list caches: the Archived view, and the Mine and Shared scopes.
const ARCHIVED_LIST_KEY = ["conversations", "", true];
const MINE_LIST_KEY = ["conversations", "", false, null, "mine"];
const SHARED_LIST_KEY = ["conversations", "", false, null, "shared"];

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderOpenSessionRow(queryClient: QueryClient, id: string | null) {
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
  return renderHook(() => useOpenSessionRow(id), { wrapper });
}

function newQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

describe("findCachedListRow", () => {
  it("finds a row in a sidebar list cache or a project folder cache", () => {
    const queryClient = newQueryClient();
    const listRow = conversation("conv_list", { owner: "me" });
    const folderRow = conversation("conv_folder", { owner: "me" });
    queryClient.setQueryData(ARCHIVED_LIST_KEY, {
      pages: [listPages([]).pages[0], listPages([listRow]).pages[0]],
      pageParams: [],
    });
    queryClient.setQueryData(["project-sessions", "Customer X"], listPages([folderRow]));

    expect(findCachedListRow(queryClient, "conv_list")).toBe(listRow);
    expect(findCachedListRow(queryClient, "conv_folder")).toBe(folderRow);
    expect(findCachedListRow(queryClient, "conv_missing")).toBeUndefined();
  });
});

describe("fetchConversationById", () => {
  it("maps the session response to a list row without items or usage", async () => {
    const family = { id: "family_1", representative_id: "conv_sibling", pinned: false };
    fetchMock.mockResolvedValue(
      mockResponse({
        id: "conv_old",
        title: "Old",
        created_at: 10,
        updated_at: 20,
        archived: true,
        family,
        project_id: "proj_1",
        parent_session_id: "conv_parent",
        host_id: "host_1",
        status: "idle",
        pending_elicitations: [{ type: "elicitation" }, { type: "elicitation" }],
      }),
    );

    const row = await fetchConversationById("conv_old");

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("/v1/sessions/conv_old?");
    expect(url).toContain("include_items=false");
    expect(url).toContain("include_usage=false");
    expect(row).toMatchObject({
      id: "conv_old",
      title: "Old",
      archived: true,
      family,
      project_id: "proj_1",
      parent_session_id: "conv_parent",
      host_id: "host_1",
      owner: null,
      permission_level: null,
      pending_elicitations_count: 2,
    });
    // Session-scoped requests route through these maps before any list row loads.
    expect(getSessionParent("conv_old")).toBe("conv_parent");
    expect(getSessionHost("conv_old")).toBe("host_1");
    const client = newQueryClient();
    rememberOpenSessionRow(client, conversation("conv_old", { owner: "owner" }));
    expect(await fetchConversationById("conv_old", client)).toMatchObject({ owner: "owner" });
  });

  it("returns null for a deleted session", async () => {
    fetchMock.mockResolvedValue(mockResponse({ detail: "missing" }, 404));
    await expect(fetchConversationById("conv_gone")).resolves.toBeNull();
  });
});

describe("useOpenSessionRow", () => {
  it.each([
    { viewer: "owned", owner: "me", permissionLevel: null, listKey: ARCHIVED_LIST_KEY },
    { viewer: "shared", owner: "teammate", permissionLevel: 1, listKey: SHARED_LIST_KEY },
  ])(
    "reuses a cached $viewer list row without fetching",
    async ({ owner, permissionLevel, listKey }) => {
      const queryClient = newQueryClient();
      const listRow = conversation("conv_listed", {
        archived: true,
        owner,
        permission_level: permissionLevel,
      });
      queryClient.setQueryData(listKey, listPages([listRow]));

      const { result } = renderOpenSessionRow(queryClient, "conv_listed");

      await waitFor(() => expect(result.current).toEqual(listRow));
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("follows status updates to the cached list row", async () => {
    const queryClient = newQueryClient();
    queryClient.setQueryData(
      MINE_LIST_KEY,
      listPages([conversation("conv_live", { owner: "me" })]),
    );
    const { result } = renderOpenSessionRow(queryClient, "conv_live");
    await waitFor(() => expect(result.current?.status).toBe("idle"));

    const running = conversation("conv_live", { owner: "me", status: "running" });
    act(() => queryClient.setQueryData(MINE_LIST_KEY, listPages([running])));

    expect(result.current?.status).toBe("running");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches the session when no cache holds its row", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({ id: "conv_direct", created_at: 1, archived: true, permission_level: null }),
    );
    const { result } = renderOpenSessionRow(newQueryClient(), "conv_direct");

    await waitFor(() => expect(result.current?.id).toBe("conv_direct"));
    expect(result.current?.archived).toBe(true);
  });

  it("hides a fetched shared session whose owner is unknown", async () => {
    // permission_level 1 (read) without an owner would otherwise list as the viewer's own.
    fetchMock.mockResolvedValue(
      mockResponse({ id: "conv_shared", created_at: 1, permission_level: 1 }),
    );
    const queryClient = newQueryClient();
    const { result } = renderOpenSessionRow(queryClient, "conv_shared");

    await waitFor(() =>
      expect(queryClient.getQueryData(["conversation-backfill", "conv_shared"])).toBeTruthy(),
    );
    expect(result.current).toBeNull();
  });

  it("lists a remembered shared row after its search cache is gone, and keeps its owner on refetch", async () => {
    const queryClient = newQueryClient();
    const sharedRow = conversation("conv_shared", {
      owner: "teammate",
      permission_level: 1,
      archived: true,
    });
    rememberOpenSessionRow(queryClient, sharedRow);

    const { result } = renderOpenSessionRow(queryClient, "conv_shared");

    expect(result.current).toEqual(sharedRow);
    expect(fetchMock).not.toHaveBeenCalled();

    fetchMock.mockResolvedValue(
      mockResponse({ id: "conv_shared", created_at: 1, permission_level: 1, archived: false }),
    );
    await act(() => queryClient.invalidateQueries({ queryKey: ["conversation-backfill"] }));

    await waitFor(() => expect(result.current?.archived).toBe(false));
    expect(result.current?.owner).toBe("teammate");
  });

  it("keeps a shared row's owner when its list drops the row and it is fetched", async () => {
    const queryClient = newQueryClient();
    const sharedRow = conversation("conv_shared", {
      owner: "teammate",
      permission_level: 1,
      archived: true,
    });
    queryClient.setQueryData(SHARED_LIST_KEY, listPages([sharedRow]));
    const { result } = renderOpenSessionRow(queryClient, "conv_shared");
    await waitFor(() => expect(result.current).toEqual(sharedRow));

    fetchMock.mockResolvedValue(
      mockResponse({ id: "conv_shared", created_at: 1, permission_level: 1, archived: false }),
    );
    act(() => queryClient.setQueryData(SHARED_LIST_KEY, listPages([])));

    await waitFor(() => expect(result.current?.archived).toBe(false));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current?.owner).toBe("teammate");
  });

  it("does nothing without an id", () => {
    const { result } = renderOpenSessionRow(newQueryClient(), null);
    expect(result.current).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
