import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { SmartRoutingCard } from "./SmartRoutingCard";

const routingArguments = {
  tasks: [
    {
      title: "review-security",
      agents: [{ agent: "codex", models: null }],
      task: "Scan the fixed fixture diff.",
    },
    {
      title: "refactor-auth",
      agents: [{ agent: "claude_code", models: null }],
      task: "Refactor the fixed fixture auth flow.",
    },
  ],
};

const routingSuccess = JSON.stringify({
  recommendations: [
    {
      title: "review-security",
      agent: "codex",
      model: "databricks-claude-haiku-4-5",
      rationale: "A mechanical diff scan can use the smaller model.",
    },
    {
      title: "refactor-auth",
      agent: "claude_code",
      model: "databricks-claude-opus-4-8",
      rationale: "The multi-file refactor needs deeper reasoning.",
    },
  ],
  enforced: true,
  note: "Deterministic visual fixture.",
});

const meta = {
  title: "Components/Blocks/SmartRoutingCard",
  component: SmartRoutingCard,
  tags: ["visual-snapshot"],
  args: {
    arguments: routingArguments,
  },
} satisfies Meta<typeof SmartRoutingCard>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Judging: Story = {
  args: {
    output: null,
    state: "input-available",
  },
};

export const SizedPlan: Story = {
  args: {
    output: routingSuccess,
    state: "output-available",
  },
};

export const RawResponseExpanded: Story = {
  args: {
    output: routingSuccess,
    state: "output-available",
  },
  play: async ({ canvasElement }) => {
    await userEvent.click(
      within(canvasElement).getByRole("button", { name: "Show raw routing response" }),
    );
  },
};

export const Unavailable: Story = {
  args: {
    output: "Error: the routing advisor is unavailable; use the configured model.",
    state: "output-available",
  },
};
