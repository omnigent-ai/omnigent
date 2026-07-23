// Regression test for: clicking a sub-agent in the right rail dropped the
// owning session's highlight in the left sidebar.
//
// The sidebar lists only top-level sessions — child (sub-agent) rows are
// omitted. ConversationRow highlights the row whose id matches the active
// route param. When the user clicks a sub-agent the URL becomes
// `/c/<childId>`, which matches no sidebar row, so the parent row lost its
// neutral sidebar-active highlight. The fix resolves the active conversation's
// top-level root (via useActiveRootSessionId, walking parentSessionId) and
// highlights against that, so the parent stays selected while viewing any
// descendant.
//
// We mock the conversations list (top-level only, as in production) and the
// per-session snapshot API so the REAL useSession / useRootSessionId chain
// resolves the child up to its root.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import type { Session } from "@/lib/types";

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
  useProjects: () => ({ data: [] }),
  useProjectSessions: () => ({ data: undefined, isLoading: false, isError: false, error: null }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRenameProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

// The snapshot API feeds the real useSession / useRootSessionId walk: the
// child reports its parent, the parent reports null (top-level).
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/sessionsApi")>()),
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
  // ConversationRow renders the title (== id here) inside its <a>; the <li>
  // wrapper carries the highlight class via the link's className.
  return screen.getByRole("link", { name: new RegExp(id) });
}

describe("sidebar highlight while viewing a sub-agent", () => {
  it("renders the official Omnigent wordmark instead of styled text", () => {
    mockConversations([]);
    renderAt("/");

    const wordmark = screen.getByTestId("sidebar-wordmark");
    expect(wordmark).toHaveAttribute("alt", "Omnigent");
    expect(wordmark).toHaveClass("h-[15px]", "dark:invert");
    expect(wordmark.getAttribute("src")).toContain("omnigent-wordmark");
  });

  it("uses the same Otto structural-container radius as the workspace rail", () => {
    mockConversations([]);
    renderAt("/");

    expect(screen.getByRole("complementary", { name: "Conversations" })).toHaveClass(
      "md:rounded-[var(--radius-otto-md)]",
      "md:m-2",
    );
  });

  it("highlights the top-level parent row when the active session is its child", async () => {
    mockConversations([topLevelConv("conv_root"), topLevelConv("conv_other")]);
    // conv_child is a sub-agent of conv_root and is NOT in the sidebar list.
    getSessionSlimMock.mockImplementation((id: string) => {
      if (id === "conv_child") return Promise.resolve(snapshot("conv_child", "conv_root"));
      if (id === "conv_root") return Promise.resolve(snapshot("conv_root", null));
      return Promise.resolve(snapshot(id, null));
    });

    // Active route is the CHILD — no matching sidebar row.
    renderAt("/c/conv_child");

    // Once the parent walk resolves, the root's row carries the highlight.
    await waitFor(() => expect(rowFor("conv_root")).toHaveClass("bg-[var(--sidebar-active)]"));
    expect(rowFor("conv_other")).not.toHaveClass("bg-[var(--sidebar-active)]");
  });

  it("still highlights a top-level session viewed directly", async () => {
    mockConversations([topLevelConv("conv_root"), topLevelConv("conv_other")]);
    getSessionSlimMock.mockImplementation((id: string) => Promise.resolve(snapshot(id, null)));

    renderAt("/c/conv_root");

    // A top-level session resolves to itself; highlight lands immediately and
    // doesn't bleed onto siblings.
    await waitFor(() => expect(rowFor("conv_root")).toHaveClass("bg-[var(--sidebar-active)]"));
    expect(rowFor("conv_other")).not.toHaveClass("bg-[var(--sidebar-active)]");
  });
});
