import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import {
  childSessionsQueryKey,
  type ChildSessionInfo,
  invalidateChildSessions,
  SubagentTreeContext,
  useChildSessions,
  useDescendantSession,
  useSubagentTree,
} from "./useChildSessions";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

function newQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={newQueryClient()}>{children}</QueryClientProvider>;
}

function wrapperFor(queryClient: QueryClient) {
  return ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
});

/** A minimal ``child_sessions`` wire row, as servers without the subtree route send it. */
function legacyRow(id: string) {
  return {
    id,
    title: `worker:${id}`,
    tool: "worker",
    session_name: id,
    current_task_status: null,
    busy: false,
  };
}

let nextCreatedAt = 1_000;

/**
 * A minimal ``descendants`` wire row under ``parentId`` at ``depth``. Each
 * row is created later than the one built before it, so siblings listed
 * earlier in a fixture are older.
 */
function descendantRow(id: string, parentId: string, depth: number, archived = false) {
  nextCreatedAt += 1;
  return {
    ...legacyRow(id),
    parent_session_id: parentId,
    depth,
    created_at: nextCreatedAt,
    archived,
  };
}

function treePage(rows: ReturnType<typeof descendantRow>[], hasMore = false) {
  return jsonResponse({
    object: "list",
    data: rows,
    last_id: rows.at(-1)?.id ?? null,
    has_more: hasMore,
  });
}

function jsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), init);
}

function ids(rows: ChildSessionInfo[] | undefined): string[] | undefined {
  return rows?.map((row) => row.id);
}

function cachedIds(queryClient: QueryClient, parentId: string): string[] | undefined {
  return ids(queryClient.getQueryData(childSessionsQueryKey(parentId)));
}

function fetchedUrls(): unknown[] {
  return vi.mocked(authenticatedFetch).mock.calls.map(([url]) => url);
}

/** A row of the Agents rail: reads its children through the per-node hook. */
function Row({ id }: { id: string }) {
  const { children } = useChildSessions(id);
  return <div data-testid={`row-${id}`}>{ids(children)?.join(",")}</div>;
}

/** The rail itself: one subtree fetch, rows rendered under the tree context. */
function Rail({ rootId }: { rootId: string }) {
  const { children, fromTree, error } = useSubagentTree(rootId);
  return (
    <SubagentTreeContext.Provider value={fromTree}>
      <div data-testid="root">{ids(children)?.join(",")}</div>
      <div data-testid="error">{error?.message ?? ""}</div>
      {children.map((child) => (
        <Row key={child.id} id={child.id} />
      ))}
    </SubagentTreeContext.Provider>
  );
}

function renderRail(queryClient: QueryClient, rootId = "conv_root") {
  return render(
    <QueryClientProvider client={queryClient}>
      <Rail rootId={rootId} />
    </QueryClientProvider>,
  );
}

describe("useChildSessions", () => {
  it("does not fetch child sessions for a provisional conversation", () => {
    const { result } = renderHook(() => useChildSessions("temp:pending-create"), { wrapper });

    expect(result.current.children).toEqual([]);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("useSubagentTree", () => {
  it("loads the whole subtree in one request and seeds every parent's child list", async () => {
    // WHY: the Agents rail used to issue one child_sessions request per
    // rendered node; the subtree fetch must leave each row's list cached.
    const queryClient = newQueryClient();
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      treePage([
        descendantRow("conv_a1x", "conv_a1", 3),
        descendantRow("conv_a1", "conv_a", 2),
        descendantRow("conv_b", "conv_root", 1),
        descendantRow("conv_a", "conv_root", 1),
      ]),
    );

    const { result } = renderHook(() => useSubagentTree("conv_root"), {
      wrapper: wrapperFor(queryClient),
    });

    expect(result.current.isLoading).toBe(true);
    expect(result.current.fromTree).toBe(true);
    // Siblings render newest-first whatever order the page listed them in.
    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_a", "conv_b"]));
    expect(result.current.fromTree).toBe(true);
    expect(fetchedUrls()).toEqual(["/v1/sessions/conv_root/descendants?max_depth=3&limit=1000"]);
    expect(cachedIds(queryClient, "conv_a")).toEqual(["conv_a1"]);
    expect(cachedIds(queryClient, "conv_b")).toEqual([]);
    expect(cachedIds(queryClient, "conv_a1")).toEqual(["conv_a1x"]);
    // Depth-cap rows render no child list, so nothing is seeded for them.
    expect(queryClient.getQueryData(childSessionsQueryKey("conv_a1x"))).toBeUndefined();
  });

  it("follows the page cursor to the end of a wide subtree", async () => {
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(treePage([descendantRow("conv_a", "conv_root", 1)], true))
      .mockResolvedValueOnce(treePage([descendantRow("conv_b", "conv_root", 1)]));

    const { result } = renderHook(() => useSubagentTree("conv_root"), { wrapper });

    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_b", "conv_a"]));
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000&after=conv_a",
    ]);
  });

  it("walks past an empty page that continues", async () => {
    // WHY: rows outside the subtree or below the depth cap are dropped from a
    // page but still advance the cursor, so an empty page is not the end.
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(
        jsonResponse({ object: "list", data: [], last_id: "conv_filtered", has_more: true }),
      )
      .mockResolvedValueOnce(treePage([descendantRow("conv_a", "conv_root", 1)]));

    const { result } = renderHook(() => useSubagentTree("conv_root"), { wrapper });

    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_a"]));
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000&after=conv_filtered",
    ]);
  });

  it("stops on a page cursor that does not advance", async () => {
    const stuck = () =>
      jsonResponse({
        object: "list",
        data: [descendantRow("conv_a", "conv_root", 1)],
        last_id: "conv_a",
        has_more: true,
      });
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(stuck()).mockResolvedValueOnce(stuck());

    const { result } = renderHook(() => useSubagentTree("conv_root"), { wrapper });

    await waitFor(() => expect(result.current.error?.message).toMatch(/cursor did not advance/));
    expect(fetchedUrls()).toHaveLength(2);
  });

  it("stops on page cursors that alternate", async () => {
    const page = (lastId: string) =>
      jsonResponse({ object: "list", data: [], last_id: lastId, has_more: true });
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(treePage([descendantRow("conv_a", "conv_root", 1)], true))
      .mockResolvedValueOnce(page("conv_x"))
      .mockResolvedValueOnce(page("conv_a"))
      .mockResolvedValueOnce(page("conv_x"));

    const { result } = renderHook(() => useSubagentTree("conv_root"), { wrapper });

    await waitFor(() => expect(result.current.error?.message).toMatch(/cursor did not advance/));
    expect(fetchedUrls()).toHaveLength(3);
  });

  it("hides settled archived rows but keeps an archived parent of live work", async () => {
    // WHY: archiving a middle session must not disconnect the live
    // sub-agents beneath it, while finished archived ones stay out of the rail.
    const queryClient = newQueryClient();
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      treePage([
        descendantRow("conv_leaf", "conv_mid", 2),
        descendantRow("conv_settled", "conv_root", 1, true),
        // Archiving does not end a session's work: a busy archived leaf stays.
        { ...descendantRow("conv_working", "conv_root", 1, true), busy: true },
        descendantRow("conv_mid", "conv_root", 1, true),
      ]),
    );

    const { result } = renderHook(() => useSubagentTree("conv_root"), {
      wrapper: wrapperFor(queryClient),
    });

    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_mid", "conv_working"]));
    expect(cachedIds(queryClient, "conv_mid")).toEqual(["conv_leaf"]);
    expect(queryClient.getQueryData(childSessionsQueryKey("conv_settled"))).toBeUndefined();
  });

  it("renders rows from cache without fetching while the subtree is still loading", async () => {
    // WHY: the root's list is often cached before the rail opens (the shell's
    // count badge fetched it); rows rendered from it must wait for the tree
    // rather than fetch their own children.
    const queryClient = newQueryClient();
    queryClient.setQueryData(childSessionsQueryKey("conv_root"), [
      { ...legacyRow("conv_a"), task_summary: null, labels: {}, last_task_error: null },
    ] as ChildSessionInfo[]);
    let release: (response: Response) => void = () => {};
    vi.mocked(authenticatedFetch).mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        }),
    );
    renderRail(queryClient);
    await waitFor(() => expect(screen.getByTestId("root")).toHaveTextContent("conv_a"));
    expect(screen.getByTestId("row-conv_a")).toBeInTheDocument();
    expect(fetchedUrls()).toEqual(["/v1/sessions/conv_root/descendants?max_depth=3&limit=1000"]);

    release(
      treePage([descendantRow("conv_a1", "conv_a", 2), descendantRow("conv_a", "conv_root", 1)]),
    );

    await waitFor(() => expect(screen.getByTestId("row-conv_a")).toHaveTextContent("conv_a1"));
    expect(fetchedUrls()).toHaveLength(1);
  });

  it("reloads the subtree, not each node, when a seeded list is refreshed", async () => {
    // WHY: live events invalidate a parent's child list; under a loaded tree
    // that must cost one descendants request and keep every seeded row (an
    // archived connector, children past the legacy page size) intact.
    const queryClient = newQueryClient();
    const wide = Array.from({ length: 20 }, (_, i) =>
      descendantRow(`conv_c${String(i).padStart(2, "0")}`, "conv_root", 1),
    );
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(
        treePage([
          descendantRow("conv_a1", "conv_a", 2),
          descendantRow("conv_b1", "conv_b", 2),
          descendantRow("conv_b", "conv_root", 1, true),
          descendantRow("conv_a", "conv_root", 1),
        ]),
      )
      .mockResolvedValueOnce(
        treePage([
          descendantRow("conv_a2", "conv_a", 2),
          descendantRow("conv_a1", "conv_a", 2),
          descendantRow("conv_b1", "conv_b", 2),
          ...wide,
          descendantRow("conv_b", "conv_root", 1, true),
          descendantRow("conv_a", "conv_root", 1),
        ]),
      );
    renderRail(queryClient);
    await waitFor(() => expect(screen.getByTestId("row-conv_a")).toHaveTextContent("conv_a1"));
    expect(screen.getByTestId("root")).toHaveTextContent("conv_a,conv_b");

    await act(() => invalidateChildSessions(queryClient, "conv_a"));

    await waitFor(() =>
      expect(screen.getByTestId("row-conv_a")).toHaveTextContent("conv_a1,conv_a2"),
    );
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
    ]);
    expect(cachedIds(queryClient, "conv_root")).toHaveLength(22);
    expect(cachedIds(queryClient, "conv_root")).toContain("conv_b");
    expect(cachedIds(queryClient, "conv_b")).toEqual(["conv_b1"]);
  });

  it("re-reads the subtree when a nested list is refreshed while it was loading", async () => {
    // WHY: an event for a nested parent can arrive after the server built the
    // in-flight snapshot; the rows beneath never fetch for themselves, so the
    // tree must read again or the new descendant stays hidden.
    const queryClient = newQueryClient();
    queryClient.setQueryData(childSessionsQueryKey("conv_root"), [
      { ...legacyRow("conv_a"), task_summary: null, labels: {}, last_task_error: null },
    ] as ChildSessionInfo[]);
    let release: (response: Response) => void = () => {};
    vi.mocked(authenticatedFetch)
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            release = resolve;
          }),
      )
      .mockResolvedValueOnce(
        treePage([descendantRow("conv_a", "conv_root", 1), descendantRow("conv_a1", "conv_a", 2)]),
      );
    renderRail(queryClient);
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    await act(() => invalidateChildSessions(queryClient, "conv_a"));
    release(treePage([descendantRow("conv_a", "conv_root", 1)]));

    await waitFor(() => expect(screen.getByTestId("row-conv_a")).toHaveTextContent("conv_a1"));
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
    ]);
  });

  it("reloads the subtree for a hidden archived parent that gains live work", async () => {
    // WHY: a settled archived session is hidden from the rail but still part
    // of the tree; a child spawned under it must bring both back into view.
    const queryClient = newQueryClient();
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(treePage([descendantRow("conv_a", "conv_root", 1, true)]))
      .mockResolvedValueOnce(
        treePage([
          descendantRow("conv_a", "conv_root", 1, true),
          descendantRow("conv_a1", "conv_a", 2),
        ]),
      );
    renderRail(queryClient);
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(cachedIds(queryClient, "conv_root")).toEqual([]));

    await act(() => invalidateChildSessions(queryClient, "conv_a"));

    await waitFor(() => expect(screen.getByTestId("root")).toHaveTextContent("conv_a"));
    await waitFor(() => expect(screen.getByTestId("row-conv_a")).toHaveTextContent("conv_a1"));
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
    ]);
  });

  it("orders same-second siblings by descending id", async () => {
    const queryClient = newQueryClient();
    const tied = (id: string) => ({ ...descendantRow(id, "conv_root", 1), created_at: 5_000 });
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      treePage([tied("conv_b"), tied("conv_c"), tied("conv_a")]),
    );

    const { result } = renderHook(() => useSubagentTree("conv_root"), {
      wrapper: wrapperFor(queryClient),
    });

    await waitFor(() =>
      expect(ids(result.current.children)).toEqual(["conv_c", "conv_b", "conv_a"]),
    );
  });

  it("reloads a stale tree when the rail reopens and keeps the shell's list fresh", async () => {
    // WHY: switching rail tabs unmounts the panel; a refresh that arrives then
    // must still reach the mounted root-list consumers and reload the tree
    // when the panel comes back, not leave it on the old snapshot.
    const queryClient = newQueryClient();
    let treeLoads = 0;
    vi.mocked(authenticatedFetch).mockImplementation(async (url) => {
      if (String(url).includes("/descendants")) {
        treeLoads += 1;
        const rows = [descendantRow("conv_a", "conv_root", 1)];
        if (treeLoads > 1) rows.push(descendantRow("conv_b", "conv_root", 1));
        return treePage(rows);
      }
      return jsonResponse({ object: "list", data: [legacyRow("conv_a")] });
    });
    const badge = renderHook(() => useChildSessions("conv_root"), {
      wrapper: wrapperFor(queryClient),
    });
    await waitFor(() => expect(ids(badge.result.current.children)).toEqual(["conv_a"]));
    const rail = renderRail(queryClient);
    await waitFor(() => expect(screen.getByTestId("root")).toHaveTextContent("conv_a"));
    expect(treeLoads).toBe(1);
    rail.unmount();

    await act(() => invalidateChildSessions(queryClient, "conv_root"));

    // The shell's own list refetches while the tree is unmounted…
    await waitFor(() => expect(fetchedUrls()).toHaveLength(3));
    expect(fetchedUrls()[2]).toBe("/v1/sessions/conv_root/child_sessions");
    expect(treeLoads).toBe(1);
    // …and reopening the rail reloads the stale tree.
    renderRail(queryClient);
    await waitFor(() => expect(screen.getByTestId("root")).toHaveTextContent("conv_b,conv_a"));
    expect(treeLoads).toBe(2);
  });

  it("schedules one more refresh when the last re-read pass is already stale", async () => {
    // WHY: the in-flight re-read is bounded; a change landing during its last
    // pass must still reach the cache through a follow-up fetch.
    const queryClient = newQueryClient();
    const pending: ((response: Response) => void)[] = [];
    vi.mocked(authenticatedFetch).mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          pending.push(resolve);
        }),
    );
    queryClient.setQueryData(childSessionsQueryKey("conv_root"), [
      { ...legacyRow("conv_a"), task_summary: null, labels: {}, last_task_error: null },
    ] as ChildSessionInfo[]);
    renderRail(queryClient);
    const stale = () => treePage([descendantRow("conv_a", "conv_root", 1)]);
    // Each pass must see its own request before the next invalidation lands.
    /* oxlint-disable no-await-in-loop */
    for (let pass = 0; pass < 3; pass++) {
      await waitFor(() => expect(pending).toHaveLength(pass + 1));
      await act(() => invalidateChildSessions(queryClient, "conv_a"));
      pending[pass](stale());
    }
    /* oxlint-enable no-await-in-loop */

    // A fourth request follows the exhausted loop and carries the change.
    await waitFor(() => expect(pending).toHaveLength(4));
    pending[3](
      treePage([descendantRow("conv_a", "conv_root", 1), descendantRow("conv_a1", "conv_a", 2)]),
    );
    await waitFor(() => expect(screen.getByTestId("row-conv_a")).toHaveTextContent("conv_a1"));
  });

  it("leaves a list patched while the subtree was loading for the next refresh", async () => {
    // WHY: an SSE patch that lands mid-fetch is newer than the snapshot the
    // server built; seeding over it would show stale status until the next event.
    const queryClient = newQueryClient();
    let release: (response: Response) => void = () => {};
    vi.mocked(authenticatedFetch).mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        }),
    );
    const patched: ChildSessionInfo = {
      id: "conv_a1",
      title: "worker:conv_a1",
      task_summary: null,
      tool: "worker",
      session_name: "conv_a1",
      labels: {},
      current_task_status: "in_progress",
      last_task_error: null,
      busy: true,
      last_message_preview: "working",
      pending_elicitations_count: 0,
      routed_model: null,
    };

    const { result } = renderHook(() => useSubagentTree("conv_root"), {
      wrapper: wrapperFor(queryClient),
    });
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => {
      setTimeout(resolve, 5);
    });
    act(() => queryClient.setQueryData(childSessionsQueryKey("conv_a"), [patched]));
    release(
      treePage([descendantRow("conv_a1", "conv_a", 2), descendantRow("conv_a", "conv_root", 1)]),
    );

    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_a"]));
    expect(queryClient.getQueryData(childSessionsQueryKey("conv_a"))).toEqual([patched]);
  });

  it("falls back to the per-node list only when the server has no subtree endpoint", async () => {
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(new Response("Not Found", { status: 404, statusText: "Not Found" }))
      .mockResolvedValueOnce(jsonResponse({ object: "list", data: [legacyRow("conv_a")] }));

    const { result } = renderHook(() => useSubagentTree("conv_root"), { wrapper });

    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_a"]));
    expect(result.current.fromTree).toBe(false);
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/child_sessions",
    ]);
  });

  it("surfaces any other subtree failure instead of fetching per node", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response("boom", { status: 500, statusText: "Internal Server Error" }),
    );

    const { result } = renderHook(() => useSubagentTree("conv_root"), { wrapper });

    await waitFor(() => expect(result.current.error?.message).toBe("500 Internal Server Error"));
    expect(result.current.fromTree).toBe(true);
    expect(fetchedUrls()).toEqual(["/v1/sessions/conv_root/descendants?max_depth=3&limit=1000"]);
  });

  it("retries a failed subtree when a nested parent's list is refreshed", async () => {
    // WHY: a refresh that failed on a transient error keeps the last good tree;
    // the next nested event must reload it, not fall into the disabled
    // per-row query and leave the new descendant hidden.
    const queryClient = newQueryClient();
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(treePage([descendantRow("conv_a", "conv_root", 1)]))
      .mockResolvedValueOnce(
        new Response("boom", { status: 500, statusText: "Internal Server Error" }),
      )
      .mockResolvedValueOnce(
        treePage([descendantRow("conv_a", "conv_root", 1), descendantRow("conv_a1", "conv_a", 2)]),
      );
    renderRail(queryClient);
    await waitFor(() => expect(screen.getByTestId("root")).toHaveTextContent("conv_a"));

    await act(() => invalidateChildSessions(queryClient, "conv_a"));
    await waitFor(() =>
      expect(screen.getByTestId("error")).toHaveTextContent("500 Internal Server Error"),
    );
    await act(() => invalidateChildSessions(queryClient, "conv_a"));

    await waitFor(() => expect(screen.getByTestId("row-conv_a")).toHaveTextContent("conv_a1"));
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
    ]);
  });

  it("lets rendered rows fetch their own children on a server without the route", async () => {
    // WHY: against an older server the rail must still show grandchildren,
    // which only the per-node child_sessions lists can provide.
    const queryClient = newQueryClient();
    vi.mocked(authenticatedFetch).mockImplementation(async (url) => {
      const path = String(url);
      if (path.includes("/descendants")) {
        return new Response("Not Found", { status: 404, statusText: "Not Found" });
      }
      if (path.includes("/conv_root/")) {
        return jsonResponse({ object: "list", data: [legacyRow("conv_a")] });
      }
      if (path.includes("/conv_a/")) {
        return jsonResponse({ object: "list", data: [legacyRow("conv_a1")] });
      }
      return jsonResponse({ object: "list", data: [] });
    });
    renderRail(queryClient);

    await waitFor(() => expect(screen.getByTestId("row-conv_a")).toHaveTextContent("conv_a1"));
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/child_sessions",
      "/v1/sessions/conv_a/child_sessions",
    ]);
  });

  it("retries a failed subtree when its root's list is refreshed", async () => {
    const queryClient = newQueryClient();
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(
        new Response("boom", { status: 500, statusText: "Internal Server Error" }),
      )
      .mockResolvedValueOnce(treePage([descendantRow("conv_a", "conv_root", 1)]));
    renderRail(queryClient);
    await waitFor(() =>
      expect(screen.getByTestId("error")).toHaveTextContent("500 Internal Server Error"),
    );

    await act(() => invalidateChildSessions(queryClient, "conv_root"));

    await waitFor(() => expect(screen.getByTestId("root")).toHaveTextContent("conv_a"));
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
    ]);
  });

  it("does not fetch for a provisional conversation", () => {
    const { result } = renderHook(() => useSubagentTree("temp:pending-create"), { wrapper });

    expect(result.current.children).toEqual([]);
    expect(result.current.isLoading).toBe(false);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("invalidateChildSessions", () => {
  it("refetches the list itself when no loaded subtree covers it", async () => {
    const queryClient = newQueryClient();
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(jsonResponse({ object: "list", data: [legacyRow("conv_x1")] }))
      .mockResolvedValueOnce(jsonResponse({ object: "list", data: [legacyRow("conv_x2")] }));
    const { result } = renderHook(() => useChildSessions("conv_x"), {
      wrapper: wrapperFor(queryClient),
    });
    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_x1"]));

    await act(() => invalidateChildSessions(queryClient, "conv_x"));

    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_x2"]));
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_x/child_sessions",
      "/v1/sessions/conv_x/child_sessions",
    ]);
  });
});

describe("useDescendantSession", () => {
  it("fetches the direct-child list when it is not cached", async () => {
    // WHY: a mirrored card can mount before anything loaded the ancestor's
    // child list; the lookup must fetch it rather than stay unlabeled.
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            {
              id: "conv_child",
              title: "researcher:auth",
              task_summary: "Investigate auth flow",
              tool: "researcher",
              session_name: "auth",
              current_task_status: "in_progress",
              busy: true,
            },
          ],
        }),
      ),
    );
    const { result } = renderHook(() => useDescendantSession("conv_parent", "conv_child"), {
      wrapper,
    });

    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current?.task_summary).toBe("Investigate auth flow"));
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv_parent/child_sessions");
  });
});
