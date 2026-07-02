// Focused coverage for SessionUpdatesProvider's watch-set computation:
// the open session must always be unioned into the set pushed to the
// socket, even when it's an off-sidebar child absent from the
// conversations cache. The cache-merge / frame-application logic is
// covered separately in sessionListCache.test.ts; here we mock the socket
// and assert exactly which ids reach `setWatched`.

import { act, cleanup, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
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

import { SessionUpdatesProvider } from "./SessionUpdatesProvider";

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
