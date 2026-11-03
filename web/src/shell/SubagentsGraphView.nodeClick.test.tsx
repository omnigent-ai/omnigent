import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { MouseEvent, ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { writeOpenSubagentsInPanel } from "@/lib/subagentPanelPreferences";
import { SubagentPanelOpenerContext } from "./subagentPanelOpener";
import { SubagentsGraphView } from "./SubagentsGraphView";

// The real canvas exhausts jsdom's memory; this stand-in renders one button per
// node and forwards clicks the way React Flow's `onNodeClick` does.
interface StubNode {
  id: string;
  data: { sessionId: string };
}
vi.mock("@xyflow/react", () => ({
  ReactFlow: ({
    nodes,
    onNodeClick,
  }: {
    nodes: StubNode[];
    onNodeClick: (event: MouseEvent, node: StubNode) => void;
    children?: ReactNode;
  }) => (
    <div>
      {nodes.map((node) => (
        <button key={node.id} type="button" onClick={(event) => onNodeClick(event, node)}>
          {node.data.sessionId}
        </button>
      ))}
    </div>
  ),
  Background: () => null,
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom" },
  useReactFlow: () => ({ zoomIn: vi.fn(), zoomOut: vi.fn(), fitView: vi.fn() }),
}));
vi.mock("@xyflow/react/dist/style.css", () => ({}));

const child: ChildSessionInfo = {
  id: "conv_child",
  title: "researcher:auth",
  task_summary: null,
  tool: "researcher",
  session_name: "auth",
  current_task_status: null,
  busy: false,
  last_message_preview: null,
  pending_elicitations_count: 0,
};
// Stable arrays: the view compares child lists by identity across renders.
const rootChildren = [child];
const noChildren: ChildSessionInfo[] = [];
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: (id: string | null) => ({
    children: id === "conv_root" ? rootChildren : noChildren,
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: null, isLoading: false, error: null }),
}));

function CurrentPath() {
  const location = useLocation();
  return <output data-testid="current-path">{location.pathname + location.search}</output>;
}

function renderGraph(opener: (id: string) => boolean, initialEntry = "/c/conv_root") {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <SubagentPanelOpenerContext.Provider value={opener}>
        <SubagentsGraphView conversationId="conv_root" rootSessionId="conv_root" />
      </SubagentPanelOpenerContext.Provider>
      <CurrentPath />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("SubagentsGraphView node clicks", () => {
  it("opens a sub-agent node in the Workspace panel when the setting is on", () => {
    writeOpenSubagentsInPanel(true);
    const opener = vi.fn(() => true);
    renderGraph(opener);

    fireEvent.click(screen.getByRole("button", { name: "conv_child" }));

    expect(opener).toHaveBeenCalledWith("conv_child");
    expect(screen.getByTestId("current-path")).toHaveTextContent("/c/conv_root");
  });

  it("navigates to a sub-agent node when the setting is off", () => {
    const opener = vi.fn(() => true);
    renderGraph(opener);

    fireEvent.click(screen.getByRole("button", { name: "conv_child" }));

    expect(opener).not.toHaveBeenCalled();
    expect(screen.getByTestId("current-path")).toHaveTextContent("/c/conv_child");
  });

  it("navigates when the opener refuses the node, as AppShell's does for the root", () => {
    writeOpenSubagentsInPanel(true);
    const opener = vi.fn((id: string) => id !== "conv_root");
    // Navigating to the root drops the file viewer param, so the URL shows the click landed.
    renderGraph(opener, "/c/conv_root?file=notes.md");

    fireEvent.click(screen.getByRole("button", { name: "conv_root" }));

    expect(opener).toHaveBeenCalledWith("conv_root");
    expect(screen.getByTestId("current-path").textContent).toBe("/c/conv_root");
  });
});
