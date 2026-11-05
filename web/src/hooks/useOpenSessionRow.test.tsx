import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { getSessionParent } from "@/lib/sessionHost";
import { findCachedListRow } from "@/lib/sessionListCache";
import { conversation } from "@/test/sidebarMockHelpers";
import { fetchConversationById, useOpenSessionRow } from "./useOpenSessionRow";

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
  it("finds a row in a search cache or a project folder cache", () => {
    const queryClient = newQueryClient();
    const searchRow = conversation("conv_search", { owner: "me" });
    const folderRow = conversation("conv_folder", { owner: "me" });
    queryClient.setQueryData(["conversations", "search", "deploy"], listPages([searchRow]));
    queryClient.setQueryData(["project-sessions", "Customer X"], listPages([folderRow]));

    expect(findCachedListRow(queryClient, "conv_search")).toBe(searchRow);
    expect(findCachedListRow(queryClient, "conv_folder")).toBe(folderRow);
    expect(findCachedListRow(queryClient, "conv_missing")).toBeUndefined();
  });
});

describe("fetchConversationById", () => {
  it("maps the session response to a list row without items or usage", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        id: "conv_old",
        title: "Old",
        created_at: 10,
        updated_at: 20,
        archived: true,
        project_id: "proj_1",
        parent_session_id: null,
        status: "idle",
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
      project_id: "proj_1",
      owner: null,
      permission_level: null,
    });
    expect(getSessionParent("conv_old")).toBeNull();
  });

  it("returns null for a deleted session", async () => {
    fetchMock.mockResolvedValue(mockResponse({ detail: "missing" }, 404));
    await expect(fetchConversationById("conv_gone")).resolves.toBeNull();
  });
});

describe("useOpenSessionRow", () => {
  it("reuses the row from the search results the session was opened from", async () => {
    const queryClient = newQueryClient();
    const searchRow = conversation("conv_archived", { archived: true, owner: "me" });
    queryClient.setQueryData(["conversations", "search", "old"], listPages([searchRow]));

    const { result } = renderOpenSessionRow(queryClient, "conv_archived");

    await waitFor(() => expect(result.current).toEqual(searchRow));
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

  it("keeps a shared search row, which carries its owner", async () => {
    const queryClient = newQueryClient();
    const sharedRow = conversation("conv_shared", { owner: "teammate", permission_level: 1 });
    queryClient.setQueryData(["conversations", "search", "x"], listPages([sharedRow]));

    const { result } = renderOpenSessionRow(queryClient, "conv_shared");

    await waitFor(() => expect(result.current).toEqual(sharedRow));
  });

  it("does nothing without an id", () => {
    const { result } = renderOpenSessionRow(newQueryClient(), null);
    expect(result.current).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
