// The workspace_sidebar_v2 flag routes the session's sidebar to the tabs panel in desktop
// clients, as a drawer in a narrow window. Off, omitted or still loading, and in touch and
// native mobile clients, the classic sidebar shows exactly as before and the tabs code never loads.

import type * as UseTerminalsModule from "@/hooks/useTerminals";
import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as UsePullRequestsModule from "@/hooks/usePullRequests";
import type * as TabStoreModule from "@/components/BrowserPane/agentTools/tabStore";
import type { WorkspaceTabsHostProps } from "./workspaceTabs/WorkspaceTabsHost";
import type { GoToFileHostProps } from "./goToFile/GoToFileHost";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useContext } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { writeWorkspacePanelDefault } from "@/lib/workspacePanelPreferences";
import { writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import { maybeOpenLinkInApp } from "@/lib/openLinkInApp";
import { registeredPaletteCommands } from "@/test/paletteCommands";
import { isMacPlatform } from "@/lib/hotkeys";
import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import type { TabsRecord } from "@/components/BrowserPane/agentTools/tabStore";
import { type BrowserActionOutcome, emitBrowserActionSettled } from "@/lib/browserActionBus";
import type { AgentBrowserTabRequest } from "@/components/BrowserPane/agentTools/useRevealBrowserOnAgentTabs";

const tabs = vi.hoisted(() => ({
  host: null as WorkspaceTabsHostProps | null,
  opened: { ok: true as const, tabId: "tab", created: true, selected: true },
  // The session's open tabs, the selected one first; set per test.
  store: {
    records: [] as { id: string; place: { kind: string }; binding?: { kind: string } }[],
    selectedTabId: null as string | null,
    tabs() {
      return this.records;
    },
    selectedTab() {
      return this.records.find((tab) => tab.id === this.selectedTabId);
    },
    select: vi.fn((_id: string) => true),
    subscribe: (_listener: () => void) => () => {},
  },
}));

// An agent's browser_tabs requests, as the reveal hook hands them to the shell (its own
// test covers which events become requests), and the tabs main holds, which the relay
// changes; the test plays the relay's part.
const browser = vi.hoisted(() => ({
  reveal: null as ((request: AgentBrowserTabRequest) => void) | null,
  records: new Map<string, TabsRecord>(),
  listeners: new Set<(conversationId: string) => void>(),
}));
vi.mock("@/components/BrowserPane/agentTools/useRevealBrowserOnAgentTabs", () => ({
  useRevealBrowserOnAgentTabs: (
    _conversationId: string | null | undefined,
    reveal: (request: AgentBrowserTabRequest) => void,
  ) => {
    browser.reveal = reveal;
  },
}));
vi.mock("@/components/BrowserPane/agentTools/tabStore", async (importOriginal) => ({
  ...(await importOriginal<typeof TabStoreModule>()),
  mainBrowserTabs: (conversationId: string) => browser.records.get(conversationId) ?? null,
  onBrowserTabsChanged: (listener: (conversationId: string) => void) => {
    browser.listeners.add(listener);
    return () => browser.listeners.delete(listener);
  },
}));

// The file action host has its own suite; these tests check that it mounts only with the flag.
const fileActionHost = vi.hoisted(() => ({ throws: false }));
vi.mock("./fileCommands/FileActionHost", () => ({
  FileActionHost: () => {
    if (fileActionHost.throws) throw new Error("file action host broke");
    return <div data-testid="file-action-host" />;
  },
}));

// Go to File has its own suites; these tests check where it mounts and what it's handed.
const goToFile = vi.hoisted(() => ({ props: null as GoToFileHostProps | null }));
vi.mock("./goToFile/GoToFileHost", () => ({
  GoToFileHost: (props: GoToFileHostProps) => {
    goToFile.props = props;
    return <div data-testid="go-to-file-host" />;
  },
}));

// A draft typed into the host stands for its editors' unsaved text, which a remount loses.
vi.mock("./workspaceTabs/WorkspaceTabsHost", () => ({
  default: (props: WorkspaceTabsHostProps) => {
    tabs.host = props;
    return (
      <div data-testid="tabs-host">
        <input aria-label="Host draft" />
      </div>
    );
  },
}));
vi.mock("./workspaceTabs/workspaceTabsSessions", () => {
  const pages = ["files", "changes", "github", "subagents", "tasks", "content-search"];
  return {
    isWorkspacePage: (value: string | null) => value !== null && pages.includes(value),
    openWorkspacePage: vi.fn(async () => tabs.opened),
    openWorkspaceFileLink: vi.fn(async () => tabs.opened),
    openWorkspaceTerminal: vi.fn(async () => tabs.opened),
    showWorkspacePullRequests: vi.fn(async () => tabs.opened),
    openWorkspaceBrowserView: vi.fn(() => "tab"),
    openWorkspaceNewTab: vi.fn(async () => tabs.opened),
    workspaceTabsSession: () => ({ store: tabs.store }),
  };
});

// A chat link opens in the session's browser without asking the server for its host.
vi.mock("@/lib/fetchSessionHostId", () => ({ fetchSessionHostId: vi.fn(async () => null) }));

vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useConversations: vi.fn(() => ({
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
  })),
  useStopSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: vi.fn(() => ({ terminals: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof UseWorkspaceChangedFilesModule>()),
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
vi.mock("./Sidebar", () => ({
  Sidebar: () => <div data-testid="sidebar" />,
  isMobileViewport: vi.fn(() => false),
}));
vi.mock("./PullRequestPanel", () => ({
  selectPullRequestInPanel: vi.fn(),
  PullRequestPanel: () => <div data-testid="github-panel" />,
}));
vi.mock("./FilesPanel", () => ({
  FilesPanel: () => <div data-testid="files-panel" />,
  revealFolderInFilesPanel: vi.fn(),
}));
vi.mock("./FileViewer", () => ({ FileViewer: () => <div data-testid="file-viewer" /> }));
vi.mock("./InlineTerminalsSection", () => ({ InlineTerminalsSection: () => null }));
vi.mock("./TerminalsPanel", () => ({ TerminalsPanel: () => null }));

import { useSessionAgent } from "@/hooks/useAgents";
import { useSession } from "@/hooks/useSession";
import { useWorkspaceEnvironment } from "@/hooks/useWorkspaceChangedFiles";
import { LEVEL_READ } from "@/lib/permissionsApi";
import { readRecentWorkspaceFiles } from "@/lib/recentWorkspaceFiles";
import { TEMP_CONV_ID_PREFIX } from "@/lib/tempConversationId";
import { useChatStore } from "@/store/chatStore";
import { openTasksTab, showTasksTabBesideChat } from "@/store/sessionTasksActions";
import { AppShell } from "./AppShell";
import { setSplitOnScreen } from "./chatPaneToolbarHost";
import { FileViewerContext, useOpenGithubTab } from "./FileViewerContext";
import { isMobileViewport } from "./Sidebar";
import * as workspaceTabsSessions from "./workspaceTabs/workspaceTabsSessions";

const LINKED_PR = "https://github.com/example/app/pull/42";
/** Every way the shell reaches the tabs: pages, file links, shells, pull requests, browsers. */
const tabRoutes = () => [
  workspaceTabsSessions.openWorkspacePage,
  workspaceTabsSessions.openWorkspaceFileLink,
  workspaceTabsSessions.openWorkspaceTerminal,
  workspaceTabsSessions.showWorkspacePullRequests,
  workspaceTabsSessions.openWorkspaceBrowserView,
];

/** Nothing reached the tabs: no host rendered and no link, page or shell routed there. */
function expectTabsUntouched() {
  expect(tabs.host).toBeNull();
  for (const route of tabRoutes()) expect(route).not.toHaveBeenCalled();
}
const showAllPullRequestsRows = () =>
  registeredPaletteCommands().filter((command) => command.label === "Show all pull requests");

const DESKTOP = 1440;
const NARROW_DESKTOP = 700;

const withFlag = (enabled: boolean): ServerInfo => ({
  ...FALLBACK_SERVER_INFO,
  features: { ...FALLBACK_SERVER_INFO.features, workspace_sidebar_v2: enabled },
});
const { workspace_sidebar_v2: _omitted, ...featuresWithoutFlag } =
  FALLBACK_SERVER_INFO.features ?? {};
const WITHOUT_FLAG: ServerInfo = { ...FALLBACK_SERVER_INFO, features: featuresWithoutFlag };

/** Each media query's change listeners, told when the window resizes. */
const viewportListeners = new Map<(event: { matches: boolean }) => void, string>();

/** The window is `width` wide: below 1181px the shell shows the sidebar as a drawer. */
function setViewport(width: number, pointer: "fine" | "coarse" = "fine") {
  vi.spyOn(window, "matchMedia").mockImplementation((query: string) => {
    // A media query's rem is the browser's default font size, 16px here.
    const pixels = (bound: RegExpExecArray | null) =>
      bound === null ? null : Number(bound[1]) * (bound[2] === "rem" ? 16 : 1);
    const max = pixels(/max-width:\s*([\d.]+)(px|rem)/.exec(query));
    const min = pixels(/min-width:\s*([\d.]+)(px|rem)/.exec(query));
    const pointerQuery = /pointer:\s*(\w+)/.exec(query);
    const matches =
      pointerQuery !== null
        ? pointerQuery[1] === pointer
        : (max === null || width <= max) && (min === null || width >= min);
    return {
      matches,
      media: query,
      onchange: null,
      addEventListener: (_type: string, listener: (event: { matches: boolean }) => void) =>
        viewportListeners.set(listener, query),
      removeEventListener: (_type: string, listener: (event: { matches: boolean }) => void) =>
        viewportListeners.delete(listener),
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    } as unknown as MediaQueryList;
  });
  vi.mocked(isMobileViewport).mockReturnValue(width < 1181);
}

/** The person resizes the window to `width`, as its media queries report it. */
function resizeTo(width: number) {
  setViewport(width);
  act(() => {
    for (const [listener, query] of [...viewportListeners]) {
      listener({ matches: window.matchMedia(query).matches });
    }
  });
}

function ChatProbe() {
  const openGithubTab = useOpenGithubTab();
  const openFile = useContext(FileViewerContext)?.openFile;
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="search">{location.search}</output>
      {/* As the new session's hydration moves it to its real id, its query with it. */}
      <button
        type="button"
        onClick={() => navigate(`/c/conv_ws${location.search}`, { replace: true })}
      >
        Get the real id
      </button>
      <button type="button" onClick={() => openGithubTab?.(LINKED_PR)}>
        Open linked PR
      </button>
      <button type="button" onClick={() => navigate("/c/conv_ws?file=README.md&line=4")}>
        Follow README link
      </button>
      <button type="button" onClick={() => navigate("/c/conv_other?file=README.md&fullscreen=1")}>
        Follow another session's full screen link
      </button>
      <button type="button" onClick={() => openFile?.("src/app.ts", { line: 3 })}>
        Open cited file
      </button>
    </>
  );
}

function renderShell(info: ServerInfo | "loading", entry = "/c/conv_ws") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <CapabilitiesProvider info={info}>
      <QueryClientProvider client={client}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={[entry]}>
              <Routes>
                <Route element={<AppShell />}>
                  <Route path="c/:conversationId" element={<ChatProbe />} />
                </Route>
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </QueryClientProvider>
    </CapabilitiesProvider>,
  );
}

/** The session's browser tabs change, as the relay's write to main reports it. */
function changeBrowserTabs(conversationId: string, openTabs: string[], selected: string) {
  browser.records.set(conversationId, {
    tabs: openTabs,
    selected,
    agentTab: null,
    navs: {},
    version: (browser.records.get(conversationId)?.version ?? 0) + 1,
  });
  for (const listener of [...browser.listeners]) listener(conversationId);
}

/** This window's relay reports how the agent's browser_tabs request `actionId` ended. */
function relayAnswers(actionId: string, outcome: BrowserActionOutcome, conversationId = "conv_ws") {
  emitBrowserActionSettled(
    { type: "browser_action_request", actionId, action: "tabs", args: { action: "new" } },
    conversationId,
    outcome,
  );
}

/** Let every routed open finish, so an open that shouldn't happen has had its chance. */
async function settleRoutes() {
  await act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  });
}

/** The browser views the shell asked the tabs to show, in order. */
const browserViewsShown = () =>
  vi
    .mocked(workspaceTabsSessions.openWorkspaceBrowserView)
    .mock.calls.map(([sessionId, viewKey, options]) => {
      expect([sessionId, options]).toEqual(["conv_ws", { background: false }]);
      return viewKey;
    });

/** The agent opens or selects a browser tab in the session on screen. */
function agentTabs(request: AgentBrowserTabRequest) {
  if (!browser.reveal) throw new Error("the shell bound no browser reveal");
  act(() => browser.reveal!(request));
}

beforeEach(() => {
  localStorage.clear();
  goToFile.props = null;
  sessionStorage.clear();
  writeWorkspacePanelDefault("open");
  tabs.host = null;
  browser.reveal = null;
  browser.records.clear();
  browser.listeners.clear();
  viewportListeners.clear();
  tabs.store.records = [];
  tabs.store.selectedTabId = null;
  tabs.store.select.mockClear();
  vi.mocked(workspaceTabsSessions.openWorkspaceNewTab).mockClear();
  for (const route of tabRoutes()) vi.mocked(route).mockClear();
});
afterEach(() => {
  cleanup();
  delete (window as unknown as { omnigentNative?: unknown }).omnigentNative;
  vi.restoreAllMocks();
});

describe("workspace_sidebar_v2", () => {
  it("shows the tabs sidebar in a desktop window when the flag is on", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true));

    expect(await screen.findByTestId("tabs-host")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /^Files$/ })).toBeNull();
    expect(tabs.host).toMatchObject({ conversationId: "conv_ws", terminals: [] });
    // One file action host carries out what the sidebar's file menus and ⌘K ask for.
    expect(await screen.findAllByTestId("file-action-host")).toHaveLength(1);
  });

  it("keeps the shell when the file action host throws", async () => {
    fileActionHost.throws = true;
    vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      setViewport(DESKTOP);
      renderShell(withFlag(true));
      expect(await screen.findByTestId("tabs-host")).toBeInTheDocument();
      expect(screen.queryByTestId("file-action-host")).toBeNull();
      expect(screen.queryByText("Unable to display file actions.")).toBeNull();
    } finally {
      fileActionHost.throws = false;
    }
  });

  it.each([
    ["off", withFlag(false)],
    ["omitted", WITHOUT_FLAG],
    ["still loading", "loading" as const],
  ])("keeps the classic sidebar, and never loads the tabs code, when the flag is %s", (_, info) => {
    setViewport(DESKTOP);
    renderShell(info);

    expect(screen.getByRole("tab", { name: /^Files$/ })).toBeInTheDocument();
    expect(screen.queryByTestId("tabs-host")).toBeNull();
    expect(screen.queryByTestId("file-action-host")).toBeNull();
    expect(showAllPullRequestsRows()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Open linked PR" }));
    fireEvent.click(screen.getByRole("button", { name: "Open cited file" }));
    expectTabsUntouched();
  });

  it("mounts Go to File for the session with the flag on", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true));

    expect(await screen.findByTestId("go-to-file-host")).toBeInTheDocument();
    expect(goToFile.props).toMatchObject({ sessionId: "conv_ws", textSearch: false });
  });

  it.each([
    [
      "a read-only viewer the session doesn't share its files with",
      () => {
        const session = vi.mocked(useSession);
        const original = session.getMockImplementation()!;
        session.mockImplementation(
          () =>
            ({
              session: { id: "conv_ws", permissionLevel: LEVEL_READ, shareWorkspaceFiles: false },
              isLoading: false,
              error: null,
            }) as never,
        );
        onTestFinished(() => {
          session.mockImplementation(original);
        });
      },
    ],
    [
      "a session whose environment has no filesystem",
      () => {
        const environment = vi.mocked(useWorkspaceEnvironment);
        const original = environment.getMockImplementation()!;
        environment.mockImplementation(
          () => ({ data: { available: false, root: null }, isLoading: false }) as never,
        );
        onTestFinished(() => {
          environment.mockImplementation(original);
        });
      },
    ],
  ])("keeps Go to File's Sessions but searches no files for %s", async (_, arrange) => {
    arrange();
    setViewport(DESKTOP);
    const info = withFlag(true);
    renderShell({ ...info, features: { ...info.features, workspace_content_search: true } });

    expect(await screen.findByTestId("go-to-file-host")).toBeInTheDocument();
    expect(goToFile.props).toMatchObject({ sessionId: null, textSearch: false });
  });

  it("offers Go to File's text search when the session has content search", async () => {
    setViewport(DESKTOP);
    const info = withFlag(true);
    renderShell({ ...info, features: { ...info.features, workspace_content_search: true } });

    expect(await screen.findByTestId("go-to-file-host")).toBeInTheDocument();
    expect(goToFile.props?.textSearch).toBe(true);
    act(() => goToFile.props?.showTextSearch());
    await waitFor(() =>
      expect(workspaceTabsSessions.openWorkspacePage).toHaveBeenCalledWith(
        "conv_ws",
        "content-search",
      ),
    );
  });

  it.each([
    ["off", withFlag(false)],
    ["omitted", WITHOUT_FLAG],
  ])("leaves Go to File out, and lists no recent files, when the flag is %s", async (_, info) => {
    setViewport(DESKTOP);
    renderShell(info);
    await settleRoutes();

    expect(screen.queryByTestId("go-to-file-host")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open cited file" }));
    expect(readRecentWorkspaceFiles("conv_ws")).toEqual([]);
  });

  it("leaves Go to File out on a phone", async () => {
    setViewport(390, "coarse");
    renderShell(withFlag(true));
    await settleRoutes();

    expect(screen.queryByTestId("go-to-file-host")).toBeNull();
  });

  it("offers ⌘K's Show all pull requests with the flag on, which opens the session's list", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true));
    expect(await screen.findByTestId("tabs-host")).toBeInTheDocument();

    await waitFor(() => expect(showAllPullRequestsRows()).toHaveLength(1));
    const [row] = showAllPullRequestsRows();
    expect(row).toMatchObject({ id: "workspace:conv_ws:pull-requests:all" });
    act(() => row!.run());
    await waitFor(() =>
      expect(workspaceTabsSessions.showWorkspacePullRequests).toHaveBeenCalledWith("conv_ws", {
        kind: "overview",
      }),
    );
  });

  it("offers ⌘K's Show all pull requests with the sidebar collapsed, and reveals the list", async () => {
    writeWorkspacePanelDefault("collapsed");
    setViewport(DESKTOP);
    renderShell(withFlag(true));
    expect(screen.getByRole("button", { name: "Expand right panel" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Collapse right panel" })).toBeNull();

    await waitFor(() => expect(showAllPullRequestsRows()).toHaveLength(1));
    act(() => showAllPullRequestsRows()[0]!.run());
    await waitFor(() =>
      expect(workspaceTabsSessions.showWorkspacePullRequests).toHaveBeenCalledWith("conv_ws", {
        kind: "overview",
      }),
    );
    expect(await screen.findByRole("button", { name: "Collapse right panel" })).toBeInTheDocument();
  });

  it.each([
    ["keeps it collapsed when the session opens", "session-open", "Expand right panel"],
    ["reveals it for a click", "click", "Collapse right panel"],
  ] as const)(
    "shows the Tasks page in a collapsed sidebar, and %s",
    async (_, requestedBy, toggle) => {
      writeWorkspacePanelDefault("collapsed");
      const initialChatState = useChatStore.getState();
      useChatStore.setState({
        todos: [{ content: "Only item", status: "pending", activeForm: "" }],
      });
      onTestFinished(() => useChatStore.setState(initialChatState));
      setViewport(DESKTOP);
      renderShell(withFlag(true));
      expect(screen.getByRole("button", { name: "Expand right panel" })).toBeInTheDocument();

      act(() =>
        requestedBy === "click"
          ? openTasksTab("conv_ws")
          : showTasksTabBesideChat("conv_ws", requestedBy),
      );

      await waitFor(() =>
        expect(workspaceTabsSessions.openWorkspacePage).toHaveBeenCalledWith("conv_ws", "tasks"),
      );
      expect(await screen.findByRole("button", { name: toggle })).toBeInTheDocument();
    },
  );

  it.each([
    ["follows the flag", { floating_panes: true }, "fine", true],
    ["stays off when the server says off", { floating_panes: false }, "fine", false],
    ["stays off on an older server without it", {}, "fine", false],
    ["stays off on a touch client", { floating_panes: true }, "coarse", false],
  ] as const)("marks the shell for floating panes: %s", (_, features, pointer, floating) => {
    setViewport(DESKTOP, pointer);
    const info = withFlag(true);
    const { floating_panes: _withoutIt, ...base } = info.features ?? {};
    renderShell({ ...info, features: { ...base, ...features } });

    const shell = document.querySelector(".app-shell");
    if (floating) expect(shell).toHaveAttribute("data-floating-panes", "");
    else expect(shell).not.toHaveAttribute("data-floating-panes");
    // The session column is marked, so the styling cards its chat page and nothing else.
    expect(document.querySelector("[data-main-view]")).toHaveAttribute("data-main-view", "chat");
  });

  // Only floating panes draw a split as cards of its own; without them the chat keeps its card.
  it.each([
    ["floats as its own cards", true, "terminal"],
    ["shows without floating panes", false, "chat"],
  ] as const)(
    "gives the column the view it has while a split on screen beside the chat %s",
    (_case, floating, splitView) => {
      setViewport(DESKTOP);
      renderShell({
        ...withFlag(true),
        features: { ...withFlag(true).features, floating_panes: floating },
      });
      const column = () => document.querySelector("[data-main-view]");
      expect(column()).toHaveAttribute("data-main-view", "chat");

      act(() => setSplitOnScreen("conv_ws", true));
      expect(column()).toHaveAttribute("data-main-view", splitView);
      act(() => setSplitOnScreen("conv_ws", false));
      expect(column()).toHaveAttribute("data-main-view", "chat");
    },
  );

  it("reserves the header's room for the rail only while the rail sits inline", async () => {
    setViewport(1000);
    renderShell({
      ...withFlag(true),
      features: { ...withFlag(true).features, floating_panes: true },
    });
    const column = () => document.querySelector<HTMLElement>("[data-main-view]")!;
    const offset = () => column().style.getPropertyValue("--workspace-panel-offset");
    expect(await screen.findByTestId("tabs-host")).toBeInTheDocument();
    expect(tabs.host).toMatchObject({ placement: "drawer" });
    expect(offset()).toBe("0px");

    // Opening the drawer and putting it away leaves the rail open, and still out of the header's way.
    fireEvent.click(screen.getByRole("button", { name: "Open linked PR" }));
    const drawer = screen.getByTestId("workspace-tabs-drawer");
    await waitFor(() => expect(drawer).toHaveAttribute("data-state", "open"));
    expect(offset()).toBe("0px");
    fireEvent.click(within(drawer).getByRole("button", { name: "Close", hidden: true }));
    expect(drawer).toHaveAttribute("data-state", "closed");
    expect(offset()).toBe("0px");

    resizeTo(DESKTOP);
    await waitFor(() => expect(tabs.host).toMatchObject({ placement: "rail" }));
    expect(Number.parseFloat(offset())).toBeGreaterThan(0);
  });

  it("shows the tabs in the Workspace drawer in a narrow desktop window", async () => {
    setViewport(NARROW_DESKTOP);
    renderShell(withFlag(true));

    expect(await screen.findByTestId("tabs-host")).toBeInTheDocument();
    expect(tabs.host).toMatchObject({ conversationId: "conv_ws", placement: "drawer" });
    const drawer = screen.getByTestId("workspace-tabs-drawer");
    expect(drawer).toHaveAttribute("data-state", "closed");

    fireEvent.click(screen.getByRole("button", { name: "Open linked PR" }));
    await waitFor(() =>
      expect(workspaceTabsSessions.showWorkspacePullRequests).toHaveBeenCalledWith("conv_ws", {
        kind: "pull",
        url: LINKED_PR,
        overview: true,
      }),
    );
    expect(drawer).toHaveAttribute("data-state", "open");
    expect(screen.getByTestId("github-panel-drawer")).toHaveAttribute("data-state", "closed");

    // The drawer's own Close puts it away; the panel has no second one.
    expect(tabs.host!.onHide).toBeUndefined();
    fireEvent.click(within(drawer).getByRole("button", { name: "Close", hidden: true }));
    expect(drawer).toHaveAttribute("data-state", "closed");

    // Go to File serves the drawer's tabs too, and a resize keeps an open finder mounted.
    const finder = screen.getByTestId("go-to-file-host");
    expect(goToFile.props).toMatchObject({ sessionId: "conv_ws" });
    resizeTo(DESKTOP);
    await waitFor(() => expect(tabs.host).toMatchObject({ placement: "rail" }));
    expect(screen.getByTestId("go-to-file-host")).toBe(finder);
    resizeTo(NARROW_DESKTOP);
    await waitFor(() => expect(tabs.host).toMatchObject({ placement: "drawer" }));
    expect(screen.getByTestId("go-to-file-host")).toBe(finder);
  });

  it("opens the Workspace drawer on a page from the session's menu in a narrow window", async () => {
    setViewport(NARROW_DESKTOP);
    renderShell(withFlag(true));
    await screen.findByTestId("tabs-host");

    const menu = screen.getByRole("button", { name: /^(?:Conversation|Session) actions$/ });
    fireEvent.pointerDown(menu, { button: 0, ctrlKey: false, pointerType: "mouse" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Pull Requests" }));
    await waitFor(() =>
      expect(workspaceTabsSessions.openWorkspacePage).toHaveBeenCalledWith("conv_ws", "github"),
    );
    expect(screen.getByTestId("workspace-tabs-drawer")).toHaveAttribute("data-state", "open");
    expect(screen.getByTestId("github-panel-drawer")).toHaveAttribute("data-state", "closed");
  });

  it.each([
    ["narrows", DESKTOP, NARROW_DESKTOP],
    ["widens", NARROW_DESKTOP, DESKTOP],
  ])("keeps the sidebar and its drafts when the window %s past 1181px", async (_, from, to) => {
    setViewport(from);
    renderShell(withFlag(true));
    const draft = await screen.findByRole("textbox", { name: "Host draft", hidden: true });
    fireEvent.change(draft, { target: { value: "unsaved text" } });

    resizeTo(to);
    await settleRoutes();

    // The same host moved to its new place, so nothing was unmounted.
    expect(screen.getByRole("textbox", { name: "Host draft", hidden: true })).toBe(draft);
    expect(draft).toHaveValue("unsaved text");
    expect(screen.getAllByTestId("tabs-host")).toHaveLength(1);
    const drawer = screen.queryByTestId("workspace-tabs-drawer");
    expect(tabs.host!.placement ?? "rail").toBe(to < 1181 ? "drawer" : "rail");
    if (to < 1181) expect(drawer).toContainElement(draft);
    else expect(drawer).toBeNull();
  });

  describe("the session menu's Shells and Side chats, with a file tab selected", () => {
    const initialChatState = useChatStore.getState();
    beforeEach(() => {
      // A harness that has side chats, and an agent that declares shells, so both entries show.
      useChatStore.setState({ ...initialChatState, sessionHarness: "openai-agents" });
      vi.mocked(useSessionAgent).mockReturnValue({
        data: { id: "agent", terminals: [{ name: "shell" }] },
      } as never);
      tabs.store.records = [
        { id: "file", place: { kind: "files" }, binding: undefined },
        { id: "shell", place: { kind: "terminal" }, binding: { kind: "terminal" } },
        { id: "chat", place: { kind: "chat" }, binding: { kind: "chat" } },
      ];
      tabs.store.selectedTabId = "file";
    });
    afterEach(() => {
      useChatStore.setState(initialChatState);
      vi.mocked(useSessionAgent).mockReturnValue({ data: undefined } as never);
    });

    async function chooseFromMenu(entry: string) {
      setViewport(NARROW_DESKTOP);
      renderShell(withFlag(true));
      await screen.findByTestId("tabs-host");
      const menu = screen.getByRole("button", { name: /^(?:Conversation|Session) actions$/ });
      fireEvent.pointerDown(menu, { button: 0, ctrlKey: false, pointerType: "mouse" });
      fireEvent.click(await screen.findByRole("menuitem", { name: entry }));
      await settleRoutes();
      expect(screen.getByTestId("workspace-tabs-drawer")).toHaveAttribute("data-state", "open");
    }

    it.each([
      ["Shells", "shell"],
      ["Side chats", "chat"],
    ])("%s shows the session's open tab of that kind", async (entry, tabId) => {
      await chooseFromMenu(entry);
      expect(tabs.store.select).toHaveBeenCalledWith(tabId);
      expect(workspaceTabsSessions.openWorkspaceNewTab).not.toHaveBeenCalled();
    });

    it.each(["Shells", "Side chats"])(
      "%s opens a New tab to choose from when none is open",
      async (entry) => {
        tabs.store.records = [{ id: "file", place: { kind: "files" }, binding: undefined }];
        await chooseFromMenu(entry);
        expect(tabs.store.select).not.toHaveBeenCalled();
        expect(workspaceTabsSessions.openWorkspaceNewTab).toHaveBeenCalledWith("conv_ws");
      },
    );
  });

  it.each([
    ["a phone's browser", 390, "coarse", null],
    ["the iOS app on a wide iPad", 1366, "fine", "ios"],
  ] as const)(
    "keeps the classic drawers in %s, even with the flag on",
    async (_, width, pointer, shell) => {
      if (shell) {
        (window as unknown as { omnigentNative?: { kind: string } }).omnigentNative = {
          kind: shell,
        };
      }
      setViewport(width, pointer);
      renderShell(withFlag(true));

      fireEvent.click(screen.getByRole("button", { name: "Open linked PR" }));
      // Give a lazy host, or an open routed to it, its chance to land.
      await settleRoutes();
      expect(screen.queryByTestId("workspace-tabs-drawer")).toBeNull();
      if (width < 1181) {
        expect(screen.getByTestId("github-panel-drawer")).toHaveAttribute("data-state", "open");
      } else {
        expect(screen.getByRole("tab", { name: /^Files$/ })).toBeInTheDocument();
      }
      expectTabsUntouched();
    },
  );

  it("doesn't reopen a file the classic sidebar remembered when the tabs sidebar reopens", async () => {
    writeSessionWorkspaceState("conv_ws", { selectedFilePath: "old.ts", open: true });
    setViewport(DESKTOP);
    renderShell(withFlag(true));
    await screen.findByTestId("tabs-host");

    // The sidebar's toggle, collapsing and reopening it; one of these is this platform's.
    for (const modifier of [{ ctrlKey: true }, { metaKey: true }]) {
      for (let press = 0; press < 2; press += 1) {
        fireEvent.keyDown(window, { code: "BracketRight", key: "]", altKey: true, ...modifier });
        // oxlint-disable-next-line no-await-in-loop
        await settleRoutes();
      }
    }
    expect(tabs.host).not.toBeNull();
    expect(workspaceTabsSessions.openWorkspaceFileLink).not.toHaveBeenCalled();
    expect(screen.getByTestId("search")).not.toHaveTextContent("file=");
  });

  it("opens chat links to pull requests and files in their tabs", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true));
    await screen.findByTestId("tabs-host");

    fireEvent.click(screen.getByRole("button", { name: "Open linked PR" }));
    fireEvent.click(screen.getByRole("button", { name: "Open cited file" }));

    await waitFor(() =>
      expect(workspaceTabsSessions.showWorkspacePullRequests).toHaveBeenCalledWith("conv_ws", {
        kind: "pull",
        url: LINKED_PR,
        overview: true,
      }),
    );
    await waitFor(() =>
      expect(workspaceTabsSessions.openWorkspaceFileLink).toHaveBeenCalledWith(
        "conv_ws",
        "src/app.ts",
        { line: 3 },
        "user",
      ),
    );
    expect(screen.getByTestId("search")).toHaveTextContent("");
  });

  it("does nothing with links while a new session waits for its id, not even in the classic sidebar", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true), `/c/${TEMP_CONV_ID_PREFIX}draft`);
    await screen.findByTestId("tabs-host");
    expect(tabs.host).toMatchObject({ pending: true });

    fireEvent.click(screen.getByRole("button", { name: "Open cited file" }));
    fireEvent.click(screen.getByRole("button", { name: "Open linked PR" }));
    await settleRoutes();
    for (const route of tabRoutes()) expect(route).not.toHaveBeenCalled();
    // The classic sidebar would have taken the file into the URL.
    expect(screen.getByTestId("search")).toHaveTextContent("");
  });

  it("opens a new session's ?file= link once the session has its real id", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true), `/c/${TEMP_CONV_ID_PREFIX}draft?file=README.md&line=4`);
    await screen.findByTestId("tabs-host");
    await settleRoutes();
    expect(workspaceTabsSessions.openWorkspaceFileLink).not.toHaveBeenCalled();
    expect(screen.getByTestId("search")).toHaveTextContent("file=README.md");

    fireEvent.click(screen.getByRole("button", { name: "Get the real id" }));
    await waitFor(() =>
      expect(workspaceTabsSessions.openWorkspaceFileLink).toHaveBeenCalledWith(
        "conv_ws",
        "README.md",
        expect.objectContaining({ line: 4 }),
        "link",
      ),
    );
    await waitFor(() => expect(screen.getByTestId("search")).not.toHaveTextContent("file="));
  });

  it("opens a ?file= link's file in its tab at the cited line, and leaves the URL", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true), "/c/conv_ws?file=README.md&line=4");

    await waitFor(() =>
      expect(workspaceTabsSessions.openWorkspaceFileLink).toHaveBeenCalledWith(
        "conv_ws",
        "README.md",
        expect.objectContaining({ line: 4 }),
        "link",
      ),
    );
    await waitFor(() => expect(screen.getByTestId("search")).not.toHaveTextContent("file="));

    // The same link followed again opens the file again.
    fireEvent.click(screen.getByRole("button", { name: "Follow README link" }));
    await waitFor(() =>
      expect(workspaceTabsSessions.openWorkspaceFileLink).toHaveBeenCalledTimes(2),
    );
  });

  it("opens a ?diff= link's file in the Changes review, and a ?comment= link's thread on the file", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true), "/c/conv_ws?file=README.md&line=4&diff=1");
    await waitFor(() =>
      expect(workspaceTabsSessions.openWorkspaceFileLink).toHaveBeenCalledWith(
        "conv_ws",
        "README.md",
        { line: 4, view: "diff" },
        "link",
      ),
    );
    await waitFor(() => expect(screen.getByTestId("search")).not.toHaveTextContent("diff="));
    cleanup();

    renderShell(withFlag(true), "/c/conv_ws?file=README.md&comment=c1");
    await waitFor(() =>
      expect(workspaceTabsSessions.openWorkspaceFileLink).toHaveBeenLastCalledWith(
        "conv_ws",
        "README.md",
        { commentId: "c1" },
        "link",
      ),
    );
    await waitFor(() => expect(screen.getByTestId("search")).not.toHaveTextContent("comment="));

    // Full screen and back only change the layout; neither opens the linked file again.
    const linkOpens = vi.mocked(workspaceTabsSessions.openWorkspaceFileLink).mock.calls.length;
    act(() => tabs.host!.onToggleMaximized());
    await waitFor(() => expect(tabs.host).toMatchObject({ maximized: true }));
    act(() => tabs.host!.onToggleMaximized());
    await waitFor(() => expect(tabs.host).toMatchObject({ maximized: false }));
    expect(screen.getByTestId("search")).not.toHaveTextContent("file=");
    expect(workspaceTabsSessions.openWorkspaceFileLink).toHaveBeenCalledTimes(linkOpens);
  });

  it("fills the window with the sidebar for a ?fullscreen=1 link, and leaves the URL", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true), "/c/conv_ws?file=README.md&fullscreen=1");
    await waitFor(() => expect(tabs.host).toMatchObject({ maximized: true }));
    await waitFor(() => expect(screen.getByTestId("search")).not.toHaveTextContent("fullscreen="));
    cleanup();

    // In a narrow window the link opens the drawer instead, with nothing to fill.
    setViewport(NARROW_DESKTOP);
    renderShell(withFlag(true), "/c/conv_ws?file=README.md&fullscreen=1");
    await waitFor(() =>
      expect(screen.getByTestId("workspace-tabs-drawer")).toHaveAttribute("data-state", "open"),
    );
    expect(tabs.host).toMatchObject({ placement: "drawer", maximized: false });
    await waitFor(() => expect(screen.getByTestId("search")).not.toHaveTextContent("file="));
    expect(screen.getByTestId("search")).not.toHaveTextContent("fullscreen=");
  });

  it("reopens the left sidebar on leaving full screen after another session's full screen link", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true));
    await screen.findByTestId("tabs-host");
    const shell = document.querySelector(".app-shell");
    // A wide window shows the left sidebar.
    expect(shell).toHaveAttribute("data-sidebar-open", "true");
    act(() => tabs.host!.onToggleMaximized());
    expect(shell).not.toHaveAttribute("data-sidebar-open");

    fireEvent.click(
      screen.getByRole("button", { name: "Follow another session's full screen link" }),
    );
    await waitFor(() => expect(tabs.host).toMatchObject({ maximized: true }));
    await waitFor(() => expect(screen.getByTestId("search")).not.toHaveTextContent("fullscreen="));
    expect(shell).not.toHaveAttribute("data-sidebar-open");

    act(() => tabs.host!.onToggleMaximized());
    expect(tabs.host).toMatchObject({ maximized: false });
    expect(shell).toHaveAttribute("data-sidebar-open", "true");
  });

  it("shows the browser tab an agent selects in its tab", async () => {
    changeBrowserTabs("conv_ws", ["agent-browser", "t1"], "agent-browser");
    setViewport(DESKTOP);
    renderShell(withFlag(true));
    await screen.findByTestId("tabs-host");

    agentTabs({ actionId: "a1", action: "select", tabId: "t1" });
    await waitFor(() => expect(browserViewsShown()).toEqual(["t1"]));
    // A tab the session doesn't have opens nothing; the relay answers the agent.
    agentTabs({ actionId: "a2", action: "select", tabId: "gone" });
    await settleRoutes();
    expect(browserViewsShown()).toEqual(["t1"]);
  });

  it("shows the tab a new request opened, whatever else changes meanwhile", async () => {
    changeBrowserTabs("conv_ws", ["agent-browser"], "agent-browser");
    setViewport(DESKTOP);
    renderShell(withFlag(true));
    await screen.findByTestId("tabs-host");

    agentTabs({ actionId: "a1", action: "new" });
    // The user opens a page in another window, and another session's agent opens a tab.
    changeBrowserTabs("conv_ws", ["agent-browser", "user-page"], "user-page");
    relayAnswers("a9", { ok: true, data: { tab_id: "elsewhere" } }, "conv_other");
    relayAnswers("a8", { ok: true, data: { tab_id: "stale" } });
    await settleRoutes();
    expect(browserViewsShown()).toEqual([]);

    changeBrowserTabs("conv_ws", ["agent-browser", "user-page", "agent-page"], "agent-page");
    relayAnswers("a1", { ok: true, data: { tab_id: "agent-page" } });
    await waitFor(() => expect(browserViewsShown()).toEqual(["agent-page"]));
  });

  it("drops a new request a later one replaced, or that failed", async () => {
    changeBrowserTabs("conv_ws", ["agent-browser", "t1"], "agent-browser");
    setViewport(DESKTOP);
    renderShell(withFlag(true));
    await screen.findByTestId("tabs-host");

    agentTabs({ actionId: "a1", action: "new" });
    agentTabs({ actionId: "a2", action: "select", tabId: "t1" });
    await waitFor(() => expect(browserViewsShown()).toEqual(["t1"]));
    changeBrowserTabs("conv_ws", ["agent-browser", "t1", "late"], "late");
    relayAnswers("a1", { ok: true, data: { tab_id: "late" } });

    agentTabs({ actionId: "a3", action: "new" });
    relayAnswers("a3", { ok: false, error: "this session already has 20 tabs open" });
    await settleRoutes();
    expect(browserViewsShown()).toEqual(["t1"]);
  });

  it("reuses the browser tab a chat link opened when the same link is clicked again", async () => {
    // The desktop app's embedded browser, which remembers what each view shows.
    const pages = new Map<string, string>();
    const browserOpenOrNavigate = vi.fn(async (viewId: string, url: string) => {
      pages.set(viewId, url);
      return { ok: true };
    });
    (window as unknown as { omnigentDesktop?: object }).omnigentDesktop = {
      kind: "electron",
      browserOpenOrNavigate,
      browserHasView: vi.fn(async (viewId: string) => ({
        exists: pages.has(viewId),
        url: pages.get(viewId),
      })),
    };
    writeOpenLinksInApp(true);
    try {
      setViewport(DESKTOP);
      renderShell(withFlag(true));
      await screen.findByTestId("tabs-host");

      expect(maybeOpenLinkInApp("conv_ws", "https://example.com/docs")).toBe(true);
      await waitFor(() => expect(browserViewsShown()).toHaveLength(1));
      // Once the first open has finished, the same link shows the same page again.
      expect(maybeOpenLinkInApp("conv_ws", "https://example.com/docs")).toBe(true);
      await waitFor(() => expect(browserViewsShown()).toHaveLength(2));

      expect(browserOpenOrNavigate).toHaveBeenCalledOnce();
      const [first, again] = browserViewsShown();
      expect(again).toBe(first);
    } finally {
      delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
    }
  });
});

describe("⇧⌘F and ⌘K's full screen row", () => {
  const pressFullScreen = () => {
    const mac = isMacPlatform();
    const event = new KeyboardEvent("keydown", {
      key: "F",
      code: "KeyF",
      metaKey: mac,
      ctrlKey: !mac,
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      window.dispatchEvent(event);
    });
    return event;
  };
  const fullScreenRow = () =>
    registeredPaletteCommands().find(
      (command) => command.shortcut === "workspace.toggleFullScreen",
    );

  it("fills the window from the chord, and leaves it on a second press", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(true));
    await screen.findByTestId("tabs-host");
    // The row and the chord load with the tabs sidebar's code.
    await waitFor(() => expect(fullScreenRow()).toBeDefined());
    expect(pressFullScreen().defaultPrevented).toBe(true);
    expect(tabs.host).toMatchObject({ maximized: true });
    expect(fullScreenRow()?.label).toBe("Exit workspace full screen");
    pressFullScreen();
    expect(tabs.host).toMatchObject({ maximized: false });
    expect(fullScreenRow()?.label).toBe("Make the workspace full screen");
  });

  it("opens a closed sidebar straight into full screen from ⌘K", async () => {
    setViewport(DESKTOP);
    writeSessionWorkspaceState("conv_ws", { open: false });
    renderShell(withFlag(true));
    await waitFor(() => expect(fullScreenRow()).toBeDefined());
    act(() => fullScreenRow()!.run());
    await waitFor(() => expect(tabs.host).toMatchObject({ maximized: true }));
  });

  it("leaves the chord alone and offers no row in the classic sidebar", async () => {
    setViewport(DESKTOP);
    renderShell(withFlag(false));
    await screen.findByRole("complementary", { name: "Workspace" });
    // A row registered late, once some lazy code loads, would still count.
    await act(async () => {
      await vi.dynamicImportSettled();
    });
    expect(pressFullScreen().defaultPrevented).toBe(false);
    expect(fullScreenRow()).toBeUndefined();
  });
});
