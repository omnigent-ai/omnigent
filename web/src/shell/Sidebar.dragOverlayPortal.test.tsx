// The session-drag preview (dnd-kit DragOverlay) must render in a portal under
// <body>, never inline inside the sidebar <aside>. The aside always carries a
// CSS translate (the mobile slide-in), which makes it the containing block for
// position:fixed descendants — an overlay rendered inline resolves its viewport
// coordinates against the aside's box and drifts away from the cursor whenever
// the aside sits off (0,0), e.g. while it peeks as a floating card.

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
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

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
          <Sidebar open onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

/** Activate a real dnd-kit drag on a session row: press, then travel past the
    MouseSensor's 5px activation distance so the DragOverlay mounts. */
function startRowDrag(row: HTMLElement) {
  fireEvent.mouseDown(row, { button: 0, clientX: 10, clientY: 10 });
  fireEvent.mouseMove(document, { clientX: 30, clientY: 40 });
  fireEvent.mouseMove(document, { clientX: 60, clientY: 80 });
}

beforeEach(() => {
  mockConversations([conv("conv_a")]);
});

afterEach(() => {
  fireEvent.mouseUp(document);
  cleanup();
  vi.clearAllMocks();
});

describe("session drag preview portal", () => {
  it("renders the drag preview under <body>, outside the translated aside", () => {
    const { container } = renderSidebar();

    const row = screen.getByRole("link", { name: "conv_a" }).closest("li");
    expect(row).not.toBeNull();
    startRowDrag(row!);

    // The preview card is the truncated compact card the overlay draws.
    const card = document.body.querySelector('[class*="max-w-[16rem]"]');
    expect(card, "drag preview did not mount — the drag never activated").not.toBeNull();

    // The invariant under test: the overlay lives in a portal under <body>,
    // not inside the aside (whose translate would re-anchor its fixed
    // coordinates and drag the preview away from the cursor).
    const aside = screen.getByRole("complementary", { name: "Conversations" });
    expect(aside.contains(card!)).toBe(false);
    expect(container.contains(card!)).toBe(false);
    expect(document.body.contains(card!)).toBe(true);
  });
});
