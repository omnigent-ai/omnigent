import type { Decorator, Meta, StoryObj } from "@storybook/react-vite";
import { userEvent } from "storybook/test";
import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import type { Session } from "@/lib/types";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { SubagentsPanel } from "./SubagentsPanel";

function child(overrides: Partial<ChildSessionInfo> & { id: string }): ChildSessionInfo {
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

function rootSession(overrides: Partial<Session> = {}): Session {
  return {
    id: "conversation-root",
    agentId: "agent-root",
    agentName: "orchestrator",
    runnerId: null,
    status: "idle",
    createdAt: 0,
    title: null,
    labels: {},
    items: [],
    pendingElicitations: [],
    permissionLevel: 4,
    parentSessionId: null,
    subAgentName: null,
    kind: "default",
    ...overrides,
  };
}

function panelEnvironment({
  activeId,
  session,
  tree,
}: {
  activeId: string;
  session: Session;
  tree: Record<string, ChildSessionInfo[]>;
}): Decorator {
  const referencedIds = new Set(
    Object.values(tree)
      .flat()
      .map((entry) => entry.id),
  );
  return (Story) => (
    <StoryQueryRouter
      route={`/c/${activeId}`}
      seed={(queryClient) => {
        queryClient.setQueryData(["session", session.id], session);
        for (const [id, children] of Object.entries(tree)) {
          queryClient.setQueryData(childSessionsQueryKey(id), children);
        }
        for (const id of referencedIds) {
          if (!(id in tree)) queryClient.setQueryData(childSessionsQueryKey(id), []);
        }
      }}
    >
      <div className="h-[520px] w-[320px] overflow-hidden rounded-lg border bg-card">
        <Story />
      </div>
    </StoryQueryRouter>
  );
}

const meta = {
  title: "Components/Shell/SubagentsPanel",
  component: SubagentsPanel,
  tags: ["visual-snapshot"],
  args: {
    rootSessionId: "conversation-root",
  },
} satisfies Meta<typeof SubagentsPanel>;

export default meta;
type Story = StoryObj<typeof meta>;

const statusTree = {
  "conversation-root": [
    child({
      id: "child-launching",
      title: "researcher:spec-scan",
      tool: "researcher",
      session_name: "spec-scan",
      current_task_status: "launching",
    }),
    child({
      id: "child-working",
      title: "frontend_engineer:rail",
      tool: "frontend_engineer",
      session_name: "rail",
      busy: true,
      last_message_preview: "Inspecting rail layout and status indicators…",
    }),
    child({
      id: "child-awaiting",
      title: "researcher:auth",
      tool: "researcher",
      session_name: "auth",
      busy: true,
      pending_elicitations_count: 1,
    }),
    child({
      id: "child-done",
      title: "Explore:find-callers",
      tool: "Explore",
      session_name: "find-callers",
      current_task_status: "completed",
      last_message_preview: "Found 14 call sites of the legacy API.",
    }),
    child({
      id: "child-failed",
      title: "pr-test-analyzer:ci",
      tool: "pr-test-analyzer",
      session_name: "ci",
      current_task_status: "failed",
      last_task_error: { code: "tool_error", message: "Tool raised ValueError" },
    }),
    child({
      id: "child-idle",
      title: "technical-writer:docs",
      tool: "technical-writer",
      session_name: "docs",
    }),
  ],
};

export const StatusSpectrum: Story = {
  args: { conversationId: "conversation-root" },
  decorators: [
    panelEnvironment({
      activeId: "conversation-root",
      session: rootSession({ status: "running", agentName: "deep-researcher" }),
      tree: statusTree,
    }),
  ],
};

const deepTree = {
  "conversation-root": [
    child({
      id: "child-coder",
      title: "frontend_engineer:rail-polish",
      tool: "frontend_engineer",
      session_name: "rail-polish",
      busy: true,
      routed_model: "databricks-claude-sonnet-5",
      last_message_preview: "Applying the depth-stepped gutter to nested rows.",
    }),
    child({
      id: "child-leaf",
      title: "researcher:audit",
      tool: "researcher",
      session_name: "audit",
      current_task_status: "completed",
    }),
  ],
  "child-coder": [
    child({
      id: "grandchild-active",
      title: "Explore:find-usages",
      tool: "Explore",
      session_name: "find-usages",
      current_task_status: "completed",
    }),
    child({
      id: "grandchild-test",
      title: "pr-test-analyzer:unit",
      tool: "pr-test-analyzer",
      session_name: "unit",
      busy: true,
    }),
  ],
  "grandchild-active": [
    child({
      id: "great-grandchild",
      title: "Explore:deep",
      tool: "Explore",
      session_name: "deep",
      current_task_status: "completed",
    }),
  ],
};

export const DeepTreeActiveGrandchild: Story = {
  args: { conversationId: "grandchild-active" },
  decorators: [
    panelEnvironment({
      activeId: "grandchild-active",
      session: rootSession({
        agentName: "claude-native-ui",
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      }),
      tree: deepTree,
    }),
  ],
};

const iconTree = {
  "conversation-root": [
    child({
      id: "child-claude",
      title: "claude_code:review",
      tool: "claude_code",
      session_name: "review",
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      busy: true,
    }),
    child({
      id: "child-codex",
      title: "codex:port-fix",
      tool: "codex",
      session_name: "port-fix",
      labels: { "omnigent.wrapper": "codex-native-ui" },
      current_task_status: "completed",
    }),
    child({ id: "child-pi", title: "pi:review-auth", tool: "pi", session_name: "review-auth" }),
    child({
      id: "child-custom",
      title: "ui:claude-native-ui:jimmy",
      tool: "claude-native-ui",
      session_name: "jimmy",
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
    }),
  ],
  "child-claude": [
    child({
      id: "child-claude-sub",
      title: "Explore:find-the-bug",
      tool: "Explore",
      busy: true,
    }),
  ],
};

export const BrandIconsCollapsed: Story = {
  args: { conversationId: "conversation-root" },
  decorators: [
    panelEnvironment({
      activeId: "conversation-root",
      session: rootSession(),
      tree: iconTree,
    }),
  ],
  play: async ({ canvasElement }) => {
    const row = canvasElement.querySelector('[data-child-session-id="child-claude"]');
    const toggle = row
      ?.closest("li")
      ?.querySelector<HTMLElement>('[data-testid="subagent-collapse-toggle"]');
    if (!toggle) throw new Error("Sub-agent collapse toggle not found");
    await userEvent.click(toggle);
  },
};
