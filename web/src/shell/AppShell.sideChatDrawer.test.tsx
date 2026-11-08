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
import type * as SessionsApiModule from "@/lib/sessionsApi";

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
  SideChatPane: ({
    childId,
    onStart,
  }: {
    childId: string;
    onStart?: (text: string) => Promise<void>;
  }) => (
    <div data-testid="side-chat-pane-stub">
      {childId}
      {onStart && (
        <button type="button" onClick={() => void onStart("why backoff?").catch(() => {})}>
          Send first message
        </button>
      )}
    </div>
  ),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  createSideChat: vi.fn(),
}));
vi.mock("./Sidebar", () => ({
  Sidebar: () => <div data-testid="sidebar" />,
  isMobileViewport: vi.fn(() => false),
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
import { isMobileViewport } from "./Sidebar";
import { usePullRequestInfo } from "@/hooks/usePullRequests";
import { useConversations } from "@/hooks/useConversations";
import { useSession } from "@/hooks/useSession";
import { createSideChat } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { subagentTabId } from "@/lib/subagentTabs";

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
  return (
    <>
      <Link to="/c/conv_other">Another session</Link>
      <Link to="/c/conv_ws">Back to session</Link>
    </>
  );
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

const openActionsMenu = () =>
  fireEvent.pointerDown(screen.getByRole("button", { name: "Session actions" }), {
    button: 0,
  });

describe("mobile side-chats drawer", () => {
  const initialStoreState = useChatStore.getState();

  beforeEach(() => {
    vi.mocked(createSideChat).mockReset();
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

  /** Send the first message from a New side chat tab in the drawer. */
  async function sendFromNewSideChatTab() {
    openActionsMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Side chats" }));
    const drawer = screen.getByTestId("side-chats-panel-drawer");
    fireEvent.click(within(drawer).getByRole("button", { name: "New side chat" }));
    await act(async () =>
      fireEvent.click(within(drawer).getByRole("button", { name: "Send first message" })),
    );
    return drawer;
  }

  function holdSideChatStart() {
    let finishStart!: (result: { childSessionId: string }) => void;
    vi.mocked(createSideChat).mockReturnValue(
      new Promise((resolve) => {
        finishStart = resolve;
      }),
    );
    return (childSessionId: string) => act(async () => finishStart({ childSessionId }));
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

  it("reveals a side chat that finished while the user was away, once they return", () => {
    renderShell();
    fireEvent.click(screen.getByRole("link", { name: "Another session" }));
    act(() => signal("conv_ws"));

    fireEvent.click(screen.getByRole("link", { name: "Back to session" }));

    const drawer = screen.getByTestId("side-chats-panel-drawer");
    expect(drawer).toHaveAttribute("data-state", "open");
    expect(within(drawer).getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_a");
    expect(rail()).toHaveAttribute("data-state", "open");
  });

  it("reveals a new side chat that finished starting while the user was away", async () => {
    const finishStart = holdSideChatStart();
    renderShell();
    const drawer = await sendFromNewSideChatTab();

    fireEvent.click(screen.getByRole("link", { name: "Another session" }));
    await finishStart("conv_side_new");
    fireEvent.click(screen.getByRole("link", { name: "Back to session" }));

    expect(drawer).toHaveAttribute("data-state", "open");
    expect(within(drawer).getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_new");
  });

  it("reveals a new side chat that finishes starting after the user leaves and returns", async () => {
    const finishStart = holdSideChatStart();
    renderShell();
    const drawer = await sendFromNewSideChatTab();

    fireEvent.click(screen.getByRole("link", { name: "Another session" }));
    fireEvent.click(screen.getByRole("link", { name: "Back to session" }));
    expect(drawer).toHaveAttribute("data-state", "closed");
    await finishStart("conv_side_new");

    expect(drawer).toHaveAttribute("data-state", "open");
    expect(within(drawer).getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_new");
    expect(rail()).toHaveAttribute("data-state", "open");
  });

  it("reveals a main-composer side chat while another conversation's native fork is pending", async () => {
    useChatStore.setState({
      sessionHarness: "codex-native",
      boundAgentId: "agent_x",
      send: vi.fn(() => Promise.resolve()),
    });
    renderShell();
    const drawer = await sendFromNewSideChatTab();

    fireEvent.click(screen.getByRole("link", { name: "Another session" }));
    // That conversation's own /side fork arrives while this pending fork waits.
    act(() => signal("conv_other", "conv_side_other"));

    expect(drawer).toHaveAttribute("data-state", "open");
    expect(rail()).toHaveAttribute("data-state", "open");
  });

  it("stays closed when dismissed while a new side chat is starting", async () => {
    const finishStart = holdSideChatStart();
    renderShell();
    const drawer = await sendFromNewSideChatTab();
    expect(createSideChat).toHaveBeenCalledTimes(1);
    expect(createSideChat).toHaveBeenCalledWith("conv_ws", undefined);

    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    expect(drawer).toHaveAttribute("data-state", "closed");
    await finishStart("conv_side_new");

    expect(drawer).toHaveAttribute("data-state", "closed");
    expect(rail()).toHaveAttribute("data-state", "closed");
    openActionsMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Side chats" }));
    expect(within(drawer).getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_new");
  });

  it("stays closed when dismissed while a native side-chat fork is starting", async () => {
    const send = vi.fn(() => Promise.resolve());
    useChatStore.setState({ sessionHarness: "codex-native", boundAgentId: "agent_x", send });
    renderShell();
    const drawer = await sendFromNewSideChatTab();
    expect(send).toHaveBeenCalledWith("/side why backoff?", "agent_x", undefined, {
      pinnedConversationId: "conv_ws",
      onError: expect.any(Function),
    });

    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    // The fork's session_created arrives after the user dismissed the drawer.
    act(() => signal("conv_ws", "conv_side_native"));

    expect(drawer).toHaveAttribute("data-state", "closed");
    expect(rail()).toHaveAttribute("data-state", "closed");
    openActionsMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Side chats" }));
    expect(drawer).toHaveAttribute("data-state", "open");
    expect(within(drawer).getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_native");
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

describe("phone menu for a viewer who can't start side chats", () => {
  const initialStoreState = useChatStore.getState();
  beforeEach(() => {
    useChatStore.setState({ ...initialStoreState, sessionHarness: "openai-agents" });
    // An editor has a side-chat tab open but cannot create another.
    const session: Session = {
      id: "conv_ws",
      agentId: "agent",
      agentName: null,
      runnerId: null,
      status: "idle",
      createdAt: 0,
      title: null,
      labels: {},
      items: [],
      pendingElicitations: [],
      permissionLevel: 2,
      parentSessionId: null,
      subAgentName: null,
      kind: "default",
      sideChatAllowed: false,
    };
    vi.mocked(useSession).mockReturnValue({ session, isLoading: false, error: null });
  });
  afterEach(() => {
    useChatStore.setState(initialStoreState);
    vi.mocked(useSession).mockReturnValue({ session: null, isLoading: false, error: null });
  });

  it("keeps a restored side chat reachable without offering a new one", () => {
    writeSessionWorkspaceState("conv_ws", {
      openSideChats: ["conv_side_a"],
      selectedSideChatId: "conv_side_a",
    });
    renderShell();

    openActionsMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Side chats" }));

    const drawer = screen.getByTestId("side-chats-panel-drawer");
    expect(drawer).toHaveAttribute("data-state", "open");
    expect(within(drawer).getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_a");
    expect(within(drawer).queryByRole("button", { name: "New side chat" })).toBeNull();
  });
});

describe("phone menu with a harness that can't start side chats", () => {
  const initialStoreState = useChatStore.getState();
  beforeEach(() => useChatStore.setState({ ...initialStoreState, sessionHarness: null }));
  afterEach(() => useChatStore.setState(initialStoreState));

  it("offers Side chats for sub-agent tabs restored from storage", () => {
    writeSessionWorkspaceState("conv_ws", {
      openSubagentTabs: [subagentTabId("conv_child")],
      selectedSideChatId: subagentTabId("conv_child"),
    });
    renderShell();

    openActionsMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Side chats" }));

    const drawer = screen.getByTestId("side-chats-panel-drawer");
    expect(drawer).toHaveAttribute("data-state", "open");
    expect(within(drawer).getByTestId("subagent-tab-pane")).toBeInTheDocument();
  });

  it("offers Side chats once a sub-agent tab opens, without a reload", () => {
    renderShell();
    openActionsMenu();
    expect(screen.queryByRole("menuitem", { name: "Side chats" })).toBeNull();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

    // Opened at desktop width, so the signal reveals only the rail.
    act(() => {
      useChatStore.setState({
        sideChatToOpen: { childId: subagentTabId("conv_child"), parentId: "conv_ws" },
      });
    });
    openActionsMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Side chats" }));

    const drawer = screen.getByTestId("side-chats-panel-drawer");
    expect(drawer).toHaveAttribute("data-state", "open");
    expect(within(drawer).getByTestId("subagent-tab-pane")).toBeInTheDocument();
  });
});
