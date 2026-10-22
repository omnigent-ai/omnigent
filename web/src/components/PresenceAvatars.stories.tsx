import type { Meta, StoryObj } from "@storybook/react-vite";
import type { SessionViewer } from "@/lib/events";
import { ChatStoreSeed } from "@/storybook/StoryProviders";
import { PresenceAvatars } from "./PresenceAvatars";

const viewer = (userId: string, idle = false): SessionViewer => ({ userId, idle });

const meta = {
  title: "Components/Presence/PresenceAvatars",
  component: PresenceAvatars,
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      <div className="flex h-14 w-[480px] items-center justify-end rounded-xl border bg-background px-3">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof PresenceAvatars>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Alone: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed seed={{ viewers: [] }}>
        <Story />
      </ChatStoreSeed>
    ),
  ],
};

export const ActiveAndIdleMix: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed
        seed={{
          viewers: [
            viewer("alice.smith@example.com"),
            viewer("bob.jones@example.com", true),
            viewer("carol.white@example.com"),
          ],
        }}
      >
        <Story />
      </ChatStoreSeed>
    ),
  ],
};

export const OverflowChip: Story = {
  decorators: [
    (Story) => (
      <ChatStoreSeed
        seed={{
          viewers: [
            viewer("alice.smith@example.com"),
            viewer("bob.jones@example.com"),
            viewer("carol.white@example.com"),
            viewer("dan.lee@example.com", true),
            viewer("erin.kim@example.com"),
            viewer("frank.wu@example.com"),
          ],
        }}
      >
        <Story />
      </ChatStoreSeed>
    ),
  ],
};
