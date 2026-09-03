import type { Meta, StoryObj } from "@storybook/react-vite";
import { UsageBreakdownCharts } from "./UsageBreakdownCharts";
import { usageStorySessions } from "./storyFixtures";

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
  args: { sessions: usageStorySessions },
};

export const Empty: Story = {
  args: { sessions: [] },
};
