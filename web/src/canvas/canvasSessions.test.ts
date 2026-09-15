import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import * as host from "@/lib/host";
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

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
  getCurrentUserId: vi.fn(() => null),
  resolveIdentity: vi.fn(async () => null),
}));

vi.mock("@/lib/host", () => ({
  getOmnigentServerIdentity: vi.fn(() => "server-a"),
}));

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

function resolveViewer(viewerId = "me"): void {
  vi.mocked(identity.getCurrentUserId).mockReturnValue(viewerId);
  vi.mocked(identity.resolveIdentity).mockResolvedValue(viewerId);
}

beforeEach(() => {
  vi.mocked(host.getOmnigentServerIdentity).mockReset().mockReturnValue("server-a");
  vi.mocked(identity.authenticatedFetch).mockReset();
  vi.mocked(identity.getCurrentUserId).mockReset().mockReturnValue(null);
  vi.mocked(identity.resolveIdentity).mockReset().mockResolvedValue(null);
  window.sessionStorage.clear();
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
    expect(result.current.networkConfirmed).toBe(true);
    expect(result.current.sessions.map((row) => row.id)).toEqual(["fresh"]);
    expect(result.current.loadingMore).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("paints the last complete list at once on a later visit and refreshes quietly", async () => {
    resolveViewer();
    const client = new QueryClient();
    vi.mocked(identity.authenticatedFetch).mockResolvedValueOnce(
      jsonResponse(page([session("a", 9), session("b", 8)], null, false)),
    );
    const first = renderHook(() => useCanvasSessions(), { wrapper: wrapper(client) });
    await waitFor(() => expect(first.result.current.complete).toBe(true));
    first.unmount();

    let resolvePage!: (value: Response) => void;
    vi.mocked(identity.authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolvePage = resolve;
      }),
    );
    const second = renderHook(() => useCanvasSessions(), { wrapper: wrapper(client) });

    // Every card from the previous visit is there before the refresh answers.
    expect(second.result.current.sessions.map((row) => row.id)).toEqual(["a", "b"]);
    expect(second.result.current).toMatchObject({
      loaded: true,
      complete: true,
      networkConfirmed: false,
    });
    await waitFor(() => expect(identity.authenticatedFetch).toHaveBeenCalledTimes(2));
    expect(second.result.current.loadingMore).toBe(false);
    // A remembered list means the refresh starts with a full page.
    expect(requestedQueries()[1].get("limit")).toBe(String(SESSION_PAGE_LIMIT));

    await act(async () => {
      resolvePage(jsonResponse(page([session("a", 10)], null, false)));
    });
    await waitFor(() => expect(second.result.current.sessions.map((row) => row.id)).toEqual(["a"]));
    expect(second.result.current.networkConfirmed).toBe(true);
  });

  it("does not reuse an in-memory list after switching servers for the same viewer", async () => {
    resolveViewer();
    const client = new QueryClient();
    vi.mocked(identity.authenticatedFetch).mockResolvedValueOnce(
      jsonResponse(page([session("same", 9)], null, false)),
    );
    const first = renderHook(() => useCanvasSessions(), { wrapper: wrapper(client) });
    await waitFor(() => expect(first.result.current.complete).toBe(true));
    first.unmount();

    vi.mocked(host.getOmnigentServerIdentity).mockReturnValue("server-b");
    let resolvePage!: (value: Response) => void;
    vi.mocked(identity.authenticatedFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolvePage = resolve;
      }),
    );
    const second = renderHook(() => useCanvasSessions(), { wrapper: wrapper(client) });

    expect(second.result.current).toMatchObject({ sessions: [], complete: false });
    await waitFor(() => expect(identity.authenticatedFetch).toHaveBeenCalledTimes(2));
    await act(async () => {
      resolvePage(jsonResponse(page([session("same", 9)], null, false)));
    });
    await waitFor(() => expect(second.result.current.networkConfirmed).toBe(true));

    const keys = Array.from({ length: window.sessionStorage.length }, (_, index) =>
      window.sessionStorage.key(index),
    );
    expect(keys).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/:server-a:me$/),
        expect.stringMatching(/:server-b:me$/),
      ]),
    );
  });

  it("keeps a load that finishes after Canvas unmounts for the next visit", async () => {
    resolveViewer();
    const client = new QueryClient();
    let resolveLastPage!: (value: Response) => void;
    vi.mocked(identity.authenticatedFetch)
      .mockResolvedValueOnce(jsonResponse(page([session("a", 9)], "a", true)))
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveLastPage = resolve;
        }),
      );

    const first = renderHook(() => useCanvasSessions(), { wrapper: wrapper(client) });
    await waitFor(() => expect(first.result.current.sessions.map((row) => row.id)).toEqual(["a"]));
    expect(first.result.current.complete).toBe(false);
    first.unmount();

    await act(async () => {
      resolveLastPage(jsonResponse(page([session("b", 8)], null, false)));
      await Promise.resolve();
    });

    // Keep the revisit refresh pending so this assertion only observes the cache.
    vi.mocked(identity.authenticatedFetch).mockReturnValueOnce(new Promise<Response>(() => {}));
    const second = renderHook(() => useCanvasSessions(), { wrapper: wrapper(client) });
    expect(second.result.current.sessions.map((row) => row.id)).toEqual(["a", "b"]);
    expect(second.result.current).toMatchObject({
      loaded: true,
      complete: true,
      networkConfirmed: false,
    });
  });

  it("restores the complete list from session storage after a page reload", async () => {
    resolveViewer();
    const firstClient = new QueryClient();
    const liveShaped = session("a", 9);
    delete (liveShaped as Partial<Conversation>).object;
    delete (liveShaped as Partial<Conversation>).status;
    vi.mocked(identity.authenticatedFetch).mockResolvedValueOnce(
      jsonResponse(page([liveShaped, session("b", 8)], null, false)),
    );
    const first = renderHook(() => useCanvasSessions(), { wrapper: wrapper(firstClient) });
    await waitFor(() => expect(first.result.current.complete).toBe(true));
    first.unmount();

    // A new QueryClient models a full page reload; keep its refresh pending so
    // the assertion can only pass from the persisted complete-list cache.
    vi.mocked(identity.authenticatedFetch).mockReturnValueOnce(new Promise<Response>(() => {}));
    const second = renderHook(() => useCanvasSessions(), {
      wrapper: wrapper(new QueryClient()),
    });
    expect(second.result.current.sessions.map((row) => row.id)).toEqual(["a", "b"]);
    expect(second.result.current).toMatchObject({
      loaded: true,
      complete: true,
      networkConfirmed: false,
    });
  });

  it("waits for identity before reading a viewer-scoped reload cache", async () => {
    resolveViewer();
    const firstClient = new QueryClient();
    vi.mocked(identity.authenticatedFetch).mockResolvedValueOnce(
      jsonResponse(page([session("a", 9), session("b", 8)], null, false)),
    );
    const first = renderHook(() => useCanvasSessions(), { wrapper: wrapper(firstClient) });
    await waitFor(() => expect(first.result.current.complete).toBe(true));
    first.unmount();

    vi.mocked(identity.getCurrentUserId).mockReturnValue(null);
    vi.mocked(identity.resolveIdentity).mockResolvedValue("me");
    const reloadedClient = new QueryClient();
    reloadedClient.setQueryData(["conversations", "", true], {
      pageParams: [undefined],
      pages: [page([session("preview", 10)], null, false)],
    });
    vi.mocked(identity.authenticatedFetch).mockReturnValueOnce(new Promise<Response>(() => {}));
    const reloaded = renderHook(() => useCanvasSessions(), {
      wrapper: wrapper(reloadedClient),
    });

    // Do not flash the short preview while the persisted cache's viewer resolves.
    expect(reloaded.result.current).toMatchObject({ sessions: [], loaded: false });
    await waitFor(() =>
      expect(reloaded.result.current.sessions.map((row) => row.id)).toEqual(["a", "b"]),
    );
    expect(reloaded.result.current).toMatchObject({
      loaded: true,
      complete: true,
      networkConfirmed: false,
    });
  });

  it("resolves identity before fetching and never stores an authenticated list as anonymous", async () => {
    let finishIdentity!: (viewerId: string) => void;
    vi.mocked(identity.resolveIdentity).mockReturnValueOnce(
      new Promise<string>((resolve) => {
        finishIdentity = resolve;
      }),
    );
    vi.mocked(identity.authenticatedFetch).mockResolvedValueOnce(
      jsonResponse(page([session("a", 9)], null, false)),
    );

    const { result } = renderHook(() => useCanvasSessions(), {
      wrapper: wrapper(new QueryClient()),
    });
    await waitFor(() => expect(identity.resolveIdentity).toHaveBeenCalled());
    expect(identity.authenticatedFetch).not.toHaveBeenCalled();
    expect(window.sessionStorage.length).toBe(0);

    await act(async () => {
      finishIdentity("me");
    });
    await waitFor(() => expect(result.current.networkConfirmed).toBe(true));
    expect(identity.authenticatedFetch).toHaveBeenCalledTimes(1);
    const keys = Array.from({ length: window.sessionStorage.length }, (_, index) =>
      window.sessionStorage.key(index),
    );
    expect(keys).toHaveLength(1);
    expect(keys[0]).toMatch(/:me$/);
    expect(keys[0]).not.toContain(":anonymous");
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
