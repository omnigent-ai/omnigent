import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { UsageSessionTable } from "./UsageSessionTable";
import { usageStoryNow, usageStorySessions } from "./storyFixtures";

const meta = {
  title: "Components/Usage/UsageSessionTable",
  component: UsageSessionTable,
  tags: ["visual-snapshot"],
  args: { sessions: usageStorySessions, now: usageStoryNow },
  decorators: [
    (Story) => (
      <StoryQueryRouter route="/usage">
        <div className="w-[760px] bg-card">
          <Story />
        </div>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof UsageSessionTable>;

export default meta;
type Story = StoryObj<typeof meta>;

export const DefaultSort: Story = {};

export const CostAscending: Story = {
  play: async ({ canvasElement }) => {
    const costHeader = within(canvasElement).getByText("Cost");
    await userEvent.click(costHeader);
    await userEvent.click(costHeader);
  },
};
