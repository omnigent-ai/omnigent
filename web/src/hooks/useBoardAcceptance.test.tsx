import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { showToast } from "@/components/ui/toast";
import type * as identity from "@/lib/identity";
import { authenticatedFetch } from "@/lib/identity";
import { BOARD_ACCEPTED_LABEL_KEY, boardColumn } from "@/lib/sessionBoard";
import type { ConversationsInfiniteData } from "@/lib/sessionListCache";
import { setSessionBoardAccepted, useSetBoardAccepted } from "./useBoardAcceptance";
import {
  clearSessionTombstones,
  expireLabelWrite,
  fetchConversationsPage,
  isLatestLabelWrite,
  markLabelWrite,
  PINNED_CONVERSATIONS_KEY,
  withLabelWrite,
  type Conversation,
  type LabelWriteTombstone,
} from "./useConversations";

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/lib/identity", async (importActual) => ({
  ...(await importActual<typeof identity>()),
  authenticatedFetch: vi.fn(),
}));

const LIST_KEY = ["conversations", "", false, null, "mine"];
const FOLDER_KEY = ["project-sessions", "Sprint", "updated_at", false];
const ACCEPTED = { [BOARD_ACCEPTED_LABEL_KEY]: "1700000101" };

function row(
  labels: Record<string, string> = {},
  boardRevision = "rev_reviewed",
  id = "conv_a",
): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_100,
    labels: { keep: "1", ...labels },
    permission_level: null,
    status: "idle",
    board_revision: boardRevision,
  };
}

function page(...conversations: Conversation[]) {
  return {
    data: conversations,
    first_id: conversations[0].id,
    last_id: conversations.at(-1)!.id,
    has_more: false,
  };
}

function pages(...conversations: Conversation[]): ConversationsInfiniteData {
  return { pages: [page(...conversations)], pageParams: [undefined] };
}

// Write the row into every cached list, as a refetch or the session stream does.
function deliver(client: QueryClient, conversation: Conversation, keys = [LIST_KEY, FOLDER_KEY]) {
  for (const key of keys) client.setQueryData(key, pages(conversation));
  if (keys.includes(LIST_KEY))
    client.setQueryData(PINNED_CONVERSATIONS_KEY, {
      conversations: [conversation],
      filterHonored: true,
    });
}

function seededClient(conversation: Conversation) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  deliver(client, conversation);
  return client;
}

function cachedRow(client: QueryClient, key: readonly unknown[], id = "conv_a") {
  return client
    .getQueryData<ConversationsInfiniteData>(key)
    ?.pages[0].data.find((conversation) => conversation.id === id);
}

function cachedLabels(client: QueryClient, key: readonly unknown[], id = "conv_a") {
  return cachedRow(client, key, id)?.labels;
}

function pinnedLabels(client: QueryClient) {
  return client.getQueryData<{ conversations: Conversation[] }>(PINNED_CONVERSATIONS_KEY)
    ?.conversations[0].labels;
}

const failure = (status: number) =>
  new Response(JSON.stringify({ error: { message: "nope" } }), { status });
const accepted = (stamp = "1700000101", id = "conv_a") =>
  new Response(JSON.stringify(row({ [BOARD_ACCEPTED_LABEL_KEY]: stamp }, "rev_reviewed", id)), {
    status: 200,
  });

// Each PATCH waits until the test resolves it, by call order or with `to(id)`;
// a list GET answers with `listed` at once, as a lagging index would.
function deferredResponses(listed: Conversation[] = []) {
  const pending: ((response: Response) => void)[] = [];
  const bySession = new Map<string, (response: Response) => void>();
  vi.mocked(authenticatedFetch).mockImplementation(async (url, init) => {
    const path = String(url);
    if (init?.method !== "PATCH")
      return new Response(JSON.stringify(page(...listed)), { status: 200 });
    return new Promise<Response>((resolve) => {
      pending.push(resolve);
      bySession.set(decodeURIComponent(path.split("/").at(-1)!), resolve);
    });
  });
  return Object.assign(pending, { to: (id: string) => bySession.get(id)! });
}

function invalidated(client: QueryClient) {
  return [LIST_KEY, FOLDER_KEY, PINNED_CONVERSATIONS_KEY].filter(
    (key) => client.getQueryState(key)?.isInvalidated,
  );
}

// The mutation, plus a mounted list query so an invalidation really refetches.
function renderMutation(client: QueryClient) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(
    () => {
      useQuery({
        queryKey: LIST_KEY,
        queryFn: async () => ({
          pages: [
            await fetchConversationsPage({
              searchQuery: "",
              includeArchived: false,
              visibility: "mine",
              queryClient: client,
            }),
          ],
          pageParams: [undefined],
        }),
        staleTime: Infinity,
      });
      return useSetBoardAccepted();
    },
    { wrapper },
  );
}

afterEach(() => {
  vi.clearAllMocks();
  clearSessionTombstones();
});

describe("setSessionBoardAccepted", () => {
  it("PATCHes the accepted label, or an empty value to reopen", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(
      async () => new Response(JSON.stringify(row()), { status: 200 }),
    );
    await setSessionBoardAccepted("conv a", "rev_reviewed");
    await setSessionBoardAccepted("conv a", null);

    const calls = vi.mocked(authenticatedFetch).mock.calls;
    expect(calls[0][0]).toBe("/v1/sessions/conv%20a");
    expect(calls[0][1]?.method).toBe("PATCH");
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({
      labels: { [BOARD_ACCEPTED_LABEL_KEY]: "rev_reviewed" },
    });
    expect(JSON.parse(String(calls[1][1]?.body))).toEqual({
      labels: { [BOARD_ACCEPTED_LABEL_KEY]: "" },
    });
  });
});

describe("useSetBoardAccepted", () => {
  it("writes the label into every cached copy of the row, then the server's stamp", async () => {
    const respond = deferredResponses();
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));

    // Optimistically just past the reviewed time, so the card reads as accepted.
    const optimistic = { keep: "1", ...ACCEPTED };
    await waitFor(() => expect(cachedLabels(client, LIST_KEY)).toEqual(optimistic));
    expect(cachedLabels(client, FOLDER_KEY)).toEqual(optimistic);
    expect(pinnedLabels(client)).toEqual(optimistic);

    const stored = { keep: "1", [BOARD_ACCEPTED_LABEL_KEY]: "1700000105" };
    respond[0](new Response(JSON.stringify(row(stored)), { status: 200 }));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(cachedLabels(client, LIST_KEY)).toEqual(stored);
    expect(cachedLabels(client, FOLDER_KEY)).toEqual(stored);
    expect(pinnedLabels(client)).toEqual(stored);
    expect(invalidated(client)).toEqual([]);
    expect(showToast).not.toHaveBeenCalled();
  });

  it("writes open-session copies only while they match the reviewed session", async () => {
    const respond = deferredResponses();
    const client = seededClient(row());
    const backfill = ["conversation-backfill", "conv_a"];
    const snapshot = ["session", "conv_a"];
    client.setQueryData(backfill, row());
    client.setQueryData(snapshot, { id: "conv_a", labels: { keep: "1" } });
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(respond).toHaveLength(1));
    respond[0](accepted());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const stored = { keep: "1", ...ACCEPTED };
    expect(client.getQueryData<Conversation>(backfill)?.labels).toEqual(stored);
    expect(client.getQueryData<Conversation>(snapshot)?.labels).toEqual(stored);

    // Without a revision, an older activity time or a different label marks a stale copy.
    const olderBackfill = {
      ...row(ACCEPTED),
      board_revision: undefined,
      updated_at: 1_700_000_050,
    };
    client.setQueryData(backfill, olderBackfill);
    const staleLabels = { keep: "1", [BOARD_ACCEPTED_LABEL_KEY]: "1600000000" };
    client.setQueryData(snapshot, { id: "conv_a", labels: staleLabels });
    act(() => result.current.mutate({ row: row(stored), reviewedRevision: null }));
    await waitFor(() => expect(respond).toHaveLength(2));
    respond[1](
      new Response(JSON.stringify(row({ [BOARD_ACCEPTED_LABEL_KEY]: "" })), { status: 200 }),
    );
    await waitFor(() =>
      expect(cachedLabels(client, LIST_KEY)?.[BOARD_ACCEPTED_LABEL_KEY]).toBe(""),
    );
    expect(client.getQueryData(backfill)).toEqual(olderBackfill);
    expect(client.getQueryData<Conversation>(snapshot)?.labels).toEqual(staleLabels);
  });

  it("applies a successful response without the label as reopened", async () => {
    // Later activity removed the acceptance before the response snapshot was read.
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify(row()), { status: 200 }),
    );
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(cachedLabels(client, LIST_KEY)).toEqual({ keep: "1" });
    expect(pinnedLabels(client)).toEqual({ keep: "1" });
  });

  it("keeps the acceptance when a pinned-list refetch started before Accept resolves", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(accepted());
    const client = seededClient(row());
    let resolveStale: (data: unknown) => void = () => {};
    const staleRefetch = client
      .fetchQuery({
        queryKey: PINNED_CONVERSATIONS_KEY,
        queryFn: () =>
          new Promise((resolve) => {
            resolveStale = resolve;
          }),
        staleTime: 0,
      })
      .catch(() => undefined);
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    resolveStale({ conversations: [row()], filterHonored: true });
    await staleRefetch;

    expect(pinnedLabels(client)?.[BOARD_ACCEPTED_LABEL_KEY]).toBe("1700000101");
  });

  it("keeps newer same-second activity reopened when the older Accept response lands", async () => {
    const respond = deferredResponses();
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(boardColumn(cachedRow(client, LIST_KEY)!)).toBe("done"));
    // Activity in the same second streams in first: a new revision, acceptance removed.
    const newer = row({}, "rev_newer");
    deliver(client, newer);
    respond[0](accepted());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    for (const key of [LIST_KEY, FOLDER_KEY]) expect(cachedRow(client, key)).toEqual(newer);
    expect(pinnedLabels(client)).toEqual(newer.labels);
    expect(boardColumn(cachedRow(client, LIST_KEY)!)).toBe("needs_review");
  });

  it("leaves a copy already at a newer revision when the Accept starts", async () => {
    const respond = deferredResponses();
    const client = seededClient(row());
    const newer = row({}, "rev_newer");
    deliver(client, newer, [FOLDER_KEY]);
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(cachedLabels(client, LIST_KEY)).toEqual({ keep: "1", ...ACCEPTED }));
    expect(cachedRow(client, FOLDER_KEY)).toEqual(newer);
    respond[0](accepted());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(cachedRow(client, FOLDER_KEY)).toEqual(newer);
  });

  it("writes the response into a copy a list read from before the write replaced", async () => {
    const respond = deferredResponses();
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(boardColumn(cachedRow(client, LIST_KEY)!)).toBe("done"));
    // Same revision, no acceptance yet: a list read taken before the PATCH committed.
    deliver(client, row());
    respond[0](accepted());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const stored = { keep: "1", ...ACCEPTED };
    expect(cachedLabels(client, LIST_KEY)).toEqual(stored);
    expect(cachedLabels(client, FOLDER_KEY)).toEqual(stored);
    expect(pinnedLabels(client)).toEqual(stored);
    expect(invalidated(client)).toEqual([]);
  });

  it("keeps other sessions' writes on a list refetched from a lagging index", async () => {
    const [first, second, third] = ["conv_a", "conv_b", "conv_c"].map((id) =>
      row({}, "rev_reviewed", id),
    );
    // The index has none of the acceptances yet.
    const respond = deferredResponses([first, second, third]);
    const client = seededClient(first);
    client.setQueryData(LIST_KEY, pages(first, second, third));
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: second, reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(respond).toHaveLength(1));
    respond.to("conv_b")(accepted("1700000101", "conv_b"));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    act(() => result.current.mutate({ row: third, reviewedRevision: "rev_reviewed" }));
    act(() => result.current.mutate({ row: first, reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(respond).toHaveLength(3));
    // The first session's Accept is refused, so the list refetches for its revision.
    respond.to("conv_a")(failure(409));
    await waitFor(() => expect(result.current.isError).toBe(true));
    await waitFor(() => expect(client.getQueryState(LIST_KEY)?.isInvalidated).toBe(false));

    expect(vi.mocked(authenticatedFetch).mock.calls.map(([url]) => String(url))).toContainEqual(
      expect.stringMatching(/^\/v1\/sessions\?/),
    );
    expect(cachedLabels(client, LIST_KEY, "conv_a")).toEqual({ keep: "1" });
    // The settled Accept and the one still in flight both stay accepted.
    expect(cachedLabels(client, LIST_KEY, "conv_b")).toEqual({ keep: "1", ...ACCEPTED });
    expect(cachedLabels(client, LIST_KEY, "conv_c")).toEqual({ keep: "1", ...ACCEPTED });
    expect(client.getQueryState(PINNED_CONVERSATIONS_KEY)?.isInvalidated).toBe(false);
  });

  it("does not write an Accept a newer write superseded before it started", async () => {
    const respond = deferredResponses();
    const client = seededClient(row());
    const seen: (string | undefined)[] = [];
    client.getQueryCache().subscribe(() => {
      const label = cachedLabels(client, LIST_KEY)?.[BOARD_ACCEPTED_LABEL_KEY];
      if (seen.at(-1) !== label) seen.push(label);
    });
    const { result } = renderMutation(client);

    act(() => {
      result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" });
      result.current.mutate({ row: row(), reviewedRevision: null });
    });
    await waitFor(() => expect(respond).toHaveLength(2));
    // The superseded Accept fails; the newer Reopen owns the outcome.
    respond[0](failure(403));
    respond[1](
      new Response(JSON.stringify(row({ [BOARD_ACCEPTED_LABEL_KEY]: "" })), { status: 200 }),
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(seen).not.toContain("1700000101");
    expect(cachedLabels(client, LIST_KEY)?.[BOARD_ACCEPTED_LABEL_KEY]).toBe("");
    expect(showToast).not.toHaveBeenCalled();
  });

  it("restores the previous acceptance and says so when the PATCH fails", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(failure(403));
    const before = row(ACCEPTED);
    const client = seededClient(before);
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: before, reviewedRevision: null }));

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(cachedLabels(client, LIST_KEY)).toEqual(before.labels);
    expect(cachedLabels(client, FOLDER_KEY)).toEqual(before.labels);
    expect(pinnedLabels(client)).toEqual(before.labels);
    expect(showToast).toHaveBeenCalledWith("Couldn't reopen the session.");
    // A refusal stored nothing, so nothing refetches.
    expect(invalidated(client)).toEqual([]);
  });

  it("leaves newer activity alone when a Reopen fails after it streamed in", async () => {
    const respond = deferredResponses();
    const before = row(ACCEPTED);
    const client = seededClient(before);
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: before, reviewedRevision: null }));
    await waitFor(() =>
      expect(cachedLabels(client, LIST_KEY)?.[BOARD_ACCEPTED_LABEL_KEY]).toBe(""),
    );
    const newer = row({}, "rev_newer");
    deliver(client, newer, [FOLDER_KEY]);
    respond[0](failure(403));
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(cachedRow(client, FOLDER_KEY)).toEqual(newer);
    expect(cachedLabels(client, LIST_KEY)).toEqual(before.labels);
    expect(showToast).toHaveBeenCalledWith("Couldn't reopen the session.");
  });

  it("rolls back only copies that still hold this write's label at its revision", async () => {
    const respond = deferredResponses();
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(boardColumn(cachedRow(client, LIST_KEY)!)).toBe("done"));
    // A newer revision that happens to carry the same stamp is not this write's,
    // nor is a same-revision label another tab wrote.
    const newer = row(ACCEPTED, "rev_newer");
    deliver(client, newer, [FOLDER_KEY]);
    const reopenedElsewhere = row({ [BOARD_ACCEPTED_LABEL_KEY]: "" });
    client.setQueryData(PINNED_CONVERSATIONS_KEY, {
      conversations: [reopenedElsewhere],
      filterHonored: true,
    });
    respond[0](failure(403));
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(cachedLabels(client, LIST_KEY)).toEqual({ keep: "1" });
    expect(cachedRow(client, FOLDER_KEY)).toEqual(newer);
    expect(pinnedLabels(client)).toEqual(reopenedElsewhere.labels);
  });

  it("explains a refused Accept when the session changed since review", async () => {
    const message = "The session changed since you reviewed it. Review it again before accepting.";
    vi.mocked(authenticatedFetch).mockImplementation(async (_url, init) =>
      init?.method === "PATCH"
        ? new Response(JSON.stringify({ error: { code: "conflict", message } }), { status: 409 })
        : new Response(JSON.stringify(page(row({}, "rev_newer"))), { status: 200 }),
    );
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(cachedLabels(client, FOLDER_KEY)).toEqual(row().labels);
    expect(showToast).toHaveBeenCalledWith(message);
    // The lists refetch so a retry carries the session's current revision; the
    // Pinned list's index lags writes, so it is left alone.
    expect(client.getQueryState(FOLDER_KEY)?.isInvalidated).toBe(true);
    expect(client.getQueryState(PINNED_CONVERSATIONS_KEY)?.isInvalidated).toBe(false);
    await waitFor(() => expect(cachedRow(client, LIST_KEY)?.board_revision).toBe("rev_newer"));
  });

  it.each([
    ["stored", () => accepted(), { keep: "1", ...ACCEPTED }, false],
    ["not stored", () => new Response(JSON.stringify(row()), { status: 200 }), { keep: "1" }, true],
    ["unreadable", () => failure(503), { keep: "1" }, true],
  ])(
    "reads the session back after a failure that may have been %s",
    async (_case, detail, labels, toasts) => {
      vi.mocked(authenticatedFetch).mockImplementation(async (url, init) => {
        if (init?.method === "PATCH") throw new TypeError("Failed to fetch");
        expect(String(url)).toMatch(/^\/v1\/sessions\/conv_a\?/);
        return detail();
      });
      const client = seededClient(row());
      const { result } = renderMutation(client);

      act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));
      await waitFor(() => expect(result.current.isError).toBe(true));

      for (const key of [LIST_KEY, FOLDER_KEY]) expect(cachedLabels(client, key)).toEqual(labels);
      expect(pinnedLabels(client)).toEqual(labels);
      expect(vi.mocked(showToast).mock.calls).toEqual(
        toasts ? [["Couldn't accept the session."]] : [],
      );
      // The detail read is authoritative, so the lagging lists are not refetched.
      expect(invalidated(client)).toEqual([]);
    },
  );
});

describe("withLabelWrite", () => {
  const entry: LabelWriteTombstone = {
    key: BOARD_ACCEPTED_LABEL_KEY,
    before: undefined,
    value: "1700000101",
    boardRevision: "rev_reviewed",
    updatedAt: 1_700_000_100,
  };

  it.each([
    ["an index row still showing the old label", row(), { keep: "1", ...ACCEPTED }],
    ["a row at a newer revision", row({}, "rev_newer"), { keep: "1" }],
    ["a label another writer changed", row({ [BOARD_ACCEPTED_LABEL_KEY]: "" }), null],
  ])("covers %s only while it is the write's", (_case, fetched, labels) => {
    markLabelWrite("conv_a", entry);
    expect(withLabelWrite(fetched).labels).toEqual(labels ?? fetched.labels);
    expect(withLabelWrite({ id: "conv_a", labels: null })).toEqual({ id: "conv_a", labels: null });
  });

  it("matches the activity time on a server without revisions", () => {
    markLabelWrite("conv_a", { ...entry, boardRevision: null });
    const legacy = { ...row(), board_revision: undefined };
    expect(withLabelWrite(legacy).labels).toEqual({ keep: "1", ...ACCEPTED });
    expect(withLabelWrite({ ...legacy, updated_at: 1_700_000_200 })).toEqual({
      ...legacy,
      updated_at: 1_700_000_200,
    });
  });

  it("drops a landed write after the reindex grace window", () => {
    vi.useFakeTimers();
    try {
      markLabelWrite("conv_a", entry);
      expireLabelWrite("conv_a", entry);
      vi.advanceTimersByTime(59_999);
      expect(isLatestLabelWrite("conv_a", entry)).toBe(true);
      vi.advanceTimersByTime(1);
      expect(isLatestLabelWrite("conv_a", entry)).toBe(false);
      expect(withLabelWrite(row())).toEqual(row());
    } finally {
      vi.useRealTimers();
    }
  });
});
