import type { Meta, StoryObj } from "@storybook/react-vite";
import type { Agent } from "@/hooks/useAgents";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { useChatStore } from "@/store/chatStore";
import { ChatHeader } from "./ChatHeader";

const mobileMenu = {
  fileViewerOpen: false,
  panelOpen: false,
  terminalFirst: false,
  executionLogsOpen: false,
  filesPanelOpen: false,
  subagentsPanelOpen: false,
  shellsPanelOpen: false,
  hideTerminalsTab: false,
  showShellsTab: false,
  terminalsLength: 0,
  debugMode: false,
  changedCount: 0,
  subagentsWorking: 0,
  agentCount: 1,
  onOpenFiles: () => undefined,
  onOpenChanges: () => undefined,
  onOpenShells: () => undefined,
  onOpenSubagents: () => undefined,
  onOpenMainExecutionLog: () => undefined,
};

const nativeAgent: Agent = {
  id: "agent-claude",
  name: "claude-native-ui",
  mcp_servers: [],
  policies: [],
};

const meta = {
  title: "Components/Shell/ChatHeader",
  component: ChatHeader,
  tags: ["visual-snapshot"],
  args: {
    onOpenSidebar: () => undefined,
    actionConversation: null,
    boundAgent: undefined,
    wrapperLabel: null,
    canShare: false,
    onShare: () => undefined,
    hasAgentInfo: false,
    onAgentInfo: () => undefined,
    hasHeaderMenu: false,
    showFilesPanel: false,
    hasRailContent: false,
    rightPanelOpen: false,
    onToggleRightPanel: () => undefined,
    mobileMenu,
  },
  decorators: [
    (Story) => {
      return (
        <StoryQueryRouter route="/c/conversation-story">
          <div className="relative h-20 w-[720px] overflow-visible rounded-xl border bg-background">
            <Story />
          </div>
        </StoryQueryRouter>
      );
    },
  ],
} satisfies Meta<typeof ChatHeader>;

export default meta;
type Story = StoryObj<typeof meta>;

export const FiledSessionWithPresence: Story = {
  args: {
    sidebarOpen: false,
    isChildSession: false,
    conversationId: "conversation-story",
    conversationTitle: "Fix flaky login retries",
    projectName: "Payments",
    canShare: true,
    hasRailContent: true,
    showFilesPanel: true,
  },
  decorators: [
    (Story) => {
      useChatStore.setState({
        viewers: [
          { userId: "alice@example.com", idle: false },
          { userId: "bob@example.com", idle: true },
        ],
      });
      return <Story />;
    },
  ],
};

export const ChildSessionShareDisabled: Story = {
  decorators: [
    (Story) => {
      useChatStore.setState({ viewers: [] });
      return <Story />;
    },
  ],
  args: {
    sidebarOpen: true,
    isChildSession: true,
    conversationId: "conversation-child",
    conversationTitle: "Fix flaky login retries",
    projectName: null,
    titleLinkTo: "/c/conversation-parent",
    boundAgent: nativeAgent,
    wrapperLabel: "claude-code-native-ui-subagent",
    canShare: true,
    shareDisabled: true,
    shareDisabledReason: "Sharing requires a deployed server",
    hasRailContent: true,
    rightPanelOpen: true,
  },
};
