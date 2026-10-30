import type * as TerminalsModule from "@/hooks/useTerminals";
import type * as SessionsApi from "@/lib/sessionsApi";
import type * as FilesPanelModule from "@/shell/FilesPanel";
import { type ComponentProps, useContext, useState } from "react";
import { act, cleanup, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { emitUiOpenFile } from "@/lib/uiFileRequests";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, onTestFinished, vi } from "vitest";
import { FileViewerContext } from "@/shell/FileViewerContext";
import type { WorkspacePanel } from "@/shell/WorkspacePanel";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { RearrangeableGrid } from "./GridWorkspace";
import { splitGrid } from "./gridLayout";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import { retrySession } from "@/lib/sessionsApi";
import { revealFolderInFilesPanel } from "@/shell/FilesPanel";

const fixture = vi.hoisted(() => ({
  labels: {} as Record<string, string>,
  permissionLevel: null as number | null,
  terminals: [] as TerminalsModule.TerminalInfo[],
  terminalPending: false,
  composerDisabled: false,
  hostOnline: undefined as boolean | undefined,
}));
vi.mock("@/lib/sessionsApi", async (original) => ({
  ...(await original<typeof SessionsApi>()),
  retrySession: vi.fn(async () => ({ recovered: true })),
}));
vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({ active, readOnly }: { active: boolean; readOnly: boolean }) => (
    <textarea aria-label="Terminal input" data-active={active} readOnly={readOnly} />
  ),
}));

vi.mock("@/canvas/CanvasWorkspace", () => ({ CanvasWorkspace: () => null }));
vi.mock("@/shell/FilesPanel", async (original) => ({
  ...(await original<typeof FilesPanelModule>()),
  revealFolderInFilesPanel: vi.fn(),
}));
vi.mock("@/hooks/useSidebarData", () => ({ useLoadedConversations: () => ({}) }));

vi.mock("@/hooks/useSession", () => ({
  useSession: (id: string) => ({
    session: {
      title: id,
      agentId: `agent-${id}`,
      labels: fixture.labels,
      permissionLevel: fixture.permissionLevel,
    },
  }),
  useRootSessionId: (id: string) => id,
}));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: () => true }));
vi.mock("@/hooks/useConversationEntryState", () => ({
  useConversationEntryState: () => ({ terminalPending: fixture.terminalPending }),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: () => {},
  useSessionRunnerOnline: () => true,
  useSessionHostOnline: () => fixture.hostOnline,
}));
vi.mock("@/hooks/useBrowserAgentRelay", () => ({ useBrowserAgentRelay: () => {} }));
vi.mock("@/hooks/useSessionLiveness", () => ({
  livenessRowFromSession: () => null,
  useSessionLiveness: () => ({ kind: "online" }),
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
vi.mock("./GridSessionsContext", () => ({
  useGridSessions: () => {
    const [selectedSessionId, selectSession] = useState("first");
    return { closePane: vi.fn(), selectedSessionId, selectSession };
  },
  gridTitle: () => "Test grid",
}));
vi.mock("@/components/chat/SideChatPane", () => ({
  SideChatPane: () => {
    const context = useContext(FileViewerContext)!;
    const terminal = useTerminalFirst();
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
        <button type="button" onClick={() => context.openFolder?.("/workspace/experimental")}>
          Open folder
        </button>
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
  localStorage.clear();
  sessionStorage.clear();
  fixture.labels = {};
  fixture.permissionLevel = null;
  fixture.terminals = [];
  fixture.terminalPending = false;
  fixture.composerDisabled = false;
  fixture.hostOnline = undefined;
  vi.restoreAllMocks();
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

it("browses a chat-linked folder in the Files tab of that pane's workspace", () => {
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
  expect(revealFolderInFilesPanel).not.toHaveBeenCalledWith("first", expect.anything());
  expect(screen.getByTestId("workspace-first")).toHaveAttribute("data-tab", "subagents");
  expect(screen.getByTestId("workspace-first")).toHaveAttribute("aria-hidden", "true");

  fireEvent.click(within(screen.getByRole("region", { name: "second" })).getByText("Open folder"));

  expect(revealFolderInFilesPanel).toHaveBeenCalledWith("second", "/workspace/experimental");
  const workspace = screen.getByRole("complementary", { name: "Workspace" });
  expect(workspace).toHaveAttribute("data-testid", "workspace-second");
  expect(workspace).toHaveAttribute("data-tab", "files");
  expect(workspace).toHaveTextContent("No file");
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
