// Layout regression tests for the sidebar's vertical spacing rhythm. These
// lock in the padding/gap tuning the design pass settled on:
//   - Primary nav group (New session / Automations / Inbox) reads as a proper
//     section: 8px above it (to the Omnigent header) and no bottom padding of
//     its own; the 16px gap below comes from the scrolling list.
//   - Nav rows are 32px tall (h-8) with 4px top/bottom padding (py-1).
//   - Section headers (Pinned / Projects / Sessions) carry 8px bottom padding.
//   - Session rows are 32px tall (h-8) with 4px padding and sit flush (no
//     inter-row gap).
//   - Project folder rows also sit flush (no inter-row gap).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
  usePinnedConversations: () => ({
    data: { conversations: [], filterHonored: true },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
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
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

function makeConv(id: string, title: string): Conversation {
  return {
    id,
    object: "conversation",
    title,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    labels: { "omnigent.wrapper": "claude-code-native-ui" },
    permission_level: null,
    status: "idle",
  };
}

function mockConversations(conversations: Conversation[]) {
  const withData = {
    data: {
      pages: [
        {
          data: conversations,
          first_id: conversations[0]?.id ?? null,
          last_id: conversations.at(-1)?.id ?? null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useConversations>;
  useConvMock.mockImplementation(() => withData);
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open={true} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockConversations([makeConv("conv_1", "My Session"), makeConv("conv_2", "Other Session")]);
});

afterEach(() => {
  cleanup();
});

describe("sidebar vertical spacing", () => {
  it("gives the primary nav an 8px top gap and no bottom padding", () => {
    renderSidebar();
    const nav = screen.getByTestId("sidebar-primary-nav");
    // pt-2 = 8px gap to the Omnigent header; pb-0 so the 16px gap below comes
    // from the scrolling list, not the nav's own padding.
    expect(nav.className).toMatch(/\bpt-2\b/);
    expect(nav.className).toMatch(/\bpb-0\b/);
    expect(nav.className).not.toMatch(/\bpb-3\b/);
  });

  it("adds a 16px top gap on the scrolling conversation list (nav -> Pinned rhythm)", () => {
    renderSidebar();
    // The list's pt-4 (16px) produces the section-to-section gap below the
    // primary nav, matching the gap-4 used between sections.
    const list = screen.getByTestId("sidebar-conversation-list");
    const scroller = list.closest("nav") as HTMLElement;
    expect(scroller.className).toMatch(/\bpt-4\b/);
  });

  it("keeps the 16px inter-section gap between Pinned / Projects / Sessions", () => {
    renderSidebar();
    const list = screen.getByTestId("sidebar-conversation-list");
    expect(list.className).toMatch(/\bgap-4\b/);
  });

  it("sizes nav rows at 32px (h-8) with 4px vertical padding (py-1)", () => {
    renderSidebar();
    const newChat = screen.getByTestId("new-chat-button");
    expect(newChat.className).toMatch(/\bh-8\b/);
    expect(newChat.className).toMatch(/\bpy-1\b/);
    expect(newChat.className).not.toMatch(/\bh-7\b/);
  });

  it("gives section headers 8px bottom padding", () => {
    renderSidebar();
    const sessions = screen.getByRole("button", { name: /Sessions/ });
    expect(sessions.className).toMatch(/\bpb-2\b/);
    expect(sessions.className).not.toMatch(/\bpb-1\b/);
  });

  it("sizes session rows at 32px (h-8) with 4px vertical padding (py-1)", () => {
    renderSidebar();
    const row = screen.getByRole("link", { name: /My Session/ });
    expect(row.className).toMatch(/\bh-8\b/);
    expect(row.className).toMatch(/\bpy-1\b/);
    expect(row.className).not.toMatch(/\bpy-0\.5\b/);
  });

  it("stacks session rows flush with no inter-row gap", () => {
    renderSidebar();
    const row = screen.getByRole("link", { name: /My Session/ });
    const list = row.closest("ul") as HTMLElement;
    expect(list.className).toMatch(/\bgap-0\b/);
    expect(list.className).not.toMatch(/\bgap-0\.5\b/);
  });
});
