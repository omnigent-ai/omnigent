import { type ComponentProps, useContext } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { FileViewerContext } from "@/shell/FileViewerContext";
import type { WorkspacePanel } from "@/shell/WorkspacePanel";
import { readSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { GridSessionPane } from "./GridSessionPane";

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
vi.mock("@/hooks/useTerminals", () => ({
  useTerminals: () => ({ terminals: [] }),
  useDeleteTerminal: () => ({}),
  terminalTabKey: () => "terminal",
}));
vi.mock("./GridSessionsContext", () => ({ useGridSessions: () => ({ closePane: vi.fn() }) }));
vi.mock("@/components/chat/SideChatPane", () => ({
  SideChatPane: () => {
    const context = useContext(FileViewerContext)!;
    return (
      <>
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
  WorkspacePanel: (props: ComponentProps<typeof WorkspacePanel>) => (
    <div
      data-testid={`workspace-${props.conversationId}`}
      data-open={String(props.open)}
      data-maximized={String(props.maximized)}
    >
      <span>{props.selectedFilePath ?? "No file"}</span>
      <span>{props.filePosition?.line}</span>
      <span>{props.agentId}</span>
      <button type="button" onClick={() => props.openFileViewer("other.md")}>
        Open other
      </button>
      <button type="button" onClick={() => props.onCloseFile(props.selectedFilePath!)}>
        Close file
      </button>
      <button type="button" onClick={props.onToggleMaximized}>
        Maximize
      </button>
    </div>
  ),
}));

afterEach(() => {
  cleanup();
  localStorage.clear();
});

it("isolates workspace state, citations and unsaved-navigation guards by grid cell", () => {
  render(
    <MemoryRouter>
      <section aria-label="First">
        <GridSessionPane sessionId="first" />
      </section>
      <section aria-label="Second">
        <GridSessionPane sessionId="second" />
      </section>
    </MemoryRouter>,
  );
  const first = within(screen.getByRole("region", { name: "First" }));
  const second = within(screen.getByRole("region", { name: "Second" }));
  fireEvent.click(first.getByText("Open citation"));
  expect(first.getByTestId("workspace-first")).toHaveAttribute("data-open", "true");
  expect(first.getByText("notes.md")).toBeInTheDocument();
  expect(first.getByText("7")).toBeInTheDocument();
  expect(first.getByText("agent-first")).toBeInTheDocument();
  expect(second.getByTestId("workspace-second")).toHaveAttribute("data-open", "false");
  fireEvent.click(first.getByText("Block navigation"));
  fireEvent.click(first.getByText("Open other"));
  expect(first.getByText("notes.md")).toBeInTheDocument();
  fireEvent.click(second.getByText("Open other"));
  expect(second.getByText("other.md")).toBeInTheDocument();
  fireEvent.click(second.getByText("Maximize"));
  expect(second.getByTestId("workspace-second")).toHaveAttribute("data-maximized", "true");
  expect(first.getByTestId("workspace-first")).toHaveAttribute("data-maximized", "false");
  fireEvent.click(second.getByRole("button", { name: "Collapse workspace for second" }));
  expect(second.getByTestId("workspace-second")).toHaveAttribute("data-maximized", "false");
  fireEvent.click(second.getByText("Close file"));
  expect(readSessionWorkspaceState("first").selectedFilePath).toBe("notes.md");
  expect(readSessionWorkspaceState("second").selectedFilePath).toBeNull();
});
