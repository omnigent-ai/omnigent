import type { Meta, StoryObj } from "@storybook/react-vite";
import { ChatStoreSeed } from "@/storybook/StoryProviders";
import { StreamBudgetBanner } from "./StreamBudgetBanner";

const meta = {
  title: "Components/Presence/StreamBudgetBanner",
  component: StreamBudgetBanner,
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      <div className="relative h-48 w-[720px] overflow-hidden rounded-xl border bg-background">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof StreamBudgetBanner>;

export default meta;
type Story = StoryObj<typeof meta>;

export const OverBudget: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed seed={{ streamBudgetExceeded: true, streamBudgetBannerDismissed: false }}>
        <Story />
      </ChatStoreSeed>
    ),
  ],
};

export const DismissedEpisode: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed seed={{ streamBudgetExceeded: true, streamBudgetBannerDismissed: true }}>
        <Story />
      </ChatStoreSeed>
    ),
  ],
};
