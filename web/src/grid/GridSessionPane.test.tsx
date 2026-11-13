import type * as TerminalsModule from "@/hooks/useTerminals";
import type * as SessionsApi from "@/lib/sessionsApi";
import type * as FilesPanelModule from "@/shell/FilesPanel";
import type * as GridSessionsModule from "./GridSessionsContext";
import {
  type ComponentProps,
  type ReactElement,
  type ReactNode,
  useContext,
  useState,
} from "react";
import {
  act,
  cleanup,
  fireEvent,
  render as renderComponent,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { emitUiOpenFile } from "@/lib/uiFileRequests";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, onTestFinished, vi } from "vitest";
import { type FileNavigationGuard, FileViewerContext } from "@/shell/FileViewerContext";
import type { WorkspacePanel } from "@/shell/WorkspacePanel";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { RearrangeableGrid } from "./GridWorkspace";
import { splitGrid } from "./gridLayout";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import { retrySession, updateSession } from "@/lib/sessionsApi";
import { clearSessionDrafts, getSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";
import type { Session } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import { serializeReplyDraft, type StoredReplyDraft } from "@/lib/replyDraft";
import { toast } from "sonner";
import { revealFolderInFilesPanel } from "@/shell/FilesPanel";
import { selectPullRequestInPanel } from "@/shell/PullRequestPanel";

const queryClient = new QueryClient();
const LINKED_PR = "https://github.com/example/app/pull/42";

function render(ui: ReactElement) {
  return renderComponent(ui, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    ),
  });
}

const fixture = vi.hoisted(() => ({
  labels: {} as Record<string, string>,
  permissionLevel: null as number | null,
  terminals: [] as TerminalsModule.TerminalInfo[],
  terminalPending: false,
  composerDisabled: false,
  hostOnline: undefined as boolean | undefined,
  livenessKind: "online",
  failedChat: "",
  failedTerminal: "",
  renderError: new Error("Grid surface render failed"),
  // Set while a cell's viewer holds unsaved edits and is asking to discard them.
  discardEdits: null as (() => void) | null,
}));
vi.mock("@/lib/sessionsApi", async (original) => ({
  ...(await original<typeof SessionsApi>()),
  retrySession: vi.fn(async () => ({ recovered: true })),
  updateSession: vi.fn(async () => ({ labels: {} })),
}));
vi.mock("@/components/HostBadge", () => ({
  HostBadge: ({ sessionId }: { sessionId: string }) => <span>Host for {sessionId}</span>,
}));
vi.mock("@/components/chat/SideChatSettings", () => ({
  SideChatSettings: ({ session }: { session: Session }) => <span>Model for {session.title}</span>,
}));
vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({
    sessionId,
    active,
    readOnly,
  }: {
    sessionId: string;
    active: boolean;
    readOnly: boolean;
  }) => {
    if (sessionId === fixture.failedTerminal) throw fixture.renderError;
    return <textarea aria-label="Terminal input" data-active={active} readOnly={readOnly} />;
  },
}));

vi.mock("@/canvas/CanvasWorkspace", () => ({ CanvasWorkspace: () => null }));
vi.mock("@/shell/PullRequestPanel", () => ({ selectPullRequestInPanel: vi.fn() }));
vi.mock("@/shell/FilesPanel", async (original) => ({
  ...(await original<typeof FilesPanelModule>()),
  revealFolderInFilesPanel: vi.fn(),
}));
vi.mock("@/hooks/useSidebarData", () => ({
  useLoadedConversations: () => ({}),
  useOptionalLoadedConversations: () => undefined,
}));

vi.mock("@/hooks/useSession", () => ({
  useSession: (id: string) => ({
    session: {
      id,
      title: id,
      agentId: `agent-${id}`,
      labels: fixture.labels,
      permissionLevel: fixture.permissionLevel,
    },
  }),
  useRootSessionId: (id: string) => id,
  sessionQueryOptions: (id: string) => ({
    queryKey: ["session", id],
    queryFn: async () => ({ id, title: id }),
  }),
}));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: () => true }));
vi.mock("@/hooks/useConversationEntryState", () => ({
  useConversationEntryState: () => ({
    terminalPending: fixture.terminalPending,
    claudePermissionMode: "default",
  }),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: () => {},
  useSessionRunnerOnline: () => true,
  useSessionHostOnline: () => fixture.hostOnline,
}));
vi.mock("@/hooks/useBrowserAgentRelay", () => ({ useBrowserAgentRelay: () => {} }));
vi.mock("@/hooks/useSessionLiveness", () => ({
  livenessRowFromSession: () => null,
  useSessionLiveness: () => ({ kind: fixture.livenessKind }),
}));
vi.mock("@/hooks/useChildSessions", () => ({ useChildSessions: () => ({ children: [] }) }));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: () => ({ data: { available: true, root: "/workspace" } }),
  useWorkspaceChangedFiles: () => ({ data: { data: [] } }),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof TerminalsModule>()),
  useTerminals: () => ({ terminals: fixture.terminals }),
  useDeleteTerminal: () => ({}),
}));
vi.mock("./GridSessionsContext", async (original) => ({
  ...(await original<typeof GridSessionsModule>()),
  useGridSessions: () => {
    const [selectedSessionId, selectSession] = useState("first");
    return { closePane: vi.fn(), selectedSessionId, selectSession };
  },
}));
vi.mock("@/components/chat/SideChatPane", () => ({
  SideChatPane: ({
    childId,
    gridComposer,
  }: {
    childId: string;
    gridComposer?: { leading?: ReactNode; trailing?: ReactNode };
  }) => {
    const context = useContext(FileViewerContext)!;
    const terminal = useTerminalFirst();
    if (childId === fixture.failedChat) throw fixture.renderError;
    return (
      <>
        <textarea aria-label="Message" disabled={fixture.composerDisabled} />
        <button type="button" onClick={() => terminal?.setView("terminal")}>
          Open terminal dialog
        </button>
        <button type="button" onClick={() => context.openFile("notes.md", { line: 7, endLine: 9 })}>
          Open citation
        </button>
        <button type="button" onClick={() => context.registerNavigationGuard?.(() => {})}>
          Block navigation
        </button>
        <button
          type="button"
          onClick={() =>
            context.registerNavigationGuard?.((_path, _options, navigate) => {
              fixture.discardEdits = navigate;
            })
          }
        >
          Edit file
        </button>
        <button type="button" onClick={() => context.openFolder?.("/workspace/experimental")}>
          Open folder
        </button>
        <button type="button" onClick={() => context.openGithubTab(LINKED_PR)}>
          Open PR
        </button>
        {gridComposer?.leading}
        {gridComposer?.trailing}
      </>
    );
  },
}));
vi.mock("@/shell/WorkspacePanel", () => ({
  WorkspacePanel: (props: ComponentProps<typeof WorkspacePanel>) => {
    const [draft, setDraft] = useState("");
    return (
      <aside
        aria-label="Workspace"
        aria-hidden={!props.open}
        data-testid={`workspace-${props.conversationId}`}
        data-maximized={String(props.maximized)}
        data-width={props.width}
        data-terminal={props.selectedTerminalKey ?? ""}
        data-tab={props.rightRailTab ?? ""}
        data-end-line={props.filePosition?.endLine}
      >
        <span>{props.selectedFilePath ?? "No file"}</span>
        <span>{props.filePosition?.line}</span>
        <span>{props.agentId}</span>
        <input
          aria-label="File draft"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <div {...props.handleProps} />
        <button type="button" onClick={() => props.openFileViewer("other.md")}>
          Open other
        </button>
        <button type="button" onClick={() => props.onCloseFile(props.selectedFilePath!)}>
          Close file
        </button>
        <button type="button" onClick={props.onToggleMaximized}>
          Maximize
        </button>
      </aside>
    );
  },
}));

afterEach(() => {
  cleanup();
  queryClient.clear();
  localStorage.clear();
  sessionStorage.clear();
  fixture.labels = {};
  fixture.permissionLevel = null;
  fixture.terminals = [];
  fixture.terminalPending = false;
  fixture.composerDisabled = false;
  fixture.hostOnline = undefined;
  fixture.livenessKind = "online";
  fixture.failedChat = "";
  fixture.failedTerminal = "";
  fixture.discardEdits = null;
  clearSessionDrafts();
  vi.restoreAllMocks();
});

/** A two-cell grid of "first" and "second". */
const twoCellGrid = () => (
  <MemoryRouter>
    <TooltipProvider>
      <RearrangeableGrid
        grid={{ id: "two-cells", layout: splitGrid("first", "first", "second", "right")! }}
      />
    </TooltipProvider>
  </MemoryRouter>
);

it("shares each cell's draft and tags with its single-session composer", () => {
  const mentions = { files: [{ path: "notes.md", isDir: false }], sessions: [] };
  const file = new File(["log"], "build.log");
  const replyDraft: StoredReplyDraft = {
    version: 1,
    quotes: [{ before: "About this:", text: "quoted line" }],
    text: "Unsent idea",
  };
  setSessionDraft("second", {
    text: serializeReplyDraft(replyDraft),
    replyDraft,
    files: [file],
    mentions,
  });
  const subagentTabDraft = { text: "Typed in a sub-agent tab", files: [] };
  useChatStore.getState().updateSideChatComposer("second", () => subagentTabDraft);
  const composer = () => useChatStore.getState().sideChatComposers.second;
  const view = render(twoCellGrid());
  expect(composer()).toMatchObject({
    text: "Unsent idea",
    quotes: [{ before: "About this:", text: "quoted line" }],
    files: [file],
    mentions,
  });
  act(() =>
    useChatStore
      .getState()
      .updateSideChatComposer("second", (draft) => ({ ...draft, text: "Edited in the grid" })),
  );
  expect(getSessionDraft("second")).toMatchObject({
    files: [file],
    replyDraft: { ...replyDraft, text: "Edited in the grid" },
    mentions,
  });
  view.unmount();
  // Leaving the grid hands the shared composer entry back untouched.
  expect(composer()).toBe(subagentTabDraft);
  expect(useChatStore.getState().sideChatComposers.first).toBeUndefined();

  // A single-view edit made meanwhile shows when the grid reopens.
  setSessionDraft("second", { text: "Edited in single view", files: [] });
  const reopened = render(twoCellGrid());
  expect(composer()?.text).toBe("Edited in single view");
  // Sending clears the cell's draft, and reopening doesn't bring it back.
  act(() => useChatStore.getState().clearSideChatComposer("second"));
  expect(getSessionDraft("second")).toBeUndefined();
  reopened.unmount();
  render(twoCellGrid());
  expect(composer()?.text).toBe("");
});

it("shows each cell's session controls and targets that session's permission mode", async () => {
  fixture.labels = { "omnigent.wrapper": "claude-code-native-ui" };
  document.title = "Single session";
  const view = render(twoCellGrid());
  // Named from the cells' snapshots; neither session is on a loaded sidebar page.
  await waitFor(() => expect(document.title).toBe("first + second"));
  const second = within(screen.getByRole("region", { name: "second" }));
  // An inactive cell keeps the toggle's slot, so activating it never shifts the titlebar.
  expect(second.queryByRole("button", { name: /grid workspace$/ })).toBeNull();
  expect(second.getByLabelText(/grid workspace$/)).toHaveStyle({ visibility: "hidden" });
  expect(second.getByText("Host for second")).toBeInTheDocument();
  expect(second.getByText("Model for second")).toBeInTheDocument();
  fireEvent.pointerDown(second.getByRole("button", { name: "Permission mode: Manual" }), {
    button: 0,
  });
  fireEvent.click(screen.getByTestId("composer-permission-option-auto"));
  await waitFor(() =>
    expect(updateSession).toHaveBeenCalledWith("second", { claudePermissionMode: "auto" }),
  );
  // A rejected switch reports why and frees the chip for another try.
  const toastError = vi.spyOn(toast, "error").mockReturnValue("error");
  vi.mocked(updateSession).mockRejectedValueOnce(new Error("Runner unavailable"));
  const chip = () => second.getByRole("button", { name: "Permission mode: Manual" });
  fireEvent.pointerDown(chip(), { button: 0 });
  fireEvent.click(screen.getByTestId("composer-permission-option-plan"));
  await waitFor(() => expect(toastError).toHaveBeenCalledWith("Runner unavailable"));
  await waitFor(() => expect(chip()).toBeEnabled());
  // Only the owner can switch modes.
  fixture.permissionLevel = 2;
  view.rerender(twoCellGrid());
  expect(chip()).toBeDisabled();
  fixture.permissionLevel = null;
  // Like the main composer, an unreachable session's chip can't switch modes.
  fixture.livenessKind = "host_offline";
  view.rerender(twoCellGrid());
  expect(second.getByRole("button", { name: "Permission mode: Manual" })).toBeDisabled();
  view.unmount();
  expect(document.title).toBe("Single session");
});

it("opens agent-requested files only in the active workspace and respects navigation guards", () => {
  render(
    <MemoryRouter>
      <TooltipProvider>
        <RearrangeableGrid
          grid={{ id: "ui-files", layout: splitGrid("first", "first", "second", "right")! }}
        />
      </TooltipProvider>
    </MemoryRouter>,
  );
  act(() => emitUiOpenFile({ type: "ui_open_file", path: "background.md" }, "second"));
  expect(screen.getByTestId("workspace-second")).not.toHaveTextContent("background.md");
  act(() => emitUiOpenFile({ type: "ui_open_file", path: "guide.md" }, "first"));
  expect(screen.getByTestId("workspace-first")).toHaveTextContent("guide.md");
  fireEvent.click(
    within(screen.getByRole("region", { name: "first (active)" })).getByText("Block navigation"),
  );
  act(() => emitUiOpenFile({ type: "ui_open_file", path: "blocked.md" }, "first"));
  expect(screen.getByTestId("workspace-first")).toHaveTextContent("guide.md");
  expect(screen.getByTestId("workspace-first")).not.toHaveTextContent("blocked.md");
});

/** The shell's file-viewer context, collecting the guards registered with it. */
function shellFileViewerContext(guards: Set<FileNavigationGuard>) {
  return {
    openFile: vi.fn(),
    registerNavigationGuard: (guard: FileNavigationGuard) => {
      guards.add(guard);
      return () => void guards.delete(guard);
    },
    openGithubTab: vi.fn(),
    isChangedPath: () => false,
    conversationId: undefined,
    workspaceRoot: null,
    workspaceHome: null,
  };
}

it("asks each cell's viewer before the shell's Close leaves the grid", () => {
  // Stands in for AppShell: its Close runs every registered guard, then leaves.
  const shellGuards = new Set<FileNavigationGuard>();
  const shellContext = shellFileViewerContext(shellGuards);
  const leave = vi.fn();
  const closeFromShell = () => {
    const guards = [...shellGuards];
    const next = (index: number): void => {
      const guard = guards[index];
      if (guard) guard(null, undefined, () => next(index + 1));
      else leave();
    };
    next(0);
  };
  render(
    <MemoryRouter>
      <TooltipProvider>
        <FileViewerContext.Provider value={shellContext}>
          <RearrangeableGrid
            grid={{ id: "dirty-close", layout: splitGrid("first", "first", "second", "right")! }}
          />
        </FileViewerContext.Provider>
      </TooltipProvider>
    </MemoryRouter>,
  );
  const second = screen.getByRole("region", { name: "second" });
  fireEvent.click(within(second).getByText("Edit file"));

  // Keep editing: the viewer never calls back, so the grid stays.
  act(() => closeFromShell());
  expect(fixture.discardEdits).not.toBeNull();
  expect(leave).not.toHaveBeenCalled();

  // Discard changes: leaving goes ahead.
  act(() => fixture.discardEdits?.());
  expect(leave).toHaveBeenCalledOnce();
});

it("opens chat-linked folders and PRs in that pane's workspace", () => {
  writeSessionWorkspaceState("first", { rightRailTab: "subagents" });
  render(
    <MemoryRouter>
      <TooltipProvider>
        <RearrangeableGrid
          grid={{ id: "folder-link", layout: splitGrid("first", "first", "second", "right")! }}
        />
      </TooltipProvider>
    </MemoryRouter>,
  );

  const first = within(screen.getByRole("region", { name: "first (active)" }));
  fireEvent.click(first.getByText("Block navigation"));
  fireEvent.click(first.getByText("Open folder"));
  fireEvent.click(first.getByText("Open PR"));
  expect(revealFolderInFilesPanel).not.toHaveBeenCalledWith("first", expect.anything());
  expect(selectPullRequestInPanel).not.toHaveBeenCalled();
  expect(screen.getByTestId("workspace-first")).toHaveAttribute("data-tab", "subagents");
  expect(screen.getByTestId("workspace-first")).toHaveAttribute("aria-hidden", "true");

  fireEvent.click(within(screen.getByRole("region", { name: "second" })).getByText("Open folder"));

  expect(revealFolderInFilesPanel).toHaveBeenCalledWith("second", "/workspace/experimental");
  const workspace = screen.getByRole("complementary", { name: "Workspace" });
  expect(workspace).toHaveAttribute("data-testid", "workspace-second");
  expect(workspace).toHaveAttribute("data-tab", "files");
  expect(workspace).toHaveTextContent("No file");

  fireEvent.click(
    within(screen.getByRole("region", { name: "second (active)" })).getByText("Open PR"),
  );
  expect(selectPullRequestInPanel).toHaveBeenCalledWith(queryClient, "second", LINKED_PR);
  expect(workspace).toHaveAttribute("data-tab", "github");
});

it("keeps cell views, input, permissions and shortcut targets independent", async () => {
  writeSessionWorkspaceState("first", { selectedTerminalKey: "terminal:terminal_tui_main" });
  fixture.labels = { "omnigent.ui": "terminal" };
  fixture.terminals = [{ id: "terminal_tui_main", name: "tui", session: "main", running: true }];
  const grid = { id: "terminal-grid", layout: splitGrid("first", "first", "second", "right")! };
  const tree = (layout = grid.layout) => (
    <MemoryRouter>
      <TooltipProvider>
        <RearrangeableGrid grid={{ ...grid, layout }} />
      </TooltipProvider>
    </MemoryRouter>
  );
  const rendered = render(tree());
  const first = screen.getByRole("region", { name: "first (active)" });
  const second = screen.getByRole("region", { name: "second" });
  expect(screen.getByTestId("workspace-first")).toHaveAttribute("data-terminal", "");
  fireEvent.change(within(first).getByLabelText("Message"), { target: { value: "Keep draft" } });
  fireEvent.click(within(first).getByRole("button", { name: "Terminal view" }));
  const terminal = await within(first).findByLabelText("Terminal input");
  expect(terminal).toHaveAttribute("data-active", "true");
  expect(terminal).not.toHaveAttribute("readonly");
  expect(within(second).getByLabelText("Message")).toBeVisible();
  fireEvent.click(within(second).getByRole("button", { name: "Open terminal dialog" }));
  expect(terminal).toHaveAttribute("data-active", "false");
  expect(await within(second).findByLabelText("Terminal input")).toHaveAttribute(
    "data-active",
    "true",
  );
  fireEvent.keyDown(window, { code: "Backslash", ctrlKey: true, altKey: true });
  expect(within(second).getByLabelText("Message")).toBeVisible();
  expect(terminal).toBeVisible();
  fireEvent.click(within(first).getByRole("button", { name: "Chat view" }));
  expect(within(first).getByLabelText("Message")).toHaveValue("Keep draft");
  fireEvent.click(within(first).getByRole("button", { name: "Terminal view" }));
  expect(within(first).getByLabelText("Terminal input")).toBe(terminal);
  fixture.permissionLevel = 2;
  rendered.rerender(tree(splitGrid("second", "second", "first", "right")!));
  expect(within(first).getByLabelText("Terminal input")).toBe(terminal);
  expect(terminal).toHaveAttribute("readonly");
  rendered.unmount();
  render(tree());
  expect(
    screen.getByRole("region", { name: "first (active)" }).querySelector('[data-visible="true"]'),
  ).not.toBeNull();
  expect(
    within(screen.getByRole("region", { name: "second" })).getByLabelText("Message"),
  ).toBeVisible();
});

it.each(["chat", "terminal"])(
  "contains a grid %s render failure without losing healthy drafts or cell controls",
  async (surface) => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const suppressExpectedError = (event: ErrorEvent) => {
      if (event.error === fixture.renderError) event.preventDefault();
    };
    window.addEventListener("error", suppressExpectedError);
    onTestFinished(() => window.removeEventListener("error", suppressExpectedError));
    fixture.labels = { "omnigent.ui": "terminal" };
    fixture.terminals = [{ id: "terminal_tui_main", name: "tui", session: "main", running: true }];
    const tree = () => (
      <MemoryRouter>
        <TooltipProvider>
          <RearrangeableGrid
            grid={{ id: "render-failure", layout: splitGrid("first", "first", "second", "right")! }}
          />
        </TooltipProvider>
      </MemoryRouter>
    );
    const view = render(tree());
    const first = screen.getByRole("region", { name: "first (active)" });
    const second = screen.getByRole("region", { name: "second" });
    const chat = within(first).getByLabelText("Message");
    const neighborChat = within(second).getByLabelText("Message");
    fireEvent.change(chat, { target: { value: "Chat draft" } });
    fireEvent.change(neighborChat, { target: { value: "Neighbor draft" } });
    fireEvent.click(within(first).getByText("Open citation"));
    const workspaceDraft = within(screen.getByTestId("workspace-first")).getByLabelText(
      "File draft",
    );
    fireEvent.change(workspaceDraft, { target: { value: "Unsaved file" } });
    fireEvent.click(within(first).getByRole("button", { name: "Terminal view" }));
    const terminal = await within(first).findByLabelText("Terminal input");
    fireEvent.change(terminal, { target: { value: "Terminal draft" } });
    if (surface === "chat") {
      fireEvent.click(within(first).getByRole("button", { name: "Chat view" }));
      fixture.failedChat = "first";
    } else {
      fixture.failedTerminal = "first";
    }

    view.rerender(tree());

    const failure = within(first).getByRole("alert");
    expect(failure).toHaveTextContent(`Unable to display ${surface}.`);
    expect(within(first).getByRole("button", { name: "Move first" })).toBeEnabled();
    expect(within(first).getByRole("link", { name: "Open first full screen" })).toHaveAttribute(
      "href",
      "/c/first",
    );
    expect(within(first).getByRole("button", { name: "Remove first from grid" })).toBeEnabled();
    const survivor = surface === "chat" ? terminal : chat;
    expect(within(first).getByLabelText(surface === "chat" ? "Terminal input" : "Message")).toBe(
      survivor,
    );
    expect(survivor).toHaveValue(surface === "chat" ? "Terminal draft" : "Chat draft");
    expect(within(second).getByLabelText("Message")).toBe(neighborChat);
    expect(neighborChat).toHaveValue("Neighbor draft");
    expect(within(screen.getByTestId("workspace-first")).getByLabelText("File draft")).toBe(
      workspaceDraft,
    );
    expect(workspaceDraft).toHaveValue("Unsaved file");

    fixture.failedChat = "";
    fixture.failedTerminal = "";
    fireEvent.click(within(failure).getByRole("button", { name: "Try again" }));
    expect(within(first).queryByRole("alert")).toBeNull();
    expect(
      await within(first).findByLabelText(surface === "chat" ? "Message" : "Terminal input"),
    ).toBeInTheDocument();
    expect(survivor).toHaveValue(surface === "chat" ? "Terminal draft" : "Chat draft");
  },
);

it("cycles keyboard focus through cells and toggles the grid workspace from the keyboard", () => {
  fixture.labels = { "omnigent.ui": "terminal" };
  fixture.terminals = [{ id: "terminal_tui_main", name: "tui", session: "main", running: true }];
  const layout = splitGrid(
    splitGrid("first", "first", "second", "right")!,
    "second",
    "third",
    "bottom",
  )!;
  render(
    <MemoryRouter>
      <TooltipProvider>
        <RearrangeableGrid grid={{ id: "cycle", layout }} />
      </TooltipProvider>
    </MemoryRouter>,
  );
  const cell = (name: string) => screen.getByRole("region", { name: new RegExp(`^${name}`) });
  // Clicking the switch also selects the third cell, so cycling starts there.
  fireEvent.click(within(cell("third")).getByRole("button", { name: "Terminal view" }));
  const next = () =>
    fireEvent.keyDown(window, {
      code: "BracketRight",
      ctrlKey: true,
      altKey: true,
      shiftKey: true,
    });

  next();
  expect(cell("first")).toHaveAttribute("data-active", "true");
  expect(within(cell("first")).getByRole("textbox", { name: "Message" })).toHaveFocus();
  next();
  expect(within(cell("second")).getByRole("textbox", { name: "Message" })).toHaveFocus();
  // A connected terminal claims focus itself; until then the cell holds it.
  next();
  expect(cell("third")).toHaveAttribute("data-active", "true");
  expect(within(cell("third")).getByLabelText("Terminal input")).toHaveAttribute(
    "data-active",
    "true",
  );
  expect(cell("third")).toHaveFocus();
  fireEvent.keyDown(window, { code: "BracketLeft", ctrlKey: true, altKey: true, shiftKey: true });
  expect(cell("second")).toHaveAttribute("data-active", "true");
  // A handled request must not pull focus back when the cell's view changes later.
  const outside = document.body.appendChild(document.createElement("button"));
  onTestFinished(() => outside.remove());
  outside.focus();
  fireEvent.keyDown(window, { code: "Backslash", ctrlKey: true, altKey: true });
  expect(outside).toHaveFocus();

  const toggle = screen.getByRole("button", { name: /grid workspace$/ });
  const label = toggle.getAttribute("aria-label");
  fireEvent.keyDown(window, { code: "BracketRight", ctrlKey: true, altKey: true });
  expect(toggle).not.toHaveAttribute("aria-label", label);
  fireEvent.keyDown(window, { code: "BracketRight", ctrlKey: true, altKey: true });
  expect(toggle).toHaveAttribute("aria-label", label);
});

it("focuses the cell itself when its composer is not ready", () => {
  fixture.composerDisabled = true;
  render(
    <MemoryRouter>
      <RearrangeableGrid
        grid={{ id: "not-ready", layout: splitGrid("first", "first", "second", "right")! }}
      />
    </MemoryRouter>,
  );
  fireEvent.keyDown(window, { code: "BracketRight", ctrlKey: true, altKey: true, shiftKey: true });
  expect(screen.getByRole("region", { name: "second (active)" })).toHaveFocus();
});

it("uses the terminal default and changes startup into a scoped resume action", async () => {
  localStorage.setItem("omnigent:default-transcript-view", "terminal");
  fixture.labels = { "omnigent.ui": "terminal" };
  fixture.terminalPending = true;
  const tree = () => (
    <MemoryRouter>
      <TooltipProvider>
        <RearrangeableGrid
          grid={{ id: "starting", layout: splitGrid("first", "first", "second", "right")! }}
        />
      </TooltipProvider>
    </MemoryRouter>
  );
  const rendered = render(tree());
  const first = screen.getByRole("region", { name: "first (active)" });
  expect(within(first).getByTestId("terminal-starting-up")).toBeVisible();
  expect(within(first).queryByRole("button", { name: "Resume session" })).toBeNull();
  fixture.terminalPending = false;
  rendered.rerender(tree());
  fireEvent.click(within(first).getByRole("button", { name: "Resume session" }));
  await waitFor(() => expect(retrySession).toHaveBeenCalledWith("first"));
});

it("keeps failed automatic recovery across grid view switches", async () => {
  fixture.labels = { "omnigent.ui": "terminal" };
  fixture.hostOnline = true;
  vi.mocked(retrySession).mockClear().mockRejectedValueOnce(new Error("Recovery unavailable"));
  render(
    <MemoryRouter>
      <TooltipProvider>
        <RearrangeableGrid
          grid={{ id: "recovery", layout: splitGrid("first", "first", "second", "right")! }}
        />
      </TooltipProvider>
    </MemoryRouter>,
  );
  const first = screen.getByRole("region", { name: "first (active)" });
  fireEvent.click(within(first).getByRole("button", { name: "Terminal view" }));
  const failure = await within(first).findByRole("alert");
  expect(failure).toHaveTextContent("Couldn't reconnect the terminal automatically.");
  fireEvent.click(within(first).getByRole("button", { name: "Chat view" }));
  fireEvent.click(within(first).getByRole("button", { name: "Terminal view" }));
  await act(async () => {});
  expect(within(first).getByRole("alert")).toBe(failure);
  expect(within(first).getByRole("button", { name: "Resume session" })).toBeEnabled();
  expect(retrySession).toHaveBeenCalledTimes(1);
});

it("shares one workspace, preserves per-session files and drafts, and persists grid width", () => {
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1200);
  writeSessionWorkspaceState("first", { widthPx: 320, open: false });
  writeSessionWorkspaceState("second", { widthPx: 900, open: true });
  const grid = { id: "saved", layout: splitGrid("first", "first", "second", "right")! };
  const tree = (layout = grid.layout) => (
    <MemoryRouter>
      <RearrangeableGrid grid={{ ...grid, layout }} />
    </MemoryRouter>
  );
  const view = render(tree());
  const first = screen.getByRole("region", { name: "first (active)" });
  expect(within(first).queryByTestId("view-mode-toggle")).toBeNull();
  const second = screen.getByRole("region", { name: "second" });
  expect(within(first).queryByTestId("workspace-first")).not.toBeInTheDocument();
  fireEvent.click(within(first).getByText("Open citation"));
  let workspace = screen.getByRole("complementary", { name: "Workspace" });
  expect(workspace).toHaveAttribute("data-width", "480");
  expect(workspace).toHaveTextContent("notes.md7agent-first");
  expect(workspace).toHaveAttribute("data-end-line", "9");
  fireEvent.change(within(workspace).getByRole("textbox", { name: "File draft" }), {
    target: { value: "Unsaved edit" },
  });
  fireEvent.keyDown(within(workspace).getByRole("separator"), { key: "ArrowLeft" });
  expect(workspace).toHaveAttribute("data-width", "504");
  fireEvent.click(within(second).getByRole("textbox", { name: "Message" }));
  expect(first).toHaveAttribute("data-active", "false");
  expect(second).toHaveAttribute("data-active", "true");
  workspace = screen.getByRole("complementary", { name: "Workspace" });
  expect(workspace).toHaveAttribute("data-testid", "workspace-second");
  expect(workspace).toHaveAttribute("data-width", "504");
  expect(workspace).toHaveTextContent("No file");
  fireEvent.click(within(second).getByText("Open citation"));
  fireEvent.focus(within(first).getByRole("textbox", { name: "Message" }));
  workspace = screen.getByRole("complementary", { name: "Workspace" });
  expect(within(workspace).getByRole("textbox", { name: "File draft" })).toHaveValue(
    "Unsaved edit",
  );
  fireEvent.click(within(first).getByText("Block navigation"));
  fireEvent.click(within(workspace).getByText("Open other"));
  expect(workspace).toHaveTextContent("notes.md");
  fireEvent.click(second);
  workspace = screen.getByRole("complementary", { name: "Workspace" });
  fireEvent.click(within(workspace).getByText("Open other"));
  expect(workspace).toHaveTextContent("other.md");
  fireEvent.click(within(workspace).getByText("Maximize"));
  const gridContent = screen.getByTestId("session-grid").parentElement!;
  expect(gridContent).toHaveAttribute("inert");
  expect(gridContent).toContainElement(
    screen.getByRole("button", { name: "Collapse grid workspace" }),
  );
  fireEvent.click(within(workspace).getByText("Maximize"));
  expect(gridContent).not.toHaveAttribute("inert");
  fireEvent.click(screen.getByRole("button", { name: "Collapse grid workspace" }));
  expect(screen.queryByRole("complementary", { name: "Workspace" })).not.toBeInTheDocument();
  expect(gridContent).not.toHaveAttribute("inert");
  expect(readSessionWorkspaceState("first").widthPx).toBe(320);
  expect(readSessionWorkspaceState("second").widthPx).toBe(900);
  expect(readSessionWorkspaceState("grid:saved")).toEqual({ open: false, widthPx: 504 });
  view.unmount();
  render(tree());
  fireEvent.click(screen.getByRole("button", { name: "Expand grid workspace" }));
  expect(screen.getByRole("complementary", { name: "Workspace" })).toHaveAttribute(
    "data-width",
    "504",
  );
});
