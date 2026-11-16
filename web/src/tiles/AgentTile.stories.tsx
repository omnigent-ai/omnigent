import type { Meta, StoryObj } from "@storybook/react-vite";
import type { Conversation } from "@/hooks/useConversations";
import { AgentTile } from "./AgentTile";

const conversation: Conversation = {
  id: "conv_example",
  object: "conversation",
  title: "Refactor the billing module",
  created_at: 1_700_000_000,
  updated_at: Math.floor(Date.now() / 1000) - 600,
  labels: {},
  permission_level: null,
  status: "idle",
  harness: "codex",
  workspace: "/workspace/example-app",
  git_branch: "feature/billing",
};

// Live tiles stream through the chat store, so these stories show the
// status renderings that need no session stream.
const meta = {
  title: "Tiles/AgentTile",
  component: AgentTile,
  decorators: [
    (Story) => (
      <div className="flex h-[220px] w-[380px]">
        <Story />
      </div>
    ),
  ],
  args: {
    conversation,
    active: false,
    live: false,
    variant: "card",
    onActivate: () => undefined,
    onOpenSingle: () => undefined,
  },
} satisfies Meta<typeof AgentTile>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Card: Story = {};

export const ActiveCard: Story = {
  args: { active: true },
};

export const Running: Story = {
  args: { conversation: { ...conversation, status: "running" } },
};

export const WaitingOnYou: Story = {
  args: { conversation: { ...conversation, pending_elicitations_count: 1 } },
};

export const NotStreaming: Story = {
  args: { variant: "tile" },
};
