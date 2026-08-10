// Behaviour tests for the My sessions / Shared with me scope tabs during bulk
// selection mode. Two guarantees:
//   1. The tabs stay visible while selection mode is active (previously the
//      whole strip was hidden, stranding the viewer on one scope).
//   2. Switching tabs exits selection mode, so the bulk-action count never
//      carries stale rows from the other (disjoint, ownership-scoped) tab.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
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
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

// The scope tabs only render on a multi-user (non-local) server; force it so
// the tabs are present. jsdom's loopback origin would otherwise read as
// single-user and hide them.
vi.mock("@/lib/serverOrigin", () => ({
  isCurrentServerLocal: () => false,
  isLocalServerOrigin: (origin: string) =>
    ["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"].includes(new URL(origin).hostname),
}));

import { useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

function conv(id: string, partial: Partial<Conversation> = {}): Conversation {
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

// The resolved viewer id is null in tests, so a null/absent owner reads as
// owned ("My sessions") and any non-null owner reads as shared.
const OWNED = conv("conv_mine", { owner: null });
const SHARED = conv("conv_shared", { owner: "other@example.com" });

function mockConversations(conversations: Conversation[]) {
  useConvMock.mockImplementation(
    () =>
      ({
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
      }) as unknown as ReturnType<typeof useConversations>,
  );
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

/** Pick a scope from the Sessions heading's filter menu. */
function switchTo(testId: "sidebar-tab-mine" | "sidebar-tab-shared") {
  const value = testId === "sidebar-tab-mine" ? "mine" : "shared";
  fireEvent.pointerDown(screen.getByTestId("session-filter"), {
    button: 0,
    ctrlKey: false,
    pointerType: "mouse",
  });
  fireEvent.click(screen.getByTestId(`session-filter-${value}`));
}

beforeEach(() => {
  mockConversations([OWNED, SHARED]);
});

afterEach(cleanup);

describe("scope filter during selection mode", () => {
  it("keeps the filter menu reachable after entering selection mode", () => {
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));

    // In selection mode the bulk-action bar is up...
    expect(screen.getByRole("button", { name: "Exit selection mode" })).toBeInTheDocument();
    // ...and the scope filter is still reachable, so the viewer can switch
    // between owned and shared sessions without leaving selection first.
    expect(screen.getByTestId("session-filter")).toBeInTheDocument();
  });

  it("exits selection mode when the viewer switches scope", () => {
    renderSidebar();

    // Enter selection mode and pick the owned session.
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
    fireEvent.click(screen.getByRole("link", { name: /conv_mine/ }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    // Switching to the Shared tab drops selection mode entirely (rather than
    // carrying the owned selection into a disjoint scope with a stale count).
    switchTo("sidebar-tab-shared");
    expect(screen.queryByRole("button", { name: "Exit selection mode" })).toBeNull();
    expect(screen.queryByText(/\d+ selected/)).toBeNull();

    // The shared session is now shown and not pre-selected; re-entering
    // selection here starts clean.
    expect(screen.getByText("conv_shared")).toBeInTheDocument();
  });

  it("starts a fresh selection after switching scope (no rows carried over)", () => {
    renderSidebar();

    // Select on the Shared tab first.
    switchTo("sidebar-tab-shared");
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
    fireEvent.click(screen.getByRole("link", { name: /conv_shared/ }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    // Back to My sessions: selection mode is gone. Re-entering shows a clean
    // zero count — the shared row's selection did not bleed across.
    switchTo("sidebar-tab-mine");
    expect(screen.queryByText("1 selected")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
    expect(screen.getByText("0 selected")).toBeInTheDocument();
  });

  it("exits selection mode when 'New session' snaps the tab back to mine", () => {
    renderSidebar();

    // Enter selection mode on the Shared tab and pick the shared session.
    switchTo("sidebar-tab-shared");
    fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
    fireEvent.click(screen.getByRole("link", { name: /conv_shared/ }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    // "New session" snaps the tab back to "mine" outside the Tabs
    // onValueChange path; it must still drop selection mode (not strand a
    // stale shared-scope count under the My-sessions header).
    fireEvent.click(screen.getByTestId("new-chat-button"));
    expect(screen.queryByRole("button", { name: "Exit selection mode" })).toBeNull();
    expect(screen.queryByText("1 selected")).toBeNull();
  });
});
