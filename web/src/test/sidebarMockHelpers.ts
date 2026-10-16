/** Default hook stubs and data builders for Sidebar component tests. */
import { vi } from "vitest";
import type { Conversation, useConversations } from "@/hooks/useConversations";
import type { SidebarListQuery } from "@/hooks/useSidebarData";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";

export function conversationHooksMock() {
  return {
    useConversations: vi.fn(),
    useStopAndDeleteConversation: () => ({
      mutate: vi.fn(),
      reset: vi.fn(),
      isPending: false,
      isError: false,
      variables: undefined,
    }),
    usePinnedConversations: () => ({
      data: { conversations: [], filterHonored: true },
      isSuccess: true,
    }),
    useTogglePinnedConversation: () => ({
      mutate: vi.fn(),
      mutateAsync: vi.fn(() => Promise.resolve({})),
    }),
    useReorderPinnedConversations: () => ({ mutate: vi.fn() }),
    setConversationPinned: vi.fn(() => Promise.resolve({})),
    PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
    useRenameConversation: () => ({ mutate: vi.fn() }),
    useLeaveSession: () => ({ mutate: vi.fn(), isPending: false }),
    useArchiveConversation: () => ({ mutate: vi.fn() }),
    useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    useBulkMoveToProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    useStopSession: () => ({ mutate: vi.fn() }),
    useProjects: () => ({ data: [] }),
    useProjectSessions: () => ({
      data: undefined,
      isLoading: false,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    }),
    useMoveToProject: () => ({ mutate: vi.fn() }),
    useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    useRenameProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    useProjectConfig: () => ({ data: undefined, isLoading: false }),
    useUpdateProjectConfig: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
    fetchProjectSessionIds: () => Promise.resolve([]),
    PROJECT_LABEL_KEY,
  };
}

export function conversation(id: string, partial: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    status: "idle",
    ...partial,
  };
}

export function conversationPage(
  conversations: Conversation[],
  bounds: { first_id: string | null; last_id: string | null } = {
    first_id: conversations[0]?.id ?? null,
    last_id: conversations.at(-1)?.id ?? null,
  },
) {
  const result = {
    data: {
      pages: [{ data: conversations, ...bounds, has_more: false }],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    isFetching: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } satisfies SidebarListQuery;
  return result as unknown as ReturnType<typeof useConversations>;
}
