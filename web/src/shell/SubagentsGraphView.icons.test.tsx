import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type { ComponentType, ReactNode } from "react";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type ChildSessionInfo, useChildSessions } from "@/hooks/useChildSessions";
import { useSession } from "@/hooks/useSession";
import { SubagentsGraphView } from "./SubagentsGraphView";

interface MockFlowNode {
  id: string;
  type: string;
  data: Record<string, unknown>;
}

vi.mock("@xyflow/react", () => ({
  ReactFlow: (props: Record<string, unknown>) => {
    const nodes = props.nodes as MockFlowNode[];
    const nodeTypes = props.nodeTypes as Record<
      string,
      ComponentType<{ data: Record<string, unknown> }>
    >;
    return (
      <div>
        {nodes.map((node) => {
          const NodeComponent = nodeTypes[node.type];
          return (
            <div key={node.id} data-testid={`graph-node-${node.id}`}>
              <NodeComponent data={node.data} />
            </div>
          );
        })}
        {props.children as ReactNode}
      </div>
    );
  },
  Background: () => null,
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom" },
  useReactFlow: () => ({
    zoomIn: vi.fn(),
    zoomOut: vi.fn(),
    fitView: vi.fn(),
  }),
}));

vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: vi.fn(),
}));

vi.mock("@/hooks/useSession", () => ({
  useSession: vi.fn(),
}));

vi.mock("@/lib/routing", () => ({
  useLocation: () => ({ search: "" }),
  useNavigate: () => vi.fn(),
}));

vi.mock("@/components/icons/CodexIcon", () => ({
  CodexIcon: (props: Record<string, unknown>) => <svg data-icon="codex" {...props} />,
}));

const useChildSessionsMock = vi.mocked(useChildSessions);
const useSessionMock = vi.mocked(useSession);

function childInfo(overrides: Partial<ChildSessionInfo> & { id: string }): ChildSessionInfo {
  return {
    title: null,
    task_summary: null,
    tool: null,
    session_name: null,
    current_task_status: null,
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    ...overrides,
  };
}

beforeEach(() => {
  useChildSessionsMock.mockReset();
  useSessionMock.mockReset();
});

afterEach(cleanup);

describe("SubagentsGraphView agent icons", () => {
  it("renders decorative icons before the root and child node labels", async () => {
    const child = childInfo({
      id: "conv_child",
      session_name: "find-auth",
      tool: "Explore",
      labels: { "omnigent.wrapper": "codex-native-ui-subagent" },
    });
    const rootChildren = [child];
    const noChildren: ChildSessionInfo[] = [];
    useChildSessionsMock.mockImplementation((sessionId) => ({
      children: sessionId === "conv_root" ? rootChildren : noChildren,
      isLoading: false,
      error: null,
    }));
    useSessionMock.mockReturnValue({
      session: {
        id: "conv_root",
        agentId: "ag_root",
        agentName: "codex-native-ui",
        harness: "codex-native",
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: null,
        labels: { "omnigent.wrapper": "codex-native-ui" },
        items: [],
        pendingElicitations: [],
        permissionLevel: 4,
        parentSessionId: null,
        subAgentName: null,
        kind: "default",
      },
      isLoading: false,
      error: null,
    });

    render(<SubagentsGraphView conversationId="conv_root" rootSessionId="conv_root" />);

    const rootNode = await screen.findByTestId("graph-node-conv_root");
    const childNode = await screen.findByTestId("graph-node-conv_child");
    const rootIcon = rootNode.querySelector('[data-icon="codex"]');
    const childIcon = childNode.querySelector(".lucide-search");
    const rootLabel = screen.getByText("Codex");
    const childLabel = screen.getByText("find-auth");

    expect(rootIcon).not.toBeNull();
    expect(childIcon).not.toBeNull();
    expect(rootIcon).toHaveAttribute("aria-hidden", "true");
    expect(childIcon).toHaveAttribute("aria-hidden", "true");
    expect(rootLabel.previousElementSibling).toBe(rootIcon);
    expect(childLabel.previousElementSibling).toBe(childIcon);
  });
});
