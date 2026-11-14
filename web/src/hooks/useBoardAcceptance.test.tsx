import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { showToast } from "@/components/ui/toast";
import type * as identity from "@/lib/identity";
import { authenticatedFetch } from "@/lib/identity";
import { BOARD_ACCEPTED_LABEL_KEY } from "@/lib/sessionBoard";
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

function row(labels: Record<string, string> = {}): Conversation {
  return {
    id: "conv_a",
    object: "conversation",
    title: "A",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_100,
    labels: { keep: "1", ...labels },
    permission_level: null,
    status: "idle",
  };
}

function pages(conversation: Conversation): ConversationsInfiniteData {
  return {
    pages: [{ data: [conversation], first_id: "conv_a", last_id: "conv_a", has_more: false }],
    pageParams: [undefined],
  };
}

function seededClient(conversation: Conversation) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(LIST_KEY, pages(conversation));
  client.setQueryData(FOLDER_KEY, pages(conversation));
  client.setQueryData(PINNED_CONVERSATIONS_KEY, {
    conversations: [conversation],
    filterHonored: true,
  });
  return client;
}

function cachedLabels(client: QueryClient, key: readonly unknown[]) {
  return client.getQueryData<ConversationsInfiniteData>(key)?.pages[0].data[0].labels;
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
    await setSessionBoardAccepted("conv a", 1_700_000_100);
    await setSessionBoardAccepted("conv a", null);

    const calls = vi.mocked(authenticatedFetch).mock.calls;
    expect(calls[0][0]).toBe("/v1/sessions/conv%20a");
    expect(calls[0][1]?.method).toBe("PATCH");
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({
      labels: { [BOARD_ACCEPTED_LABEL_KEY]: "1700000100" },
    });
    expect(JSON.parse(String(calls[1][1]?.body))).toEqual({
      labels: { [BOARD_ACCEPTED_LABEL_KEY]: "" },
    });
  });
});

describe("useSetBoardAccepted", () => {
  it("writes the label into every cached copy of the row before the PATCH returns", async () => {
    let respond: (response: Response) => void = () => {};
    vi.mocked(authenticatedFetch).mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
    );
    const client = seededClient(row());
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: row(), acceptedAt: 1_700_000_100 }));

    const expected = { keep: "1", [BOARD_ACCEPTED_LABEL_KEY]: "1700000100" };
    await waitFor(() => expect(cachedLabels(client, LIST_KEY)).toEqual(expected));
    expect(cachedLabels(client, FOLDER_KEY)).toEqual(expected);
    expect(
      client.getQueryData<{ conversations: Conversation[] }>(PINNED_CONVERSATIONS_KEY)
        ?.conversations[0].labels,
    ).toEqual(expected);

    respond(new Response(JSON.stringify(row(expected)), { status: 200 }));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(cachedLabels(client, LIST_KEY)).toEqual(expected);
    expect(showToast).not.toHaveBeenCalled();
  });

  it("restores the previous acceptance and says so when the PATCH fails", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ error: { message: "nope" } }), { status: 403 }),
    );
    const before = row({ [BOARD_ACCEPTED_LABEL_KEY]: "1700000100" });
    const client = seededClient(before);
    const { result } = renderMutation(client);

    act(() => result.current.mutate({ row: before, acceptedAt: null }));

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(cachedLabels(client, LIST_KEY)).toEqual(before.labels);
    expect(cachedLabels(client, FOLDER_KEY)).toEqual(before.labels);
    expect(showToast).toHaveBeenCalledWith("Couldn't reopen the session.");
  });
});
