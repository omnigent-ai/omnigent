import type * as TerminalsModule from "@/hooks/useTerminals";
import { type ComponentProps, useContext, useState } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { FileViewerContext } from "@/shell/FileViewerContext";
import type { WorkspacePanel } from "@/shell/WorkspacePanel";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { RearrangeableGrid } from "./GridWorkspace";
import { splitGrid } from "./gridLayout";

vi.mock("@/canvas/CanvasWorkspace", () => ({ CanvasWorkspace: () => null }));
vi.mock("@/hooks/useSidebarData", () => ({ useLoadedConversations: () => ({}) }));

vi.mock("@/hooks/useSession", () => ({
  useSession: (id: string) => ({ session: { title: id, agentId: `agent-${id}` } }),
  useRootSessionId: (id: string) => id,
}));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: () => true }));
vi.mock("@/hooks/useConversationEntryState", () => ({ useConversationEntryState: () => ({}) }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({ useRunnerHealthRegistration: () => {} }));
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
  useTerminals: () => ({ terminals: [] }),
  useDeleteTerminal: () => ({}),
  terminalTabKey: () => "terminal",
}));
vi.mock("./GridSessionsContext", () => ({
  useGridSessions: () => ({ closePane: vi.fn() }),
  gridTitle: () => "Test grid",
}));
vi.mock("@/components/chat/SideChatPane", () => ({
  SideChatPane: () => {
    const context = useContext(FileViewerContext)!;
    return (
      <>
        <input aria-label="Message" />
        <button type="button" onClick={() => context.openFile("notes.md", { line: 7 })}>
          Open citation
        </button>
        <button type="button" onClick={() => context.registerNavigationGuard?.(() => {})}>
          Block navigation
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
  vi.restoreAllMocks();
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
  const second = screen.getByRole("region", { name: "second" });
  expect(within(first).queryByTestId("workspace-first")).not.toBeInTheDocument();
  fireEvent.click(within(first).getByText("Open citation"));
  let workspace = screen.getByRole("complementary", { name: "Workspace" });
  expect(workspace).toHaveAttribute("data-width", "480");
  expect(workspace).toHaveTextContent("notes.md7agent-first");
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
  expect(screen.getByTestId("session-grid")).toHaveAttribute("inert");
  fireEvent.click(screen.getByRole("button", { name: "Collapse grid workspace" }));
  expect(screen.queryByRole("complementary", { name: "Workspace" })).not.toBeInTheDocument();
  expect(screen.getByTestId("session-grid")).not.toHaveAttribute("inert");
  expect(readSessionWorkspaceState("first").widthPx).toBe(320);
  expect(readSessionWorkspaceState("second").widthPx).toBe(900);
  expect(readSessionWorkspaceState("grid:saved")).toEqual({ open: false, widthPx: 504 });
  // Removing the active session chooses a remaining cell.
  view.rerender(tree(splitGrid("first", "first", "third", "bottom")!));
  expect(first).toHaveAttribute("data-active", "true");
  view.unmount();
  render(tree());
  fireEvent.click(screen.getByRole("button", { name: "Expand grid workspace" }));
  expect(screen.getByRole("complementary", { name: "Workspace" })).toHaveAttribute(
    "data-width",
    "504",
  );
});
