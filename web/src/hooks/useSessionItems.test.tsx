// The execution-logs pager's stale-cursor recovery: an item deleted between
// two scroll pages must reload the panel from the top, not replace it with
// the server's raw 400.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchSessionItemsPage, useSessionItems } from "./useSessionItems";
import { ApiError } from "@/lib/sessionsApi";

const fetchMock = vi.fn();

function jsonResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

function itemsPage(ids: string[], hasMore: boolean) {
  return {
    object: "list",
    data: ids.map((id) => ({ id })),
    first_id: ids[0] ?? null,
    last_id: ids.at(-1) ?? null,
    has_more: hasMore,
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function wrapperFor(queryClient: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

describe("fetchSessionItemsPage", () => {
  it("surfaces the server's stale_cursor code, not a bare status line", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        { error: { code: "stale_cursor", message: "cursor 'msg_1' no longer exists" } },
        { ok: false, status: 400 },
      ),
    );

    await expect(fetchSessionItemsPage("conv_1", "msg_1")).rejects.toMatchObject({
      code: "stale_cursor",
      status: 400,
    });
  });
});

describe("useSessionItems stale cursor", () => {
  it("reloads from the first page when a scroll cursor goes stale", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(itemsPage(["msg_1"], true)))
      .mockResolvedValueOnce(
        jsonResponse(
          { error: { code: "stale_cursor", message: "gone" } },
          {
            ok: false,
            status: 400,
          },
        ),
      )
      .mockResolvedValue(jsonResponse(itemsPage(["msg_2"], false)));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    const { result } = renderHook(() => useSessionItems("conv_1"), {
      wrapper: wrapperFor(queryClient),
    });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    await act(async () => {
      result.current.fetchNextPage();
    });

    await waitFor(() => expect(result.current.error).toBeNull());
    // Exactly the post-deletion first page — neither truncated nor doubled.
    await waitFor(() => expect(result.current.items.map((i) => i.id)).toEqual(["msg_2"]));
    expect(fetchMock.mock.calls.at(-1)![0] as string).not.toContain("after=");
  });

  it("does not restart on an unrelated error", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        { error: { code: "server_error", message: "boom" } },
        {
          ok: false,
          status: 500,
        },
      ),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    const { result } = renderHook(() => useSessionItems("conv_1"), {
      wrapper: wrapperFor(queryClient),
    });

    await waitFor(() => expect(result.current.error).toBeInstanceOf(ApiError));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
