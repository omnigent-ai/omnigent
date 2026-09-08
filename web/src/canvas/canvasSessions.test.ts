import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import * as identity from "@/lib/identity";
import {
  applyLiveRows,
  cachedSessionPreview,
  INITIAL_SESSION_PAGE_LIMIT,
  loadAllSessions,
  SESSION_PAGE_LIMIT,
  SESSION_POLL_INTERVAL_MS,
  useCanvasSessions,
} from "./canvasSessions";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

function session(
  id: string,
  updatedAt: number,
  overrides: Partial<Conversation> = {},
): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 1,
    updated_at: updatedAt,
    labels: {},
    permission_level: null,
    status: "idle",
    ...overrides,
  };
}

function page(data: Conversation[], lastId: string | null, hasMore: boolean): ConversationsPage {
  return { data, first_id: data[0]?.id ?? null, last_id: lastId, has_more: hasMore };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Query strings of every list request so far. */
function requestedQueries(): URLSearchParams[] {
  return vi
    .mocked(identity.authenticatedFetch)
    .mock.calls.map(([path]) => new URLSearchParams(String(path).split("?")[1]));
}

beforeEach(() => {
  vi.mocked(identity.authenticatedFetch).mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("loadAllSessions", () => {
  it("takes a quick first page without a preview, then full pages, and returns only server rows", async () => {
    vi.mocked(identity.authenticatedFetch)
      .mockResolvedValueOnce(jsonResponse(page([session("a", 9), session("b", 8)], "b", true)))
      .mockResolvedValueOnce(jsonResponse(page([session("c", 7)], null, false)));
    const progress: { ids: string[]; hasMore: boolean }[] = [];

    const result = await loadAllSessions([], (update) =>
      progress.push({ ids: update.sessions.map((row) => row.id), hasMore: update.hasMore }),
    );

    const queries = requestedQueries();
    expect(queries.map((query) => query.get("limit"))).toEqual([
      String(INITIAL_SESSION_PAGE_LIMIT),
      String(SESSION_PAGE_LIMIT),
    ]);
    expect(queries[1].get("after")).toBe("b");
    expect(queries[0].get("kind")).toBe("default");
    expect(queries[0].get("sort_by")).toBe("updated_at");
    expect(progress).toEqual([
      { ids: ["a", "b"], hasMore: true },
      { ids: ["a", "b", "c"], hasMore: false },
    ]);
    expect(result.map((row) => row.id)).toEqual(["a", "b", "c"]);
  });

  it("starts with a full page when a preview exists and keeps the preview merged until complete", async () => {
    const preview = [session("p", 5), session("a", 9)];
    vi.mocked(identity.authenticatedFetch)
      .mockResolvedValueOnce(jsonResponse(page([session("a", 9)], "a", true)))
      .mockResolvedValueOnce(jsonResponse(page([session("z", 1)], null, false)));
    const progress: string[][] = [];

    const result = await loadAllSessions(preview, (update) =>
      progress.push(update.sessions.map((row) => row.id)),
    );

    expect(requestedQueries()[0].get("limit")).toBe(String(SESSION_PAGE_LIMIT));
    // Mid-load the preview's "p" is still present; the final set drops it.
    expect(progress).toEqual([
      ["p", "a"],
      ["a", "z"],
    ]);
    expect(result.map((row) => row.id)).toEqual(["a", "z"]);
  });

  it("drops archived and child rows and rejects a missing or repeated cursor", async () => {
    vi.mocked(identity.authenticatedFetch).mockResolvedValueOnce(
      jsonResponse(
        page(
          [
            session("keep", 3),
            session("archived", 2, { archived: true }),
            session("child", 1, { parent_session_id: "keep" }),
          ],
          null,
          false,
        ),
      ),
    );
    expect((await loadAllSessions([], () => undefined)).map((row) => row.id)).toEqual(["keep"]);

    vi.mocked(identity.authenticatedFetch).mockResolvedValueOnce(
      jsonResponse(page([session("a", 1)], null, true)),
    );
    await expect(loadAllSessions([], () => undefined)).rejects.toThrow("no next cursor");

    vi.mocked(identity.authenticatedFetch)
      .mockResolvedValueOnce(jsonResponse(page([session("a", 1)], "a", true)))
      .mockResolvedValueOnce(jsonResponse(page([session("b", 1)], "a", true)));
    await expect(loadAllSessions([], () => undefined)).rejects.toThrow("repeated cursor");
  });

  it("surfaces a failed page as an error", async () => {
    vi.mocked(identity.authenticatedFetch).mockResolvedValueOnce(jsonResponse({}, 503));
    await expect(loadAllSessions([], () => undefined)).rejects.toThrow("503");
  });
});

describe("cachedSessionPreview", () => {
  it("reads top-level active rows from the sidebar cache, newest first, capped", () => {
    const client = new QueryClient();
    client.setQueryData(["conversations", "", true], {
      pageParams: [undefined],
      pages: [
        page(
          [
            session("old", 1),
            session("new", 3),
            session("archived", 9, { archived: true }),
            session("child", 8, { parent_session_id: "new" }),
          ],
          null,
          false,
        ),
      ],
    });
    expect(cachedSessionPreview(client).map((row) => row.id)).toEqual(["new", "old"]);
    expect(cachedSessionPreview(client, 1).map((row) => row.id)).toEqual(["new"]);
  });
});

describe("applyLiveRows", () => {
  it("takes fresher live rows, ignores older ones, and drops rows that became archived", () => {
    const stale = session("a", 5, { status: "idle", title: null });
    const untouched = session("b", 7);
    const olderInCache = session("c", 9);
    const live = new Map([
      ["a", session("a", 5, { status: "running", title: "Named" })],
      ["b", session("b", 7)],
      ["c", session("c", 3, { status: "running" })],
      ["d", session("d", 8, { archived: true })],
    ]);
    const next = applyLiveRows([stale, untouched, olderInCache, session("d", 8)], live);
    expect(next.map((row) => [row.id, row.status, row.title])).toEqual([
      ["a", "running", "Named"],
      ["b", "idle", "b"],
      ["c", "idle", "c"],
    ]);
  });

  it("returns an equal list when nothing renders differently", () => {
    const rows = [session("a", 5)];
    expect(applyLiveRows(rows, new Map([["a", session("a", 5)]]))).toEqual(rows);
  });
});

describe("useCanvasSessions", () => {
  function wrapper(client: QueryClient) {
    return ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
  }

  it("paints the cached preview at once, then replaces it with the canonical list", async () => {
    const client = new QueryClient();
    client.setQueryData(["conversations", "", true], {
      pageParams: [undefined],
      pages: [page([session("cached", 5)], null, false)],
    });
    let resolvePage!: (value: Response) => void;
    vi.mocked(identity.authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolvePage = resolve;
      }),
    );

    const { result } = renderHook(() => useCanvasSessions(), { wrapper: wrapper(client) });

    expect(result.current.loaded).toBe(true);
    expect(result.current.sessions.map((row) => row.id)).toEqual(["cached"]);
    await waitFor(() => expect(result.current.loadingMore).toBe(true));
    expect(result.current.complete).toBe(false);

    await act(async () => {
      resolvePage(jsonResponse(page([session("fresh", 9)], null, false)));
    });
    await waitFor(() => expect(result.current.complete).toBe(true));
    expect(result.current.sessions.map((row) => row.id)).toEqual(["fresh"]);
    expect(result.current.loadingMore).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("mirrors a stream patch to the sidebar cache without re-fetching", async () => {
    const client = new QueryClient();
    const key = ["conversations", "", true];
    client.setQueryData(key, {
      pageParams: [undefined],
      pages: [page([session("s", 5, { title: null })], null, false)],
    });
    vi.mocked(identity.authenticatedFetch).mockResolvedValueOnce(
      jsonResponse(page([session("s", 5, { title: null })], null, false)),
    );
    const { result } = renderHook(() => useCanvasSessions(), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.complete).toBe(true));
    expect(result.current.sessions[0]).toMatchObject({ status: "idle", title: null });

    // What SessionUpdatesProvider does when the stream reports the session running.
    act(() => {
      client.setQueryData(key, {
        pageParams: [undefined],
        pages: [page([session("s", 5, { status: "running", title: "Ugh" })], null, false)],
      });
    });
    await waitFor(() =>
      expect(result.current.sessions[0]).toMatchObject({ status: "running", title: "Ugh" }),
    );
    expect(identity.authenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it("polls again after each interval while the page is visible", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(identity.authenticatedFetch).mockImplementation(async () =>
      jsonResponse(page([], null, false)),
    );
    const { result } = renderHook(() => useCanvasSessions(), {
      wrapper: wrapper(new QueryClient()),
    });
    await waitFor(() => expect(result.current.complete).toBe(true));
    expect(identity.authenticatedFetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(SESSION_POLL_INTERVAL_MS + 50);
    });
    expect(identity.authenticatedFetch).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SESSION_POLL_INTERVAL_MS + 50);
    });
    expect(identity.authenticatedFetch).toHaveBeenCalledTimes(3);
  });

  it("reports an initial failure and keeps cards through a failed refresh on focus", async () => {
    const client = new QueryClient();
    vi.mocked(identity.authenticatedFetch)
      .mockResolvedValueOnce(jsonResponse({}, 500))
      .mockResolvedValueOnce(jsonResponse(page([session("a", 1)], null, false)))
      .mockResolvedValueOnce(jsonResponse({}, 502));

    const { result } = renderHook(() => useCanvasSessions(), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.error).toContain("500"));
    expect(result.current.loaded).toBe(true);
    expect(result.current.sessions).toEqual([]);

    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.sessions.map((row) => row.id)).toEqual(["a"]);
    expect(result.current.error).toBeNull();

    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.error).toContain("502"));
    expect(result.current.sessions.map((row) => row.id)).toEqual(["a"]);
    expect(result.current.loadingMore).toBe(false);
    expect(identity.authenticatedFetch).toHaveBeenCalledTimes(3);
  });
});
