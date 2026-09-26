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

vi.mock("@/hooks/useSession", () => ({ useSession: vi.fn() }));

vi.mock("@/lib/routing", () => ({
  useLocation: () => ({ search: "" }),
  useNavigate: () => vi.fn(),
}));

vi.mock("@/components/icons/CodexIcon", () => ({
  CodexIcon: (props: Record<string, unknown>) => <svg data-icon="codex" {...props} />,
}));
vi.mock("@/components/icons/OttoIcon", () => ({
  OttoIcon: (props: Record<string, unknown>) => <svg data-icon="otto" {...props} />,
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
  it("renders hidden icons immediately before root, branded, role, and fallback labels", async () => {
    const rootChildren = [
      childInfo({
        id: "conv_brand",
        session_name: "brand-child",
        tool: "reviewer",
        labels: { "omnigent.wrapper": "codex-native-ui" },
      }),
      childInfo({
        id: "conv_role",
        session_name: "role-child",
        tool: "Explore",
        labels: { "omnigent.wrapper": "codex-native-ui-subagent" },
      }),
      childInfo({ id: "conv_unknown", session_name: "unknown-child", tool: "general-purpose" }),
    ];
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

    const cases = [
      ["conv_root", "Codex", '[data-icon="codex"]', "Codex"],
      ["conv_brand", "brand-child", '[data-icon="codex"]', "Codex"],
      ["conv_role", "Explore", ".lucide-search", "Explore"],
      ["conv_unknown", "unknown-child", '[data-icon="otto"]', "general-purpose"],
    ] as const;
    await Promise.all(
      cases.map(async ([id, label, selector, accessibleIdentity]) => {
        const node = await screen.findByTestId(`graph-node-${id}`);
        const icon = node.querySelector(selector);
        const labelElement = screen.getByText(label);
        const identityElement = node.querySelector(".sr-only");
        expect(icon).not.toBeNull();
        expect(icon).toHaveAttribute("aria-hidden", "true");
        expect(icon).toHaveAttribute("data-testid", "agent-node-icon");
        expect(labelElement.previousElementSibling).toBe(icon);
        expect(identityElement).toHaveTextContent(`Agent identity: ${accessibleIdentity}`);
      }),
    );
  });
});
