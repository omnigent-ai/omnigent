import type { Meta, StoryObj } from "@storybook/react-vite";
import { CostTimelineChart } from "./CostTimelineChart";
import { usageStoryNow } from "./storyFixtures";

const meta = {
  title: "Components/Usage/CostTimelineChart",
  component: CostTimelineChart,
  tags: ["visual-snapshot"],
  args: { now: usageStoryNow, animate: false },
  decorators: [
    (Story) => (
      <div className="w-[720px] rounded-lg border bg-card p-4">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof CostTimelineChart>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Empty: Story = {
  args: { dailyCosts: [] },
};

export const MonthWithGaps: Story = {
  args: {
    dailyCosts: [
      { day: "2026-02-09", costUsd: 4.2 },
      { day: "2026-02-10", costUsd: 1.75 },
      { day: "2026-02-12", costUsd: 9.6 },
      { day: "2026-02-18", costUsd: 14.1 },
      { day: "2026-02-24", costUsd: 2.8 },
      { day: "2026-02-27", costUsd: 11.05 },
      { day: "2026-03-03", costUsd: 7.5 },
      { day: "2026-03-05", costUsd: 19.9 },
      { day: "2026-03-08", costUsd: 3.35 },
      { day: "2026-03-09", costUsd: 5 },
    ],
  },
};
