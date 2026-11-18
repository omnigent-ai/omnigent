import { conversationPage } from "@/test/sidebarMockHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type ReactNode, Suspense } from "react";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
// Navigating to a session never opens its project folder. A collapsed folder lists
// only the open session (its held row), so folders open and close only on request.

import type * as SessionsApiModule from "@/lib/sessionsApi";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { sessionNumberAriaShortcut } from "@/components/KeyboardShortcut";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { sidebarConfig } from "@/lib/sidebarConfig";
import type { Session } from "@/lib/types";
import { EXPANDED_PROJECT_SECTIONS_STORAGE_KEY } from "@/shell/sidebarNav";

// Server-authoritative pinned set for the mock; tests push the pinned conv here.
const { pinnedRef } = vi.hoisted(() => ({ pinnedRef: { current: [] as unknown[] } }));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    usePinnedConversations: () => ({
      data: { conversations: pinnedRef.current, filterHonored: true },
      isSuccess: true,
    }),
    useProjects: () => ({ data: [{ id: "p_repro", name: "Repro 2506" }] }),
    useProjectSessions: vi.fn(),
  };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
// Snapshot reads keep their real behavior unless a test stubs a child's parent.
vi.mock("@/lib/sessionsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof SessionsApiModule>();
  return { ...actual, getSessionSlim: vi.fn(actual.getSessionSlim) };
});

import { useConversations, useProjectSessions } from "@/hooks/useConversations";
import { getSessionSlim } from "@/lib/sessionsApi";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);
const useProjectSessionsMock = vi.mocked(useProjectSessions);
const getSessionSlimMock = vi.mocked(getSessionSlim);

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
  useConvMock.mockReturnValue(conversationPage(convs, { first_id: null, last_id: null }));
}

function renderAt(initialEntry: string, config = sidebarConfig) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = () => (
    <QueryClientProvider client={qc}>
      <SidebarDataProvider config={config}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[initialEntry]}>
            <Routes>
              <Route path="/" element={<Sidebar open onClose={vi.fn()} />} />
              <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>
  );
  const result = render(tree());
  return { ...result, rerenderSidebar: () => result.rerender(tree()) };
}

function expandedProjects(): unknown {
  return JSON.parse(localStorage.getItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY) ?? "[]");
}

beforeEach(() => {
  useConvMock.mockReset();
  useProjectSessionsMock.mockReset();
  getSessionSlimMock.mockReset();
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
  pinnedRef.current = [];
  localStorage.clear();
});

afterEach(cleanup);

describe("navigating to a session in a collapsed project", () => {
  function collapsedProject() {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify([]));
    mockConversations([
      conv("pinned_a", "Repro 2506"),
      conv("sibling_b", "Repro 2506"),
      conv("sibling_c", "Repro 2506"),
    ]);
  }
  const header = () => screen.getByRole("button", { name: "Repro 2506" });

  it("holds no row for a pinned member", () => {
    pinnedRef.current = [conv("pinned_a", "Repro 2506")];
    collapsedProject();

    renderAt("/c/pinned_a");

    // The Pinned section already shows it, so the folder lists nothing.
    expect(screen.getAllByRole("link", { name: /pinned_a/ })).toHaveLength(1);
    expect(screen.queryByRole("link", { name: /sibling_b/ })).not.toBeInTheDocument();
    expect(header()).toHaveAttribute("aria-expanded", "false");
    expect(expandedProjects()).toEqual([]);
  });

  it("keeps the folder collapsed and lists only the open session", () => {
    collapsedProject();

    renderAt("/c/sibling_b");

    expect(header()).toHaveAttribute("aria-expanded", "false");
    expect(screen.getAllByRole("link", { name: /sibling_b/ })).toHaveLength(1);
    expect(screen.queryByRole("link", { name: /sibling_c/ })).not.toBeInTheDocument();
    expect(expandedProjects()).toEqual([]);
  });

  it("holds the open session under the collapsed folder once it's unpinned", () => {
    pinnedRef.current = [conv("pinned_a", "Repro 2506")];
    collapsedProject();
    const { rerenderSidebar } = renderAt("/c/pinned_a");

    pinnedRef.current = [];
    act(() => rerenderSidebar());

    expect(header()).toHaveAttribute("aria-expanded", "false");
    expect(screen.getAllByRole("link", { name: /pinned_a/ })).toHaveLength(1);
    expect(screen.queryByRole("link", { name: /sibling_b/ })).not.toBeInTheDocument();
    expect(expandedProjects()).toEqual([]);
  });

  it("opens the folder only when the user clicks its header", () => {
    collapsedProject();
    renderAt("/c/sibling_b");

    fireEvent.click(header());

    expect(header()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: /sibling_c/ })).toBeInTheDocument();
    expect(expandedProjects()).toEqual(["Repro 2506"]);
  });
});

describe("collapsing the project that holds the open session", () => {
  // The user opened the folder earlier; navigating no longer opens it for them.
  function openFiledSession() {
    pinnedRef.current = [conv("pinned_x")];
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["Repro 2506"]));
    mockConversations([conv("pinned_x"), conv("sibling_b", "Repro 2506")]);
  }

  it("stays collapsed when a pinned session's row updates", () => {
    openFiledSession();
    const { rerenderSidebar } = renderAt("/c/sibling_b");
    const header = screen.getByRole("button", { name: "Repro 2506" });
    expect(header).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(expandedProjects()).toEqual([]);

    pinnedRef.current = [{ ...conv("pinned_x"), status: "working", updated_at: 1 }];
    act(() => rerenderSidebar());

    expect(screen.getByRole("button", { name: "Repro 2506" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(expandedProjects()).toEqual([]);
  });

  it("stays collapsed after the click when pins exclude shared sessions", () => {
    openFiledSession();
    renderAt("/c/sibling_b", { ...sidebarConfig, pinsIncludeShared: false });
    const header = screen.getByRole("button", { name: "Repro 2506" });
    expect(header).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(header);

    expect(screen.getByRole("button", { name: "Repro 2506" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(expandedProjects()).toEqual([]);
  });

  it("keeps the open session's row under the collapsed header until navigating away", () => {
    openFiledSession();
    renderAt("/c/sibling_b");

    fireEvent.click(screen.getByRole("button", { name: "Repro 2506" }));

    expect(screen.getByRole("button", { name: "Repro 2506" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getAllByRole("link", { name: /sibling_b/ })).toHaveLength(1);
    // The held row keeps its numbered hotkey, after the pinned row's.
    expect(screen.getByRole("link", { name: /sibling_b/ })).toHaveAttribute(
      "aria-keyshortcuts",
      sessionNumberAriaShortcut("2"),
    );
    fireEvent.click(screen.getByRole("link", { name: /pinned_x/ }));
    expect(screen.queryByRole("link", { name: /sibling_b/ })).not.toBeInTheDocument();
  });

  it("badges the collapsed header only for the rows it hides", () => {
    const awaiting = (id: string) =>
      ({ ...conv(id, "Repro 2506"), pending_elicitations_count: 1 }) as Conversation;
    openFiledSession();
    mockConversations([conv("pinned_x"), awaiting("sibling_b"), conv("sibling_c", "Repro 2506")]);
    const { rerenderSidebar } = renderAt("/c/sibling_b");
    fireEvent.click(screen.getByRole("button", { name: /^Repro 2506/ }));

    const header = () => screen.getByRole("button", { name: /^Repro 2506/ });
    expect(header()).toHaveAttribute("aria-expanded", "false");
    expect(within(header()).queryByText("Needs response")).toBeNull();
    expect(screen.getAllByText("Needs response")).toHaveLength(1);

    mockConversations([conv("pinned_x"), awaiting("sibling_b"), awaiting("sibling_c")]);
    act(() => rerenderSidebar());
    expect(within(header()).getByText("Needs response")).toBeInTheDocument();
    expect(screen.getAllByText("Needs response")).toHaveLength(2);
  });

  // The chat page suspends until released, holding the route commit as a loading chat does.
  function renderWithChatPage(chatPage: ReactNode) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const shell = (page: ReactNode) => (
      <>
        <Sidebar open onClose={vi.fn()} />
        <Suspense fallback={null}>{page}</Suspense>
      </>
    );
    return render(
      <QueryClientProvider client={qc}>
        <SidebarDataProvider config={sidebarConfig}>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/"]}>
              <Routes>
                <Route path="/" element={shell(null)} />
                <Route path="/c/:conversationId" element={shell(chatPage)} />
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );
  }

  it("stays collapsed when the route commits after the collapse", async () => {
    openFiledSession();
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["Repro 2506"]));
    let releaseRoute = () => {};
    let routeReleased = false;
    const routeGate = new Promise<void>((resolve) => {
      releaseRoute = () => {
        routeReleased = true;
        resolve();
      };
    });
    function SlowChatPage() {
      if (!routeReleased) throw routeGate;
      return <div data-testid="chat-page" />;
    }
    renderWithChatPage(<SlowChatPage />);

    fireEvent.click(screen.getByRole("link", { name: /sibling_b/ }));
    fireEvent.click(screen.getByRole("button", { name: "Repro 2506" }));
    expect(screen.queryByTestId("chat-page")).not.toBeInTheDocument();
    await act(async () => {
      releaseRoute();
      await routeGate;
    });

    expect(await screen.findByTestId("chat-page")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Repro 2506" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(expandedProjects()).toEqual([]);
  });

  it("stays collapsed when a session only its folder had loaded joins the list", () => {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["Repro 2506"]));
    mockConversations([conv("chat_c")]);
    const { rerenderSidebar } = renderAt("/");

    fireEvent.click(screen.getByRole("link", { name: /sibling_b/ }));
    fireEvent.click(screen.getByRole("button", { name: "Repro 2506" }));
    mockConversations([conv("chat_c"), conv("sibling_b", "Repro 2506")]);
    act(() => rerenderSidebar());

    expect(screen.getByRole("button", { name: "Repro 2506" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(expandedProjects()).toEqual([]);
    expect(screen.getAllByRole("link", { name: /sibling_b/ })).toHaveLength(1);
  });

  it("stays collapsed when an open child session resolves to its parent", async () => {
    // child_c's listed row stands in for the open-session row shown while its parent loads.
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["Repro 2506"]));
    mockConversations([conv("child_c", "Repro 2506"), conv("sibling_b", "Repro 2506")]);
    let releaseParent = () => {};
    const parentGate = new Promise<void>((resolve) => {
      releaseParent = resolve;
    });
    const snapshot = (id: string, parentSessionId: string | null) =>
      ({ id, labels: {}, items: [], parentSessionId }) as unknown as Session;
    getSessionSlimMock.mockImplementation(async (id: string) => {
      if (id === "child_c") return snapshot("child_c", "sibling_b");
      await parentGate;
      return snapshot(id, null);
    });
    renderAt("/c/child_c");
    const header = screen.getByRole("button", { name: "Repro 2506" });
    expect(header).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(header);
    await act(async () => {
      releaseParent();
      await parentGate;
    });

    // The collapsed folder now holds the resolved parent's row.
    expect(await screen.findByRole("link", { name: /sibling_b/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Repro 2506" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(expandedProjects()).toEqual([]);
  });
});
