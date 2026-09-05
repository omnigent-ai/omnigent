import type { Meta, StoryObj } from "@storybook/react-vite";
import type { QueuedMessage } from "@/store/chatStore";
import { QueuedMessagesStrip } from "./QueuedMessagesStrip";

const message = (queueId: string, text: string): QueuedMessage => ({
  queueId,
  text,
  conversationId: "conversation-story",
});

const meta = {
  title: "Components/Composer/QueuedMessagesStrip",
  component: QueuedMessagesStrip,
  tags: ["visual-snapshot"],
  args: {
    onDelete: () => undefined,
    onEdit: () => undefined,
    widthClassName: "max-w-[620px]",
  },
  decorators: [
    (Story) => (
      <div className="flex min-h-48 w-[680px] items-end rounded-2xl bg-muted/30 px-4 pb-8">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof QueuedMessagesStrip>;

export default meta;
type Story = StoryObj<typeof meta>;

export const WaitingForIdle: Story = {
  args: {
    messages: [message("queue-1", "Please add focused coverage for the new component state.")],
  },
};

export const ReorderableWithSteer: Story = {
  args: {
    messages: [
      message("queue-1", "Run the focused frontend test."),
      message("queue-2", "Then summarize the visual changes."),
      message("queue-3", "Finally prepare the pull request."),
    ],
    onSteer: () => undefined,
    onReorder: () => undefined,
  },
};

export const LongBacklog: Story = {
  args: {
    messages: Array.from({ length: 7 }, (_, index) =>
      message(
        `queue-${index + 1}`,
        `Queued follow-up ${index + 1}: verify this intentionally long message remains truncated inside the composer tray.`,
      ),
    ),
    onSteer: () => undefined,
  },
};
