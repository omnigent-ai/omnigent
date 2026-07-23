// Regression test for #2506: clicking a pinned session that belongs to a
// project was auto-expanding the project folder every time, undoing the
// user's manual collapse. The auto-expand effect exists so navigating to a
// filed-but-not-pinned session reveals it; a pinned session is already
// reachable from the Pinned section, so the effect must skip pinned targets.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import {
  EXPANDED_PROJECT_SECTIONS_STORAGE_KEY,
  PINNED_CONVERSATION_IDS_STORAGE_KEY,
} from "@/shell/sidebarNav";

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({ mutate: vi.fn() }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [{ id: "p_repro", name: "Repro 2506" }] }),
  useProjectSessions: vi.fn(),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRenameProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations, useProjectSessions } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);
const useProjectSessionsMock = vi.mocked(useProjectSessions);

function conv(id: string, project?: string): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: project ? { omni_project: project } : {},
    permission_level: null,
    agent_name: null,
  } as unknown as Conversation;
}

function mockConversations(convs: Conversation[]) {
  useConvMock.mockReturnValue({
    data: {
      pages: [{ data: convs, first_id: null, last_id: null, has_more: false }],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useConversations>);
}

function renderAt(initialEntry: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route path="/" element={<Sidebar open onClose={vi.fn()} />} />
            <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useConvMock.mockReset();
  useProjectSessionsMock.mockReset();
  // ProjectFolder fetches its own paginated list when expanded. The tests
  // above already seed the top-level list; mirror it for the folder query so
  // an expanded folder actually renders rows.
  useProjectSessionsMock.mockReturnValue({
    data: {
      pages: [
        { data: [conv("sibling_b", "Repro 2506")], first_id: null, last_id: null, has_more: false },
      ],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useProjectSessions>);
  localStorage.clear();
});

afterEach(cleanup);

describe("sidebar auto-expand vs. pinned sessions (#2506)", () => {
  it("does not auto-expand a collapsed project when navigating to a pinned member", () => {
    // Pinned session `pinned_a` + a project sibling `sibling_b`, both filed
    // under "Repro 2506". Project starts collapsed (empty
    // EXPANDED_PROJECT_SECTIONS_STORAGE_KEY), pinned id already stored.
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["pinned_a"]));
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify([]));
    mockConversations([conv("pinned_a", "Repro 2506"), conv("sibling_b", "Repro 2506")]);

    renderAt("/c/pinned_a");

    // The pinned row is present (via the Pinned section).
    expect(screen.getByRole("link", { name: /pinned_a/ })).toBeInTheDocument();
    // But the project's non-pinned member is NOT rendered — proof that the
    // project folder stayed collapsed. Before the fix, the auto-expand effect
    // opened the folder and this row would be visible.
    expect(screen.queryByRole("link", { name: /sibling_b/ })).not.toBeInTheDocument();
    // And the persistent flag is untouched.
    expect(localStorage.getItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY)).toBe(JSON.stringify([]));
  });

  it("still auto-expands the project when navigating to a filed, non-pinned member", () => {
    // Same shape, but the active route is the NON-pinned sibling. This is the
    // intended auto-expand path: without opening the folder, the row is
    // invisible and the user would land on a "hidden" session.
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["pinned_a"]));
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify([]));
    mockConversations([conv("pinned_a", "Repro 2506"), conv("sibling_b", "Repro 2506")]);

    renderAt("/c/sibling_b");

    // Folder opened, sibling visible.
    expect(screen.getByRole("link", { name: /sibling_b/ })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY) ?? "[]")).toEqual(
      ["Repro 2506"],
    );
  });
});
