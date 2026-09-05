import type { Meta, StoryObj } from "@storybook/react-vite";
import { MoreHorizontalIcon } from "lucide-react";
import { expect, within } from "storybook/test";
import { Button } from "@/components/ui/button";
import type { Agent } from "@/hooks/useAgents";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { ConversationBreadcrumb } from "./ConversationBreadcrumb";

const nativeAgent: Agent = {
  id: "agent-claude",
  name: "claude-native-ui",
  mcp_servers: [],
  policies: [],
};

const meta = {
  title: "Components/Shell/ConversationBreadcrumb",
  component: ConversationBreadcrumb,
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      <StoryQueryRouter route="/c/conversation-child">
        <div className="w-[620px] rounded-lg border bg-background p-3">
          <Story />
        </div>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof ConversationBreadcrumb>;

export default meta;
type Story = StoryObj<typeof meta>;

export const FiledConversation: Story = {
  args: {
    conversationTitle: "Review the workspace navigation architecture",
    projectName: "Web application",
    isChildSession: false,
    boundAgent: undefined,
    wrapperLabel: null,
    actions: (
      <Button variant="ghost" size="icon-xs" aria-label="Conversation actions">
        <MoreHorizontalIcon />
      </Button>
    ),
  },
};

export const FiledWithEmojiIcon: Story = {
  args: {
    conversationTitle: "Review the workspace navigation architecture",
    projectName: "Web application",
    projectIcon: "🚀",
    isChildSession: false,
    boundAgent: undefined,
    wrapperLabel: null,
  },
  play: async ({ canvasElement }) => {
    // The project's emoji renders as the leading segment instead of the
    // default folder glyph.
    await expect(within(canvasElement).getByTestId("project-icon")).toHaveTextContent("🚀");
  },
};

export const NativeChildSession: Story = {
  args: {
    conversationTitle: "Review the workspace navigation architecture",
    projectName: null,
    titleLinkTo: "/c/conversation-parent",
    isChildSession: true,
    boundAgent: nativeAgent,
    wrapperLabel: "claude-code-native-ui-subagent",
  },
};
