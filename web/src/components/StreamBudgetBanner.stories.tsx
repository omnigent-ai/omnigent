import type { Meta, StoryObj } from "@storybook/react-vite";
import { ChatStoreSeed } from "@/storybook/StoryProviders";
import { StreamBudgetBanner } from "./StreamBudgetBanner";

const meta = {
  title: "Components/Presence/StreamBudgetBanner",
  component: StreamBudgetBanner,
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      // Same floating column Transcript mounts the card in, so the snapshot
      // matches the live placement.
      <div className="relative h-48 w-[720px] overflow-hidden rounded-xl border bg-background">
        <div
          style={{ top: "calc(56px + var(--omnigent-inset-top, 0px))" }}
          className="pointer-events-none absolute inset-x-0 z-40 flex flex-col items-end gap-2 px-3"
        >
          <Story />
        </div>
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
