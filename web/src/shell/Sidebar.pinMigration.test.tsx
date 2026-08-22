// Regression tests for the UI-before-server pin-migration data loss.
//
// Pins moved from localStorage to a server-side `omnigent.pinned` label. The
// one-time migration must NOT run against a pre-upgrade server that ignores
// the `?pinned=true` filter — doing so PATCHes pins the old server can't
// per-user-scope AND clears the legacy key, so after the server upgrade every
// pin reads as unpinned. `usePinnedConversations` reports `filterHonored`;
// migration is gated on it. Meanwhile a still-local pin keeps rendering via the
// union of the server set and the legacy localStorage key.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { PINNED_CONVERSATION_IDS_STORAGE_KEY } from "@/shell/sidebarNav";

// Controls for the mocked pinned query and the migration write spy.
const { pinnedRef, filterHonoredRef, setPinnedSpy } = vi.hoisted(() => ({
  pinnedRef: { current: [] as unknown[] },
  filterHonoredRef: { current: true },
  // Resolves with a minimal confirmed row so a successful migration write
  // clears the legacy id.
  setPinnedSpy: vi.fn((id: string) =>
    Promise.resolve({ id, object: "conversation", labels: { "omnigent.pinned": "1" } }),
  ),
}));

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
    data: { conversations: pinnedRef.current, filterHonored: filterHonoredRef.current },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: setPinnedSpy,
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [] }),
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

function conv(id: string): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
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

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route path="/" element={<Sidebar open onClose={vi.fn()} />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useConvMock.mockReset();
  useProjectSessionsMock.mockReset();
  useProjectSessionsMock.mockReturnValue({
    data: {
      pages: [{ data: [], first_id: null, last_id: null, has_more: false }],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useProjectSessions>);
  pinnedRef.current = [];
  filterHonoredRef.current = true;
  setPinnedSpy.mockClear();
  localStorage.clear();
});

afterEach(cleanup);

describe("localStorage → server pin migration gate", () => {
  it("does NOT migrate (and preserves the legacy key) against an old server that ignores the filter", async () => {
    // Old server: filter not honored, so it reports no server pins.
    filterHonoredRef.current = false;
    pinnedRef.current = [];
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["chat_1"]));
    mockConversations([conv("chat_1")]);

    renderSidebar();

    // Give any (erroneous) async migration a chance to fire.
    await Promise.resolve();
    await Promise.resolve();

    // No pins were pushed to the server, and the legacy key is intact — so the
    // eventual server upgrade can migrate them instead of finding them gone.
    expect(setPinnedSpy).not.toHaveBeenCalled();
    expect(localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY)).toBe(
      JSON.stringify(["chat_1"]),
    );
  });

  it("still renders a legacy-only pin in the Pinned section against an old server", () => {
    // The pin must stay visible while migration is inert — union of the (empty)
    // server set and the legacy localStorage key.
    filterHonoredRef.current = false;
    pinnedRef.current = [];
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["chat_1"]));
    mockConversations([conv("chat_1")]);

    renderSidebar();

    expect(screen.getByText("Pinned")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /chat_1/ })).toBeInTheDocument();
  });

  it("migrates legacy pins and clears the legacy key against a new server", async () => {
    // New server honors the filter but doesn't yet know this pin.
    filterHonoredRef.current = true;
    pinnedRef.current = [];
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["chat_1"]));
    mockConversations([conv("chat_1")]);

    renderSidebar();

    await waitFor(() =>
      expect(setPinnedSpy).toHaveBeenCalledWith("chat_1", true, expect.any(Number)),
    );
    // The confirmed write drops the id from the legacy key (nothing left to retry).
    await waitFor(() =>
      expect(localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY)).toBeNull(),
    );
  });

  it("does not re-PATCH a pin the new server already owns", async () => {
    filterHonoredRef.current = true;
    // Server already reports chat_1 as pinned.
    pinnedRef.current = [conv("chat_1")];
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["chat_1"]));
    mockConversations([conv("chat_1")]);

    renderSidebar();

    await Promise.resolve();
    await Promise.resolve();

    expect(setPinnedSpy).not.toHaveBeenCalled();
    // Legacy key cleared: the server owns everything it listed.
    await waitFor(() =>
      expect(localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY)).toBeNull(),
    );
  });

  it("retains the legacy pin when its migration write fails (e.g. a 404 for a deleted session)", async () => {
    // Guards the empty-page ambiguity in `filterHonored`: an old server with
    // zero sessions returns an empty page (read as honored), so migration may
    // fire — but its PATCH to a now-deleted session 404s. The failed write must
    // leave the id in localStorage so no pin is silently lost.
    filterHonoredRef.current = true;
    pinnedRef.current = [];
    setPinnedSpy.mockRejectedValueOnce(new Error("404 Not Found"));
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["chat_gone"]));
    mockConversations([]);

    renderSidebar();

    await waitFor(() =>
      expect(setPinnedSpy).toHaveBeenCalledWith("chat_gone", true, expect.any(Number)),
    );
    // The write failed, so the id is kept for a later retry — not cleared.
    await waitFor(() =>
      expect(localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY)).toBe(
        JSON.stringify(["chat_gone"]),
      ),
    );
  });
});
