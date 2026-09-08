// Behaviour tests for the peek card's entry window: while the card is still
// fading in it is (nearly) invisible yet already covers the header toggle
// whose hover armed it, so it must stay click-through — otherwise a fast
// click aimed at the toggle lands on invisible sidebar content (the brand
// link navigates the user to "/"). The card takes the pointer over only once
// its entry animation completes.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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

function sidebarAt(props: { open: boolean; peek?: boolean }) {
  return <Sidebar open={props.open} peek={props.peek} onClose={vi.fn()} onOpen={vi.fn()} />;
}

function renderSidebar(props: { open: boolean; peek?: boolean }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>{sidebarAt(props)}</MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return {
    ...view,
    rerenderSidebar: (next: { open: boolean; peek?: boolean }) =>
      view.rerender(
        <QueryClientProvider client={qc}>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/"]}>{sidebarAt(next)}</MemoryRouter>
          </TooltipProvider>
        </QueryClientProvider>,
      ),
  };
}

function card() {
  return screen.getByRole("complementary", { name: "Conversations" });
}

function endAnimation(element: Element = card()) {
  // jsdom lacks AnimationEvent, so React 18 listens for its WebKit fallback.
  fireEvent(element, new Event("webkitAnimationEnd", { bubbles: true }));
}

beforeEach(() => {
  mockConversations([conv("conv_a")]);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("peek card entry window", () => {
  it("starts click-through so the toggle underneath keeps the click", () => {
    renderSidebar({ open: false, peek: true });

    expect(card()).toHaveClass("pointer-events-none");
  });

  it("takes the pointer over once its own entry animation completes", () => {
    renderSidebar({ open: false, peek: true });

    endAnimation();

    expect(card()).not.toHaveClass("pointer-events-none");
  });

  it("takes the pointer over if the entry animation end never fires", () => {
    vi.useFakeTimers();
    renderSidebar({ open: false, peek: true });

    act(() => vi.advanceTimersByTime(200));

    expect(card()).not.toHaveClass("pointer-events-none");
  });

  it("is immediately interactive when reduced motion is preferred", () => {
    const defaultMatchMedia = window.matchMedia;
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
      ...defaultMatchMedia(query),
      matches: query === "(prefers-reduced-motion: reduce)",
    }));

    renderSidebar({ open: false, peek: true });

    expect(card()).not.toHaveClass("pointer-events-none");
  });

  it("ignores a child's bubbling animation end", () => {
    // Rows and badges inside the card animate too; their animationend events
    // bubble to the aside and must not cut the click-through window short.
    renderSidebar({ open: false, peek: true });

    const child = card().querySelector("div");
    expect(child).not.toBeNull();
    endAnimation(child as Element);

    expect(card()).toHaveClass("pointer-events-none");
  });

  it("is click-through again on the next peek", () => {
    const view = renderSidebar({ open: false, peek: true });
    endAnimation();
    expect(card()).not.toHaveClass("pointer-events-none");

    view.rerenderSidebar({ open: false, peek: false });
    view.rerenderSidebar({ open: false, peek: true });

    expect(card()).toHaveClass("pointer-events-none");
  });

  it("never blocks pointer events while docked open", () => {
    renderSidebar({ open: true });

    expect(card()).not.toHaveClass("pointer-events-none");
  });
});
