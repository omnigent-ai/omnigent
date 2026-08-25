// Behaviour tests for the mobile sidebar drawer shape: it stops short of the
// right edge so a strip of the chat stays visible, tapping that strip dismisses
// it (replacing the collapse toggle, which is now desktop-only), and Search /
// Settings float at the top and bottom of the drawer.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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

function renderSidebar(props: { open?: boolean; onClose?: () => void; route?: string } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[props.route ?? "/"]}>
          <Sidebar open={props.open ?? true} onClose={props.onClose ?? vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockConversations([conv("conv_a")]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("mobile sidebar drawer", () => {
  it("dismisses when the exposed strip of the chat is tapped", () => {
    const onClose = vi.fn();
    renderSidebar({ onClose });

    fireEvent.click(screen.getByTestId("sidebar-scrim"));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps the scrim inert while the drawer is closed", () => {
    renderSidebar({ open: false });

    const scrim = screen.getByTestId("sidebar-scrim");
    expect(scrim).toHaveClass("pointer-events-none", "opacity-0");
    // Untappable is not enough for a focusable control: parked, it must also
    // leave the tab order and the a11y tree. (Not `inert` — React 18 drops the
    // boolean form, so this would silently stay reachable.)
    expect(scrim).toHaveAttribute("tabindex", "-1");
    expect(scrim).toHaveAttribute("aria-hidden", "true");
  });

  it("exposes the dismiss as a labeled control, not a bare click target", () => {
    // With the collapse toggle gone on mobile, the scrim is the only
    // non-navigational way out — so it has to be reachable and announced,
    // not just tappable.
    renderSidebar();

    const scrim = screen.getByTestId("sidebar-scrim");
    expect(scrim.tagName).toBe("BUTTON");
    expect(scrim).toHaveAttribute("aria-label", "Close sidebar");
    expect(scrim).toHaveAttribute("tabindex", "0");
    expect(scrim).toHaveAttribute("aria-hidden", "false");
  });

  it("stacks the scrim above chat chrome but below the drawer", () => {
    // Chat chrome (ChatPage's jump-to-top pill) also sits at z-40 and renders
    // after the sidebar, so a same-z scrim would lose the tie inside the strip.
    renderSidebar();

    expect(screen.getByTestId("sidebar-scrim")).toHaveClass("z-[45]");
    expect(screen.getByRole("complementary", { name: "Conversations" })).toHaveClass("z-50");
  });

  it("stops the drawer short of the right edge so the chat stays reachable", () => {
    renderSidebar();

    expect(screen.getByRole("complementary", { name: "Conversations" })).toHaveClass(
      "max-md:right-14",
    );
  });

  it("drops the peek strip and the scrim on the settings page, where Back is the only exit", () => {
    renderSidebar({ route: "/settings" });

    expect(screen.queryByTestId("sidebar-scrim")).toBeNull();
    expect(screen.getByRole("complementary", { name: "Conversations" })).not.toHaveClass(
      "max-md:right-14",
    );
  });

  it("floats Settings at the bottom and hides the desktop collapse toggle on mobile", () => {
    renderSidebar();

    // Search stays in the header row (top); Settings gets its own float at the
    // bottom of the session list.
    const headerActions = screen.getByTestId("sidebar-header-actions");
    expect(within(headerActions).getByTestId("sidebar-search-button")).toBeInTheDocument();

    const float = screen.getByTestId("sidebar-settings-float");
    expect(float).toHaveAttribute("href", "/settings");
    expect(float.closest("a, button")).toHaveClass("absolute", "bottom-3", "md:hidden");

    // The header-row Settings copy and the collapse toggle are desktop-only.
    expect(screen.getByTestId("settings-button")).toHaveClass("max-md:hidden");
    expect(within(headerActions).getByRole("button", { name: "Close sidebar" })).toHaveClass(
      "max-md:hidden",
    );
  });

  it("gives both floating chips one shared glass treatment", () => {
    // These drifted apart once — Settings landed opaque with a heavier shadow
    // than Search. The look now lives in a single CSS class, so assert both
    // wear it and neither carries a competing background or shadow utility.
    renderSidebar();

    const search = screen.getByTestId("sidebar-search-button");
    const settings = screen.getByTestId("sidebar-settings-float").closest("a, button")!;

    // Where the chip sits, and which breakpoint it shows at, legitimately
    // differ; everything about how it looks must not.
    // `relative` is the Button base's own position, which tailwind-merge drops
    // from the floating copy in favour of `absolute` — placement, not looks.
    const PLACEMENT = new Set([
      "absolute",
      "relative",
      "right-3",
      "bottom-3",
      "md:hidden",
      "max-md:hidden",
    ]);
    const appearance = (el: Element) =>
      el.className
        .split(/\s+/)
        .filter((c) => c && !PLACEMENT.has(c))
        .sort()
        .join(" ");

    expect(search).toHaveClass("sidebar-glass-chip");
    expect(settings).toHaveClass("sidebar-glass-chip");
    expect(appearance(settings)).toBe(appearance(search));
  });

  it("gives the session list a gutter so the last row clears the floating chip", () => {
    // The chip doesn't scroll, so without this the bottom row's always-visible
    // kebab sits underneath it and can't be tapped.
    renderSidebar();

    expect(screen.getByRole("navigation")).toHaveClass("max-md:pb-14");
  });
});
