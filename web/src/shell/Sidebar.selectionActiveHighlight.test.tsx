// Regression test for: toggling "Select sessions" left the currently-viewed
// session's row highlighted. In selection mode the active-route highlight must
// be suppressed — a row should carry a background only when it's explicitly
// checked, so the selection state reads unambiguously.

import type * as SessionsApiModule from "@/lib/sessionsApi";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import type { Session } from "@/lib/types";

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useLeaveSession: () => ({ mutate: vi.fn(), isPending: false }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkMoveToProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({ mutate: vi.fn() }),
  usePinnedConversations: () => ({
    data: { conversations: [], filterHonored: true },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [] }),
  useProjectSessions: () => ({ data: undefined, isLoading: false, isError: false, error: null }),
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

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSlim: vi.fn(),
}));

import { useConversations } from "@/hooks/useConversations";
import { getSessionSlim } from "@/lib/sessionsApi";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);
const getSessionSlimMock = vi.mocked(getSessionSlim);

function topLevelConv(id: string): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    agent_name: "Claude Code",
  };
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

function snapshot(id: string, parentSessionId: string | null): Session {
  return {
    id,
    agentId: "ag",
    agentName: null,
    runnerId: null,
    status: "idle",
    createdAt: 0,
    title: null,
    labels: {},
    items: [],
    pendingElicitations: [],
    permissionLevel: 4,
    parentSessionId,
  } as unknown as Session;
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
  getSessionSlimMock.mockReset();
  localStorage.clear();
});

afterEach(cleanup);

function rowFor(id: string): HTMLElement {
  return screen.getByRole("link", { name: new RegExp(id) });
}

describe("sidebar active highlight in selection mode", () => {
  it("drops the active-session highlight once selection mode is on", async () => {
    mockConversations([topLevelConv("conv_active"), topLevelConv("conv_other")]);
    getSessionSlimMock.mockImplementation((id: string) => Promise.resolve(snapshot(id, null)));

    // Viewing conv_active — it starts highlighted as the active route.
    renderAt("/c/conv_active");
    await waitFor(() => expect(rowFor("conv_active")).toHaveClass("bg-[var(--sidebar-active)]"));

    // Toggle "Select sessions": the active highlight must clear because no row
    // is explicitly selected yet.
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
    await waitFor(() =>
      expect(rowFor("conv_active")).not.toHaveClass("bg-[var(--sidebar-active)]"),
    );
    expect(rowFor("conv_other")).not.toHaveClass("bg-[var(--sidebar-active)]");
  });

  it("highlights only the explicitly-selected row, not the active one", async () => {
    mockConversations([topLevelConv("conv_active"), topLevelConv("conv_other")]);
    getSessionSlimMock.mockImplementation((id: string) => Promise.resolve(snapshot(id, null)));

    renderAt("/c/conv_active");
    await waitFor(() => expect(rowFor("conv_active")).toHaveClass("bg-[var(--sidebar-active)]"));

    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));

    // Explicitly select the OTHER row — it gets the highlight; the active row
    // stays unhighlighted because it wasn't selected.
    fireEvent.click(rowFor("conv_other"));
    await waitFor(() => expect(rowFor("conv_other")).toHaveClass("bg-[var(--sidebar-active)]"));
    expect(rowFor("conv_active")).not.toHaveClass("bg-[var(--sidebar-active)]");
  });
});
