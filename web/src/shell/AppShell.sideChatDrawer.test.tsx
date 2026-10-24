import { SidebarDataProvider } from "@/hooks/useSidebarData";
// The mobile side-chats drawer: who may open it, and when it must stay shut.
// The rail is hidden below `md`, so on a phone the drawer is the only surface
// that shows a side chat — and a fork that resolves after the user moved on
// must not surface over the conversation now on screen.

import type * as UseTerminalsModule from "@/hooks/useTerminals";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as UsePullRequestsModule from "@/hooks/usePullRequests";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { writeWorkspacePanelDefault } from "@/lib/workspacePanelPreferences";

vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useConversations: vi.fn(),
  useStopSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  // Keep the real module (inventoryTerminals etc.) — only the
  // network-backed hook is replaced.
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: vi.fn(() => ({ terminals: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: vi.fn(() => ({
    data: { available: true, root: null },
    isLoading: false,
  })),
  useWorkspaceChangedFiles: vi.fn(() => ({
    data: { data: [] },
    isSuccess: true,
    isLoading: false,
  })),
}));
vi.mock("@/hooks/usePullRequests", async (importOriginal) => ({
  // Keep the real module (types, the panel's sibling hooks) — only the
  // info hook AppShell reads is replaced, per-test below.
  ...(await importOriginal<typeof UsePullRequestsModule>()),
  usePullRequestInfo: vi.fn(() => ({ data: undefined, isLoading: true })),
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: vi.fn(() => ({ children: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: vi.fn(() => ({ session: null, isLoading: false, error: null })),
}));
vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: vi.fn(() => ({ data: undefined })),
  useCreateMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useUpdateMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useDeleteMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));
vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => true,
}));
// The pane itself is covered by its own suite; the stub just names its child.
vi.mock("@/components/chat/SideChatPane", () => ({
  SideChatPane: ({ childId }: { childId: string }) => (
    <div data-testid="side-chat-pane-stub">{childId}</div>
  ),
}));
vi.mock("@/lib/breakpoints", async (importOriginal) => ({
  ...(await importOriginal<typeof BreakpointsModule>()),
  isMobileViewport: vi.fn(() => false),
}));
vi.mock("./Sidebar", () => ({
  Sidebar: () => <div data-testid="sidebar" />,
}));
vi.mock("./PullRequestPanel", () => ({
  PullRequestPanel: () => <div data-testid="github-panel">Pull request details</div>,
}));
vi.mock("./FilesPanel", () => ({
  FilesPanel: () => <div data-testid="files-panel" />,
}));
vi.mock("./FileViewer", () => ({
  FileViewer: () => <div data-testid="file-viewer" />,
}));
vi.mock("./InlineTerminalsSection", () => ({
  InlineTerminalsSection: () => <div data-testid="inline-terminals-section" />,
}));
vi.mock("./FilesPanelDrawer", () => ({
  FilesPanelDrawer: () => <div data-testid="files-panel-drawer" />,
}));
vi.mock("./TerminalsPanel", () => ({
  TerminalsPanel: () => <div data-testid="terminals-panel" />,
}));

import { AppShell } from "./AppShell";
import type * as BreakpointsModule from "@/lib/breakpoints";
import { isMobileViewport } from "@/lib/breakpoints";
import { usePullRequestInfo } from "@/hooks/usePullRequests";
import { useConversations } from "@/hooks/useConversations";
import { useChatStore } from "@/store/chatStore";
import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

const usePullRequestInfoMock = vi.mocked(usePullRequestInfo);

afterEach(cleanup);

beforeEach(() => {
  // The rail persists per-session state (selected tab, width) in
  // localStorage; clear it so one test's writes can't leak into another.
  localStorage.clear();
  writeWorkspacePanelDefault("open");
  sessionStorage.clear();
  vi.mocked(isMobileViewport).mockReturnValue(false);
  usePullRequestInfoMock.mockReset();
  usePullRequestInfoMock.mockReturnValue({ data: undefined, isLoading: true } as ReturnType<
    typeof usePullRequestInfo
  >);
  vi.mocked(useConversations).mockReset();
  vi.mocked(useConversations).mockReturnValue({
    data: {
      pages: [
        {
          data: [
            {
              id: "conv_ws",
              object: "conversation" as const,
              title: null,
              created_at: 0,
              updated_at: 0,
              labels: {},
              permission_level: null,
              host_id: null,
              runner_id: null,
            },
          ],
          first_id: null,
          last_id: null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
  } as never);
});

function NavProbe() {
  return <Link to="/c/conv_other">Another session</Link>;
}

function renderShell(path = "/c/conv_ws") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route path="c/:conversationId" element={<NavProbe />} />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

describe("mobile side-chats drawer", () => {
  const initialStoreState = useChatStore.getState();

  beforeEach(() => {
    vi.mocked(isMobileViewport).mockReturnValue(true);
    useChatStore.setState({ ...initialStoreState, sessionHarness: "openai-agents" });
    // Two already-open tabs, so a drawer that opens has something to show.
    writeSessionWorkspaceState("conv_ws", {
      open: false,
      openSideChats: ["conv_side_a"],
      selectedSideChatId: "conv_side_a",
    });
  });
  afterEach(() => useChatStore.setState(initialStoreState));

  /** The desktop rail. Queried by DOM, not role: a collapsed rail sets
   *  `aria-hidden`, so it is absent from the accessibility tree. */
  const rail = () => document.querySelector('aside[aria-label="Workspace"]');

  /** Publish the one-shot "a side chat is ready" signal for `parentId`. */
  function signal(parentId: string, childId = "conv_side_a") {
    useChatStore.setState({ sideChatToOpen: { childId, parentId } });
  }

  it("opens for a side chat belonging to the conversation on screen", () => {
    renderShell();

    const drawer = screen.getByTestId("side-chats-panel-drawer");
    expect(drawer).toHaveAttribute("data-state", "closed");

    act(() => signal("conv_ws"));

    expect(drawer).toHaveAttribute("data-state", "open");
    expect(within(drawer).getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_a");
    // The same signal reveals the rail, which is what desktop shows it in.
    expect(rail()).toHaveAttribute("data-state", "open");
  });

  it("stays shut for a side chat belonging to another conversation", () => {
    // The user started a side chat in conv_other, then navigated here before
    // the fork resolved. Its signal must not surface over this conversation.
    renderShell();

    act(() => signal("conv_other", "conv_side_elsewhere"));

    expect(screen.getByTestId("side-chats-panel-drawer")).toHaveAttribute("data-state", "closed");
    // Nor is the rail revealed — the same gate covers the desktop surface, so a
    // foreign side chat can't pop open an empty rail over this conversation.
    expect(rail()).toHaveAttribute("data-state", "closed");
    // The signal is left for its own parent to consume, not dropped.
    expect(useChatStore.getState().sideChatToOpen).toEqual({
      childId: "conv_side_elsewhere",
      parentId: "conv_other",
    });
  });

  it("closes when navigating to another session", () => {
    renderShell();
    act(() => signal("conv_ws"));
    expect(screen.getByTestId("side-chats-panel-drawer")).toHaveAttribute("data-state", "open");

    fireEvent.click(screen.getByRole("link", { name: "Another session" }));

    expect(screen.getByTestId("side-chats-panel-drawer")).toHaveAttribute("data-state", "closed");
  });

  it("replaces an already-open mobile drawer rather than stacking on it", () => {
    // ?panel=agents opens the Agents drawer on a phone; a side chat arriving
    // afterwards must take over the screen, not render behind it.
    renderShell("/c/conv_ws?panel=agents");
    expect(screen.getByTestId("subagents-panel-drawer")).toHaveAttribute("data-state", "open");

    act(() => signal("conv_ws"));

    expect(screen.getByTestId("side-chats-panel-drawer")).toHaveAttribute("data-state", "open");
    expect(screen.getByTestId("subagents-panel-drawer")).toHaveAttribute("data-state", "closed");
  });
});
