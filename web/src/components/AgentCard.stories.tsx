import type { Meta, StoryObj } from "@storybook/react-vite";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { AgentCard } from "./AgentCard";

const claudeAgent: AvailableAgent = {
  id: "agent-claude",
  name: "claude-native-ui",
  display_name: "Claude Code",
  description: "Anthropic's coding agent for complex repository work.",
  harness: "claude-native",
  skills: [],
};

const customAgent: AvailableAgent = {
  id: "agent-reviewer",
  name: "reviewer",
  display_name: "Code Reviewer",
  description: "Reviews a change for correctness, maintainability, and test coverage.",
  harness: null,
  skills: [],
};

const meta = {
  title: "Components/AgentCard",
  component: AgentCard,
  tags: ["visual-snapshot"],
  args: {
    agent: claudeAgent,
    selected: false,
    onSelect: () => undefined,
  },
} satisfies Meta<typeof AgentCard>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};

export const Selected: Story = {
  args: { selected: true },
};

export const Compact: Story = {
  args: { compact: true },
};

export const CustomAgent: Story = {
  args: { agent: customAgent },
};
