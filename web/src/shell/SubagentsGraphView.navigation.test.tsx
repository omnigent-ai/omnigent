import type * as ChildSessionsModule from "@/hooks/useChildSessions";
import type { MouseEvent, ReactNode } from "react";
import type { Node } from "@xyflow/react";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import type { AgentNodeData } from "./subagentGraphLayout";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionNavigationTestHost } from "@/lib/sessionNavigation.test-utils";
import { canvasSessionHref } from "@/canvas/canvasNavigation";
import { useLocation } from "@/lib/routing";
import { SubagentsGraphView } from "./SubagentsGraphView";

vi.mock("@xyflow/react", () => ({
  Background: () => null,
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom" },
  useReactFlow: () => ({ zoomIn: vi.fn(), zoomOut: vi.fn(), fitView: vi.fn() }),
  ReactFlow: ({
    nodes,
    onNodeClick,
    children,
  }: {
    nodes: Node<AgentNodeData>[];
    onNodeClick: (event: MouseEvent, node: Node<AgentNodeData>) => void;
    children: ReactNode;
  }) => (
    <div>
      {nodes.map((node) => (
        <button key={node.id} type="button" onClick={(event) => onNodeClick(event, node)}>
          {node.data.sessionId}
        </button>
      ))}
      {children}
    </div>
  ),
}));

const { children, empty } = vi.hoisted(() => ({
  children: [
    {
      id: "child",
      title: null,
      task_summary: null,
      tool: "researcher",
      session_name: null,
      current_task_status: null,
      busy: false,
      last_message_preview: null,
      pending_elicitations_count: 0,
    },
  ] satisfies ChildSessionInfo[],
  empty: [] as ChildSessionInfo[],
}));

vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof ChildSessionsModule>()),
  useChildSessions: (id: string | null) => ({
    children: id === "root" ? children : empty,
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: null, isLoading: false, error: null }),
}));

afterEach(cleanup);

function LocationProbe() {
  const { pathname, search } = useLocation();
  return <output data-testid="location">{pathname + search}</output>;
}

function renderGraph(canvas: boolean) {
  const graph = <SubagentsGraphView conversationId="root" rootSessionId="root" />;
  return render(
    <MemoryRouter
      initialEntries={[
        canvas
          ? "/canvas?canvas=board&session=root&file=a&diff=1&comment=c1&view=terminal&debug=1&o=123"
          : "/c/root?file=a&diff=1&comment=c1&view=terminal&debug=1&o=123",
      ]}
    >
      <LocationProbe />
      {canvas ? (
        <SessionNavigationTestHost resolveHref={canvasSessionHref}>
          {graph}
        </SessionNavigationTestHost>
      ) : (
        graph
      )}
    </MemoryRouter>,
  );
}

describe("SubagentsGraphView navigation", () => {
  it("keeps standalone node navigation canonical and clears session view state", () => {
    renderGraph(false);
    fireEvent.click(screen.getByRole("button", { name: "child" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/c/child?debug=1&o=123");
  });

  it("selects child and root nodes inside the Canvas host", () => {
    renderGraph(true);
    fireEvent.click(screen.getByRole("button", { name: "child" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/canvas?canvas=board&debug=1&o=123&session=child&view=chat",
    );
    fireEvent.click(screen.getByRole("button", { name: "root" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/canvas?canvas=board&debug=1&o=123&session=root&view=chat",
    );
  });
});
