import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { showToast } from "@/components/ui/toast";
import type * as identity from "@/lib/identity";
import { authenticatedFetch } from "@/lib/identity";
import { BOARD_ACCEPTED_LABEL_KEY, boardColumn } from "@/lib/sessionBoard";
import type { ConversationsInfiniteData } from "@/lib/sessionListCache";
import { setSessionBoardAccepted, useSetBoardAccepted } from "./useBoardAcceptance";
import { PINNED_CONVERSATIONS_KEY, type Conversation } from "./useConversations";

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/lib/identity", async (importActual) => ({
  ...(await importActual<typeof identity>()),
  authenticatedFetch: vi.fn(),
}));

const LIST_KEY = ["conversations", "", false, null, "mine"];
const FOLDER_KEY = ["project-sessions", "Sprint", "updated_at", false];

function row(labels: Record<string, string> = {}, boardRevision = "rev_reviewed"): Conversation {
  return {
    id: "conv_a",
    object: "conversation",
    title: "A",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_100,
    labels: { keep: "1", ...labels },
    permission_level: null,
    status: "idle",
    board_revision: boardRevision,
  };
}

function pages(conversation: Conversation): ConversationsInfiniteData {
  return {
    pages: [{ data: [conversation], first_id: "conv_a", last_id: "conv_a", has_more: false }],
    pageParams: [undefined],
  };
}

// Write the row into every cached list, as a refetch or the session stream does.
function deliver(client: QueryClient, conversation: Conversation) {
  client.setQueryData(LIST_KEY, pages(conversation));
  client.setQueryData(FOLDER_KEY, pages(conversation));
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

function cachedRow(client: QueryClient, key: readonly unknown[]) {
  return client.getQueryData<ConversationsInfiniteData>(key)?.pages[0].data[0];
}

// Each call's response waits until the test resolves it, in call order.
function deferredResponses() {
  const pending: ((response: Response) => void)[] = [];
  vi.mocked(authenticatedFetch).mockImplementation(
    () =>
      new Promise<Response>((resolve) => {
        pending.push(resolve);
      }),
  );
  return pending;
}

function cachedLabels(client: QueryClient, key: readonly unknown[]) {
  return cachedRow(client, key)?.labels;
}

function pinnedLabels(client: QueryClient) {
  return client.getQueryData<{ conversations: Conversation[] }>(PINNED_CONVERSATIONS_KEY)
    ?.conversations[0].labels;
}

function renderMutation(client: QueryClient) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(() => useSetBoardAccepted(), { wrapper });
}

afterEach(() => {
  vi.clearAllMocks();
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
    let respond: (response: Response) => void = () => {};
    vi.mocked(authenticatedFetch).mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
    );
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));

    // Optimistically just past the reviewed time, so the card reads as accepted.
    const optimistic = { keep: "1", [BOARD_ACCEPTED_LABEL_KEY]: "1700000101" };
    await waitFor(() => expect(cachedLabels(client, LIST_KEY)).toEqual(optimistic));
    expect(cachedLabels(client, FOLDER_KEY)).toEqual(optimistic);
    expect(pinnedLabels(client)).toEqual(optimistic);

    const stored = { keep: "1", [BOARD_ACCEPTED_LABEL_KEY]: "1700000105" };
    respond(new Response(JSON.stringify(row(stored)), { status: 200 }));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(cachedLabels(client, LIST_KEY)).toEqual(stored);
    expect(pinnedLabels(client)).toEqual(stored);
    expect(showToast).not.toHaveBeenCalled();
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
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify(row({ [BOARD_ACCEPTED_LABEL_KEY]: "1700000101" })), {
        status: 200,
      }),
    );
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
    respond[0](
      new Response(JSON.stringify(row({ [BOARD_ACCEPTED_LABEL_KEY]: "1700000101" })), {
        status: 200,
      }),
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    for (const key of [LIST_KEY, FOLDER_KEY]) expect(cachedRow(client, key)).toEqual(newer);
    expect(pinnedLabels(client)).toEqual(newer.labels);
    expect(boardColumn(cachedRow(client, LIST_KEY)!)).toBe("needs_review");
  });

  it("refetches when a list fetched before the write committed lands before the response", async () => {
    const respond = deferredResponses();
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(boardColumn(cachedRow(client, LIST_KEY)!)).toBe("done"));
    // Same revision, no acceptance yet: a list read taken before the PATCH committed.
    deliver(client, row());
    const stored = { keep: "1", [BOARD_ACCEPTED_LABEL_KEY]: "1700000101" };
    respond[0](new Response(JSON.stringify(row(stored)), { status: 200 }));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(client.getQueryState(LIST_KEY)?.isInvalidated).toBe(true);
    expect(client.getQueryState(PINNED_CONVERSATIONS_KEY)?.isInvalidated).toBe(true);
  });

  it("leaves newer activity alone when a Reopen fails after it streamed in", async () => {
    const respond = deferredResponses();
    const before = row({ [BOARD_ACCEPTED_LABEL_KEY]: "1700000101" });
    const client = seededClient(before);
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: before, reviewedRevision: null }));
    await waitFor(() =>
      expect(cachedLabels(client, LIST_KEY)?.[BOARD_ACCEPTED_LABEL_KEY]).toBe(""),
    );
    const newer = row({}, "rev_newer");
    deliver(client, newer);
    respond[0](new Response(JSON.stringify({ error: { message: "nope" } }), { status: 503 }));
    await waitFor(() => expect(result.current.isError).toBe(true));

    for (const key of [LIST_KEY, FOLDER_KEY]) expect(cachedRow(client, key)).toEqual(newer);
    expect(pinnedLabels(client)).toEqual(newer.labels);
    expect(showToast).toHaveBeenCalledWith("Couldn't reopen the session.");
  });

  it("lets only the latest write to a session settle its label", async () => {
    const respond = deferredResponses();
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(respond).toHaveLength(1));
    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));
    await waitFor(() => expect(respond).toHaveLength(2));
    // The first Accept fails while the second is still in flight.
    respond[0](new Response(JSON.stringify({ error: { message: "nope" } }), { status: 503 }));
    await waitFor(() => expect(showToast).toHaveBeenCalledWith("Couldn't accept the session."));
    expect(cachedLabels(client, LIST_KEY)?.[BOARD_ACCEPTED_LABEL_KEY]).toBe("1700000101");

    const stored = { keep: "1", [BOARD_ACCEPTED_LABEL_KEY]: "1700000105" };
    respond[1](new Response(JSON.stringify(row(stored)), { status: 200 }));
    await waitFor(() => expect(cachedLabels(client, LIST_KEY)).toEqual(stored));
    expect(pinnedLabels(client)).toEqual(stored);
  });

  it("restores the previous acceptance and says so when the PATCH fails", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ error: { message: "nope" } }), { status: 403 }),
    );
    const before = row({ [BOARD_ACCEPTED_LABEL_KEY]: "1700000101" });
    const client = seededClient(before);
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: before, reviewedRevision: null }));

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(cachedLabels(client, LIST_KEY)).toEqual(before.labels);
    expect(cachedLabels(client, FOLDER_KEY)).toEqual(before.labels);
    expect(showToast).toHaveBeenCalledWith("Couldn't reopen the session.");
    // The write may have committed before it failed, so the lists refetch.
    expect(client.getQueryState(PINNED_CONVERSATIONS_KEY)?.isInvalidated).toBe(true);
  });

  it("explains a refused Accept when the session changed since review", async () => {
    const message = "The session changed since you reviewed it. Review it again before accepting.";
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "conflict", message } }), { status: 409 }),
    );
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), reviewedRevision: "rev_reviewed" }));

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(cachedLabels(client, LIST_KEY)).toEqual(row().labels);
    expect(showToast).toHaveBeenCalledWith(message);
    // The lists refetch so a retry carries the session's current revision.
    expect(client.getQueryState(LIST_KEY)?.isInvalidated).toBe(true);
  });
});
