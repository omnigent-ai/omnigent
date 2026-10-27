import type { Meta, StoryObj } from "@storybook/react-vite";
import { UsageBreakdownCharts } from "./UsageBreakdownCharts";
import { usageStorySessions } from "./storyFixtures";

// Compute breakdown from sessions for stories
function computeBreakdowns(sessions: typeof usageStorySessions) {
  const harnessBreakdown: Record<string, number> = {};
  const modelBreakdown: Record<string, number> = {};

  for (const s of sessions) {
    if (s.harness) {
      harnessBreakdown[s.harness] = (harnessBreakdown[s.harness] ?? 0) + s.costUsd;
    }
    for (const [model, cost] of Object.entries(s.models)) {
      modelBreakdown[model] = (modelBreakdown[model] ?? 0) + cost;
    }
  }

  return { harnessBreakdown, modelBreakdown };
}

const populatedBreakdowns = computeBreakdowns(usageStorySessions);

const meta = {
  title: "Components/Usage/UsageBreakdownCharts",
  component: UsageBreakdownCharts,
  tags: ["visual-snapshot"],
  args: { animate: false },
  decorators: [
    (Story) => (
      <div className="w-[760px] rounded-lg border bg-card p-4">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof UsageBreakdownCharts>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Populated: Story = {
  args: populatedBreakdowns,
};

export const Empty: Story = {
  args: { harnessBreakdown: {}, modelBreakdown: {} },
};
