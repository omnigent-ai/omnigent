import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
}));

vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  // Keep the real module (inventoryTerminals, EMBEDDED_REPL_TERMINAL_ID)
  // — the REPL rail-inventory tests exercise the real filter; only the
  // network-backed hook is replaced.
  ...(await importOriginal<typeof import("@/hooks/useTerminals")>()),
  useTerminals: vi.fn(() => ({ terminals: [], isLoading: false, error: null })),
}));

vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: vi.fn(() => ({ data: undefined, isLoading: true })),
  useWorkspaceChangedFiles: vi.fn(() => ({ data: undefined, isLoading: true })),
}));

vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  // Keep the real module (childSessionsQueryKey, MAX_TREE_DEPTH,
  // cachedTreeContains) — only the hook is replaced.
  ...(await importOriginal<typeof import("@/hooks/useChildSessions")>()),
  useChildSessions: vi.fn(() => ({ children: [], isLoading: false, error: null })),
}));

vi.mock("@/hooks/useSession", async (importOriginal) => ({
  // useRootSessionId stays real — with useSession mocked to a null /
  // top-level session it resolves synchronously without fetching.
  ...(await importOriginal<typeof import("@/hooks/useSession")>()),
  useSession: vi.fn(() => ({ session: null, isLoading: false, error: null })),
}));

// The header's AgentInfoButton (desktop) and the mobile menu's "Agent info"
// entry gate on the bound agent's tools/policies. Default: no agent data, so
// both stay hidden — tests that exercise the agent-info path set a return.
vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: vi.fn(() => ({ data: undefined })),
}));

vi.mock("./Sidebar", () => ({
  Sidebar: () => <div data-testid="sidebar" />,
}));
vi.mock("./FilesPanel", () => ({
  // Scope-only stand-in matching the real FilesPanel after the open-file tabs
  // and the FileViewer moved up to WorkspacePanel. It exposes file-select and
  // scope-toggle buttons only; the inline viewer is now rendered directly by
  // WorkspacePanel via the FileViewer mock below (data-testid="file-viewer-
  // inline"), so the desktop file-viewer assertions still hold.
  FilesPanel: ({
    onFileSelect,
    flatView,
    onFlatViewChange,
  }: {
    onFileSelect: (path: string) => void;
    flatView: boolean;
    onFlatViewChange: (v: boolean) => void;
  }) => (
    <div data-testid="files-panel" data-flat-view={String(flatView)}>
      <button
        type="button"
        aria-label="files: select README.md"
        onClick={() => onFileSelect("README.md")}
      >
        select
      </button>
      <button
        type="button"
        aria-label="files: select AGENTS.md"
        onClick={() => onFileSelect("AGENTS.md")}
      >
        select-agents
      </button>
      <button
        type="button"
        aria-label="files: switch to explore"
        onClick={() => onFlatViewChange(false)}
      >
        explore
      </button>
      <button
        type="button"
        aria-label="files: switch to changed"
        onClick={() => onFlatViewChange(true)}
      >
        changed
      </button>
    </div>
  ),
}));
vi.mock("./FileViewer", () => ({
  // frameless=true → the inline desktop viewer (always open when rendered).
  // frameless=false/absent → the mobile push-panel (open prop controls visibility).
  // Use different testids so tests can target the one they care about.
  FileViewer: ({
    open,
    path,
    onClose,
    frameless,
  }: {
    open: boolean;
    path: string;
    onClose: () => void;
    frameless?: boolean;
  }) => (
    <div
      data-testid={frameless ? "file-viewer-inline" : "file-viewer"}
      data-state={open ? "open" : "closed"}
      data-path={path}
    >
      <button type="button" aria-label="file-viewer: close" onClick={onClose}>
        close
      </button>
    </div>
  ),
}));
vi.mock("./InlineTerminalsSection", () => ({
  // Minimal stand-in exposing onExpand so tests can trigger the inline terminal expand path.
  InlineTerminalsSection: ({ onExpand }: { onExpand: (key: string) => void }) => (
    <div data-testid="inline-terminals-section">
      <button
        type="button"
        aria-label="rail: open terminal"
        onClick={() => onExpand("terminal:terminal_main")}
      >
        rail-terminal
      </button>
    </div>
  ),
}));
vi.mock("./SubagentsPanel", () => ({
  SubagentsPanel: ({ conversationId }: { conversationId: string }) => (
    <div data-testid="subagents-panel" data-conversation-id={conversationId} />
  ),
}));
vi.mock("./TodoPanel", () => ({
  TodoPanel: () => <div data-testid="todo-panel" />,
}));
vi.mock("./FilesPanelDrawer", () => ({
  FilesPanelDrawer: ({ open, flatView }: { open: boolean; flatView: boolean }) => (
    <div
      data-testid="files-panel-drawer"
      data-state={open ? "open" : "closed"}
      data-flat-view={String(flatView)}
    />
  ),
}));
vi.mock("./TerminalsPanel", () => ({
  TerminalsPanel: ({
    open,
    initialTerminalKey,
    fluid,
  }: {
    open: boolean;
    initialTerminalKey: string | null;
    fluid?: boolean;
  }) => (
    <div
      data-testid="terminals-panel"
      data-state={open ? "open" : "closed"}
      data-initial-key={initialTerminalKey ?? ""}
      data-fluid={fluid ? "true" : "false"}
    />
  ),
}));

import { useConversations } from "@/hooks/useConversations";
import { useTerminals } from "@/hooks/useTerminals";
const useConvMock = vi.mocked(useConversations);
const useTerminalsMock = vi.mocked(useTerminals);

import {
  useWorkspaceEnvironment,
  useWorkspaceChangedFiles,
} from "@/hooks/useWorkspaceChangedFiles";
const useEnvironmentMock = vi.mocked(useWorkspaceEnvironment);
const useChangedFilesMock = vi.mocked(useWorkspaceChangedFiles);

import { useChildSessions } from "@/hooks/useChildSessions";
const useChildSessionsMock = vi.mocked(useChildSessions);

import { useSession } from "@/hooks/useSession";
const useSessionMock = vi.mocked(useSession);

import { useSessionAgent } from "@/hooks/useAgents";
import type { Agent } from "@/hooks/useAgents";
const useSessionAgentMock = vi.mocked(useSessionAgent);

import { AppShell } from "./AppShell";
import { useTerminalFirst } from "./TerminalFirstContext";
import { useForkDialog } from "./ForkDialogContext";
import { useChatStore } from "@/store/chatStore";

/**
 * Test-only consumer of the TerminalFirstContext provided by AppShell.
 * The production view toggle now lives inside ChatPage's
 * ConnectionIndicator; these tests are scoped to the shell's state
 * machine, so we use a probe component with the exact same
 * `aria-label`s as the production pill ("Chat" / "Terminal" — see
 * ConnectedTerminalFirstPill in ChatPage.tsx) to drive `setView`. If
 * the production labels ever change, these tests fail loudly instead
 * of drifting silently.
 */
function TerminalFirstViewProbe() {
  const ctx = useTerminalFirst();
  if (!ctx) return <div data-testid="view-probe" data-no-context="true" />;
  return (
    <div
      data-testid="view-probe"
      data-is-terminal-first={ctx.isTerminalFirst ? "true" : "false"}
      data-is-claude-native={ctx.isClaudeNative ? "true" : "false"}
      data-view={ctx.view}
      data-terminals-available={ctx.terminalsAvailable ? "true" : "false"}
      data-terminal-starting-up={ctx.terminalStartingUp ? "true" : "false"}
    >
      <button
        type="button"
        aria-label="Chat"
        aria-pressed={ctx.view === "chat"}
        onClick={() => ctx.setView("chat")}
      >
        chat
      </button>
      <button
        type="button"
        aria-label="Terminal"
        aria-pressed={ctx.view === "terminal"}
        onClick={() => ctx.setView("terminal")}
      >
        terminal
      </button>
    </div>
  );
}

/**
 * Probe for ForkDialogContext — the per-message "Fork from here" entry
 * point lives in ChatPage (not mounted here), so these tests consume the
 * context the same way the real action does: read `canFork` for gating
 * and call `openForkDialog` with a truncation point.
 */
function ForkDialogProbe() {
  const fork = useForkDialog();
  if (!fork) return <div data-testid="fork-probe" data-no-context="true" />;
  return (
    <div data-testid="fork-probe" data-can-fork={fork.canFork ? "true" : "false"}>
      <button
        type="button"
        data-testid="fork-probe-open"
        onClick={() => fork.openForkDialog({ upToResponseId: "resp_probe" })}
      >
        fork-from-here
      </button>
    </div>
  );
}

/**
 * Renders the current search params into a testid element so URL-sync
 * tests can assert on param changes without reaching into router internals.
 */
function LocationDisplay() {
  const [params] = useSearchParams();
  return <div data-testid="url-params">{params.toString()}</div>;
}

/**
 * Renders the current pathname (the `/c/:conversationId` segment) so a test
 * can detect an unwanted conversation switch — a redirect shows up as a
 * pathname change, which `LocationDisplay` (search params only) can't catch.
 */
function PathDisplay() {
  const { pathname } = useLocation();
  return <div data-testid="url-pathname">{pathname}</div>;
}

/**
 * Button that navigates to another conversation within the same mount, so a
 * test can exercise the conversation-switch effect (AppShell stays mounted,
 * only the :conversationId route param changes) without a full remount.
 */
function SessionNavButton({ to }: { to: string }) {
  const navigate = useNavigate();
  return (
    <button type="button" data-testid="nav-session" onClick={() => navigate(to)}>
      switch session
    </button>
  );
}

function renderShell(path: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route element={<AppShell />}>
              <Route
                index
                element={
                  <>
                    <div>home</div>
                    <LocationDisplay />
                  </>
                }
              />
              <Route
                path="c/:conversationId"
                element={
                  <>
                    <TerminalFirstViewProbe />
                    <ForkDialogProbe />
                    <LocationDisplay />
                  </>
                }
              />
            </Route>
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function mockConversations(
  convs: Array<{
    id: string;
    permission_level: number | null;
    labels?: Record<string, string>;
    host_id?: string | null;
    runner_id?: string | null;
  }>,
) {
  useConvMock.mockReturnValue({
    data: {
      pages: [
        {
          data: convs.map((c) => ({
            id: c.id,
            object: "conversation" as const,
            title: null,
            created_at: 0,
            updated_at: 0,
            labels: c.labels ?? {},
            permission_level: c.permission_level,
            host_id: c.host_id ?? null,
            runner_id: c.runner_id ?? null,
          })),
          first_id: null,
          last_id: null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
  } as ReturnType<typeof useConversations>);
}

beforeEach(() => {
  useConvMock.mockReset();
  useTerminalsMock.mockReset();
  useTerminalsMock.mockReturnValue({
    terminals: [],
    isLoading: false,
    error: null,
  });
  useChildSessionsMock.mockReset();
  useChildSessionsMock.mockReturnValue({
    children: [],
    isLoading: false,
    error: null,
  });
  useSessionMock.mockReset();
  useSessionMock.mockReturnValue({ session: null, isLoading: false, error: null });
  // Default: no agent tools/policies → agent-info affordances hidden.
  useSessionAgentMock.mockReset();
  useSessionAgentMock.mockReturnValue({ data: undefined } as ReturnType<typeof useSessionAgent>);
  // Default: loading state (data undefined) → showFilesPanel stays true,
  // no flash for agents that do have os_env.
  useEnvironmentMock.mockReset();
  useEnvironmentMock.mockReturnValue({ data: undefined, isLoading: true } as ReturnType<
    typeof useWorkspaceEnvironment
  >);
  useChangedFilesMock.mockReset();
  useChangedFilesMock.mockReturnValue({ data: undefined, isLoading: true } as unknown as ReturnType<
    typeof useWorkspaceChangedFiles
  >);
  // The Chat/TUI toggle persists its position to sessionStorage so leaving
  // and re-entering a conversation restores the last view. Clear
  // it between tests so persistence from one test can't leak into another.
  sessionStorage.clear();
  // The Files-panel scope (Changed | All) persists to localStorage so the
  // choice carries across sessions. Clear it so a stored preference from one
  // test can't change another test's default scope.
  localStorage.clear();
  // The Tasks tab/drawer gates on chatStore.todos; reset so a populated
  // todo list from one test doesn't leak into the next.
  // Reset terminal-first startup signals so one test's terminalPending /
  // failed status can't leak into another's terminalStartingUp.
  useChatStore.setState({ todos: [], terminalPending: false, sessionStatus: "idle" });
});

afterEach(cleanup);

describe("AppShell header", () => {
  it("renders the sidebar toggle on all pages", () => {
    mockConversations([]);
    renderShell("/");
    expect(screen.getByRole("button", { name: /sidebar/i })).toBeInTheDocument();
  });

  it("defaults to chat view on a native Claude session", () => {
    // The shell used to auto-open the terminals panel for terminal-first
    // sessions. The new behavior is: default to Chat, let the user opt
    // into Terminal via the connection-pill segmented control. Note that
    // the TerminalsPanel drawer is never mounted for terminal-first
    // sessions — the terminal renders inline inside main via
    // MainTerminalView, so we assert the panel is absent (rather than
    // closed) and the probe's view is "chat".
    mockConversations([
      {
        id: "conv_terminal",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
    ]);
    useTerminalsMock.mockReturnValue({
      terminals: [
        {
          id: "terminal_claude_main",
          name: "claude",
          session: "main",
          running: true,
        },
      ],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_terminal");

    expect(screen.queryByTestId("terminals-panel")).toBeNull();
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "chat");
  });

  it("shows the terminal-startup spinner while a terminal-first session is coming up", () => {
    // Baseline for the suppression test below: terminalPending (PTY being
    // created) with no terminals available drives terminalStartingUp true.
    mockConversations([
      { id: "conv_terminal", permission_level: null, labels: { "omnigent.ui": "terminal" } },
    ]);
    useChatStore.setState({ terminalPending: true, sessionStatus: "running" });

    renderShell("/c/conv_terminal");

    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-terminal-starting-up", "true");
  });

  it("suppresses the terminal-startup spinner once the session has failed", () => {
    // A runner that crashed before connecting sits in the startup window
    // (terminalPending/starting) but can never come up. The failed status
    // must drop the spinner so the error banner stands alone — otherwise
    // the user sees a spinner that spins forever beside the error.
    mockConversations([
      { id: "conv_terminal", permission_level: null, labels: { "omnigent.ui": "terminal" } },
    ]);
    useChatStore.setState({ terminalPending: true, sessionStatus: "failed" });

    renderShell("/c/conv_terminal");

    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-terminal-starting-up", "false");
  });
});

describe("TerminalFirstContext", () => {
  it("flags terminal-first sessions and reports terminal availability", () => {
    // The context drives both the inline Chat/Terminal pill in
    // ChatPage's ConnectionIndicator and the visibility of the right-
    // rail terminals card. We assert via a test probe to keep these
    // shell-level tests independent of the consumer's rendering.
    mockConversations([
      {
        id: "conv_native",
        permission_level: null,
        labels: {
          "omnigent.ui": "terminal",
          "omnigent.wrapper": "claude-code-native-ui",
        },
      },
      {
        id: "conv_regular",
        permission_level: null,
        labels: {},
      },
    ]);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "claude", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_native");
    const probe = screen.getByTestId("view-probe");
    expect(probe).toHaveAttribute("data-is-terminal-first", "true");
    expect(probe).toHaveAttribute("data-is-claude-native", "true");
    expect(probe).toHaveAttribute("data-terminals-available", "true");
    expect(probe).toHaveAttribute("data-view", "chat");

    cleanup();
    renderShell("/c/conv_regular");
    const regularProbe = screen.getByTestId("view-probe");
    expect(regularProbe).toHaveAttribute("data-is-terminal-first", "false");
    expect(regularProbe).toHaveAttribute("data-is-claude-native", "false");
  });

  it("flags a child (sub-agent) session terminal-first from the snapshot when the sidebar omits it", () => {
    // The sidebar conversations list omits sub-agent rows, so for a
    // user-added claude-native agent ``activeConv`` is null and the
    // terminal-first flags must come from the per-session snapshot
    // (useSession). Regression guard for the reported "added claude-native
    // agent has no Chat|Terminal pill" bug.
    mockConversations([]); // sidebar omits the child row
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_added_child",
        agentId: "ag_x",
        agentName: null,
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: null,
        labels: {
          "omnigent.ui": "terminal",
          "omnigent.wrapper": "claude-code-native-ui",
        },
        items: [],
        pendingElicitations: [],
        permissionLevel: 4,
        parentSessionId: "conv_parent",
        subAgentName: null,
      },
      isLoading: false,
      error: null,
    });
    useTerminalsMock.mockReturnValue({
      terminals: [
        { id: "terminal_child", name: "claude", session: "conv_added_child", running: true },
      ],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_added_child");
    const probe = screen.getByTestId("view-probe");
    expect(probe).toHaveAttribute("data-is-terminal-first", "true");
    expect(probe).toHaveAttribute("data-is-claude-native", "true");
  });

  it("flipping to Terminal in a terminal-first session keeps the rail visible and never mounts the drawer", () => {
    // Terminal-first sessions render the terminal inline in main (via
    // MainTerminalView in ChatPage). The TerminalsPanel drawer is never
    // mounted for these sessions, and the right rail (FilesPanel)
    // stays visible alongside the inline terminal.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      {
        id: "conv_native",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
    ]);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "claude", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_native");

    // Default: chat view, drawer not mounted, the rail (Files panel) shows.
    // With no child agents the Agents tab is hidden, so the rail lands on its
    // default Files tab, which mounts FilesPanel.
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "chat");
    expect(screen.queryByTestId("terminals-panel")).toBeNull();
    expect(screen.getByTestId("files-panel")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));

    // After flip: view is "terminal", drawer still NOT mounted (inline
    // render lives in ChatPage), and the rail's files panel remains.
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "terminal");
    expect(screen.queryByTestId("terminals-panel")).toBeNull();
    expect(screen.getByTestId("files-panel")).toBeInTheDocument();

    // Toggling back to Chat returns the probe to "chat".
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "chat");
  });

  it("flips to an empty Terminal view when a terminal-first session has no terminal resource", () => {
    // The terminal-first pill is still a useful navigation affordance when a
    // stopped/killed session has no resource rows. `setView("terminal")`
    // must persist an open-but-empty terminal view instead of refusing to
    // switch because there is no first terminal key.
    mockConversations([
      {
        id: "conv_native_empty",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
    ]);
    useTerminalsMock.mockReturnValue({
      terminals: [],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_native_empty");

    const probe = screen.getByTestId("view-probe");
    expect(probe).toHaveAttribute("data-terminals-available", "false");
    expect(probe).toHaveAttribute("data-view", "chat");

    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));

    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "terminal");
    expect(screen.queryByTestId("terminals-panel")).toBeNull();
  });

  it("restores the terminal view when re-entering a native session within the same tab", () => {
    // Bug: switching to another chat and back used to drop the user
    // out of terminal view because the conversation-switch effect reset
    // panelInitialKey to null. The toggle position now persists per
    // conversation in sessionStorage, so re-entering restores it.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      {
        id: "conv_native",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
      { id: "conv_other", permission_level: null, labels: {} },
    ]);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "claude", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    const { unmount } = renderShell("/c/conv_native");

    // Opt into terminal view.
    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "terminal");

    // Leave to a different (non-terminal-first) conversation and come back.
    unmount();
    renderShell("/c/conv_other");
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "chat");
    cleanup();
    renderShell("/c/conv_native");

    // The native session should land back on terminal view, not chat.
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "terminal");
    expect(screen.getByRole("button", { name: "Terminal" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("does not restore terminal view in a fresh tab (sessionStorage scope)", () => {
    // First-time visitors must still land in chat view — the persistence
    // is sessionStorage, so a new tab starts with no stored preference.
    // This is the deliberate default.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      {
        id: "conv_native",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
    ]);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "claude", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_native");
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "chat");
    expect(screen.getByRole("button", { name: "Chat" })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("Right-rail terminals card", () => {
  it("is hidden in terminal-first sessions (claude-native)", () => {
    // The terminals card is removed in claude-native sessions — the
    // inline Chat/Terminal pill in ConnectionIndicator is the single
    // entry point into the terminal view, which renders inline in main
    // (not as a drawer).
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      {
        id: "conv_native",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
    ]);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "claude", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_native");
    expect(screen.queryByTestId("inline-terminals-section")).toBeNull();
    // The TerminalsPanel drawer is never mounted for terminal-first
    // sessions — terminal rendering lives inline inside main.
    expect(screen.queryByTestId("terminals-panel")).toBeNull();

    // The context-backed toggle still flips view state; main stays
    // mounted (no md:hidden) so it can host the inline terminal.
    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "terminal");
    expect(screen.queryByTestId("terminals-panel")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "chat");
  });

  it("renders the Terminals tab in a regular session once a terminal is attached, and the inline section after selecting it", () => {
    // With the tabbed rail, the inline section only mounts once the user
    // switches from Files (default) to Terminals. The tab button is present
    // in a non-terminal-first session as long as a terminal is attached.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "main", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_abc");

    // Default tab is Files — inline section is unmounted.
    expect(screen.queryByTestId("inline-terminals-section")).toBeNull();
    // Tab button is present (regex tolerates the inline count badge "1").
    const terminalsTab = screen.getByRole("tab", { name: /Shells/i });
    expect(terminalsTab).toBeInTheDocument();

    // Radix Tabs activates on mousedown, not click.
    fireEvent.mouseDown(terminalsTab);
    expect(screen.getByTestId("inline-terminals-section")).toBeInTheDocument();
  });

  it("hides the Terminals tab in a regular session with no terminal attached", () => {
    // Terminals are agent-created (the rail has no "new terminal" affordance),
    // so an empty Terminals tab is a dead end ("No terminals running."). It
    // must stay hidden until a terminal attaches — matching the mobile
    // session-menu's rule. This also avoids the snapshot-load flash:
    // ``hideTerminalsTab`` is label-derived and starts false, so without the
    // attach gate the tab would briefly appear then vanish once the snapshot
    // reveals a terminal-first / claude-native-subagent session.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    // Default beforeEach mock already returns no terminals; assert explicitly
    // for clarity that this is the no-terminal case.
    useTerminalsMock.mockReturnValue({ terminals: [], isLoading: false, error: null });

    renderShell("/c/conv_abc");

    expect(screen.queryByRole("tab", { name: /Shells/i })).toBeNull();
    // The rail still works — Files is the default and remains selected.
    expect(screen.getByRole("tab", { name: /Files/i })).toHaveAttribute("aria-selected", "true");
  });

  it("reveals the Terminals tab when a terminal attaches after mount", () => {
    // The tab is additive: it pops in (no flash-out) the moment a terminal
    // lands over SSE, which the seed/snapshot streams into the cache.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    useTerminalsMock.mockReturnValue({ terminals: [], isLoading: false, error: null });

    // Stable QueryClient + fresh element per render so the rerender reads the
    // updated mock (React bails on an identical element reference).
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const makeTree = () => (
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_abc"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route
                  path="c/:conversationId"
                  element={
                    <>
                      <TerminalFirstViewProbe />
                      <LocationDisplay />
                    </>
                  }
                />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(makeTree());

    // No terminal yet → tab hidden.
    expect(screen.queryByRole("tab", { name: /Shells/i })).toBeNull();

    // A terminal lands.
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "main", session: "main", running: true }],
      isLoading: false,
      error: null,
    });
    rerender(makeTree());

    // Tab now present (additive — the user wasn't yanked off any tab).
    expect(screen.getByRole("tab", { name: /Shells/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Files/i })).toHaveAttribute("aria-selected", "true");
  });

  it("opening a file in a terminal-first session keeps the view in Terminal", () => {
    // Regression: openFileViewer used to call setPanelInitialKey(null)
    // unconditionally to "close the terminal panel" — but in terminal-
    // first sessions there is no terminal drawer, and panelInitialKey
    // doubles as the view flag. Resetting it silently flipped the
    // connection pill from Terminal back to Chat when the user clicked
    // a file in the rail.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      {
        id: "conv_native",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
    ]);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "claude", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_native");

    // Opt into Terminal view.
    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "terminal");

    // Surface the rail's Files panel (Agents is the default tab now).
    fireEvent.mouseDown(screen.getByRole("tab", { name: /^Files$/i }));
    // Click a file — the file-viewer mock fires onFileSelect via this button.
    fireEvent.click(screen.getByRole("button", { name: /files: select README\.md/i }));

    // View must still be Terminal.
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-view", "terminal");
  });
});

describe("Chat-mode terminal panel layout", () => {
  it("hides chat and makes the panel fluid when a terminal is opened from the rail", () => {
    // Rail click → chat hidden, panel fluid (no split, no resize).
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "main", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_abc");

    // Baseline: closed, push-panel sizing, chat visible. The md:hidden gate
    // lives on the chat+workspace group (main's parent), not main itself.
    const chatGroup = () => screen.getByRole("main").parentElement as HTMLElement;
    expect(screen.getByTestId("terminals-panel")).toHaveAttribute("data-state", "closed");
    expect(screen.getByTestId("terminals-panel")).toHaveAttribute("data-fluid", "false");
    expect(chatGroup().className.split(" ")).not.toContain("md:hidden");

    // Switch the rail to the Terminals tab so the inline section mounts.
    // Radix Tabs activates on mousedown, not click.
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Shells/i }));
    fireEvent.click(screen.getByRole("button", { name: /rail: open terminal/i }));

    // After click: open, fluid, chat hidden.
    expect(screen.getByTestId("terminals-panel")).toHaveAttribute("data-state", "open");
    expect(screen.getByTestId("terminals-panel")).toHaveAttribute("data-fluid", "true");
    expect(chatGroup().className.split(" ")).toContain("md:hidden");
  });
});

describe("Subagents tab", () => {
  // A single child agent — enough to make the root "multi-agent" so the
  // Agents tab is shown.
  const oneChild = {
    children: [
      {
        id: "conv_child_a",
        title: "researcher:auth",
        tool: "researcher",
        session_name: "auth",
        current_task_status: "completed" as const,
        busy: false,
        last_message_preview: null,
        pending_elicitations_count: 0,
      },
    ],
    isLoading: false,
    error: null,
  };

  it("shows the Agents tab with a count of 1 when there are no child agents", () => {
    // The Agents tab is unconditional — the panel always lists at
    // least the main agent — and its badge counts the whole tree, so a
    // lone agent reads "1". A missing tab regresses the
    // always-visible rule; "0" means the main agent was dropped from
    // the count. Files stays the default tab.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc");

    const tab = screen.getByRole("tab", { name: /Agents\s*1/i });
    expect(within(tab).getByText("1")).toBeInTheDocument();
    // Files is the default tab, whose content slot mounts FilesPanel.
    expect(screen.getByRole("tab", { name: /Files/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("files-panel")).toBeInTheDocument();
  });

  it("shows the Agents tab and mounts its panel once there's more than one agent", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    useChildSessionsMock.mockReturnValue(oneChild);

    renderShell("/c/conv_abc");

    // Agents is no longer the default tab — click to open it.
    const subagentsTab = screen.getByRole("tab", { name: /Agents/i });
    expect(subagentsTab).toBeInTheDocument();
    fireEvent.mouseDown(subagentsTab);
    expect(screen.getByTestId("subagents-panel")).toHaveAttribute(
      "data-conversation-id",
      "conv_abc",
    );
  });

  it("renders the Subagents tab in terminal-first sessions too", () => {
    // Subagents are spawned independently of terminals, so the tab
    // must be reachable in claude-native sessions where the Terminals
    // tab is intentionally absent.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      {
        id: "conv_native",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
    ]);
    useChildSessionsMock.mockReturnValue(oneChild);

    renderShell("/c/conv_native");

    // No Terminals tab — terminal renders inline in main.
    expect(screen.queryByRole("tab", { name: /Shells/i })).toBeNull();
    // Subagents tab is present.
    const subagentsTab = screen.getByRole("tab", { name: /Agents/i });
    expect(subagentsTab).toBeInTheDocument();
    fireEvent.mouseDown(subagentsTab);
    expect(screen.getByTestId("subagents-panel")).toHaveAttribute(
      "data-conversation-id",
      "conv_native",
    );
  });

  it("shows a count badge on the Subagents tab when children exist", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    useChildSessionsMock.mockReturnValue({
      children: [
        {
          id: "conv_child_a",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "completed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
        {
          id: "conv_child_b",
          title: "frontend_engineer:rail",
          tool: "frontend_engineer",
          session_name: "rail",
          current_task_status: "in_progress",
          busy: true,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_abc");

    // One of the two children is busy → the badge shows working/total
    // ("1/3" — two children + the main agent) with the active green
    // (success) tint, so activity is visible without opening the
    // panel. "1/2" means the main agent was dropped from the total.
    const tab = screen.getByRole("tab", { name: /Agents\s*1\/3/i });
    expect(tab).toBeInTheDocument();
    expect(within(tab).getByText("1/3").className).toContain("text-success");
  });

  it("shows a plain total (muted, no tint) on the Agents badge when none are working", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    useChildSessionsMock.mockReturnValue({
      children: [
        {
          id: "conv_child_a",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "completed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
        {
          id: "conv_child_b",
          title: "researcher:api",
          tool: "researcher",
          session_name: "api",
          current_task_status: "completed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_abc");

    // No busy child → plain total "3" (two children + the main agent)
    // in the muted style (no success tint), so a settled fan-out
    // doesn't draw the eye.
    const tab = screen.getByRole("tab", { name: /Agents\s*3/i });
    const badge = within(tab).getByText("3");
    expect(badge.className).toContain("text-muted-foreground");
    expect(badge.className).not.toContain("text-success");
  });

  it("keeps the terminal count badge neutral when a terminal spawns off-tab", () => {
    // The tab badge is a quantity indicator, not an error or alert. A new
    // terminal should update the count without switching to destructive red.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    // Baseline: one pre-existing terminal at mount.
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "main", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    // Build the tree with a stable QueryClient and a fresh element per call:
    // React bails on a rerender given the identical element reference, so the
    // new mock would not be read.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const makeTree = () => (
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_abc"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route
                  path="c/:conversationId"
                  element={
                    <>
                      <TerminalFirstViewProbe />
                      <LocationDisplay />
                    </>
                  }
                />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(makeTree());

    let badge = within(screen.getByRole("tab", { name: /Shells/i })).getByText("1");
    expect(badge.className).toContain("text-muted-foreground");
    expect(badge.className).not.toContain("bg-destructive");

    // A second terminal spawns while the user is on the (default) Files tab.
    useTerminalsMock.mockReturnValue({
      terminals: [
        { id: "terminal_main", name: "main", session: "main", running: true },
        { id: "terminal_2", name: "bash", session: "main", running: true },
      ],
      isLoading: false,
      error: null,
    });
    rerender(makeTree());

    badge = within(screen.getByRole("tab", { name: /Shells/i })).getByText("2");
    expect(badge.className).toContain("text-muted-foreground");
    expect(badge.className).not.toContain("bg-destructive");
    expect(badge.className).not.toContain("text-white");

    // Opening the Terminals tab keeps the count in the same neutral style.
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Shells/i }));
    badge = within(screen.getByRole("tab", { name: /Shells/i })).getByText("2");
    expect(badge.className).toContain("text-muted-foreground");
    expect(badge.className).not.toContain("bg-destructive");
  });

  it("keeps the terminal count badge neutral when terminals stream in on connect", () => {
    // useTerminals is SSE-driven: on connect it seeds [] then snapshot-on-
    // connect replays a create for every running terminal. The replay should
    // update the count without making a normal refresh look like an error.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    // Cold cache on connect: empty seed, no terminals yet.
    useTerminalsMock.mockReturnValue({
      terminals: [],
      isLoading: false,
      error: null,
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const makeTree = () => (
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_abc"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route
                  path="c/:conversationId"
                  element={
                    <>
                      <TerminalFirstViewProbe />
                      <LocationDisplay />
                    </>
                  }
                />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(makeTree());

    // Snapshot-on-connect streams two terminals into the cache.
    useTerminalsMock.mockReturnValue({
      terminals: [
        { id: "terminal_main", name: "main", session: "main", running: true },
        { id: "terminal_2", name: "bash", session: "main", running: true },
      ],
      isLoading: false,
      error: null,
    });
    rerender(makeTree());

    const badge = within(screen.getByRole("tab", { name: /Shells/i })).getByText("2");
    expect(badge.className).toContain("text-muted-foreground");
    expect(badge.className).not.toContain("bg-destructive");
    expect(badge.className).not.toContain("text-white");
  });

  it("keeps the terminal count badge neutral while already viewing Terminals", () => {
    // The count badge should stay muted regardless of whether the user is
    // already on the Terminals tab.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "main", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const makeTree = () => (
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_abc"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route
                  path="c/:conversationId"
                  element={
                    <>
                      <TerminalFirstViewProbe />
                      <LocationDisplay />
                    </>
                  }
                />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(makeTree());

    // Switch to the Terminals tab first. Confirm it actually activated (the
    // inline section only mounts when the tab is selected) — otherwise the
    // "no alert" assertion below could pass for the wrong reason.
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Shells/i }));
    expect(screen.getByTestId("inline-terminals-section")).toBeInTheDocument();

    // Now a terminal spawns while it's the active tab.
    useTerminalsMock.mockReturnValue({
      terminals: [
        { id: "terminal_main", name: "main", session: "main", running: true },
        { id: "terminal_2", name: "bash", session: "main", running: true },
      ],
      isLoading: false,
      error: null,
    });
    rerender(makeTree());

    const badge = within(screen.getByRole("tab", { name: /Shells/i })).getByText("2");
    expect(badge.className).toContain("text-muted-foreground");
    expect(badge.className).not.toContain("bg-destructive");
  });

  it("keeps the idle Agents count badge neutral when a sub-agent spawns off-tab", () => {
    // Idle Agents badge counts are quantity indicators, not warning badges.
    // A new child should update the count without switching to destructive red.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    // Baseline: one pre-existing (settled, idle) child at mount.
    useChildSessionsMock.mockReturnValue({
      children: [
        {
          id: "conv_child_a",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "completed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ],
      isLoading: false,
      error: null,
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const makeTree = () => (
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_abc"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route
                  path="c/:conversationId"
                  element={
                    <>
                      <TerminalFirstViewProbe />
                      <LocationDisplay />
                    </>
                  }
                />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(makeTree());

    // 2 = one child + the main agent.
    let badge = within(screen.getByRole("tab", { name: /Agents/i })).getByText("2");
    expect(badge.className).toContain("text-muted-foreground");
    expect(badge.className).not.toContain("bg-destructive");

    // A second sub-agent spawns while the user is on another tab.
    useChildSessionsMock.mockReturnValue({
      children: [
        {
          id: "conv_child_a",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "completed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
        {
          id: "conv_child_b",
          title: "researcher:api",
          tool: "researcher",
          session_name: "api",
          current_task_status: "in_progress",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ],
      isLoading: false,
      error: null,
    });
    rerender(makeTree());

    badge = within(screen.getByRole("tab", { name: /Agents/i })).getByText("3");
    expect(badge.className).toContain("text-muted-foreground");
    expect(badge.className).not.toContain("bg-destructive");
    expect(badge.className).not.toContain("text-white");

    // Opening the Agents tab keeps the idle count in the same neutral style.
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Agents/i }));
    badge = within(screen.getByRole("tab", { name: /Agents/i })).getByText("3");
    expect(badge.className).toContain("text-muted-foreground");
    expect(badge.className).not.toContain("bg-destructive");
  });

  it("keeps the Agents count badge neutral when children stream in on connect", () => {
    // Mirror of the terminal connect case: child sessions arrive over SSE
    // (snapshot + live deltas). The count should update without looking like
    // an error on refresh.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    useChildSessionsMock.mockReturnValue({ children: [], isLoading: false, error: null });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const makeTree = () => (
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_abc"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route
                  path="c/:conversationId"
                  element={
                    <>
                      <TerminalFirstViewProbe />
                      <LocationDisplay />
                    </>
                  }
                />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(makeTree());

    useChildSessionsMock.mockReturnValue({
      children: [
        {
          id: "conv_child_a",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "completed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
        {
          id: "conv_child_b",
          title: "researcher:api",
          tool: "researcher",
          session_name: "api",
          current_task_status: "completed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ],
      isLoading: false,
      error: null,
    });
    rerender(makeTree());

    // 3 = two streamed-in children + the main agent.
    const badge = within(screen.getByRole("tab", { name: /Agents/i })).getByText("3");
    expect(badge.className).toContain("text-muted-foreground");
    expect(badge.className).not.toContain("bg-destructive");
    expect(badge.className).not.toContain("text-white");
  });

  it("keeps the Subagents tab visible inside a child session", () => {
    // The Subagents tab is now the canonical navigation surface for
    // the parent-children tree: inside a child it lists the siblings
    // plus a "main" link back to the parent. So it must stay visible.
    // The parent's child list is non-empty here (it contains this child),
    // so the multi-agent gate is satisfied and the tab shows.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_child", permission_level: null }]);
    useChildSessionsMock.mockReturnValue({
      children: [
        {
          id: "conv_child",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "in_progress",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ],
      isLoading: false,
      error: null,
    });
    // A terminal is attached so the Terminals tab is present — this test
    // asserts the three workspace tabs coexist inside a child, not the
    // Terminals-attach gate itself (covered separately below).
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "main", session: "main", running: true }],
      isLoading: false,
      error: null,
    });
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_child",
        agentId: "ag_child",
        agentName: null,
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: "researcher:auth",
        labels: {},
        items: [],
        pendingElicitations: [],
        permissionLevel: 4,
        parentSessionId: "conv_parent",
        subAgentName: null,
      },
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_child");

    expect(screen.getByRole("tab", { name: /Agents/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Files/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Shells/i })).toBeInTheDocument();
  });
});

describe("FilesPanel visibility", () => {
  it("renders FilesPanel when environmentQuery returns available true", () => {
    // available: true → os_env is configured, FilesPanel should be mounted.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc");

    // Files is the default tab, so its panel mounts immediately.
    // FilesPanel must be in the document — the spec has os_env so the
    // working-folder panel is available. Failure here means
    // showFilesPanel evaluated to false when it should be true.
    expect(screen.getByTestId("files-panel")).toBeInTheDocument();
  });

  it("hides FilesPanel when environmentQuery returns available false", () => {
    // available: false → server returned 404, spec has no os_env.
    // The whole panel must be removed from the DOM, including the
    // drawer (which is gated on the same showFilesPanel signal).
    useEnvironmentMock.mockReturnValue({
      data: { available: false, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc");

    expect(screen.queryByTestId("files-panel")).toBeNull();
    expect(screen.queryByTestId("files-panel-drawer")).toBeNull();
  });
});

describe("Right workspace card visibility", () => {
  it("keeps the card mounted with Agents as the only tab for a minimal agent", () => {
    // A no-os_env agent (available: false) with no shells and no todos
    // still has the unconditional Agents tab (the panel lists at least
    // the main agent), so the card mounts, the Agents tab is selected
    // by the fallback, and Files/Shells/Tasks are absent. An unmounted
    // card here means the always-visible Agents rule regressed.
    useEnvironmentMock.mockReturnValue({
      data: { available: false, root: null, home: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc");

    expect(screen.getByRole("complementary", { name: "Workspace" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /Files/i })).toBeNull();
    expect(screen.queryByRole("tab", { name: /Shells/i })).toBeNull();
    // The tab-fallback effect lands on Agents (the only available tab).
    expect(screen.getByRole("tab", { name: /Agents/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: "Collapse right panel" })).toBeInTheDocument();
  });

  it("keeps the card and collapse toggle when terminals are the only rail content", () => {
    // Same no-filesystem agent, but with an attached terminal: the rail
    // has a Terminals tab, so the card mounts and the collapse toggle
    // must render. Failure here means the toggle is still gated on
    // showFilesPanel alone — the pre-fix bug left a visible card with
    // no way to collapse it.
    useEnvironmentMock.mockReturnValue({
      data: { available: false, root: null, home: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_tui_main", name: "tui", session: "main", running: true }],
      isLoading: false,
      error: null,
    });
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc");

    expect(screen.getByRole("complementary", { name: "Workspace" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Shells/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collapse right panel" })).toBeInTheDocument();
  });

  it("starts open for a fresh session (no stored open-state)", () => {
    // A brand-new session has no persisted open-state, so the rail opens by
    // default — the card is mounted and the header offers Collapse, not
    // Expand.
    useEnvironmentMock.mockReturnValue({
      data: { available: false, root: null, home: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_fresh", permission_level: null }]);

    renderShell("/c/conv_fresh");

    expect(screen.getByRole("complementary", { name: "Workspace" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collapse right panel" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Expand right panel" })).toBeNull();
  });

  it("persists the open-state per session across remounts", () => {
    // Collapse the rail on a fresh session, then remount the same session:
    // the persisted closed-state wins over the open default.
    useEnvironmentMock.mockReturnValue({
      data: { available: false, root: null, home: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_persist", permission_level: null }]);

    const first = renderShell("/c/conv_persist");
    expect(screen.getByRole("complementary", { name: "Workspace" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Collapse right panel" }));
    expect(screen.queryByRole("complementary", { name: "Workspace" })).toBeNull();
    first.unmount();

    // Remount the same conversation: the stored closed-state (written by the
    // toggle) keeps the card hidden despite the open default.
    renderShell("/c/conv_persist");
    expect(screen.queryByRole("complementary", { name: "Workspace" })).toBeNull();
    expect(screen.getByRole("button", { name: "Expand right panel" })).toBeInTheDocument();
  });

  it("restores the selected rail tab per session", () => {
    // Seed conv_tabmem open on the Agents tab; on mount the rail restores that
    // tab as selected rather than falling back to Files.
    writeSessionWorkspaceState("conv_tabmem", { open: true, rightRailTab: "subagents" });
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null, home: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_tabmem", permission_level: null }]);

    renderShell("/c/conv_tabmem");

    expect(screen.getByRole("tab", { name: /Agents/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /Files/i })).toHaveAttribute("aria-selected", "false");
  });

  it("restores the open file tabs per session (independent of the ?file= param)", () => {
    // Seed conv_filemem with two open file tabs, none of which is in the URL.
    // On mount the rail restores both tabs even though no ?file= is present.
    writeSessionWorkspaceState("conv_filemem", {
      open: true,
      openFiles: ["a.ts", "b.ts"],
      selectedFilePath: "b.ts",
    });
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null, home: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_filemem", permission_level: null }]);

    renderShell("/c/conv_filemem");

    // Both remembered tabs render in the rail's file-tab strip (each tab div
    // carries title={path}), and the persisted active file drives the viewer.
    expect(screen.getByTitle("a.ts")).toBeInTheDocument();
    expect(screen.getByTitle("b.ts")).toBeInTheDocument();
    expect(screen.getByTestId("file-viewer-inline")).toHaveAttribute("data-path", "b.ts");
  });
});

describe("Embedded REPL terminal rail inventory", () => {
  it("shows no Terminals tab when the REPL is a terminal-first SDK session's only terminal", () => {
    // The runner auto-creates terminal_tui_main (the embedded Omnigent
    // REPL) for every runner-hosted SDK session. It backs the pill's
    // Terminal view; a rail entry for it reads as a phantom "main"
    // terminal on agents (Debby/Polly) that don't run a TUI. The pill
    // must stay openable: data-terminals-available reads the FULL list.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null, home: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_tui_main", name: "tui", session: "main", running: true }],
      isLoading: false,
      error: null,
    });
    mockConversations([
      {
        id: "conv_sdk",
        permission_level: null,
        // Terminal-first SDK session: omnigent.ui stamped by the
        // runner's REPL auto-create, NO native wrapper label.
        labels: { "omnigent.ui": "terminal" },
      },
    ]);

    renderShell("/c/conv_sdk");

    // The phantom rail entry is the reported bug: a tab here means the
    // REPL terminal leaked back into the inventory.
    expect(screen.queryByRole("tab", { name: /Shells/i })).toBeNull();
    // The pill still sees the REPL terminal — false here would grey out
    // the Terminal pill and make the embedded REPL unreachable.
    expect(screen.getByTestId("view-probe")).toHaveAttribute("data-terminals-available", "true");
  });

  it("lists only agent-launched terminals in the rail for terminal-first SDK sessions", () => {
    // With the REPL plus an agent-launched bash terminal, the tab shows
    // and its badge counts 1 (the bash terminal). 2 would mean the REPL
    // leaked into the inventory; 0/absent would hide the agent's real
    // terminal along with it.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null, home: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    useTerminalsMock.mockReturnValue({
      terminals: [
        { id: "terminal_tui_main", name: "tui", session: "main", running: true },
        { id: "terminal_bash_s1", name: "bash", session: "s1", running: true },
      ],
      isLoading: false,
      error: null,
    });
    mockConversations([
      {
        id: "conv_sdk",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
    ]);

    renderShell("/c/conv_sdk");

    const tab = screen.getByRole("tab", { name: /Shells/i });
    // The badge renders the inventory count next to the tab title.
    expect(tab).toHaveTextContent(/Shells\s*1/);
  });

  it("shows the Shells tab by default when the agent declares shell access", () => {
    // No shells exist yet (only the embedded REPL, which is excluded
    // from the inventory), but the agent's spec has a terminals: block
    // — the tab must show anyway so the "+ New shell" empty state is
    // reachable. A missing tab here means the gate still requires an
    // existing shell; a "0" in the tab means the badge leaked for the
    // empty default state.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null, home: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_tui_main", name: "tui", session: "main", running: true }],
      isLoading: false,
      error: null,
    });
    useSessionAgentMock.mockReturnValue({
      data: { id: "ag_x", name: "polly", terminals: ["zsh"] },
    } as ReturnType<typeof useSessionAgent>);
    mockConversations([
      {
        id: "conv_sdk",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
    ]);

    renderShell("/c/conv_sdk");

    const tab = screen.getByRole("tab", { name: /Shells/i });
    expect(tab).not.toHaveTextContent(/0/);
    // Display order: Shells sits to the RIGHT of Agents in the strip.
    const agentsTab = screen.getByRole("tab", { name: /Agents/i });
    expect(agentsTab.compareDocumentPosition(tab) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Selecting the tab mounts the shells section (whose empty state
    // carries the new-shell affordance), not a fall-through to the
    // Files panel — the content branch must share the trigger's gate.
    fireEvent.mouseDown(tab);
    expect(screen.getByTestId("inline-terminals-section")).toBeInTheDocument();
  });
});

describe("AppShell URL sync — file param", () => {
  it("restores the file viewer from the ?file= URL param on load", () => {
    // A shared link like /c/conv_abc?file=README.md should open the viewer
    // immediately, without the user having to click the file in the panel.
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc?file=README.md");

    // Viewer must be open and pointing at the file from the URL.
    // file-viewer = mobile push-panel (md:hidden); file-viewer-inline = desktop inline.
    // Failure: the conversationId effect did not read searchParams.get("file")
    // and call setSelectedFilePath with it.
    const pushPanel = screen.getByTestId("file-viewer");
    expect(pushPanel).toHaveAttribute("data-state", "open");
    expect(pushPanel).toHaveAttribute("data-path", "README.md");
  });

  it("restores the file viewer into the desktop rail on a ?file= reload", () => {
    // Regression (E2E reload-persistence): the Subagents/Terminals/Todos
    // panels are checked before the file viewer in the rail content
    // precedence. A ?file= reload must pull the rail to Files so the inline
    // viewer renders instead of another panel shadowing it.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc?file=README.md");

    // Desktop inline viewer shows the file; the Agents panel is not rendered.
    expect(screen.getByTestId("file-viewer-inline")).toHaveAttribute("data-path", "README.md");
    expect(screen.queryByTestId("subagents-panel")).toBeNull();
  });

  it("does not open the file viewer for an empty ?file= URL param", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc?file=");

    // Empty file params are malformed links, not real paths. They must not
    // mount FileViewer with an empty path or hide the normal Files panel.
    expect(screen.queryByTestId("file-viewer")).toBeNull();
    expect(screen.queryByTestId("file-viewer-inline")).toBeNull();
    expect(screen.getByTestId("files-panel")).toBeInTheDocument();
  });

  it("adds ?file= to the URL when a file is selected while already in explore view", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    // Start from the legacy ?view=explore link — resolves to the tree view.
    renderShell("/c/conv_abc?view=explore");

    // Sanity-check: tree (explore) mode active, no file open yet.
    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "false");

    fireEvent.click(screen.getByRole("button", { name: /files: select README.md/i }));

    // After selecting, ?file=README.md must appear in the URL.
    // Failure: the selectedFilePath sync effect did not fire (or fired with a stale
    // prev that had already lost the file param).
    expect(screen.getByTestId("url-params").textContent).toContain("file=README.md");
  });

  it("adds ?file= to the URL when a file is selected from the panel", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc");

    // No file open yet — URL params should be empty.
    expect(screen.getByTestId("url-params").textContent).toBe("");

    // Files is the default tab, so the files panel is already mounted.
    fireEvent.click(screen.getByRole("button", { name: /files: select README.md/i }));

    // After selecting, ?file=README.md must appear in the URL so the link is shareable.
    // Failure: the selectedFilePath sync useEffect did not call setSearchParams.
    expect(screen.getByTestId("url-params").textContent).toContain("file=README.md");
  });

  it("removes ?file=, ?diff=, and ?comment= from the URL when the file is closed", () => {
    // Starting with all three params to prove they are all cleaned up on close.
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc?file=README.md&diff=1&comment=c1");

    // Scope to mobile push-panel to avoid ambiguity with the inline frameless viewer.
    const pushPanel = screen.getByTestId("file-viewer");
    expect(pushPanel).toHaveAttribute("data-state", "open");

    fireEvent.click(within(pushPanel).getByRole("button", { name: /file-viewer: close/i }));

    // After closing, all file-related params must be gone.
    // Failure: the selectedFilePath sync effect did not delete diff/comment params
    // when selectedFilePath became null.
    const params = screen.getByTestId("url-params").textContent ?? "";
    expect(params).not.toContain("file=");
    expect(params).not.toContain("diff=");
    expect(params).not.toContain("comment=");
  });

  it("clears file/diff/comment/view params from the URL when the rail is collapsed", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    // Deep-link into the workspace with every rail-pointing param. conv_abc is
    // seeded open, so the rail mounts open with the params live.
    renderShell("/c/conv_abc?file=README.md&diff=1&comment=c1&view=changed");

    // Sanity: the rail is open and the params survived restore.
    expect(screen.getByRole("button", { name: "Collapse right panel" })).toBeInTheDocument();
    expect(screen.getByTestId("url-params").textContent).toContain("file=README.md");
    expect(screen.getByTestId("url-params").textContent).toContain("view=changed");

    fireEvent.click(screen.getByRole("button", { name: "Collapse right panel" }));

    // Collapsing hides the workspace, so every param that points into it is
    // stripped: file/diff/comment by the toggle's clearFileViewerUrl, and
    // view= by the scope-sync effect's rightPanelOpen gate. Failure means a
    // reload would re-open the rail to a file/view the user just dismissed.
    const afterCollapse = screen.getByTestId("url-params").textContent ?? "";
    expect(afterCollapse).not.toContain("file=");
    expect(afterCollapse).not.toContain("diff=");
    expect(afterCollapse).not.toContain("comment=");
    expect(afterCollapse).not.toContain("view=");

    fireEvent.click(screen.getByRole("button", { name: "Expand right panel" }));

    // Reopening rehydrates the URL from the remembered workspace state: the
    // file (re-added by the toggle) and the Changed scope (re-added by the
    // scope-sync effect). diff/comment were URL-only ephemerals, so they stay
    // gone. Failure means a reopened rail is no longer reflected/shareable in
    // the URL.
    const afterReopen = screen.getByTestId("url-params").textContent ?? "";
    expect(afterReopen).toContain("file=README.md");
    expect(afterReopen).toContain("view=changed");
    expect(afterReopen).not.toContain("diff=");
    expect(afterReopen).not.toContain("comment=");
  });
});

describe("AppShell URL sync — view param", () => {
  it("restores the tree view from the legacy ?view=explore URL param on load", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc?view=explore");

    // FilesPanel mock exposes data-flat-view; false means the folder tree.
    // Failure: the conversationId effect did not read searchParams.get("view")
    // and call setFilesPanelFlatView(false).
    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "false");
  });

  it("restores Changed-only view from the ?view=changed URL param on load", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc?view=changed");

    // FilesPanel mock exposes data-flat-view; true means Changed-only.
    // Failure: the conversationId effect did not read searchParams.get("view")
    // and call setFilesPanelFlatView(true).
    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "true");
  });

  it("writes ?view=changed to the URL when the user enables Changed only", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc");

    // Baseline: Files tab, tree view (flatView=false, no ?view= param).
    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "false");
    expect(screen.getByTestId("url-params").textContent).not.toContain("view=");

    fireEvent.click(screen.getByRole("button", { name: /files: switch to changed/i }));

    // After enabling Changed only: flat list active, ?view=changed in URL.
    // Failure: the filesPanelFlatView sync useEffect did not call setSearchParams.
    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "true");
    expect(screen.getByTestId("url-params").textContent).toContain("view=changed");
  });
});

describe("Files scope default and persistence", () => {
  const PREF_KEY = "omnigent:files-panel-preferences";

  function mockChangedFiles(paths: string[]) {
    useChangedFilesMock.mockReturnValue({
      data: {
        available: true,
        data: paths.map((path) => ({
          path,
          name: path.split("/").pop() ?? path,
          status: "modified" as const,
          bytes: 1,
          modified_at: 1,
        })),
      },
      isSuccess: true,
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceChangedFiles>);
  }

  it("defaults to All (tree) even when the conversation loads with changes", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    // Changes are present, but with nothing persisted the panel must open on
    // "All", not "Changed" — the core behavior change. Failure here
    // means the old auto-default-to-Changed-when-changes-exist logic is back.
    mockChangedFiles(["src/App.tsx"]);

    renderShell("/c/conv_abc");

    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "false");
    expect(screen.getByTestId("url-params").textContent).not.toContain("view=");
    // And the default must stay unpersisted: merely loading a conversation
    // with changes must not write a "Changed" preference behind the user's
    // back. A non-null value here means the removed auto-default crept back
    // in as a silent localStorage write.
    expect(localStorage.getItem(PREF_KEY)).toBeNull();
  });

  it("restores the remembered Changed scope on load from localStorage", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    // A previously-saved "Changed" choice must seed the scope on a fresh
    // mount. Failure means the useState initializer / restore effect isn't
    // reading the persisted preference.
    localStorage.setItem(PREF_KEY, JSON.stringify({ changedOnly: true }));

    renderShell("/c/conv_abc");

    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "true");
  });

  it("persists the toggle choice so it carries into the next session", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      { id: "conv_abc", permission_level: null },
      { id: "conv_xyz", permission_level: null },
    ]);

    // Session 1: opens on All, user switches to Changed.
    renderShell("/c/conv_abc");
    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "false");
    fireEvent.click(screen.getByRole("button", { name: /files: switch to changed/i }));
    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "true");
    // The choice was written to localStorage — that's what makes it sticky.
    expect(localStorage.getItem(PREF_KEY)).toBe(
      JSON.stringify({ changedOnly: true, sort: "recent", collapsed: false }),
    );

    // Re-enter a *different* session fresh: it must open on the remembered
    // "Changed" scope. cleanup() unmounts the shell but does NOT touch
    // localStorage (only beforeEach clears it, between tests), so the choice
    // written above survives into this second mount — exactly the cross-
    // session carry-over the issue asks for. Failure means the choice didn't
    // carry across sessions.
    cleanup();
    renderShell("/c/conv_xyz");
    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "true");
  });

  it("keeps the toggled scope on an in-mount session switch when the storage write is blocked", () => {
    // Simulate blocked storage (Safari private mode): the write is swallowed,
    // so localStorage stays empty. The remembered scope must come from the
    // in-memory ref, not a fresh localStorage read — otherwise switching
    // sessions resets the user's just-made choice back to the default.
    //
    // jsdom routes setItem through Storage.prototype (an instance-level spy is
    // a no-op), so we spy there but throw ONLY for the files-panel preference
    // key and delegate every other key to the real method. That keeps the
    // blast radius to this one localStorage write — AppShell's sessionStorage
    // panel-key writes on mount/navigation still go through untouched.
    const realSetItem = Storage.prototype.setItem;
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      if (key === PREF_KEY) throw new Error("storage blocked");
      realSetItem.call(this, key, value);
    });
    try {
      useEnvironmentMock.mockReturnValue({
        data: { available: true, root: null },
        isLoading: false,
      } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
      mockConversations([
        { id: "conv_abc", permission_level: null },
        { id: "conv_xyz", permission_level: null },
      ]);

      // Stable QueryClient + a fresh element per call so AppShell stays mounted
      // across the navigation (a fresh client would remount and reset the ref).
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={qc}>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/c/conv_abc"]}>
              <Routes>
                <Route element={<AppShell />}>
                  <Route
                    path="c/:conversationId"
                    element={
                      <>
                        <SessionNavButton to="/c/conv_xyz" />
                        <LocationDisplay />
                      </>
                    }
                  />
                </Route>
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </QueryClientProvider>,
      );

      // Opens on All, user switches to Changed. The write throws (swallowed),
      // so localStorage never records the choice.
      expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "false");
      fireEvent.click(screen.getByRole("button", { name: /files: switch to changed/i }));
      expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "true");
      expect(localStorage.getItem(PREF_KEY)).toBeNull();

      // Switch to another session within the same mount (bare path → no ?view=
      // param, so the conversation-switch effect hits the remembered-scope
      // fallback). Failure here means the fallback re-read empty localStorage
      // and reverted to All instead of using the in-memory ref.
      fireEvent.click(screen.getByTestId("nav-session"));
      expect(screen.getByTestId("url-params").textContent).not.toContain("view=");
      expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "true");
    } finally {
      setItemSpy.mockRestore();
    }
  });

  it("lets a deep-link ?view=explore param win over the remembered scope", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    // Remembered choice is "Changed", but the explicit deep-link to the tree
    // must win for this navigation.
    localStorage.setItem(PREF_KEY, JSON.stringify({ changedOnly: true }));

    renderShell("/c/conv_abc?view=explore");

    expect(screen.getByTestId("files-panel")).toHaveAttribute("data-flat-view", "false");
    // The deep-link override is transient: it must NOT rewrite the stored
    // preference. If this flips to changedOnly:false, a shared ?view=explore
    // link would silently clobber the recipient's remembered choice.
    expect(localStorage.getItem(PREF_KEY)).toBe(
      JSON.stringify({ changedOnly: true, sort: "recent", collapsed: false }),
    );
  });
});

describe("AppShell scope switch — conversation redirect (stale-closure regression)", () => {
  it("keeps the URL on the current conversation when All is clicked after an in-app switch", () => {
    // Regression for the "click All → jump to a different conversation" bug.
    // AppShell is a layout route that never remounts across /c/:a → /c/:b, so
    // when showScopeView was useCallback([]) it stayed frozen to AppShell's
    // first render: its clearFileViewerUrl closed over react-router's
    // first-mount navigate, whose relative setSearchParams resolves against the
    // pathname captured then. Clicking a scope button after switching sessions
    // therefore yanked the URL back to the conversation open at first mount.
    //
    // This test reproduces that exact flow: mount on conv_abc, switch in-app to
    // conv_xyz (AppShell stays mounted, carrying the stale closure), open a
    // file, click All. With the bug the pathname reverts to /c/conv_abc; with
    // the fix (showScopeView depends on clearFileViewerUrl → setSearchParams →
    // the live location) it stays on /c/conv_xyz.
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      { id: "conv_abc", permission_level: null },
      { id: "conv_xyz", permission_level: null },
    ]);

    // Stable QueryClient + a single render so AppShell stays mounted across the
    // navigation — a remount would rebuild the callback and hide the bug.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_abc"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route
                  path="c/:conversationId"
                  element={
                    <>
                      <SessionNavButton to="/c/conv_xyz" />
                      <PathDisplay />
                    </>
                  }
                />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );

    // First mount is conv_abc; switch in-app to conv_xyz (no remount).
    fireEvent.click(screen.getByTestId("nav-session"));
    expect(screen.getByTestId("url-pathname").textContent).toBe("/c/conv_xyz");

    // Open a file in conv_xyz so the viewer's close has an active file to clear.
    fireEvent.click(screen.getByRole("button", { name: /files: select README\.md/i }));
    // Failure here means openFileViewer didn't set selectedFilePath.
    expect(screen.getByTestId("file-viewer-inline")).toHaveAttribute("data-path", "README.md");

    // Close the file via the viewer — this invokes showScopeView, the callback
    // that regressed. (The Changed/All switch lives in FilesPanel, which is
    // unmounted while a file is open, so the viewer's close is now the
    // affordance wired to showScopeView.)
    fireEvent.click(
      within(screen.getByTestId("file-viewer-inline")).getByRole("button", {
        name: /file-viewer: close/i,
      }),
    );

    // The conversation segment must be unchanged: clearing the file only edits
    // query params. If this reads /c/conv_abc the stale-closure redirect is
    // back (showScopeView frozen to the first-mount navigate).
    expect(screen.getByTestId("url-pathname").textContent).toBe("/c/conv_xyz");
    // And the file params were cleared (the scope view is now active).
    expect(screen.queryByTestId("file-viewer-inline")).toBeNull();
  });
});

describe("Right-rail tab switching — file viewer close", () => {
  function setupFilesAvailable() {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
  }

  it("closes the open file when switching rail tabs and does not restore it on return", () => {
    // With a single Files tab there's no per-tab file stash: switching to
    // another rail tab closes the viewer, and returning to Files shows the
    // panel, not the previously open file.
    setupFilesAvailable();
    // A terminal is attached so the Terminals tab is available to switch to
    // (the tab is gated on an attached terminal).
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "main", session: "main", running: true }],
      isLoading: false,
      error: null,
    });
    renderShell("/c/conv_abc");

    // Files is the default tab. Open README.md from the panel.
    fireEvent.click(screen.getByRole("button", { name: /files: select README\.md/i }));
    // Failure: openFileViewer did not set selectedFilePath.
    expect(screen.getByTestId("file-viewer-inline")).toHaveAttribute("data-path", "README.md");

    // Switch to Terminals — file viewer must close, terminals section appears.
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Shells/i }));
    // Failure: the tab-change handler did not close the viewer when leaving Files.
    expect(screen.queryByTestId("file-viewer-inline")).toBeNull();
    expect(screen.getByTestId("inline-terminals-section")).toBeInTheDocument();

    // Go back to Files — the panel shows, NOT the previously open file.
    fireEvent.mouseDown(screen.getByRole("tab", { name: /^Files$/i }));
    expect(screen.queryByTestId("file-viewer-inline")).toBeNull();
    expect(screen.getByTestId("files-panel")).toBeInTheDocument();
  });

  it("closes the file viewer when the user clicks the close (X) button", () => {
    setupFilesAvailable();
    renderShell("/c/conv_abc");

    fireEvent.click(screen.getByRole("button", { name: /files: select README\.md/i }));
    expect(screen.getByTestId("file-viewer-inline")).toHaveAttribute("data-path", "README.md");

    // Explicitly close the viewer with the X button (scoped to the inline rail viewer).
    fireEvent.click(
      within(screen.getByTestId("file-viewer-inline")).getByRole("button", {
        name: /file-viewer: close/i,
      }),
    );
    // Failure: closeFileViewer did not clear selectedFilePath.
    expect(screen.queryByTestId("file-viewer-inline")).toBeNull();
    expect(screen.getByTestId("files-panel")).toBeInTheDocument();
  });
});

describe("Mobile session menu", () => {
  // The right-rail tabs have no room on a phone, so they're reached via the
  // top-right session-menu FAB, which opens each tab's content as a full-
  // screen drawer. jsdom doesn't apply the `md:hidden` CSS, so the FAB and
  // its menu items are present in the DOM regardless of viewport.

  /** Open the session-menu dropdown and return its trigger. */
  function openSessionMenu() {
    const trigger = screen.getByRole("button", { name: /open session menu/i });
    // Radix DropdownMenu opens on pointerdown, not click.
    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
    return trigger;
  }

  it("lists the native session's menu entries (Terminals folds into the pill)", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      {
        id: "conv_native",
        permission_level: null,
        // Native sessions stamp BOTH labels: the wrapper (behavior
        // gates) and the terminal-first UI marker (presentation).
        labels: {
          "omnigent.wrapper": "claude-code-native-ui",
          "omnigent.ui": "terminal",
        },
      },
    ]);
    useTerminalsMock.mockReturnValue({
      // The vendor pane only — it is the pill's Terminal view, not a
      // shell, so the menu must not grow a Shells entry for it.
      terminals: [{ id: "terminal_claude_main", name: "claude", session: "main", running: true }],
      isLoading: false,
      error: null,
    });
    useChildSessionsMock.mockReturnValue({
      children: [
        {
          id: "conv_child_a",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "completed",
          busy: false,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ],
      isLoading: false,
      error: null,
    });
    useChatStore.setState({
      todos: [
        { content: "do a thing", status: "completed", activeForm: "doing a thing" },
        { content: "do another", status: "pending", activeForm: "doing another" },
      ],
    });

    renderShell("/c/conv_native");
    openSessionMenu();

    // Mirror of the desktop rail's tab strip for a native-wrapper session:
    // Files · Agents · Tasks. Shells is absent because the only terminal is
    // the vendor pane (the pill's Terminal view — excluded from the shell
    // inventory) and the mocked agent declares no terminals. An unexpected
    // Shells entry means the vendor pane leaked into the inventory.
    expect(screen.getByRole("menuitem", { name: /^Files$/i })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Shells/i })).toBeNull();
    expect(screen.getByRole("menuitem", { name: /Agents/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Tasks/i })).toBeInTheDocument();
  });

  it("keeps the Terminals entry in terminal-first SDK sessions (no native wrapper)", () => {
    // Terminal-first SDK sessions (embedded Omnigent REPL, `omnigent.ui:
    // terminal` without a wrapper label) keep the rail/menu Shells entry
    // for user shells: the pill is quick access to the REPL, while the
    // rail lists the shell inventory (sans the agent's own terminal).
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      {
        id: "conv_native",
        permission_level: null,
        labels: { "omnigent.ui": "terminal" },
      },
    ]);
    useTerminalsMock.mockReturnValue({
      terminals: [{ id: "terminal_main", name: "claude", session: "main", running: true }],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_native");
    openSessionMenu();

    // A missing Terminals entry here means the hide gating wrongly keys on
    // `isTerminalFirst` again instead of the native wrapper label.
    expect(screen.getByRole("menuitem", { name: /^Files$/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Shells/i })).toBeInTheDocument();
  });

  it("opens the Agents drawer and mounts the subagents panel", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);
    useChildSessionsMock.mockReturnValue({
      children: [
        {
          id: "conv_child_a",
          title: "researcher:auth",
          tool: "researcher",
          session_name: "auth",
          current_task_status: "in_progress",
          busy: true,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ],
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_abc");

    // Drawer starts closed and its content unmounted. Scope to the drawer:
    // the desktop rail renders its own default panel (Files), and jsdom
    // doesn't apply the rail's md:hidden.
    const drawer = screen.getByTestId("subagents-panel-drawer");
    expect(drawer).toHaveAttribute("data-state", "closed");
    expect(within(drawer).queryByTestId("subagents-panel")).toBeNull();

    openSessionMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /Agents/i }));

    // After selecting: drawer open, panel mounted against the active id.
    // Failure: openSubagentsPanel didn't set subagentsPanelOpen, or the
    // drawer didn't mount its children while open.
    const openDrawer = screen.getByTestId("subagents-panel-drawer");
    expect(openDrawer).toHaveAttribute("data-state", "open");
    expect(within(openDrawer).getByTestId("subagents-panel")).toHaveAttribute(
      "data-conversation-id",
      "conv_abc",
    );
  });

  it("opens the files drawer from the Files entry in the default tree view", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc");

    openSessionMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /^Files$/i }));

    // Files → drawer open in the default tree view (flatView=false). The
    // "Changed only" scope is the drawer's own toggle, not forced by the entry.
    // Failure: openFilesPanel didn't set filesPanelOpen.
    const drawer = screen.getByTestId("files-panel-drawer");
    expect(drawer).toHaveAttribute("data-state", "open");
    expect(drawer).toHaveAttribute("data-flat-view", "false");
  });

  it("opens the Tasks drawer for a claude-native session with todos", () => {
    useEnvironmentMock.mockReturnValue({
      data: { available: true, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([
      {
        id: "conv_native",
        permission_level: null,
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      },
    ]);
    useChatStore.setState({
      todos: [{ content: "build the thing", status: "in_progress", activeForm: "building" }],
    });

    renderShell("/c/conv_native");

    expect(screen.getByTestId("todos-panel-drawer")).toHaveAttribute("data-state", "closed");
    expect(screen.queryByTestId("todo-panel")).toBeNull();

    openSessionMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /Tasks/i }));

    // Failure: openTodosPanel didn't set todosPanelOpen, or the Tasks entry
    // was gated out despite isClaudeNative + a non-empty todo list.
    expect(screen.getByTestId("todos-panel-drawer")).toHaveAttribute("data-state", "open");
    expect(screen.getByTestId("todo-panel")).toBeInTheDocument();
  });

  it("keeps the FAB with only the Agents entry for a minimal agent", () => {
    // available:false → no files; no shells, no todos, no debug. The
    // Agents entry is unconditional (badge = 1, the main agent), so the
    // FAB still renders with exactly that entry. A missing FAB means
    // the always-visible Agents rule regressed on mobile.
    useEnvironmentMock.mockReturnValue({
      data: { available: false, root: null },
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
    mockConversations([{ id: "conv_abc", permission_level: null }]);

    renderShell("/c/conv_abc");

    // Radix DropdownMenu opens on pointerdown, not click.
    fireEvent.pointerDown(screen.getByRole("button", { name: /open session menu/i }), {
      button: 0,
      ctrlKey: false,
    });
    expect(screen.getByRole("menuitem", { name: /Agents\s*1/i })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Files/i })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /Shells/i })).toBeNull();
  });
});

describe("AppShell clone/fork action", () => {
  it("exposes canFork to a read-only collaborator on a top-level session", () => {
    // level 1 = read. A collaborator who can only view the shared session
    // must still be able to fork it into their own copy. The header/menu
    // has no Clone entry at all — the per-message "Fork from here" action
    // (ChatPage) reads ForkDialogContext.canFork, probed here.
    mockConversations([{ id: "conv_shared", permission_level: 1 }]);

    renderShell("/c/conv_shared");

    expect(screen.getByTestId("fork-probe")).toHaveAttribute("data-can-fork", "true");
  });

  it("reports canFork=false on a sub-agent (child) session", () => {
    // The server rejects forking a sub-agent session, so the affordance is
    // suppressed for children (parentSessionId set on the snapshot).
    mockConversations([]); // sidebar omits child rows
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_child",
        agentId: "ag_x",
        agentName: null,
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: null,
        labels: {},
        items: [],
        pendingElicitations: [],
        permissionLevel: 4,
        parentSessionId: "conv_parent",
        subAgentName: null,
      },
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_child");

    // The per-message fork action hides itself off this flag.
    expect(screen.getByTestId("fork-probe")).toHaveAttribute("data-can-fork", "false");
  });

  it("opens the fork dialog (name suggested from the source title) when clicked", () => {
    mockConversations([]);
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_shared",
        agentId: "ag_x",
        agentName: null,
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: "Auth refactor",
        labels: {},
        items: [],
        pendingElicitations: [],
        permissionLevel: 1,
        parentSessionId: null,
        subAgentName: null,
      },
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_shared");
    fireEvent.click(screen.getByTestId("fork-probe-open"));

    expect(screen.getByTestId("fork-session-dialog")).toBeInTheDocument();
    // Opened with a truncation point → the dialog announces a partial fork.
    expect(screen.getByText("Fork from this response")).toBeInTheDocument();
    // Name is optional and lives under Advanced now — the source-derived
    // default is a placeholder, not a prefilled value, so submitting blank
    // lets the server derive it.
    fireEvent.click(screen.getByTestId("fork-session-advanced-toggle"));
    const nameInput = screen.getByTestId("fork-session-title-input");
    expect(nameInput).toHaveValue("");
    expect(nameInput).toHaveAttribute("placeholder", "Fork of Auth refactor");
  });
});

describe("AppShell share action", () => {
  it("shows the Share button to an owner of a top-level session", () => {
    // permission_level null = owner. A top-level session can be shared.
    mockConversations([{ id: "conv_top", permission_level: null }]);

    renderShell("/c/conv_top");

    expect(screen.getByRole("button", { name: /share session/i })).toBeInTheDocument();
  });

  it("hides the Share button on a sub-agent (child) session", () => {
    // The server rejects sharing a sub-agent session (children inherit the
    // parent's grants), so the affordance is suppressed for children even
    // for an owner-level viewer (parentSessionId set on the snapshot).
    mockConversations([]); // sidebar omits child rows
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_child",
        agentId: "ag_x",
        agentName: null,
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: null,
        labels: {},
        items: [],
        pendingElicitations: [],
        permissionLevel: 4,
        parentSessionId: "conv_parent",
        subAgentName: null,
      },
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_child");

    expect(screen.queryByRole("button", { name: /share session/i })).toBeNull();
  });

  it("hides Share while the snapshot is still loading (no flicker on child nav)", () => {
    // Regression: navigating into a child loads its snapshot async. The row
    // is absent from the sidebar (children omitted) AND the snapshot hasn't
    // resolved, so we don't yet know it's a child. Gating on the absence of a
    // child marker rendered Share here, then yanked it once parentSessionId
    // arrived — a flicker. Share must stay hidden until the session is *known*
    // top-level (in the sidebar list, or a resolved snapshot with no parent).
    mockConversations([]); // sidebar omits the child row
    useSessionMock.mockReturnValue({ session: null, isLoading: true, error: null });

    renderShell("/c/conv_child");

    expect(screen.queryByRole("button", { name: /share session/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /clone session/i })).toBeNull();
  });
});

describe("Mobile header actions menu", () => {
  // On mobile (`< md`) the Share / Clone / Agent-info buttons collapse
  // into a single three-dot "Session actions" menu, gated by the same
  // permission booleans as the desktop buttons. jsdom doesn't apply the
  // responsive CSS, so both the desktop buttons and the mobile trigger are in
  // the DOM here — we assert on the menu's testid'd trigger and its menuitems
  // (which carry distinct `mobile-*` testids so they never collide with the
  // desktop buttons' `*-header` testids).

  /** An agent with one MCP server and one policy → agent-info affordance shown. */
  const agentWithInfo: Agent = {
    id: "ag_info",
    name: "hello_world",
    description: "A friendly agent",
    mcp_servers: [{ name: "files", transport: "stdio" }],
    policies: [{ name: "block_long_sleep", type: "function", on: ["tool_call"] }],
  };

  /** Open the three-dot menu (Radix opens on pointerdown, not click). */
  function openActionsMenu() {
    const trigger = screen.getByTestId("session-actions-menu");
    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
    return trigger;
  }

  it("offers Share and Clone for an owner of a top-level session", () => {
    mockConversations([
      {
        id: "conv_host",
        permission_level: null,
        labels: {},
        host_id: "host_a1b2",
        runner_id: "runner_token_abc",
      },
    ]);

    renderShell("/c/conv_host");
    openActionsMenu();

    // Menu labels drop the redundant "session" suffix (most entries relate to
    // the session), so match the bare verbs.
    expect(screen.getByRole("menuitem", { name: /^share$/i })).toBeInTheDocument();
    // Clone is not a menu entry — forking lives on each assistant
    // message's "Fork from here" action (ChatPage).
    expect(screen.queryByRole("menuitem", { name: /^clone$/i })).toBeNull();
    // Agent info is always available (policies section is shown for any session).
    expect(screen.getByRole("menuitem", { name: /agent info/i })).toBeInTheDocument();
    // Stop session is not a header action — it lives in the sidebar row's kebab.
    expect(screen.queryByRole("menuitem", { name: /^stop$/i })).toBeNull();
  });

  it("offers no Share to a read-only collaborator", () => {
    // level 1 = read: can fork (via the per-message action), but not
    // share (needs ≥3) — and Clone is not a menu entry at all.
    mockConversations([{ id: "conv_shared", permission_level: 1 }]);

    renderShell("/c/conv_shared");
    openActionsMenu();

    expect(screen.queryByRole("menuitem", { name: /^share$/i })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /^clone$/i })).toBeNull();
  });

  it("shows the Agent info entry when the agent has tools or policies", () => {
    mockConversations([{ id: "conv_abc", permission_level: 1 }]);
    useSessionAgentMock.mockReturnValue({ data: agentWithInfo } as ReturnType<
      typeof useSessionAgent
    >);

    renderShell("/c/conv_abc");
    openActionsMenu();

    expect(screen.getByRole("menuitem", { name: /agent info/i })).toBeInTheDocument();
  });

  it("opens the agent-info dialog with the agent's tools", () => {
    mockConversations([{ id: "conv_abc", permission_level: 1 }]);
    useSessionAgentMock.mockReturnValue({ data: agentWithInfo } as ReturnType<
      typeof useSessionAgent
    >);

    renderShell("/c/conv_abc");
    openActionsMenu();
    // onSelect closes the menu and flips agentInfoOpen → the dialog mounts.
    fireEvent.click(screen.getByRole("menuitem", { name: /agent info/i }));

    const dialog = screen.getByRole("dialog");
    // The extracted AgentInfoContent renders the server names.
    expect(within(dialog).getByText("files")).toBeInTheDocument();
  });

  it("shows only Agent info in the three-dot menu for a child session with no other actions", () => {
    // A child session at level 1 (no share) and child (no clone) — only
    // Agent info is available (policies are always accessible).
    mockConversations([]);
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_child",
        agentId: "ag_x",
        agentName: null,
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: null,
        labels: {},
        items: [],
        pendingElicitations: [],
        permissionLevel: 1,
        parentSessionId: "conv_parent",
        subAgentName: null,
      },
      isLoading: false,
      error: null,
    });

    renderShell("/c/conv_child");
    openActionsMenu();

    // Child session: policy/tools info remains available, but every
    // session-mutating action is hidden by permission or parent gating.
    expect(screen.getByRole("menuitem", { name: /agent info/i })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^share$/i })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /^clone$/i })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /^resume$/i })).toBeNull();
  });
});
