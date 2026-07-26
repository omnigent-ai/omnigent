import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { type Conversation, PINNED_CONVERSATIONS_KEY } from "@/hooks/useConversations";
import { COMPLETED_LABEL_KEY, type ConversationsInfiniteData } from "@/lib/sessionListCache";
import { authenticatedFetch } from "@/lib/identity";
import { useToggleCompletedConversation } from "./useToggleCompletedConversation";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const row: Conversation = {
  id: "conv_done",
  object: "conversation",
  title: "Done",
  created_at: 1,
  updated_at: 2,
  labels: {},
  permission_level: null,
};

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
});

it("patches completion into the existing list caches without a separate feed", async () => {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const page: ConversationsInfiniteData = {
    pages: [{ data: [row], first_id: row.id, last_id: row.id, has_more: false }],
    pageParams: [undefined],
  };
  client.setQueryData(["conversations", "", true], page);
  client.setQueryData(["project-sessions", "Project"], page);
  client.setQueryData(PINNED_CONVERSATIONS_KEY, [row]);
  vi.mocked(authenticatedFetch).mockResolvedValue({
    ok: true,
    json: async () => ({
      ...row,
      labels: { [COMPLETED_LABEL_KEY]: "1721760000000" },
    }),
  } as Response);

  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const rendered = renderHook(() => useToggleCompletedConversation(), { wrapper });

  act(() => rendered.result.current.mutate({ id: row.id, completed: true }));
  await waitFor(() => expect(rendered.result.current.isSuccess).toBe(true));

  for (const key of [
    ["conversations", "", true],
    ["project-sessions", "Project"],
  ]) {
    const cached = client.getQueryData<ConversationsInfiniteData>(key);
    expect(cached?.pages[0].data[0].labels[COMPLETED_LABEL_KEY]).toBe("1721760000000");
  }
  expect(
    client.getQueryData<Conversation[]>(PINNED_CONVERSATIONS_KEY)?.[0].labels[COMPLETED_LABEL_KEY],
  ).toBe("1721760000000");
  expect(JSON.parse(String(vi.mocked(authenticatedFetch).mock.calls[0][1]?.body))).toEqual({
    labels: { [COMPLETED_LABEL_KEY]: expect.any(String) },
  });
});
